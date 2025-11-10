"""
Pytest configuration and shared fixtures for integration tests.

This module provides:
1. Pre-flight environment validation
2. Shared fixtures for integration tests
3. Pytest hooks for test collection and setup
4. Automatic loading of .env.test configuration
"""

import os
import sys
import pytest
import asyncio
from typing import Optional
from pathlib import Path
from dotenv import load_dotenv

# Add backend to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# Load test environment configuration
# Priority: environment variables > .env > .env.test
_project_root = Path(__file__).parent.parent.parent
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


# ============================================================================
# Pre-flight Environment Validation
# ============================================================================

def check_zotero_available() -> tuple[bool, Optional[str]]:
    """
    Check if Zotero is running and accessible.

    Returns:
        (available, error_message): True if Zotero is running, False otherwise
    """
    try:
        import httpx

        # Try to connect to Zotero local API
        response = httpx.get("http://localhost:23119/connector/ping", timeout=2.0)

        if response.status_code == 200:
            return True, None
        else:
            return False, f"Zotero API responded with status {response.status_code}"
    except httpx.ConnectError:
        return False, "Cannot connect to Zotero on localhost:23119. Is Zotero running?"
    except Exception as e:
        return False, f"Error checking Zotero: {e}"


def check_api_key_available(env_var: str = "KISSKI_API_KEY") -> tuple[bool, Optional[str]]:
    """
    Check if required API key is available.

    Args:
        env_var: Environment variable name for API key

    Returns:
        (available, error_message): True if key is set, False otherwise
    """
    api_key = os.getenv(env_var)

    if api_key:
        return True, None
    else:
        return False, f"API key not found: {env_var} environment variable not set"


def check_model_preset_valid() -> tuple[bool, Optional[str]]:
    """
    Check if MODEL_PRESET environment variable is valid.

    Returns:
        (valid, error_message): True if preset is valid, False otherwise
    """
    from backend.config.presets import get_preset

    preset_name = os.getenv("MODEL_PRESET", "remote-kisski")
    preset = get_preset(preset_name)

    if preset is None:
        return False, f"Invalid MODEL_PRESET: '{preset_name}' not found"

    return True, None


def check_test_library_synced() -> tuple[bool, Optional[str]]:
    """
    Check if the test library is synced in Zotero.

    Returns:
        (synced, error_message): True if test library is accessible, False otherwise
    """
    from backend.zotero.local_api import ZoteroLocalAPI

    try:
        client = ZoteroLocalAPI()

        # Run async check in sync context
        async def _check():
            libraries = await client.list_libraries()

            if not libraries:
                return False, "No libraries found in Zotero"

            # Look for test library
            test_lib_id = get_test_library_id()
            library_ids = [lib["id"] for lib in libraries]

            if test_lib_id not in library_ids:
                test_url = get_test_library_url()
                return False, (
                    f"Test library {test_lib_id} not found. "
                    f"Please sync test group: {test_url}"
                )

            return True, None

        # Run the async function
        return asyncio.run(_check())

    except Exception as e:
        return False, f"Error checking test library: {e}"


def validate_integration_environment(verbose: bool = True) -> dict[str, tuple[bool, Optional[str]]]:
    """
    Validate all integration test requirements.

    Returns:
        Dictionary of check results: {check_name: (passed, error_message)}
    """
    checks = {
        "zotero_running": check_zotero_available(),
        "model_preset": check_model_preset_valid(),
        "api_key": check_api_key_available(),
        "test_library": check_test_library_synced(),
    }

    if verbose:
        print("\n" + "=" * 70)
        print("Integration Test Environment Validation")
        print("=" * 70)

        for check_name, (passed, error_msg) in checks.items():
            status = "[PASS]" if passed else "[FAIL]"
            print(f"{check_name:20s}: {status}")

            if not passed and error_msg:
                print(f"  -> {error_msg}")

        print("=" * 70)

        # Overall status
        all_passed = all(passed for passed, _ in checks.values())

        if all_passed:
            print("[PASS] All checks passed - integration tests ready to run")
        else:
            print("[FAIL] Some checks failed - integration tests will be skipped")
            print("\nTo fix:")
            print("  1. Start Zotero desktop application")
            print("  2. Sync test group: https://www.zotero.org/groups/6297749/test-rag-plugin")
            print("  3. Set KISSKI_API_KEY environment variable")
            print("  4. Verify MODEL_PRESET (default: remote-kisski)")

        print("=" * 70 + "\n")

    return checks


# ============================================================================
# Pytest Hooks
# ============================================================================

def pytest_configure(config):
    """
    Pytest hook called after command line options have been parsed.

    This runs before test collection and setup.
    """
    # Register custom markers
    config.addinivalue_line(
        "markers",
        "integration: marks tests that require real external services (Zotero, APIs)"
    )
    config.addinivalue_line(
        "markers",
        "slow: marks tests as slow (deselect with '-m \"not slow\"')"
    )


def pytest_collection_modifyitems(config, items):
    """
    Pytest hook to modify collected test items.

    This runs after test collection, before test execution.
    If integration tests are selected, validate environment first.
    """
    # Check if any integration tests are selected
    has_integration = any(
        item.get_closest_marker("integration") for item in items
    )

    if not has_integration:
        return

    # Validate environment for integration tests
    checks = validate_integration_environment(verbose=True)

    # If any check failed, skip all integration tests
    all_passed = all(passed for passed, _ in checks.values())

    if not all_passed:
        skip_marker = pytest.mark.skip(
            reason="Integration test environment not ready (see validation output above)"
        )

        for item in items:
            if item.get_closest_marker("integration"):
                item.add_marker(skip_marker)


# ============================================================================
# Session-level Fixtures
# ============================================================================

@pytest.fixture(scope="session")
def integration_environment_validated():
    """
    Session-level fixture that validates environment once.

    This can be used as a dependency for other fixtures to ensure
    environment is checked before creating resources.
    """
    checks = validate_integration_environment(verbose=False)

    # Raise if any check failed
    failed_checks = {
        name: error_msg
        for name, (passed, error_msg) in checks.items()
        if not passed
    }

    if failed_checks:
        error_messages = "\n".join(
            f"  - {name}: {msg}"
            for name, msg in failed_checks.items()
        )
        pytest.skip(f"Environment validation failed:\n{error_messages}")

    return True


# ============================================================================
# Helper Functions for Tests
# ============================================================================

def get_test_library_id() -> str:
    """Get the test library ID from configuration."""
    return os.getenv("TEST_LIBRARY_ID", "6297749")


def get_test_library_type() -> str:
    """Get the test library type from configuration."""
    return os.getenv("TEST_LIBRARY_TYPE", "group")


def get_test_library_url() -> str:
    """Get the test library URL."""
    lib_id = get_test_library_id()
    return f"https://www.zotero.org/groups/{lib_id}/test-rag-plugin"


def get_expected_min_items() -> int:
    """Get expected minimum items from configuration."""
    return int(os.getenv("EXPECTED_MIN_ITEMS", "5"))


def get_expected_min_chunks() -> int:
    """Get expected minimum chunks from configuration."""
    return int(os.getenv("EXPECTED_MIN_CHUNKS", "50"))


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
