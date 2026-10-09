"""
Configuration presets for different hardware scenarios.

Presets are loaded from JSON files under ``<data_path>/presets/`` rather
than defined in this module. Bundled defaults live in
``backend/config/default_presets/`` and are copied into the data
directory — for any preset name not already present there — by
``ensure_default_presets``, called from ``Settings.ensure_directories()``
on every startup, and again defensively from ``get_preset``/``list_presets``
themselves on every call, so a caller that points ``data_path`` at a fresh
directory without going through ``Settings.ensure_directories()`` first
(e.g. a test fixture) still gets a working lookup. A user's edited or
added preset file is never overwritten. See
docs/superpowers/specs/2026-10-08-file-based-presets-design.md.

Loaded presets are cached in memory, keyed by (resolved data_path, name):
once a preset file has been read and validated, every subsequent
``get_preset()`` call for the same name returns a deep copy of the cached
object instead of re-reading the file. This matters because this module
is called from several ``async def`` FastAPI route handlers (directly and
via ``Settings.get_hardware_preset()``) — per this project's own "never
block the event loop" rule, synchronous disk I/O must not run on every
request, and a plain dict-of-HardwarePreset lookup (like the hardcoded
PRESETS dict this module replaced) is the cheapest way to guarantee that.
The trade-off: hand-editing an already-loaded preset's *file content*
while the backend is running requires a restart to take effect (adding a
*new* preset file, or editing one that hasn't been loaded yet in this
process, does not). ``ensure_default_presets``/seeding bookkeeping is
cached per-path separately so it isn't redone on every call either.
"""

import json
import logging
import os
import platform as _platform_module
import shutil
import tempfile
from pathlib import Path
from typing import List, Literal, Optional
from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

DEFAULT_PRESETS_DIR = Path(__file__).parent / "default_presets"

_seeded_paths: set[Path] = set()
_preset_cache: dict[tuple[Path, str], "HardwarePreset"] = {}


class EmbeddingConfig(BaseModel):
    """Configuration for embedding models."""

    model_type: Literal["local", "remote"] = "local"
    model_name: str = Field(..., description="Model identifier or API endpoint")
    model_kwargs: dict = Field(default_factory=dict, description="Additional model parameters")
    batch_size: int = Field(default=32, description="Batch size for embedding generation")
    cache_enabled: bool = Field(default=True, description="Enable content-hash based caching")
    health_check_provider: Optional[Literal["runpod"]] = Field(
        default=None,
        description="If set, GET /api/config/health checks this config's "
                    "shared_base_url_env endpoint via this provider's health API.",
    )


class LLMConfig(BaseModel):
    """Configuration for LLM models."""

    model_type: Literal["local", "remote"] = "local"
    model_names: List[str] = Field(..., description="Model identifier(s) — first entry is the default")
    quantization: Optional[Literal["4bit", "8bit", "none"]] = None
    max_context_length: int = Field(default=4096, description="Maximum context window size")
    max_answer_tokens: int = Field(default=2048, description="Maximum tokens for generated answers")
    temperature: float = Field(default=0.7, description="Sampling temperature")
    model_kwargs: dict = Field(default_factory=dict, description="Additional model parameters")
    models_status_url: Optional[str] = Field(
        default=None,
        description="URL to query for per-model availability metrics (KISSKI format: POST → data[].{id, demand, status})",
    )
    health_check_provider: Optional[Literal["runpod"]] = Field(
        default=None,
        description="If set, GET /api/config/health checks this config's "
                    "shared_base_url_env endpoint via this provider's health API.",
    )

    @field_validator("model_names", mode="before")
    @classmethod
    def coerce_to_list(cls, v: object) -> list:
        if isinstance(v, str):
            return [m.strip() for m in v.split(",") if m.strip()]
        return v  # type: ignore[return-value]

    @property
    def model_name(self) -> str:
        """Backward-compatible alias: returns the first (default) model name."""
        return self.model_names[0]


class RAGConfig(BaseModel):
    """Configuration for RAG retrieval."""

    top_k: int = Field(default=5, description="Number of chunks to retrieve")
    score_threshold: float = Field(default=0.3, description="Minimum similarity score (0.0-1.0)")
    max_chunk_size: int = Field(default=512, description="Maximum characters per chunk (passed to chunker as max_characters)")


class HardwarePreset(BaseModel):
    """Complete hardware-specific configuration preset."""

    name: str
    description: str
    embedding: EmbeddingConfig
    llm: LLMConfig
    rag: RAGConfig
    memory_budget_gb: float = Field(..., description="Estimated memory usage in GB")
    platform: Literal["any", "darwin", "linux", "windows"] = Field(
        default="any",
        description="Host OS this preset is meant for. 'any' (the default) is visible "
                    "everywhere; a preset naming a specific platform (e.g. a local-model "
                    "preset that needs Apple Silicon's MPS backend, or a Windows-oriented "
                    "preset) is hidden from listings shown to a client on any other "
                    "platform — see current_platform() and list_presets()'s platform= arg.",
    )
    provisioning_script: Optional[str] = Field(
        default=None,
        description="Repo-relative path to a script that provisions/wakes this "
                    "preset's remote endpoint(s) (see docs/superpowers/specs/"
                    "2026-10-09-endpoint-health-provisioning-design.md for the "
                    "script's --json output contract). Presence of this field "
                    "is what makes the Preferences pane show a 'Provision "
                    "endpoints' button.",
    )


