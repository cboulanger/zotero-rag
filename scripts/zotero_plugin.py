#!/usr/bin/env python
"""
Cross-platform proxy script for zotero-plugin command.

This script detects the platform and calls the appropriate zotero-plugin
executable (.cmd on Windows, shell script on Unix-like systems).

Usage:
    python scripts/zotero_plugin.py <command> [args...]

Examples:
    python scripts/zotero_plugin.py dev
    python scripts/zotero_plugin.py build
"""

import sys
import subprocess
import platform
from pathlib import Path

def main():
    """Execute the appropriate zotero-plugin script based on platform."""
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
        sys.exit(1)

    # Get command-line arguments (excluding the script name)
    args = sys.argv[1:]

    # Build the command
    if system == "Windows":
        # On Windows, cmd files can be executed directly
        cmd = [str(script_path)] + args
    else:
        # On Unix-like systems, ensure it's executable
        cmd = [str(script_path)] + args

    # Execute the command
    try:
        # Use subprocess.run to pass through all I/O
        result = subprocess.run(cmd, cwd=project_root)
        sys.exit(result.returncode)
    except FileNotFoundError:
        print(f"[ERROR] Failed to execute: {script_path}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n[INFO] Interrupted by user")
        sys.exit(130)
    except Exception as e:
        print(f"[ERROR] Unexpected error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
