"""Helpers shared between backend/services/chapter_segmentation.py's
PDF-internal heuristic and the backend/services/chapter_evidence/
strategies (external/local metadata sources) -- kept in one place so
"is this title a part-divider/back-matter section" and "what year does
this date string represent" are answered identically everywhere. See
docs/superpowers/specs/2026-08-01-chapter-segmentation-strategy-pipeline-design.md
section 9.
"""

import re
import unicodedata

# Entries that are structural markers, not chapters: part dividers
# ("Teil 1: ...", "Part II ...", "PARTIE I. ...") and standard back-matter
# lists (index, contributors, bibliography, ...).
_PART_DIVIDER_RE = re.compile(
    r"^(?:teil|part|partie|section|abschnitt)\b[\s.:]*(?:[0-9]+|[ivxlcdm]+)?\b", re.IGNORECASE
)
_BACK_MATTER_TITLES = {
    "contributors", "notes on contributors", "about the authors", "about the contributors",
    "index", "indexes", "name index", "subject index",
    "register", "sachregister", "personenregister", "namensregister",
    "bibliography", "bibliographie", "literatur", "literaturverzeichnis", "references",
    "quellenverzeichnis", "acknowledgments", "acknowledgements", "danksagung", "remerciements",
    "glossary", "glossar", "glossaire", "abbreviations", "abkurzungsverzeichnis", "abkurzungen",
    "list of figures", "list of tables", "tabellenverzeichnis", "abbildungsverzeichnis",
    "les auteurs", "auteurs", "autorinnen und autoren", "die autorinnen und autoren",
    "verzeichnis der autorinnen und autoren", "zu den autorinnen und autoren",
    "liste des auteurs", "memento", "autorinnenverzeichnis", "autorenverzeichnis",
    "contents", "table of contents", "inhalt", "inhaltsverzeichnis", "inhaltsubersicht",
    "sommaire", "table des matieres",
}


def _normalized_title(title: str) -> str:
    decomposed = unicodedata.normalize("NFKD", title.lower())
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", stripped)).strip()


def _is_part_divider(title: str) -> bool:
    return _PART_DIVIDER_RE.match(title) is not None


def _is_back_matter(title: str) -> bool:
    normalized = _normalized_title(title)
    if normalized in _BACK_MATTER_TITLES:
        return True
    # Author-marker TOCs fold the author into the entry title
    # ("MEMENTO par Lucie Daudin") -- test the part before the marker too.
    stripped = re.split(r"\b(?:par|by)\b", normalized, maxsplit=1)[0].strip()
    return stripped in _BACK_MATTER_TITLES


def year_from_date(date_str: str | None) -> int | None:
    """Extracts a 4-digit year token from a Zotero `date` field string
    ("2019-05", "May 2019", ...). Returns None on anything unparseable --
    never raises."""
    if not date_str:
        return None
    for token in date_str.replace("-", " ").split():
        if token.isdigit() and len(token) == 4:
            return int(token)
    return None
