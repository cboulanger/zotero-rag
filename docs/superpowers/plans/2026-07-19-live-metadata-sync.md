# Live Client-Side Metadata Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Push Zotero item metadata edits (title/authors/tags/year/item_type) to the backend immediately via a debounced client-side task queue, instead of waiting for the hourly auto-index cron.

**Architecture:** A new generic, type-discriminated `TaskQueue` engine (plugin-lifetime module) debounces per-item edits, batches them, and sequentially dispatches them with exponential backoff. The plugin's existing item notifier gains a `modify` branch that feeds this queue; a registered `metadata` dispatcher POSTs batches to an extended `/api/index/items/metadata` endpoint.

**Tech Stack:** Plain JS (Zotero bootstrap plugin, loaded via `Services.scriptloader.loadSubScript`), Node's built-in test runner (`node --test`), Python/FastAPI backend, `unittest`/`pytest`.

**Spec:** `docs/superpowers/specs/2026-07-19-live-metadata-sync-design.md`

**Correction vs. the spec:** The spec's §5.4 proposed having `batch_update_metadata` delegate wholesale to `VectorStore.update_item_bibliographic_metadata()`, which requires `tags`/`item_version`/`zotero_modified` unconditionally. Tracing it through, that would corrupt existing Qdrant payload values (blank out `tags`, zero out `item_version`) for any caller that omits those fields — such as the *existing* schema-migration caller, before Task 5 below updates it. This plan instead keeps the endpoint's existing per-field-conditional pattern (already correct for `title`/`authors`/`year`/`item_type`) and just extends it the same way for the three new fields, calling the lower-level `vector_store.update_item_metadata()` directly. No changes to `vector_store.py` are needed at all.

---

### Task 1: Backend — extend `/api/index/items/metadata` with `tags`, `item_version`, `zotero_modified`

**Files:**
- Modify: `backend/api/document_upload.py:171-178` (`ItemMetadataUpdate`), `backend/api/document_upload.py:918-929` (`batch_update_metadata`'s field-assembly loop)
- Test: `backend/tests/test_batch_metadata_update_fields.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_batch_metadata_update_fields.py`:

```python
"""Tests for the extended /api/index/items/metadata endpoint: tags,
item_version, and zotero_modified are now accepted and patched onto
existing Qdrant chunks, in addition to the fields it already supported
(title/authors/year/item_type)."""

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from qdrant_client.models import Distance

from backend.main import app
from backend.config.settings import get_settings, reset_settings
from backend.db.vector_store import VectorStore
from backend.models.document import ChunkMetadata, DocumentChunk, DocumentMetadata
from backend.services.zotero_identity import ZoteroIdentity, reset_identity_cache
import backend.dependencies as dependencies


class BatchMetadataUpdateFieldsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        reset_settings()
        reset_identity_cache()
        s = get_settings()
        s.data_path = Path(self.tmp.name)
        s.testing = True
        self.client = TestClient(app)
        self.vector_store = VectorStore(
            storage_path=Path(self.tmp.name) / "qdrant",
            embedding_dim=8,
            embedding_model_name="test-model",
            distance=Distance.COSINE,
        )
        app.state.vector_store = self.vector_store
        app.dependency_overrides[dependencies.get_zotero_identity] = (
            lambda: ZoteroIdentity(user_id=1, username="u", targets=["users/1"])
        )

    def tearDown(self):
        app.dependency_overrides.clear()
        self.tmp.cleanup()
        reset_settings()
        reset_identity_cache()

    def _seed_chunk(self, tags, item_version):
        self.vector_store.add_chunk(DocumentChunk(
            text="Chunk 0",
            metadata=ChunkMetadata(
                chunk_id="chunk-0",
                document_metadata=DocumentMetadata(
                    library_id="u1",
                    item_key="ITEM1",
                    title="Old Title",
                    authors=["Old Author"],
                    tags=tags,
                    year=2000,
                    item_type="book",
                ),
                page_number=1,
                text_preview="Chunk 0",
                chunk_index=0,
                content_hash="hash0",
                item_version=item_version,
            ),
            embedding=[0.1] * 8,
        ))

    def test_tags_item_version_and_zotero_modified_are_patched(self):
        self._seed_chunk(tags=["OldTag"], item_version=1)

        r = self.client.post(
            "/api/index/items/metadata",
            json={
                "library_id": "u1",
                "items": [{
                    "item_key": "ITEM1",
                    "title": "New Title",
                    "authors": ["New Author"],
                    "tags": ["NewTag", "SecondTag"],
                    "year": 2020,
                    "item_type": "journalArticle",
                    "item_version": 5,
                    "zotero_modified": "2026-01-01T00:00:00Z",
                }],
            },
        )

        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["updated_items"], 1)
        self.assertEqual(body["updated_chunks"], 1)

        chunks = self.vector_store.get_item_chunks("u1", "ITEM1")
        self.assertEqual(len(chunks), 1)
        payload = chunks[0]["payload"]
        self.assertEqual(payload["tags"], ["NewTag", "SecondTag"])
        self.assertEqual(payload["tags_lower"], ["newtag", "secondtag"])
        self.assertEqual(payload["item_version"], 5)
        self.assertEqual(payload["zotero_modified"], "2026-01-01T00:00:00Z")

    def test_omitting_the_new_fields_leaves_existing_values_untouched(self):
        """A caller that doesn't send tags/item_version/zotero_modified (e.g.
        a hypothetical legacy caller) must not wipe them — this is the bug
        the wholesale-delegation approach in the design spec would have
        introduced; the conditional-field pattern here avoids it."""
        self._seed_chunk(tags=["KeepMe"], item_version=3)

        r = self.client.post(
            "/api/index/items/metadata",
            json={"library_id": "u1", "items": [{"item_key": "ITEM1", "title": "New Title"}]},
        )

        self.assertEqual(r.status_code, 200)
        chunks = self.vector_store.get_item_chunks("u1", "ITEM1")
        payload = chunks[0]["payload"]
        self.assertEqual(payload["title"], "New Title")
        self.assertEqual(payload["tags"], ["KeepMe"])
        self.assertEqual(payload["item_version"], 3)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest backend/tests/test_batch_metadata_update_fields.py -v`
Expected: FAIL — `test_tags_item_version_and_zotero_modified_are_patched` fails because `tags`/`item_version`/`zotero_modified` aren't accepted fields on `ItemMetadataUpdate` yet (Pydantic drops unknown fields silently by default, so the request itself returns 200 but the assertions on `payload["tags"]` etc. fail since nothing was patched — they'd still read the seeded `["OldTag"]`/`1`).

- [ ] **Step 3: Extend `ItemMetadataUpdate` and the field-assembly loop**

In `backend/api/document_upload.py`, replace lines 171-178:

```python
class ItemMetadataUpdate(BaseModel):
    """Metadata fields to update for a single Zotero item."""

    item_key: str
    title: Optional[str] = None
    authors: list[str] = []
    year: Optional[int] = None
    item_type: Optional[str] = None
```

with:

```python
class ItemMetadataUpdate(BaseModel):
    """Metadata fields to update for a single Zotero item."""

    item_key: str
    title: Optional[str] = None
    authors: list[str] = []
    tags: list[str] = []
    year: Optional[int] = None
    item_type: Optional[str] = None
    item_version: Optional[int] = None
    zotero_modified: Optional[str] = None
```

Then replace the field-assembly loop at lines 918-929:

```python
    for item in request.items:
        fields: dict = {"schema_version": CURRENT_SCHEMA_VERSION}
        if item.title is not None:
            fields["title"] = item.title
        if item.authors:
            fields["authors"] = item.authors
            fields["author_lastnames"] = _extract_lastnames(item.authors)
        if item.year is not None:
            fields["year"] = item.year
        if item.item_type is not None:
            fields["item_type"] = item.item_type
```

with:

```python
    for item in request.items:
        fields: dict = {"schema_version": CURRENT_SCHEMA_VERSION}
        if item.title is not None:
            fields["title"] = item.title
        if item.authors:
            fields["authors"] = item.authors
            fields["author_lastnames"] = _extract_lastnames(item.authors)
        if item.tags:
            fields["tags"] = item.tags
            fields["tags_lower"] = _lower_all(item.tags)
        if item.year is not None:
            fields["year"] = item.year
        if item.item_type is not None:
            fields["item_type"] = item.item_type
        if item.item_version is not None:
            fields["item_version"] = item.item_version
        if item.zotero_modified is not None:
            fields["zotero_modified"] = item.zotero_modified
```

