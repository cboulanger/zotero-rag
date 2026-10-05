"""Unit tests for bin/migrate_library.py."""

import importlib.util
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "bin" / "migrate_library.py"
_SPEC = importlib.util.spec_from_file_location("migrate_library_script", _SCRIPT_PATH)
migrate_library = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(migrate_library)


class FakeResponse:
    def __init__(self, status_code, json_body):
        self.status_code = status_code
        self._json_body = json_body
        self.text = str(json_body)

    def json(self):
        return self._json_body


class FakeClient:
    """Minimal stand-in for httpx.Client. `responses` is a list of (status_code,
    json_body) tuples or Exception instances, consumed in order, one per
    .request() call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def request(self, method, url, headers=None, params=None, json=None, timeout=None):
        self.calls.append((method, url, params, json))
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        status_code, body = item
        return FakeResponse(status_code, body)


class ParseArgsTest(unittest.TestCase):
    def test_required_positional_args(self):
        args = migrate_library._parse_args(["users/39226", "http://source", "http://dest"])
        self.assertEqual(args.slug, "users/39226")
        self.assertEqual(args.source_url, "http://source")
        self.assertEqual(args.dest_url, "http://dest")
        self.assertEqual(args.batch_size, 200)
        self.assertFalse(args.dry_run)
        self.assertIsNone(args.source_key)

    def test_optional_flags(self):
        args = migrate_library._parse_args([
            "groups/6297749", "http://source", "http://dest",
            "--source-key", "SK", "--dest-key", "DK", "--batch-size", "50", "--dry-run",
        ])
        self.assertEqual(args.source_key, "SK")
        self.assertEqual(args.dest_key, "DK")
        self.assertEqual(args.batch_size, 50)
        self.assertTrue(args.dry_run)


class RequestRetryTest(unittest.TestCase):
    def test_retries_on_connect_error_then_succeeds(self):
        client = FakeClient([httpx.ConnectError("boom"), (200, {"ok": True})])
        with patch.object(migrate_library.time, "sleep"):
            result = migrate_library._get(client, "http://source", "/x", "KEY")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(client.calls), 2)

    def test_raises_migration_error_after_max_attempts(self):
        client = FakeClient([httpx.ConnectError("boom")] * migrate_library._MAX_ATTEMPTS)
        with patch.object(migrate_library.time, "sleep"):
            with self.assertRaises(migrate_library.MigrationError):
                migrate_library._get(client, "http://source", "/x", "KEY")

    def test_raises_migration_error_on_non_200(self):
        client = FakeClient([(403, {"detail": "nope"})])
        with self.assertRaises(migrate_library.MigrationError):
            migrate_library._get(client, "http://source", "/x", "KEY")

    def test_retries_on_500_then_succeeds(self):
        client = FakeClient([(500, {"detail": "transient qdrant error"}), (200, {"ok": True})])
        with patch.object(migrate_library.time, "sleep"):
            result = migrate_library._get(client, "http://source", "/x", "KEY")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(client.calls), 2)

    def test_raises_migration_error_after_max_attempts_on_500(self):
        client = FakeClient([(500, {"detail": "boom"})] * migrate_library._MAX_ATTEMPTS)
        with patch.object(migrate_library.time, "sleep"):
            with self.assertRaises(migrate_library.MigrationError):
                migrate_library._get(client, "http://source", "/x", "KEY")
        self.assertEqual(len(client.calls), migrate_library._MAX_ATTEMPTS)

    def test_does_not_retry_on_400(self):
        client = FakeClient([(400, {"detail": "bad request"})])
        with self.assertRaises(migrate_library.MigrationError):
            migrate_library._get(client, "http://source", "/x", "KEY")
        self.assertEqual(len(client.calls), 1)

    def test_does_not_retry_on_403(self):
        client = FakeClient([(403, {"detail": "forbidden"})])
        with self.assertRaises(migrate_library.MigrationError):
            migrate_library._get(client, "http://source", "/x", "KEY")
        self.assertEqual(len(client.calls), 1)

    def test_does_not_retry_on_404(self):
        client = FakeClient([(404, {"detail": "not found"})])
        with self.assertRaises(migrate_library.MigrationError):
            migrate_library._get(client, "http://source", "/x", "KEY")
        self.assertEqual(len(client.calls), 1)

    def test_get_omits_none_params(self):
        client = FakeClient([(200, {"ok": True})])
        migrate_library._get(client, "http://source", "/x", "KEY", offset=None, limit=5)
        _, _, params, _ = client.calls[0]
        self.assertEqual(params, {"limit": 5})

    def test_post_sends_json_body_and_query_params(self):
        client = FakeClient([(200, {"ok": True})])
        migrate_library._post(client, "http://dest", "/x", "KEY", {"a": 1}, collection="chunks")
        method, url, params, json_body = client.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(params, {"collection": "chunks"})
        self.assertEqual(json_body, {"a": 1})


class RunMigrationTest(unittest.TestCase):
    def setUp(self):
        self.embedding_info = {"embedding_model_name": "m", "embedding_dim": 8}
        self.metadata = {
            "library_id": "u39226", "library_type": "user", "library_name": "Mine",
            "last_indexed_version": 1, "total_items_indexed": 2, "total_chunks": 3,
        }

    def test_invalid_mode_raises_before_any_network_call(self):
        with patch.object(migrate_library, "_get") as mock_get, \
             patch.object(migrate_library, "_post") as mock_post:
            with self.assertRaises(migrate_library.MigrationError):
                migrate_library.run_migration(
                    client=object(), slug="users/39226",
                    source_url="http://source", dest_url="http://dest",
                    source_key="SK", dest_key="DK", mode="bogus",
                )
        mock_get.assert_not_called()
        mock_post.assert_not_called()

    def test_embedding_mismatch_raises_before_any_write(self):
        def fake_get(client, base_url, path, api_key, **params):
            if base_url == "http://source":
                return {"embedding_model_name": "m", "embedding_dim": 8}
            return {"embedding_model_name": "m", "embedding_dim": 16}

        with patch.object(migrate_library, "_get", side_effect=fake_get), \
             patch.object(migrate_library, "_post") as mock_post:
            with self.assertRaises(migrate_library.MigrationError):
                migrate_library.run_migration(
                    client=object(), slug="users/39226",
                    source_url="http://source", dest_url="http://dest",
                    source_key="SK", dest_key="DK",
                )
        mock_post.assert_not_called()

    def test_dry_run_returns_without_posting(self):
        def fake_get(client, base_url, path, api_key, **params):
            if path == "/api/migration/embedding-info":
                return self.embedding_info
            return self.metadata

        with patch.object(migrate_library, "_get", side_effect=fake_get), \
             patch.object(migrate_library, "_post") as mock_post:
            result = migrate_library.run_migration(
                client=object(), slug="users/39226",
                source_url="http://source", dest_url="http://dest",
                source_key="SK", dest_key="DK", dry_run=True,
            )
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["library_id"], "u39226")
        mock_post.assert_not_called()

    def test_full_run_paginates_and_calls_import_begin_and_metadata(self):
        chunk_pages = [
            {"points": [{"id": "1", "vector": [0.1] * 8, "payload": {}}], "next_offset": "cursor-1"},
            {"points": [{"id": "2", "vector": [0.2] * 8, "payload": {}}], "next_offset": None},
        ]
        dedup_pages = [{"points": [], "next_offset": None}]
        get_calls = []

        def fake_get(client, base_url, path, api_key, **params):
            get_calls.append((path, params))
            if path == "/api/migration/embedding-info":
                return self.embedding_info
            if path == "/api/migration/export/metadata":
                return self.metadata
            if path == "/api/migration/export/count" and params["collection"] == "chunks":
                return {"count": 2}
            if path == "/api/migration/export/count" and params["collection"] == "dedup":
                return {"count": 0}
            if path == "/api/migration/export" and params["collection"] == "chunks":
                return chunk_pages.pop(0)
            if path == "/api/migration/export" and params["collection"] == "dedup":
                return dedup_pages.pop(0)
            raise AssertionError(f"unexpected GET {path} {params}")

        post_calls = []

        def fake_post(client, base_url, path, api_key, body, **params):
            post_calls.append((path, params, body))
            if path == "/api/migration/import/begin":
                return {"library_id": "u39226", "chunks_deleted": 0, "dedup_deleted": 0, "metadata_deleted": False}
            return {}

        with TemporaryDirectory() as tmp:
            with patch.object(migrate_library, "_get", side_effect=fake_get), \
                 patch.object(migrate_library, "_post", side_effect=fake_post):
                result = migrate_library.run_migration(
                    client=object(), slug="users/39226",
                    source_url="http://source", dest_url="http://dest",
                    source_key="SK", dest_key="DK", batch_size=1,
                    data_path=Path(tmp),
                )

        self.assertEqual(result["transferred"], {"chunks": 2, "dedup": 0})

        import_paths = [p for p, _, _ in post_calls]
        self.assertEqual(
            import_paths,
            [
                "/api/migration/import/begin",
                "/api/migration/import",
                "/api/migration/import",
                "/api/migration/import/metadata",
            ],
        )

        chunk_offsets = [
            params["offset"] for path, params in get_calls
            if path == "/api/migration/export" and params["collection"] == "chunks"
        ]
        self.assertEqual(chunk_offsets, [None, "cursor-1"])

        count_calls = [params["collection"] for path, params in get_calls if path == "/api/migration/export/count"]
        self.assertEqual(count_calls, ["chunks", "dedup"])

    def test_export_count_called_once_per_collection_before_pagination(self):
        chunk_pages = [{"points": [], "next_offset": None}]
        dedup_pages = [{"points": [], "next_offset": None}]
        get_calls = []

        def fake_get(client, base_url, path, api_key, **params):
            get_calls.append((path, params))
            if path == "/api/migration/embedding-info":
                return self.embedding_info
            if path == "/api/migration/export/metadata":
                return self.metadata
            if path == "/api/migration/export/count" and params["collection"] == "chunks":
                return {"count": 0}
            if path == "/api/migration/export/count" and params["collection"] == "dedup":
                return {"count": 0}
            if path == "/api/migration/export" and params["collection"] == "chunks":
                return chunk_pages.pop(0)
            if path == "/api/migration/export" and params["collection"] == "dedup":
                return dedup_pages.pop(0)
            raise AssertionError(f"unexpected GET {path} {params}")

        def fake_post(client, base_url, path, api_key, body, **params):
            if path == "/api/migration/import/begin":
                return {"library_id": "u39226", "chunks_deleted": 0, "dedup_deleted": 0, "metadata_deleted": False}
            return {}

        with TemporaryDirectory() as tmp:
            with patch.object(migrate_library, "_get", side_effect=fake_get), \
                 patch.object(migrate_library, "_post", side_effect=fake_post):
                migrate_library.run_migration(
                    client=object(), slug="users/39226",
                    source_url="http://source", dest_url="http://dest",
                    source_key="SK", dest_key="DK", batch_size=1,
                    data_path=Path(tmp),
                )

        count_call_indices = [
            i for i, (path, _) in enumerate(get_calls) if path == "/api/migration/export/count"
        ]
        export_call_indices = [
            i for i, (path, _) in enumerate(get_calls) if path == "/api/migration/export"
        ]
        self.assertEqual(len(count_call_indices), 2)
        # Each collection's count call happens before its own export call.
        self.assertLess(count_call_indices[0], export_call_indices[0])

    def test_clean_mode_ignores_existing_state_and_clears_destination(self):
        with TemporaryDirectory() as tmp:
            data_path = Path(tmp)
            path = migrate_library.state_path("users/39226", "http://source", "http://dest", data_path)
            stale_state = migrate_library.new_state(
                "users/39226", "http://source", "http://dest", "u39226", "m", 8, "2026-01-01T00:00:00Z",
            )
            stale_state["begin_done"] = True
            stale_state["collections"]["chunks"] = {"cursor": "stale-cursor", "transferred": 999, "done": False}
            migrate_library.save_state(path, stale_state)

            chunk_pages = [{"points": [], "next_offset": None}]
            dedup_pages = [{"points": [], "next_offset": None}]

            def fake_get(client, base_url, path_, api_key, **params):
                if path_ == "/api/migration/embedding-info":
                    return self.embedding_info
                if path_ == "/api/migration/export/metadata":
                    return self.metadata
                if path_ == "/api/migration/export/count":
                    return {"count": 0}
                if path_ == "/api/migration/export" and params["collection"] == "chunks":
                    self.assertIsNone(params["offset"])
                    return chunk_pages.pop(0)
                if path_ == "/api/migration/export" and params["collection"] == "dedup":
                    return dedup_pages.pop(0)
                raise AssertionError(f"unexpected GET {path_} {params}")

            post_calls = []

            def fake_post(client, base_url, path_, api_key, body, **params):
                post_calls.append(path_)
                if path_ == "/api/migration/import/begin":
                    return {"library_id": "u39226", "chunks_deleted": 0, "dedup_deleted": 0, "metadata_deleted": False}
                return {}

            with patch.object(migrate_library, "_get", side_effect=fake_get), \
                 patch.object(migrate_library, "_post", side_effect=fake_post):
                result = migrate_library.run_migration(
                    client=object(), slug="users/39226",
                    source_url="http://source", dest_url="http://dest",
                    source_key="SK", dest_key="DK", batch_size=1,
                    mode="clean", data_path=data_path,
                )

        self.assertIn("/api/migration/import/begin", post_calls)
        self.assertIsNotNone(result["begin_result"])

    def test_resume_mode_skips_import_begin_and_continues_from_cursor(self):
        with TemporaryDirectory() as tmp:
            data_path = Path(tmp)
            path = migrate_library.state_path("users/39226", "http://source", "http://dest", data_path)
            state = migrate_library.new_state(
                "users/39226", "http://source", "http://dest", "u39226", "m", 8, "2026-01-01T00:00:00Z",
            )
            state["begin_done"] = True
            state["collections"]["chunks"] = {"cursor": "cursor-1", "transferred": 1, "done": False}
            state["collections"]["dedup"] = {"cursor": None, "transferred": 0, "done": True}
            migrate_library.save_state(path, state)

            def fake_get(client, base_url, path_, api_key, **params):
                if path_ == "/api/migration/embedding-info":
                    return self.embedding_info
                if path_ == "/api/migration/export/metadata":
                    return self.metadata
                if path_ == "/api/migration/export/count":
                    return {"count": 2}
                if path_ == "/api/migration/export" and params["collection"] == "chunks":
                    self.assertEqual(params["offset"], "cursor-1")
                    return {"points": [{"id": "2", "vector": [0.2] * 8, "payload": {}}], "next_offset": None}
                raise AssertionError(f"unexpected GET {path_} {params}")

            post_calls = []

            def fake_post(client, base_url, path_, api_key, body, **params):
                post_calls.append(path_)
                return {}

            with patch.object(migrate_library, "_get", side_effect=fake_get), \
                 patch.object(migrate_library, "_post", side_effect=fake_post):
                result = migrate_library.run_migration(
                    client=object(), slug="users/39226",
                    source_url="http://source", dest_url="http://dest",
                    source_key="SK", dest_key="DK", batch_size=1,
                    mode="resume", data_path=data_path,
                )

        self.assertNotIn("/api/migration/import/begin", post_calls)
        self.assertEqual(result["transferred"], {"chunks": 2, "dedup": 0})
        self.assertIsNone(result["begin_result"])

    def test_resume_mode_without_existing_state_raises(self):
        def fake_get(client, base_url, path, api_key, **params):
            if path == "/api/migration/embedding-info":
                return self.embedding_info
            return self.metadata

        with TemporaryDirectory() as tmp:
            with patch.object(migrate_library, "_get", side_effect=fake_get), \
                 patch.object(migrate_library, "_post") as mock_post:
                with self.assertRaises(migrate_library.MigrationError):
                    migrate_library.run_migration(
                        client=object(), slug="users/39226",
                        source_url="http://source", dest_url="http://dest",
                        source_key="SK", dest_key="DK",
                        mode="resume", data_path=Path(tmp),
                    )
        mock_post.assert_not_called()

    def test_resume_mode_with_embedding_mismatch_against_state_raises(self):
        with TemporaryDirectory() as tmp:
            data_path = Path(tmp)
            path = migrate_library.state_path("users/39226", "http://source", "http://dest", data_path)
            stale_state = migrate_library.new_state(
                "users/39226", "http://source", "http://dest", "u39226", "old-model", 16, "2026-01-01T00:00:00Z",
            )
            migrate_library.save_state(path, stale_state)

            def fake_get(client, base_url, path_, api_key, **params):
                if path_ == "/api/migration/embedding-info":
                    return self.embedding_info
                return self.metadata

            with patch.object(migrate_library, "_get", side_effect=fake_get), \
                 patch.object(migrate_library, "_post") as mock_post:
                with self.assertRaises(migrate_library.MigrationError):
                    migrate_library.run_migration(
                        client=object(), slug="users/39226",
                        source_url="http://source", dest_url="http://dest",
                        source_key="SK", dest_key="DK",
                        mode="resume", data_path=data_path,
                    )
        mock_post.assert_not_called()

    def test_successful_run_deletes_state_file(self):
        chunk_pages = [{"points": [], "next_offset": None}]
        dedup_pages = [{"points": [], "next_offset": None}]

        def fake_get(client, base_url, path, api_key, **params):
            if path == "/api/migration/embedding-info":
                return self.embedding_info
            if path == "/api/migration/export/metadata":
                return self.metadata
            if path == "/api/migration/export/count":
                return {"count": 0}
            if path == "/api/migration/export" and params["collection"] == "chunks":
                return chunk_pages.pop(0)
            if path == "/api/migration/export" and params["collection"] == "dedup":
                return dedup_pages.pop(0)
            raise AssertionError(f"unexpected GET {path} {params}")

        def fake_post(client, base_url, path, api_key, body, **params):
            if path == "/api/migration/import/begin":
                return {"library_id": "u39226", "chunks_deleted": 0, "dedup_deleted": 0, "metadata_deleted": False}
            return {}

        with TemporaryDirectory() as tmp:
            data_path = Path(tmp)
            with patch.object(migrate_library, "_get", side_effect=fake_get), \
                 patch.object(migrate_library, "_post", side_effect=fake_post):
                migrate_library.run_migration(
                    client=object(), slug="users/39226",
                    source_url="http://source", dest_url="http://dest",
                    source_key="SK", dest_key="DK", batch_size=1,
                    data_path=data_path,
                )
            path = migrate_library.state_path("users/39226", "http://source", "http://dest", data_path)
            self.assertFalse(path.exists())

    def test_batch_failure_persists_state_up_to_last_successful_batch(self):
        chunk_pages = [
            {"points": [{"id": "1", "vector": [0.1] * 8, "payload": {}}], "next_offset": "cursor-1"},
            {"points": [{"id": "2", "vector": [0.2] * 8, "payload": {}}], "next_offset": None},
        ]

        def fake_get(client, base_url, path, api_key, **params):
            if path == "/api/migration/embedding-info":
                return self.embedding_info
            if path == "/api/migration/export/metadata":
                return self.metadata
            if path == "/api/migration/export/count":
                return {"count": 2}
            if path == "/api/migration/export" and params["collection"] == "chunks":
                return chunk_pages.pop(0)
            raise AssertionError(f"unexpected GET {path} {params}")

        call_count = {"n": 0}

        def fake_post(client, base_url, path, api_key, body, **params):
            if path == "/api/migration/import/begin":
                return {"library_id": "u39226", "chunks_deleted": 0, "dedup_deleted": 0, "metadata_deleted": False}
            if path == "/api/migration/import":
                call_count["n"] += 1
                if call_count["n"] == 2:
                    raise migrate_library.MigrationError("simulated network failure")
                return {}
            return {}

        with TemporaryDirectory() as tmp:
            data_path = Path(tmp)
            with patch.object(migrate_library, "_get", side_effect=fake_get), \
                 patch.object(migrate_library, "_post", side_effect=fake_post):
                with self.assertRaises(migrate_library.MigrationError):
                    migrate_library.run_migration(
                        client=object(), slug="users/39226",
                        source_url="http://source", dest_url="http://dest",
                        source_key="SK", dest_key="DK", batch_size=1,
                        data_path=data_path,
                    )

            path = migrate_library.state_path("users/39226", "http://source", "http://dest", data_path)
            state = migrate_library.load_state(path)

        self.assertIsNotNone(state)
        self.assertTrue(state["begin_done"])
        self.assertEqual(state["collections"]["chunks"]["transferred"], 1)
        self.assertEqual(state["collections"]["chunks"]["cursor"], "cursor-1")
        self.assertFalse(state["collections"]["chunks"]["done"])


class MainErrorHandlingTest(unittest.TestCase):
    def test_invalid_slug_value_error_is_caught_cleanly(self):
        """A bare ValueError from slug_to_backend_id (e.g. an invalid <slug>)
        must be caught by main()'s except clause and converted into a clean
        exit(1), not left to propagate as an unhandled traceback."""
        argv = ["migrate_library.py", "invalid-slug", "http://source", "http://dest"]
        with patch.object(migrate_library.sys, "argv", argv), \
             patch.object(
                 migrate_library, "slug_to_backend_id",
                 side_effect=ValueError("Invalid library slug: 'invalid-slug'"),
             ):
            with self.assertRaises(SystemExit) as ctx:
                migrate_library.main()
        self.assertEqual(ctx.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
