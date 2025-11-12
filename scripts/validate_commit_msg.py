#!/usr/bin/env python3
"""
Commit message validator for Conventional Commits.

Validates commit messages against the conventional commits specification.
Can be used as a Git commit-msg hook or standalone validator.

Usage:
    python scripts/validate_commit_msg.py <commit-msg-file>
    python scripts/validate_commit_msg.py --message "feat: add new feature"
"""

import re
import sys
from pathlib import Path
from typing import Tuple

# Conventional Commits regex pattern
# Format: <type>(<scope>): <subject>
# Optional: <type>(<scope>)!: <subject> for breaking changes
COMMIT_PATTERN = re.compile(
    r"^(?P<type>feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)"
    r"(?:\((?P<scope>[a-z0-9-]+)\))?"
    r"(?P<breaking>!)?"
    r": "
    r"(?P<subject>.+)$",
    re.IGNORECASE
)

VALID_TYPES = [
    "feat",      # New feature
    "fix",       # Bug fix
    "docs",      # Documentation changes
    "style",     # Code style changes (formatting, etc.)
    "refactor",  # Code refactoring
    "perf",      # Performance improvements
    "test",      # Adding or updating tests
    "build",     # Build system or dependencies
    "ci",        # CI/CD changes
    "chore",     # Other changes (maintenance)
    "revert",    # Revert previous commit
]


def validate_commit_message(message: str) -> Tuple[bool, str]:
    """
    Validate a commit message against conventional commits spec.

    Args:
        message: The commit message to validate

    Returns:
        Tuple of (is_valid, error_message)
    """
    # Get first line (ignore body and footer)
    first_line = message.strip().split("\n")[0]

    # Check length
    if len(first_line) > 100:
        return False, f"Commit message too long ({len(first_line)} chars, max 100)"

    # Check format
    match = COMMIT_PATTERN.match(first_line)
    if not match:
        return False, (
            "Invalid commit message format.\n\n"
            "Expected format:\n"
            "  <type>(<scope>): <subject>\n\n"
            "Valid types: " + ", ".join(VALID_TYPES) + "\n\n"
            "Examples:\n"
            "  feat: add new feature\n"
            "  fix(api): resolve crash on startup\n"
            "  docs: update README\n"
            "  feat!: breaking API change\n"
        )

    # Validate type
    commit_type = match.group("type").lower()
    if commit_type not in VALID_TYPES:
        return False, (
            f"Invalid commit type: {commit_type}\n"
            f"Valid types: {', '.join(VALID_TYPES)}"
        )

    # Validate subject
    subject = match.group("subject")
    if not subject:
        return False, "Commit subject cannot be empty"

    if subject[0].isupper():
        return False, "Commit subject must start with lowercase letter"

    if subject.endswith("."):
        return False, "Commit subject must not end with a period"

    return True, "Valid commit message"


def main():
    """Main entry point."""
    if len(sys.argv) < 2:
        print("Usage: python validate_commit_msg.py <commit-msg-file>")
        print("   or: python validate_commit_msg.py --message 'commit message'")
        sys.exit(1)

    # Read commit message
    if sys.argv[1] == "--message":
        if len(sys.argv) < 3:
            print("Error: --message requires a message argument")
            sys.exit(1)
        message = sys.argv[2]
    else:
        commit_msg_file = Path(sys.argv[1])
        if not commit_msg_file.exists():
            print(f"Error: File not found: {commit_msg_file}")
            sys.exit(1)
        message = commit_msg_file.read_text(encoding="utf-8")

    # Validate
    is_valid, error_msg = validate_commit_message(message)

    if is_valid:
        print(f"[VALID] {error_msg}")
        sys.exit(0)
    else:
        print("\n" + "=" * 70)
        print("[ERROR] Invalid commit message")
        print("=" * 70)
        print(f"\nYour message:\n  {message.strip().split(chr(10))[0]}")
        print(f"\n{error_msg}")
        print("\nCommit aborted. Please fix your commit message and try again.")
        print("=" * 70 + "\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
