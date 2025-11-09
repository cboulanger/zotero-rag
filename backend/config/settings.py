"""
Application settings management.

Loads configuration from environment variables and provides access to
hardware presets and storage paths.
"""

import os
from pathlib import Path
from typing import Optional
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .presets import HardwarePreset, get_preset


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

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
        default_factory=lambda: Path.home() / ".local" / "share" / "zotero-rag" / "logs" / "app.log",
        description="Path to log file"
    )

    # Application version
    version: str = Field(default="0.1.0", description="Backend version")

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
