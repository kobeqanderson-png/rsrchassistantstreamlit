from fastapi import FastAPI, Depends
from sqlalchemy.orm import Session
from database import SessionLocal, LabNote
from Bio import Entrez, Medline
import datetime
import json
import os
import re

import requests

app = FastAPI(title="PubMed Research Hub")

Entrez.email = os.getenv("ENTREZ_EMAIL", "kobe.q.anderson@gmail.com")

SUPPORTED_SOURCES = {"pubmed", "europe_pmc", "openalex", "both", "all"}

# Common English stop words to strip when building search queries.
# Deliberately excludes biomedical terms so they are never filtered out.
_STOP_WORDS = {
    "a", "an", "the", "and", "or", "not", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "as", "is", "was", "are", "were", "be",
    "been", "being", "have", "has", "had", "do", "does", "did", "will",
    "would", "could", "should", "may", "might", "shall", "can",
    "that", "this", "these", "those", "i", "we", "you", "he", "she", "it",
    "they", "my", "our", "your", "his", "her", "its", "their",
    "what", "which", "who", "when", "where", "how", "why",
    "all", "both", "each", "few", "more", "most", "other", "some", "such",
    "into", "through", "during", "before", "after", "above", "below",
    "between", "out", "off", "then", "here", "there", "about",
    "if", "than", "so", "but", "also", "just", "because", "while",
    "although", "however", "therefore", "thus", "hence", "whereas",
    "please", "want", "get", "am", "very", "too", "any", "its",
    "using", "used", "use", "based", "within", "without", "across",
    "looking", "find", "related", "regarding", "concerning",
    "investigating", "papers", "articles", "literature",
    "focus", "topic", "subject", "current", "recent",
}


def _clip_summary(text: str, limit: int = 500) -> str:
    if not text:
        return "No Abstract available"
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _openalex_abstract_from_index(index_obj: dict | None) -> str:
    if not index_obj:
        return "No Abstract available"

    indexed_words: list[tuple[int, str]] = []
    for word, positions in index_obj.items():
        for pos in positions:
            indexed_words.append((pos, word))

    if not indexed_words:
        return "No Abstract available"

    indexed_words.sort(key=lambda item: item[0])
    return " ".join(word for _, word in indexed_words)


def _extract_key_terms(text: str, max_terms: int = 8) -> list[str]:
    """
    Extract meaningful keywords from a free-text research description.

    Removes punctuation (except hyphens), lowercases, strips stop words and
    very short tokens, then deduplicates while preserving order.
    """
    cleaned = re.sub(r"[^\w\s\-]", " ", text.lower())
    tokens = cleaned.split()

    terms: list[str] = []
    seen: set[str] = set()
    for t in tokens:
        if t not in _STOP_WORDS and len(t) > 2 and not t.isdigit() and t not in seen:
            seen.add(t)
            terms.append(t)
        if len(terms) == max_terms:
            break

    return terms


def _build_pubmed_query(research_focus: str) -> str:
    """
    Build an optimized PubMed query with [Title/Abstract] field tags.

    Strategy:
      - Respect RESEARCH_KEYWORDS env var if set.
      - For short inputs (<= 4 meaningful words): search as-is.
      - For longer descriptions: extract key terms, require the top 4 via AND,
        then allow any additional terms via OR.
    Quoted terms improve PubMed phrase matching.
    """
    env_keywords = os.getenv("RESEARCH_KEYWORDS")
    if env_keywords:
        keywords = [k.strip() for k in env_keywords.split(",") if k.strip()]
        return " AND ".join(f'"{k}"[Title/Abstract]' for k in keywords)

    terms = _extract_key_terms(research_focus, max_terms=7)
    if not terms:
        return research_focus

    tagged = [f'"{t}"[Title/Abstract]' for t in terms]

    if len(tagged) <= 4:
        return " AND ".join(tagged)

    core = " AND ".join(tagged[:4])
    extras = " OR ".join(tagged[4:])
    return f"({core}) AND ({extras})"


def _build_europe_pmc_query(research_focus: str) -> str:
    """
    Build an optimized Europe PMC query using TITLE/ABSTRACT field syntax.

    Europe PMC supports field-specific searches: TITLE:"term" ABSTRACT:"term".
    We AND the top 4 terms (required) and OR any remaining ones (broadening).
    """
    env_keywords = os.getenv("RESEARCH_KEYWORDS")
    if env_keywords:
        keywords = [k.strip() for k in env_keywords.split(",") if k.strip()]
        parts = [f'(TITLE:"{k}" OR ABSTRACT:"{k}")' for k in keywords]
        return " AND ".join(parts)

    terms = _extract_key_terms(research_focus, max_terms=6)
    if not terms:
        return research_focus

    core_terms = terms[:4]
    extra_terms = terms[4:]

    core_parts = [f'(TITLE:"{t}" OR ABSTRACT:"{t}")' for t in core_terms]
    query = " AND ".join(core_parts)

    if extra_terms:
        extra_parts = [f'(TITLE:"{t}" OR ABSTRACT:"{t}")' for t in extra_terms]
        query = f"({query}) AND ({' OR '.join(extra_parts)})"

    return query


