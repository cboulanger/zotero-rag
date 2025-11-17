#!/usr/bin/env python
"""
Cross-platform server management script for Zotero RAG backend.

Usage:
    python scripts/server.py start             # Start backend (production mode, no auto-reload)
    python scripts/server.py start --dev       # Start backend (dev mode) + plugin server
    python scripts/server.py stop              # Stop backend (and plugin if running)
    python scripts/server.py restart           # Restart backend (production mode)
    python scripts/server.py restart --dev     # Restart backend (dev mode) + plugin server
    python scripts/server.py status            # Check backend server status
    python scripts/server.py status --dev      # Check backend + plugin server status

Production mode: Backend only, no auto-reload (suitable for deployment)
Development mode (--dev): Backend with auto-reload + plugin server with hot-reload
"""

import sys
import subprocess
import signal
import time
import psutil
import os
import atexit
from pathlib import Path
from dotenv import load_dotenv

# Server configuration
HOST = "localhost"
PORT = 8119
PROJECT_ROOT = Path(__file__).parent.parent
LOG_DIR = PROJECT_ROOT / "logs"
LOG_FILE = LOG_DIR / "server.log"
PLUGIN_LOG_FILE = LOG_DIR / "plugin.log"

# Track subprocess references to prevent handle cleanup errors on Windows
# These need to be module-level to prevent premature garbage collection
_plugin_process = None
_strip_process = None
_log_file_handle = None


def _cleanup_on_exit():
    """Cleanup function to be called on script exit.

    Ensures all subprocess handles are properly closed to prevent
    'invalid handle' errors during Python shutdown on Windows.
    """
    cleanup_plugin_processes()


def find_server_process():
    """Find the running server process by checking for uvicorn on our port."""
    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            cmdline = proc.info['cmdline']
            if cmdline and 'uvicorn' in ' '.join(cmdline) and 'backend.main:app' in ' '.join(cmdline):
                return proc
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
    return None


def find_plugin_process():
    """Find the running plugin development server process."""
    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            cmdline = proc.info['cmdline']
            if cmdline:
                cmdline_str = ' '.join(cmdline)
                # Look for the zotero_plugin.py start process OR the underlying zotero-plugin dev
                if ('zotero_plugin.py' in cmdline_str and 'start' in cmdline_str) or \
                   ('zotero-plugin' in cmdline_str and 'dev' in cmdline_str and 'zotero_plugin.py' not in cmdline_str):
                    return proc
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
    return None


