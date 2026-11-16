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
from pathlib import Path
_project_root = Path(__file__).parent.parent.parent

# The test environment (TESTING, a throwaway AUTOINDEX_SECRET, .env.test/.env)
# and the shared fixtures come from backend/conftest.py (backend.pytest_plugin).

# Core tests run on the test-only plugin in fake_plugins/ (fake providers and
# presets), so they pass with every shipped vendor plugin removed or disabled.
FAKE_PLUGIN_DIR = Path(__file__).resolve().parent / "fake_plugins"
#: The preset core tests run on unless a test chooses another (the real one is
#: kept for the integration tests' server, which needs real providers).
CORE_TEST_PRESET = "test-remote"
ORIGINAL_MODEL_PRESET = os.environ.get("MODEL_PRESET")
_dirs = [d for d in os.environ.get("BACKEND_PLUGIN_DIRS", "").split(os.pathsep) if d]
if str(FAKE_PLUGIN_DIR) not in _dirs:
    os.environ["BACKEND_PLUGIN_DIRS"] = os.pathsep.join([*_dirs, str(FAKE_PLUGIN_DIR)])
os.environ["MODEL_PRESET"] = CORE_TEST_PRESET


def _load_plugins_now() -> None:
    """Load the plugin host while the environment above is in place.

    Loading is lazy, and a test that clears ``os.environ`` (``patch.dict(..., clear=True)``)
    must not be the first to trigger it. When plugin tests collected first already
    loaded the host, the fakes are added to it.
    """
    from backend.pluginhost import get_plugin_host

    host = get_plugin_host()
    if "testvendor" not in {r.id for r in host.plugins()}:
        host._load_directory(FAKE_PLUGIN_DIR, set())


_load_plugins_now()

# Import shared environment validation
from backend.tests.test_environment import validate_test_environment


# ============================================================================
# Helper Functions
# ============================================================================

