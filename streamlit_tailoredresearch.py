from fastapi import FastAPI, Depends
from sqlalchemy.orm import Session
from database import SessionLocal, LabNote
from Bio import Entrez, Medline
import datetime
import json
import os

import requests

app = FastAPI(title="PubMed Research Hub")

# Set Entrez email from environment or default
Entrez.email = os.getenv("ENTREZ_EMAIL", "kobe.q.anderson@gmail.com")

SUPPORTED_SOURCES = {"pubmed", "europe_pmc", "openalex", "both"}


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


def _search_pubmed(search_term: str, limit: int) -> tuple[list[dict], str]:
    handle = Entrez.esearch(db="pubmed", term=search_term, retmax=limit, sort="pub_date")
    record = Entrez.read(handle)
    ids = record.get("IdList", [])

    papers_found = []
    if ids:
        fetch_handle = Entrez.efetch(db="pubmed", id=",".join(ids), rettype="medline", retmode="text")
        medline_records = list(Medline.parse(fetch_handle))
        for rec in medline_records:
            paper = {
                "source": "pubmed",
                "title": rec.get("TI", "No Title"),
                "summary": _clip_summary(rec.get("AB", "No Abstract available")),
                "authors": rec.get("AU", []),
                "journal": rec.get("JT", "Unknown Journal"),
                "year": rec.get("DP", "Unknown Year"),
                "link": f"https://pubmed.ncbi.nlm.nih.gov/{rec.get('PMID', '')}/" if rec.get("PMID") else None,
            }
            papers_found.append(paper)
        ref_url = f"https://pubmed.ncbi.nlm.nih.gov/{ids[0]}/"
    else:
        ref_url = "No relevant papers found"

    return papers_found, ref_url


def _search_europe_pmc(search_term: str, limit: int) -> tuple[list[dict], str]:
    endpoint = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
    response = requests.get(
        endpoint,
        params={
            "query": search_term,
            "pageSize": limit,
            "format": "json",
            "sort": "date_desc",
        },
        timeout=20,
    )
    response.raise_for_status()

    payload = response.json()
    records = payload.get("resultList", {}).get("result", [])

    papers_found = []
    for rec in records:
        source_tag = rec.get("source", "MED")
        article_id = rec.get("id")
        link = f"https://europepmc.org/article/{source_tag}/{article_id}" if article_id else None
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

    ref_url = papers_found[0]["link"] if papers_found and papers_found[0].get("link") else "No relevant papers found"
    return papers_found, ref_url


def _search_openalex(search_term: str, limit: int) -> tuple[list[dict], str]:
    endpoint = "https://api.openalex.org/works"
    response = requests.get(
        endpoint,
        params={
            "search": search_term,
            "per-page": limit,
            "sort": "publication_date:desc",
        },
        timeout=20,
    )
    response.raise_for_status()

    payload = response.json()
    records = payload.get("results", [])

    papers_found = []
    for rec in records:
        authors = []
        for authorship in rec.get("authorships", []):
            display_name = authorship.get("author", {}).get("display_name")
            if display_name:
                authors.append(display_name)

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

    ref_url = papers_found[0]["link"] if papers_found and papers_found[0].get("link") else "No relevant papers found"
    return papers_found, ref_url

def get_db():
    db = SessionLocal()
    try: yield db
    finally: db.close()

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
            "message": f"Unsupported source '{source}'. Use one of: {', '.join(sorted(SUPPORTED_SOURCES))}",
        }

    # Use RESEARCH_FOCUS env variable if present
    # Use RESEARCH_KEYWORDS env variable if present (comma-separated)
    env_keywords = os.getenv("RESEARCH_KEYWORDS")
    if env_keywords:
        keywords = [k.strip() for k in env_keywords.split(",") if k.strip()]
        search_term = " OR ".join(keywords)
    else:
        search_term = research_focus

    sources_to_query = ["europe_pmc", "openalex"] if normalized_source == "both" else [normalized_source]

    papers_found = []
    primary_reference = "No relevant papers found"
    source_summaries = []

    for source_name in sources_to_query:
        try:
            if source_name == "pubmed":
                source_papers, source_ref = _search_pubmed(search_term=search_term, limit=limit)
            elif source_name == "europe_pmc":
                source_papers, source_ref = _search_europe_pmc(search_term=search_term, limit=limit)
            else:
                source_papers, source_ref = _search_openalex(search_term=search_term, limit=limit)

            if source_ref != "No relevant papers found" and primary_reference == "No relevant papers found":
                primary_reference = source_ref

            papers_found.extend(source_papers)
            source_summaries.append({"source": source_name, "results": len(source_papers), "reference": source_ref})
        except Exception as exc:
            source_summaries.append({"source": source_name, "results": 0, "reference": None, "error": str(exc)})


    # 2. Save the session to your local database
    new_entry = LabNote(
        title=note_name, 
        content=(
            f"Focus: {research_focus}\n"
            f"Search term: {search_term}\n"
            f"Sources: {', '.join(sources_to_query)}\n"
            f"Top Results: {len(papers_found)} papers found."
        ),
        pubmed_ref=primary_reference
    )
    db.add(new_entry)
    db.commit()

    output = {
        "status": "Success",
        "note_saved_as": note_name,
        "timestamp": datetime.datetime.now().isoformat(),
        "source": normalized_source,
        "sources_queried": sources_to_query,
        "search_term": search_term,
        "original_research_focus": research_focus,
        "primary_reference": primary_reference,
        "papers_found": len(papers_found),
        "papers": papers_found if papers_found else "No relevant papers found for this topic.",
        "input_news_summary": research_focus,
        "details": {
            "search_term_used": search_term,
            "input_title": note_name,
            "input_content": research_focus,
            "source_breakdown": source_summaries,
            "primary_reference": primary_reference,
        },
        "summary": (
            f"Research focus: {search_term}\n"
            f"Input title: {note_name}\n"
            f"Input content: {research_focus}\n"
            f"Sources: {', '.join(sources_to_query)}\n"
            f"Top Results: {len(papers_found)} papers found.\n"
            + ("No API results, but here is the original prompt/news: " + research_focus if not papers_found else "")
        )
    }
    # Save output to tailorednote_output.txt
    output_path = os.path.abspath("tailorednote_output.txt")
    try:
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)
        print(f"[INFO] Output saved to: {output_path}")
    except Exception as e:
        print(f"[ERROR] Could not write output file: {e}")
    return output
