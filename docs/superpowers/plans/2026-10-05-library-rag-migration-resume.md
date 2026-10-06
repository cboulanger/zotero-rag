# Library RAG Migration Resume-From-Cursor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `bin/migrate_library.py` resumable after an interruption (crash, network outage, kill), instead of requiring a full restart that re-clears the destination.

**Architecture:** A new pure-stdlib state module (`backend/utils/migration_state.py`) persists per-collection cursors/counts to a JSON file under `<data_path>/system/migration_state/`, keyed by a hash of `(slug, source_url, dest_url)`. `bin/migrate_library.py`'s `run_migration()` becomes mode-aware (`"clean"` default / `"resume"`), writing state after every successful batch and deleting it on full success. A new `--mode` CLI flag plus an interactive stdin prompt (when `--mode` is omitted and an incomplete run is found) resolve which mode to use before `run_migration()` is ever called — `run_migration()` itself stays non-interactive so it's fully testable.

**Tech Stack:** Python stdlib (`json`, `hashlib`, `os`, `pathlib`), `httpx` (already a dependency), `unittest` (project convention).

See [2026-10-05-library-rag-migration-resume-design.md](../specs/2026-10-05-library-rag-migration-resume-design.md) for the full design rationale.

---

### Task 1: State persistence module

**Files:**
- Create: `backend/utils/migration_state.py`
- Test: `backend/tests/test_migration_state.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Unit tests for backend/utils/migration_state.py."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from backend.utils.migration_state import (
    delete_state,
    load_state,
    new_state,
    save_state,
    state_path,
)


class StatePathTest(unittest.TestCase):
    def test_same_inputs_produce_same_path(self):
        p1 = state_path("users/1", "http://a", "http://b", Path("/data"))
        p2 = state_path("users/1", "http://a", "http://b", Path("/data"))
        self.assertEqual(p1, p2)

    def test_different_dest_url_produces_different_path(self):
        p1 = state_path("users/1", "http://a", "http://b", Path("/data"))
        p2 = state_path("users/1", "http://a", "http://c", Path("/data"))
        self.assertNotEqual(p1, p2)

    def test_path_is_under_data_path_system_migration_state(self):
        p = state_path("users/1", "http://a", "http://b", Path("/data"))
        self.assertEqual(p.parent, Path("/data/system/migration_state"))
        self.assertTrue(p.name.endswith(".json"))


class SaveLoadStateTest(unittest.TestCase):
    def test_round_trip(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            state = new_state(
                "users/1", "http://a", "http://b", "u1", "model", 8,
                "2026-01-01T00:00:00Z",
            )
            save_state(path, state)
            loaded = load_state(path)
        self.assertEqual(loaded, state)

    def test_atomic_write_leaves_no_tmp_file(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            save_state(path, {"a": 1})
            tmp_files = list(Path(tmp).glob("*.tmp"))
        self.assertEqual(tmp_files, [])

    def test_save_creates_parent_directories(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "dir" / "state.json"
            save_state(path, {"a": 1})
            self.assertTrue(path.exists())

    def test_load_missing_file_returns_none(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing.json"
            self.assertIsNone(load_state(path))

    def test_load_corrupt_file_returns_none(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("not json{{{")
            self.assertIsNone(load_state(path))


class DeleteStateTest(unittest.TestCase):
    def test_delete_removes_existing_file(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            save_state(path, {"a": 1})
            delete_state(path)
            self.assertFalse(path.exists())

    def test_delete_missing_file_is_noop(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing.json"
            delete_state(path)  # must not raise


class NewStateTest(unittest.TestCase):
    def test_new_state_shape(self):
        state = new_state(
            "users/1", "http://a", "http://b", "u1", "model", 8,
            "2026-01-01T00:00:00Z",
        )
        self.assertEqual(state["slug"], "users/1")
        self.assertEqual(state["source_url"], "http://a")
        self.assertEqual(state["dest_url"], "http://b")
        self.assertEqual(state["library_id"], "u1")
        self.assertEqual(state["embedding_model_name"], "model")
        self.assertEqual(state["embedding_dim"], 8)
        self.assertFalse(state["begin_done"])
        self.assertEqual(
            state["collections"]["chunks"],
            {"cursor": None, "transferred": 0, "done": False},
        )
        self.assertEqual(
            state["collections"]["dedup"],
            {"cursor": None, "transferred": 0, "done": False},
        )
        self.assertFalse(state["metadata_done"])
        self.assertEqual(state["started_at"], "2026-01-01T00:00:00Z")
        self.assertEqual(state["updated_at"], "2026-01-01T00:00:00Z")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest backend/tests/test_migration_state.py -v`
