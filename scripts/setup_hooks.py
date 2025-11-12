#!/usr/bin/env python3
"""
Setup Git hooks for commit message validation.

This script installs a commit-msg hook that validates commit messages
against the Conventional Commits specification.

Usage:
    python scripts/setup_hooks.py
"""

import os
import stat
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
GIT_HOOKS_DIR = PROJECT_ROOT / ".git" / "hooks"
COMMIT_MSG_HOOK = GIT_HOOKS_DIR / "commit-msg"


def create_commit_msg_hook():
    """Create the commit-msg hook file."""
    # Ensure hooks directory exists
    GIT_HOOKS_DIR.mkdir(parents=True, exist_ok=True)

    # Determine if we're on Windows
    is_windows = sys.platform == "win32"

    if is_windows:
        # Windows: Use Python script directly
        hook_content = f"""@echo off
REM Commit message validation hook (Windows)
python "{PROJECT_ROOT / 'scripts' / 'validate_commit_msg.py'}" %1
exit /b %errorlevel%
"""
        hook_file = COMMIT_MSG_HOOK.with_suffix(".bat")
    else:
        # Unix: Use shell script
        hook_content = f"""#!/bin/sh
# Commit message validation hook (Unix)
python3 "{PROJECT_ROOT / 'scripts' / 'validate_commit_msg.py'}" "$1"
"""
        hook_file = COMMIT_MSG_HOOK

    # Write hook file
    hook_file.write_text(hook_content, encoding="utf-8")

    # Make executable on Unix
    if not is_windows:
        current_permissions = hook_file.stat().st_mode
        hook_file.chmod(current_permissions | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    print(f"[SUCCESS] Git hook installed: {hook_file}")
    return True


def main():
    """Main entry point."""
    print("=" * 70)
    print("Setting up Git hooks for commit message validation")
    print("=" * 70)

    # Check if we're in a Git repository
    if not (PROJECT_ROOT / ".git").exists():
        print("\n[ERROR] Not a Git repository. Initialize Git first:")
        print("  git init")
        sys.exit(1)

    # Create commit-msg hook
    try:
        create_commit_msg_hook()
        print("\n[INFO] Commit message hook installed successfully!")
        print("\nAll commits will now be validated against Conventional Commits spec.")
        print("\nValid commit format:")
        print("  <type>(<scope>): <subject>")
        print("\nExamples:")
        print("  feat: add new feature")
        print("  fix(api): resolve crash on startup")
        print("  docs: update README")
        print("  feat!: breaking API change")
        print("\nValid types: feat, fix, docs, style, refactor, perf, test, build, ci, chore, revert")
        print("=" * 70 + "\n")

    except Exception as e:
        print(f"\n[ERROR] Failed to install hooks: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
