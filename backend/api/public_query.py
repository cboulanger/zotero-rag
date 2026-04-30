"""
Public web UI for unauthenticated RAG queries against publicly readable Zotero libraries.

Enabled by setting PUBLIC_LIBRARIES_CONFIG in .env to a JSON file path containing:
  {
    "users/{userId}":  { "title": "...", "description": "..." },
    "groups/{groupId}": { "title": "...", "description": "..." }
  }

Routes:
  GET  /public/                           — index page listing all configured libraries
  GET  /public/{library_type}/{library_id} — query form for a single library
  POST /public/{library_type}/{library_id} — submit question, show results
"""

import json
import logging
import re
import asyncio
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Form
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt

from backend.config.settings import get_settings
from backend.dependencies import get_vector_store, make_embedding_service, make_llm_service
from backend.db.vector_store import VectorStore
from backend.services.rag_engine import RAGEngine, SourceInfo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/public")

_templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def _normalize_slug(slug: str) -> str:
    """Normalize a Zotero slug to just {type}/{numericId}.

    Strips the optional short-name suffix that Zotero appends to group URLs,
    e.g. "groups/2224334/ag_graphen__netzwerke" → "groups/2224334".
    """
    parts = slug.strip("/").split("/")
    return f"{parts[0]}/{parts[1]}"


def _load_public_config() -> dict[str, dict]:
    """Load the public libraries config from the JSON file specified in settings.

    Returns an empty dict when PUBLIC_LIBRARIES_CONFIG is unset or unreadable.
    Slugs are normalized to {type}/{numericId} regardless of how they appear in the file.
    """
    settings = get_settings()
    path = settings.public_libraries_config
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        result = {}
        for k, v in raw.items():
            if not (k.startswith("users/") or k.startswith("groups/")):
                continue  # skip documentation/comment keys
            result[_normalize_slug(k)] = v
        return result
    except Exception as e:
        logger.warning(f"[public_query] Could not load PUBLIC_LIBRARIES_CONFIG ({path}): {e}")
        return {}


# ---------------------------------------------------------------------------
# Slug ↔ backend library ID helpers
# ---------------------------------------------------------------------------

def slug_to_backend_id(slug: str) -> str:
    """Convert a Zotero.org slug to the backend library ID format.

    users/12345 → u12345
    groups/678  → 678
    """
    if slug.startswith("users/"):
        return "u" + slug[6:]
    if slug.startswith("groups/"):
        return slug[7:]
    raise ValueError(f"Invalid library slug: {slug!r}")


def backend_id_to_slug(backend_id: str) -> str:
    """Convert a backend library ID to a Zotero.org slug.

    u12345 → users/12345
    678    → groups/678
    """
    if backend_id.startswith("u"):
        return "users/" + backend_id[1:]
    return "groups/" + backend_id


# ---------------------------------------------------------------------------
# Zotero web API helpers
# ---------------------------------------------------------------------------

ZOTERO_API_BASE = "https://api.zotero.org"
ZOTERO_WEB_BASE = "https://www.zotero.org"
_META_FETCH_TIMEOUT = 5.0


async def _fetch_item_metadata(client: httpx.AsyncClient, slug: str, item_id: str) -> Optional[dict]:
    """Fetch item metadata from the Zotero web API for a public library.

    Returns the parsed JSON dict or None on error/timeout.
    """
    url = f"{ZOTERO_API_BASE}/{slug}/items/{item_id}?format=json"
    try:
        resp = await client.get(url, timeout=_META_FETCH_TIMEOUT)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


def _format_display_text(meta: Optional[dict], src: SourceInfo) -> str:
    """Format author/year citation display text.

    Prefers Zotero web API metadata when available; falls back to author/year
    stored in the vector index, and only uses the title as a last resort.
    """
    author_name = ""
    year_str = ""

    if meta:
        try:
            data = meta.get("data", {})
            creators = data.get("creators", [])
            if creators:
                first = creators[0]
                author_name = first.get("lastName") or first.get("name") or first.get("firstName") or ""
                if len(creators) > 1:
                    author_name += " et al."
            date_str = data.get("date", "")
            if date_str:
                m = re.search(r"\b(\d{4})\b", date_str)
                if m:
                    year_str = m.group(1)
            if author_name and year_str:
                return f"{author_name}, {year_str}"
            if author_name:
                return author_name
            if year_str:
                return year_str
            title_from_api = data.get("title")
            if title_from_api:
                return title_from_api
        except Exception:
            pass

    # Fall back to vector-store metadata (author/year preferred over title)
    if src.authors:
        last_names = [
            a.split(",")[0].strip() if "," in a else a.split()[-1]
            for a in src.authors
        ]
        if len(last_names) == 1:
            author_name = last_names[0]
        elif len(last_names) == 2:
            author_name = f"{last_names[0]} & {last_names[1]}"
        else:
            author_name = f"{last_names[0]} et al."
    if src.year:
        year_str = str(src.year)
    if author_name and year_str:
        return f"{author_name}, {year_str}"
    if author_name:
        return author_name
    if year_str:
        return year_str
    return src.title