Add `_lower_all` to the existing import at line 32:

```python
from backend.db.vector_store import VectorStore, _extract_lastnames, _lower_all
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest backend/tests/test_batch_metadata_update_fields.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Run the full backend test suite to check for regressions**

Run: `uv run pytest backend/tests/test_document_upload_authorization.py backend/tests/test_vector_store.py -v`
Expected: PASS (no changes to those files, this just confirms nothing broke)

- [ ] **Step 6: Commit**

```bash
git add backend/api/document_upload.py backend/tests/test_batch_metadata_update_fields.py
git commit -m "$(cat <<'EOF'
feat(backend): accept tags/item_version/zotero_modified in metadata update endpoint

Extends the existing metadata-only patch endpoint so it can fully
sync an item's bibliographic state, needed for the upcoming live
client-side metadata push. Keeps the endpoint's existing
per-field-conditional pattern rather than delegating to
update_item_bibliographic_metadata, since that method requires all
fields unconditionally and would silently blank tags/item_version
for any caller (like the not-yet-updated plugin schema-migration
path) that omits them.
EOF
)"
```

---

### Task 2: Plugin — generic `TaskQueue` engine

**Files:**
- Create: `plugin/src/task_queue.js`
- Test: `plugin/test/task_queue.test.js` (new)

- [ ] **Step 1: Write the failing tests**

Create `plugin/test/task_queue.test.js`:

```js
// Tests for plugin/src/task_queue.js — the generic debounced, batched,
// retrying task queue used for live metadata sync (and, later, other
// task types). No Zotero dependency: loaded into a bare vm context with
// just `console` and the timer globals. Tests never wait on a real
// timer — they call _tick() directly after moving the queue's
// overridable _now() clock forward.

const assert = require('node:assert');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SOURCE_PATH = path.join(__dirname, '..', 'src', 'task_queue.js');

/**
 * Load a fresh TaskQueue into its own vm context, with console calls
 * captured into arrays instead of printed.
 * @returns {{ queue: any, warnings: string[], errors: string[] }}
 */
function loadTaskQueue() {
	const warnings = [];
	const errors = [];
	const context = {
		console: {
			warn: (/** @type {string} */ msg) => warnings.push(msg),
			error: (/** @type {string} */ msg) => errors.push(msg),
			log: () => {},
		},
		setInterval,
		clearInterval,
	};
	vm.createContext(context);
	const src = fs.readFileSync(SOURCE_PATH, 'utf8');
	vm.runInContext(src, context, { filename: 'task_queue.js' });
	return { queue: context.TaskQueue, warnings, errors };
}

test('a task is not dispatched before its debounce window elapses', () => {
	const { queue } = loadTaskQueue();
	let calls = 0;
	queue._now = () => 1000;
	queue.registerDispatcher('metadata', async () => { calls++; return { succeededKeys: new Set() }; });
	queue.enqueue('metadata', 'lib:ITEM1', { title: 'A' }, 4000);

	queue._tick(); // still at t=1000, due at 5000 — not ready yet

	assert.strictEqual(calls, 0);
});

test('a task is dispatched once its debounce window elapses', async () => {
	const { queue } = loadTaskQueue();
	/** @type {any[]} */
	const dispatchedBatches = [];
	queue._now = () => 1000;
	queue.registerDispatcher('metadata', async (/** @type {any[]} */ batch) => {
		dispatchedBatches.push(batch);
		return { succeededKeys: new Set(batch.map(t => t.key)) };
	});
	queue.enqueue('metadata', 'lib:ITEM1', { title: 'A' }, 4000);

	queue._now = () => 5001; // past the 4000ms debounce window
	queue._tick();
	await new Promise(r => setImmediate(r)); // let the dispatcher's promise settle

	assert.strictEqual(dispatchedBatches.length, 1);
	assert.strictEqual(dispatchedBatches[0][0].payload.title, 'A');
});

test('repeated enqueue calls for the same key collapse into one dispatch with the latest payload', async () => {
	const { queue } = loadTaskQueue();
	/** @type {any[]} */
	const dispatchedBatches = [];
	queue._now = () => 1000;
	queue.registerDispatcher('metadata', async (/** @type {any[]} */ batch) => {
		dispatchedBatches.push(batch);
		return { succeededKeys: new Set(batch.map(t => t.key)) };
	});
	queue.enqueue('metadata', 'lib:ITEM1', { title: 'First edit' }, 4000);
	queue._now = () => 2000; // still within the debounce window from the first call
	queue.enqueue('metadata', 'lib:ITEM1', { title: 'Second edit' }, 4000); // resets dueAt to 6000

	queue._now = () => 5001; // past the first dueAt, but not the reset one
	queue._tick();
	await new Promise(r => setImmediate(r));
	assert.strictEqual(dispatchedBatches.length, 0);

	queue._now = () => 6001;
	queue._tick();
	await new Promise(r => setImmediate(r));

	assert.strictEqual(dispatchedBatches.length, 1);
	assert.strictEqual(dispatchedBatches[0].length, 1);
	assert.strictEqual(dispatchedBatches[0][0].payload.title, 'Second edit');
});

test('a failing dispatch is retried with exponential backoff and logs a warning', async () => {
	const { queue, warnings } = loadTaskQueue();
	let attempts = 0;
	queue._now = () => 1000;
	queue.registerDispatcher('metadata', async () => {
		attempts++;
		throw new Error('network down');
	});
	queue.enqueue('metadata', 'lib:ITEM1', { title: 'A' }, 4000);

	queue._now = () => 5001;
	queue._tick();
	await new Promise(r => setImmediate(r));
	assert.strictEqual(attempts, 1);
	assert.strictEqual(warnings.length, 1);
	assert.match(warnings[0], /attempt 1, retry in 5000ms/);

	// Retrying before the 5000ms backoff elapses must not re-dispatch.
	queue._now = () => 5500;
	queue._tick();
	await new Promise(r => setImmediate(r));
	assert.strictEqual(attempts, 1);

	// Once the backoff window passes, it retries and backs off further.
	queue._now = () => 10002;
	queue._tick();
	await new Promise(r => setImmediate(r));
	assert.strictEqual(attempts, 2);
	assert.match(warnings[1], /attempt 2, retry in 10000ms/);
});

test('the failure count resets to 0 after a successful dispatch', async () => {
	const { queue, warnings } = loadTaskQueue();
	let shouldFail = true;
	queue._now = () => 1000;
	queue.registerDispatcher('metadata', async (/** @type {any[]} */ batch) => {
		if (shouldFail) throw new Error('boom');
		return { succeededKeys: new Set(batch.map(t => t.key)) };
	});
	queue.enqueue('metadata', 'lib:ITEM1', { title: 'A' }, 4000);
	queue._now = () => 5001;
	queue._tick();
	await new Promise(r => setImmediate(r));
	assert.match(warnings[0], /attempt 1/);

	shouldFail = false;
	queue._now = () => 10002; // past the first backoff window
	queue._tick();
	await new Promise(r => setImmediate(r));

	// A new failure after the reset should start again at attempt 1.
	shouldFail = true;
	queue.enqueue('metadata', 'lib:ITEM2', { title: 'B' }, 4000);
	queue._now = () => 14003;
	queue._tick();
	await new Promise(r => setImmediate(r));
	assert.match(warnings[1], /attempt 1/);
});

test('escalates to console.error once the stuck threshold is reached', async () => {
	const { queue, warnings, errors } = loadTaskQueue();
	queue._now = () => 0;
	queue.registerDispatcher('metadata', async () => { throw new Error('down'); });
	queue.enqueue('metadata', 'lib:ITEM1', { title: 'A' }, 1000);

	let now = 1001;
	for (let i = 0; i < 5; i++) {
		queue._now = () => now;
		queue._tick();
		await new Promise(r => setImmediate(r));
		now += 5 * 60 * 1000 + 1; // past the capped 5-minute backoff each time
	}

	assert.strictEqual(warnings.length, 4); // attempts 1-4 are warnings
	assert.strictEqual(errors.length, 1); // attempt 5 escalates
	assert.match(errors[0], /stuck after 5 attempts/);
});

