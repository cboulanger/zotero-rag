"""Regression test for scripts/zotero_plugin.py's Zotero process targeting.

`npm run dev:stop` (server.py stop -> zotero_plugin.py stop) must only ever
touch the Zotero process the plugin dev server itself launched -- never any
other Zotero instance (e.g. the user's main personal instance) that happens
to be running concurrently. See CLAUDE.md's "Never kill/restart Zotero by
name -- always target the dev instance's PID" section: the dev instance is
distinguished only by the `-profile "$ZOTERO_PLUGIN_PROFILE_PATH"` argument
on its command line, so process selection must match on that, not on the
process name alone.

Run with: uv run pytest backend/tests/test_zotero_plugin_dev.py
"""

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).parent.parent.parent
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "zotero_plugin.py"


def _load_zotero_plugin_module():
    """Load scripts/zotero_plugin.py as a module without needing a package."""
    spec = importlib.util.spec_from_file_location("zotero_plugin_dev_test_target", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


zotero_plugin = _load_zotero_plugin_module()


class FakeProcess:
    """Minimal stand-in for psutil.Process as returned by process_iter(attrs)."""

    def __init__(self, pid, name, cmdline):
        self.pid = pid
        self.info = {"pid": pid, "name": name, "cmdline": cmdline}


DEV_PROFILE = "/Users/cboulanger/Library/Application Support/Zotero/Profiles/5m0xb0bq.zotero-rag"
MAIN_PROFILE = "/Users/cboulanger/Library/Application Support/Zotero/Profiles/abc12345.default"


class TestGetZoteroProcesses(unittest.TestCase):
    """get_zotero_processes() must scope to the dev profile, not match by name."""

    def test_only_returns_process_matching_dev_profile(self):
        dev_proc = FakeProcess(111, "zotero", ["/Applications/Zotero.app/Contents/MacOS/zotero", "-profile", DEV_PROFILE])
        main_proc = FakeProcess(222, "zotero", ["/Applications/Zotero.app/Contents/MacOS/zotero", "-profile", MAIN_PROFILE])
        unrelated_proc = FakeProcess(333, "Zotero", ["/Applications/Zotero.app/Contents/MacOS/zotero"])

        with mock.patch.object(zotero_plugin.os, "environ", {"ZOTERO_PLUGIN_PROFILE_PATH": DEV_PROFILE}), \
             mock.patch.object(zotero_plugin.psutil, "process_iter", return_value=[dev_proc, main_proc, unrelated_proc]):
            result = zotero_plugin.get_zotero_processes()

        result_pids = {p.pid for p in result}
        self.assertEqual(result_pids, {111}, "must select only the process launched with the dev profile")

    def test_returns_empty_when_profile_path_not_configured(self):
        # Without a configured dev profile there is no safe way to distinguish
        # the dev Zotero instance from any other -- must NOT fall back to
        # matching every Zotero process by name.
        dev_proc = FakeProcess(111, "zotero", ["/Applications/Zotero.app/Contents/MacOS/zotero", "-profile", DEV_PROFILE])
        main_proc = FakeProcess(222, "zotero", ["/Applications/Zotero.app/Contents/MacOS/zotero", "-profile", MAIN_PROFILE])

        with mock.patch.object(zotero_plugin.os, "environ", {}), \
             mock.patch.object(zotero_plugin.psutil, "process_iter", return_value=[dev_proc, main_proc]):
            result = zotero_plugin.get_zotero_processes()

        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
