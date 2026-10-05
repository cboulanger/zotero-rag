"""Unit tests for bin/migrate_library.py."""

import importlib.util
import unittest
from pathlib import Path
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

        with patch.object(migrate_library, "_get", side_effect=fake_get), \
             patch.object(migrate_library, "_post", side_effect=fake_post):
            result = migrate_library.run_migration(
                client=object(), slug="users/39226",
                source_url="http://source", dest_url="http://dest",
                source_key="SK", dest_key="DK", batch_size=1,
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


if __name__ == "__main__":
    unittest.main()
