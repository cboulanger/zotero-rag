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

test('a partial batch failure (failed:true) keeps succeeded keys out of the ready map, re-queues the rest with backoff, and resets on a later full success', async () => {
	const { queue, warnings } = loadTaskQueue();
	/** @type {any[]} */
	const dispatchedBatches = [];
	// First call: lib1's item succeeds, lib2's item fails (mirrors a dispatcher
	// like zotero-rag.js's metadata one, which POSTs per-library and folds a
	// per-library failure into `failed: true` rather than throwing).
	let secondCallSucceedsBoth = false;
	queue._now = () => 1000;
	queue.registerDispatcher('metadata', async (/** @type {any[]} */ batch) => {
		// `batch` is an array from task_queue.js's own vm-context realm, so
		// `batch.map(...)` would produce another foreign-realm array — build
		// plain Node-realm arrays with Array.from() instead, since
		// assert.deepStrictEqual treats cross-realm arrays as non-equal even
		// with identical contents.
		dispatchedBatches.push(Array.from(batch, t => t.key));
		if (secondCallSucceedsBoth) {
			return { succeededKeys: new Set(Array.from(batch, t => t.key)) };
		}
		const succeededKeys = new Set(Array.from(batch.filter(t => t.key.startsWith('lib1:')), t => t.key));
		const failed = batch.some(t => t.key.startsWith('lib2:'));
		return { succeededKeys, failed };
	});
	queue.enqueue('metadata', 'lib1:ITEM1', { title: 'A' }, 1000);
	queue.enqueue('metadata', 'lib2:ITEM2', { title: 'B' }, 1000);

	queue._now = () => 2001;
	queue._tick();
	await new Promise(r => setImmediate(r));

	assert.strictEqual(dispatchedBatches.length, 1);
	assert.deepStrictEqual(dispatchedBatches[0].sort(), ['lib1:ITEM1', 'lib2:ITEM2']);

	// (a)+(b): lib1's succeeded key is gone from the ready map; lib2's is re-queued.
	const readyKeys = [...(/** @type {Map<string, any>} */ (queue._ready.get('metadata'))).keys()];
	assert.deepStrictEqual(readyKeys, ['lib2:ITEM2']);

	// (c): backoff applied — a warning was logged and a retry before the
	// backoff window elapses must not re-dispatch.
	assert.strictEqual(warnings.length, 1);
	assert.match(warnings[0], /attempt 1, retry in 5000ms/);

	queue._now = () => 2500; // still within the 5000ms backoff window
	queue._tick();
	await new Promise(r => setImmediate(r));
	assert.strictEqual(dispatchedBatches.length, 1); // no new dispatch

	// (d): once the backoff window elapses and the dispatcher fully succeeds,
	// the failure count resets cleanly (next failure would start again at attempt 1).
	secondCallSucceedsBoth = true;
	queue._now = () => 7002; // past the 5000ms backoff from t=2001
	queue._tick();
	await new Promise(r => setImmediate(r));

	assert.strictEqual(dispatchedBatches.length, 2);
	assert.deepStrictEqual(dispatchedBatches[1], ['lib2:ITEM2']);
	assert.strictEqual(queue._failureCount.get('metadata'), 0);
	assert.strictEqual(queue._nextAttemptAt.has('metadata'), false);
	assert.strictEqual(warnings.length, 1); // no additional warning on the successful retry
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

test('does not drop a newer edit if re-enqueued during in-flight dispatch', async () => {
	const { queue } = loadTaskQueue();
	/** @type {any[]} */
	const dispatchedBatches = [];
	/** @type {any} */
	let resolveDispatcher;
	queue._now = () => 1000;
	queue.registerDispatcher('metadata', async (/** @type {any[]} */ batch) => {
		dispatchedBatches.push({ batch, timestamp: queue._now() });
		return new Promise(resolve => {
			resolveDispatcher = () => {
				resolve({ succeededKeys: new Set(batch.map(t => t.key)) });
			};
		});
	});

	// Enqueue first version of the task
	queue.enqueue('metadata', 'lib:ITEM1', { title: 'First' }, 1000);

	// Let it become ready and start dispatch
	queue._now = () => 2001;
	queue._tick();
	await new Promise(r => setImmediate(r));
	assert.strictEqual(dispatchedBatches.length, 1);
	assert.strictEqual(dispatchedBatches[0].batch[0].payload.title, 'First');

	// While the first dispatch is still in-flight, enqueue a newer version
	queue._now = () => 3000;
	queue.enqueue('metadata', 'lib:ITEM1', { title: 'Second' }, 1000); // due at 4000

	// Let the newer version become ready
	queue._now = () => 4001;
	queue._tick();
	await new Promise(r => setImmediate(r));
	// The first dispatch is still in-flight, so the second shouldn't have dispatched yet
	assert.strictEqual(dispatchedBatches.length, 1);

	// Now resolve the first dispatch
	resolveDispatcher();
	await new Promise(r => setImmediate(r));

	// After the first dispatch resolves, _inFlight is false, so we can dispatch the next batch.
	// Call _tick() to trigger the second dispatch.
	queue._tick();
	await new Promise(r => setImmediate(r));

	// The second dispatch should have happened, with the newer payload
	assert.strictEqual(dispatchedBatches.length, 2);
	assert.strictEqual(dispatchedBatches[1].batch[0].payload.title, 'Second');
});
