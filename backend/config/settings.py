"""
Application settings management.

Loads configuration from environment variables and provides access to
hardware presets and storage paths.
"""

import os
from pathlib import Path
from typing import Optional, Literal
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

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

    # Model Configuration
    model_preset: str = Field(
        default="cpu-only",
        description="Hardware preset name"
    )

    model_weights_path: Path = Field(
        default_factory=lambda: Path.home() / ".cache" / "zotero-rag" / "models",
        description="Path to store model weights"
    )

    vector_db_path: Path = Field(
        default_factory=lambda: Path.home() / ".local" / "share" / "zotero-rag" / "qdrant",
        description="Path to Qdrant vector database"
    )

    # Zotero Configuration
    zotero_api_url: str = Field(
        default="http://localhost:23119",
        description="Zotero local API URL"
    )

    # Logging Configuration
    log_level: str = Field(default="INFO", description="Logging level")
    log_file: Optional[Path] = Field(
        default_factory=lambda: Path.home() / ".local" / "share" / "zotero-rag" / "logs" / "server.log",
        description="Path to log file"
    )

    # Application version
    version: str = Field(default="0.1.0", description="Backend version")

    # Vector Database Sync Configuration
    sync_enabled: bool = Field(default=False, description="Enable vector database synchronization")
    sync_backend: Literal["webdav", "s3"] = Field(default="webdav", description="Storage backend type")
    sync_auto_pull: bool = Field(default=True, description="Auto-pull on startup if remote is newer")
    sync_auto_push: bool = Field(default=False, description="Auto-push after indexing completes")
    sync_strategy: Literal["full", "delta", "hybrid"] = Field(default="full", description="Sync strategy")
    sync_hybrid_full_threshold_weeks: int = Field(default=4, description="Weeks before forcing full sync in hybrid mode")

    # WebDAV Configuration
    sync_webdav_url: Optional[str] = Field(default=None, description="WebDAV server URL")
    sync_webdav_username: Optional[str] = Field(default=None, description="WebDAV username")
    sync_webdav_password: Optional[str] = Field(default=None, description="WebDAV password")
    sync_webdav_base_path: str = Field(default="/zotero-rag/vectors/", description="WebDAV base path")

    # S3 Configuration
    sync_s3_bucket: Optional[str] = Field(default=None, description="S3 bucket name")
    sync_s3_region: str = Field(default="us-east-1", description="AWS region")
    sync_s3_prefix: str = Field(default="zotero-rag/vectors/", description="S3 key prefix")
    sync_s3_endpoint_url: Optional[str] = Field(default=None, description="S3-compatible endpoint URL")
    sync_s3_access_key: Optional[str] = Field(default=None, description="AWS access key")
    sync_s3_secret_key: Optional[str] = Field(default=None, description="AWS secret key")

    @field_validator("model_weights_path", "vector_db_path", "log_file", mode="before")
    @classmethod
    def expand_path(cls, v):
        """Expand user home directory in paths."""
        if v is None:
            return v
        path_str = str(v)
        # Expand ~ to home directory
        if path_str.startswith("~"):
            path_str = os.path.expanduser(path_str)
        return Path(path_str)

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
        self.model_weights_path.mkdir(parents=True, exist_ok=True)
        self.vector_db_path.mkdir(parents=True, exist_ok=True)

        if self.log_file:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)

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
