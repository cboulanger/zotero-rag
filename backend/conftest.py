"""Shared test setup for backend/tests and the plugins' bundled tests (see backend/pytest_plugin.py)."""

from backend.pytest_plugin import *  # noqa: F401,F403
from backend.pytest_plugin import _isolated_failed_store  # noqa: F401  (underscore names are not star-exported)
