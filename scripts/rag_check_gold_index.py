"""Console-script shim for ``skills/rag-quality-eval/scripts/check_gold_index.py``.

The skill directory name contains a hyphen, so it cannot be named in a
``[project.scripts]`` dotted import path; this loads the script by file path
instead. Exposed as ``uv run rag-check-gold-index``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "skills" / "rag-quality-eval" / "scripts" / "check_gold_index.py"
)


def main() -> None:
    """Entry point for the ``rag-check-gold-index`` console script."""
    spec = importlib.util.spec_from_file_location("check_gold_index", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.exit(module.main())
