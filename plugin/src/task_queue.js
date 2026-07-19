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
