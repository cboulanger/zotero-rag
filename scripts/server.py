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
LOG_DIR = PROJECT_ROOT / "data" / "logs"
LOG_FILE = LOG_DIR / "server.log"
PLUGIN_LOG_FILE = LOG_DIR / "plugin.log"

# Kreuzberg sidecar configuration
KREUZBERG_IMAGE = "ghcr.io/kreuzberg-dev/kreuzberg:4.9.5"
KREUZBERG_CONTAINER = "zotero-rag-kreuzberg"
KREUZBERG_PORT = 8100          # host-side port for kreuzberg sidecar
KREUZBERG_CONTAINER_PORT = 8000  # kreuzberg listens on 8000 inside the container

# Qdrant sidecar configuration
QDRANT_IMAGE = "docker.io/qdrant/qdrant:v1.15"
QDRANT_CONTAINER = "zotero-rag-qdrant"
QDRANT_PORT = 6333
QDRANT_STORAGE_DIR = PROJECT_ROOT / "data" / "qdrant-server"

# Track subprocess references to prevent handle cleanup errors on Windows
# These need to be module-level to prevent premature garbage collection
_plugin_process = None
_strip_process = None
_log_file_handle = None
# Set to True once the plugin server and backend are fully started; atexit
# must NOT kill the plugin server in that case – the user stops it explicitly.
_startup_succeeded = False


def _cleanup_on_exit():
    """Cleanup function to be called on script exit.

    Ensures all subprocess handles are properly closed to prevent
    'invalid handle' errors during Python shutdown on Windows.

    When startup succeeded the plugin server must keep running – the user
    stops it via `server.py stop`.  Only clean up on error paths.
    """
    if _startup_succeeded:
        # Release our handle references without terminating the processes.
        # The plugin dev server (zotero-plugin dev) keeps running in the
        # background so hot-reload continues to work.
        return
    cleanup_plugin_processes()


def find_server_process():
    """Find the running server process by checking for uvicorn on our port.

    Uvicorn with --reload spawns multiprocessing children whose cmdline does
    not contain 'uvicorn'.  Fall back to finding any process that has our
    port open so we always find the actual listener.
    """
    # Primary: look for the uvicorn parent/main process by cmdline
    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            cmdline = proc.info['cmdline']
            if cmdline and 'uvicorn' in ' '.join(cmdline) and 'backend.main:app' in ' '.join(cmdline):
                return proc
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass

    # Fallback: find any process listening on PORT (catches spawn children)
    try:
        import socket
        for proc in psutil.process_iter(['pid', 'name']):
            try:
                conns = proc.net_connections(kind='inet')
                for c in conns:
                    if c.laddr.port == PORT and c.status == 'LISTEN':
                        return proc
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass
    except Exception:
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


def get_extractor_backend():
    """Read EXTRACTOR_BACKEND from the current environment (loaded from .env by load_dotenv).

    Returns:
        str: 'kreuzberg' (default) or 'legacy'
    """
    return os.environ.get('EXTRACTOR_BACKEND', 'kreuzberg').lower()


def find_container_runtime():
    """Detect an available container runtime (docker or podman).

    For podman, attempts to start the machine via `podman machine start` if the
    daemon is not reachable (common on macOS when the VM is stopped).

    Returns:
        str: 'docker' or 'podman' if available and daemon is running, None otherwise.
    """
    for cmd in ['docker', 'podman']:
        try:
            subprocess.run([cmd, '--version'], capture_output=True, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue
        try:
            subprocess.run([cmd, 'info'], capture_output=True, check=True)
            return cmd
        except (subprocess.CalledProcessError, FileNotFoundError):
            if cmd == 'podman':
                # On macOS the podman machine may simply be stopped — try starting it.
                print("[INFO] Podman daemon not reachable, attempting `podman machine start`...")
                result = subprocess.run(
                    ['podman', 'machine', 'start'],
                    capture_output=True, text=True,
                )
                if result.returncode == 0:
                    print("[OK] Podman machine started")
                    return cmd
                else:
                    print(f"[WARN] `podman machine start` failed: {result.stderr.strip()}")
            continue
    return None


def start_kreuzberg():
    """Start the kreuzberg extraction sidecar container.

    Skipped when EXTRACTOR_BACKEND=legacy (no container needed).
    Pulls the image if needed, then starts the container on localhost:8100.
    Warns but does not abort if no container runtime is available.

    Returns:
        bool: True if the sidecar is running (or was already running), False otherwise.
    """
    if get_extractor_backend() != 'kreuzberg':
        print("[INFO] EXTRACTOR_BACKEND=legacy — skipping kreuzberg sidecar")
        return True

    runtime = find_container_runtime()
    if runtime is None:
        print("[WARN] No container runtime found (docker/podman) — kreuzberg sidecar will not be started")
        print(f"[WARN] Document extraction will fail; set EXTRACTOR_BACKEND=legacy to use the built-in pypdf extractor")
        return False

    # Check if already running
    try:
        result = subprocess.run(
            [runtime, 'ps', '--filter', f'name=^{KREUZBERG_CONTAINER}$', '--format', '{{.ID}}'],
            capture_output=True, text=True, check=True
        )
        if result.stdout.strip():
            print(f"[OK] kreuzberg sidecar already running ({KREUZBERG_CONTAINER})")
            return True
    except subprocess.CalledProcessError:
        pass

    # Remove stopped container with the same name, if any
    subprocess.run([runtime, 'rm', '-f', KREUZBERG_CONTAINER], capture_output=True)

    print(f"[INFO] Pulling kreuzberg image ({KREUZBERG_IMAGE})...")
    subprocess.run([runtime, 'pull', KREUZBERG_IMAGE], capture_output=True)

    print(f"[INFO] Starting kreuzberg sidecar on port {KREUZBERG_PORT}...")
    try:
        subprocess.run(
            [
                runtime, 'run', '-d',
                '--name', KREUZBERG_CONTAINER,
                '-p', f'127.0.0.1:{KREUZBERG_PORT}:{KREUZBERG_CONTAINER_PORT}',
                KREUZBERG_IMAGE,
            ],
            check=True,
            capture_output=True,
        )
        print(f"[OK] kreuzberg sidecar started ({KREUZBERG_CONTAINER})")
        return True
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] Failed to start kreuzberg sidecar: {e.stderr.decode().strip()}")
        return False


