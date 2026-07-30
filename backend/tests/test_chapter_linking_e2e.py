"""End-to-end (live) tests for the four chapter-segmentation scripts
(docs/chapter-segmentation.md) against a real Zotero library:

    1. scripts/analyze_book_chapters.py
    2. scripts/ocr_attachments.py
    3. scripts/retrofit_chapter_links.py
    4. scripts/upload_chapters.py

Each script is invoked exactly as a user would from a shell (via
subprocess), against the Zotero group configured by
`CHAPTER_LINKING_TEST_LIBRARY_SLUG` (default: "groups/6297749", the
project's dedicated "test-rag-plugin" group -- see CLAUDE.md's "Live Query
Debugging" section) using the write-scoped `ZOTERO_API_KEY` from `.env`.
Set `CHAPTER_LINKING_TEST_LIBRARY_SLUG` to point this at a different group
you control (e.g. "groups/<your-id>" or "users/<your-id>") -- the key just
needs write access to whichever library it names.

Every book/chapter item, attachment, and collection created by these tests
is deleted again in a `finally` block (see the `cleanup` fixture), so the
target library is left unchanged after a run regardless of pass/fail. Every
created item also carries a "zotero-rag-e2e-test" tag so leftovers are easy
to find by hand in the rare case cleanup itself is interrupted (e.g. the
process is killed).

Only the OA (open-access) fixtures in backend/evaluation/book-segmentation/
are ever uploaded to Zotero here. The one scanned-book fixture in that
directory (9783322969828.pdf) is marked "oa": false in manifest.json --
non-open-access, copyrighted content acquired for local heuristic testing
only -- so it is never uploaded anywhere, including to this dedicated test
group. Script 2's OCR path is instead exercised against a synthetic,
image-only PDF page built on the fly with Pillow, which is just as "no
extractable text layer" as a real scan from script 1/2's point of view.

Marked "integration" so it's excluded from the default `uv run pytest` /
`npm test` run (see pyproject.toml's addopts) -- it costs real network calls
and Zotero storage, and takes tens of seconds to minutes. Run it directly:

    uv run pytest backend/tests/test_chapter_linking_e2e.py -v -s

or via the existing integration flag, alongside the rest of the suite:

    npm run test:integration

This is a smoke test, not a duplicate of the unit test suites in
backend/tests/test_chapter_*.py -- it checks that each script's CLI wiring
and real Zotero API calls work end-to-end, not the full range of heuristic
edge cases already covered there.
"""

import json
import os
import socket
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader
from pyzotero import zotero

_PROJECT_ROOT = Path(__file__).parent.parent.parent
_EVAL_DIR = _PROJECT_ROOT / "backend" / "evaluation" / "book-segmentation"
_SCRIPTS_DIR = _PROJECT_ROOT / "scripts"

load_dotenv(_PROJECT_ROOT / ".env")

API_KEY = os.getenv("ZOTERO_API_KEY")
LIBRARY_SLUG = os.getenv("CHAPTER_LINKING_TEST_LIBRARY_SLUG", "groups/6297749")
_kind, NUMERIC_ID = LIBRARY_SLUG.split("/", 1)
LIBRARY_TYPE = "user" if _kind == "users" else "group"

# Small, native-text, English fixture with 1.00 precision/recall in the
# evaluation set (see backend/evaluation/book-segmentation/README.md) --
# open access, so it's safe to upload to the test group.
_NATIVE_PDF = _EVAL_DIR / "9781771993661.pdf"

_TEST_TAG = "zotero-rag-e2e-test"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not API_KEY, reason="ZOTERO_API_KEY not set in .env"),
    pytest.mark.skipif(
        not _NATIVE_PDF.exists(),
        reason=f"{_NATIVE_PDF.name} fixture not present locally -- "
               f"run `uv run python scripts/fetch_evaluation_pdfs.py` first",
    ),
]


