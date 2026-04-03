<<<<<<< HEAD
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
=======
import json
from html import escape

import streamlit as st

from database import SessionLocal
from tailoredresearch import conduct_research


st.set_page_config(page_title="Research Hub", page_icon="🔬", layout="wide")

st.markdown(
    """
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Montserrat:wght@500;700;800&display=swap');

        :root {
            --rh-bg-0: #050505;
            --rh-bg-1: #0d0d0d;
            --rh-card: #161616;
            --rh-card-soft: #202020;
            --rh-text: #f6f6f6;
            --rh-muted: #adadad;
            --rh-accent: #1ed760;
            --rh-accent-soft: #15b24f;
            --rh-pink: #ff4fd8;
            --rh-cyan: #2dd4ff;
            --rh-amber: #f7b500;
        }

        .stApp {
            background:
                radial-gradient(circle at 12% 8%, rgba(45, 212, 255, 0.22) 0%, rgba(45, 212, 255, 0) 34%),
                radial-gradient(circle at 85% 6%, rgba(255, 79, 216, 0.2) 0%, rgba(255, 79, 216, 0) 38%),
                radial-gradient(circle at 50% 82%, rgba(30, 215, 96, 0.17) 0%, rgba(30, 215, 96, 0) 32%),
                linear-gradient(180deg, var(--rh-bg-1) 0%, var(--rh-bg-0) 100%);
            color: var(--rh-text);
            font-family: 'Montserrat', sans-serif;
        }

        [data-testid="stHeader"] {
            background: transparent;
        }

        [data-testid="stSidebar"] {
            border-right: 1px solid #2a2a2a;
            background: linear-gradient(180deg, #111111 0%, #0a0a0a 100%);
        }

        div[data-testid="stForm"] {
            border-radius: 18px;
            border: 1px solid #2b2b2b;
            background: rgba(20, 20, 20, 0.84);
            backdrop-filter: blur(5px);
            padding: 1rem 1.2rem 0.4rem 1.2rem;
            margin-bottom: 1rem;
        }

        div[data-testid="stMetric"] {
            border: 1px solid #323232;
            border-radius: 16px;
            background: var(--rh-card);
            padding: 0.8rem;
        }

        .rh-hero {
            background: linear-gradient(120deg, #1db954 0%, #2dd4ff 42%, #ff4fd8 100%);
            border-radius: 22px;
            padding: 1.6rem;
            border: 1px solid #2f2f2f;
            box-shadow: 0 8px 28px rgba(0, 0, 0, 0.35);
            margin-bottom: 1rem;
        }

        .rh-hero h1 {
            margin: 0;
            font-size: 2rem;
            font-weight: 800;
            letter-spacing: 0.4px;
        }

        .rh-hero p {
            margin-top: 0.45rem;
            margin-bottom: 0;
            color: #f3fff7;
        }

        .rh-card {
            border: 1px solid #3a3a3a;
            background:
                linear-gradient(180deg, var(--rh-card-soft) 0%, var(--rh-card) 100%),
                linear-gradient(90deg, rgba(45, 212, 255, 0.12) 0%, rgba(255, 79, 216, 0.1) 100%);
            border-radius: 16px;
            padding: 1rem;
            margin-bottom: 0.8rem;
            box-shadow: 0 6px 18px rgba(0, 0, 0, 0.28);
        }

        .rh-title {
            font-weight: 700;
            font-size: 1.05rem;
            margin: 0;
        }

        .rh-meta {
            color: var(--rh-muted);
            font-size: 0.88rem;
            margin-top: 0.35rem;
            margin-bottom: 0.55rem;
        }

        .rh-badge {
            display: inline-block;
            border-radius: 999px;
            padding: 0.2rem 0.55rem;
            font-size: 0.76rem;
            font-weight: 700;
            margin-right: 0.45rem;
            color: #0a0a0a;
            background: var(--rh-accent);
        }

        .rh-badge.europe_pmc { background: var(--rh-cyan); }
        .rh-badge.openalex { background: var(--rh-pink); }
        .rh-badge.pubmed { background: var(--rh-amber); }

        .rh-link a {
            color: var(--rh-accent) !important;
            text-decoration: none;
            font-weight: 700;
        }

        .rh-link a:hover {
            color: #8af8af !important;
        }

        .stButton > button,
        [data-testid="stFormSubmitButton"] > button {
            border-radius: 999px;
            border: none;
            font-weight: 800;
            background: linear-gradient(90deg, var(--rh-accent) 0%, var(--rh-accent-soft) 100%);
            color: #0b0b0b;
            transition: transform 120ms ease, box-shadow 120ms ease;
        }

        .stButton > button:hover,
        [data-testid="stFormSubmitButton"] > button:hover {
            transform: translateY(-1px);
            box-shadow: 0 8px 22px rgba(30, 215, 96, 0.32);
        }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown(
    """
    <div class="rh-hero">
      <h1>Research Hub</h1>
            <p>Discovery flow for PubMed, Europe PMC, and OpenAlex.</p>
    </div>
    """,
    unsafe_allow_html=True,
)

left_col, right_col = st.columns([2.0, 1.0], gap="large")

with left_col:
    with st.form("research_form"):
        note_name = st.text_input("Note name", placeholder="e.g., mRNA delivery optimization")
        research_focus = st.text_area(
            "Research focus",
            placeholder="Describe the topic or paste keywords.",
            height=145,
        )
        source = st.selectbox(
            "Data source",
            options=["both", "europe_pmc", "openalex", "pubmed"],
            index=0,
            help="Use both to query Europe PMC and OpenAlex in one run.",
        )
        limit = st.slider("Max papers", min_value=1, max_value=20, value=6)
        submitted = st.form_submit_button("Run research")

with right_col:
        st.markdown("### Sources")
        st.markdown(
                """
                <div class="rh-card">
                    <span class="rh-badge pubmed">pubmed</span>
                    <span class="rh-badge europe_pmc">europe_pmc</span>
                    <span class="rh-badge openalex">openalex</span>
                    <p class="rh-meta" style="margin-top:0.7rem;">Use the selector to query one source or combine multiple in one run.</p>
                </div>
                """,
                unsafe_allow_html=True,
        )

if submitted:
    if not note_name.strip() or not research_focus.strip():
        st.error("Please provide both a note name and a research focus.")
    else:
        db = SessionLocal()
        try:
            with st.spinner("Querying selected source(s) and saving note..."):
                result = conduct_research(
                    note_name=note_name.strip(),
                    research_focus=research_focus.strip(),
                    limit=limit,
                    source=source,
                    db=db,
                )

            st.success(f"Saved note: {result['note_saved_as']}")

            m1, m2 = st.columns(2)
            with m1:
                st.metric("Papers found", result.get("papers_found", 0))
            with m2:
                st.metric("Sources", len(result.get("sources_queried", [])))

            st.write("Sources queried: " + ", ".join(result.get("sources_queried", [])))

            primary_reference = result.get("primary_reference")
            if primary_reference and primary_reference.startswith("http"):
                st.markdown(f"Primary reference: [{primary_reference}]({primary_reference})")
            else:
                st.write(f"Primary reference: {primary_reference}")

            papers = result.get("papers")
            if isinstance(papers, list) and papers:
                st.subheader("Top papers")
                for paper in papers:
                    title = escape(paper.get("title", "No Title"))
                    source_name = escape(str(paper.get("source", "unknown")))
                    journal = escape(str(paper.get("journal", "Unknown Journal")))
                    year = escape(str(paper.get("year", "Unknown Year")))
                    authors = paper.get("authors") or []
                    authors_str = escape(", ".join(authors[:7]) + ("..." if len(authors) > 7 else "")) if authors else "N/A"
                    summary = escape(str(paper.get("summary", "No Abstract available")))
                    link = paper.get("link")

                    st.markdown(
                        f"""
                        <div class="rh-card">
                            <span class="rh-badge {source_name}">{source_name}</span>
                            <p class="rh-title">{title}</p>
                            <p class="rh-meta">{journal} | {year} | {authors_str}</p>
                            <p>{summary}</p>
                        </div>
                        """,
                        unsafe_allow_html=True,
                    )
                    if link:
                        safe_link = escape(str(link), quote=True)
                        st.markdown(
                            f"<p class='rh-link'><a href='{safe_link}' target='_blank'>Open paper</a></p>",
                            unsafe_allow_html=True,
                        )
            else:
                st.info("No relevant papers found for this topic.")

            with st.expander("Raw output JSON"):
                st.code(json.dumps(result, indent=2, ensure_ascii=False), language="json")
        finally:
            db.close()
>>>>>>> ebb81e2 (Initial commit)
