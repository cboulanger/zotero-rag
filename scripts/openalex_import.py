#!/usr/bin/env python
"""
openalex_import.py — Fetch OA articles from OpenAlex and import them into a Zotero group library.

Usage:
    uv run python scripts/openalex_import.py fetch --issn 1234-5678 [--email addr@example.com]
    uv run python scripts/openalex_import.py import --issn 1234-5678 --group-id 12345 \\
        [--api-key KEY] [--email addr@example.com]

Commands:
    fetch   Query OpenAlex for all OA articles of the journal and save to a CSV in .local/.
            No authentication required. Providing --email (or OPENALEX_EMAIL) enables the
            OpenAlex "polite pool" with higher rate limits.
    import  Read the CSV and create Zotero items with PDF attachments in the group library.
            Requires a Zotero API key (--api-key or ZOTERO_API_KEY env var) with
            read+write access to the target group library.
            Create a key at: https://www.zotero.org/settings/keys

The script is idempotent: re-running fetch resumes from where it left off; re-running import
skips rows that are already marked as imported or exist.

State files written to .local/:
    openalex_{issn_}.csv     — article rows (doi, pdf_url, title, year, authors_json,
                                journal, volume, issue, pages, issn, openalex_id, status)
    openalex_{issn_}.cursor  — last OpenAlex pagination cursor (deleted on completion)

Status column values:
    pending     — fetched, not yet imported
    imported    — item + PDF successfully created in Zotero
    exists      — DOI already present in Zotero (skipped)
    error:…     — item creation failed
    pdf_error:… — item created but PDF download/upload failed
"""

import argparse
import csv
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Iterator

import httpx
from dotenv import load_dotenv
from filelock import FileLock
from pyzotero import zotero
from tqdm import tqdm

PROJECT_ROOT = Path(__file__).parent.parent
load_dotenv(PROJECT_ROOT / ".env")