test('a dispatch failure in one type does not block a ready task of another type', async () => {
	const { queue } = loadTaskQueue();
	/** @type {any[]} */
	const attachmentCalls = [];
	queue._now = () => 1000;
	queue.registerDispatcher('metadata', async () => { throw new Error('down'); });
	queue.registerDispatcher('attachment', async (/** @type {any[]} */ batch) => {
		attachmentCalls.push(batch);
		return { succeededKeys: new Set(batch.map(t => t.key)) };
	});
	queue.enqueue('metadata', 'lib:ITEM1', {}, 1000);
	queue.enqueue('attachment', 'lib:ATT1', {}, 1000);

	queue._now = () => 2001;
	queue._tick(); // dispatches 'metadata' first (Map insertion order) — it fails
	await new Promise(r => setImmediate(r));
	queue._tick(); // next tick: 'metadata' is backing off, so 'attachment' gets its turn
	await new Promise(r => setImmediate(r));

	assert.strictEqual(attachmentCalls.length, 1);
});

test('stop() clears the pending queue and the running timer', () => {
	const { queue } = loadTaskQueue();
	queue.enqueue('metadata', 'lib:ITEM1', {}, 4000);
	queue.start();
	assert.notStrictEqual(queue._timer, null);

	queue.stop();

	assert.strictEqual(queue._timer, null);
	assert.strictEqual(queue._pending.size, 0);
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `node --test plugin/test/task_queue.test.js`
Expected: FAIL — `plugin/src/task_queue.js` doesn't exist yet (`ENOENT`), all 8 tests fail.

- [ ] **Step 3: Write `plugin/src/task_queue.js`**

```js
// Generic debounced, batched, retrying task queue.
//
// Type-agnostic: callers register a dispatcher function per task `type`;
// the engine only handles debounce timing, batching, sequential dispatch,
// and per-type exponential backoff. This lets a future task type (e.g.
// attachment sync) reuse the engine unchanged — see docs/superpowers/specs/
// 2026-07-19-live-metadata-sync-design.md §8.

// @ts-check

const TICK_INTERVAL_MS = 1000;
const MAX_BATCH_SIZE = 50;
const BASE_BACKOFF_MS = 5000;
const MAX_BACKOFF_MS = 5 * 60 * 1000;
const STUCK_THRESHOLD = 5;

/**
 * @typedef {Object} Task
 * @property {string} type
 * @property {string} key
 * @property {any} payload
 * @property {number} [dueAt]
 */

/**
 * @typedef {Object} DispatchResult
 * @property {Set<string>} succeededKeys - Keys from the batch that were
 *   successfully processed and should be removed from the queue. Keys not
 *   in this set stay queued and are retried on a later dispatch attempt.
 */

var TaskQueue = {
	/** @type {Map<string, Task>} "type:key" -> pending task, not yet due */
	_pending: new Map(),

	/** @type {Map<string, Map<string, Task>>} type -> (key -> ready task) */
	_ready: new Map(),

	/** @type {Map<string, function(Task[]): Promise<DispatchResult>>} */
	_dispatchers: new Map(),

	/** @type {Map<string, number>} type -> consecutive failure count */
	_failureCount: new Map(),

	/** @type {Map<string, number>} type -> timestamp before which dispatch is skipped */
	_nextAttemptAt: new Map(),

	/** @type {any} */
	_timer: null,

	/** @type {boolean} */
	_inFlight: false,

	/**
	 * Current time, in ms. Overridable in tests for deterministic debounce/
	 * backoff assertions without waiting on real timers.
	 * @returns {number}
	 */
	_now() {
		return Date.now();
	},

	/** Start the heartbeat tick. Idempotent. @returns {void} */
	start() {
		if (this._timer) return;
		this._timer = setInterval(() => this._tick(), TICK_INTERVAL_MS);
	},

	/** Stop the heartbeat and discard all pending/ready state. @returns {void} */
	stop() {
		if (this._timer) {
			clearInterval(this._timer);
			this._timer = null;
		}
		this._pending.clear();
		this._ready.clear();
	},

	/**
	 * Register the dispatcher function for a task type. Replaces any
	 * previously registered dispatcher for the same type.
	 * @param {string} type
	 * @param {function(Task[]): Promise<DispatchResult>} fn
	 * @returns {void}
	 */
	registerDispatcher(type, fn) {
		this._dispatchers.set(type, fn);
	},

	/**
	 * Queue (or re-queue) a task. Repeated calls with the same type+key
	 * overwrite the payload and reset the debounce window — this is how
	 * rapid successive edits to the same item get coalesced into one
	 * dispatch.
	 * @param {string} type
	 * @param {string} key
	 * @param {any} payload
	 * @param {number} debounceMs
	 * @returns {void}
	 */
	enqueue(type, key, payload, debounceMs) {
		this._pending.set(`${type}:${key}`, { type, key, payload, dueAt: this._now() + debounceMs });
	},

	/**
	 * Promote due tasks from _pending to _ready, then attempt a dispatch
	 * if none is currently in flight. Called every TICK_INTERVAL_MS by
	 * start(), and can be called directly in tests.
	 * @returns {void}
	 */
	_tick() {
		const now = this._now();
		for (const [pendingKey, task] of this._pending) {
			if (task.dueAt !== undefined && task.dueAt <= now) {
				this._pending.delete(pendingKey);
				if (!this._ready.has(task.type)) this._ready.set(task.type, new Map());
				/** @type {Map<string, Task>} */ (this._ready.get(task.type)).set(task.key, task);
			}
		}
		if (!this._inFlight) this._dispatchNext();
	},

	/**
	 * Dispatch one batch for the first ready type whose backoff window
	 * has elapsed and that has a registered dispatcher. At most one
	 * request is ever in flight across all types; a failure in one type
	 * does not block another (see per-type _nextAttemptAt/_failureCount).
	 * @returns {Promise<void>}
	 */
	async _dispatchNext() {
		const now = this._now();
		for (const [type, tasks] of this._ready) {
			if (tasks.size === 0) continue;
			if ((this._nextAttemptAt.get(type) || 0) > now) continue;
			const dispatcher = this._dispatchers.get(type);
			if (!dispatcher) continue;

			const batch = [...tasks.values()].slice(0, MAX_BATCH_SIZE);
			this._inFlight = true;
			try {
				const result = await dispatcher(batch);
				for (const key of result.succeededKeys) tasks.delete(key);
				this._failureCount.set(type, 0);
				this._nextAttemptAt.delete(type);
			} catch (e) {
				const attempt = (this._failureCount.get(type) || 0) + 1;
				this._failureCount.set(type, attempt);
				const delay = Math.min(BASE_BACKOFF_MS * 2 ** (attempt - 1), MAX_BACKOFF_MS);
				this._nextAttemptAt.set(type, this._now() + delay);
				this._logDispatchFailure(type, batch, attempt, delay, /** @type {Error} */ (e));
			} finally {
				this._inFlight = false;
			}
			return; // one type per tick — remaining ready types get the next tick
		}
	},

	/**
	 * @param {string} type
	 * @param {Task[]} batch
	 * @param {number} attempt
	 * @param {number} delayMs
	 * @param {Error} err
	 * @returns {void}
	 */
	_logDispatchFailure(type, batch, attempt, delayMs, err) {
		const keys = batch.map(t => t.key).join(', ');
		if (attempt >= STUCK_THRESHOLD) {
			console.error(`TaskQueue: ${type} dispatch stuck after ${attempt} attempts, items: [${keys}]`);
		} else {
			console.warn(`TaskQueue: ${type} dispatch failed (attempt ${attempt}, retry in ${delayMs}ms): ${err.message} [${keys}]`);
		}
	},
};
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `node --test plugin/test/task_queue.test.js`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add plugin/src/task_queue.js plugin/test/task_queue.test.js
git commit -m "$(cat <<'EOF'
feat(plugin): add generic debounced/batched/retrying TaskQueue engine

Type-agnostic engine for the upcoming live metadata sync feature:
debounces per-key edits, batches ready tasks, dispatches at most one
request at a time, and backs off exponentially per type on failure so
one stuck task type can't block another. No Zotero dependency.
EOF
)"
```

---

### Task 3: Plugin — load `task_queue.js` at plugin startup

**Files:**
- Modify: `plugin/src/bootstrap.js:37-42`

- [ ] **Step 1: Add the script load, before `zotero-rag.js`**

In `plugin/src/bootstrap.js`, replace:

```js
	// Load Zotero Plugin Toolkit bundle
	Services.scriptloader.loadSubScript(rootURI + 'toolkit.bundle.js');

	// Load main plugin script and preferences pane logic
	Services.scriptloader.loadSubScript(rootURI + 'zotero-rag.js');
	Services.scriptloader.loadSubScript(rootURI + 'preferences.js');
```

with:

```js
	// Load Zotero Plugin Toolkit bundle
	Services.scriptloader.loadSubScript(rootURI + 'toolkit.bundle.js');

	// Load the generic task queue before zotero-rag.js, which registers a
	// metadata dispatcher on it and starts it during init().
	Services.scriptloader.loadSubScript(rootURI + 'task_queue.js');

	// Load main plugin script and preferences pane logic
	Services.scriptloader.loadSubScript(rootURI + 'zotero-rag.js');
	Services.scriptloader.loadSubScript(rootURI + 'preferences.js');
```

- [ ] **Step 2: Manually verify the load order doesn't break plugin startup**

There's no automated test for `bootstrap.js` (it only runs inside the Zotero chrome process). Verification happens in Task 8's manual end-to-end check, once `zotero-rag.js` actually references `TaskQueue` (Tasks 6-7). For now, just confirm the file parses:

Run: `node --check plugin/src/bootstrap.js`
Expected: no output (exit code 0)

- [ ] **Step 3: Commit**

```bash
git add plugin/src/bootstrap.js
git commit -m "feat(plugin): load task_queue.js at plugin startup"
```

---

### Task 4: Plugin — shared `getBackendLibraryId()` helper; fix delete-notifier bug

**Files:**
- Modify: `plugin/src/zotero-rag.js:686-689` (add helper after `getCurrentZoteroUserId`), `plugin/src/zotero-rag.js:198-212` (delete-notifier), `plugin/src/zotero-rag.js:960-981` (`getCurrentLibrary`), `plugin/src/zotero-rag.js:1811-1820` (`_scanUnavailableCount`)
- Modify: `plugin/test/zotero-rag.test.js` (extend `loadPlugin` helper signature)
- Test: `plugin/test/zotero-rag.test.js` (new tests)

**Context:** `zotero-rag.js:198-212`'s delete-notifier sends the raw internal Zotero `libraryID` in its DELETE URL, unlike every other endpoint call in the plugin, which sends `u{userId}` for the personal library or the numeric zotero.org group ID for a group library. This means deletions likely fail silently for any group library or unsynced personal library. The mapping logic already exists, duplicated, in `getCurrentLibrary()` and `_scanUnavailableCount()` — this task extracts it into one method and fixes the notifier to use it. The next tasks (6-7) need this same helper for the new modify-notifier, so it must exist before them.

- [ ] **Step 1: Extend the `loadPlugin` test helper to accept extra vm-context globals**

In `plugin/test/zotero-rag.test.js`, replace:

```js
function loadPlugin(zoteroStub, ioUtilsStub, pathUtilsStub) {
	const src = fs.readFileSync(SOURCE_PATH, 'utf8');
	const context = { Zotero: zoteroStub, IOUtils: ioUtilsStub, PathUtils: pathUtilsStub, console };
	vm.createContext(context);
	vm.runInContext(src, context, { filename: 'zotero-rag.js' });
	// `class ZoteroRAGPlugin` is a top-level class declaration, not a `var` —
	// it lives in the context's global lexical environment, not as a property
	// on the context object itself. Pull it out with a second script eval in
	// the same context (lexical bindings persist across runInContext calls on
	// the same context object).
	const ZoteroRAGPluginClass = vm.runInContext('ZoteroRAGPlugin', context);
	return new ZoteroRAGPluginClass();
}
```

with (adds an optional 4th `extra` param, merged into the vm context — used by later tasks to stub `TaskQueue` and `fetch`):

```js
/**
 * @param {any} zoteroStub
 * @param {any} ioUtilsStub
 * @param {any} pathUtilsStub
 * @param {Record<string, any>} [extra] - Extra globals to add to the vm context (e.g. TaskQueue, fetch)
 * @returns {any} a new ZoteroRAGPlugin instance
 */
function loadPlugin(zoteroStub, ioUtilsStub, pathUtilsStub, extra = {}) {
	const src = fs.readFileSync(SOURCE_PATH, 'utf8');
	const context = { Zotero: zoteroStub, IOUtils: ioUtilsStub, PathUtils: pathUtilsStub, console, ...extra };
	vm.createContext(context);
	vm.runInContext(src, context, { filename: 'zotero-rag.js' });
	// `class ZoteroRAGPlugin` is a top-level class declaration, not a `var` —
	// it lives in the context's global lexical environment, not as a property
	// on the context object itself. Pull it out with a second script eval in
	// the same context (lexical bindings persist across runInContext calls on
	// the same context object).
	const ZoteroRAGPluginClass = vm.runInContext('ZoteroRAGPlugin', context);
	return new ZoteroRAGPluginClass();
}
```

- [ ] **Step 2: Write the failing tests**

Append to `plugin/test/zotero-rag.test.js`:

```js
test('getBackendLibraryId returns "u{userId}" for the personal library', () => {
	const zotero = {
		Libraries: { userLibraryID: 1 },
		Users: { getCurrentUserID: () => 12345 },
		Groups: { getByLibraryID: () => null },
	};
	const plugin = loadPlugin(zotero, {}, {});

	assert.strictEqual(plugin.getBackendLibraryId(1), 'u12345');
});

test('getBackendLibraryId returns the numeric group id for a group library', () => {
	const zotero = {
		Libraries: { userLibraryID: 1 },
		Users: { getCurrentUserID: () => 12345 },
		Groups: { getByLibraryID: (/** @type {number} */ id) => (id === 7 ? { id: 999 } : null) },
	};
	const plugin = loadPlugin(zotero, {}, {});

	assert.strictEqual(plugin.getBackendLibraryId(7), '999');
});

test('getBackendLibraryId falls back to the raw libraryID when unsynced and not a group', () => {
	const zotero = {
		Libraries: { userLibraryID: 1 },
		Users: { getCurrentUserID: () => null },
		Groups: { getByLibraryID: () => null },
	};
	const plugin = loadPlugin(zotero, {}, {});

	assert.strictEqual(plugin.getBackendLibraryId(1), '1');
});

test('the item-delete notifier maps the internal libraryID to the backend library_id in the DELETE URL', () => {
	/** @type {string[]} */
	const deletedUrls = [];
	/** @type {any} */
	let capturedObserver;
	const zotero = {
		Libraries: { userLibraryID: 1 },
		Users: { getCurrentUserID: () => 12345 },
		Groups: { getByLibraryID: (/** @type {number} */ id) => (id === 7 ? { id: 999 } : null) },
		Notifier: {
			registerObserver: (/** @type {any} */ observer) => { capturedObserver = observer; return 'nid'; },
			unregisterObserver: () => {},
		},
		Prefs: { get: () => null },
	};
	const fetchStub = (/** @type {string} */ url) => { deletedUrls.push(url); return Promise.resolve({ ok: true }); };
	const plugin = loadPlugin(zotero, {}, {}, { fetch: fetchStub });
	plugin.init({ id: 'x', version: '1', rootURI: 'chrome://x/' });

	capturedObserver.notify('delete', 'item', [1], { 1: { libraryID: 1, key: 'ITEM1' } });
	capturedObserver.notify('delete', 'item', [2], { 2: { libraryID: 7, key: 'ITEM2' } });

	assert.strictEqual(deletedUrls[0], 'http://localhost:8119/api/libraries/u12345/items/ITEM1/chunks');
	assert.strictEqual(deletedUrls[1], 'http://localhost:8119/api/libraries/999/items/ITEM2/chunks');
});
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `node --test plugin/test/zotero-rag.test.js`
Expected: FAIL — `getBackendLibraryId` doesn't exist (`TypeError: plugin.getBackendLibraryId is not a function`); the delete-notifier test fails because the URL still contains the raw `libraryID` (`1` and `7`) instead of `u12345`/`999`.

- [ ] **Step 4: Add the helper; fix the three call sites**

In `plugin/src/zotero-rag.js`, add the helper right after `getCurrentZoteroUserId()` (after line 689):

```js
	/**
	 * Map a Zotero-internal numeric libraryID to the backend's library_id
	 * string convention: "u{zoteroUserId}" for the personal library, or the
	 * numeric zotero.org group ID (as a string) for a group library. Falls
	 * back to the raw libraryID (stringified) if unsynced/not a group.
	 * @param {number} libraryID
	 * @returns {string}
	 */
	getBackendLibraryId(libraryID) {
		if (libraryID === Zotero.Libraries.userLibraryID) {
			const userId = this.getCurrentZoteroUserId();
			return userId ? `u${userId}` : String(libraryID);
		}
		const group = Zotero.Groups.getByLibraryID(libraryID);
		return group ? String(group.id) : String(libraryID);
	}
```

Replace the delete-notifier body (lines 198-212):

```js
		this._notifierID = Zotero.Notifier.registerObserver(
			{
				notify: (/** @type {string} */ event, /** @type {string} */ type, /** @type {number[]} */ ids, /** @type {Record<number, {libraryID: number, key: string}>} */ extraData) => {
					if (event !== 'delete' || type !== 'item') return;
					for (const id of ids) {
						const { libraryID, key } = extraData[id] || {};
						if (!libraryID || !key) continue;
						const url = `${this.backendURL}/api/libraries/${libraryID}/items/${key}/chunks`;
						fetch(url, { method: 'DELETE', headers: this.getAuthHeaders() })
							.catch(e => console.warn(`Failed to delete chunks for item ${key}: ${e.message}`));
					}
				}
			},
			['item']
		);
```

with:

```js
		this._notifierID = Zotero.Notifier.registerObserver(
			{
				notify: (/** @type {string} */ event, /** @type {string} */ type, /** @type {number[]} */ ids, /** @type {Record<number, {libraryID: number, key: string}>} */ extraData) => {
					if (event !== 'delete' || type !== 'item') return;
					for (const id of ids) {
						const { libraryID, key } = extraData[id] || {};
						if (!libraryID || !key) continue;
						const backendLibraryId = this.getBackendLibraryId(libraryID);
						const url = `${this.backendURL}/api/libraries/${backendLibraryId}/items/${key}/chunks`;
						fetch(url, { method: 'DELETE', headers: this.getAuthHeaders() })
							.catch(e => console.warn(`Failed to delete chunks for item ${key}: ${e.message}`));
					}
				}
			},
			['item']
		);
```

Replace `getCurrentLibrary()` (lines 960-981):

```js
	getCurrentLibrary() {
		const zoteroPane = Zotero.getActiveZoteroPane();
		if (!zoteroPane) return null;

		const libraryID = zoteroPane.getSelectedLibraryID();
		if (!libraryID) return null;

		// For group libraries, return the group ID instead of library ID
		const library = Zotero.Libraries.get(libraryID);
		// @ts-ignore - libraryType exists on ZoteroLibrary at runtime
		if (library && library.libraryType === 'group') {
			// Get the group associated with this library
			const group = Zotero.Groups.getByLibraryID(libraryID);
			if (group) {
				return String(group.id);  // Return group ID for backend
			}
		}

		// For user library, use "u{zoteroUserId}" when synced; fall back to raw ID on localhost-no-registration
		const userId = this.getCurrentZoteroUserId();
		return userId ? `u${userId}` : String(libraryID);
	}
```

with:

```js
	getCurrentLibrary() {
		const zoteroPane = Zotero.getActiveZoteroPane();
		if (!zoteroPane) return null;

		const libraryID = zoteroPane.getSelectedLibraryID();
		if (!libraryID) return null;

		return this.getBackendLibraryId(libraryID);
	}
```

Replace the inline mapping in `_scanUnavailableCount()` (lines 1811-1820):

```js
		try {
			// Map Zotero internal library ID → backend library ID to read the pref.
			let backendId = null;
			if (libraryID === Zotero.Libraries.userLibraryID) {
				const userId = this.getCurrentZoteroUserId();
				backendId = userId ? `u${userId}` : String(libraryID);
			} else {
				const group = Zotero.Groups.getAll().find(/** @param {any} g */ g => g.libraryID === libraryID);
				if (group) backendId = String(group.id);
			}
```

with:

```js
		try {
			// Map Zotero internal library ID → backend library ID to read the pref.
			const backendId = this.getBackendLibraryId(libraryID);
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `node --test plugin/test/zotero-rag.test.js`
Expected: PASS (all tests, including the 3 pre-existing ones and the 4 new ones)

- [ ] **Step 6: Run the full plugin test suite to check for regressions**

Run: `node --test plugin/test/*.test.js`
Expected: PASS (all tests across all 4 test files)

- [ ] **Step 7: Commit**

```bash
git add plugin/src/zotero-rag.js plugin/test/zotero-rag.test.js
git commit -m "$(cat <<'EOF'
fix(plugin): map libraryID to backend library_id in delete-notifier

The delete-item notifier sent the raw internal Zotero libraryID in
its DELETE URL instead of the backend's expected "u{userId}"/group-id
format used everywhere else, so deletions likely failed silently for
any group library or unsynced personal library. Extracts the
existing (previously duplicated 2x) mapping logic into a shared
getBackendLibraryId() helper, used here and by getCurrentLibrary()/
_scanUnavailableCount(). This helper is also needed by the upcoming
modify-notifier for live metadata sync.
EOF
)"
```

---

### Task 5: Plugin — shared `_extractAuthors`/`_extractYear`/`_extractTags`; dedupe from `remote_indexer.js`; fix its metadata-sync payload

**Files:**
- Modify: `plugin/src/zotero-rag.js` (add 3 methods after `getBackendLibraryId`)
- Modify: `plugin/src/remote_indexer.js:827-828`, `:1122-1139`, `:1181-1182`, `:1267,1273`, `:1284-1317` (call sites + remove duplicated definitions)
- Test: `plugin/test/zotero-rag.test.js` (new tests)

**Context:** These two extraction helpers currently live in `remote_indexer.js`, which is only loaded into an on-demand dialog window — unusable from the plugin-lifetime notifier being extended in Task 6. Moving them to `zotero-rag.js` (always loaded) and having `remote_indexer.js` call `Zotero.ZoteroRAG._extractAuthors(...)` instead (that global is set by `bootstrap.js:44`, already loaded before any dialog opens) avoids two copies of the same regex/creator-filtering logic drifting apart.

This task also fixes a correctness gap: `_sendMetadataUpdates` (the existing schema-migration caller of `/api/index/items/metadata`) doesn't send `tags`/`item_version`/`zotero_modified` at all. After Task 1's backend change, that's harmless on its own (the endpoint's conditional-field pattern skips absent fields, per Task 1's second test) — but it's still an unnecessary permanent gap in that call site's payload now that the endpoint supports these fields, since it already has direct access to the same Zotero item object used for `title`/`authors`/`year`. Fixing it here keeps parity between the two callers of this endpoint.

- [ ] **Step 1: Write the failing tests**

Append to `plugin/test/zotero-rag.test.js`:

```js
test('_extractAuthors returns "First Last" for authors and editors, skipping other creator types', () => {
	const zotero = {
		Libraries: { userLibraryID: 1 },
		CreatorTypes: { getID: (/** @type {string} */ name) => (/** @type {Record<string, number>} */ ({ author: 1, editor: 2, contributor: 3 }))[name] },
	};
	const plugin = loadPlugin(zotero, {}, {});
	const item = {
		getCreators: () => [
			{ creatorTypeID: 1, firstName: 'Jane', lastName: 'Doe' },
			{ creatorTypeID: 3, firstName: 'Ignored', lastName: 'Contributor' },
			{ creatorTypeID: 2, firstName: '', lastName: 'Smith' },
		],
	};

	assert.deepStrictEqual(plugin._extractAuthors(item), ['Jane Doe', 'Smith']);
});

test('_extractYear extracts a 4-digit year from the date field', () => {
	const plugin = loadPlugin({ Libraries: { userLibraryID: 1 } }, {}, {});
	const item = { getField: (/** @type {string} */ f) => (f === 'date' ? 'March 3, 2021' : '') };

	assert.strictEqual(plugin._extractYear(item), 2021);
});

test('_extractYear returns null when there is no parseable year', () => {
	const plugin = loadPlugin({ Libraries: { userLibraryID: 1 } }, {}, {});
	const item = { getField: () => '' };

	assert.strictEqual(plugin._extractYear(item), null);
});

test('_extractTags maps Zotero tag objects to a plain string array, dropping empty tags', () => {
	const plugin = loadPlugin({ Libraries: { userLibraryID: 1 } }, {}, {});
	const item = { getTags: () => [{ tag: 'Law', type: 0 }, { tag: 'Automatic', type: 1 }, { tag: '' }] };

	assert.deepStrictEqual(plugin._extractTags(item), ['Law', 'Automatic']);
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `node --test plugin/test/zotero-rag.test.js`
Expected: FAIL — `plugin._extractAuthors is not a function` (and likewise for `_extractYear`/`_extractTags`)

- [ ] **Step 3: Add the three methods to `zotero-rag.js`**

Add right after `getBackendLibraryId()` (the method added in Task 4):

```js
	/**
	 * Extract "First Last" display names for authors and editors of an item.
	 * @param {any} item - Zotero item
	 * @returns {Array<string>}
	 */
	_extractAuthors(item) {
		if (!item || !item.getCreators) return [];
		try {
			return item.getCreators()
				.filter((/** @type {any} */ c) => c.creatorTypeID === Zotero.CreatorTypes.getID('author') ||
				             c.creatorTypeID === Zotero.CreatorTypes.getID('editor'))
				.map((/** @type {any} */ c) => `${c.firstName || ''} ${c.lastName || ''}`.trim())
				.filter(Boolean);
		} catch (_) {
			return [];
		}
	}

	/**
	 * Extract a 4-digit publication year from an item's date field.
	 * @param {any} item - Zotero item
	 * @returns {number|null}
	 */
	_extractYear(item) {
		if (!item || !item.getField) return null;
		try {
			const dateStr = item.getField('date') || '';
			const m = dateStr.match(/\b(19|20)\d{2}\b/);
			return m ? parseInt(m[0], 10) : null;
		} catch (_) {
			return null;
		}
	}

	/**
	 * Extract tag names (manual and automatic) as a plain string array.
	 * @param {any} item - Zotero item
	 * @returns {Array<string>}
	 */
	_extractTags(item) {
		if (!item || !item.getTags) return [];
		try {
			return item.getTags().map((/** @type {any} */ t) => t.tag).filter(Boolean);
		} catch (_) {
			return [];
		}
	}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `node --test plugin/test/zotero-rag.test.js`
Expected: PASS (all tests, including the 4 new ones)

- [ ] **Step 5: Remove the duplicated definitions from `remote_indexer.js` and update its call sites**

In `plugin/src/remote_indexer.js`, remove the duplicated `_extractAuthors`/`_extractYear` definitions (lines 1284-1317 — the two commented method definitions right before the object's closing `};`):

```js
	/**
	 * Extract author names from a Zotero item.
	 * @param {any} item
	 * @returns {Array<string>}
	 */
	_extractAuthors(item) {
		if (!item || !item.getCreators) return [];
		try {
			return item.getCreators()
				.filter(c => c.creatorTypeID === Zotero.CreatorTypes.getID('author') ||
				             c.creatorTypeID === Zotero.CreatorTypes.getID('editor'))
				.map(c => `${c.firstName || ''} ${c.lastName || ''}`.trim())
				.filter(Boolean);
		} catch (_) {
			return [];
		}
	},

	/**
	 * Extract the publication year from a Zotero item.
	 * @param {any} item
	 * @returns {number|null}
	 */
	_extractYear(item) {
		if (!item || !item.getField) return null;
		try {
			const dateStr = item.getField('date') || '';
			const m = dateStr.match(/\b(19|20)\d{2}\b/);
			return m ? parseInt(m[0], 10) : null;
		} catch (_) {
			return null;
		}
	},
};
```

Replace with just the closing brace:

```js
};
```

Update the 4 call sites. At line 827-828 (in the document-upload metadata builder):

```js
			authors: this._extractAuthors(parent),
			year: this._extractYear(parent),
```

becomes:

```js
			authors: Zotero.ZoteroRAG._extractAuthors(parent),
			year: Zotero.ZoteroRAG._extractYear(parent),
```

At line 1181-1182 (in `_uploadAbstract`):

```js
			authors: this._extractAuthors(item),
			year: this._extractYear(item),
```

becomes:

```js
			authors: Zotero.ZoteroRAG._extractAuthors(item),
			year: Zotero.ZoteroRAG._extractYear(item),
```

At line 1267 and 1273 (in `_formatCitationLabel`):

```js
		const authors = this._extractAuthors(item);
```
becomes:
```js
		const authors = Zotero.ZoteroRAG._extractAuthors(item);
```
and:
```js
		const year = this._extractYear(item);
```
becomes:
```js
		const year = Zotero.ZoteroRAG._extractYear(item);
```

- [ ] **Step 6: Fix `_sendMetadataUpdates`'s payload to include `tags`/`item_version`/`zotero_modified`**

At lines 1122-1139, replace:

```js
	async _sendMetadataUpdates({ statuses, attachments, libraryId, backendURL, getAuthHeaders, log, signal, onProgress }) {
		const BATCH_SIZE = 100;
		/** @type {Array<{item_key: string, title: string|null, authors: string[], year: number|null, item_type: string|null}>} */
		const items = [];

		for (const status of statuses) {
			const att = attachments.find(a => a.item_key === status.item_key);
			if (!att) continue;
			const parent = att.parentItem || att.zoteroItem;
			if (parent && parent.loadAllData) await parent.loadAllData();
			items.push({
				item_key: status.item_key,
				title: parent && parent.getField ? (parent.getField('title') || null) : null,
				authors: parent ? this._extractAuthors(parent) : [],
				year: parent ? this._extractYear(parent) : null,
				item_type: parent ? (parent.itemType || null) : null,
			});
		}
```

with:

```js
	async _sendMetadataUpdates({ statuses, attachments, libraryId, backendURL, getAuthHeaders, log, signal, onProgress }) {
		const BATCH_SIZE = 100;
		/** @type {Array<{item_key: string, title: string|null, authors: string[], tags: string[], year: number|null, item_type: string|null, item_version: number, zotero_modified: string}>} */
		const items = [];

		for (const status of statuses) {
			const att = attachments.find(a => a.item_key === status.item_key);
			if (!att) continue;
			const parent = att.parentItem || att.zoteroItem;
			if (parent && parent.loadAllData) await parent.loadAllData();
			items.push({
				item_key: status.item_key,
				title: parent && parent.getField ? (parent.getField('title') || null) : null,
				authors: parent ? Zotero.ZoteroRAG._extractAuthors(parent) : [],
				tags: parent ? Zotero.ZoteroRAG._extractTags(parent) : [],
				year: parent ? Zotero.ZoteroRAG._extractYear(parent) : null,
				item_type: parent ? (parent.itemType || null) : null,
				item_version: parent ? (parent.version || 0) : 0,
				zotero_modified: parent ? (parent.dateModified || new Date().toISOString()) : new Date().toISOString(),
			});
		}
```

- [ ] **Step 7: Run the full plugin test suite**

Run: `node --test plugin/test/*.test.js`
Expected: PASS (all tests across all 4 files — `remote_indexer.test.js`'s 3 tests don't exercise the changed functions, per the exploration done during planning, so they're unaffected)

- [ ] **Step 8: Commit**

```bash
git add plugin/src/zotero-rag.js plugin/src/remote_indexer.js plugin/test/zotero-rag.test.js
git commit -m "$(cat <<'EOF'
refactor(plugin): move metadata extraction helpers to plugin-lifetime scope

_extractAuthors/_extractYear lived only in remote_indexer.js, which is
loaded on demand into a dialog window — unusable from the always-on
item notifier the live metadata sync feature needs. Moves them (plus
a new _extractTags) to zotero-rag.js, which is loaded at plugin
startup and already exposed as Zotero.ZoteroRAG, and updates
remote_indexer.js's 4 call sites to use that instead of duplicating
the logic.

Also fixes _sendMetadataUpdates (the existing schema-migration caller
of /api/index/items/metadata) to send tags/item_version/
zotero_modified now that the endpoint accepts them, keeping it at
parity with the new live-push caller added next.
EOF
)"
```

---

### Task 6: Plugin — notifier `modify` branch enqueues metadata tasks

**Files:**
- Modify: `plugin/src/zotero-rag.js:198-213` (notifier registration)
- Test: `plugin/test/zotero-rag.test.js` (new tests)

- [ ] **Step 1: Write the failing tests**

Append to `plugin/test/zotero-rag.test.js`:

```js
test('the item-modify notifier enqueues a metadata task for top-level regular items', () => {
	/** @type {any[]} */
	const enqueued = [];
	/** @type {any} */
	let capturedObserver;
	const fakeItem = {
		key: 'ITEM1', libraryID: 1, version: 7, dateModified: '2026-01-01T00:00:00Z',
		itemType: 'journalArticle',
		isRegularItem: () => true,
		getField: (/** @type {string} */ f) => (f === 'title' ? 'A Title' : ''),
		getCreators: () => [],
		getTags: () => [{ tag: 'Law' }],
	};
	const zotero = {
		Libraries: { userLibraryID: 1 },
		Users: { getCurrentUserID: () => 12345 },
		Groups: { getByLibraryID: () => null },
		Items: { get: (/** @type {number} */ id) => (id === 42 ? fakeItem : null) },
		Notifier: {
			registerObserver: (/** @type {any} */ observer) => { capturedObserver = observer; return 'nid'; },
			unregisterObserver: () => {},
		},
		Prefs: { get: () => null },
	};
	const taskQueueStub = {
		enqueue: (/** @type {any[]} */ ...args) => enqueued.push(args),
		start: () => {},
		registerDispatcher: () => {},
	};
	const plugin = loadPlugin(zotero, {}, {}, { TaskQueue: taskQueueStub });
	plugin.init({ id: 'x', version: '1', rootURI: 'chrome://x/' });

	capturedObserver.notify('modify', 'item', [42], {});

	assert.strictEqual(enqueued.length, 1);
	const [type, key, payload, debounceMs] = enqueued[0];
	assert.strictEqual(type, 'metadata');
	assert.strictEqual(key, 'u12345:ITEM1');
	assert.strictEqual(payload.title, 'A Title');
	assert.deepStrictEqual(payload.tags, ['Law']);
	assert.strictEqual(payload.item_version, 7);
	assert.strictEqual(debounceMs, 4000);
});

test('the item-modify notifier ignores non-regular items (attachments, notes)', () => {
	/** @type {any[]} */
	const enqueued = [];
	/** @type {any} */
	let capturedObserver;
	const fakeItem = { key: 'ATT1', libraryID: 1, isRegularItem: () => false };
	const zotero = {
		Libraries: { userLibraryID: 1 },
		Items: { get: () => fakeItem },
		Notifier: {
			registerObserver: (/** @type {any} */ observer) => { capturedObserver = observer; return 'nid'; },
			unregisterObserver: () => {},
		},
		Prefs: { get: () => null },
	};
	const taskQueueStub = { enqueue: (/** @type {any[]} */ ...args) => enqueued.push(args), start: () => {}, registerDispatcher: () => {} };
	const plugin = loadPlugin(zotero, {}, {}, { TaskQueue: taskQueueStub });
	plugin.init({ id: 'x', version: '1', rootURI: 'chrome://x/' });

	capturedObserver.notify('modify', 'item', [1], {});

	assert.strictEqual(enqueued.length, 0);
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `node --test plugin/test/zotero-rag.test.js`
Expected: FAIL — the notifier's `notify` callback returns early for any event other than `'delete'`, so nothing is enqueued for `'modify'` events yet.

- [ ] **Step 3: Add the `modify` branch**

Add a top-level constant right before `class ZoteroRAGPlugin {` (currently line 86):

```js
/**
 * How long to wait after the last edit to an item before pushing its
 * metadata to the backend — collapses rapid successive field edits
 * (e.g. typing a title, then adding tags) into one request.
 */
const METADATA_DEBOUNCE_MS = 4000;

```

Replace the notifier registration (lines 197-213):

```js
		// Watch for permanent item deletions and remove their indexed chunks from the backend.
		this._notifierID = Zotero.Notifier.registerObserver(
			{
				notify: (/** @type {string} */ event, /** @type {string} */ type, /** @type {number[]} */ ids, /** @type {Record<number, {libraryID: number, key: string}>} */ extraData) => {
					if (event !== 'delete' || type !== 'item') return;
					for (const id of ids) {
						const { libraryID, key } = extraData[id] || {};
						if (!libraryID || !key) continue;
						const backendLibraryId = this.getBackendLibraryId(libraryID);
						const url = `${this.backendURL}/api/libraries/${backendLibraryId}/items/${key}/chunks`;
						fetch(url, { method: 'DELETE', headers: this.getAuthHeaders() })
							.catch(e => console.warn(`Failed to delete chunks for item ${key}: ${e.message}`));
					}
				}
			},
			['item']
		);
	}
```

with:

```js
		// Watch for permanent item deletions (removes indexed chunks) and
		// metadata edits (pushes an updated payload) to the backend.
		this._notifierID = Zotero.Notifier.registerObserver(
			{
				notify: (/** @type {string} */ event, /** @type {string} */ type, /** @type {number[]} */ ids, /** @type {Record<number, {libraryID: number, key: string}>} */ extraData) => {
					if (type !== 'item') return;
					if (event === 'delete') {
						for (const id of ids) {
							const { libraryID, key } = extraData[id] || {};
							if (!libraryID || !key) continue;
							const backendLibraryId = this.getBackendLibraryId(libraryID);
							const url = `${this.backendURL}/api/libraries/${backendLibraryId}/items/${key}/chunks`;
							fetch(url, { method: 'DELETE', headers: this.getAuthHeaders() })
								.catch(e => console.warn(`Failed to delete chunks for item ${key}: ${e.message}`));
						}
					} else if (event === 'modify') {
						for (const id of ids) {
							const item = Zotero.Items.get(id);
							if (!item || !item.isRegularItem()) continue;
							const backendLibraryId = this.getBackendLibraryId(item.libraryID);
							const snapshot = {
								item_key: item.key,
								title: item.getField('title') || null,
								authors: this._extractAuthors(item),
								tags: this._extractTags(item),
								year: this._extractYear(item),
								item_type: item.itemType || null,
								item_version: item.version || 0,
								zotero_modified: item.dateModified || new Date().toISOString(),
							};
							TaskQueue.enqueue('metadata', `${backendLibraryId}:${item.key}`, snapshot, METADATA_DEBOUNCE_MS);
						}
					}
				}
			},
			['item']
		);
	}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `node --test plugin/test/zotero-rag.test.js`
Expected: PASS (all tests, including the 2 new ones)

- [ ] **Step 5: Run the full plugin test suite**

Run: `node --test plugin/test/*.test.js`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add plugin/src/zotero-rag.js plugin/test/zotero-rag.test.js
git commit -m "$(cat <<'EOF'
feat(plugin): enqueue metadata updates on item modify events

Extends the existing item notifier (previously delete-only) to also
watch 'modify' events for top-level regular items — attachments/notes/
annotations are skipped since they either lack these bibliographic
fields or can't be distinguished from a real content change without
out-of-scope logic. Builds a full metadata snapshot and hands it to
TaskQueue for debounced, batched, backed-off dispatch (wired up in the
next commit).
EOF
)"
```

---

### Task 7: Plugin — register the metadata dispatcher; wire `TaskQueue` start/stop into the plugin lifecycle

**Files:**
- Modify: `plugin/src/zotero-rag.js` (end of `init()`; `removeFromAllWindows()`)
- Test: `plugin/test/zotero-rag.test.js` (new tests)

- [ ] **Step 1: Write the failing tests**

Append to `plugin/test/zotero-rag.test.js`:

```js
test('the metadata dispatcher POSTs one request per library_id and reports succeeded keys', async () => {
	/** @type {Array<{url: string, init: any}>} */
	const fetchCalls = [];
	const fetchStub = async (/** @type {string} */ url, /** @type {any} */ init) => {
		fetchCalls.push({ url, init });
		return { ok: true, status: 200, json: async () => ({ updated_items: 1, updated_chunks: 1 }) };
	};
	/** @type {any} */
	let registeredDispatcher;
	const taskQueueStub = {
		start: () => {},
		registerDispatcher: (/** @type {string} */ type, /** @type {any} */ fn) => { if (type === 'metadata') registeredDispatcher = fn; },
	};
	const zotero = {
		Libraries: { userLibraryID: 1 },
		Notifier: { registerObserver: () => 'nid', unregisterObserver: () => {} },
		Prefs: { get: () => null },
	};
	const plugin = loadPlugin(zotero, {}, {}, { TaskQueue: taskQueueStub, fetch: fetchStub });
	plugin.init({ id: 'x', version: '1', rootURI: 'chrome://x/' });

	assert.strictEqual(typeof registeredDispatcher, 'function');

	const result = await registeredDispatcher([
		{ type: 'metadata', key: 'u123:ITEM1', payload: { item_key: 'ITEM1', title: 'A' } },
		{ type: 'metadata', key: 'u123:ITEM2', payload: { item_key: 'ITEM2', title: 'B' } },
		{ type: 'metadata', key: 'u456:ITEM3', payload: { item_key: 'ITEM3', title: 'C' } },
	]);

	assert.strictEqual(fetchCalls.length, 2); // one request per distinct library_id
	const bodies = fetchCalls.map(c => JSON.parse(c.init.body));
	const u123Body = bodies.find(b => b.library_id === 'u123');
	assert.strictEqual(u123Body.items.length, 2);
	assert.deepStrictEqual([...result.succeededKeys].sort(), ['u123:ITEM1', 'u123:ITEM2', 'u456:ITEM3'].sort());
});

test('the metadata dispatcher throws when the backend returns a non-2xx response', async () => {
	const fetchStub = async () => ({ ok: false, status: 500 });
	/** @type {any} */
	let registeredDispatcher;
	const taskQueueStub = { start: () => {}, registerDispatcher: (/** @type {string} */ _type, /** @type {any} */ fn) => { registeredDispatcher = fn; } };
	const zotero = {
		Libraries: { userLibraryID: 1 },
		Notifier: { registerObserver: () => 'nid', unregisterObserver: () => {} },
		Prefs: { get: () => null },
	};
	const plugin = loadPlugin(zotero, {}, {}, { TaskQueue: taskQueueStub, fetch: fetchStub });
	plugin.init({ id: 'x', version: '1', rootURI: 'chrome://x/' });

	await assert.rejects(
		() => registeredDispatcher([{ type: 'metadata', key: 'u1:ITEM1', payload: { item_key: 'ITEM1' } }]),
		/HTTP 500/
	);
});

test('init() starts the TaskQueue and removeFromAllWindows() stops it', () => {
	/** @type {string[]} */
	const calls = [];
	const taskQueueStub = {
		start: () => calls.push('start'),
		stop: () => calls.push('stop'),
		registerDispatcher: () => {},
	};
	const zotero = {
		Libraries: { userLibraryID: 1 },
		Notifier: { registerObserver: () => 'nid', unregisterObserver: () => {} },
		Prefs: { get: () => null },
		getMainWindows: () => [],
	};
	const plugin = loadPlugin(zotero, {}, {}, { TaskQueue: taskQueueStub });
	plugin.init({ id: 'x', version: '1', rootURI: 'chrome://x/' });
	plugin.removeFromAllWindows();

	assert.deepStrictEqual(calls, ['start', 'stop']);
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `node --test plugin/test/zotero-rag.test.js`
Expected: FAIL — `registeredDispatcher` stays `undefined` (nothing calls `TaskQueue.registerDispatcher`/`start`/`stop` yet)

- [ ] **Step 3: Register the dispatcher and wire start/stop**

In `init()`, the method currently ends with the notifier registration added in Task 6, closed like this:

```js
			},
			['item']
		);
	}
```

Replace that closing with (inserting the dispatcher registration and `TaskQueue.start()` between the notifier registration and the method's closing brace):

```js
			},
			['item']
		);

		// Register the metadata dispatcher and start the queue's heartbeat.
		// Groups ready tasks by library_id (the key format is "libraryId:itemKey",
		// set by the notifier above) since the backend call is per-library.
		TaskQueue.registerDispatcher('metadata', async (/** @type {any[]} */ tasks) => {
			/** @type {Map<string, any[]>} */
			const byLibrary = new Map();
			for (const task of tasks) {
				const libraryId = task.key.split(':')[0];
				if (!byLibrary.has(libraryId)) byLibrary.set(libraryId, []);
				/** @type {any[]} */ (byLibrary.get(libraryId)).push(task);
			}
			const succeededKeys = new Set();
			for (const [libraryId, libTasks] of byLibrary) {
				const response = await fetch(`${this.backendURL}/api/index/items/metadata`, {
					method: 'POST',
					headers: this.getAuthHeaders({ 'Content-Type': 'application/json' }),
					body: JSON.stringify({
						library_id: libraryId,
						items: libTasks.map(t => t.payload),
					}),
				});
				if (!response.ok) throw new Error(`HTTP ${response.status}`);
				for (const t of libTasks) succeededKeys.add(t.key);
			}
			return { succeededKeys };
		});
		TaskQueue.start();
	}
```

Note this `},\n\t\t\t['item']\n\t\t);\n\t}` sequence is specific to the end of `init()` — don't confuse it with the structurally similar `);` a few lines earlier inside the `notify` callback body; match on the trailing `\t}` (method close) to anchor the right occurrence.

In `removeFromAllWindows()` (`zotero-rag.js:438-448`), add `TaskQueue.stop()` alongside the existing notifier unregistration:

```js
	removeFromAllWindows() {
		var windows = Zotero.getMainWindows();
		for (let win of windows) {
			if (!win.ZoteroPane) continue;
			this.removeFromWindow(win);
		}
		if (this._notifierID) {
			Zotero.Notifier.unregisterObserver(this._notifierID);
			this._notifierID = null;
		}
		TaskQueue.stop();
	}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `node --test plugin/test/zotero-rag.test.js`
Expected: PASS (all tests, including the 3 new ones)

- [ ] **Step 5: Run the full plugin test suite**

Run: `node --test plugin/test/*.test.js`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add plugin/src/zotero-rag.js plugin/test/zotero-rag.test.js
git commit -m "$(cat <<'EOF'
feat(plugin): wire TaskQueue into the plugin lifecycle for live metadata sync

Registers a 'metadata' dispatcher (groups ready tasks by library_id,
POSTs to /api/index/items/metadata, treats non-2xx as a failure so
TaskQueue retries with backoff) and starts the queue's heartbeat at
the end of init(). Stops it in removeFromAllWindows(), alongside the
existing Notifier cleanup. This completes the live metadata sync
feature end-to-end.
EOF
)"
```

---

### Task 8: Manual end-to-end verification

**No new files.** This uses the Zotero dev-tools MCP server already available in this environment to drive a real running Zotero + plugin instance, since `bootstrap.js`/live notifier behavior can't be exercised by the Node unit tests above.

- [ ] **Step 1: Reload the plugin to pick up all the changes**

Use the `zotero_plugin_reload` MCP tool (or, if unavailable, restart the Zotero dev profile manually) to load the updated plugin source.

- [ ] **Step 2: Confirm the backend is running**

Run: `curl -s http://localhost:8119/api/version`
Expected: a JSON response (not a connection error). If it fails, start the backend per the project's `npm start` (see `CLAUDE.md`'s "Live Server" section) before continuing.

- [ ] **Step 3: Pick an already-indexed item and note its current backend payload**

Use `zotero_db_query` (or the `zotero_execute_js` tool to call `Zotero.Items.getByLibraryAndKeyAsync`) to find a regular item that has already been indexed (has chunks in Qdrant — any item you've previously queried successfully via the plugin's dialog is a good candidate). Note its key and library.

- [ ] **Step 4: Edit the item's title and tags in Zotero**

Use `zotero_execute_js` to run something equivalent to:

```js
const item = await Zotero.Items.getByLibraryAndKeyAsync(<libraryID>, '<ITEM_KEY>');
item.setField('title', 'Live Sync Test Title ' + Date.now());
item.addTag('live-sync-test');
await item.saveTx();
```

- [ ] **Step 5: Watch the Browser Console for the dispatch**

Use `zotero_read_logs` (filtering for `Zotero RAG` or `TaskQueue`) shortly after the edit (within ~5-10 seconds, past the 4s debounce window) — there should be no warning/error logged if the backend call succeeded silently (the design has no success-path logging). To positively confirm dispatch happened, proceed to Step 6.

- [ ] **Step 6: Verify the backend payload updated**

Query the backend directly (adjust the library/item to match Step 3-4):

```bash
curl -s "http://localhost:8119/api/libraries/<library_id>/items/<ITEM_KEY>/chunks" -H "X-Zotero-API-Key: <your-key>" | head -c 500
```

(If no such read endpoint is convenient, use `zotero_execute_js` to call the plugin's own `check-indexed` flow, or inspect via the backend's `/docs` Swagger UI at `http://localhost:8119/docs`.) Confirm the title now matches Step 4's new title and `live-sync-test` appears in tags — within a few seconds of the edit, not after waiting for a cron run.

- [ ] **Step 7: Verify the stuck-batch escalation path (optional but recommended)**

Stop the backend (`Ctrl-C` the `npm start` process or equivalent), repeat Step 4 with a different title, and watch `zotero_read_logs` over the next ~1-2 minutes — you should see `console.warn` messages with increasing retry delays (5s, 10s, 20s...), escalating to a `console.error` "stuck after 5 attempts" message once 5 consecutive failures accumulate. Restart the backend afterward and confirm the queued edit is eventually delivered (no need to wait for the full backoff cycle — restarting the backend lets the next retry attempt succeed).

- [ ] **Step 8: Report results**

No commit for this task — it's verification only. If any step reveals a bug, fix it in the relevant task's files, re-run that task's automated tests, and re-verify here before considering the feature done.