def _build_openalex_query(research_focus: str) -> str:
    """
    Build a clean keyword string for OpenAlex full-text search.

    OpenAlex's `search` parameter matches against title + abstract.
    A space-separated list of key terms works better than raw natural language.
    """
    env_keywords = os.getenv("RESEARCH_KEYWORDS")
    if env_keywords:
        return " ".join(k.strip() for k in env_keywords.split(",") if k.strip())

    terms = _extract_key_terms(research_focus, max_terms=6)
    return " ".join(terms) if terms else research_focus


def _search_pubmed(research_focus: str, limit: int) -> tuple[list[dict], str, str]:
    search_term = _build_pubmed_query(research_focus)

    handle = Entrez.esearch(db="pubmed", term=search_term, retmax=limit)
    record = Entrez.read(handle)
    ids = record.get("IdList", [])

    # Fallback to raw focus text if optimized query returns nothing
    if not ids and search_term != research_focus:
        handle = Entrez.esearch(db="pubmed", term=research_focus, retmax=limit)
        record = Entrez.read(handle)
        ids = record.get("IdList", [])
        search_term = research_focus

    papers_found: list[dict] = []
    if ids:
        fetch_handle = Entrez.efetch(
            db="pubmed", id=",".join(ids), rettype="medline", retmode="text"
        )
        medline_records = list(Medline.parse(fetch_handle))
        for rec in medline_records:
            paper = {
                "source": "pubmed",
                "title": rec.get("TI", "No Title"),
                "summary": _clip_summary(rec.get("AB", "No Abstract available")),
                "authors": rec.get("AU", []),
                "journal": rec.get("JT", "Unknown Journal"),
                "year": rec.get("DP", "Unknown Year"),
                "link": (
                    f"https://pubmed.ncbi.nlm.nih.gov/{rec.get('PMID', '')}/"
                    if rec.get("PMID")
                    else None
                ),
            }
            papers_found.append(paper)
        ref_url = f"https://pubmed.ncbi.nlm.nih.gov/{ids[0]}/"
    else:
        ref_url = "No relevant papers found"

    return papers_found, ref_url, search_term