Expected: FAIL / ERROR — `ModuleNotFoundError: No module named 'backend.utils.migration_state'`

- [ ] **Step 3: Write the implementation**

```python
"""Resume-from-cursor state tracking for bin/migrate_library.py.

Pure stdlib, no Qdrant/VectorStore dependency, so it's testable in
isolation. See
docs/superpowers/specs/2026-10-05-library-rag-migration-resume-design.md.
"""

import hashlib
import json
import os
from pathlib import Path
from typing import Optional

MIGRATION_COLLECTIONS = ("chunks", "dedup")


def state_path(slug: str, source_url: str, dest_url: str, data_path: Path) -> Path:
    """Return the state file path for this (slug, source_url, dest_url) key."""
    key = hashlib.sha1(f"{slug}|{source_url}|{dest_url}".encode()).hexdigest()
    return data_path / "system" / "migration_state" / f"{key}.json"


def load_state(path: Path) -> Optional[dict]:
    """Return the parsed state dict, or None if missing or unparseable."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def save_state(path: Path, state: dict) -> None:
    """Atomically write state to path (write to a .tmp sibling, then replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(json.dumps(state, indent=2))
    os.replace(tmp_path, path)


def delete_state(path: Path) -> None:
    """Remove the state file if present; no-op if it doesn't exist."""
    path.unlink(missing_ok=True)


def new_state(
    slug: str,
    source_url: str,
    dest_url: str,
    library_id: str,
    embedding_model_name: str,
    embedding_dim: int,
    started_at: str,
) -> dict:
    """Build a fresh state dict for a new migration run."""
    return {
        "slug": slug,
        "source_url": source_url,
        "dest_url": dest_url,
        "library_id": library_id,
        "embedding_model_name": embedding_model_name,
        "embedding_dim": embedding_dim,
        "begin_done": False,
        "collections": {
            collection: {"cursor": None, "transferred": 0, "done": False}
            for collection in MIGRATION_COLLECTIONS
        },
        "metadata_done": False,
        "started_at": started_at,
        "updated_at": started_at,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest backend/tests/test_migration_state.py -v`