def stop_kreuzberg():
    """Stop and remove the kreuzberg extraction sidecar container.

    Skipped when EXTRACTOR_BACKEND=legacy.
    Loads .env/.env.local so the backend setting is respected even when
    called from stop_server (which does not call load_dotenv itself).
    """
    # Load env so EXTRACTOR_BACKEND is visible (idempotent if already loaded)
    env_local = PROJECT_ROOT / ".env.local"
    env_file = PROJECT_ROOT / ".env"
    if env_local.exists():
        load_dotenv(env_local, override=True)
    if env_file.exists():
        load_dotenv(env_file, override=True)

    if get_extractor_backend() != 'kreuzberg':
        return

    runtime = find_container_runtime()
    if runtime is None:
        return

    try:
        result = subprocess.run(
            [runtime, 'ps', '-a', '--filter', f'name=^{KREUZBERG_CONTAINER}$', '--format', '{{.ID}}'],
            capture_output=True, text=True, check=True
        )
        if not result.stdout.strip():
            return  # not running
        subprocess.run([runtime, 'stop', KREUZBERG_CONTAINER], capture_output=True)
        subprocess.run([runtime, 'rm', KREUZBERG_CONTAINER], capture_output=True)
        print(f"[OK] kreuzberg sidecar stopped ({KREUZBERG_CONTAINER})")
    except subprocess.CalledProcessError:
        pass


def start_qdrant():
    """Start the Qdrant vector store sidecar container.

    Skipped when QDRANT_URL is already configured (external or remote Qdrant).
    Pulls the image if needed, then starts the container on localhost:6333.
    Data is persisted in data/qdrant-server/.

    Returns:
        bool: True if the sidecar is running (or was already running), False otherwise.
    """
    runtime = find_container_runtime()
    if runtime is None:
        print("[WARN] No container runtime found (docker/podman) — Qdrant sidecar will not be started")
        return False

    # Check if already running
    try:
        result = subprocess.run(
            [runtime, 'ps', '--filter', f'name=^{QDRANT_CONTAINER}$', '--format', '{{.ID}}'],
            capture_output=True, text=True, check=True
        )
        if result.stdout.strip():
            print(f"[OK] Qdrant sidecar already running ({QDRANT_CONTAINER})")
            return True
    except subprocess.CalledProcessError:
        pass

    # Remove stopped container with the same name, if any
    subprocess.run([runtime, 'rm', '-f', QDRANT_CONTAINER], capture_output=True)

    # Ensure persistent storage directory exists
    QDRANT_STORAGE_DIR.mkdir(parents=True, exist_ok=True)

    print(f"[INFO] Pulling Qdrant image ({QDRANT_IMAGE})...")
    subprocess.run([runtime, 'pull', QDRANT_IMAGE], capture_output=True)

    print(f"[INFO] Starting Qdrant sidecar on port {QDRANT_PORT}...")
    try:
        subprocess.run(
            [
                runtime, 'run', '-d',
                '--name', QDRANT_CONTAINER,
                '-p', f'127.0.0.1:{QDRANT_PORT}:{QDRANT_PORT}',
                '-v', f'{QDRANT_STORAGE_DIR}:/qdrant/storage',
                QDRANT_IMAGE,
            ],
            check=True,
            capture_output=True,
        )
        print(f"[OK] Qdrant sidecar started ({QDRANT_CONTAINER})")
        return True
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] Failed to start Qdrant sidecar: {e.stderr.decode().strip()}")
        return False


def wait_for_qdrant(timeout=60):
    """Wait until Qdrant accepts TCP connections on QDRANT_PORT.

    Returns:
        bool: True if Qdrant became ready within timeout, False otherwise.
    """
    import socket
    print("[INFO] Waiting for Qdrant to be ready...")
    start = time.time()
    while time.time() - start < timeout:
        try:
            with socket.create_connection(("127.0.0.1", QDRANT_PORT), timeout=1):
                print("[OK] Qdrant is ready")
                return True
        except OSError:
            time.sleep(1)
    print(f"[WARN] Qdrant did not become ready within {timeout}s — continuing anyway")
    return False


