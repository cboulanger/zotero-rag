#!/usr/bin/env python
"""Test environment loading."""
from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Check .env file exists and has correct content
env_path = Path(".env").resolve()
print(f"Env file: {env_path}")
print(f"Exists: {env_path.exists()}")
if env_path.exists():
    with open(env_path) as f:
        for line in f:
            if "MODEL_PRESET" in line:
                print(f"Found: {line.strip()}")

class TestSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )
    model_preset: str = Field(default="cpu-only")

settings = TestSettings()
print(f"\nLoaded model_preset: {settings.model_preset}")
