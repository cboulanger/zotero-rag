#!/usr/bin/env python
"""
Cross-platform server management script for Zotero RAG backend.

Usage:
    python scripts/server.py start             # Start backend server only
    python scripts/server.py start --dev       # Start backend + plugin development server
    python scripts/server.py stop              # Stop both backend and plugin servers
    python scripts/server.py restart           # Restart backend server only
    python scripts/server.py restart --dev     # Restart backend + plugin development server
    python scripts/server.py status            # Check server status

The --dev flag enables the plugin development server alongside the backend server.
Both servers will be stopped together when using the stop command.
"""

import sys
import subprocess
import signal
import time
import psutil
import os
from pathlib import Path
from dotenv import load_dotenv

# Server configuration
HOST = "localhost"
PORT = 8119
PROJECT_ROOT = Path(__file__).parent.parent
PID_FILE = PROJECT_ROOT / ".server.pid"
PLUGIN_PID_FILE = PROJECT_ROOT / ".plugin.pid"
LOG_DIR = PROJECT_ROOT / "logs"
LOG_FILE = LOG_DIR / "server.log"
PLUGIN_LOG_FILE = LOG_DIR / "plugin.log"


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
                # Look for the zotero_plugin.py dev process
                if 'zotero_plugin.py' in cmdline_str and 'dev' in cmdline_str:
                    return proc
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
    return None