Expected: PASS (13 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/utils/migration_state.py backend/tests/test_migration_state.py
git commit -m "feat(migration): add resume-from-cursor state persistence module

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 2: Make `run_migration()` mode/state-aware

**Files:**
- Modify: `bin/migrate_library.py`
- Modify: `backend/tests/test_migrate_library.py`

This task changes `run_migration()`'s signature and body only. `--mode`
argparse wiring and the interactive prompt are Task 3 — `run_migration()`
always receives an already-resolved `mode` ("clean", the default, or
"resume"), so it stays non-interactive and fully unit-testable here.

- [ ] **Step 1: Write the failing tests**

Add these imports to the top of `backend/tests/test_migrate_library.py`
(keep the existing ones):

```python
from tempfile import TemporaryDirectory
```

Add these tests to the `RunMigrationTest` class (after the existing
`test_export_count_called_once_per_collection_before_pagination` method,
keeping it and all earlier tests in that class unchanged):

```python
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
```

Also update the two *existing* tests that run the full migration all the
way through (they now need an isolated `data_path` so they don't touch the
real project's `data/` directory):

In `test_full_run_paginates_and_calls_import_begin_and_metadata`, wrap the
existing body in a `with TemporaryDirectory() as tmp:` block and add
`data_path=Path(tmp)` to the `run_migration(...)` call:

```python
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
```

Apply the same change (wrap in `TemporaryDirectory`, add
`data_path=Path(tmp)`) to
`test_export_count_called_once_per_collection_before_pagination`:

```python
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
        self.assertLess(count_call_indices[0], export_call_indices[0])
```

Leave `test_embedding_mismatch_raises_before_any_write` and
`test_dry_run_returns_without_posting` exactly as they are — both
raise/return before `run_migration()` ever touches the state file, so they
need no `data_path`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest backend/tests/test_migrate_library.py -v`
Expected: FAIL — `TypeError: run_migration() got an unexpected keyword argument 'mode'` (and `'data_path'`)

- [ ] **Step 3: Write the implementation**

In `bin/migrate_library.py`, add this import after the existing
`from backend.api.public_query import slug_to_backend_id` line:

```python
from backend.utils.migration_state import (  # noqa: E402
    delete_state,
    load_state,
    new_state,
    save_state,
    state_path,
)
```

Add `from datetime import datetime, timezone` to the top-level imports
(alongside `import time`).

Replace the entire `run_migration` function with:

```python
def run_migration(
    client,
    slug: str,
    source_url: str,
    dest_url: str,
    source_key: Optional[str],
    dest_key: Optional[str],
    batch_size: int = 200,
    dry_run: bool = False,
    mode: str = "clean",
    data_path: Optional[Path] = None,
) -> dict:
    """Run the full migration (or, if dry_run, just the compatibility/size check).

    `mode` is "clean" (ignore/clear any prior state and start fresh, the
    default) or "resume" (continue a previously interrupted run for this
    exact slug/source/dest combination; raises MigrationError if no
    matching state file exists). The clean-vs-resume *prompt* shown when
    the caller hasn't decided yet lives in main(), not here — this
    function always receives an already-resolved mode so it stays
    non-interactive and testable.

    Returns a summary dict used by main() for its console output and by
    tests for assertions.
    """
    library_id = slug_to_backend_id(slug)

    source_info = _get(client, source_url, "/api/migration/embedding-info", source_key)
    dest_info = _get(client, dest_url, "/api/migration/embedding-info", dest_key)
    source_model = (source_info["embedding_model_name"], source_info["embedding_dim"])
    dest_model = (dest_info["embedding_model_name"], dest_info["embedding_dim"])
    if source_model != dest_model:
        raise MigrationError(
            f"Embedding mismatch: source uses {source_model[0]} ({source_model[1]}-dim), "
            f"destination uses {dest_model[0]} ({dest_model[1]}-dim). "
            "Re-index on the destination instead of migrating vectors between incompatible models."
        )

    metadata = _get(client, source_url, "/api/migration/export/metadata", source_key, library_id=library_id)

    if dry_run:
        return {"library_id": library_id, "dry_run": True, "metadata": metadata}

    if data_path is None:
        from backend.config.settings import get_settings
        data_path = get_settings().data_path
    path = state_path(slug, source_url, dest_url, data_path)

    if mode == "resume":
        state = load_state(path)
        if state is None:
            raise MigrationError(
                f"No incomplete migration found for {slug} ({source_url} -> {dest_url}); "
                "use --mode=clean or omit --mode to start fresh."
            )
        if (state["embedding_model_name"], state["embedding_dim"]) != source_model:
            raise MigrationError(
                "Embedding config changed since the interrupted run: recorded "
                f"{state['embedding_model_name']} ({state['embedding_dim']}-dim), "
                f"now {source_model[0]} ({source_model[1]}-dim). Use --mode=clean to start fresh."
            )
    else:
        delete_state(path)
        state = new_state(
            slug, source_url, dest_url, library_id,
            source_model[0], source_model[1],
            started_at=datetime.now(timezone.utc).isoformat(),
        )
        save_state(path, state)

    if not state["begin_done"]:
        begin_result = _post(client, dest_url, "/api/migration/import/begin", dest_key, {"library_id": library_id})
        state["begin_done"] = True
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        save_state(path, state)
    else:
        begin_result = None

    transferred = {}
    for collection in MIGRATION_COLLECTIONS:
        coll_state = state["collections"][collection]
        total = _get(
            client, source_url, "/api/migration/export/count", source_key,
            library_id=library_id, collection=collection,
        )["count"]
        count = coll_state["transferred"]
        offset = coll_state["cursor"]
        if coll_state["done"]:
            print(f"[OK] {collection}: {count}/{total} transferred (already complete)", file=sys.stderr)
            transferred[collection] = count
            continue
        while True:
            page = _get(
                client, source_url, "/api/migration/export", source_key,
                library_id=library_id, collection=collection, offset=offset, limit=batch_size,
            )
            points = page["points"]
            if points:
                _post(
                    client, dest_url, "/api/migration/import", dest_key,
                    {"library_id": library_id, "points": points}, collection=collection,
                )
                count += len(points)
            offset = page["next_offset"]
            coll_state["cursor"] = offset
            coll_state["transferred"] = count
            coll_state["done"] = offset is None
            state["updated_at"] = datetime.now(timezone.utc).isoformat()
            save_state(path, state)
            print(f"[..] {collection}: {count}/{total} transferred", end="\r", file=sys.stderr)
            if offset is None:
                break
        print(f"[OK] {collection}: {count}/{total} transferred", file=sys.stderr)
        transferred[collection] = count

    if not state["metadata_done"]:
        _post(client, dest_url, "/api/migration/import/metadata", dest_key, {"library_id": library_id, "payload": metadata})
        state["metadata_done"] = True
        save_state(path, state)

    delete_state(path)

    return {
        "library_id": library_id,
        "dry_run": False,
        "begin_result": begin_result,
        "transferred": transferred,
        "metadata": metadata,
    }
```

Note the batch-failure test relies on `save_state` having already
persisted the first successful batch *before* the second batch's `_post`
raises — this holds because `save_state(path, state)` is called
immediately after each batch's counters are updated, inside the `while`
loop, before the loop re-enters `_get`/`_post` for the next page.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest backend/tests/test_migrate_library.py -v`
Expected: PASS (all tests, old and new)

- [ ] **Step 5: Commit**

```bash
git add bin/migrate_library.py backend/tests/test_migrate_library.py
git commit -m "feat(migration): make run_migration mode/state-aware for resume support

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 3: `--mode` CLI flag, interactive prompt, and `main()` wiring

**Files:**
- Modify: `bin/migrate_library.py`
- Modify: `backend/tests/test_migrate_library.py`

- [ ] **Step 1: Write the failing tests**

Add `from io import StringIO` to the imports at the top of
`backend/tests/test_migrate_library.py`.

Add these new test classes at the end of the file, before the
`if __name__ == "__main__":` block:

```python
class ParseArgsModeTest(unittest.TestCase):
    def test_mode_defaults_to_none(self):
        args = migrate_library._parse_args(["users/39226", "http://source", "http://dest"])
        self.assertIsNone(args.mode)

    def test_mode_accepts_clean_and_resume(self):
        args = migrate_library._parse_args(["users/39226", "http://source", "http://dest", "--mode", "resume"])
        self.assertEqual(args.mode, "resume")
        args = migrate_library._parse_args(["users/39226", "http://source", "http://dest", "--mode", "clean"])
        self.assertEqual(args.mode, "clean")

    def test_mode_rejects_invalid_value(self):
        with self.assertRaises(SystemExit):
            migrate_library._parse_args(["users/39226", "http://source", "http://dest", "--mode", "bogus"])


class ResolveModeTest(unittest.TestCase):
    def _make_state_file(self, data_path):
        path = migrate_library.state_path("users/39226", "http://source", "http://dest", data_path)
        migrate_library.save_state(path, migrate_library.new_state(
            "users/39226", "http://source", "http://dest", "u39226", "m", 8, "2026-01-01T00:00:00Z",
        ))
        return path

    def test_explicit_mode_short_circuits_without_prompting(self):
        args = migrate_library._parse_args(["users/39226", "http://source", "http://dest", "--mode", "clean"])
        with TemporaryDirectory() as tmp:
            with patch("builtins.input") as mock_input:
                mode = migrate_library._resolve_mode(args, Path(tmp))
        self.assertEqual(mode, "clean")
        mock_input.assert_not_called()

    def test_no_state_file_resolves_to_clean_without_prompting(self):
        args = migrate_library._parse_args(["users/39226", "http://source", "http://dest"])
        with TemporaryDirectory() as tmp:
            with patch("builtins.input") as mock_input:
                mode = migrate_library._resolve_mode(args, Path(tmp))
        self.assertEqual(mode, "clean")
        mock_input.assert_not_called()

    def test_prompts_and_returns_resume_on_r(self):
        args = migrate_library._parse_args(["users/39226", "http://source", "http://dest"])
        with TemporaryDirectory() as tmp:
            data_path = Path(tmp)
            self._make_state_file(data_path)
            with patch("builtins.input", return_value="r"):
                mode = migrate_library._resolve_mode(args, data_path)
        self.assertEqual(mode, "resume")

    def test_prompts_and_returns_clean_on_c(self):
        args = migrate_library._parse_args(["users/39226", "http://source", "http://dest"])
        with TemporaryDirectory() as tmp:
            data_path = Path(tmp)
            self._make_state_file(data_path)
            with patch("builtins.input", return_value="c"):
                mode = migrate_library._resolve_mode(args, data_path)
        self.assertEqual(mode, "clean")

    def test_reprompts_on_invalid_answer_then_accepts_valid_one(self):
        args = migrate_library._parse_args(["users/39226", "http://source", "http://dest"])
        with TemporaryDirectory() as tmp:
            data_path = Path(tmp)
            self._make_state_file(data_path)
            with patch("builtins.input", side_effect=["bogus", "resume"]):
                mode = migrate_library._resolve_mode(args, data_path)
        self.assertEqual(mode, "resume")

    def test_abort_answer_exits_with_code_1(self):
        args = migrate_library._parse_args(["users/39226", "http://source", "http://dest"])
        with TemporaryDirectory() as tmp:
            data_path = Path(tmp)
            self._make_state_file(data_path)
            with patch("builtins.input", return_value="a"):
                with self.assertRaises(SystemExit) as ctx:
                    migrate_library._resolve_mode(args, data_path)
        self.assertEqual(ctx.exception.code, 1)

    def test_eof_on_prompt_exits_with_code_1(self):
        args = migrate_library._parse_args(["users/39226", "http://source", "http://dest"])
        with TemporaryDirectory() as tmp:
            data_path = Path(tmp)
            self._make_state_file(data_path)
            with patch("builtins.input", side_effect=EOFError):
                with self.assertRaises(SystemExit) as ctx:
                    migrate_library._resolve_mode(args, data_path)
        self.assertEqual(ctx.exception.code, 1)


class MainModeIntegrationTest(unittest.TestCase):
    def test_main_passes_resolved_mode_and_data_path_to_run_migration(self):
        argv = ["migrate_library.py", "users/39226", "http://source", "http://dest", "--mode", "clean"]
        fake_result = {
            "library_id": "u39226", "dry_run": False,
            "begin_result": {"chunks_deleted": 0, "dedup_deleted": 0, "metadata_deleted": False},
            "transferred": {"chunks": 0, "dedup": 0},
            "metadata": {"total_chunks": 0, "total_items_indexed": 0},
        }
        with patch.object(migrate_library.sys, "argv", argv), \
             patch.object(migrate_library, "run_migration", return_value=fake_result) as mock_run, \
             patch("backend.config.settings.get_settings") as mock_get_settings:
            mock_get_settings.return_value.data_path = Path("/fake/data")
            migrate_library.main()
        _, kwargs = mock_run.call_args
        self.assertEqual(kwargs["mode"], "clean")
        self.assertEqual(kwargs["data_path"], Path("/fake/data"))

    def test_main_prints_resumed_message_when_begin_result_is_none(self):
        argv = ["migrate_library.py", "users/39226", "http://source", "http://dest", "--mode", "resume"]
        fake_result = {
            "library_id": "u39226", "dry_run": False,
            "begin_result": None,
            "transferred": {"chunks": 1, "dedup": 0},
            "metadata": {"total_chunks": 1, "total_items_indexed": 1},
        }
        with patch.object(migrate_library.sys, "argv", argv), \
             patch.object(migrate_library, "run_migration", return_value=fake_result), \
             patch("backend.config.settings.get_settings") as mock_get_settings, \
             patch("sys.stdout", new_callable=StringIO) as mock_stdout:
            mock_get_settings.return_value.data_path = Path("/fake/data")
            migrate_library.main()
        self.assertIn("Resumed previous run", mock_stdout.getvalue())

    def test_main_dry_run_never_touches_settings_or_state(self):
        argv = ["migrate_library.py", "users/39226", "http://source", "http://dest", "--dry-run"]
        fake_result = {
            "library_id": "u39226", "dry_run": True,
            "metadata": {"total_chunks": 5, "total_items_indexed": 2},
        }
        with patch.object(migrate_library.sys, "argv", argv), \
             patch.object(migrate_library, "run_migration", return_value=fake_result) as mock_run, \
             patch("backend.config.settings.get_settings") as mock_get_settings:
            migrate_library.main()
        mock_get_settings.assert_not_called()
        _, kwargs = mock_run.call_args
        self.assertEqual(kwargs["mode"], "clean")
        self.assertIsNone(kwargs["data_path"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest backend/tests/test_migrate_library.py -v`
Expected: FAIL — `AttributeError: module 'migrate_library_script' has no attribute '_resolve_mode'` and `argparse` errors for `--mode` not being a recognized flag.

- [ ] **Step 3: Write the implementation**

In `bin/migrate_library.py`, update the module docstring (the whole
top-of-file docstring, lines 1–22) to:

```python
"""
Migrate one Zotero library's indexed RAG data (document_chunks,
deduplication records, and index metadata) from one zotero-rag backend
instance to another.

Usage:
    uv run python bin/migrate_library.py <slug> <source-url> <dest-url>
        --source-key <source-admin-zotero-api-key>
        --dest-key <dest-admin-zotero-api-key>
        [--batch-size 200] [--dry-run] [--mode clean|resume]

<slug> is a Zotero.org library slug, e.g. users/39226 or groups/6297749.
--source-key/--dest-key must belong to an account that is an owner/admin of
the respective instance's AUTHORIZED_GROUP_ID (omit for a loopback-mode
instance, which needs no admin key).

The destination's existing data for this library is fully overwritten on a
fresh run. If a previous run for the same (slug, source-url, dest-url) was
interrupted, a state file under <data_path>/system/migration_state/ lets a
later run resume instead of restarting: pass --mode=resume to continue, or
--mode=clean to discard the incomplete state and start over. Omitting
--mode prompts interactively when an incomplete run is found.

See docs/superpowers/specs/2026-10-05-library-rag-migration-design.md and
docs/superpowers/specs/2026-10-05-library-rag-migration-resume-design.md.
"""
```

Add `--mode` to `_parse_args`, right after the existing `--dry-run`
argument:

```python
    parser.add_argument(
        "--mode", choices=["clean", "resume"], default=None,
        help="Skip the interactive prompt for an incomplete prior run: "
             "'clean' discards it and starts fresh, 'resume' continues it.",
    )
```

Add a new `_resolve_mode` function, right before `main()`:

```python
def _resolve_mode(args: argparse.Namespace, data_path: Path) -> str:
    """Resolve the final clean-vs-resume mode for this run, prompting on
    stdin if --mode wasn't given and an incomplete migration is on record."""
    if args.mode is not None:
        return args.mode
    path = state_path(args.slug, args.source_url, args.dest_url, data_path)
    state = load_state(path)
    if state is None:
        return "clean"
    print(
        f"Incomplete migration found for {args.slug} ({args.source_url} -> {args.dest_url}):\n"
        f"  started {state['started_at']}, last updated {state['updated_at']}\n"
        f"  chunks: {state['collections']['chunks']['transferred']} transferred"
        f"{' (done)' if state['collections']['chunks']['done'] else ''}\n"
        f"  dedup: {state['collections']['dedup']['transferred']} transferred"
        f"{' (done)' if state['collections']['dedup']['done'] else ''}\n"
        f"  metadata: {'done' if state['metadata_done'] else 'pending'}",
        file=sys.stderr,
    )
    try:
        while True:
            answer = input("Resume, Clean, or Abort? [r/c/a]: ").strip().lower()
            if answer in ("r", "resume"):
                return "resume"
            if answer in ("c", "clean"):
                return "clean"
            if answer in ("a", "abort"):
                break
            print("Please answer r, c, or a.", file=sys.stderr)
    except (EOFError, KeyboardInterrupt):
        print(file=sys.stderr)
    print("[FAIL] Aborted by user.", file=sys.stderr)
    sys.exit(1)
```

Replace `main()` with:

```python
def main() -> None:
    args = _parse_args()
    start = time.monotonic()
    try:
        if args.dry_run:
            mode = "clean"
            data_path = None
        else:
            from backend.config.settings import get_settings
            data_path = get_settings().data_path
            mode = _resolve_mode(args, data_path)
        with httpx.Client() as client:
            result = run_migration(
                client, args.slug, args.source_url, args.dest_url,
                args.source_key, args.dest_key, args.batch_size, args.dry_run,
                mode=mode, data_path=data_path,
            )
    except (MigrationError, ValueError) as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        sys.exit(1)

    if result["dry_run"]:
        meta = result["metadata"]
        print(
            f"[OK] --dry-run: {args.slug} ({result['library_id']}) has {meta['total_chunks']} chunks, "
            f"{meta['total_items_indexed']} items on {args.source_url}. No data written."
        )
        return

    if result["begin_result"] is not None:
        print(
            f"[OK] Cleared destination: {result['begin_result']['chunks_deleted']} chunks, "
            f"{result['begin_result']['dedup_deleted']} dedup records."
        )
    else:
        print("[OK] Resumed previous run; destination was not re-cleared.")
    # Per-collection "N/total transferred" progress/completion lines are already
    # printed to stderr by run_migration as each collection finishes; avoid
    # reprinting the same counts here and just give the overall wrap-up.
    total_points = sum(result["transferred"].values())
    print(f"[OK] {total_points} points and metadata transferred. Done in {time.monotonic() - start:.1f}s")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest backend/tests/test_migrate_library.py -v`
Expected: PASS (all tests, old and new)

- [ ] **Step 5: Run the full backend test suite**

Run: `uv run pytest backend/tests/ -v`
Expected: PASS (no regressions elsewhere)

- [ ] **Step 6: Commit**

```bash
git add bin/migrate_library.py backend/tests/test_migrate_library.py
git commit -m "feat(migration): add --mode flag and interactive resume/clean prompt

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 4: Update CLAUDE.md documentation

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Replace the outdated "no resume" note**

In the `## Migrating Library RAG Data Between Instances` section, replace
this paragraph:

```
`<slug>` is a Zotero.org library slug (`users/<id>` or `groups/<id>`), e.g.
`groups/6297749` for the `test-rag-plugin` library. `--dry-run` reports the
source library's indexed size and checks embedding-model compatibility
without writing anything. The script aborts before any write if source
and destination use different embedding models/dimensions — re-index on
the destination in that case rather than migrating incompatible vectors.
A failed run is recovered by simply re-running the script; the
destination is re-cleared on every run, so there is no
resume-from-cursor logic.
```

with:

```
`<slug>` is a Zotero.org library slug (`users/<id>` or `groups/<id>`), e.g.
`groups/6297749` for the `test-rag-plugin` library. `--dry-run` reports the
source library's indexed size and checks embedding-model compatibility
without writing anything. The script aborts before any write if source
and destination use different embedding models/dimensions — re-index on
the destination in that case rather than migrating incompatible vectors.

If a run is interrupted (crash, network outage, kill), it can resume
instead of restarting: progress is checkpointed to
`<data_path>/system/migration_state/` after every batch. Re-running the
same command with no `--mode` flag finds the incomplete state and prompts
`Resume, Clean, or Abort? [r/c/a]`; pass `--mode=resume` to continue
non-interactively or `--mode=clean` to discard it and start over (the
original "always re-clear the destination" behavior). See
`docs/superpowers/specs/2026-10-05-library-rag-migration-resume-design.md`.
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: document migration script's resume/clean/abort behavior

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```