def stop_qdrant():
    """Stop and remove the Qdrant sidecar container.

    Loads .env/.env.local so the setting is respected even when called from
    stop_server (which does not call load_dotenv itself).
    """
    env_local = PROJECT_ROOT / ".env.local"
    env_file = PROJECT_ROOT / ".env"
    if env_local.exists():
        load_dotenv(env_local, override=True)
    if env_file.exists():
        load_dotenv(env_file, override=True)

    runtime = find_container_runtime()
    if runtime is None:
        return

    try:
        result = subprocess.run(
            [runtime, 'ps', '-a', '--filter', f'name=^{QDRANT_CONTAINER}$', '--format', '{{.ID}}'],
            capture_output=True, text=True, check=True
        )
        if not result.stdout.strip():
            return  # not running
        subprocess.run([runtime, 'stop', QDRANT_CONTAINER], capture_output=True)
        subprocess.run([runtime, 'rm', QDRANT_CONTAINER], capture_output=True)
        print(f"[OK] Qdrant sidecar stopped ({QDRANT_CONTAINER})")
    except subprocess.CalledProcessError:
        pass


def _start_plugin_server_and_wait():
    """Start the plugin development server and wait until it is ready.

    Exits the process on failure.
    """
    print("Starting plugin development server (this will launch Zotero)...")
    result = start_plugin_server()
    if not result:
        print("[ERROR] Failed to start plugin development server")
        sys.exit(1)

    print("Waiting for plugin server to start...")
    if not wait_for_plugin_startup(timeout=60):
        print("[ERROR] Plugin server failed to start within timeout")
        print(f"[INFO] Check logs at: {PLUGIN_LOG_FILE}")
        stop_plugin_server()
        sys.exit(1)

    print("[OK] Plugin server started successfully")


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

    # Check if backend server already running
    existing = find_server_process()
    if existing:
        print(f"Server is already running (PID: {existing.pid})")
        print(f"Access at: http://{HOST}:{PORT}")
        if with_plugin:
            plugin_proc = find_plugin_process()
            if not plugin_proc:
                _start_plugin_server_and_wait()
            else:
                print(f"[INFO] Plugin server already running (PID: {plugin_proc.pid})")
        return

    # Start kreuzberg extraction sidecar
    start_kreuzberg()

    # Start Qdrant sidecar unless an external QDRANT_URL is already configured
    qdrant_url = os.environ.get('QDRANT_URL')
    if qdrant_url:
        print(f"[INFO] QDRANT_URL already set ({qdrant_url}) — skipping local Qdrant sidecar")
    else:
        if start_qdrant():
            wait_for_qdrant()
            qdrant_url = f"http://localhost:{QDRANT_PORT}"
        else:
            print("[WARN] Qdrant sidecar could not be started; falling back to local file mode")

    # Start the backend server
    log_config_path = PROJECT_ROOT / "backend" / "logging_config.json"
    cmd = [
        "uv", "run", "uvicorn", "backend.main:app",
        "--host", HOST,
        "--port", str(PORT),
        "--log-config", str(log_config_path)
    ]

    if dev_mode:
        cmd.extend(["--reload", "--reload-dir", str(PROJECT_ROOT / "backend")])
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

        # Point the backend at our local Qdrant sidecar (or the pre-configured URL)
        if qdrant_url:
            env["QDRANT_URL"] = qdrant_url

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
            # Poll for up to 60 seconds for the server to become ready
            import requests
            max_wait = 60
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
                    global _startup_succeeded
                    _startup_succeeded = True
                    zotero_url = env.get("ZOTERO_API_URL", "http://localhost:23119")
                    print(f"[OK] Server started successfully (PID: {process.pid})")
                    print(f"[OK] Access at: http://{HOST}:{PORT}")
                    print(f"[OK] API docs at: http://{HOST}:{PORT}/docs")
                    print(f"[OK] Logs: {LOG_FILE}")
                    print(f"[INFO] Check logs for Zotero API connectivity status ({zotero_url})")

                    if with_plugin:
                        plugin_proc = find_plugin_process()
                        if not plugin_proc:
                            _start_plugin_server_and_wait()
                        else:
                            print(f"[INFO] Plugin server already running (PID: {plugin_proc.pid})")
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
    """Stop the running backend server, kreuzberg sidecar, and plugin server if running."""
    # First stop the plugin server if running
    plugin_proc = find_plugin_process()
    if plugin_proc:
        stop_plugin_server()

    # Stop kreuzberg extraction sidecar
    stop_kreuzberg()

    # Stop Qdrant sidecar
    stop_qdrant()

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
    elif command == "kreuzberg:start":
        start_kreuzberg()
    elif command == "kreuzberg:stop":
        stop_kreuzberg()
    elif command == "qdrant:start":
        if start_qdrant():
            wait_for_qdrant()
    elif command == "qdrant:stop":
        stop_qdrant()
    else:
        print(f"Unknown command: {command}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
