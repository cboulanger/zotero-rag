"""Read/write the book<->chapter linking scheme stored in a Zotero item's
Extra field: X-Contained-By, X-Contains, X-Chapter-Pdf-Range.

See docs/superpowers/specs/2026-07-24-chapter-segmentation-linking-design.md
section 2 for the full rationale, in particular why X-Chapter-Pdf-Range
(PDF page-index space) is kept entirely separate from Zotero's native
`pages` field (printed/citation page-number space, human-editable).
"""

import re
from dataclasses import dataclass, field


@dataclass
class ChapterLinks:
    contained_by: str | None = None
    contains: list[str] = field(default_factory=list)
    pdf_ranges: dict[str, tuple[int, int]] = field(default_factory=dict)


_CONTAINED_BY_LINE = re.compile(r"^X-Contained-By:")
_CONTAINS_LINE = re.compile(r"^X-Contains:")
_PDF_RANGE_LINE = re.compile(r"^X-Chapter-Pdf-Range:")

_CONTAINED_BY_RE = re.compile(r"^X-Contained-By:\s*(.+)$", re.MULTILINE)
_CONTAINS_RE = re.compile(r"^X-Contains:\s*(.+)$", re.MULTILINE)
_PDF_RANGE_RE = re.compile(r"^X-Chapter-Pdf-Range:\s*(.+)$", re.MULTILINE)


def parse_library_slug(slug: str) -> tuple[str, str, str]:
    """Parse a Zotero slug ("users/12345" / "groups/678") into
    (library_type, numeric_id, backend_library_id), matching the backend's
    own u{id}/{id} convention (see backend/api/public_query.py).
    """
    prefix, numeric_id = slug.split("/", 1)
    library_type = "user" if prefix == "users" else "group"
    backend_library_id = f"u{numeric_id}" if library_type == "user" else numeric_id
    return library_type, numeric_id, backend_library_id


def format_chapter_id(slug: str, item_key: str) -> str:
    """Format a cross-library-safe chapter/book identifier: '<slug>:<item_key>'."""
    return f"{slug}:{item_key}"


def parse_links(extra: str) -> ChapterLinks:
    """Extract X-Contained-By / X-Contains / X-Chapter-Pdf-Range from an
    item's Extra field text. Unrelated lines are ignored.
    """
    extra = extra or ""
    links = ChapterLinks()

    m = _CONTAINED_BY_RE.search(extra)
    if m:
        links.contained_by = m.group(1).strip()

    m = _CONTAINS_RE.search(extra)
    if m:
        links.contains = [x.strip() for x in m.group(1).split(",") if x.strip()]

    m = _PDF_RANGE_RE.search(extra)
    if m:
        for entry in m.group(1).split(","):
            entry = entry.strip()
            if not entry:
                continue
            # entry format: "<slug>:<item_key>:<start>-<end>" — the chapter id
            # itself contains exactly one colon (slug:item_key), so splitting
            # from the right by one isolates the trailing range unambiguously.
            chapter_id, _, range_part = entry.rpartition(":")
            if not chapter_id or "-" not in range_part:
                continue
            start_s, end_s = range_part.split("-", 1)
            try:
                links.pdf_ranges[chapter_id] = (int(start_s), int(end_s))
            except ValueError:
                continue

    return links


def write_links(
    extra: str,
    *,
    contained_by: str | None = None,
    contains: list[str] | None = None,
    pdf_ranges: dict[str, tuple[int, int]] | None = None,
) -> str:
    """Replace (or append) the X-Contained-By / X-Contains /
    X-Chapter-Pdf-Range lines in an item's Extra field text. Only keys
    explicitly passed (non-None) are touched; everything else in `extra`
    (including unrelated X-* lines, e.g. Better BibTeX citekeys) is left
    untouched. Idempotent: calling twice with the same values is a no-op.
    """
    lines = (extra or "").splitlines()

    if contained_by is not None:
        lines = [ln for ln in lines if not _CONTAINED_BY_LINE.match(ln)]
        lines.append(f"X-Contained-By: {contained_by}")

    if contains is not None:
        lines = [ln for ln in lines if not _CONTAINS_LINE.match(ln)]
        if contains:
            lines.append(f"X-Contains: {','.join(contains)}")

    if pdf_ranges is not None:
        lines = [ln for ln in lines if not _PDF_RANGE_LINE.match(ln)]
        if pdf_ranges:
            entries = [f"{cid}:{start}-{end}" for cid, (start, end) in pdf_ranges.items()]
            lines.append(f"X-Chapter-Pdf-Range: {','.join(entries)}")

    return "\n".join(lines)