CSV_COLUMNS = [
    "doi", "pdf_url", "title", "year", "authors_json",
    "journal", "volume", "issue", "pages", "issn",
    "openalex_id", "status",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def sanitize_issn(issn: str) -> str:
    return issn.strip().replace("-", "_")


def csv_path(issn: str) -> Path:
    return PROJECT_ROOT / ".local" / f"openalex_{sanitize_issn(issn)}.csv"


def cursor_path(issn: str) -> Path:
    return PROJECT_ROOT / ".local" / f"openalex_{sanitize_issn(issn)}.cursor"


def _split_name(display_name: str) -> tuple[str, str]:
    """Split 'First Last' into ('Last', 'First'). Handles multi-word first names."""
    parts = display_name.strip().rsplit(" ", 1)
    if len(parts) == 2:
        return parts[1], parts[0]
    return parts[0], ""


def reconstruct_abstract(inv_idx: dict | None) -> str:
    """Reconstruct a plain-text abstract from OpenAlex's inverted index format."""
    if not inv_idx:
        return ""
    pos: dict[int, str] = {}
    for word, positions in inv_idx.items():
        for p in positions:
            pos[p] = word
    return " ".join(pos[k] for k in sorted(pos))


def get_pdf_url(work: dict) -> str | None:
    """Return the best available open-access PDF URL for a work, or None.

    Checks best_oa_location first, then primary_location, then all locations.
    Prefers publisher-hosted PDFs over repository copies.
    """
    # Preferred: best_oa_location (OpenAlex's recommended OA copy)
    best = work.get("best_oa_location") or {}
    if best.get("pdf_url"):
        return best["pdf_url"]

    # Fallback: primary publisher location
    primary = work.get("primary_location") or {}
    if primary.get("pdf_url"):
        return primary["pdf_url"]

    # Fallback: any location with a PDF URL
    for loc in work.get("locations") or []:
        if loc.get("pdf_url"):
            return loc["pdf_url"]

    return None


def work_to_row(work: dict) -> dict | None:
    """Convert an OpenAlex work dict to a CSV row dict. Returns None if no PDF URL."""
    pdf_url = get_pdf_url(work)
    if not pdf_url:
        return None
    doi = work.get("doi") or ""
    if not doi:
        return None

    loc = work.get("primary_location") or {}
    source = loc.get("source") or {}
    issn_list = source.get("issn") or []
    biblio = work.get("biblio") or {}
    first_page = biblio.get("first_page") or ""
    last_page = biblio.get("last_page") or ""
    pages = f"{first_page}-{last_page}" if first_page and last_page else first_page

    authors = []
    for authorship in work.get("authorships") or []:
        author = authorship.get("author") or {}
        display_name = author.get("display_name") or ""
        if display_name:
            last, first = _split_name(display_name)
            authors.append({"lastName": last, "firstName": first})

    return {
        "doi": doi,
        "pdf_url": pdf_url,
        "title": work.get("title") or "",
        "year": str(work.get("publication_year") or ""),
        "authors_json": json.dumps(authors, ensure_ascii=False),
        "journal": source.get("display_name") or "",
        "volume": biblio.get("volume") or "",
        "issue": biblio.get("issue") or "",
        "pages": pages,
        "issn": issn_list[0] if issn_list else "",
        "openalex_id": work.get("id") or "",
        "status": "pending",
    }


# ---------------------------------------------------------------------------
# CSV state file
# ---------------------------------------------------------------------------

class CsvStateFile:
    """Thread-safe, crash-safe CSV state file using filelock + atomic replace."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = FileLock(str(path) + ".lock")

    def read_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        with self.path.open(newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def append_rows(self, rows: list[dict], seen_dois: set[str]) -> int:
        """Append rows not yet in the CSV (deduplicated by DOI). Returns count appended."""
        new_rows = [r for r in rows if r["doi"] not in seen_dois]
        if not new_rows:
            return 0
        with self._lock:
            existing = self._read_raw()
            existing_dois = {r["doi"] for r in existing}
            to_add = [r for r in new_rows if r["doi"] not in existing_dois]
            if not to_add:
                return 0
            self._write_raw(existing + to_add)
        for r in to_add:
            seen_dois.add(r["doi"])
        return len(to_add)

    def update_row(self, doi: str, **updates) -> None:
        """Update fields of the row with the given DOI atomically."""
        with self._lock:
            rows = self._read_raw()
            for row in rows:
                if row["doi"] == doi:
                    row.update(updates)
                    break
            self._write_raw(rows)

    def _read_raw(self) -> list[dict]:
        if not self.path.exists():
            return []
        with self.path.open(newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def _write_raw(self, rows: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(tmp, self.path)


# ---------------------------------------------------------------------------
# Cursor store
# ---------------------------------------------------------------------------

class CursorStore:
    """Persists the OpenAlex pagination cursor so fetch can resume after interruption."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> str | None:
        if not self.path.exists():
            return None
        text = self.path.read_text(encoding="utf-8").strip()
        return text or None

    def save(self, cursor: str) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(cursor, encoding="utf-8")
        os.replace(tmp, self.path)

    def delete(self) -> None:
        self.path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# OpenAlex client
# ---------------------------------------------------------------------------

class OpenAlexClient:
    """Thin httpx wrapper for the OpenAlex REST API with polite-pool rate limiting."""

    BASE = "https://api.openalex.org"
    PAGE_SIZE = 200
    # Fields to request — keeps payload small
    _SELECT = ",".join([
        "id", "doi", "title", "publication_year",
        "primary_location", "best_oa_location", "locations",
        "authorships", "biblio", "abstract_inverted_index",
        "language",
    ])

    def __init__(self, email: str | None = None) -> None:
        self._email = email
        self._client = httpx.Client(
            base_url=self.BASE,
            timeout=30,
            follow_redirects=True,
        )

    def _get(self, path: str, params: dict) -> dict:
        if self._email:
            params.setdefault("mailto", self._email)
        last_exc: Exception = RuntimeError("unknown error")
        for attempt in range(3):
            try:
                r = self._client.get(path, params=params)
                if r.status_code == 429:
                    wait = int(r.headers.get("Retry-After", 15))
                    print(f"\n[WARN] OpenAlex rate limit hit, waiting {wait}s...", file=sys.stderr)
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                return r.json()
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                if attempt < 2:
                    time.sleep(2 ** attempt)
        raise last_exc

    def iter_works(
        self,
        issn: str,
        start_cursor: str = "*",
    ) -> Iterator[tuple[list[dict], str | None, int]]:
        """Yield (works_page, next_cursor, total_count) for each page of results."""
        cursor: str | None = start_cursor
        while cursor is not None:
            data = self._get("/works", {
                "filter": f"primary_location.source.issn:{issn},is_oa:true",
                "per-page": self.PAGE_SIZE,
                "cursor": cursor,
                "select": self._SELECT,
            })
            meta = data.get("meta", {})
            total = meta.get("count", 0)
            next_cursor: str | None = meta.get("next_cursor")
            works: list[dict] = data.get("results", [])
            yield works, next_cursor, total
            # Polite pool: ~8 req/sec
            time.sleep(0.12)
            cursor = next_cursor

    def fetch_work(self, openalex_id: str) -> dict | None:
        """Fetch a single work by its OpenAlex ID (URL or bare ID like W12345678)."""
        work_id = openalex_id.rsplit("/", 1)[-1]
        try:
            data = self._get(f"/works/{work_id}", {})
            time.sleep(0.12)
            return data
        except Exception:
            return None

    def close(self) -> None:
        self._client.close()


# ---------------------------------------------------------------------------
# Zotero importer
# ---------------------------------------------------------------------------

class ZoteroImporter:
    """Creates journal article items and uploads PDF attachments via pyzotero."""

    def __init__(self, group_id: str, api_key: str) -> None:
        self._zot = zotero.Zotero(
            library_id=group_id,
            library_type="group",
            api_key=api_key,
        )

    def doi_exists(self, doi: str) -> str | None:
        """Return the item key if a work with this DOI already exists, else None."""
        doi_bare = re.sub(r"^https?://doi\.org/", "", doi, flags=re.IGNORECASE)
        try:
            results = self._zot.items(q=doi_bare, qmode="everything", limit=5)
            for item in results:
                item_doi = item.get("data", {}).get("DOI", "")
                item_doi_bare = re.sub(
                    r"^https?://doi\.org/", "", item_doi, flags=re.IGNORECASE
                )
                if item_doi_bare.lower() == doi_bare.lower():
                    return item["key"]
        except Exception:
            pass
        return None

    @staticmethod
    def map_work_to_template(template: dict, work: dict) -> dict:
        """Map OpenAlex work fields onto a Zotero journalArticle item template."""
        t = dict(template)
        t["title"] = work.get("title") or ""

        raw_doi = work.get("doi") or ""
        t["DOI"] = re.sub(r"^https?://doi\.org/", "", raw_doi, flags=re.IGNORECASE)
        t["url"] = raw_doi
        t["date"] = str(work.get("publication_year") or "")

        loc = work.get("primary_location") or {}
        source = loc.get("source") or {}
        t["publicationTitle"] = source.get("display_name") or ""

        issn_list = source.get("issn") or []
        t["ISSN"] = issn_list[0] if issn_list else ""

        biblio = work.get("biblio") or {}
        t["volume"] = biblio.get("volume") or ""
        t["issue"] = biblio.get("issue") or ""
        first_page = biblio.get("first_page") or ""
        last_page = biblio.get("last_page") or ""
        if first_page and last_page:
            t["pages"] = f"{first_page}-{last_page}"
        elif first_page:
            t["pages"] = first_page
        else:
            t["pages"] = ""

        t["abstractNote"] = reconstruct_abstract(work.get("abstract_inverted_index"))
        t["language"] = work.get("language") or ""

        creators = []
        for authorship in work.get("authorships") or []:
            author = authorship.get("author") or {}
            display_name = author.get("display_name") or ""
            if display_name:
                last, first = _split_name(display_name)
                creators.append({"creatorType": "author", "firstName": first, "lastName": last})
        t["creators"] = creators
        return t

    def create_journal_article(self, work: dict) -> str:
        """Create a journalArticle in Zotero and return its item key."""
        template = self._zot.item_template("journalArticle")
        item_data = self.map_work_to_template(template, work)
        resp = self._zot.create_items([item_data])
        successful = resp.get("successful", {})
        if not successful:
            failed = resp.get("failed", {})
            raise RuntimeError(f"create_items failed: {failed}")
        return list(successful.values())[0]["key"]

    def attach_pdf(self, item_key: str, pdf_url: str, title: str) -> None:
        """Download a PDF and upload it as a child attachment of the given item."""
        safe_name = re.sub(r"[^\w\s\-]", "", title)[:100].strip() or "article"
        safe_name += ".pdf"
        tmp_path = Path(tempfile.gettempdir()) / safe_name

        try:
            with httpx.Client(follow_redirects=True, timeout=120) as client:
                r = client.get(pdf_url)
                r.raise_for_status()
                tmp_path.write_bytes(r.content)

            result = self._zot.attachment_simple([str(tmp_path)], parentid=item_key)
            failures = result.get("failure", [])
            if failures:
                raise RuntimeError(f"attachment upload failure: {failures}")
        finally:
            tmp_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Fallback work dict built from CSV row (when OpenAlex re-fetch fails)
# ---------------------------------------------------------------------------

def work_from_row(row: dict) -> dict:
    """Reconstruct a minimal OpenAlex-shaped work dict from a CSV row."""
    authors = json.loads(row.get("authors_json") or "[]")
    pages = row.get("pages") or ""
    parts = pages.split("-", 1) if pages else []
    return {
        "doi": row["doi"],
        "title": row.get("title"),
        "publication_year": int(row["year"]) if row.get("year") else None,
        "primary_location": {
            "source": {
                "display_name": row.get("journal"),
                "issn": [row["issn"]] if row.get("issn") else [],
            },
        },
        "best_oa_location": {"pdf_url": row.get("pdf_url")},
        "authorships": [
            {"author": {"display_name": f"{a['firstName']} {a['lastName']}"}}
            for a in authors
        ],
        "biblio": {
            "volume": row.get("volume"),
            "issue": row.get("issue"),
            "first_page": parts[0] if parts else None,
            "last_page": parts[1] if len(parts) > 1 else None,
        },
        "abstract_inverted_index": None,
        "language": None,
    }


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_fetch(args: argparse.Namespace) -> None:
    issn = args.issn.strip()
    email = args.email or os.environ.get("OPENALEX_EMAIL")

    state = CsvStateFile(csv_path(issn))
    cursors = CursorStore(cursor_path(issn))

    existing_rows = state.read_all()
    seen_dois: set[str] = {r["doi"] for r in existing_rows}
    stored_cursor = cursors.load()

    # If CSV exists but no cursor: a previous fetch completed — don't re-fetch.
    if existing_rows and stored_cursor is None:
        print(f"[INFO] Fetch already complete ({len(seen_dois)} articles in {csv_path(issn)}).")
        print("[INFO] Delete the .csv and .cursor files to start over.")
        return

    start_cursor = stored_cursor or "*"
    if stored_cursor:
        print(f"[INFO] Resuming fetch for ISSN {issn} (already have {len(seen_dois)} articles)...")
    else:
        print(f"[INFO] Starting fetch for ISSN {issn}...")

    client = OpenAlexClient(email=email)
    total_appended = 0
    pbar: tqdm | None = None

    try:
        for works, next_cursor, total_count in client.iter_works(issn, start_cursor=start_cursor):
            if pbar is None:
                pbar = tqdm(
                    total=total_count,
                    desc="Fetching articles",
                    unit="article",
                    initial=len(seen_dois),
                )

            rows = [r for work in works if (r := work_to_row(work)) is not None]
            total_appended += state.append_rows(rows, seen_dois)
            pbar.update(len(works))

            if next_cursor:
                cursors.save(next_cursor)
            else:
                cursors.delete()

    except KeyboardInterrupt:
        print("\n[INFO] Interrupted — progress saved. Re-run to continue.")
    finally:
        if pbar:
            pbar.close()
        client.close()

    print(f"\n[INFO] {total_appended} new articles added. Total: {len(seen_dois)} in {csv_path(issn)}.")


def cmd_import(args: argparse.Namespace) -> None:
    issn = args.issn.strip()
    group_id = args.group_id.strip()
    api_key = args.api_key or os.environ.get("ZOTERO_API_KEY")
    email = args.email or os.environ.get("OPENALEX_EMAIL")

    if not api_key:
        print("[ERROR] Zotero API key required (--api-key or ZOTERO_API_KEY).", file=sys.stderr)
        sys.exit(1)

    state = CsvStateFile(csv_path(issn))
    all_rows = state.read_all()

    if not all_rows:
        print(f"[ERROR] No CSV at {csv_path(issn)}. Run `fetch` first.", file=sys.stderr)
        sys.exit(1)

    pending = [r for r in all_rows if r.get("status") == "pending"]
    print(f"[INFO] {len(pending)} pending / {len(all_rows)} total articles.")

    if not pending:
        print("[INFO] Nothing to import.")
        return

    oa_client = OpenAlexClient(email=email)
    zot = ZoteroImporter(group_id=group_id, api_key=api_key)

    imported = skipped = errors = 0

    try:
        with tqdm(total=len(pending), desc="Importing to Zotero", unit="article") as pbar:
            for row in pending:
                doi = row["doi"]
                title = row.get("title") or doi

                try:
                    # Skip if already in Zotero
                    existing_key = zot.doi_exists(doi)
                    if existing_key:
                        state.update_row(doi, status="exists")
                        skipped += 1
                        pbar.update(1)
                        continue

                    # Fetch full metadata from OpenAlex (richer than CSV snapshot)
                    work: dict | None = None
                    openalex_id = row.get("openalex_id") or ""
                    if openalex_id:
                        work = oa_client.fetch_work(openalex_id)
                    if work is None:
                        work = work_from_row(row)

                    # Create Zotero item
                    item_key = zot.create_journal_article(work)

                    # Upload PDF
                    try:
                        zot.attach_pdf(item_key, row["pdf_url"], title)
                        state.update_row(doi, status="imported")
                        imported += 1
                    except Exception as exc:
                        msg = str(exc)[:200]
                        state.update_row(doi, status=f"pdf_error:{msg}")
                        errors += 1
                        print(f"\n[WARN] PDF upload failed for {doi}: {msg}", file=sys.stderr)

                except KeyboardInterrupt:
                    print("\n[INFO] Interrupted — progress saved. Re-run to continue.")
                    break
                except Exception as exc:
                    msg = str(exc)[:200]
                    state.update_row(doi, status=f"error:{msg}")
                    errors += 1
                    print(f"\n[WARN] Failed to import {doi}: {msg}", file=sys.stderr)

                pbar.update(1)
                time.sleep(0.5)  # Zotero API courtesy delay

    finally:
        oa_client.close()

    print(
        f"\n[INFO] Done: {imported} imported, {skipped} already existed, {errors} errors."
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="openalex_import.py",
        description="Fetch OA articles from OpenAlex and import them into a Zotero group library.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_fetch = sub.add_parser("fetch", help="Fetch article metadata from OpenAlex into a CSV.")
    p_fetch.add_argument(
        "--issn", required=True, metavar="ISSN",
        help="ISSN of the open-access journal (e.g. 1234-5678).",
    )
    p_fetch.add_argument(
        "--email", default=None, metavar="EMAIL",
        help="Email for OpenAlex polite-pool requests. Falls back to OPENALEX_EMAIL env var.",
    )

    p_import = sub.add_parser("import", help="Import CSV articles into Zotero group library.")
    p_import.add_argument(
        "--issn", required=True, metavar="ISSN",
        help="ISSN identifying which CSV to read.",
    )
    p_import.add_argument(
        "--group-id", required=True, metavar="GROUP_ID",
        help="Zotero group library ID (integer).",
    )
    p_import.add_argument(
        "--api-key", default=None, metavar="KEY",
        help="Zotero API key. Falls back to ZOTERO_API_KEY env var.",
    )
    p_import.add_argument(
        "--email", default=None, metavar="EMAIL",
        help="Email for OpenAlex polite-pool. Falls back to OPENALEX_EMAIL env var.",
    )

    args = parser.parse_args()
    if args.command == "fetch":
        cmd_fetch(args)
    elif args.command == "import":
        cmd_import(args)


if __name__ == "__main__":
    main()
