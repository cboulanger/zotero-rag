"""The backend plugin host: discovery in several directories, isolation of broken
plugins, disabling, capability registration and local engine selection."""

import asyncio
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.pluginhost import (
    SHIPPED_PLUGIN_DIR,
    BackendPlugin,
    Capability,
    InferenceProvider,
    LocalEngineUnavailable,
    LocalInference,
    PluginHost,
    get_plugin_host,
    select_local_engine,
)
from backend.providers import PROVIDERS


def _write_plugin(root: Path, name: str, body: str, files: dict | None = None) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "__init__.py").write_text(textwrap.dedent(body))
    for fname, content in (files or {}).items():
        (directory / fname).write_text(textwrap.dedent(content))
    return directory


class ExternalPluginTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self._forget_modules)

    def _forget_modules(self):
        for name in [m for m in sys.modules if m.startswith("zotero_rag_plugins.")]:
            del sys.modules[name]
        for pid in [p for p in PROVIDERS if p.startswith("test-ext-")]:
            del PROVIDERS[pid]

    def test_external_plugin_with_relative_import_registers_a_provider(self):
        _write_plugin(self.root, "ext_vendor", '''
            from backend.pluginhost import BackendPlugin, InferenceProvider
            from .provider import ExtProvider

            def register():
                return BackendPlugin(id="ext-vendor", name="Ext", capabilities=[InferenceProvider(provider=ExtProvider)])
        ''', {"provider.py": '''
            from backend.providers import Provider

            class ExtProvider(Provider):
                id = "test-ext-vendor"
                label = "External vendor"
                supported_sides = frozenset({"llm"})
        '''})
        host = PluginHost(dirs=[self.root], disabled=[]).load()
        record = {r.id: r for r in host.plugins()}["ext-vendor"]
        self.assertEqual(record.status, "loaded")
        self.assertEqual(record.capabilities[0].plugin_id, "ext-vendor")
        self.assertIn("test-ext-vendor", PROVIDERS)
        described = host.capability("inference.provider", "test-ext-vendor").describe()
        self.assertEqual(described["sides"], ["llm"])
        self.assertEqual(described["plugin"], "ext-vendor")

    def test_a_broken_plugin_is_reported_and_does_not_stop_the_others(self):
        _write_plugin(self.root, "a_broken", "raise RuntimeError('boom')\n")
        _write_plugin(self.root, "b_no_register", "X = 1\n")
        _write_plugin(self.root, "c_fine", '''
            from backend.pluginhost import BackendPlugin
            def register():
                return BackendPlugin(id="fine", name="Fine")
        ''')
        host = PluginHost(dirs=[self.root], disabled=[]).load()
        records = {r.id: r for r in host.plugins()}
        self.assertEqual(records["a_broken"].status, "failed")
        self.assertIn("boom", records["a_broken"].error)
        self.assertEqual(records["b_no_register"].status, "failed")
        self.assertEqual(records["fine"].status, "loaded")

    def test_disabled_plugins_are_listed_but_not_imported(self):
        _write_plugin(self.root, "off", "raise RuntimeError('must not be imported')\n")
        host = PluginHost(dirs=[self.root], disabled=["off"]).load()
        self.assertEqual({r.id: r.status for r in host.plugins()}["off"], "disabled")

    def test_missing_directory_is_skipped(self):
        host = PluginHost(dirs=[self.root / "nope"], disabled=[]).load()
        self.assertEqual([r.id for r in host.plugins()], ["core"])

    def test_duplicate_plugin_and_capability_ids_are_rejected_whole(self):
        for name in ("one", "two"):
            _write_plugin(self.root, name, f'''
                from backend.pluginhost import BackendPlugin, Capability
                def register():
                    return BackendPlugin(id="{'same' if name == 'two' else 'first'}", name="{name}",
                                         capabilities=[Capability(id="shared")])
            ''')
        host = PluginHost(dirs=[self.root], disabled=[]).load()
        records = {r.id: r for r in host.plugins()}
        self.assertEqual(records["first"].status, "loaded")
        self.assertEqual(records["two"].status, "failed")
        self.assertIn("already registered", records["two"].error)
        self.assertEqual(host.capability("capability", "shared").plugin_id, "first")


class CapabilityTest(unittest.TestCase):
    def test_new_capability_kinds_need_no_host_change(self):
        class Extraction(Capability):
            kind = "extraction"

        host = PluginHost(dirs=[], disabled=[]).load()
        host.add(BackendPlugin(id="extractor", name="E", capabilities=[Extraction(id="pdf")]))
        self.assertEqual([c.id for c in host.capabilities("extraction")], ["pdf"])
        self.assertEqual(host.describe()["capabilities"][-1]["kind"], "extraction")

    def test_local_inference_reports_missing_packages_and_platforms(self):
        cap = LocalInference(side="embedding", engine="x", create=lambda *a, **k: None, requires=("no_such_module_xyz",))
        self.assertIn("no_such_module_xyz", cap.unavailable_reason())
        cap = LocalInference(side="embedding", engine="x", create=lambda *a, **k: None, platforms=("plan9",))
        self.assertIn("plan9", cap.unavailable_reason())
        self.assertFalse(cap.describe()["available"])

    def test_inference_provider_id_must_match_the_class(self):
        from backend.providers import Provider

        with self.assertRaises(ValueError):
            InferenceProvider(id="other", provider=Provider)


class ShippedPluginsTest(unittest.TestCase):
    def test_every_shipped_plugin_loads(self):
        records = {r.id: r for r in get_plugin_host().plugins()}
        from backend.pluginhost.host import disabled_plugin_ids

        shipped = sorted(p.name for p in SHIPPED_PLUGIN_DIR.iterdir() if (p / "__init__.py").is_file())
        for name in sorted(set(shipped) - disabled_plugin_ids()):
            self.assertEqual(records[name].status, "loaded", records[name].error)

    def test_unknown_engine_is_an_error(self):
        with self.assertRaises(LocalEngineUnavailable):
            select_local_engine("embedding", "mlx")

    def test_an_available_engine_beats_a_higher_priority_unavailable_one(self):
        good = LocalInference(side="embedding", engine="test-good", create=lambda *a, **k: "good", priority=-100)
        bad = LocalInference(side="embedding", engine="test-bad", create=lambda *a, **k: "bad", priority=100,
                             check=lambda: "never here")
        # Availability depends on the install; fix it for this check.
        with patch.object(LocalInference, "unavailable_reason", lambda self: None if self.engine == "test-good" else "no"):
            fake = PluginHost(dirs=[], disabled=[]).load()
            fake.add(BackendPlugin(id="engines", name="E", capabilities=[good, bad]))
            with patch("backend.pluginhost.get_plugin_host", return_value=fake):
                self.assertEqual(select_local_engine("embedding").engine, "test-good")

    def test_lifecycle_hooks_run_and_failures_are_contained(self):
        calls = []

        async def start_ok():
            calls.append("start")

        async def start_fail():
            raise RuntimeError("down")

        async def stop_ok():
            calls.append("stop")

        host = PluginHost(dirs=[], disabled=[]).load()
        host.add(BackendPlugin(id="ok", name="OK", on_startup=start_ok, on_shutdown=stop_ok))
        host.add(BackendPlugin(id="bad", name="Bad", on_startup=start_fail, on_shutdown=stop_ok))
        asyncio.run(host.startup())
        asyncio.run(host.shutdown())
        self.assertEqual(calls, ["start", "stop"])  # the failed plugin is not shut down


if __name__ == "__main__":
    unittest.main()