def check_zotero_connectivity_from_log():
    """Read the log file to check Zotero connectivity status and display it.

    Returns:
        True if connectivity check result found, None otherwise
    """
    if not LOG_FILE.exists():
        return None

    try:
        # Read the last 50 lines of the log
        with open(LOG_FILE, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            recent_lines = lines[-50:] if len(lines) > 50 else lines

        # Look for connectivity check messages
        for line in recent_lines:
            if "Successfully connected to Zotero API" in line:
                # Extract the URL from the log line
                if "at http" in line:
                    url_part = line.split("at http")[1].strip()
                    print(f"[OK] Zotero API connected: http{url_part}")
                else:
                    print("[OK] Zotero API connected")
                return True
            elif "WARNING: Cannot connect to Zotero API" in line:
                print("[WARN] Cannot connect to Zotero API!")
                # Print the warning details
                in_warning = True
                for warn_line in recent_lines[recent_lines.index(line):]:
                    if "⚠" in warn_line:
                        # Extract just the warning message part
                        msg = warn_line.split("⚠")[-1].strip()
                        if msg and not msg.startswith("="):
                            print(f"       {msg}")
                    elif "=" * 80 in warn_line:
                        break
                return True

    except Exception:
        # Silently ignore errors reading log
        pass

    return None


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

    # Check if already running
    existing = find_server_process()
    if existing:
        print(f"Server is already running (PID: {existing.pid})")
        print(f"Access at: http://{HOST}:{PORT}")

        # Start plugin server if requested and not running
        if with_plugin:
            plugin_proc = find_plugin_process()
            if not plugin_proc:
                start_plugin_server()
        return

    # Start the server
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

            # Save PID
            PID_FILE.write_text(str(process.pid))

            # Wait a moment for server to start and run connectivity check
            time.sleep(4)

            # Check if process is still running
            if process.poll() is None:
                print(f"[OK] Server started successfully (PID: {process.pid})")
                print(f"[OK] Access at: http://{HOST}:{PORT}")
                print(f"[OK] API docs at: http://{HOST}:{PORT}/docs")
                print(f"[OK] Logs: {LOG_FILE}")
                print(f"[INFO] Check logs for Zotero API connectivity status")

                # Start plugin server if requested
                if with_plugin:
                    start_plugin_server()

                print(f"\nTo stop: python scripts/server.py stop")
            else:
                print("[ERROR] Server failed to start")
                print(f"Check logs at: {LOG_FILE}")
                if PID_FILE.exists():
                    PID_FILE.unlink()
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

    # Clean up PID file
    if PID_FILE.exists():
        PID_FILE.unlink()


def start_plugin_server():
    """Start the plugin development server."""
    # Check if already running
    existing = find_plugin_process()
    if existing:
        print(f"Plugin server is already running (PID: {existing.pid})")
        return existing

    # Start the plugin server using the zotero_plugin.py script
    plugin_cmd = ["uv", "run", "python", "scripts/zotero_plugin.py", "dev"]

    # Command to strip ANSI codes
    strip_cmd = ["uv", "run", "python", "scripts/strip_ansi.py"]

    try:
        # Ensure log directory exists
        LOG_DIR.mkdir(exist_ok=True)

        print("Starting plugin development server...")

        # Open log file for output (truncate to start fresh)
        with open(PLUGIN_LOG_FILE, 'w') as log_file:
            # Start the plugin server process with stdout piped
            plugin_process = subprocess.Popen(
                plugin_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=PROJECT_ROOT,
                text=True,
                bufsize=1,  # Line buffered
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == 'win32' else 0
            )

            # Start the ANSI stripper process
            strip_process = subprocess.Popen(
                strip_cmd,
                stdin=plugin_process.stdout,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                cwd=PROJECT_ROOT,
                text=True,
                bufsize=1,  # Line buffered
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == 'win32' else 0
            )

            # Allow plugin_process to receive SIGPIPE if strip_process exits
            plugin_process.stdout.close()

            # Save PID of the plugin process (not the stripper)
            PLUGIN_PID_FILE.write_text(str(plugin_process.pid))

            # Wait a moment for plugin server to start
            time.sleep(2)

            # Check if process is still running
            if plugin_process.poll() is None:
                print(f"[OK] Plugin server started successfully (PID: {plugin_process.pid})")
                print(f"[OK] Plugin logs: {PLUGIN_LOG_FILE}")
                return plugin_process
            else:
                print("[ERROR] Plugin server failed to start")
                print(f"Check logs at: {PLUGIN_LOG_FILE}")
                if PLUGIN_PID_FILE.exists():
                    PLUGIN_PID_FILE.unlink()
                return None

    except Exception as e:
        print(f"[ERROR] Error starting plugin server: {e}")
        return None


def stop_plugin_server():
    """Stop the running plugin development server."""
    # Try to find by process
    proc = find_plugin_process()

    if proc:
        try:
            print(f"Stopping plugin server (PID: {proc.pid})...")

            # On Windows, we need to kill the entire process tree
            if sys.platform == 'win32':
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
                            print(f"Force killing plugin PID {p.pid}...")
                            p.kill()
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            pass

                    print("[OK] Plugin server stopped successfully")

                except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                    print(f"[ERROR] Error stopping plugin server: {e}")
            else:
                # On Unix, terminate should handle the process group
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                    print("[OK] Plugin server stopped successfully")
                except psutil.TimeoutExpired:
                    print("Plugin server didn't stop gracefully, forcing...")
                    proc.kill()
                    print("[OK] Plugin server killed")

        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
            print(f"[ERROR] Error stopping plugin server: {e}")
    else:
        print("Plugin server is not running")

    # Clean up PID file
    if PLUGIN_PID_FILE.exists():
        PLUGIN_PID_FILE.unlink()


def check_status():
    """Check if the server is running."""
    import requests

    proc = find_server_process()

    if proc:
        print(f"[OK] Server is running (PID: {proc.pid})")
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
            # Silently ignore if we can't fetch config (server might still be starting)
            pass

        return True
    else:
        print("[INFO] Server is not running")
        return False


def restart_server(dev_mode=True, with_plugin=False):
    """Restart the server and optionally the plugin server."""
    print("Restarting server...")
    stop_server()
    time.sleep(1)
    start_server(dev_mode, with_plugin)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    command = sys.argv[1].lower()

    if command == "start":
        # Check for --dev flag to enable plugin development server
        with_plugin = "--dev" in sys.argv
        start_server(dev_mode=True, with_plugin=with_plugin)
    elif command == "stop":
        stop_server()
    elif command == "restart":
        # Check for --dev flag to enable plugin development server
        with_plugin = "--dev" in sys.argv
        restart_server(dev_mode=True, with_plugin=with_plugin)
    elif command == "status":
        check_status()
    else:
        print(f"Unknown command: {command}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