def current_platform() -> str:
    """
    This host's normalized platform identifier: 'darwin', 'linux', or 'windows'.

    Matches the values a preset's own `platform` field can declare, so a
    caller can filter a preset listing with
    ``list_presets(data_path, platform=current_platform())``.
    """
    return _platform_module.system().lower()


def ensure_default_presets(data_path: Path) -> None:
    """
    Seed <data_path>/presets/ with the bundled default preset files.

    Copies each file under DEFAULT_PRESETS_DIR into the data directory only
    if no file of that name exists there yet — a user's edited or deleted
    preset is never touched or resurrected. A given data_path is seeded at
    most once per process (cached by resolved path): get_preset/list_presets
    call this defensively on every invocation (see module docstring), and
    redoing the directory scan on every call would be wasted work once a
    path is known to already be seeded.
    """
    resolved = Path(data_path).resolve()
    if resolved in _seeded_paths:
        return

    presets_dir = resolved / "presets"
    presets_dir.mkdir(parents=True, exist_ok=True)
    for default_file in DEFAULT_PRESETS_DIR.glob("*.json"):
        target = presets_dir / default_file.name
        if target.exists():
            continue
        # Copy via a temp file + atomic rename (same pattern as
        # admin_settings_store._atomic_write_json) rather than a direct
        # shutil.copy, so a concurrent reader (e.g. another process sharing
        # this data_path, like the cron indexer) can never observe a
        # partially-written preset file.
        fd, tmp_path = tempfile.mkstemp(dir=presets_dir, suffix=".tmp", prefix=default_file.stem + "_")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(default_file.read_bytes())
            os.replace(tmp_path, target)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    _seeded_paths.add(resolved)


def _load_preset_file(path: Path) -> HardwarePreset:
    """
    Load and validate a single preset file.

    The file's own "name" key, if present, is ignored — the preset name
    always comes from the filename stem, so a rename or copy can't leave
    a preset's identity out of sync with its content.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # ValueError also covers json.JSONDecodeError and UnicodeDecodeError
        # (raised by read_text on non-UTF-8 bytes) — both are "this file is
        # unreadable", not a schema problem, so both get the same message.
        raise ValueError(f"Could not read preset file '{path}': {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Preset file '{path}' must contain a JSON object")
    data["name"] = path.stem
    try:
        return HardwarePreset.model_validate(data)
    except Exception as exc:
        raise ValueError(f"Invalid preset file '{path}': {exc}") from exc


def get_preset(name: str, data_path: Optional[Path] = None) -> HardwarePreset:
    """
    Get a hardware preset by name, loading it from <data_path>/presets/<name>.json.

    Args:
        name: Preset name (e.g., "cpu-only")
        data_path: Base data directory. Defaults to the global Settings' data_path.

    Returns:
        HardwarePreset configuration (a fresh copy — safe for the caller to
        mutate, e.g. Settings.get_hardware_preset()'s EMBEDDING_BATCH_SIZE
        override — without corrupting the in-memory cache).

    Raises:
        ValueError: If preset name is not found, or its file is malformed/invalid.
    """
    if name != Path(name).name:
        # Rejects anything with a path separator or a ".."/"." component
        # (e.g. "../../etc/passwd") before it ever reaches the filesystem —
        # defense in depth should a less-trusted caller ever reach this
        # function directly, even though today's only HTTP-reachable caller
        # (backend/api/config.py) already validates against list_presets().
        raise ValueError(f"Invalid preset name: {name!r}")

    if data_path is None:
        from backend.config.settings import get_settings
        data_path = get_settings().data_path
    resolved = Path(data_path).resolve()
    ensure_default_presets(resolved)

    cache_key = (resolved, name)
    cached = _preset_cache.get(cache_key)
    if cached is not None:
        return cached.model_copy(deep=True)

    path = resolved / "presets" / f"{name}.json"
    if not path.exists():
        available = ", ".join(list_presets(resolved))
        raise ValueError(f"Unknown preset '{name}'. Available: {available}")

    preset = _load_preset_file(path)
    _preset_cache[cache_key] = preset
    return preset.model_copy(deep=True)


def list_presets(data_path: Optional[Path] = None, *, platform: Optional[str] = None) -> list[str]:
    """
    List all available preset names found under <data_path>/presets/.

    A file that fails to parse or validate is skipped (with a logged
    warning) rather than raising, so one broken custom preset doesn't hide
    every other valid preset. Unlike get_preset(), this always re-scans the
    directory (not cached) so a newly added or removed preset file shows up
    immediately — this is only called from low-frequency admin/tooling
    paths, not the hot query path, so that cost is acceptable.

    Args:
        platform: If given, a preset whose own `platform` field names a
            specific platform other than this one is excluded — e.g.
            list_presets(data_path, platform=current_platform()) hides the
            bundled Windows/Apple-Silicon-only presets on a Linux host.
            Omit it (the default) for an unfiltered listing of every
            preset regardless of platform (e.g. internal tooling that
            wants to see everything).
    """
    if data_path is None:
        from backend.config.settings import get_settings
        data_path = get_settings().data_path
    resolved = Path(data_path).resolve()
    ensure_default_presets(resolved)

    presets_dir = resolved / "presets"
    if not presets_dir.is_dir():
        return []

    names = []
    for path in sorted(presets_dir.glob("*.json")):
        try:
            preset = _load_preset_file(path)
        except ValueError as exc:
            logger.warning("Skipping invalid preset file: %s", exc)
            continue
        if platform is not None and preset.platform not in ("any", platform):
            continue
        names.append(path.stem)
    return names
