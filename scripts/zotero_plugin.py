#!/usr/bin/env python
"""
Cross-platform proxy script for zotero-plugin command.

This script detects the platform and calls the appropriate zotero-plugin
executable (.cmd on Windows, shell script on Unix-like systems).

TEMPORARY WORKAROUND: This script also patches a bug in zotero-plugin-scaffold
where user preferences are not properly merged. See docs/bug-report.md for details.

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

def apply_scaffold_patch(project_root):
    """
    TEMPORARY WORKAROUND: Patch the zotero-plugin-scaffold preference merging bug.

    This fixes a bug where user preferences in test.prefs are ignored because
    Object.assign(defaultPref, userPrefs) mutates defaultPref instead of creating
    a new object with proper override behavior.

    See docs/bug-report.md for details.
    Once upstream bug is fixed, this function can be removed.
    """
    # Use glob to find the scaffold file (hash in filename may change between builds)
    scaffold_dir = project_root / "node_modules" / "zotero-plugin-scaffold" / "dist" / "shared"

    if not scaffold_dir.exists():
        # Directory doesn't exist, skip patching
        return

    # Find all matching files (should only be one)
    scaffold_files = list(scaffold_dir.glob("zotero-plugin-scaffold.*.mjs"))

    if not scaffold_files:
        # No matching files found, skip patching
        return

    # Patch all matching files (though there should only be one)
    for scaffold_file in scaffold_files:
        try:
            content = scaffold_file.read_text(encoding='utf-8')

            # Check if already patched
            buggy_line = "return Object.assign(defaultPref, this.ctx.test.prefs || {});"
            fixed_line = "return Object.assign({}, defaultPref, this.ctx.test.prefs || {});"

            if buggy_line in content:
                # Apply the patch
                content = content.replace(buggy_line, fixed_line)
                scaffold_file.write_text(content, encoding='utf-8')
                print(f"[PATCH] Applied zotero-plugin-scaffold preference merging fix to {scaffold_file.name}")
                print("[PATCH] This workaround can be removed once upstream bug is fixed")
                print("[PATCH] See: docs/bug-report.md")
            elif fixed_line in content:
                # Already patched, no action needed
                pass
            else:
                # Pattern not found - scaffold may have been updated
                print("[WARN] Could not find expected code pattern in zotero-plugin-scaffold")
                print("[WARN] The upstream bug may have been fixed. You can remove the patch code.")
        except Exception as e:
            print(f"[WARN] Failed to apply scaffold patch: {e}", file=sys.stderr)
            # Continue anyway - the patch is a workaround, not critical


def main():
    """Execute the appropriate zotero-plugin script based on platform."""
    # Get project root and node_modules bin directory
    project_root = Path(__file__).parent.parent
    bin_dir = project_root / "node_modules" / ".bin"

    # Apply the scaffold patch before running
    apply_scaffold_patch(project_root)

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
