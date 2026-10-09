"""Tests for the indexed-status tag feature: event log, VectorStore hooks and
ground-truth query, reconciliation planning, script library enumeration, run
tailing and the HTTP endpoints."""

import asyncio
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from qdrant_client.models import Distance

from backend.config.settings import get_settings, reset_settings
from backend.db.vector_store import VectorStore
from backend.main import app
from backend.models.document import ChunkMetadata, DocumentChunk, DocumentMetadata
from backend.services.autoindex_key_store import AutoIndexKeyStore, fingerprint
from backend.services.autoindex_resolver import resolve_targets
from backend.services.index_event_log import INDEXED_TAG_NAME, IndexEventLog
from backend.services.indexed_tag_runs import read_run
from backend.services.indexed_tag_sync import IndexedTagSync, plan_tag_ops
from backend.zotero.key_validator import KeyValidation


def _chunk(lib="1", item="ITEM1", att="ATT1", idx=0, has_content=True) -> DocumentChunk:
    return DocumentChunk(
        text="t",
        metadata=ChunkMetadata(
            chunk_id=f"{lib}:{item}:{att}:{idx}",
            document_metadata=DocumentMetadata(library_id=lib, item_key=item, attachment_key=att, title="T"),
            text_preview="t",
            chunk_index=idx,
            content_hash=f"h{lib}{item}{att}{idx}",
            has_content=has_content,
        ),
        embedding=[0.1] * 8,
    )


def _att(key, tagged=False, parent="PARENT"):
    return {"data": {"key": key, "parentItem": parent, "tags": [{"tag": INDEXED_TAG_NAME, "type": 1}] if tagged else []}}


class IndexEventLogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.log = IndexEventLog(Path(self.tmp) / "events.jsonl", max_events=5)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sequence_and_read_since(self):
        self.assertEqual(self.log.last_seq(), 0)
        self.log.append([{"type": "indexed", "library_id": "1", "attachment_key": "A"}])
        self.log.append([{"type": "unindexed", "library_id": "1", "attachment_key": "A"}])
        out = self.log.read_since(1)
        self.assertEqual([e["seq"] for e in out["events"]], [2])
        self.assertEqual(out["last_seq"], 2)
        self.assertFalse(out["gap"])

    def test_rotation_keeps_sequence_monotonic_and_reports_gap(self):
        for i in range(8):
            self.log.append([{"type": "indexed", "library_id": "1", "attachment_key": str(i)}])
        out = self.log.read_since(0)
        self.assertEqual(len(out["events"]), 5)
        self.assertEqual(out["events"][-1]["seq"], 8)
        self.assertTrue(out["gap"])
        self.assertFalse(self.log.read_since(5)["gap"])

    def test_torn_line_is_ignored(self):
        self.log.append([{"type": "indexed", "library_id": "1", "attachment_key": "A"}])
        with open(self.log.path, "a") as f:
            f.write('{"seq": 2, "typ')
        self.assertEqual(len(self.log.read_since(0)["events"]), 1)


class VectorStoreIndexedStateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = VectorStore(Path(self.tmp) / "q", embedding_dim=8, embedding_model_name="m", distance=Distance.COSINE)
        self.store.index_events = IndexEventLog(Path(self.tmp) / "events.jsonl")

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _events(self):
        return [(e["type"], e.get("attachment_key")) for e in self.store.index_events.read_since(0)["events"]]

    def test_indexed_keys_query(self):
        self.store.add_chunks_batch([_chunk(att="A1"), _chunk(att="A1", idx=1), _chunk(att="A2")])
        self.store.add_chunks_batch([_chunk(lib="2", att="OTHERLIB")])
        self.assertEqual(self.store.get_indexed_attachment_keys("1"), {"A1", "A2"})
        self.assertEqual(self.store.get_indexed_attachment_keys("1", ["A1", "MISSING"]), {"A1"})
        self.assertEqual(self.store.get_indexed_attachment_keys("1", []), set())

    def test_catalog_stubs_and_legacy_chunks_do_not_count(self):
        self.store.add_chunk(_chunk(att="STUB", has_content=False))
        self.store.add_chunks_batch([_chunk(att=None)])
        self.assertEqual(self.store.get_indexed_attachment_keys("1"), set())
        self.assertEqual(self._events(), [])

    def test_events_emitted_once_per_attachment_on_index_and_delete(self):
        self.store.add_chunks_batch([_chunk(att="A1"), _chunk(att="A1", idx=1), _chunk(att="A2")])
        self.assertEqual(self._events(), [("indexed", "A1"), ("indexed", "A2")])
        self.store.delete_item_chunks("1", "ITEM1")
        self.assertEqual(self._events()[2:], [("unindexed", "A1"), ("unindexed", "A2")])
        self.assertEqual(self.store.get_indexed_attachment_keys("1"), set())

    def test_library_delete_emits_library_event(self):
        self.store.add_chunks_batch([_chunk(att="A1")])
        self.store.delete_library_chunks("1")
        last = self.store.index_events.read_since(0)["events"][-1]
        self.assertEqual((last["type"], last["library_id"]), ("library_unindexed", "1"))

    def test_no_event_log_is_a_noop(self):
        self.store.index_events = None
        self.store.add_chunks_batch([_chunk()])  # must not raise


