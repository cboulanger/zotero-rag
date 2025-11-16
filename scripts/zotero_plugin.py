#!/usr/bin/env python
"""
Plugin development server lifecycle management script.

This script manages the plugin development server and its associated Zotero instance.
The plugin dev server (zotero-plugin-scaffold) starts and controls a Zotero instance
via the Remote Debugging Protocol (RDP). This script ensures proper lifecycle management.

Usage:
    python scripts/zotero_plugin.py start    # Start plugin dev server (starts Zotero)
    python scripts/zotero_plugin.py stop     # Stop plugin dev server (stops Zotero)
    python scripts/zotero_plugin.py build    # Build plugin for distribution

Examples:
    python scripts/zotero_plugin.py start
    python scripts/zotero_plugin.py stop
"""

import sys
import subprocess
import platform
import time
from pathlib import Path

try:
    import psutil
except ImportError:
    psutil = None


def get_zotero_processes():
    """Get all running Zotero processes.

    Returns:
        list: List of psutil.Process objects for Zotero processes
    """
    if psutil is None:
        return []

    processes = []
    try:
        for proc in psutil.process_iter(['pid', 'name']):
            if proc.info['name'] and 'zotero' in proc.info['name'].lower():
                processes.append(proc)
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        pass

    return processes


def is_zotero_running():
    """Check if any Zotero process is currently running.

    Returns:
        bool: True if Zotero is running, False otherwise
    """
    return len(get_zotero_processes()) > 0


def find_plugin_process():
    """Find the running plugin development server process.

    Returns:
        psutil.Process or None: The plugin dev server process if found
    """
    if psutil is None:
        return None

    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            cmdline = proc.info['cmdline']
            if cmdline:
                cmdline_str = ' '.join(cmdline)
                # Look for zotero-plugin dev command (not this wrapper script)
                if 'zotero-plugin' in cmdline_str and 'dev' in cmdline_str and 'zotero_plugin.py' not in cmdline_str:
                    return proc
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass

    return None


def stop_zotero_processes(processes):
    """Stop the given Zotero processes.

    Args:
        processes: List of psutil.Process objects
    """
    for proc in processes:
        try:
            print(f"  Stopping Zotero (PID: {proc.pid})...")
            proc.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    # Wait for graceful shutdown
    if processes:
        gone, alive = psutil.wait_procs(processes, timeout=5)

        # Force kill any remaining
        for p in alive:
            try:
                print(f"  Force killing Zotero (PID: {p.pid})...")
                p.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

    if processes:
        print("[OK] Zotero stopped")


def stop_plugin_server():
    """Stop the plugin development server and associated Zotero instance.

    Returns:
        int: Exit code (0 for success, 1 for error)
    """
    if psutil is None:
        print("[ERROR] psutil not available, cannot stop plugin server", file=sys.stderr)
        return 1

    # Find the plugin dev server process
    plugin_proc = find_plugin_process()

    if not plugin_proc:
        print("[INFO] Plugin development server is not running")

        # Check if there are orphaned Zotero processes
        zotero_procs = get_zotero_processes()
        if zotero_procs:
            print(f"[WARN] Found {len(zotero_procs)} orphaned Zotero process(es)")
            print("[INFO] Cleaning up orphaned Zotero processes...")
            stop_zotero_processes(zotero_procs)

        return 0

    print(f"Stopping plugin development server (PID: {plugin_proc.pid})...")

    try:
        # On Windows, we need to kill the entire process tree
        if sys.platform == 'win32':
            parent = psutil.Process(plugin_proc.pid)
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
                    print(f"  Force killing PID {p.pid}...")
                    p.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

        else:
            # On Unix, terminate should handle the process group
            plugin_proc.terminate()
            try:
                plugin_proc.wait(timeout=5)
            except psutil.TimeoutExpired:
                print("  Plugin server didn't stop gracefully, forcing...")
                plugin_proc.kill()

        print("[OK] Plugin development server stopped")

        # Wait a moment for Zotero to clean up
        time.sleep(2)

        # Check if Zotero is still running and stop it if needed
        zotero_procs = get_zotero_processes()
        if zotero_procs:
            print(f"[INFO] Stopping {len(zotero_procs)} Zotero process(es)...")
            stop_zotero_processes(zotero_procs)

        return 0

    except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
        print(f"[ERROR] Error stopping plugin server: {e}", file=sys.stderr)
        return 1


def start_plugin_server():
    """Start the plugin development server.

    This delegates to the zotero-plugin dev command which will start Zotero.

    Returns:
        int: Exit code from the zotero-plugin command
    """
    # Check if Zotero is already running (from a previous session or user instance)
    if is_zotero_running():
        print("[ERROR] Zotero is already running", file=sys.stderr)
        print("[INFO] Please close all Zotero instances before running this command", file=sys.stderr)
        print("[INFO] The plugin development server needs to start its own Zotero instance", file=sys.stderr)
        return 1

    # Get project root and node_modules bin directory
    project_root = Path(__file__).parent.parent
    bin_dir = project_root / "node_modules" / ".bin"

    # Determine which script to use based on platform
    system = platform.system()

    if system == "Windows":
        # On Windows, use the .cmd file
        script_path = bin_dir / "zotero-plugin.cmd"
    else:
        # On Unix-like systems (Linux, macOS), use the plain script
        script_path = bin_dir / "zotero-plugin"

    # Check if the script exists
    if not script_path.exists():
        print(f"[ERROR] zotero-plugin not found at: {script_path}", file=sys.stderr)
        print(f"[INFO] Please run 'npm install' to install dependencies", file=sys.stderr)
        return 1

    # Build the command - always call "dev" on the underlying tool
    if system == "Windows":
        # On Windows, cmd files can be executed directly
        cmd = [str(script_path), "dev"]
    else:
        # On Unix-like systems, ensure it's executable
        cmd = [str(script_path), "dev"]

    # Execute the command
    try:
        # Use subprocess.run to pass through all I/O
        result = subprocess.run(cmd, cwd=project_root)
        return result.returncode
    except FileNotFoundError:
        print(f"[ERROR] Failed to execute: {script_path}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n[INFO] Interrupted by user")
        return 130
    except Exception as e:
        print(f"[ERROR] Unexpected error: {e}", file=sys.stderr)
        return 1


def main():
    """Main entry point - route commands to appropriate handlers."""
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    command = sys.argv[1].lower()

    if command == "start":
        # Start the plugin development server (which starts Zotero)
        sys.exit(start_plugin_server())

    elif command == "stop":
        # Stop the plugin development server and Zotero
        sys.exit(stop_plugin_server())

    elif command == "build":
        # Build command - delegate to zotero-plugin build
        project_root = Path(__file__).parent.parent
        bin_dir = project_root / "node_modules" / ".bin"
        system = platform.system()

        if system == "Windows":
            script_path = bin_dir / "zotero-plugin.cmd"
        else:
            script_path = bin_dir / "zotero-plugin"

        if not script_path.exists():
            print(f"[ERROR] zotero-plugin not found at: {script_path}", file=sys.stderr)
            print(f"[INFO] Please run 'npm install' to install dependencies", file=sys.stderr)
            sys.exit(1)

        try:
            result = subprocess.run([str(script_path), "build"], cwd=project_root)
            sys.exit(result.returncode)
        except Exception as e:
            print(f"[ERROR] Failed to build: {e}", file=sys.stderr)
            sys.exit(1)

    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
