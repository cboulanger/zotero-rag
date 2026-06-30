# Subprocess Batch Indexing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent the cron indexer from OOMing on large libraries by processing items in isolated subprocesses that exit after N items, completely resetting the Python heap between batches.

**Architecture:** Replace the monolithic item loop in `_index_library_full` with a batch loop. Each batch is processed by a fresh `multiprocessing.Process` that inherits env vars, re-initialises all clients, processes N items, writes results to a `multiprocessing.Queue`, and exits. When the subprocess exits, the OS reclaims all its memory unconditionally — glibc heap fragmentation, Python arena leakage, and HTTP client state all vanish. The main process accumulates batch results and drives progress reporting. When `settings.testing` is True the old inline loop runs instead, keeping all existing tests green without change.

**Tech Stack:** Python 3.12 `multiprocessing` (fork start method on Linux), `asyncio.run` inside subprocess worker, existing `make_embedding_service` / `make_vector_store` from `backend.dependencies`, `ZoteroWebAPI` async context manager.

## Global Constraints

- Python 3.12 only — `max_tasks_per_child` not needed (we restart explicitly via fresh Process per batch)
- `multiprocessing` start method: `fork` (Linux default) — do NOT call `set_start_method`
- All existing tests must pass unchanged — use `settings.testing` gate to bypass subprocess in tests
- Batch size default 300, overridable via `INDEX_BATCH_SIZE` env var
- On subprocess OOM (exitcode -9): log warning, skip batch, continue — items will be retried next cron run
- On subprocess fatal embedding error: propagate to main process and abort the full sync
- `uv run pytest backend/tests/test_document_processor.py` must pass before and after

---

## File Map

| Action | Path | Responsibility |
|---|---|---|
| Modify | `backend/services/document_processor.py` | Add module-level worker functions; replace item loop with batch loop |
| Modify | `backend/tests/test_document_processor.py` | Add tests for batch error handling |
| Modify | `.env.dist` | Document `INDEX_BATCH_SIZE` |

---

### Task 1: Add the subprocess worker function and wire up the batch loop

**Files:**
- Modify: `backend/services/document_processor.py`

**Context — existing code to understand before editing:**

`_index_library_full` (line ~357) currently:
1. Fetches all items from Zotero, spills to JSONL, filters to `items_with_attachments`
2. Gets `indexed_versions` dict from Qdrant (one scroll)
3. Deletes orphaned chunks
4. Runs `for idx, item in enumerate(items_with_attachments):` calling `await self._index_item(...)`
5. Calls `_trim_memory_if_needed()` in the `finally` block

The `DocumentProcessor.__init__` signature is:
```python
def __init__(self, zotero_client: ZoteroLocalAPI, embedding_service: EmbeddingService,
             vector_store: VectorStore, document_extractor=None, max_chunk_size=512, chunk_overlap=50)
```

`make_embedding_service()` and `make_vector_store()` in `backend/dependencies.py` build these from env vars with no arguments.

`ZoteroWebAPI(api_key: str)` creates a Zotero client; must be used as `async with web_api:`.

`_FATAL_EMBEDDING_ERRORS = (EmbeddingAuthenticationError, EmbeddingRateLimitExhaustedError)` — these must abort the whole run, not just a single batch.

**Interfaces:**
- Produces: `_subprocess_index_batch(items, library_id, library_type, indexed_versions) -> dict` — module-level, picklable
- Produces: `_run_subprocess_batch(items, library_id, library_type, indexed_versions, result_queue)` — module-level target for `multiprocessing.Process`
- Produces: `SUBPROCESS_BATCH_SIZE: int` — module-level constant (read from env at import time)

- [ ] **Step 1: Baseline — run existing tests and confirm they pass**

```bash
uv run pytest backend/tests/test_document_processor.py -v --tb=short 2>&1 | tail -10
```
Expected: all pass. If any fail, stop and investigate before proceeding.

- [ ] **Step 2: Add the module-level constants and worker functions**

In `backend/services/document_processor.py`, find the block that currently reads:

```python
_GC_RSS_THRESHOLD_MB = 3000

_libc = ctypes.CDLL("libc.so.6") if sys.platform == "linux" else None
```

Add the following **immediately after** the `_libc` line (before the `DocumentProcessor` class):