# ---------------------------------------------------------------------------
# Citation processing
# ---------------------------------------------------------------------------

async def _process_citations(text: str, sources: list) -> str:
    """Replace inline citation references with HTML links to www.zotero.org.

    Mirrors the plugin's replaceCitationsInText() logic, ported to Python.
    Fetches author/year metadata from the Zotero web API in parallel.
    """
    if not sources:
        return text

    # Normalise legacy "Source N" → [SN]
    text = re.sub(r"\*{0,2}Source\s+(\d+)\*{0,2}", lambda m: f"[S{m.group(1)}]", text)

    # Fetch metadata for all unique (slug, item_id) pairs in parallel
    unique_pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for src in sources:
        slug = backend_id_to_slug(src.library_id)
        key = (slug, src.item_id)
        if key not in seen:
            seen.add(key)
            unique_pairs.append(key)

    meta_map: dict[tuple[str, str], Optional[dict]] = {}
    async with httpx.AsyncClient() as client:
        tasks = [_fetch_item_metadata(client, slug, item_id) for slug, item_id in unique_pairs]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for key, result in zip(unique_pairs, results):
            meta_map[key] = result if not isinstance(result, Exception) else None

    # Build per-source display data (keyed by 1-based index)
    source_data: dict[int, dict] = {}
    for i, src in enumerate(sources, 1):
        slug = backend_id_to_slug(src.library_id)
        meta = meta_map.get((slug, src.item_id))
        source_data[i] = {
            "url": f"{ZOTERO_WEB_BASE}/{slug}/items/{src.item_id}",
            "display": _format_display_text(meta, src),
            "anchor": src.text_anchor or "",
        }

    # Citation pattern: [S1], [S1:10], [S1:p.10], [S1:0.3.1], [S1,S2:5], [1], [1:10]
    # Page token allows section numbers with dots (e.g. 0.3.1) in addition to plain integers.
    page_token = r"(?::p\.?\s*[\d.]+|:[\d.]+)?"
    s_ref = r"[Ss]\d+" + page_token
    n_ref = r"\d+(?::[\d.]+)?"
    pattern = re.compile(
        r"\[(" + s_ref + r"(?:,\s*" + s_ref + r")*|" + n_ref + r"(?:,\s*" + n_ref + r")*)\]"
    )

    def replace_match(m: re.Match) -> str:
        parts = []
        for citation in re.split(r",\s*", m.group(1)):
            normalised = re.sub(r"^[Ss]", "", citation)
            colon = normalised.find(":")
            num_str = normalised[:colon] if colon >= 0 else normalised
            page_raw = re.sub(r"^p\.?\s*", "", normalised[colon + 1:]) if colon >= 0 else None
            try:
                num = int(num_str)
            except ValueError:
                parts.append(f"[{citation}]")
                continue
            info = source_data.get(num)
            if not info:
                parts.append(f"[{citation}]")
                continue
            display = info["display"]
            if page_raw:
                display += f", p. {page_raw}"
            title_attr = f' title="{_escape_html(info["anchor"])}"' if info["anchor"] else ""
            parts.append(f'<a href="{info["url"]}"{title_attr}>({_escape_html(display)})</a>')
        return " ".join(parts)

    return pattern.sub(replace_match, text)