def extract_error_from_log():
    """Extract user-friendly error message from log file.

    Returns:
        Error message string if found, None otherwise
    """
    if not LOG_FILE.exists():
        return None

    try:
        # Read the last 100 lines of the log
        with open(LOG_FILE, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            recent_lines = lines[-100:] if len(lines) > 100 else lines

        # Join all lines to search for RuntimeError message
        log_text = "".join(recent_lines)

        # Look for RuntimeError with embedded error block
        if "RuntimeError:" in log_text:
            # Find the start of the error block after RuntimeError:
            runtime_error_pos = log_text.rfind("RuntimeError:")
            after_runtime = log_text[runtime_error_pos:]

            # Look for error block between separator lines
            separator_start = after_runtime.find("=" * 70)
            if separator_start == -1:
                separator_start = after_runtime.find("=" * 80)

            if separator_start != -1:
                # Find the end separator
                separator_end = after_runtime.find("=" * 70, separator_start + 70)
                if separator_end == -1:
                    separator_end = after_runtime.find("=" * 80, separator_start + 80)

                if separator_end != -1:
                    # Extract the error message between separators
                    error_block = after_runtime[separator_start:separator_end].strip()
                    # Remove the separator line at start
                    lines_in_block = error_block.split('\n')
                    if lines_in_block and '=' in lines_in_block[0]:
                        lines_in_block = lines_in_block[1:]
                    return '\n'.join(lines_in_block).strip()

        # Fallback: look for ERROR: messages in recent lines
        for line in reversed(recent_lines):
            if " - ERROR - " in line and ("Cannot connect" in line or "ERROR:" in line):
                # Extract just the error message
                error_msg = line.split(" - ERROR - ", 1)[1].strip()
                return error_msg

    except Exception:
        # Silently ignore errors reading log
        pass

    return None


def is_zotero_running():
    """Check if any Zotero process is currently running.

    Returns:
        bool: True if Zotero is running, False otherwise
    """
    try:
        for proc in psutil.process_iter(['name']):
            if proc.info['name'] and 'zotero' in proc.info['name'].lower():
                return True
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        pass
    return False


def wait_for_plugin_startup(timeout=60):
    """Wait for the plugin server to successfully start by monitoring the log file.

    Args:
        timeout: Maximum time to wait in seconds

    Returns:
        bool: True if startup successful, False otherwise
    """
    if not PLUGIN_LOG_FILE.exists():
        return False

    start_time = time.time()
    last_pos = 0

    # Success indicators in the log
    success_indicators = [
        "server ready",
        "server started",
        "watching for file changes",
        "build succeeded"
    ]

    # Failure indicators in the log
    failure_indicators = [
        "[ERROR] Zotero is already running",
        "Failed to execute",
        "Unexpected error"
    ]

    while time.time() - start_time < timeout:
        try:
            with open(PLUGIN_LOG_FILE, 'r', encoding='utf-8', errors='ignore') as f:
                f.seek(last_pos)
                new_content = f.read()
                last_pos = f.tell()

                if new_content:
                    new_content_lower = new_content.lower()

                    # Check for failure first
                    for indicator in failure_indicators:
                        if indicator.lower() in new_content_lower:
                            print(f"[ERROR] Plugin server startup failed")
                            # Print the relevant error lines
                            for line in new_content.split('\n'):
                                if '[ERROR]' in line or '[INFO]' in line:
                                    print(f"  {line.strip()}")
                            return False

                    # Check for success
                    for indicator in success_indicators:
                        if indicator in new_content_lower:
                            return True

        except Exception:
            pass

        time.sleep(0.5)

    return False


def start_server(dev_mode=True, with_plugin=False):
    """Start the FastAPI server and optionally the plugin development server.

    Args:
        dev_mode: Enable auto-reload for the backend server
        with_plugin: Also start the plugin development server
    """
    # Load environment from .env.local (overrides) then .env
    env_local = PROJECT_ROOT / ".env.local"
    env_file = PROJECT_ROOT / ".env"

    if env_local.exists():
        print(f"Loading .env.local: {env_local}")
        load_dotenv(env_local, override=True)
    load_dotenv(env_file, override=True)  # Always reload from .env file

    # Debug: Show what MODEL_PRESET is set to
    model_preset = os.environ.get("MODEL_PRESET", "not set")
    print(f"MODEL_PRESET: {model_preset}")

    # If plugin mode is requested, start plugin server first (which launches Zotero)
    # This ensures Zotero is running before the backend tries to connect to it
    if with_plugin:
        # First check if Zotero is already running
        if is_zotero_running():
            print("[ERROR] Zotero is already running")
            print("[INFO] Please close all Zotero instances before starting in --dev mode")
            print("[INFO] The plugin development server needs to start its own Zotero instance")
            sys.exit(1)

        plugin_proc = find_plugin_process()
        if not plugin_proc:
            print("Starting plugin development server (this will launch Zotero)...")
            result = start_plugin_server()
            if not result:
                print("[ERROR] Failed to start plugin development server")
                sys.exit(1)

            # Wait for plugin server to fully start up by monitoring logs
            print("Waiting for plugin server to start...")
            if not wait_for_plugin_startup(timeout=60):
                print("[ERROR] Plugin server failed to start within timeout")
                print(f"[INFO] Check logs at: {PLUGIN_LOG_FILE}")
                stop_plugin_server()  # Clean up
                sys.exit(1)

            print("[OK] Plugin server started successfully")
        else:
            print(f"[INFO] Plugin server already running (PID: {plugin_proc.pid})")

    # Check if backend server already running
    existing = find_server_process()
    if existing:
        print(f"Server is already running (PID: {existing.pid})")
        print(f"Access at: http://{HOST}:{PORT}")
        return

    # Start the backend server
    log_config_path = PROJECT_ROOT / "backend" / "logging_config.json"
    cmd = [
        "uv", "run", "uvicorn", "backend.main:app",
        "--host", HOST,
        "--port", str(PORT),
        "--log-config", str(log_config_path)
    ]

    if dev_mode:
        cmd.append("--reload")
        print("Starting server in development mode (with auto-reload)...")
    else:
        print("Starting server in production mode...")

    try:
        # Ensure log directory exists
        LOG_DIR.mkdir(exist_ok=True)

        # Prepare environment for subprocess
        # IMPORTANT: Copy current environment which includes .env.local overrides
        env = os.environ.copy()

        # Explicitly set MODEL_PRESET from current environment (which has .env.local loaded)
        # This overrides any system/user environment variable
        if model_preset != "not set":
            env["MODEL_PRESET"] = model_preset
        elif "MODEL_PRESET" in env:
            # If we don't have a value from .env files, remove the system one
            # so pydantic will use the default
            del env["MODEL_PRESET"]

        # Open log file for output (truncate to start fresh)
        with open(LOG_FILE, 'w') as log_file:
            # Start in background with explicit environment
            process = subprocess.Popen(
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                env=env,  # Pass environment with .env.local values
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == 'win32' else 0
            )

            # Wait for server to start and run connectivity check
            # Poll for up to 30 seconds for the server to become ready
            import requests
            max_wait = 30
            poll_interval = 0.5
            server_ready = False

            for i in range(int(max_wait / poll_interval)):
                time.sleep(poll_interval)

                # Check if process is still running
                if process.poll() is not None:
                    break

                # Try to connect to the server
                try:
                    response = requests.get(f"http://{HOST}:{PORT}/health", timeout=1)
                    if response.ok:
                        server_ready = True
                        break
                except requests.exceptions.RequestException:
                    # Server not ready yet, continue waiting
                    continue

            # Check if process is still running
            if process.poll() is None:
                # Verify that the server is actually responding to requests
                if server_ready:
                    zotero_url = env.get("ZOTERO_API_URL", "http://localhost:23119")
                    print(f"[OK] Server started successfully (PID: {process.pid})")
                    print(f"[OK] Access at: http://{HOST}:{PORT}")
                    print(f"[OK] API docs at: http://{HOST}:{PORT}/docs")
                    print(f"[OK] Logs: {LOG_FILE}")
                    print(f"[INFO] Check logs for Zotero API connectivity status ({zotero_url})")

                    if with_plugin:
                        print(f"[INFO] Plugin development server is running")
                        print(f"[INFO] Plugin logs: {PLUGIN_LOG_FILE}")

                    print(f"\nTo stop: python scripts/server.py stop")
                else:
                    print("[ERROR] Server process is running but application failed to start")
                    print(f"[ERROR] Server did not become ready within {max_wait} seconds")

                    # Try to extract helpful error message from logs
                    error_msg = extract_error_from_log()
                    if error_msg:
                        print("\n" + error_msg)
                    else:
                        print(f"\nCheck logs at: {LOG_FILE}")

                    # Kill the non-functional process
                    process.terminate()
                    sys.exit(1)
            else:
                print("[ERROR] Server failed to start")
                print(f"Check logs at: {LOG_FILE}")
                sys.exit(1)

    except Exception as e:
        print(f"[ERROR] Error starting server: {e}")
        sys.exit(1)


def stop_server():
    """Stop the running backend server and plugin server if running."""
    # First stop the plugin server if running
    plugin_proc = find_plugin_process()
    if plugin_proc:
        stop_plugin_server()

    # Try to find backend server by process
    proc = find_server_process()

    if proc:
        try:
            print(f"Stopping server (PID: {proc.pid})...")

            # On Windows, we need to kill the entire process tree
            # because uvicorn --reload creates child processes
            if sys.platform == 'win32':
                # Get all child processes
                try:
                    parent = psutil.Process(proc.pid)
                    children = parent.children(recursive=True)

                    # Terminate parent first
                    parent.terminate()

                    # Terminate all children
                    for child in children:
                        try:
                            child.terminate()
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            pass

                    # Wait for graceful shutdown
                    gone, alive = psutil.wait_procs([parent] + children, timeout=5)

                    # Force kill any remaining
                    for p in alive:
                        try:
                            print(f"Force killing PID {p.pid}...")
                            p.kill()
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            pass

                    print("[OK] Server stopped successfully")

                except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                    print(f"[ERROR] Error stopping server: {e}")
            else:
                # On Unix, terminate should handle the process group
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                    print("[OK] Server stopped successfully")
                except psutil.TimeoutExpired:
                    print("Server didn't stop gracefully, forcing...")
                    proc.kill()
                    print("[OK] Server killed")

        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
            print(f"[ERROR] Error stopping server: {e}")
    else:
        print("Server is not running")


def start_plugin_server():
    """Start the plugin development server.

    Returns:
        Popen object if successful, None otherwise
    """
    global _plugin_process, _strip_process, _log_file_handle

    # Check if already running
    existing = find_plugin_process()
    if existing:
        print(f"Plugin server is already running (PID: {existing.pid})")
        return existing

    # Clean up any previous references
    cleanup_plugin_processes()

    # Start the plugin server using the zotero_plugin.py script
    # Use "start" command which internally calls "dev" on the underlying tool
    plugin_cmd = ["uv", "run", "python", "scripts/zotero_plugin.py", "start"]

    # Command to strip ANSI codes
    strip_cmd = ["uv", "run", "python", "scripts/strip_ansi.py"]

    try:
        # Ensure log directory exists
        LOG_DIR.mkdir(exist_ok=True)

        # Open log file for output (truncate to start fresh)
        # Keep reference to prevent premature closure
        _log_file_handle = open(PLUGIN_LOG_FILE, 'w', encoding='utf-8')

        # Start the plugin server process with stdout piped
        _plugin_process = subprocess.Popen(
            plugin_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=PROJECT_ROOT,
            text=True,
            bufsize=1,  # Line buffered
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == 'win32' else 0
        )

        # Start the ANSI stripper process
        _strip_process = subprocess.Popen(
            strip_cmd,
            stdin=_plugin_process.stdout,
            stdout=_log_file_handle,
            stderr=subprocess.STDOUT,
            cwd=PROJECT_ROOT,
            text=True,
            bufsize=1,  # Line buffered
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == 'win32' else 0
        )

        # Allow plugin_process to receive SIGPIPE if strip_process exits
        _plugin_process.stdout.close()

        # Wait a moment for plugin server to start
        time.sleep(2)

        # Check if process is still running
        if _plugin_process.poll() is None:
            print(f"[OK] Plugin server started successfully (PID: {_plugin_process.pid})")
            print(f"[OK] Plugin logs: {PLUGIN_LOG_FILE}")
            return _plugin_process
        else:
            print("[ERROR] Plugin server failed to start")
            print(f"Check logs at: {PLUGIN_LOG_FILE}")
            cleanup_plugin_processes()
            return None

    except Exception as e:
        print(f"[ERROR] Error starting plugin server: {e}")
        cleanup_plugin_processes()
        return None


def cleanup_plugin_processes():
    """Clean up plugin subprocess references and handles to prevent resource leaks.

    This properly closes all handles and resets module-level process references
    to prevent 'invalid handle' errors on Windows during garbage collection.
    """
    global _plugin_process, _strip_process, _log_file_handle

    # Close the strip process first (end of the pipeline)
    if _strip_process is not None:
        try:
            if _strip_process.poll() is None:
                _strip_process.terminate()
                try:
                    _strip_process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    _strip_process.kill()
        except Exception:
            pass
        _strip_process = None

    # Close the plugin process
    if _plugin_process is not None:
        try:
            if _plugin_process.poll() is None:
                _plugin_process.terminate()
                try:
                    _plugin_process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    _plugin_process.kill()
        except Exception:
            pass
        _plugin_process = None

    # Close the log file handle
    if _log_file_handle is not None:
        try:
            _log_file_handle.close()
        except Exception:
            pass
        _log_file_handle = None


def stop_plugin_server():
    """Stop the running plugin development server by delegating to zotero_plugin.py.

    This delegates to zotero_plugin.py which handles stopping both the plugin
    dev server and the associated Zotero instance.
    """
    try:
        result = subprocess.run(
            ["uv", "run", "python", "scripts/zotero_plugin.py", "stop"],
            cwd=PROJECT_ROOT
        )
        # Clean up our tracked references
        cleanup_plugin_processes()
        return result.returncode == 0
    except Exception as e:
        print(f"[ERROR] Error stopping plugin server: {e}")
        cleanup_plugin_processes()
        return False


def check_status(include_plugin=False):
    """Check if the server is running.

    Args:
        include_plugin: Also check plugin server status
    """
    import requests

    backend_running = False
    plugin_running = False

    # Check backend server
    proc = find_server_process()

    if proc:
        # Verify the server is actually responding
        try:
            health_response = requests.get(f"http://{HOST}:{PORT}/health", timeout=2)
            if health_response.ok:
                print(f"[OK] Backend server is running (PID: {proc.pid})")
                print(f"  Access at: http://{HOST}:{PORT}")
                print(f"  API docs at: http://{HOST}:{PORT}/docs")

                # Try to get configuration info
                try:
                    response = requests.get(f"http://{HOST}:{PORT}/api/config", timeout=2)
                    if response.ok:
                        config = response.json()
                        print(f"  Preset: {config.get('preset_name', 'unknown')}")
                        print(f"  LLM: {config.get('llm_model', 'unknown')}")
                except Exception:
                    # Silently ignore if we can't fetch config
                    pass

                backend_running = True
            else:
                print(f"[WARN] Backend server process exists (PID: {proc.pid}) but is not responding properly")
                print(f"  Health check returned status: {health_response.status_code}")
                print(f"  Check logs at: {LOG_FILE}")
        except requests.exceptions.RequestException as e:
            print(f"[WARN] Backend server process exists (PID: {proc.pid}) but application is not responding")
            print(f"  Cannot connect to health endpoint: {e}")
            print(f"  Check logs at: {LOG_FILE}")
    else:
        print("[INFO] Backend server is not running")

    # Check plugin server if requested
    if include_plugin:
        print()  # Blank line for separation
        plugin_proc = find_plugin_process()

        if plugin_proc:
            print(f"[OK] Plugin development server is running (PID: {plugin_proc.pid})")
            print(f"  Plugin logs: {PLUGIN_LOG_FILE}")

            # Check if Zotero is actually running (plugin server should have launched it)
            if is_zotero_running():
                print(f"  Zotero is running")
            else:
                print(f"  [WARN] Zotero does not appear to be running")

            plugin_running = True
        else:
            print("[INFO] Plugin development server is not running")

    return backend_running or (plugin_running if include_plugin else False)


def restart_server(dev_mode=True, with_plugin=False):
    """Restart the server and optionally the plugin server."""
    print("Restarting server...")
    stop_server()
    time.sleep(1)
    start_server(dev_mode, with_plugin)


def main():
    # Register cleanup handler to ensure proper resource cleanup on exit
    atexit.register(_cleanup_on_exit)

    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    command = sys.argv[1].lower()

    if command == "start":
        # Check for --dev flag to enable development mode with plugin server
        with_dev = "--dev" in sys.argv
        # Production mode by default, development mode only with --dev flag
        start_server(dev_mode=with_dev, with_plugin=with_dev)
    elif command == "stop":
        stop_server()
    elif command == "restart":
        # Check for --dev flag to enable development mode with plugin server
        with_dev = "--dev" in sys.argv
        # Production mode by default, development mode only with --dev flag
        restart_server(dev_mode=with_dev, with_plugin=with_dev)
    elif command == "status":
        # Check for --dev flag to include plugin status
        include_plugin = "--dev" in sys.argv
        check_status(include_plugin=include_plugin)
    else:
        print(f"Unknown command: {command}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