```python
SUBPROCESS_BATCH_SIZE = int(os.environ.get("INDEX_BATCH_SIZE", "300"))


def _subprocess_index_batch(
    items: list[dict],
    library_id: str,
    library_type: str,
    indexed_versions: dict[str, int],
) -> dict:
    """Process a batch of items in an isolated subprocess.

    Re-initialises all clients from env vars so the subprocess is completely
    independent of the parent's heap. Called via multiprocessing.Process on Linux
    (fork context) — inherits all env vars from the parent process.

    Returns a stats dict: chunks_added, items_added, items_updated, items_skipped.
    Raises EmbeddingAuthenticationError / EmbeddingRateLimitExhaustedError on fatal
    embedding failures so the main process can abort the full run.
    """
    import asyncio as _asyncio

    from backend.dependencies import make_embedding_service, make_vector_store
    from backend.zotero.web_api import ZoteroWebAPI

    async def _run() -> dict:
        settings = get_settings()
        if not settings.zotero_api_key:
            raise RuntimeError("ZOTERO_API_KEY is not set — cannot initialise ZoteroWebAPI in subprocess")

        embedding_service = make_embedding_service()
        vector_store = make_vector_store()
        web_api = ZoteroWebAPI(api_key=settings.zotero_api_key)

        chunks_added = items_added = items_updated = items_skipped = 0
        async with web_api:
            processor = DocumentProcessor(
                zotero_client=web_api,
                embedding_service=embedding_service,
                vector_store=vector_store,
            )
            for item in items:
                item_key = item["data"]["key"]
                item_version = item.get("version", 0)
                existing = indexed_versions.get(item_key)
                try:
                    if existing is None:
                        n = await processor._index_item(item, library_id, library_type)
                        chunks_added += n
                        items_added += 1
                    elif existing < item_version:
                        vector_store.delete_item_chunks(library_id, item_key)
                        n = await processor._index_item(item, library_id, library_type)
                        chunks_added += n
                        items_updated += 1
                    else:
                        items_skipped += 1
                except _FATAL_EMBEDDING_ERRORS:
                    raise
                except Exception as e:
                    logger.error("Error processing item %s in subprocess batch: %s", item_key, e, exc_info=True)

        return {
            "chunks_added": chunks_added,
            "items_added": items_added,
            "items_updated": items_updated,
            "items_skipped": items_skipped,
        }

    return _asyncio.run(_run())


def _run_subprocess_batch(
    items: list[dict],
    library_id: str,
    library_type: str,
    indexed_versions: dict[str, int],
    result_queue,  # multiprocessing.Queue
) -> None:
    """Target for multiprocessing.Process. Runs _subprocess_index_batch and puts
    result on result_queue. On fatal embedding error puts {'fatal': True, ...}."""
    try:
        result = _subprocess_index_batch(items, library_id, library_type, indexed_versions)
        result_queue.put({"fatal": False, **result})
    except _FATAL_EMBEDDING_ERRORS as e:
        result_queue.put({"fatal": True, "error": repr(e), "error_type": type(e).__name__})
    except Exception as e:
        logger.error("Subprocess batch worker raised unexpected error: %s", e, exc_info=True)
        result_queue.put({"fatal": False, "chunks_added": 0, "items_added": 0,
                          "items_updated": 0, "items_skipped": 0})
```

- [ ] **Step 3: Replace the item loop in `_index_library_full` with the batch dispatcher**

Find the comment `# Index items: skip unchanged, update outdated, add new` (around line 464) and the loop that follows it, down through the `finally: _trim_memory_if_needed()`. Replace that entire block (from the `chunks_added = 0` initialisation through the closing `}` of the `for` loop) with:

```python
        # Pre-compute max_version_seen from the full item list — the subprocess
        # worker processes a slice and cannot update this value in the parent.
        max_version_seen = max(
            (item.get("version", 0) for item in items_with_attachments),
            default=0,
        )

        # Index items: skip unchanged, update outdated, add new
        chunks_added = 0
        items_added = 0
        items_updated = 0
        items_skipped = 0
        total_items = len(items_with_attachments)

        if progress_callback:
            progress_callback(0, total_items, 0)

        use_subprocess = not get_settings().testing and SUBPROCESS_BATCH_SIZE > 0

        if use_subprocess:
            # --- Subprocess-isolated batch processing ---
            # Each batch runs in a fresh OS process; when it exits the OS reclaims
            # all memory unconditionally, bounding heap growth to one batch at a time.
            from multiprocessing import Process, Queue as MPQueue

            items_processed_so_far = 0
            for batch_start in range(0, total_items, SUBPROCESS_BATCH_SIZE):
                if cancellation_check and cancellation_check():
                    logger.info("Cancellation requested during full sync of library %s", library_id)
                    raise RuntimeError("Indexing cancelled by user")

                batch = items_with_attachments[batch_start: batch_start + SUBPROCESS_BATCH_SIZE]
                batch_keys = {item["data"]["key"] for item in batch}
                batch_indexed = {k: v for k, v in indexed_versions.items() if k in batch_keys}

                result_q: MPQueue = MPQueue()
                proc = Process(
                    target=_run_subprocess_batch,
                    args=(batch, library_id, library_type, batch_indexed, result_q),
                    daemon=True,
                )
                proc.start()
                proc.join()

                if not result_q.empty():
                    result = result_q.get_nowait()
                    if result.get("fatal"):
                        # Re-raise fatal embedding error to abort the full run
                        from backend.services.embeddings import (
                            EmbeddingAuthenticationError,
                            EmbeddingRateLimitExhaustedError,
                        )
                        error_type = result.get("error_type", "")
                        if error_type == "EmbeddingAuthenticationError":
                            raise EmbeddingAuthenticationError(result.get("error", ""))
                        raise EmbeddingRateLimitExhaustedError(result.get("error", ""))
                    chunks_added += result.get("chunks_added", 0)
                    items_added += result.get("items_added", 0)
                    items_updated += result.get("items_updated", 0)
                    items_skipped += result.get("items_skipped", 0)
                elif proc.exitcode is not None and proc.exitcode < 0:
                    logger.warning(
                        "Batch %d–%d killed by signal %d (likely OOM); "
                        "items will be retried on next run",
                        batch_start, batch_start + len(batch), -proc.exitcode,
                    )
                else:
                    logger.warning(
                        "Batch %d–%d produced no result (exit code %s)",
                        batch_start, batch_start + len(batch), proc.exitcode,
                    )

                items_processed_so_far += len(batch)
                if progress_callback:
                    progress_callback(items_processed_so_far, total_items, chunks_added)

        else:
            # --- Inline processing (used when settings.testing=True) ---
            for idx, item in enumerate(items_with_attachments):
                if cancellation_check and cancellation_check():
                    logger.info("Cancellation requested during full sync of library %s", library_id)
                    raise RuntimeError("Indexing cancelled by user")

                try:
                    item_key = item["data"]["key"]
                    item_version = item.get("version", 0)
                    existing_version = indexed_versions.get(item_key)

                    if existing_version is None:
                        logger.debug("New item %s (version %s)", item_key, item_version)
                        chunk_count = await self._index_item(item, library_id, library_type)
                        items_added += 1
                        chunks_added += chunk_count
                    elif existing_version < item_version:
                        logger.debug("Updated item %s (%s -> %s)", item_key, existing_version, item_version)
                        self.vector_store.delete_item_chunks(library_id, item_key)
                        chunk_count = await self._index_item(item, library_id, library_type)
                        items_updated += 1
                        chunks_added += chunk_count
                    else:
                        items_skipped += 1

                except _FATAL_EMBEDDING_ERRORS:
                    raise
                except Exception as e:
                    logger.error("Error processing item in full sync mode: %s", e, exc_info=True)
                finally:
                    if progress_callback:
                        progress_callback(idx + 1, total_items, chunks_added)
                    _trim_memory_if_needed()
```

Note: the `max_version_seen` variable is no longer updated inside the loop — it was pre-computed above. Remove the `max_version_seen = max(max_version_seen, item_version)` line from the old loop (it is not present in the new code above).

- [ ] **Step 4: Run tests to verify existing behaviour is preserved**

```bash
uv run pytest backend/tests/test_document_processor.py -v --tb=short 2>&1 | tail -20
```
Expected: all 22 tests pass. If any fail, the loop replacement introduced a regression — compare the new inline path with the original and fix.

- [ ] **Step 5: Commit**

```bash
git add backend/services/document_processor.py
git commit -m "perf: process full-sync items in isolated subprocesses to bound heap growth

Each batch of INDEX_BATCH_SIZE items (default 300) runs in a fresh subprocess.
When the subprocess exits the OS reclaims all memory regardless of Python heap
fragmentation, capping RSS growth to one batch instead of the whole library.
OOM-killed batches are logged and skipped; items are retried on the next run.
settings.testing=True falls back to the original inline loop so unit tests
need no changes.

[skip ci]"
```