class PlanTagOpsTest(unittest.TestCase):
    def test_add_remove_and_noop(self):
        ops = plan_tag_ops([_att("A"), _att("B", tagged=True), _att("C", tagged=True), _att("D")], {"A", "C"})
        self.assertEqual([(o["op"], o["attachment_key"]) for o in ops], [("add", "A"), ("remove", "B")])
        self.assertEqual(ops[0]["item_key"], "PARENT")

    def test_idempotent_once_applied(self):
        indexed = {"A", "C"}
        before = [_att("A"), _att("B", tagged=True), _att("C", tagged=True)]
        self.assertTrue(plan_tag_ops(before, indexed))
        after = [_att("A", tagged=True), _att("B"), _att("C", tagged=True)]
        self.assertEqual(plan_tag_ops(after, indexed), [])


class FakeWebAPI:
    def __init__(self, pages):
        self.pages = pages

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def iter_attachment_pages(self, library_id, library_type, page_size=100):
        for page in self.pages:
            yield page


class IndexedTagSyncTest(unittest.TestCase):
    def _run(self, pages, indexed, event_log=None, failing=None):
        records = []
        store = MagicMock()
        store.get_indexed_attachment_keys.side_effect = lambda lib, keys: {k for k in keys if k in indexed}
        log = event_log or MagicMock(last_seq=MagicMock(return_value=7))

        def factory(slug):
            if slug == failing:
                raise RuntimeError("boom")
            return FakeWebAPI(pages)

        sync = IndexedTagSync(store, log, records.append, factory)
        totals = asyncio.run(sync.run(["users/1", "groups/2"]))
        return records, totals, store

    def test_emits_ops_with_as_of_seq_and_progress(self):
        records, totals, store = self._run([[_att("A"), _att("B", tagged=True)]], {"A"})
        ops = [r for r in records if r["type"] == "ops"]
        self.assertEqual(len(ops), 2)  # one per library
        self.assertEqual(ops[0]["as_of_seq"], 7)
        self.assertEqual(totals["to_add"], 2)
        self.assertEqual(totals["to_remove"], 2)
        # Backend ids, not slugs, reach the vector store.
        self.assertEqual([c.args[0] for c in store.get_indexed_attachment_keys.call_args_list], ["u1", "2"])

    def test_second_run_over_converged_data_emits_no_ops(self):
        records, totals, _ = self._run([[_att("A", tagged=True), _att("B")]], {"A"})
        self.assertFalse([r for r in records if r["type"] == "ops"])
        self.assertEqual(totals["to_add"] + totals["to_remove"], 0)
        self.assertEqual(totals["already_correct"], 4)

    def test_indexed_state_read_fresh_per_page(self):
        indexed = {"A"}
        store = MagicMock()
        calls = []

        def fetch(lib, keys):
            calls.append(list(keys))
            if len(calls) == 1:
                indexed.add("B")  # becomes indexed while page 1 is being processed
            return {k for k in keys if k in indexed}

        store.get_indexed_attachment_keys.side_effect = fetch
        records = []
        sync = IndexedTagSync(store, MagicMock(last_seq=MagicMock(return_value=1)), records.append,
                              lambda s: FakeWebAPI([[_att("A")], [_att("B")]]))
        asyncio.run(sync.run(["users/1"]))
        self.assertEqual(calls, [["A"], ["B"]])
        added = [o["attachment_key"] for r in records if r["type"] == "ops" for o in r["ops"]]
        self.assertEqual(added, ["A", "B"])

    def test_failing_library_is_reported_and_others_continue(self):
        records, totals, _ = self._run([[_att("A")]], {"A"}, failing="users/1")
        self.assertEqual([r["library"] for r in records if r["type"] == "library_error"], ["users/1"])
        self.assertEqual(totals["libraries_failed"], 1)
        self.assertEqual(totals["to_add"], 1)