def _run_script(name: str, args: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    result = subprocess.run(
        [
            sys.executable, str(_SCRIPTS_DIR / name),
            "--library-slug", LIBRARY_SLUG,
            "--api-key", API_KEY,
            *args,
        ],
        cwd=_PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    assert result.returncode == 0, (
        f"{name} exited {result.returncode}\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    return result


def _unique(label: str) -> str:
    return f"[E2E-TEST] {label} {uuid.uuid4().hex[:8]}"


def _kreuzberg_available() -> bool:
    try:
        with socket.create_connection(("localhost", 8100), timeout=1.5):
            return True
    except OSError:
        return False


@pytest.fixture(scope="module")
def zot() -> zotero.Zotero:
    return zotero.Zotero(library_id=NUMERIC_ID, library_type=LIBRARY_TYPE, api_key=API_KEY)


@pytest.fixture
def cleanup(zot):
    """Tracks item/collection keys created during a test and deletes them
    afterward regardless of pass/fail, so the shared library is left
    unchanged. Sub-collections must be listed before their parent (deletion
    happens in list order)."""
    item_keys: list[str] = []
    collection_keys: list[str] = []
    try:
        yield item_keys, collection_keys
    finally:
        for key in item_keys:
            try:
                zot.delete_item(zot.item(key))
            except Exception as exc:  # noqa: BLE001 - best-effort cleanup, never masks the test result
                print(f"[cleanup] could not delete item {key}: {exc}")
        for key in collection_keys:
            try:
                zot.delete_collection(zot.collection(key))
            except Exception as exc:  # noqa: BLE001
                print(f"[cleanup] could not delete collection {key}: {exc}")


def _create_book_item(zot, *, title: str, pdf_path: Path, language: str, item_keys: list[str]) -> tuple[str, str]:
    template = zot.item_template("book")
    template["title"] = title
    template["language"] = language
    template["date"] = "2023"
    template["publisher"] = "E2E Test Publisher"
    template["place"] = "Testland"
    template["ISBN"] = "0000000000"
    template["tags"] = [{"tag": _TEST_TAG}]
    resp = zot.create_items([template])
    book_key = list(resp["successful"].values())[0]["key"]
    item_keys.append(book_key)
    attachment_key = _upload_pdf_attachment(zot, parent_key=book_key, file_path=pdf_path)
    item_keys.append(attachment_key)
    return book_key, attachment_key


def _upload_pdf_attachment(zot, *, parent_key: str, file_path: Path) -> str:
    """Create and upload a PDF attachment under `parent_key`.

    NOTE: deliberately does NOT use pyzotero's attachment_simple() helper --
    it sends str(file_path) verbatim as the Zotero "filename" field, which
    the API rejects ("cannot contain a directory path") for any non-bare
    filename, and both a resolved fixture path and a tempfile path are
    always absolute. This is the same bug fixed in
    backend/services/chapter_upload.py; see its NOTE for the full
    explanation. Two-step create_items + upload_attachments(basedir=...)
    sidesteps it by resolving the local file path separately from the
    server-side field.
    """
    attachment_template = zot.item_template("attachment", linkmode="imported_file")
    attachment_template["title"] = file_path.name
    attachment_template["filename"] = file_path.name
    attachment_template["contentType"] = "application/pdf"
    attachment_template["parentItem"] = parent_key
    resp = zot.create_items([attachment_template])
    attachment_key = list(resp["successful"].values())[0]["key"]

    upload_resp = zot.upload_attachments([{"key": attachment_key, "filename": file_path.name}], basedir=str(file_path.parent))
    assert not upload_resp["failure"], f"attachment file upload failed: {upload_resp}"
    return attachment_key


@pytest.mark.timeout(300)  # overrides pyproject.toml's global 30s -- real network + subprocess calls
def test_analyze_and_upload_chapters(zot, cleanup, tmp_path):
    """Scripts 1 (analyze) and 4 (segment & upload) against a real book item
    with a real, open-access PDF attachment."""
    item_keys, collection_keys = cleanup

    title = _unique("Violence, Imagination, and Resistance")
    book_key, _attachment_key = _create_book_item(
        zot, title=title, pdf_path=_NATIVE_PDF, language="en", item_keys=item_keys
    )

    analysis_path = tmp_path / "analysis.json"
    _run_script("analyze_book_chapters.py", ["--item-keys", book_key, "--output", str(analysis_path)])
    analysis = json.loads(analysis_path.read_text())
    assert len(analysis["attachments"]) == 1
    entry = analysis["attachments"][0]
    assert entry["item_key"] == book_key
    assert entry["has_text_layer"] is True
    assert entry["needs_ocr"] is False
    assert len(entry["chapters"]) > 0
    # This fixture is the evaluation set's highest-scoring native-text book
    # (1.00 precision/recall, see backend/evaluation/book-segmentation/README.md).
    assert any(c["confidence"] >= 0.90 for c in entry["chapters"])

    target_collection = _unique("Book Chapters")
    upload_output = tmp_path / "upload_result.json"
    _run_script(
        "upload_chapters.py",
        [
            "--input", str(analysis_path),
            "--commit",
            "--target-collection", target_collection,
            "--output", str(upload_output),
        ],
    )
    upload_result = json.loads(upload_output.read_text())
    assert not upload_result["failed"], upload_result["failed"]
    assert len(upload_result["created"]) > 0

    chapter_keys = [c["chapter_key"] for c in upload_result["created"]]
    item_keys.extend(chapter_keys)

    for chapter_key in chapter_keys:
        chapter_item = zot.item(chapter_key)
        assert chapter_item["data"]["itemType"] == "bookSection"
        assert f"{LIBRARY_SLUG}:{book_key}" in chapter_item["data"].get("extra", "")
        # Track this chapter's own uploaded PDF-slice attachment for cleanup.
        item_keys.extend(child["key"] for child in zot.children(chapter_key))

    book_item = zot.item(book_key)
    assert any(f"{LIBRARY_SLUG}:{k}" in book_item["data"].get("extra", "") for k in chapter_keys)

    top_matches = [c for c in zot.collections() if c["data"]["name"] == target_collection]
    assert top_matches, f"target collection {target_collection!r} was not created"
    top_key = top_matches[0]["key"]
    collection_keys.extend(c["key"] for c in zot.collections_sub(top_key))
    collection_keys.append(top_key)


def _make_synthetic_scan_pdf(path: Path, lines: list[str]) -> None:
    """Build a one-page, image-only PDF (no text layer) with Pillow. This
    lets script 1/2's "needs_ocr" path be exercised for real without
    uploading any copyrighted scanned-book content (see module docstring)."""
    image = Image.new("RGB", (1200, 1600), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=56)
    y = 200
    for line in lines:
        draw.text((100, y), line, fill="black", font=font)
        y += 90
    image.save(path, "PDF", resolution=150.0)


@pytest.mark.timeout(180)  # overrides pyproject.toml's global 30s -- real network + OCR calls
def test_ocr_attachments(zot, cleanup, tmp_path):
    """Script 2 (OCR), plus script 1's cache-pickup on re-run.

    Unlike the ZOTERO_API_KEY / fixture-file checks at module load (which
    skip cleanly -- those mean "this test isn't configured to run here"),
    Kreuzberg is a live dependency of the test *itself* once it's running:
    if the Zotero-side environment is ready but Kreuzberg isn't reachable,
    that's a real failure to surface, not a silent skip.
    """
    assert _kreuzberg_available(), (
        "Kreuzberg sidecar not reachable at http://localhost:8100 -- "
        "start it (see docs/architecture.md, or `npm start`) to run this test"
    )

    item_keys, _collection_keys = cleanup

    scan_pdf = tmp_path / "synthetic_scan.pdf"
    _make_synthetic_scan_pdf(scan_pdf, [
        "CHAPTER ONE",
        "INTRODUCTION",
        "THIS IS A SYNTHETIC TEST PAGE",
        "GENERATED FOR VERIFYING THE",
        "END TO END OCR PIPELINE WORKS",
        "CORRECTLY ON SCANNED CONTENT",
    ])
    assert not (PdfReader(str(scan_pdf)).pages[0].extract_text() or "").strip(), \
        "synthetic PDF unexpectedly has a text layer"

    title = _unique("Synthetic Scanned Test Book")
    book_key, _attachment_key = _create_book_item(
        zot, title=title, pdf_path=scan_pdf, language="en", item_keys=item_keys
    )

    analysis_path = tmp_path / "analysis_scan.json"
    _run_script("analyze_book_chapters.py", ["--item-keys", book_key, "--output", str(analysis_path)])
    analysis = json.loads(analysis_path.read_text())
    assert analysis["attachments"][0]["needs_ocr"] is True

    cache_dir = tmp_path / "ocr_cache"
    ocr_output = tmp_path / "ocr_results.json"
    _run_script(
        "ocr_attachments.py",
        ["--input", str(analysis_path), "--output", str(ocr_output), "--cache-dir", str(cache_dir)],
    )
    ocr_result = json.loads(ocr_output.read_text())
    assert len(ocr_result["results"]) == 1
    entry = ocr_result["results"][0]
    assert entry["ocr_succeeded"] is True
    assert entry["char_count"] > 0

    # Re-running script 1 with the same --cache-dir should pick up the OCR'd
    # text automatically (docs/chapter-segmentation.md "Typical workflow")
    # instead of reporting needs_ocr again.
    reanalysis_path = tmp_path / "analysis_scan_2.json"
    _run_script(
        "analyze_book_chapters.py",
        ["--item-keys", book_key, "--output", str(reanalysis_path), "--cache-dir", str(cache_dir)],
    )
    reanalysis = json.loads(reanalysis_path.read_text())
    assert reanalysis["attachments"][0]["needs_ocr"] is False
    assert reanalysis["attachments"][0]["has_text_layer"] is True


@pytest.mark.timeout(90)  # overrides pyproject.toml's global 30s -- real network + subprocess calls
def test_retrofit_link_dummy_entries(zot, cleanup, tmp_path):
    """Script 3 (retrofit-link) against dummy, separately-catalogued
    book/bookSection items -- no PDF attachments needed, since retrofit only
    matches on title/year metadata."""
    item_keys, _collection_keys = cleanup

    book_title = _unique("Dummy Retrofit Book")
    book_template = zot.item_template("book")
    book_template["title"] = book_title
    book_template["date"] = "2021"
    book_template["tags"] = [{"tag": _TEST_TAG}]
    book_resp = zot.create_items([book_template])
    book_key = list(book_resp["successful"].values())[0]["key"]
    item_keys.append(book_key)

    matching_title = _unique("Dummy Matching Chapter")
    matching_chapter = zot.item_template("bookSection")
    matching_chapter["title"] = matching_title
    matching_chapter["bookTitle"] = book_title
    matching_chapter["date"] = "2021"
    matching_chapter["tags"] = [{"tag": _TEST_TAG}]

    # A chapter with no bookTitle at all, to exercise the "no_match" bucket
    # in the same run -- chapter_retrofit.run() only reports "no_match" when
    # there's nothing to search for (no bookTitle) or zero book candidates
    # exist; a bookTitle that merely fails to score above threshold against
    # real candidates lands in "ambiguous" instead (see chapter_retrofit.py).
    unmatched_title = _unique("Dummy Unmatched Chapter")
    unmatched_chapter = zot.item_template("bookSection")
    unmatched_chapter["title"] = unmatched_title
    unmatched_chapter["tags"] = [{"tag": _TEST_TAG}]

    chapters_resp = zot.create_items([matching_chapter, unmatched_chapter])
    created_chapters = list(chapters_resp["successful"].values())
    item_keys.extend(c["key"] for c in created_chapters)
    matching_key = next(c["key"] for c in created_chapters if c["data"]["title"] == matching_title)
    unmatched_key = next(c["key"] for c in created_chapters if c["data"]["title"] == unmatched_title)

    dryrun_output = tmp_path / "retrofit_dry.json"
    _run_script(
        "retrofit_chapter_links.py",
        ["--item-keys", f"{matching_key},{unmatched_key}", "--output", str(dryrun_output)],
    )
    dry_result = json.loads(dryrun_output.read_text())
    assert matching_key in {e["chapter_key"] for e in dry_result["would_link"]}
    assert unmatched_key in dry_result["no_match"]

    commit_output = tmp_path / "retrofit_commit.json"
    _run_script(
        "retrofit_chapter_links.py",
        ["--item-keys", f"{matching_key},{unmatched_key}", "--commit", "--output", str(commit_output)],
    )
    commit_result = json.loads(commit_output.read_text())
    assert not commit_result["failed"], commit_result["failed"]
    assert matching_key in {e["chapter_key"] for e in commit_result["linked"]}
    assert unmatched_key in commit_result["no_match"]

    chapter_item = zot.item(matching_key)
    assert f"{LIBRARY_SLUG}:{book_key}" in chapter_item["data"]["extra"]

    book_item = zot.item(book_key)
    assert f"{LIBRARY_SLUG}:{matching_key}" in book_item["data"]["extra"]

    unmatched_item = zot.item(unmatched_key)
    assert "X-Contained-By" not in unmatched_item["data"].get("extra", "")