def _kill_process_tree(process):
    """
    Kill a process and all its child processes.

    This is important on Windows where uv run creates multiple processes
    (uv -> python -> uvicorn -> python workers).
    """
    import subprocess

    try:
        if sys.platform == 'win32':
            # On Windows, use taskkill to kill the process tree
            subprocess.run(
                ['taskkill', '/F', '/T', '/PID', str(process.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5
            )
        else:
            # On Unix, terminate the process group
            import signal
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass  # Process already dead

        # Wait for process to fully terminate
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            # Force kill if still alive
            process.kill()
            process.wait()
    except Exception as e:
        # Best effort - log but don't fail
        print(f"[TEST] Warning: Error killing process tree: {e}")


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
        "api: marks tests that require running backend server (API integration tests)"
    )
    config.addinivalue_line(
        "markers",
        "slow: marks tests as slow (deselect with '-m \"not slow\"')"
    )


def pytest_collection_modifyitems(config, items):
    """
    Pytest hook to modify collected test items.

    This runs after test collection, before test execution.
    If integration or API tests are selected, validate environment first.
    """
    # Check if any integration or API tests are selected
    has_integration = any(
        item.get_closest_marker("integration") for item in items
    )
    has_api = any(
        item.get_closest_marker("api") for item in items
    )

    if not (has_integration or has_api):
        return

    # Validate environment for integration tests
    # Note: API tests don't check backend here because test_server fixture will start one
    checks = validate_test_environment(
        verbose=True,
        check_backend=False  # Don't check backend - test_server fixture handles it for API tests
    )

    # If any check failed, skip all integration/API tests
    all_passed = all(passed for passed, _ in checks.values())

    if not all_passed:
        skip_marker = pytest.mark.skip(
            reason="Test environment not ready (see validation output above)"
        )

        for item in items:
            if item.get_closest_marker("integration") or item.get_closest_marker("api"):
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
    checks = validate_test_environment(verbose=False, check_backend=False)

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


@pytest.fixture(scope="session")
def test_server():
    """
    Session-level fixture that starts/stops a test backend server.

    The test server runs on port 8219 (instead of the default 8119)
    to avoid conflicts with any development servers.
    """
    import subprocess
    import time
    import httpx

    # Get test server configuration
    test_port = 8219
    base_url = get_backend_base_url()

    # Start the test server
    print(f"\n[TEST] Starting test server on port {test_port}...")

    # Use hardcoded test log file for easy debugging
    # (Cannot rely on LOG_FILE env var since .env may override .env.test)
    log_file_path = _project_root / "logs" / "test_server.log"
    log_file_path.parent.mkdir(parents=True, exist_ok=True)

    # Clear previous test logs
    if log_file_path.exists():
        log_file_path.unlink()

    log_file = open(log_file_path, 'w', encoding='utf-8')

    # Create test environment - ensure .env.test settings take precedence
    test_env = {**os.environ}
    # The test server runs on real providers, not on the core tests' fakes.
    test_env.pop("MODEL_PRESET", None)
    if ORIGINAL_MODEL_PRESET:
        test_env["MODEL_PRESET"] = ORIGINAL_MODEL_PRESET
    test_env["BACKEND_PLUGIN_DIRS"] = os.pathsep.join(
        d for d in test_env.get("BACKEND_PLUGIN_DIRS", "").split(os.pathsep) if d and d != str(FAKE_PLUGIN_DIR)
    )

    # Override MODEL_PRESET for tests (avoid cpu-only which uses local models)
    if "MODEL_PRESET" not in test_env or test_env["MODEL_PRESET"] == "cpu-only":
        test_env["MODEL_PRESET"] = "remote-kisski"
        print(f"[TEST] Using preset: remote-kisski, logs: {log_file_path}")
    else:
        print(f"[TEST] Using preset: {test_env['MODEL_PRESET']}, logs: {log_file_path}")

    # Disable LOG_FILE so all logs go to stdout/stderr (which we capture in test_server.log)
    # This prevents duplicate logs in server.log and test_server.log
    test_env["LOG_FILE"] = ""

    # Use uvicorn to start the server with test configuration
    server_process = subprocess.Popen(
        [
            "uv", "run", "uvicorn",
            "backend.main:app",
            "--host", "localhost",
            "--port", str(test_port),
            "--log-level", "info",  # Changed to info to see more details
            "--log-config", str(_project_root / "backend" / "logging_config.json")
        ],
        stdout=log_file,
        stderr=subprocess.STDOUT,  # Merge stderr into stdout
        env=test_env,  # Use test environment with overrides
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == 'win32' else 0
    )

    # Wait for server to be ready (max 60 seconds with longer timeout per check)
    # Give server initial time to start before first check
    time.sleep(2)

    server_ready = False
    for i in range(30):
        try:
            response = httpx.get(f"{base_url}/health", timeout=5.0)  # Increased timeout
            if response.status_code == 200:
                server_ready = True
                print(f"[TEST] Test server ready at {base_url}")
                break
        except (httpx.ConnectError, httpx.TimeoutException):
            time.sleep(2)  # Wait 2 seconds between attempts

    if not server_ready:
        log_file.close()
        _kill_process_tree(server_process)
        pytest.fail("Test server failed to start within 60 seconds")

    try:
        # Yield control to tests
        yield base_url
    finally:
        # Cleanup: Stop the server (always runs, even on test failure)
        print("\n[TEST] Stopping test server...")
        log_file.close()
        _kill_process_tree(server_process)
        print("[TEST] Test server stopped")

        # Print server logs if there were any errors
        try:
            with open(log_file_path, 'r', encoding='utf-8') as f:
                log_contents = f.read()
                if "error" in log_contents.lower() or "traceback" in log_contents.lower():
                    print("\n[TEST] Server log excerpt (errors/tracebacks):")
                    print("=" * 70)
                    for line in log_contents.splitlines():
                        if any(keyword in line.lower() for keyword in ["error", "traceback", "exception"]):
                            print(line)
                    print("=" * 70)
                else:
                    print(f"\n[TEST] No errors in server logs. Full log at: {log_file_path}")
        except Exception as e:
            print(f"[TEST] Could not read server logs: {e}")


@pytest.fixture(scope="session")
def api_environment_validated(test_server):
    """
    Session-level fixture for API tests that validates environment and ensures test server is running.

    This fixture depends on test_server, which automatically starts/stops the test server.
    """
    # Validate environment without checking backend (test_server fixture handles that)
    checks = validate_test_environment(verbose=False, check_backend=False)

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

    return test_server  # Return the server URL


# ============================================================================
# Helper Functions for Tests (re-exported from test_environment)
# ============================================================================

from backend.tests.test_environment import (
    get_test_library_id,
    get_test_library_type,
    get_test_library_url,
    get_expected_min_items,
    get_expected_min_chunks,
    get_backend_base_url,
)