def _search_europe_pmc(research_focus: str, limit: int) -> tuple[list[dict], str, str]:
    search_term = _build_europe_pmc_query(research_focus)
    endpoint = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"

    response = requests.get(
        endpoint,
        params={
            "query": search_term,
            "pageSize": limit,
            "format": "json",
            "resultType": "core",  # returns full records including abstractText
        },
        timeout=20,
    )
    response.raise_for_status()

    payload = response.json()
    records = payload.get("resultList", {}).get("result", [])

    # Fallback to raw focus text if optimized query returns nothing
    if not records and search_term != research_focus:
        response = requests.get(
            endpoint,
            params={
                "query": research_focus,
                "pageSize": limit,
                "format": "json",
                "resultType": "core",
            },
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        records = payload.get("resultList", {}).get("result", [])
        search_term = research_focus

    papers_found: list[dict] = []
    for rec in records:
        source_tag = rec.get("source", "MED")
        article_id = rec.get("id")
        link = (
            f"https://europepmc.org/article/{source_tag}/{article_id}"
            if article_id
            else None
        )
        paper = {
            "source": "europe_pmc",
            "title": rec.get("title", "No Title"),
            "summary": _clip_summary(rec.get("abstractText", "No Abstract available")),
            "authors": [rec.get("authorString")] if rec.get("authorString") else [],
            "journal": rec.get("journalTitle", "Unknown Journal"),
            "year": rec.get("pubYear", "Unknown Year"),
            "link": link,
        }
        papers_found.append(paper)

    ref_url = (
        papers_found[0]["link"]
        if papers_found and papers_found[0].get("link")
        else "No relevant papers found"
    )
    return papers_found, ref_url, search_term


def _search_openalex(research_focus: str, limit: int) -> tuple[list[dict], str, str]:
    search_term = _build_openalex_query(research_focus)
    endpoint = "https://api.openalex.org/works"

    response = requests.get(
        endpoint,
        params={
            "search": search_term,
            "per-page": limit,
            "sort": "relevance_score:desc",
            "select": "id,display_name,authorships,primary_location,publication_year,abstract_inverted_index",
        },
        timeout=20,
    )
    response.raise_for_status()

    payload = response.json()
    records = payload.get("results", [])

    # Fallback to raw focus text if optimized query returns nothing
    if not records and search_term != research_focus:
        response = requests.get(
            endpoint,
            params={
                "search": research_focus,
                "per-page": limit,
                "sort": "relevance_score:desc",
                "select": "id,display_name,authorships,primary_location,publication_year,abstract_inverted_index",
            },
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        records = payload.get("results", [])
        search_term = research_focus

    papers_found: list[dict] = []
    for rec in records:
        authors = [
            authorship.get("author", {}).get("display_name")
            for authorship in rec.get("authorships", [])
            if authorship.get("author", {}).get("display_name")
        ]

        journal = (
            rec.get("primary_location", {})
            .get("source", {})
            .get("display_name", "Unknown Journal")
        )
        summary = _openalex_abstract_from_index(rec.get("abstract_inverted_index"))

        paper = {
            "source": "openalex",
            "title": rec.get("display_name", "No Title"),
            "summary": _clip_summary(summary),
            "authors": authors,
            "journal": journal,
            "year": rec.get("publication_year", "Unknown Year"),
            "link": rec.get("id"),
        }
        papers_found.append(paper)

    ref_url = (
        papers_found[0]["link"]
        if papers_found and papers_found[0].get("link")
        else "No relevant papers found"
    )
    return papers_found, ref_url, search_term


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@app.post("/research")
def conduct_research(
    note_name: str,
    research_focus: str,
    limit: int = 5,
    source: str = "pubmed",
    db: Session = Depends(get_db),
):
    """
    Input a note name, research focus, and source.
    Queries supported literature APIs and saves to the ELN database.
    """
    normalized_source = source.strip().lower()
    if normalized_source not in SUPPORTED_SOURCES:
        return {
            "status": "Error",
            "message": (
                f"Unsupported source '{source}'. "
                f"Use one of: {', '.join(sorted(SUPPORTED_SOURCES))}"
            ),
        }

    if normalized_source == "all":
        sources_to_query = ["pubmed", "europe_pmc", "openalex"]
    elif normalized_source == "both":
        sources_to_query = ["europe_pmc", "openalex"]
    else:
        sources_to_query = [normalized_source]

    papers_found: list[dict] = []
    primary_reference = "No relevant papers found"
    source_summaries: list[dict] = []
    queries_used: dict[str, str] = {}

    for source_name in sources_to_query:
        try:
            if source_name == "pubmed":
                source_papers, source_ref, query_used = _search_pubmed(
                    research_focus=research_focus, limit=limit
                )
            elif source_name == "europe_pmc":
                source_papers, source_ref, query_used = _search_europe_pmc(
                    research_focus=research_focus, limit=limit
                )
            else:
                source_papers, source_ref, query_used = _search_openalex(
                    research_focus=research_focus, limit=limit
                )

            queries_used[source_name] = query_used

            if source_ref != "No relevant papers found" and primary_reference == "No relevant papers found":
                primary_reference = source_ref

            papers_found.extend(source_papers)
            source_summaries.append(
                {
                    "source": source_name,
                    "results": len(source_papers),
                    "reference": source_ref,
                    "query_used": query_used,
                }
            )
        except Exception as exc:
            source_summaries.append(
                {"source": source_name, "results": 0, "reference": None, "error": str(exc)}
            )

    new_entry = LabNote(
        title=note_name,
        content=(
            f"Focus: {research_focus}\n"
            f"Sources: {', '.join(sources_to_query)}\n"
            f"Top Results: {len(papers_found)} papers found."
        ),
        pubmed_ref=primary_reference,
    )
    db.add(new_entry)
    db.commit()

    output = {
        "status": "Success",
        "note_saved_as": note_name,
        "timestamp": datetime.datetime.now().isoformat(),
        "source": normalized_source,
        "sources_queried": sources_to_query,
        "original_research_focus": research_focus,
        "queries_used": queries_used,
        "primary_reference": primary_reference,
        "papers_found": len(papers_found),
        "papers": papers_found if papers_found else "No relevant papers found for this topic.",
        "details": {
            "input_title": note_name,
            "input_content": research_focus,
            "source_breakdown": source_summaries,
            "primary_reference": primary_reference,
        },
    }

    output_path = os.path.abspath("tailorednote_output.txt")
    try:
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[ERROR] Could not write output file: {e}")

    return output
