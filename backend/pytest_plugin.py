"""Shared pytest setup for core tests (``backend/tests``) and every backend plugin's
bundled tests (``backend/plugins/<id>/tests``).

``backend/conftest.py`` loads it for everything under ``backend/``. An external
plugin's tests use it with ``uv run pytest -p backend.pytest_plugin <plugin>/tests``.
"""

import os
import sys
from pathlib import Path

import pytest
from dotenv import load_dotenv

# Ensure testing mode is active before any backend code is imported.
# This prevents the subprocess dispatch path in document_processor from
# spawning real multiprocessing.Process workers during unit tests, which
# would consume 10+ GB RAM and crash the host.
os.environ.setdefault("TESTING", "true")

# Shared API keys in admin_settings.json are encrypted with AUTOINDEX_SECRET and
# cannot be stored without it; give the test process a throwaway one.
if "AUTOINDEX_SECRET" not in os.environ:
    from cryptography.fernet import Fernet as _Fernet
    os.environ["AUTOINDEX_SECRET"] = _Fernet.generate_key().decode()

# Add backend to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Load test environment configuration
# Priority: environment variables > .env > .env.test
_project_root = Path(__file__).resolve().parent.parent
_env_test_path = _project_root / ".env.test"
_env_path = _project_root / ".env"

# Load .env.test first (lowest priority) - don't override existing env vars
if _env_test_path.exists():
    load_dotenv(_env_test_path, override=False)

# Load .env if it exists (higher priority) - overrides .env.test but not env vars
# We need to manually handle this to get the right priority
if _env_path.exists():
    # Load .env values
    from dotenv import dotenv_values
    env_values = dotenv_values(_env_path)

    # Set values from .env, but only if not already in environment variables
    for key, value in env_values.items():
        if key not in os.environ:
            os.environ[key] = value


@pytest.fixture(autouse=True)
def _isolated_failed_store(tmp_path, monkeypatch):
    """Keep rag-failed records (and their events) out of the real data directory."""
    from backend.services import failed_attachments
    from backend.services.index_event_log import IndexEventLog

    def _store():
        return failed_attachments.FailedAttachmentStore(
            tmp_path / "failed_attachments.json", IndexEventLog(tmp_path / "index_events.jsonl")
        )

    monkeypatch.setattr(failed_attachments, "get_failed_store", _store)
    monkeypatch.setattr("backend.services.document_processor.get_failed_store", _store)
    monkeypatch.setattr("backend.api.indexed_tags.get_failed_store", _store)


def pytest_sessionfinish(session, exitstatus):
    """
    Pytest hook called after whole test session finished.

    Clean up PyTorch models to prevent Windows access violations during shutdown.
    """
    try:
        import gc
        import torch

        # Force garbage collection to clean up PyTorch models
        gc.collect()

        # Clear CUDA cache if available
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except (ImportError, RuntimeError, Exception):
        # Ignore any errors during cleanup
        pass
