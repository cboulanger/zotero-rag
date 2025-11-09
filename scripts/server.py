#!/usr/bin/env python
"""
Cross-platform server management script for Zotero RAG backend.

Usage:
    python scripts/server.py start      # Start the server
    python scripts/server.py stop       # Stop the server
    python scripts/server.py restart    # Restart the server
    python scripts/server.py status     # Check server status
"""

import sys
import subprocess
import signal
import time
import psutil
from pathlib import Path

# Server configuration
HOST = "localhost"
PORT = 8119
PROJECT_ROOT = Path(__file__).parent.parent
PID_FILE = PROJECT_ROOT / ".server.pid"
LOG_DIR = PROJECT_ROOT / "logs"
LOG_FILE = LOG_DIR / "server.log"


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


def start_server(dev_mode=True):
    """Start the FastAPI server."""
    # Check if already running
    existing = find_server_process()
    if existing:
        print(f"Server is already running (PID: {existing.pid})")
        print(f"Access at: http://{HOST}:{PORT}")
        return

    # Start the server
    cmd = [
        "uv", "run", "uvicorn", "backend.main:app",
        "--host", HOST,
        "--port", str(PORT)
    ]

    if dev_mode:
        cmd.append("--reload")
        print("Starting server in development mode (with auto-reload)...")
    else:
        print("Starting server in production mode...")

    try:
        # Ensure log directory exists
        LOG_DIR.mkdir(exist_ok=True)

        # Open log file for output
        with open(LOG_FILE, 'w') as log_file:
            # Start in background
            process = subprocess.Popen(
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == 'win32' else 0
            )

            # Save PID
            PID_FILE.write_text(str(process.pid))

            # Wait a moment for server to start
            time.sleep(3)

            # Check if process is still running
            if process.poll() is None:
                print(f"[OK] Server started successfully (PID: {process.pid})")
                print(f"[OK] Access at: http://{HOST}:{PORT}")
                print(f"[OK] API docs at: http://{HOST}:{PORT}/docs")
                print(f"[OK] Logs: {LOG_FILE}")
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
    """Stop the running server."""
    # Try to find by process
    proc = find_server_process()

    if proc:
        try:
            print(f"Stopping server (PID: {proc.pid})...")
            proc.terminate()

            # Wait up to 5 seconds for graceful shutdown
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


def check_status():
    """Check if the server is running."""
    proc = find_server_process()

    if proc:
        print(f"[OK] Server is running (PID: {proc.pid})")
        print(f"  Access at: http://{HOST}:{PORT}")
        print(f"  API docs at: http://{HOST}:{PORT}/docs")
        return True
    else:
        print("[INFO] Server is not running")
        return False


def restart_server(dev_mode=True):
    """Restart the server."""
    print("Restarting server...")
    stop_server()
    time.sleep(1)
    start_server(dev_mode)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    command = sys.argv[1].lower()

    if command == "start":
        dev_mode = "--prod" not in sys.argv
        start_server(dev_mode)
    elif command == "stop":
        stop_server()
    elif command == "restart":
        dev_mode = "--prod" not in sys.argv
        restart_server(dev_mode)
    elif command == "status":
        check_status()
    else:
        print(f"Unknown command: {command}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
