"""
Shared environment validation for integration and API tests.

Checks whether required dependencies (API keys, backend, model preset) are
available before running integration tests. Zotero is not checked here —
indexing is now push-only (the plugin uploads files directly).

Usage:
    from backend.tests.test_environment import validate_test_environment

    checks = validate_test_environment(verbose=True)
    if not all(passed for passed, _ in checks.values()):
        # Handle missing dependencies
        ...
"""

import os
from typing import Optional


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


def check_backend_server_available(base_url: str = "http://localhost:8119") -> tuple[bool, Optional[str]]:
    """
    Check if the FastAPI backend server is running and accessible.

    Args:
        base_url: Base URL of the backend server

    Returns:
        (available, error_message): True if server is reachable, False otherwise
    """
    try:
        import httpx

        response = httpx.get(f"{base_url}/health", timeout=2.0)

        if response.status_code == 200:
            return True, None
        return False, f"Backend server responded with status {response.status_code}"
    except httpx.ConnectError:
        return False, f"Cannot connect to backend at {base_url}. Is the server running?"
    except Exception as e:
        return False, f"Error checking backend server: {e}"


def validate_test_environment(
    verbose: bool = True,
    check_backend: bool = False,
    base_url: str = "http://localhost:8119"
) -> dict[str, tuple[bool, Optional[str]]]:
    """
    Validate all integration test requirements.

    Args:
        verbose: Print validation results to console
        check_backend: Also check if backend server is running (for API tests)
        base_url: Base URL for backend server (if check_backend=True)

    Returns:
        Dictionary of check results: {check_name: (passed, error_message)}
    """
    checks = {
        "model_preset": check_model_preset_valid(),
        "api_key": check_api_key_available(),
    }

    if check_backend:
        checks["backend_server"] = check_backend_server_available(base_url)

    if verbose:
        print("\n" + "=" * 70)
        print("Test Environment Validation")
        print("=" * 70)

        for check_name, (passed, error_msg) in checks.items():
            status = "[PASS]" if passed else "[FAIL]"
            print(f"{check_name:20s}: {status}")

            if not passed and error_msg:
                print(f"  -> {error_msg}")

        print("=" * 70)

        all_passed = all(passed for passed, _ in checks.values())
        if all_passed:
            print("[PASS] All checks passed - tests ready to run")
        else:
            print("[FAIL] Some checks failed - tests will be skipped")
            print("\nTo fix:")
            print("  1. Set KISSKI_API_KEY environment variable")
            print("  2. Verify MODEL_PRESET (default: remote-kisski)")
            if check_backend:
                print("  3. Start backend server: npm run server:start")

        print("=" * 70 + "\n")

    return checks


# ============================================================================
# Helper Functions
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


def get_backend_base_url() -> str:
    """Get the backend server base URL from configuration."""
    return os.getenv("BACKEND_BASE_URL", "http://localhost:8119")