---

### Task 2: Add tests for the subprocess batch error paths

**Files:**
- Modify: `backend/tests/test_document_processor.py`

**Context:** All existing tests use `settings.testing = True` implicitly (the test environment sets it), so they already exercise the inline code path and need no changes. This task adds two new tests for behaviour that is only reachable via the subprocess path (fatal error propagation and OOM-killed batch recovery), using `unittest.mock.patch` to control subprocess behaviour without actually forking.

**Interfaces:**
- Consumes: `_run_subprocess_batch` (module-level, patchable)
- Consumes: `SUBPROCESS_BATCH_SIZE` (module-level constant, can be monkey-patched in tests)

- [ ] **Step 1: Write the failing tests**

Add the following test class to `backend/tests/test_document_processor.py` (after the existing `TestDocumentProcessor` class):

```python
class TestSubprocessBatchIndexing(unittest.IsolatedAsyncioTestCase):
    """Tests for the subprocess-isolated batch processing path in _index_library_full."""

    def setUp(self):
        self.mock_zotero_client = AsyncMock()
        self.mock_embedding_service = AsyncMock()
        self.mock_vector_store = Mock()
        self.mock_vector_store.find_cross_library_duplicate.return_value = None
        self.mock_vector_store.get_all_indexed_item_versions.return_value = {}
        self.mock_extractor = AsyncMock(spec=DocumentExtractor)
        self.processor = DocumentProcessor(
            zotero_client=self.mock_zotero_client,
            embedding_service=self.mock_embedding_service,
            vector_store=self.mock_vector_store,
            document_extractor=self.mock_extractor,
        )

    @patch("backend.services.document_processor.SUBPROCESS_BATCH_SIZE", 1)
    @patch("backend.services.document_processor.Process")
    @patch("backend.services.document_processor.MPQueue")
    async def test_subprocess_batch_fatal_error_aborts_run(self, mock_queue_cls, mock_process_cls):
        """A fatal embedding error reported by a subprocess must abort _index_library_full."""
        from backend.services.embeddings import EmbeddingAuthenticationError

        item = {"version": 1, "data": {"key": "AAA", "itemType": "journalArticle", "title": "A"}}
        pdf = _attachment("PDF", "AAA")
        self.mock_zotero_client.get_library_items_since.return_value = [item, pdf]

        # Simulate subprocess reporting a fatal embedding error via the queue
        mock_q = MagicMock()
        mock_q.empty.return_value = False
        mock_q.get_nowait.return_value = {
            "fatal": True,
            "error": "bad key",
            "error_type": "EmbeddingAuthenticationError",
        }
        mock_queue_cls.return_value = mock_q

        mock_proc = MagicMock()
        mock_proc.exitcode = 0
        mock_process_cls.return_value = mock_proc

        # Patch settings.testing to False so the subprocess path is taken
        with patch("backend.services.document_processor.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                testing=False,
                min_abstract_words=5,
                zotero_api_key="dummy",
            )
            with self.assertRaises(EmbeddingAuthenticationError):
                await self.processor._index_library_full(
                    library_id="test_lib",
                    library_type="user",
                    metadata=MagicMock(last_indexed_version=0),
                )

    @patch("backend.services.document_processor.SUBPROCESS_BATCH_SIZE", 1)
    @patch("backend.services.document_processor.Process")
    @patch("backend.services.document_processor.MPQueue")
    async def test_subprocess_oom_kill_skips_batch_and_continues(self, mock_queue_cls, mock_process_cls):
        """An OOM-killed subprocess (exitcode -9) must be logged and skipped, not abort the run."""
        item1 = {"version": 1, "data": {"key": "AAA", "itemType": "journalArticle", "title": "A"}}
        item2 = {"version": 1, "data": {"key": "BBB", "itemType": "journalArticle", "title": "B"}}
        pdf1 = _attachment("PDF1", "AAA")
        pdf2 = _attachment("PDF2", "BBB")
        self.mock_zotero_client.get_library_items_since.return_value = [item1, item2, pdf1, pdf2]

        # First batch OOM-killed (empty queue, exitcode -9)
        # Second batch succeeds
        mock_q1 = MagicMock()
        mock_q1.empty.return_value = True  # no result — OOM killed
        mock_q2 = MagicMock()
        mock_q2.empty.return_value = False
        mock_q2.get_nowait.return_value = {
            "fatal": False, "chunks_added": 3, "items_added": 1,
            "items_updated": 0, "items_skipped": 0,
        }
        mock_queue_cls.side_effect = [mock_q1, mock_q2]

        mock_proc1 = MagicMock()
        mock_proc1.exitcode = -9  # SIGKILL
        mock_proc2 = MagicMock()
        mock_proc2.exitcode = 0
        mock_process_cls.side_effect = [mock_proc1, mock_proc2]

        with patch("backend.services.document_processor.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                testing=False,
                min_abstract_words=5,
                zotero_api_key="dummy",
            )
            result = await self.processor._index_library_full(
                library_id="test_lib",
                library_type="user",
                metadata=MagicMock(last_indexed_version=0),
            )

        # Run completed despite the OOM-killed first batch
        self.assertEqual(result["chunks_added"], 3)
```

