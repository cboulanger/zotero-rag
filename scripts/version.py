#!/usr/bin/env python3
"""
Version management script for zotero-rag project.

Updates version numbers across all project files:
- package.json
- pyproject.toml
- plugin/manifest.json
- backend/__version__.py

Usage:
    python scripts/version.py [patch|minor|major|VERSION]

Examples:
    python scripts/version.py patch    # 0.1.0 -> 0.1.1
    python scripts/version.py minor    # 0.1.0 -> 0.2.0
    python scripts/version.py major    # 0.1.0 -> 1.0.0
    python scripts/version.py 1.5.2    # Set specific version
"""

import json
import re
import sys
from pathlib import Path
from typing import Tuple

PROJECT_ROOT = Path(__file__).parent.parent


def parse_version(version: str) -> Tuple[int, int, int]:
    """Parse semantic version string into tuple."""
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:-.*)?$", version)
    if not match:
        raise ValueError(f"Invalid version format: {version}")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def format_version(major: int, minor: int, patch: int) -> str:
    """Format version tuple as string."""
    return f"{major}.{minor}.{patch}"


def increment_version(current: str, bump_type: str) -> str:
    """Increment version based on bump type."""
    major, minor, patch = parse_version(current)

    if bump_type == "major":
        return format_version(major + 1, 0, 0)
    elif bump_type == "minor":
        return format_version(major, minor + 1, 0)
    elif bump_type == "patch":
        return format_version(major, minor, patch + 1)
    else:
        # Assume it's a specific version string
        parse_version(bump_type)  # Validate format
        return bump_type


def get_current_version() -> str:
    """Get current version from package.json."""
    package_json = PROJECT_ROOT / "package.json"
    with open(package_json) as f:
        data = json.load(f)
    return data["version"]


def update_package_json(new_version: str) -> None:
    """Update version in package.json."""
    package_json = PROJECT_ROOT / "package.json"
    with open(package_json) as f:
        data = json.load(f)

    data["version"] = new_version

    with open(package_json, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")  # Ensure newline at end

    print(f"[UPDATED] package.json -> {new_version}")


def update_pyproject_toml(new_version: str) -> None:
    """Update version in pyproject.toml."""
    pyproject = PROJECT_ROOT / "pyproject.toml"
    content = pyproject.read_text()

    # Update version line in [project] section
    updated = re.sub(
        r'^version = "[^"]+"',
        f'version = "{new_version}"',
        content,
        flags=re.MULTILINE
    )

    pyproject.write_text(updated)
    print(f"[UPDATED] pyproject.toml -> {new_version}")


def update_manifest_json(new_version: str) -> None:
    """Update version in plugin/manifest.json."""
    manifest = PROJECT_ROOT / "plugin" / "manifest.json"
    with open(manifest) as f:
        data = json.load(f)

    data["version"] = new_version

    with open(manifest, "w") as f:
        json.dump(data, f, indent="\t")
        f.write("\n")  # Ensure newline at end

    print(f"[UPDATED] plugin/manifest.json -> {new_version}")


def create_version_file(new_version: str) -> None:
    """Create/update backend/__version__.py."""
    version_file = PROJECT_ROOT / "backend" / "__version__.py"
    version_file.parent.mkdir(exist_ok=True)

    content = f'''"""Version information for zotero-rag backend."""

__version__ = "{new_version}"
'''

    version_file.write_text(content)
    print(f"[UPDATED] backend/__version__.py -> {new_version}")


def main():
    if len(sys.argv) != 2:
        print("Usage: python scripts/version.py [patch|minor|major|VERSION]")
        print()
        print("Examples:")
        print("  python scripts/version.py patch    # Increment patch version")
        print("  python scripts/version.py minor    # Increment minor version")
        print("  python scripts/version.py major    # Increment major version")
        print("  python scripts/version.py 1.5.2    # Set specific version")
        sys.exit(1)

    bump_type = sys.argv[1]

    try:
        current_version = get_current_version()
        new_version = increment_version(current_version, bump_type)

        print(f"\nUpdating version: {current_version} -> {new_version}")
        print("=" * 50)

        # Update all files
        update_package_json(new_version)
        update_pyproject_toml(new_version)
        update_manifest_json(new_version)
        create_version_file(new_version)

        print("=" * 50)
        print(f"\n[SUCCESS] All files updated to version {new_version}")
        print(f"\nNext steps:")
        print(f"  1. Review changes: git diff")
        print(f"  2. Create release: npm run release:commit")

    except Exception as e:
        print(f"\n[ERROR] Failed to update version: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
