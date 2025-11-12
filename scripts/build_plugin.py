#!/usr/bin/env python3

"""
Build script for Zotero RAG plugin
Creates an XPI file from the plugin source directory
"""

import os
import sys
import json
import shutil
import zipfile
from pathlib import Path


def get_plugin_paths():
    """Get all required paths for the build process."""
    script_dir = Path(__file__).parent
    plugin_dir = script_dir.parent / 'plugin'

    return {
        'plugin': plugin_dir,
        'src': plugin_dir / 'src',
        'locale': plugin_dir / 'locale',
        'build': plugin_dir / 'build',
        'dist': plugin_dir / 'dist',
        'manifest': plugin_dir / 'manifest.json'
    }


def clean(paths):
    """Clean build directories."""
    print('Cleaning build directories...')

    # Remove build and dist directories if they exist
    for dir_key in ['build', 'dist']:
        dir_path = paths[dir_key]
        if dir_path.exists():
            shutil.rmtree(dir_path)

    # Create fresh directories
    paths['build'].mkdir(parents=True, exist_ok=True)
    paths['dist'].mkdir(parents=True, exist_ok=True)


def copy_files(paths):
    """Copy plugin files to build directory."""
    print('Copying plugin files...')

    # Copy manifest
    shutil.copy2(paths['manifest'], paths['build'] / 'manifest.json')

    # Copy source files
    src_dest = paths['build']
    if paths['src'].exists():
        for item in paths['src'].iterdir():
            if item.is_dir():
                shutil.copytree(item, src_dest / item.name, dirs_exist_ok=True)
            else:
                shutil.copy2(item, src_dest / item.name)

    # Copy locale files
    locale_dest = paths['build'] / 'locale'
    if paths['locale'].exists():
        shutil.copytree(paths['locale'], locale_dest, dirs_exist_ok=True)

    print('Files copied to build directory')


def create_xpi(paths):
    """Create XPI archive."""
    print('Creating XPI archive...')

    # Read version from manifest
    with open(paths['manifest'], 'r', encoding='utf-8') as f:
        manifest = json.load(f)

    version = manifest['version']
    xpi_name = f"zotero-rag-{version}.xpi"
    xpi_path = paths['dist'] / xpi_name

    # Create ZIP archive with .xpi extension
    with zipfile.ZipFile(xpi_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as xpi:
        # Add all files from build directory
        build_dir = paths['build']
        for root, dirs, files in os.walk(build_dir):
            for file in files:
                file_path = Path(root) / file
                # Calculate relative path from build directory
                arcname = file_path.relative_to(build_dir)
                xpi.write(file_path, arcname)

    # Get file size
    file_size = xpi_path.stat().st_size
    print(f"XPI created: {xpi_path} ({file_size} bytes)")

    return xpi_path


def build():
    """Main build function."""
    print('Building Zotero RAG plugin...\n')

    try:
        paths = get_plugin_paths()

        # Validate that plugin directory exists
        if not paths['plugin'].exists():
            raise FileNotFoundError(f"Plugin directory not found: {paths['plugin']}")

        clean(paths)
        copy_files(paths)
        xpi_path = create_xpi(paths)

        print('\n[PASS] Build successful!')
        print('\nTo install the plugin:')
        print('1. Open Zotero')
        print('2. Go to Tools > Add-ons')
        print('3. Click the gear icon > Install Add-on From File')
        print(f'4. Select: {xpi_path}')

    except Exception as error:
        print(f'\n[FAIL] Build failed: {error}', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    # Check for watch mode
    if '--watch' in sys.argv:
        print('Watch mode not yet implemented. Use the build script directly for now.')
        sys.exit(1)

    # Run build
    build()