Note: `_attachment` and `MagicMock` are already imported in the test file. Add `from unittest.mock import patch, MagicMock` if not already present (check the existing imports first).

Also add to the imports section of the test file:
```python
from backend.services.document_processor import DocumentProcessor, _attachment  # check existing imports
```

- [ ] **Step 2: Run the new tests to confirm they fail (expected at this point)**

```bash
uv run pytest backend/tests/test_document_processor.py::TestSubprocessBatchIndexing -v --tb=short 2>&1 | tail -15
```

Expected: FAIL — `Process` and `MPQueue` are not importable at the test patch path yet (they're inside the `if use_subprocess:` block). This confirms the test is exercising the right code path.

**Fix needed:** The `from multiprocessing import Process, Queue as MPQueue` import inside the `if use_subprocess:` block must be moved to module-level so the patch target exists:

Add to the top-level imports in `document_processor.py`:
```python
from multiprocessing import Process
from multiprocessing import Queue as MPQueue
```

Remove the inline `from multiprocessing import Process, Queue as MPQueue` from inside the `if use_subprocess:` block.

- [ ] **Step 3: Re-run all tests**

```bash
uv run pytest backend/tests/test_document_processor.py -v --tb=short 2>&1 | tail -20
```

Expected: all 22 original tests + 2 new tests = 24 pass.

- [ ] **Step 4: Commit**

```bash
git add backend/tests/test_document_processor.py backend/services/document_processor.py
git commit -m "test: add subprocess batch error path tests (OOM skip, fatal embed abort)

[skip ci]"
```

---

### Task 3: Document INDEX_BATCH_SIZE in .env.dist

**Files:**
- Modify: `.env.dist`

- [ ] **Step 1: Add the env var documentation**

Find the section in `.env.dist` that covers performance or indexing settings. If no obvious section exists, add after the last non-comment line. Insert:

```bash
# Number of items processed per subprocess batch during full library sync.
# Each batch runs in a fresh OS process; when it exits all memory is freed.
# Lower values reduce peak RSS but increase subprocess-spawn overhead.
# Set to 0 to disable subprocess isolation (runs inline; not recommended in production).
# Default: 300
#INDEX_BATCH_SIZE=300
```

- [ ] **Step 2: Verify the file looks right**

```bash
grep -A4 "INDEX_BATCH_SIZE" .env.dist
```

Expected: the 5 lines above appear in the file.

- [ ] **Step 3: Commit**

```bash
git add .env.dist
git commit -m "docs: document INDEX_BATCH_SIZE env var for subprocess batch indexing

[skip ci]"
```

---

### Task 4: Make embedding batch size configurable via env var

**Background:** During the Jun 29 outages, the cron indexer hit OOM twice while the embedding service was sending 215 chunks in a single API call. The `remote-kisski` preset has `batch_size=256` hardcoded — reducing this breaks the embedding payload into smaller calls, lowering peak RSS at the cost of extra round-trips.

**Files:**
- Modify: `backend/config/presets.py`
- Modify: `.env.dist`

**Context:** `EmbeddingConfig.batch_size` (default 32, defined in `presets.py` line 17) controls how many texts are sent per embedding API call. The `remote-kisski` preset overrides this to 256 (`presets.py` line 219). This is the preset used in production (`INFO - Using preset: remote-kisski`).

- [ ] **Step 1: Make the batch_size in remote-kisski read from an env var**

In `backend/config/presets.py`, find the `remote-kisski` embedding config block:

```python
        embedding=EmbeddingConfig(
            model_type="remote",
            model_name="multilingual-e5-large-instruct",  # KISSKI: 1024-dim, multilingual
            batch_size=256,  # Send more texts per API call to reduce round-trips
```

Replace the `batch_size` line with:

```python
            batch_size=int(os.environ.get("EMBEDDING_BATCH_SIZE", "256")),  # tunable; lower to reduce peak RSS
```

Confirm `import os` is already present at the top of `presets.py`. If not, add it.

- [ ] **Step 2: Document the env var in `.env.dist`**

Find the `INDEX_BATCH_SIZE` block added by Task 3 and append immediately after it:

```bash
# Maximum number of texts sent per embedding API call.
# Lower values reduce peak RSS when indexing large documents (fewer texts held in
# memory simultaneously) at the cost of more round-trips to the embedding service.
# The remote-kisski preset defaults to 256; for the current 16 GB host, 64 is safer.
# Default: 256 (remote-kisski preset value)
#EMBEDDING_BATCH_SIZE=64
```

- [ ] **Step 3: Smoke-test**

```bash
uv run python -c "
import os; os.environ['EMBEDDING_BATCH_SIZE'] = '64'
from backend.config.presets import PRESETS
p = PRESETS['remote-kisski']
assert p.embedding.batch_size == 64, f'Expected 64, got {p.embedding.batch_size}'
print('OK: batch_size overridden to', p.embedding.batch_size)
"
```

Expected output: `OK: batch_size overridden to 64`

- [ ] **Step 4: Commit**

```bash
git add backend/config/presets.py .env.dist
git commit -m "feat: make embedding batch size configurable via EMBEDDING_BATCH_SIZE env var

The remote-kisski preset sent up to 256 texts per API call, holding all
embeddings in memory simultaneously. On the 16 GB production host this
contributed to OOM crashes during cron indexing runs. EMBEDDING_BATCH_SIZE
lets operators cap this without changing presets.

[skip ci]"
```

---

## Self-Review

**Spec coverage:**
- Subprocess isolation per batch ✓ (Task 1)
- OOM-killed batch: logged + skipped ✓ (Task 1, Task 2 test)
- Fatal embedding error: propagated + run aborted ✓ (Task 1, Task 2 test)
- Existing tests unchanged ✓ (`settings.testing` gate)
- `INDEX_BATCH_SIZE` env var ✓ (Tasks 1, 3)
- `max_version_seen` pre-computed before loop ✓ (Task 1)
- Cancellation check at batch boundary ✓ (Task 1)
- `EMBEDDING_BATCH_SIZE` env var to cap per-call memory ✓ (Task 4)

**Placeholder scan:** No TBDs. All code blocks are complete.

**Type consistency:**
- `_subprocess_index_batch` returns `dict` → `_run_subprocess_batch` puts same `dict` on queue → main loop reads `.get("chunks_added", 0)` etc. ✓
- `result_queue` typed as `MPQueue` in the call — matches the `MPQueue()` instantiation ✓
- `EmbeddingAuthenticationError` re-raised by class name lookup — matches `_FATAL_EMBEDDING_ERRORS` tuple ✓

---

## Implementation Summary (completed 2026-06-30)

All four tasks implemented and committed.

**Root cause discovered during implementation:** `settings.testing` defaulted to `False` because no test environment set the `TESTING` env var. This caused pytest to spawn real `multiprocessing.Process` workers, each consuming 13+ GB RSS, which triggered the kernel OOM killer and crashed the host VM twice on 2026-06-30 (confirmed via `journalctl -b -2`). Fix: `conftest.py` now calls `os.environ.setdefault("TESTING", "true")` at the top, and `.env.test` also sets `TESTING=true`.

**Host protection:** An 8 GB swapfile was created at `/swapfile` and made persistent via `/etc/fstab`. This is a one-time per-server action — not in deployment scripts because disk capacity is server-specific.

**Commits:**
1. `fix: prevent test-triggered OOM and isolate indexing memory via subprocesses` — Task 1 + testing guard
2. `test: add subprocess batch error path tests` — Task 2
3. `feat: make embedding batch size configurable + document INDEX_BATCH_SIZE` — Tasks 3 & 4
