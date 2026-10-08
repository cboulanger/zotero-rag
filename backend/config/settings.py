"""
Application settings management.

Loads configuration from environment variables and provides access to
hardware presets and storage paths.
"""

import logging
import os
from pathlib import Path
from typing import Annotated, Optional
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_settings import NoDecode

from backend.__version__ import __version__
from .presets import HardwarePreset, get_preset, ensure_default_presets

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        # Look for .env in project root (parent of backend/)
        env_file=str((Path(__file__).parent.parent.parent / ".env").resolve()),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    def __init__(self, **kwargs):
        """Initialize settings."""
        super().__init__(**kwargs)

    # API Configuration
    api_host: str = Field(default="localhost", description="API server host")
    api_port: int = Field(default=8119, description="API server port")
    public_libraries_config: Optional[str] = Field(
        default=None,
        description="Path to JSON file listing publicly exposed library slugs "
                    "(users/{id} or groups/{id}) with title and description. "
                    "When set, enables the /public/ web UI for unauthenticated RAG queries."
    )
    allowed_origins: list[str] = Field(
        default=["*"],
        description="CORS allowed origins. Use ['*'] for local deployments. "
                    "Set to specific origins for remote deployments."
    )

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def parse_allowed_origins(cls, v):
        if isinstance(v, list):
            return v
        if isinstance(v, str):
            import json
            try:
                parsed = json.loads(v)
                return parsed if isinstance(parsed, list) else [parsed]
            except (json.JSONDecodeError, ValueError):
                return [o.strip() for o in v.split(",") if o.strip()]
        return v

    # Model Configuration
    model_preset: str = Field(
        default="cpu-only",
        description="Hardware preset name"
    )

    # Abstract fallback indexing
    min_abstract_words: int = Field(
        default=100,
        description="Minimum word count for abstractNote to be used as fallback when no attachment is available"
    )

    # Follow-up chat
    metadata_narrowing_threshold: int = Field(
        default=50,
        description="MetadataAgent: max distinct catalog items to answer directly; "
                    "above this, ask the user to narrow their question instead."
    )
    max_conversation_context_chars: int = Field(
        default=6000,
        description="Max characters of prior Q&A turns included in the routing prompt "
                    "for a follow-up chat turn; older turns are dropped once this is exceeded."
    )

    # Extraction backend
    extractor_backend: str = Field(
        default="kreuzberg",
        description="Document extraction backend: 'kreuzberg' (default) or 'legacy' (pypdf+spaCy)"
    )
    ocr_enabled: bool = Field(
        default=True,
        description="Enable OCR for image-only pages (requires Tesseract). "
                    "Set to False when Tesseract is not installed or OCR is not needed."
    )
    kreuzberg_url: str = Field(
        default="http://localhost:8100",
        description="URL of the kreuzberg sidecar HTTP API. "
                    "Used when extractor_backend='kreuzberg' and the kreuzberg container is running."
    )
    kreuzberg_timeout_seconds: int = Field(
        default=1800,
        description="Upper bound (seconds) for the kreuzberg sidecar's per-request timeout, "
                    "which is otherwise scaled down automatically for smaller documents. "
                    "Raise this if large OCR-heavy PDFs or HTML snapshots hit skipped_timeout."
    )
    pdf_split_threshold: int = Field(
        default=50 * 1024 ** 2,
        description="PDFs larger than this are split into parts before sending to kreuzberg. "
                    "Accepts '50MB', '1GB', or a raw byte count.",
    )
    pdf_split_target_part_size: int = Field(
        default=30 * 1024 ** 2,
        description="Target byte size of each part when splitting a large PDF. "
                    "Accepts '30MB', '500KB', or a raw byte count.",
    )
    kreuzberg_max_content_bytes: int = Field(
        default=200 * 1024 ** 2,
        description="Hard cap on the bytes sent to the kreuzberg sidecar in a single request. "
                    "Applies both to a whole document and to each part after PDF splitting — "
                    "including the fallback that sends the whole file when splitting itself "
                    "fails, which would otherwise bypass pdf_split_threshold entirely. Refusing "
                    "outright above this size (raising AttachmentTooLargeError, surfaced to the "
                    "Fix Unavailable dialog as skipped_too_large) protects the sidecar's memory "
                    "limit — production saw an 8GB OOM kill from a single 329MB scanned PDF. "
                    "Accepts '200MB', '1GB', or a raw byte count.",
    )

    @field_validator(
        "pdf_split_threshold", "pdf_split_target_part_size", "kreuzberg_max_content_bytes", mode="before"
    )
    @classmethod
    def _parse_size_fields(cls, v: int | str) -> int:
        if isinstance(v, int):
            return v
        s = str(v).strip().upper()
        for suffix, mult in [("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)]:
            if s.endswith(suffix):
                return int(float(s[: -len(suffix)]) * mult)
        return int(s)

    # Data storage — all paths default to subdirs of data_path
    data_path: Path = Field(
        default_factory=lambda: Path(__file__).parent.parent.parent / "data",
        description="Base directory for all persistent data (models, vector DB, logs, system). "
                    "Defaults to <project_root>/data. Override individual paths below if needed."
    )
    model_weights_path: Optional[Path] = Field(
        default=None,
        description="Path to store model weights. Defaults to <data_path>/models."
    )
    vector_db_path: Optional[Path] = Field(
        default=None,
        description="Path to Qdrant vector database (local file mode only). Defaults to <data_path>/qdrant."
    )
    registrations_path: Optional[Path] = Field(
        default=None,
        description="Path to the library/user registrations JSON file. Defaults to <data_path>/system/registrations.json."
    )
    autoindex_secret: Optional[str] = Field(
        default=None,
        description="Fernet secret (urlsafe base64, 32 bytes) for encrypting auto-index keys. "
                    "If unset, the auto-indexing key feature is disabled.",
    )
    autoindex_keys_path: Optional[Path] = Field(
        default=None,
        description="Path to the encrypted auto-index keys JSON. Defaults to "
                    "<data_path>/system/autoindex_keys.json.",
    )
    autoindex_interval_minutes: Optional[int] = Field(
        default=None,
        gt=0,
        description="If set, the backend runs its own in-process scheduler that "
                    "triggers an auto-index run every N minutes, instead of "
                    "relying on an external OS cron job. Unset (default) leaves "
                    "scheduling entirely to the operator (see docs/cron-indexing.md)."
    )
    autoindex_min_free_disk_percent: float = Field(
        default=15.0,
        description="Minimum free disk space (as a percentage of total size on the "
                    "data_path volume) required before an auto-index run starts. "
                    "Below this, bin/index_libraries.py skips the run and logs why "
                    "instead of writing more data — Qdrant's segment optimizer needs "
                    "multi-GB of free space to merge segments, and running it down to "
                    "near-zero free space leaves the optimizer stuck (segments pile up "
                    "unmerged, which keeps the disk full)."
    )
    autoindex_reserved_cpus: int = Field(
        default=1,
        ge=0,
        description="Number of CPUs to leave free for serving RAG queries while an "
                    "indexing run is active. bin/index_libraries.py restricts its own "
                    "CPU affinity to (available CPUs - this value), down to a minimum "
                    "of 1 CPU for indexing itself. Set to 0 to disable (indexing may "
                    "use all CPUs). No-op on platforms without sched_getaffinity/"
                    "sched_setaffinity (e.g. macOS dev)."
    )

    ntfy_topic_url: Optional[str] = Field(
        default=None,
        description="Full ntfy.sh (or self-hosted ntfy) topic URL, e.g. "
                    "'https://ntfy.sh/your-private-topic'. If set, the production "
                    "health check posts an alert here when it starts/stops failing. "
                    "Unset disables alerting (the health check still logs locally "
                    "either way)."
    )
    health_check_interval_minutes: Optional[int] = Field(
        default=None,
        gt=0,
        description="If set, the backend runs its own in-process scheduler that "
                    "checks disk space and Qdrant collection health every N minutes "
                    "(see backend/services/health_check.py), the same way "
                    "autoindex_interval_minutes runs the auto-indexer — this way the "
                    "check survives every redeploy automatically, with nothing living "
                    "only on the host. Unset (default) disables the periodic check; "
                    "bin/check_production_health.py can still be run manually/via cron."
    )
    health_check_min_free_disk_percent: float = Field(
        default=15.0,
        description="Minimum free disk space (as a percentage of total size on the "
                    "data_path volume) before the health check reports a problem. "
                    "Separate from autoindex_min_free_disk_percent so the health "
                    "check can warn earlier than the hard indexing-skip floor."
    )

    qdrant_url: Optional[str] = Field(
        default=None,
        description="Qdrant server URL (e.g. http://qdrant:6333). If set, uses server mode instead of local file mode."
    )

    qdrant_timeout: int = Field(
        default=30,
        description="Qdrant client request timeout in seconds. Retries double this up to 3 attempts."
    )

    # Logging Configuration
    log_level: str = Field(default="INFO", description="Logging level")
    log_file: Optional[Path] = Field(
        default=None,
        description="Path to log file. Defaults to <data_path>/logs/server.log."
    )

    zotero_api_key: Optional[str] = Field(
        default=None,
        description="Zotero API key for cron indexing via api.zotero.org. "
                    "Create at https://www.zotero.org/settings/keys"
    )

    authorized_group_id: Optional[int] = Field(
        default=None,
        description="Zotero group ID that gates access to this instance in remote mode. "
                    "A caller's validated Zotero key must grant read access to "
                    "groups/<id> (i.e. the caller is a member of this group) to be "
                    "authorized. Combines with AUTHORIZED_USER_IDS via OR. "
                    "Ignored on loopback deployments (api_host=localhost/127.0.0.1)."
    )
    authorized_user_ids: Annotated[list[int], NoDecode] = Field(
        default=[],
        description="Explicit allowlist of Zotero user IDs authorized to use this "
                    "instance in remote mode, in addition to AUTHORIZED_GROUP_ID "
                    "membership (OR semantics). Comma-separated list of integers "
                    "in the environment. At least one of AUTHORIZED_GROUP_ID / "
                    "AUTHORIZED_USER_IDS must be set for the server to start in "
                    "remote mode — see backend.services.access_gate.assert_safe_to_start."
    )

    @field_validator("authorized_user_ids", mode="before")
    @classmethod
    def parse_authorized_user_ids(cls, v):
        if isinstance(v, list):
            return [int(x) for x in v]
        if isinstance(v, str):
            return [int(x.strip()) for x in v.split(",") if x.strip()]
        return v

    testing: bool = Field(
        default=False,
        description="Testing mode: use mock embedding/LLM services (no API keys or model downloads required)"
    )

    # Application version
    version: str = Field(default=__version__, description="Backend version")

    @field_validator("data_path", "model_weights_path", "vector_db_path", "log_file", "registrations_path", "autoindex_keys_path", mode="before")
    @classmethod
    def expand_path(cls, v):
        """Expand user home directory in paths."""
        if v is None:
            return v
        path_str = str(v)
        if path_str.startswith("~"):
            path_str = os.path.expanduser(path_str)
        return Path(path_str)

    @model_validator(mode="after")
    def set_derived_paths(self) -> "Settings":
        """Fill in paths that were not explicitly set, using data_path as base."""
        if self.model_weights_path is None:
            self.model_weights_path = self.data_path / "models"
        if self.vector_db_path is None:
            self.vector_db_path = self.data_path / "qdrant"
        if self.log_file is None:
            self.log_file = self.data_path / "logs" / "server.log"
        if self.registrations_path is None:
            self.registrations_path = self.data_path / "system" / "registrations.json"
        if self.autoindex_keys_path is None:
            self.autoindex_keys_path = self.data_path / "system" / "autoindex_keys.json"
        return self

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v):
        """Validate log level."""
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        v_upper = v.upper()
        if v_upper not in valid_levels:
            raise ValueError(f"Invalid log level. Must be one of: {valid_levels}")
        return v_upper

    def get_hardware_preset(self) -> HardwarePreset:
        """Get the configured hardware preset.

        An admin-set runtime override (backend.services.admin_settings_store,
        set via POST /api/config) takes precedence over MODEL_PRESET, letting
        an admin hot-swap between presets without a restart — see
        docs/superpowers/specs/2026-10-08-dynamic-remote-preset-config-design.md.
        An override naming an unknown preset (e.g. after a code change removes
        it) is ignored with a warning, falling back to MODEL_PRESET.

        EMBEDDING_BATCH_SIZE, if set in the environment, overrides
        embedding.batch_size on the returned preset regardless of which
        preset is active — a documented memory-tuning knob for the cron
        indexer on low-RAM hosts (see docs/cron-indexing.md). A non-integer
        value is ignored with a warning rather than raising, since this is
        called from request-handling code, not just at startup.
        """
        from backend.services.admin_settings_store import get_active_preset_override
        override = get_active_preset_override(self.data_path)
        if override:
            try:
                preset = get_preset(override, self.data_path)
            except ValueError:
                logger.warning(
                    "active_preset_override=%r is not a known preset; falling back to MODEL_PRESET=%r",
                    override, self.model_preset,
                )
                preset = get_preset(self.model_preset, self.data_path)
        else:
            preset = get_preset(self.model_preset, self.data_path)

        batch_size_override = os.environ.get("EMBEDDING_BATCH_SIZE")
        if batch_size_override:
            try:
                preset.embedding.batch_size = int(batch_size_override)
            except ValueError:
                logger.warning(
                    "EMBEDDING_BATCH_SIZE=%r is not a valid integer; ignoring it and using "
                    "the preset's own default (%d)",
                    batch_size_override, preset.embedding.batch_size,
                )

        return preset

    def ensure_directories(self):
        """Create necessary directories if they don't exist."""
        self.data_path.mkdir(parents=True, exist_ok=True)
        self.model_weights_path.mkdir(parents=True, exist_ok=True)
        self.vector_db_path.mkdir(parents=True, exist_ok=True)
        if self.log_file:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
        if self.registrations_path:
            self.registrations_path.parent.mkdir(parents=True, exist_ok=True)
        if self.autoindex_keys_path:
            self.autoindex_keys_path.parent.mkdir(parents=True, exist_ok=True)
        ensure_default_presets(self.data_path)

    def get_api_key(self, env_var_name: str) -> Optional[str]:
        """
        Get API key from environment variable.

        This method dynamically reads API keys from environment variables,
        allowing for flexible configuration without hardcoding provider-specific fields.

        Args:
            env_var_name: Name of the environment variable (e.g., "OPENAI_API_KEY", "KISSKI_API_KEY")

        Returns:
            API key if available, None otherwise

        Examples:
            >>> settings.get_api_key("OPENAI_API_KEY")
            "sk-..."
            >>> settings.get_api_key("KISSKI_API_KEY")
            "your-kisski-key"
        """
        return os.getenv(env_var_name)


# Global settings instance
_settings: Optional[Settings] = None


def get_settings() -> Settings:
    """
    Get the global settings instance.

    Creates and caches the settings on first call.
    """
    global _settings
    if _settings is None:
        _settings = Settings()
        _settings.ensure_directories()
    return _settings


def reset_settings():
    """Reset the global settings instance (mainly for testing)."""
    global _settings
    _settings = None