def _escape_html(text: str) -> str:
    return (text
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&#039;"))


_CITATION_ANCHOR = r'<a\s+[^>]*>\([^<)]+\)</a>'
_CITATION_GROUP_PAT = re.compile(
    r'(' + _CITATION_ANCHOR + r')(\s+' + _CITATION_ANCHOR + r')+'
)
_SINGLE_CITATION_PAT = re.compile(r'(<a\s+[^>]*>)\(([^<)]+)\)(</a>)')


def _merge_consecutive_citations(html: str) -> str:
    """Merge runs of adjacent citation links into one semicolon-separated group.

    Converts:  <a href="u1">(A)</a> <a href="u2">(B)</a> <a href="u3">(C)</a>
    To:        (<a href="u1">A</a>; <a href="u2">B</a>; <a href="u3">C</a>)
    """
    def merge(m: re.Match) -> str:
        parts = [
            f"{open_tag}{text}{close_tag}"
            for open_tag, text, close_tag in _SINGLE_CITATION_PAT.findall(m.group(0))
        ]
        return "(" + "; ".join(parts) + ")"

    return _CITATION_GROUP_PAT.sub(merge, html)


async def _build_bibliography(sources: list) -> list[dict]:
    """Build a deduplicated, sorted bibliography with Zotero web URLs and display labels."""
    if not sources:
        return []

    seen_ids: set[str] = set()
    unique: list = []
    for src in sources:
        if src.item_id not in seen_ids:
            seen_ids.add(src.item_id)
            unique.append(src)

    async with httpx.AsyncClient() as client:
        tasks = [_fetch_item_metadata(client, backend_id_to_slug(src.library_id), src.item_id)
                 for src in unique]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    entries = []
    for src, meta in zip(unique, results):
        if isinstance(meta, Exception):
            meta = None
        slug = backend_id_to_slug(src.library_id)
        display = _format_display_text(meta, src)
        # For bibliography, show full title when available
        label = display
        if meta:
            title = meta.get("data", {}).get("title", "")
            if title and display != title:
                label = f"{display} — {title}"
        entries.append({
            "url": f"{ZOTERO_WEB_BASE}/{slug}/items/{src.item_id}",
            "label": label,
            "sort_key": (label or "").lower(),
        })

    entries.sort(key=lambda e: e["sort_key"])
    return entries


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
async def public_index(request: Request) -> HTMLResponse:
    """Index page listing all configured public libraries."""
    config = _load_public_config()
    libraries = [
        {"slug": slug, "url": f"/public/{slug}", **info}
        for slug, info in config.items()
    ]
    # Inline minimal HTML — no separate template needed for a simple list
    items_html = "\n".join(
        f'<li><a href="{lib["url"]}">{_escape_html(lib["title"])}</a>'
        f'<br><span style="color:#555;font-size:.9rem">{_escape_html(lib.get("description",""))}</span></li>'
        for lib in libraries
    )
    if not libraries:
        body = "<p>No public libraries are currently configured.</p>"
    else:
        body = f"<ul style='list-style:none;padding:0'>{items_html}</ul>"
    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Public Libraries — Zotero RAG</title>
<style>body{{font-family:Georgia,serif;max-width:720px;margin:2rem auto;padding:0 1rem}}
h1{{font-size:1.5rem}}li{{margin-bottom:1.2rem}}a{{color:#1a5c9e}}</style></head>
<body><h1>Public Libraries</h1>{body}</body></html>"""
    return HTMLResponse(html)


@router.get("/{library_type}/{library_id}", response_class=HTMLResponse)
async def public_library_form(
    request: Request,
    library_type: str,
    library_id: str,
) -> HTMLResponse:
    """Show the query form for a public library."""
    slug = f"{library_type}/{library_id}"
    config = _load_public_config()
    info = config.get(slug)
    if not info:
        raise HTTPException(status_code=403, detail=f"Library {slug!r} is not publicly accessible.")
    return _templates.TemplateResponse("public_form.html", {
        "request": request,
        "title": info.get("title", slug),
        "description": info.get("description", ""),
        "placeholder": info.get("placeholder", ""),
        "question": "",
        "error": None,
    })


@router.post("/{library_type}/{library_id}", response_class=HTMLResponse)
async def public_library_query(
    request: Request,
    library_type: str,
    library_id: str,
    question: str = Form(...),
    vector_store: VectorStore = Depends(get_vector_store),
) -> HTMLResponse:
    """Process a RAG query and return the results page."""
    slug = f"{library_type}/{library_id}"
    config = _load_public_config()
    info = config.get(slug)
    if not info:
        raise HTTPException(status_code=403, detail=f"Library {slug!r} is not publicly accessible.")

    title = info.get("title", slug)
    description = info.get("description", "")
    placeholder = info.get("placeholder", "")
    question = question.strip()

    def _form(q: str, error: str) -> HTMLResponse:
        return _templates.TemplateResponse("public_form.html", {
            "request": request,
            "title": title,
            "description": description,
            "placeholder": placeholder,
            "question": q,
            "error": error,
        })

    if not question:
        return _form("", "Please enter a question.")

    if vector_store is None:
        return _form(question, "The search service is temporarily unavailable. Please try again later.")

    backend_lib_id = slug_to_backend_id(slug)

    try:
        settings = get_settings()
        preset = settings.get_hardware_preset()
        embedding_service = make_embedding_service()
        llm_service = make_llm_service()

        top_k = preset.rag.top_k
        min_score = preset.rag.score_threshold

        # Verify the library has indexed content
        from qdrant_client.models import Filter, FieldCondition, MatchValue
        count = vector_store.client.count(
            collection_name=vector_store.CHUNKS_COLLECTION,
            count_filter=Filter(must=[FieldCondition(key="library_id", match=MatchValue(value=backend_lib_id))])
        ).count
        if count == 0:
            return _form(question, "This library has not been indexed yet. Please check back later.")

        rag_engine = RAGEngine(
            embedding_service=embedding_service,
            llm_service=llm_service,
            vector_store=vector_store,
            settings=settings,
        )
        result = await rag_engine.query(
            question=question,
            library_ids=[backend_lib_id],
            top_k=top_k,
            min_score=min_score,
        )

    except Exception as e:
        logger.exception(f"[public_query] RAG query failed for {slug}: {e}")
        return _form(question, f"Query failed: {e}")

    # Render markdown → HTML, then replace citation refs, then merge consecutive
    md = MarkdownIt()
    answer_html = md.render(result.answer)
    answer_html = await _process_citations(answer_html, result.sources)
    answer_html = _merge_consecutive_citations(answer_html)

    # Build bibliography
    bib_entries = await _build_bibliography(result.sources)

    return _templates.TemplateResponse("public_result.html", {
        "request": request,
        "title": title,
        "question": question,
        "answer_html": answer_html,
        "sources": bib_entries,
        "slug": slug,
    })
