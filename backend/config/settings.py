"""
Application settings management.

Loads configuration from environment variables and provides access to
hardware presets and storage paths.
"""

import os
from pathlib import Path
from typing import Optional
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from backend.__version__ import __version__
from .presets import HardwarePreset, get_preset


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
    api_key: Optional[str] = Field(
        default=None,
        description="API key for remote access (X-API-Key header). "
                    "When set, all requests must include this key. "
                    "Leave unset for local-only deployments."
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
        default=300,
        description="Per-request timeout in seconds for the kreuzberg sidecar. "
                    "Large HTML snapshots and OCR-heavy PDFs may need >120s. Default: 300."
    )

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

    require_registration: bool = Field(
        default=True,
        description="Require users to register before indexing. "
                    "Automatically skipped when api_host is localhost or 127.0.0.1."
    )

    testing: bool = Field(
        default=False,
        description="Testing mode: use mock embedding/LLM services (no API keys or model downloads required)"
    )

    # Application version
    version: str = Field(default=__version__, description="Backend version")

    @field_validator("data_path", "model_weights_path", "vector_db_path", "log_file", "registrations_path", mode="before")
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
        """Get the configured hardware preset."""
        return get_preset(self.model_preset)

    def ensure_directories(self):
        """Create necessary directories if they don't exist."""
        self.data_path.mkdir(parents=True, exist_ok=True)
        self.model_weights_path.mkdir(parents=True, exist_ok=True)
        self.vector_db_path.mkdir(parents=True, exist_ok=True)
        if self.log_file:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
        if self.registrations_path:
            self.registrations_path.parent.mkdir(parents=True, exist_ok=True)

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
