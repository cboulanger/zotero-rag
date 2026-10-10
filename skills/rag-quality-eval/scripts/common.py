"""Shared helpers for the rag-quality-eval scripts.

The skill runs inside the repository checkout, so it reuses backend code
(settings, presets, key store, the RAG engine's citation regex) wherever that
is importable; every such import is guarded so the scripts also work against a
remote instance (``--no-local``) using HTTP only. Only the standard library is
needed for HTTP, so the scripts run with plain ``python`` as well as
``uv run python`` (use ``uv run python`` for the local-code features).
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

SKILL_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = SKILL_DIR.parent.parent
GOLD_PATH = SKILL_DIR / "gold" / "questions.json"
DEFAULT_URL = "http://localhost:8119"
ZOTERO_API = "https://api.zotero.org"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def load_gold(path: Path = GOLD_PATH) -> dict:
    """Load the gold-standard question file."""
    return json.loads(path.read_text(encoding="utf-8"))


def local_backend_available() -> bool:
    """True if backend modules (settings, presets) can be imported in-process."""
    try:
        import backend.config.settings  # noqa: F401
        return True
    except Exception:
        return False


def local_settings() -> Any:
    """The backend ``Settings`` (reads ``.env``) or None when not importable."""
    try:
        from backend.config.settings import get_settings
        return get_settings()
    except Exception:
        return None


def strip_html(text: str) -> str:
    """Render the API's HTML answer as plain text (keeps list/paragraph breaks)."""
    text = re.sub(r"</(p|li|h[1-6]|ul|ol|tr|blockquote)>", "\n", text, flags=re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def http_json(
    method: str,
    url: str,
    headers: Optional[dict[str, str]] = None,
    body: Optional[dict] = None,
    timeout: float = 300.0,
) -> tuple[int, Any]:
    """Perform an HTTP request, returning ``(status, parsed_json_or_text)``.

    Never raises on an HTTP error status; network errors surface as status 0.
    """
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in {"Content-Type": "application/json", **(headers or {})}.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # URLError, timeout, connection reset
        return 0, f"{type(exc).__name__}: {exc}"
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw


def _store_entries() -> list[tuple[str, str, Any]]:
    """Decrypted key-store entries ``(fingerprint, zotero_key, entry)`` (never printed)."""
    try:
        from backend.services.secret_store import get_key_store
        store = get_key_store()
        return list(store.iter_decrypted()) if store.enabled else []
    except Exception:
        return []


def get_zotero_key(env_var: str = "RAG_EVAL_ZOTERO_KEY") -> Optional[str]:
    """Zotero identity key from ``$RAG_EVAL_ZOTERO_KEY`` or the encrypted key store.

    Returns None when neither exists (fine for a loopback dev instance that has
    no ``AUTHORIZED_GROUP_ID``).
    """
    if os.environ.get(env_var):
        return os.environ[env_var]
    entries = _store_entries()
    return entries[0][1] if entries else None


def provider_headers(preset: Any, extra: Optional[dict[str, str]] = None) -> dict[str, str]:
    """Per-request provider-key headers a preset needs (values never printed).

    For each personal ``api_key`` field the preset requires (embedding + LLM),
    use ``$<KEY_NAME>`` if set, else the matching key stored in the auto-index
    key store. Admin-set ``shared_*`` fields are resolved server-side.
    """
    headers: dict[str, str] = dict(extra or {})
    if preset is None:
        return headers
    try:
        from backend.services.embeddings import RemoteEmbeddingService
        from backend.services.llm import RemoteLLMService
    except Exception:
        return headers
    fields = RemoteEmbeddingService.required_client_fields(preset.embedding)
    fields += RemoteLLMService.required_client_fields_for_config(preset.llm)
    stored: dict[str, str] = {}
    try:
        from backend.services.secret_store import get_key_store
        store = get_key_store()
        for fp, _zkey, _entry in _store_entries():
            got = store.get_decrypted_embedding_key(fp)
            if got:
                stored.setdefault(got[0], got[1])
    except Exception:
        pass
    for field in fields:
        if field.get("kind", "api_key") != "api_key":
            continue
        value = os.environ.get(field["key_name"]) or stored.get(field["key_name"])
        if value:
            headers[field["header_name"]] = value
    return headers


def auth_headers(zotero_key: Optional[str]) -> dict[str, str]:
    """Identity header for the backend (empty on a loopback dev instance)."""
    return {"X-Zotero-API-Key": zotero_key} if zotero_key else {}


def resolve_library_id(base_url: str, zotero_key: Optional[str], gold: dict) -> str:
    """The backend ``library_id`` of the gold library, verified via /api/libraries."""
    wanted = gold["library"]["library_id"]
    status, data = http_json("GET", f"{base_url}/api/libraries", auth_headers(zotero_key), timeout=30)
    if status == 200 and isinstance(data, list):
        if not any(lib.get("library_id") == wanted for lib in data):
            raise SystemExit(
                f"[FAIL] gold library {wanted} ({gold['library']['name']}) is not listed by "
                f"{base_url}/api/libraries - index it first."
            )
    return wanted