class LibraryEnumerationTest(unittest.TestCase):
    """The script must list the same libraries as the cron indexer's resolver."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        reset_settings()
        s = get_settings()
        s.data_path = Path(self.tmp)
        self.store = AutoIndexKeyStore(Path(self.tmp) / "keys.json", Fernet.generate_key().decode())
        self.fp_a = self.store.add("KEY-A", KeyValidation(1, "a", ["users/1", "groups/10"], read_only=True))
        self.fp_b = self.store.add("KEY-B", KeyValidation(2, "b", ["users/2"], read_only=True))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        reset_settings()

    def test_only_fingerprint_validates_just_that_key(self):
        validate = AsyncMock(return_value=KeyValidation(1, "a", ["users/1", "groups/10"], read_only=True))
        with patch("backend.services.autoindex_resolver.validate_key", new=validate):
            targets, _ = asyncio.run(resolve_targets(self.store, only_fingerprint=self.fp_a, require_embedding_key=False))
        self.assertEqual(sorted(targets), ["groups/10", "users/1"])
        self.assertEqual(validate.await_count, 1)
        self.assertTrue(all(t["fingerprint"] == self.fp_a for t in targets.values()))

    def test_script_resolves_slugs_via_resolver_and_filters_to_fingerprint(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location("sync_indexed_tags", Path(__file__).resolve().parents[2] / "bin" / "sync_indexed_tags.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        settings = get_settings()
        settings.autoindex_keys_path = Path(self.tmp) / "keys.json"
        args = mod._parse_args(["--fingerprint", self.fp_a])
        fake = {"users/1": {"zotero_key": "KEY-A", "fingerprint": self.fp_a}, "groups/10": {"zotero_key": "KEY-A", "fingerprint": self.fp_a}}
        with patch("backend.services.autoindex_resolver.resolve_targets", new=AsyncMock(return_value=(fake, []))) as rt, \
             patch("backend.services.autoindex_key_store.AutoIndexKeyStore") as store_cls:
            store_cls.return_value.enabled = True
            slugs, keys = asyncio.run(mod._resolve_slugs(args, settings))
        self.assertEqual(slugs, ["groups/10", "users/1"])
        self.assertEqual(keys["users/1"], "KEY-A")
        self.assertEqual(rt.await_args.kwargs, {"only_fingerprint": self.fp_a, "require_embedding_key": False})

    def test_script_api_key_mode_uses_key_validator_targets(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location("sync_indexed_tags", Path(__file__).resolve().parents[2] / "bin" / "sync_indexed_tags.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        args = mod._parse_args(["--api-key", "KEY-A"])
        with patch("backend.zotero.key_validator.validate_key", new=AsyncMock(return_value=KeyValidation(1, "a", ["users/1"], read_only=True))):
            slugs, _ = asyncio.run(mod._resolve_slugs(args, get_settings()))
        self.assertEqual(slugs, ["users/1"])
        with patch("backend.zotero.key_validator.validate_key", new=AsyncMock(return_value=KeyValidation(1, "a", [], read_only=False, reason="write"))):
            with self.assertRaises(RuntimeError):
                asyncio.run(mod._resolve_slugs(args, get_settings()))


class ScriptMainTest(unittest.TestCase):
    """bin/sync_indexed_tags.py end to end with the network and Qdrant stubbed."""

    def setUp(self):
        import importlib.util

        self.tmp = tempfile.mkdtemp()
        reset_settings()
        get_settings().data_path = Path(self.tmp)
        spec = importlib.util.spec_from_file_location(
            "sync_indexed_tags", Path(__file__).resolve().parents[2] / "bin" / "sync_indexed_tags.py")
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)
        self.out = Path(self.tmp) / "out.jsonl"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        reset_settings()

    def _run(self, tagged):
        store = MagicMock()
        store.get_indexed_attachment_keys.side_effect = lambda lib, keys: {"A"} & set(keys)
        fake_api = lambda api_key: FakeWebAPI([[_att("A", tagged=tagged), _att("B")]])  # noqa: E731
        with patch.object(self.mod, "_resolve_slugs", new=AsyncMock(return_value=(["users/1"], {"users/1": "K"}))), \
             patch("backend.dependencies.make_vector_store", return_value=store), \
             patch("backend.zotero.web_api.ZoteroWebAPI", side_effect=fake_api):
            code = asyncio.run(self.mod._main(["--fingerprint", "fp", "--output-file", str(self.out), "--run-id", "r1"]))
        records = [json.loads(line) for line in self.out.read_text().splitlines()]
        self.out.unlink()
        return code, records, store

    def test_writes_ordered_records_and_is_idempotent(self):
        code, records, store = self._run(tagged=False)
        self.assertEqual(code, 0)
        self.assertEqual([r["type"] for r in records],
                         ["start", "libraries", "library_start", "ops", "progress", "library_done", "done"])
        self.assertEqual(records[0]["run_id"], "r1")
        self.assertEqual(records[0]["tag"], INDEXED_TAG_NAME)
        self.assertEqual(records[3]["ops"], [{"op": "add", "attachment_key": "A", "item_key": "PARENT"}])
        store.close.assert_called_once()
        code2, records2, _ = self._run(tagged=True)  # state after the plugin applied the plan
        self.assertEqual(code2, 0)
        self.assertNotIn("ops", [r["type"] for r in records2])
        self.assertEqual(records2[-1]["to_add"] + records2[-1]["to_remove"], 0)

    def test_fatal_error_is_a_terminal_record_with_nonzero_exit(self):
        with patch.object(self.mod, "_resolve_slugs", new=AsyncMock(side_effect=RuntimeError("no key"))):
            code = asyncio.run(self.mod._main(["--fingerprint", "fp", "--output-file", str(self.out)]))
        records = [json.loads(line) for line in self.out.read_text().splitlines()]
        self.assertEqual(code, 1)
        self.assertEqual([r["type"] for r in records], ["start", "error"])
        self.assertEqual(records[-1]["message"], "no key")

    def test_library_ids_filter_reports_inaccessible_libraries(self):
        with patch.object(self.mod, "_resolve_slugs", new=AsyncMock(return_value=(["users/1"], {"users/1": "K"}))), \
             patch("backend.dependencies.make_vector_store", return_value=MagicMock()), \
             patch("backend.zotero.web_api.ZoteroWebAPI", side_effect=lambda api_key: FakeWebAPI([])):
            asyncio.run(self.mod._main(["--fingerprint", "fp", "--output-file", str(self.out),
                                        "--library-ids", "users/1", "groups/999"]))
        records = [json.loads(line) for line in self.out.read_text().splitlines()]
        errors = [r for r in records if r["type"] == "library_error"]
        self.assertEqual([e["library"] for e in errors], ["groups/999"])
        self.assertEqual([r for r in records if r["type"] == "libraries"][0]["libraries"], ["users/1"])


class RunTailingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        reset_settings()
        get_settings().data_path = Path(self.tmp)
        self.dir = get_settings().indexed_tag_runs_path / "fp"
        self.dir.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        reset_settings()

    def test_reads_by_offset_and_waits_for_complete_lines(self):
        p = self.dir / ("a" * 32 + ".jsonl")
        p.write_text(json.dumps({"type": "start", "pid": 1, "pid_create_time": None}) + "\n" + '{"type": "pro')
        with patch("backend.services.indexed_tag_runs.is_process_alive", return_value=True):
            first = read_run(get_settings(), "fp", "a" * 32, 0)
            self.assertEqual([r["type"] for r in first["records"]], ["start"])
            self.assertFalse(first["done"])
            with open(p, "a") as f:
                f.write('gress"}\n' + json.dumps({"type": "done"}) + "\n")
            second = read_run(get_settings(), "fp", "a" * 32, first["offset"])
        self.assertEqual([r["type"] for r in second["records"]], ["progress", "done"])
        self.assertTrue(second["done"])

    def test_dead_process_without_terminal_record_is_failed(self):
        p = self.dir / ("b" * 32 + ".jsonl")
        p.write_text(json.dumps({"type": "start", "pid": 999999, "pid_create_time": 1.0}) + "\n")
        out = read_run(get_settings(), "fp", "b" * 32, 0)
        self.assertEqual(out["records"][-1]["type"], "error")
        self.assertTrue(out["done"])

    def test_unknown_run_and_other_users_dir(self):
        self.assertIsNone(read_run(get_settings(), "fp", "c" * 32, 0))
        (self.dir / ("d" * 32 + ".jsonl")).write_text("")
        self.assertIsNone(read_run(get_settings(), "other-fp", "d" * 32, 0))


class IndexedTagsApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        reset_settings()
        s = get_settings()
        s.data_path = Path(self.tmp)
        s.autoindex_secret = Fernet.generate_key().decode()
        s.autoindex_keys_path = Path(self.tmp) / "keys.json"
        app.state.vector_store = MagicMock()
        self.client = TestClient(app)

    def tearDown(self):
        del app.state.vector_store
        shutil.rmtree(self.tmp, ignore_errors=True)
        reset_settings()

    def test_events_head_then_follow(self):
        log = IndexEventLog(get_settings().index_events_path)
        log.append([{"type": "indexed", "library_id": "1", "item_key": "I", "attachment_key": "A"}])
        head = self.client.get("/api/indexed-tags/events").json()
        self.assertEqual((head["events"], head["last_seq"], head["tag"]), ([], 1, INDEXED_TAG_NAME))
        log.append([{"type": "unindexed", "library_id": "1", "item_key": "I", "attachment_key": "A"}])
        out = self.client.get("/api/indexed-tags/events", params={"since": 1}).json()
        self.assertEqual([e["type"] for e in out["events"]], ["unindexed"])

    def test_events_filtered_to_callers_libraries(self):
        from backend.dependencies import get_zotero_identity
        from backend.services.zotero_identity import ZoteroIdentity

        IndexEventLog(get_settings().index_events_path).append([
            {"type": "indexed", "library_id": "1", "attachment_key": "A"},
            {"type": "indexed", "library_id": "999", "attachment_key": "SECRET"},
        ])
        app.dependency_overrides[get_zotero_identity] = lambda: ZoteroIdentity(user_id=5, username="u", targets=["groups/1"])
        try:
            out = self.client.get("/api/indexed-tags/events", params={"since": 0}).json()
        finally:
            app.dependency_overrides.clear()
        self.assertEqual([e["attachment_key"] for e in out["events"]], ["A"])

    def test_check_reads_ground_truth_and_enforces_library_access(self):
        from backend.dependencies import get_zotero_identity
        from backend.services.zotero_identity import ZoteroIdentity

        app.state.vector_store.get_indexed_attachment_keys.return_value = {"A"}
        r = self.client.post("/api/indexed-tags/check", json={"library_id": "1", "attachment_keys": ["A", "B"]})
        self.assertEqual(r.json(), {"indexed": ["A"]})
        app.state.vector_store.get_indexed_attachment_keys.assert_called_with("1", ["A", "B"])
        app.dependency_overrides[get_zotero_identity] = lambda: ZoteroIdentity(user_id=5, username="u", targets=["groups/1"])
        try:
            denied = self.client.post("/api/indexed-tags/check", json={"library_id": "2", "attachment_keys": ["A"]})
        finally:
            app.dependency_overrides.clear()
        self.assertEqual(denied.status_code, 403)

    def test_refresh_requires_registered_key(self):
        r = self.client.post("/api/indexed-tags/refresh", headers={"X-Zotero-API-Key": "UNREGISTERED"})
        self.assertEqual(r.status_code, 400)

    def test_refresh_spawns_for_callers_fingerprint_and_reuses_active_run(self):
        store = AutoIndexKeyStore(get_settings().autoindex_keys_path, get_settings().autoindex_secret)
        store.add("MY-KEY", KeyValidation(1, "me", ["users/1"], read_only=True))
        fp = fingerprint("MY-KEY")
        spawn = AsyncMock()
        with patch("backend.services.indexed_tag_runs.asyncio.create_subprocess_exec", new=spawn):
            first = self.client.post("/api/indexed-tags/refresh", headers={"X-Zotero-API-Key": "MY-KEY"}).json()
            second = self.client.post("/api/indexed-tags/refresh", headers={"X-Zotero-API-Key": "MY-KEY"}).json()
        self.assertFalse(first["already_running"])
        self.assertTrue(second["already_running"])
        self.assertEqual(second["run_id"], first["run_id"])
        self.assertEqual(spawn.await_count, 1)
        argv = spawn.await_args.args
        self.assertIn(fp, argv)
        self.assertNotIn("MY-KEY", argv)  # key never reaches the command line

    def test_refresh_tail_rejects_bad_ids_and_foreign_runs(self):
        h = {"X-Zotero-API-Key": "K"}
        self.assertEqual(self.client.get("/api/indexed-tags/refresh/../../etc", headers=h).status_code, 404)
        self.assertEqual(self.client.get("/api/indexed-tags/refresh/nothex", headers=h).status_code, 400)
        self.assertEqual(self.client.get("/api/indexed-tags/refresh/" + "e" * 32, headers=h).status_code, 404)


if __name__ == "__main__":
    unittest.main()
