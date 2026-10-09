// Auto-indexing status monitoring dialog for Zotero RAG.

// @ts-check

/// <reference path='./zotero-rag.js' />
/// <reference path='./rate-limit-widget.js' />

/**
 * @typedef {Object} AutoIndexSlugStatus
 * @property {string} status - pending|indexing|done|error|skipped|crashed|aborted
 * @property {number} [items_processed]
 * @property {number} [items_total]
 * @property {number} [chunks_added]
 * @property {number} [items_failed] - items whose attachment failed to download/process this run; they remain candidates for the next scan
 * @property {string} [error]
 * @property {string} [skip_reason]
 * @property {string} [rate_limit_until] - ISO timestamp; set when skip_reason is "embedding_rate_limit"
 * @property {string} [library_name] - human-readable name, falls back to the raw slug server-side
 * @property {number} [owner_id] - numeric Zotero user id; not shown in the UI (no username resolution available), kept for potential future use
 */

/**
 * @typedef {Object} AutoIndexKeyIssue
 * @property {string} [user]
 * @property {string} reason
 * @property {boolean} pruned
 * @property {string} [kind]
 */

/**
 * @typedef {Object} AutoIndexStatusResponse
 * @property {boolean} enabled
 * @property {number} keys_registered
 * @property {string} [disabled_reason]
 * @property {boolean} [running]
 * @property {boolean} [crashed]
 * @property {boolean} [aborted]
 * @property {string} [started_at]
 * @property {string} [finished_at]
 * @property {Record<string, AutoIndexSlugStatus>} [slugs]
 * @property {AutoIndexKeyIssue[]} [key_issues]
 * @property {boolean} [is_admin]
 * @property {{active: boolean, interval_minutes: number|null, paused: boolean, next_tick_at: string|null}} [scheduler]
 * @property {SystemHealth} [system_health] - admin-only; omitted entirely for non-admin callers
 * @property {RateLimitsInfo} [rate_limits] - cached embedding rate limits (never probed server-side)
 */

/**
 * @typedef {Object} RateLimitsInfo
 * @property {boolean} available
 * @property {Record<string, string>} [limits] - x-ratelimit-{limit,remaining}-{hour,day} headers
 * @property {string} [as_of] - ISO timestamp the headers were captured, when known
 * @property {'run'|'cache'} [source]
 */

/**
 * @typedef {Object} SwitchablePreset
 * @property {string} name
 * @property {boolean} active
 * @property {'ok'|'missing'} credentials
 */

/**
 * @typedef {Object} SidecarHealth
 * @property {string} status - ok|unreachable|timeout|local-mode|error|http_<code>
 * @property {number} [latency_ms]
 * @property {string} [error]
 */

/**
 * @typedef {Object} SystemHealth
 * @property {number} cpu_percent
 * @property {{used_gb: number, total_gb: number, percent: number}} memory
 * @property {{used_gb: number, total_gb: number, percent: number}} swap
 * @property {{free_gb: number, total_gb: number, free_percent: number}|null} disk
 * @property {{kreuzberg: SidecarHealth, qdrant: SidecarHealth}} sidecars
 */

// How long a freshly-triggered run is trusted to be "about to start" before
// its pending UI state is dropped even without server confirmation — covers
// a request that fails fast server-side before ever flipping the server's
// running flag (e.g. a race lost to another trigger). See _advancePendingFlag.
const PENDING_RUN_GRACE_MS = 15000;

var ZoteroRAGAutoIndexStatus = {
	/** @type {ZoteroRAGPlugin|null} */
	plugin: null,
	/** @type {number|null} */
	refreshTimer: null,
	/** @type {'own'|'all'} */
	adminScope: 'own',
	/**
	 * Slugs with a user-triggered run-slug request in flight, keyed by slug.
	 * Guards against the Index button re-enabling (and inviting a second,
	 * concurrent click) during the gap between the server accepting the
	 * request and the spawned subprocess actually writing "running"/"indexing"
	 * to its status — see _updatePendingRunState.
	 * @type {Map<string, {since: number, confirmedStarted: boolean}>}
	 */
	pendingRunSlugs: new Map(),

	/**
	 * Set while a user-triggered "Run now" (own libraries) request is in
	 * flight/unconfirmed — see _advancePendingFlag and runNow.
	 * @type {{since: number, confirmedStarted: boolean}|null}
	 */
	pendingOwnRun: null,

	/**
	 * Same as pendingOwnRun, for the admin "Run full index now" button.
	 * @type {{since: number, confirmedStarted: boolean}|null}
	 */
	pendingAdminRun: null,

	/** Whether the open-time GET /api/rate-limits fallback has already been tried. @type {boolean} */
	rateLimitFallbackTried: false,

	/** Last headers rendered into the Status-section widget. @type {Record<string, string>|null} */
	rateLimitHeaders: null,

	/** Presets the admin may switch to, from GET /api/config. @type {SwitchablePreset[]} */
	switchablePresets: [],

	/** True while a preset switch POST is in flight. @type {boolean} */
	presetSwitching: false,

	/** Whether the last status poll reported a running index. @type {boolean} */
	runInProgress: false,

	/**
	 * Initialize the dialog.
	 * @returns {void}
	 */
	init() {
		// @ts-ignore - window.arguments is available in XUL/Firefox extension context
		if (window.arguments && window.arguments[0]) {
			// @ts-ignore
			this.plugin = window.arguments[0].plugin;
		} else {
			console.error('No plugin reference passed to autoindex-status dialog');
			return;
		}

		const closeButton = document.getElementById('close-button');
		if (closeButton) {
			closeButton.addEventListener('click', () => window.close());
		}

		const runNowButton = document.getElementById('run-now-button');
		if (runNowButton) {
			runNowButton.addEventListener('click', () => this.runNow());
		}

		const adminRunNowButton = document.getElementById('admin-run-now-button');
		if (adminRunNowButton) {
			adminRunNowButton.addEventListener('click', () => this.runNowAdmin());
		}

		const adminPauseButton = document.getElementById('admin-pause-button');
		if (adminPauseButton) {
			adminPauseButton.addEventListener('click', () => this.pauseScheduler());
		}

		const adminResumeButton = document.getElementById('admin-resume-button');
		if (adminResumeButton) {
			adminResumeButton.addEventListener('click', () => this.resumeScheduler());
		}

		const adminAbortButton = document.getElementById('admin-abort-button');
		if (adminAbortButton) {
			adminAbortButton.addEventListener('click', () => this.abortRun());
		}

		const adminScopeToggle = /** @type {HTMLInputElement} */ (document.getElementById('admin-scope-toggle'));
		if (adminScopeToggle) {
			adminScopeToggle.addEventListener('change', () => {
				this.adminScope = adminScopeToggle.checked ? 'all' : 'own';
				this.fetchAndRender();
			});
		}

		const presetSelect = /** @type {HTMLSelectElement|null} */ (document.getElementById('admin-preset-select'));
		if (presetSelect) {
			presetSelect.addEventListener('change', () => this.switchPreset(presetSelect.value));
		}
		// A switch made in the Preferences pane must show up here too.
		if (this.plugin) {
			this.plugin.observePresetChanged(window, 'autoindex-status', () => {
				this.rateLimitHeaders = null;
				this.rateLimitFallbackTried = false;
				this.loadSwitchablePresets();
				this.fetchAndRender();
			});
		}

		window.addEventListener('unload', () => {
			if (this.refreshTimer !== null) {
				clearInterval(this.refreshTimer);
				this.refreshTimer = null;
			}
		});

		this.fetchAndRender();
		this.loadSwitchablePresets();
		this.refreshTimer = setInterval(() => this.fetchAndRender(), 5000);
	},

	/**
	 * Fetch the latest status from the backend and re-render the dialog.
	 * @returns {Promise<void>}
	 */
	async fetchAndRender() {
		if (!this.plugin) return;
		try {
			const url = this.adminScope === 'all'
				? `${this.plugin.backendURL}/api/autoindex/status?scope=all`
				: `${this.plugin.backendURL}/api/autoindex/status`;
			const response = await fetch(url, {
				headers: this.plugin.getAuthHeaders(),
			});
			if (!response.ok) {
				this.renderBanner(`Error: could not load status (HTTP ${response.status}).`, 'crashed');
				return;
			}
			/** @type {AutoIndexStatusResponse} */
			const data = await response.json();
			this.render(data);
		} catch (e) {
			this.renderBanner(`Error: ${e}`, 'crashed');
		}
	},

	/**
	 * Render the run banner with a given message and state class.
	 * @param {string} message
	 * @param {'idle'|'running'|'crashed'} state
	 * @returns {void}
	 */
	renderBanner(message, state) {
		const banner = document.getElementById('run-banner');
		if (!banner) return;
		banner.textContent = message;
		banner.className = state === 'idle' ? '' : state;
	},

	/**
	 * Format the elapsed/remaining time between now and an ISO timestamp as
	 * "X hours, Y minutes" (hours omitted when zero), for a parenthetical
	 * like "(since 2 hours, 5 minutes)" or "(in 45 minutes)".
	 * @param {string|undefined} isoString
	 * @returns {string|null} null if isoString is missing/invalid
	 */
	_formatDurationFromNow(isoString) {
		if (!isoString) return null;
		const then = new Date(isoString).getTime();
		if (Number.isNaN(then)) return null;
		const totalMinutes = Math.floor(Math.abs(Date.now() - then) / 60000);
		const hours = Math.floor(totalMinutes / 60);
		const minutes = totalMinutes % 60;
		const parts = [];
		if (hours > 0) parts.push(`${hours} hour${hours === 1 ? '' : 's'}`);
		if (minutes > 0 || hours === 0) parts.push(`${minutes} minute${minutes === 1 ? '' : 's'}`);
		return parts.join(', ');
	},

	/**
	 * Render the full dialog from a status response.
	 * @param {AutoIndexStatusResponse} data
	 * @returns {void}
	 */
	render(data) {
		if (!data.enabled) {
			this.renderBanner(data.disabled_reason || 'Automatic indexing is not configured on this server.', 'crashed');
			return;
		}
		const ownSlugCount = Object.keys(data.slugs || {}).length;
		const scheduler = data.scheduler || {};

		// A just-triggered run (runNow/runNowAdmin) hasn't necessarily updated
		// the server's on-disk status yet — the subprocess that does so starts
		// asynchronously and can take a second or more (interpreter startup,
		// imports). Until it's confirmed via data.running, or the grace window
		// lapses, suppress the *previous* run's leftover state (crashed,
		// rate-limited, idle, etc.) so it doesn't flash before being replaced
		// by real progress. See _advancePendingFlag.
		const confirmedRunning = data.running === true;
		const ownRunInPlay = this._advancePendingFlag(this.pendingOwnRun, confirmedRunning);
		if (!ownRunInPlay) this.pendingOwnRun = null;
		const ownRunUnconfirmed = ownRunInPlay && !this.pendingOwnRun.confirmedStarted;
		const adminRunInPlay = this._advancePendingFlag(this.pendingAdminRun, confirmedRunning);
		if (!adminRunInPlay) this.pendingAdminRun = null;
		const adminRunUnconfirmed = adminRunInPlay && !this.pendingAdminRun.confirmedStarted;

		if (ownRunUnconfirmed || adminRunUnconfirmed) {
			this.renderBanner('Starting indexing…', 'running');
		} else if (data.aborted) {
			this.renderBanner('The last automatic indexing run was stopped by an admin.', 'idle');
		} else if (data.crashed) {
			this.renderBanner('The last automatic indexing run crashed unexpectedly.', 'crashed');
		} else if (data.running && ownSlugCount === 0) {
			// A run is active, but none of it is this caller's own libraries —
			// most likely another user's manual trigger or a shared-lock cron tick.
			this.renderBanner('Indexing server currently busy, please wait and try again later.', 'running');
		} else if (data.running) {
			const elapsed = this._formatDurationFromNow(data.started_at);
			const suffix = elapsed ? ` (since ${elapsed})` : '';
			this.renderBanner(`Running since ${this.formatTime(data.started_at)}${suffix}`, 'running');
		} else if (scheduler.active && !scheduler.paused && scheduler.next_tick_at) {
			const remaining = this._formatDurationFromNow(scheduler.next_tick_at);
			const suffix = remaining ? ` (in ${remaining})` : '';
			this.renderBanner(`Next run at ${this.formatTime(scheduler.next_tick_at)}${suffix}`, 'idle');
		} else if (data.finished_at) {
			this.renderBanner(`Idle. Last run finished ${this.formatTime(data.finished_at)}.`, 'idle');
		} else {
			this.renderBanner('Idle. No automatic indexing run has happened yet.', 'idle');
		}

		// Only override the per-row display during the unconfirmed race window
		// (not for the run's full duration) — once confirmed, the live
		// per-slug status from the server is accurate and should be shown.
		const pendingStart = adminRunUnconfirmed || (ownRunUnconfirmed && this.adminScope === 'own');
		this.renderLibraries(data.slugs || {}, data.is_admin === true, data.running === true, pendingStart);
		this.renderProblems(data.key_issues || []);
		this.renderSystemHealth(data.system_health);
		this.updateRunNowButtonState(data, ownRunInPlay, adminRunInPlay);
		this.updateAdminControlsVisibility(data);
		this.runInProgress = data.running === true;
		this.renderRateLimits(data);
		this.renderPresetRow(data);
	},

	/**
	 * Render the rate-limit bars and the "resumes at" banner line in the
	 * Status section. Falls back once per dialog open to GET /api/rate-limits
	 * when the status payload carries no cached limits.
	 * @param {AutoIndexStatusResponse} data
	 * @returns {void}
	 */
	renderRateLimits(data) {
		const info = data.rate_limits;
		if (info && info.available && info.limits) {
			this.rateLimitHeaders = info.limits;
		} else if (!this.rateLimitFallbackTried) {
			this.rateLimitFallbackTried = true;
			ZoteroRAGRateLimitWidget.fetch(this.plugin).then((headers) => {
				if (headers) {
					this.rateLimitHeaders = headers;
					this.paintRateLimits(null);
				}
			});
		}
		this.paintRateLimits(info || null);

		const banner = document.getElementById('rate-limit-banner');
		if (banner) {
			const until = this.earliestRateLimitUntil(data.slugs || {});
			if (until) {
				banner.textContent = `Embedding rate limit reached; resumes at ${this.formatClock(until)}.`;
				banner.style.display = '';
			} else {
				banner.textContent = '';
				banner.style.display = 'none';
			}
		}
	},

	/**
	 * Paint the bars from the current headers and the optional as-of line.
	 * @param {RateLimitsInfo|null} info
	 * @returns {void}
	 */
	paintRateLimits(info) {
		const headers = this.rateLimitHeaders;
		ZoteroRAGRateLimitWidget.render(document, headers, { visible: !!headers, prefix: 'ai-' });
		const asOf = document.getElementById('ai-rate-limit-asof');
		if (asOf) {
			const ago = info && info.as_of && headers ? this._formatDurationFromNow(info.as_of) : null;
			asOf.textContent = ago ? `as of ${ago} ago` : '';
		}
	},

	/**
	 * Earliest `rate_limit_until` among slugs skipped for an embedding rate limit.
	 * @param {Record<string, AutoIndexSlugStatus>} slugs
	 * @returns {string|null} ISO timestamp, or null
	 */
	earliestRateLimitUntil(slugs) {
		/** @type {{iso: string, ms: number}|null} */
		let best = null;
		for (const info of Object.values(slugs)) {
			if (info.skip_reason !== 'embedding_rate_limit' || !info.rate_limit_until) continue;
			const ms = new Date(info.rate_limit_until).getTime();
			if (Number.isNaN(ms)) continue;
			if (!best || ms < best.ms) best = { iso: info.rate_limit_until, ms };
		}
		return best ? best.iso : null;
	},

	/**
	 * Format an ISO timestamp as a local HH:MM clock time.
	 * @param {string} isoString
	 * @returns {string}
	 */
	formatClock(isoString) {
		try {
			return new Date(isoString).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
		} catch (_) {
			return isoString;
		}
	},

	/**
	 * Fetch GET /api/config and (re)populate the admin preset dropdown.
	 * Called at dialog open and after a successful switch.
	 * @returns {Promise<void>}
	 */
	async loadSwitchablePresets() {
		if (!this.plugin) return;
		try {
			const response = await fetch(`${this.plugin.backendURL}/api/config`, {
				headers: this.plugin.getAuthHeaders(),
			});
			if (!response.ok) return;
			/** @type {{switchable_presets?: SwitchablePreset[]}} */
			const config = await response.json();
			this.switchablePresets = config.switchable_presets || [];
			this.populatePresetSelect();
		} catch (_) {
			// non-fatal — the row simply stays hidden
		}
	},

	/**
	 * Fill the select from `switchablePresets`, selecting the active one.
	 * @returns {void}
	 */
	populatePresetSelect() {
		const select = /** @type {HTMLSelectElement|null} */ (document.getElementById('admin-preset-select'));
		if (!select) return;
		select.innerHTML = '';
		for (const preset of this.switchablePresets) {
			const option = document.createElement('option');
			option.value = preset.name;
			option.textContent = preset.name;
			select.appendChild(option);
			if (preset.active) select.value = preset.name;
		}
		this.applyPresetRowState(this.lastIsAdmin);
	},

	/** Whether the last status poll flagged the caller as admin. @type {boolean} */
	lastIsAdmin: false,

	/**
	 * Update visibility/enabled state of the preset row from a status poll.
	 * @param {AutoIndexStatusResponse} data
	 * @returns {void}
	 */
	renderPresetRow(data) {
		this.lastIsAdmin = data.is_admin === true;
		this.applyPresetRowState(this.lastIsAdmin);
	},

	/**
	 * Show the row only for admins with >= 2 options; disable while a run is
	 * in progress (the running subprocess already built its embedding service).
	 * @param {boolean} isAdmin
	 * @returns {void}
	 */
	applyPresetRowState(isAdmin) {
		const row = document.getElementById('admin-preset-row');
		const select = /** @type {HTMLSelectElement|null} */ (document.getElementById('admin-preset-select'));
		if (!row) return;
		row.style.display = isAdmin && this.switchablePresets.length >= 2 ? '' : 'none';
		if (select) {
			select.disabled = this.runInProgress || this.presetSwitching;
			select.title = this.runInProgress
				? 'A run is in progress; a switch only takes effect from the next run.'
				: '';
		}
	},

	/**
	 * Switch the active preset via POST /api/config. On error, revert the
	 * select and show the server's `detail`; on success refresh everything.
	 * @param {string} name
	 * @returns {Promise<void>}
	 */
	async switchPreset(name) {
		if (!this.plugin) return;
		const status = document.getElementById('admin-preset-status');
		this.presetSwitching = true;
		this.applyPresetRowState(this.lastIsAdmin);
		if (status) status.textContent = 'Switching…';
		try {
			const response = await fetch(`${this.plugin.backendURL}/api/config`, {
				method: 'POST',
				headers: { ...this.plugin.getAuthHeaders(), 'Content-Type': 'application/json' },
				body: JSON.stringify({ preset_name: name }),
			});
			if (!response.ok) {
				const err = await response.json().catch(() => ({}));
				if (status) status.textContent = `Error: ${err.detail || response.status}`;
				this.populatePresetSelect(); // revert to the still-active preset
				return;
			}
			if (status) status.textContent = 'Switched.';
			this.plugin.notifyPresetChanged('autoindex-status');
			// Cached limits belong to the previous preset.
			this.rateLimitHeaders = null;
			this.rateLimitFallbackTried = false;
			await this.loadSwitchablePresets();
			await this.fetchAndRender();
		} catch (e) {
			if (status) status.textContent = `Error: ${e}`;
			this.populatePresetSelect();
		} finally {
			this.presetSwitching = false;
			this.applyPresetRowState(this.lastIsAdmin);
		}
	},

	/**
	 * Enable/disable the "Run now" button based on server- and client-side
	 * indexing state.
	 * @param {AutoIndexStatusResponse} data
	 * @param {boolean} [ownRunPending] - a user-triggered own-run is in flight/unconfirmed
	 * @param {boolean} [adminRunPending] - a user-triggered full run is in flight/unconfirmed
	 * @returns {void}
	 */
	updateRunNowButtonState(data, ownRunPending = false, adminRunPending = false) {
		const busy = data.running === true || ownRunPending || adminRunPending
			|| (this.plugin && this.plugin.isClientIndexingActive());

		const button = /** @type {HTMLButtonElement} */ (document.getElementById('run-now-button'));
		if (button) {
			button.disabled = busy;
			this.setButtonLabel(button, busy ? 'Indexing in progress…' : 'Run indexing now');
		}

		const adminButton = /** @type {HTMLButtonElement} */ (document.getElementById('admin-run-now-button'));
		if (adminButton) {
			adminButton.disabled = busy;
			this.setButtonLabel(adminButton, busy ? 'Indexing in progress…' : 'Run full index now (all libraries)');
		}
	},

	/**
	 * Set a button's visible label text without clobbering its icon span
	 * (`.button-icon`), for buttons whose label changes based on state.
	 * Falls back to plain textContent for buttons with no icon/label spans.
	 * @param {HTMLButtonElement} button
	 * @param {string} text
	 * @returns {void}
	 */
	setButtonLabel(button, text) {
		const label = button.querySelector('.button-label');
		if (label) {
			label.textContent = text;
		} else {
			button.textContent = text;
		}
	},

	/**
	 * Show/hide the admin-only controls block based on the server-reported
	 * is_admin flag. Runs on every poll so admin status granted/revoked
	 * mid-session takes effect within one tick. Also toggles which of the
	 * pause/resume buttons is shown, based on the scheduler's persisted
	 * pause state.
	 * @param {AutoIndexStatusResponse} data
	 * @returns {void}
	 */
	updateAdminControlsVisibility(data) {
		const block = document.getElementById('admin-controls');
		if (!block) return;
		const isAdmin = data.is_admin === true;
		block.style.display = isAdmin ? '' : 'none';
		if (!isAdmin) {
			this.adminScope = 'own';
			const toggle = /** @type {HTMLInputElement} */ (document.getElementById('admin-scope-toggle'));
			if (toggle) toggle.checked = false;
		}

		const paused = data.scheduler?.paused === true;
		const pauseButton = document.getElementById('admin-pause-button');
		const resumeButton = document.getElementById('admin-resume-button');
		if (pauseButton) pauseButton.style.display = paused ? 'none' : '';
		if (resumeButton) resumeButton.style.display = paused ? '' : 'none';
	},

	/**
	 * Trigger an immediate, unscoped indexing run covering every registered
	 * library (admin only).
	 * @returns {Promise<void>}
	 */
	async runNowAdmin() {
		if (!this.plugin) return;
		const button = /** @type {HTMLButtonElement} */ (document.getElementById('admin-run-now-button'));
		if (button) {
			button.disabled = true;
			this.setButtonLabel(button, 'Starting…');
		}
		this.renderBanner('Starting full index…', 'running');
		// Mark pending before the request even lands, so a 5s poll tick firing
		// mid-request (or the fetchAndRender below) can't see the previous
		// run's stale status and flash it / re-enable the button — see
		// _advancePendingFlag.
		this.pendingAdminRun = { since: Date.now(), confirmedStarted: false };
		try {
			const response = await fetch(`${this.plugin.backendURL}/api/autoindex/scheduler/run-now`, {
				method: 'POST',
				headers: this.plugin.getAuthHeaders(),
			});
			if (!response.ok) {
				const body = await response.json().catch(() => ({}));
				this.pendingAdminRun = null;
				this.renderBanner(body.detail || `Could not start indexing (HTTP ${response.status}).`, 'crashed');
				if (button) {
					button.disabled = false;
					this.setButtonLabel(button, 'Run full index now (all libraries)');
				}
				return;
			}
			await this.fetchAndRender();
		} catch (e) {
			this.pendingAdminRun = null;
			this.renderBanner(`Error: ${e}`, 'crashed');
			if (button) {
				button.disabled = false;
				this.setButtonLabel(button, 'Run full index now (all libraries)');
			}
		}
	},

	/**
	 * Pause the built-in scheduler (admin only).
	 * @returns {Promise<void>}
	 */
	async pauseScheduler() {
		if (!this.plugin) return;
		try {
			const response = await fetch(`${this.plugin.backendURL}/api/autoindex/scheduler/pause`, {
				method: 'POST',
				headers: this.plugin.getAuthHeaders(),
			});
			if (!response.ok) {
				const body = await response.json().catch(() => ({}));
				this.renderBanner(body.detail || `Could not pause scheduler (HTTP ${response.status}).`, 'crashed');
				return;
			}
			await this.fetchAndRender();
		} catch (e) {
			this.renderBanner(`Error: ${e}`, 'crashed');
		}
	},

	/**
	 * Resume the built-in scheduler (admin only).
	 * @returns {Promise<void>}
	 */
	async resumeScheduler() {
		if (!this.plugin) return;
		try {
			const response = await fetch(`${this.plugin.backendURL}/api/autoindex/scheduler/resume`, {
				method: 'POST',
				headers: this.plugin.getAuthHeaders(),
			});
			if (!response.ok) {
				const body = await response.json().catch(() => ({}));
				this.renderBanner(body.detail || `Could not resume scheduler (HTTP ${response.status}).`, 'crashed');
				return;
			}
			await this.fetchAndRender();
		} catch (e) {
			this.renderBanner(`Error: ${e}`, 'crashed');
		}
	},

	/**
	 * Abort the entire running indexing process (admin only).
	 * @returns {Promise<void>}
	 */
	async abortRun() {
		if (!this.plugin) return;
		try {
			const response = await fetch(`${this.plugin.backendURL}/api/autoindex/abort`, {
				method: 'POST',
				headers: this.plugin.getAuthHeaders(),
			});
			if (!response.ok) {
				const body = await response.json().catch(() => ({}));
				this.renderBanner(body.detail || `Could not abort run (HTTP ${response.status}).`, 'crashed');
				return;
			}
			await this.fetchAndRender();
		} catch (e) {
			this.renderBanner(`Error: ${e}`, 'crashed');
		}
	},

	/**
	 * Cooperatively skip a single job in the active run without killing the
	 * whole process (admin only).
	 * @param {string} slug
	 * @returns {Promise<void>}
	 */
	async skipSlug(slug) {
		if (!this.plugin) return;
		const button = /** @type {HTMLButtonElement|null} */ (document.querySelector(`[data-skip-slug="${slug}"]`));
		if (button) {
			button.disabled = true;
			button.textContent = 'Skipping…';
		}
		try {
			const response = await fetch(`${this.plugin.backendURL}/api/autoindex/scheduler/skip-slug`, {
				method: 'POST',
				headers: { ...this.plugin.getAuthHeaders(), 'Content-Type': 'application/json' },
				body: JSON.stringify({ slug }),
			});
			if (!response.ok) {
				const body = await response.json().catch(() => ({}));
				this.renderBanner(body.detail || `Could not skip job (HTTP ${response.status}).`, 'crashed');
				if (button) {
					button.disabled = false;
					button.textContent = 'Skip';
				}
				return;
			}
			await this.fetchAndRender();
		} catch (e) {
			this.renderBanner(`Error: ${e}`, 'crashed');
		}
	},

	/**
	 * Trigger an immediate server-side indexing run scoped to a single
	 * library (admin only) — lets an admin target one library in between
	 * scheduled runs or after aborting the current one.
	 * @param {string} slug
	 * @returns {Promise<void>}
	 */
	async runSlug(slug) {
		if (!this.plugin) return;
		const button = /** @type {HTMLButtonElement|null} */ (document.querySelector(`[data-run-slug="${slug}"]`));
		if (button) {
			button.disabled = true;
			button.textContent = 'Starting…';
		}
		// Mark pending before the request even lands, so a 5s poll tick firing
		// mid-request can't see a stale "not running" status and re-enable the
		// button early — see _updatePendingRunState for how this clears again.
		this.pendingRunSlugs.set(slug, { since: Date.now(), confirmedStarted: false });
		try {
			const response = await fetch(`${this.plugin.backendURL}/api/autoindex/scheduler/run-slug`, {
				method: 'POST',
				headers: { ...this.plugin.getAuthHeaders(), 'Content-Type': 'application/json' },
				body: JSON.stringify({ slug }),
			});
			if (!response.ok) {
				const body = await response.json().catch(() => ({}));
				this.renderBanner(body.detail || `Could not start indexing (HTTP ${response.status}).`, 'crashed');
				this.pendingRunSlugs.delete(slug);
				if (button) {
					button.disabled = false;
					button.textContent = 'Index';
				}
				return;
			}
			await this.fetchAndRender();
		} catch (e) {
			this.renderBanner(`Error: ${e}`, 'crashed');
			this.pendingRunSlugs.delete(slug);
		}
	},

	/**
	 * Decide whether a run-slug request is still "pending" for button-disabling
	 * purposes, and advance/clear its tracking state as fresher status arrives.
	 *
	 * The server accepts a run-slug request and spawns a subprocess
	 * asynchronously — there's a real gap (subprocess startup, imports) before
	 * that subprocess writes "indexing" to the status file, during which a
	 * status poll still reads the previous (not-running) state. Without this
	 * tracking, that stale read re-enables the button and invites a second
	 * overlapping click (observed: three concurrent indexing subprocesses
	 * spawned from rapid clicks). A request that fails fast server-side before
	 * ever updating per-slug status (e.g. an already-rate-limited embedding
	 * key) would otherwise leave the button disabled forever, so an unconfirmed
	 * pending entry expires after a grace window.
	 * @param {string} slug
	 * @param {AutoIndexSlugStatus} [info]
	 * @returns {boolean} true if the Index button for this slug should stay disabled
	 */
	_updatePendingRunState(slug, info) {
		const isActive = !!info && (info.status === 'pending' || info.status === 'indexing');
		const pending = this.pendingRunSlugs.get(slug);
		const stillPending = this._advancePendingFlag(pending, isActive);
		if (!stillPending) {
			this.pendingRunSlugs.delete(slug);
		}
		return stillPending;
	},

	/**
	 * Advance a single pending-trigger marker given the latest confirmation
	 * signal from the server, expiring it once either it's been confirmed
	 * and then gone terminal again, or the grace window elapses without ever
	 * being confirmed (e.g. a request that failed fast server-side before
	 * flipping any status). Shared by the per-slug admin run tracking
	 * (pendingRunSlugs) and the global own-run/admin-run-all tracking
	 * (pendingOwnRun/pendingAdminRun) — see the race described in
	 * _updatePendingRunState's and runNow's docs: the server accepts a
	 * trigger and spawns a subprocess asynchronously, and there's a real gap
	 * before that subprocess updates on-disk status, during which a poll
	 * would otherwise see the *previous* run's stale result.
	 * @param {{since: number, confirmedStarted: boolean}|null|undefined} pending
	 * @param {boolean} isActiveNow - true if the latest server data confirms this trigger has started
	 * @returns {boolean} true if the pending marker is still (or newly) in effect
	 */
	_advancePendingFlag(pending, isActiveNow) {
		if (!pending) return false;
		if (isActiveNow) {
			pending.confirmedStarted = true;
			return true;
		}
		if (pending.confirmedStarted) {
			// Was confirmed running, now back to a terminal status — finished.
			return false;
		}
		if (Date.now() - pending.since > PENDING_RUN_GRACE_MS) {
			return false;
		}
		return true;
	},

	/**
	 * Trigger an on-demand server-side indexing run for the caller's own libraries.
	 * @returns {Promise<void>}
	 */
	async runNow() {
		if (!this.plugin) return;
		const button = /** @type {HTMLButtonElement} */ (document.getElementById('run-now-button'));
		// Give immediate feedback rather than waiting for the next 5s poll tick.
		if (button) {
			button.disabled = true;
			this.setButtonLabel(button, 'Indexing in progress…');
		}
		this.renderBanner('Starting indexing…', 'running');
		// Mark pending before the request even lands, so a 5s poll tick firing
		// mid-request (or the fetchAndRender below) can't see the previous
		// run's stale status and flash it / re-enable the button — see
		// _advancePendingFlag.
		this.pendingOwnRun = { since: Date.now(), confirmedStarted: false };
		try {
			const response = await fetch(`${this.plugin.backendURL}/api/autoindex/run`, {
				method: 'POST',
				headers: this.plugin.getAuthHeaders(),
			});
			if (!response.ok) {
				const body = await response.json().catch(() => ({}));
				this.pendingOwnRun = null;
				this.renderBanner(body.detail || `Could not start indexing (HTTP ${response.status}).`, 'crashed');
				if (button) {
					button.disabled = false;
					this.setButtonLabel(button, 'Run indexing now');
				}
				return;
			}
			// Sync with the server's actual state right away instead of waiting
			// for the next 5s poll tick.
			await this.fetchAndRender();
		} catch (e) {
			this.pendingOwnRun = null;
			this.renderBanner(`Error: ${e}`, 'crashed');
			if (button) {
				button.disabled = false;
				this.setButtonLabel(button, 'Run indexing now');
			}
		}
	},

	/**
	 * Format an ISO timestamp for display, falling back to the raw value.
	 * @param {string|undefined} isoString
	 * @returns {string}
	 */
	formatTime(isoString) {
		if (!isoString) return 'an unknown time';
		try {
			return new Date(isoString).toLocaleString();
		} catch (_) {
			return isoString;
		}
	},

	/**
	 * Turn a per-library skip_reason/error into a human-readable message.
	 * Known machine-readable reasons (currently just "embedding_rate_limit")
	 * get a friendly, actionable message; anything else (admin skip messages,
	 * arbitrary exception text) is already human-written and passed through.
	 * @param {AutoIndexSlugStatus} info
	 * @returns {string}
	 */
	_formatSkipOrErrorReason(info) {
		if (info.error) return info.error;
		if (info.skip_reason === 'embedding_rate_limit') {
			return info.rate_limit_until
				? `Embedding quota exhausted for today — resumes automatically at ${this.formatTime(info.rate_limit_until)}.`
				: 'Embedding quota exhausted for today — indexing will resume automatically once the limit resets.';
		}
		if (info.skip_reason) return info.skip_reason;
		if (info.items_failed) {
			return `${info.items_failed} item(s) failed to index this run — check the server logs; they remain candidates for the next scan.`;
		}
		return '';
	},

	/**
	 * Render one row per library with a progress bar reflecting its status.
	 * @param {Record<string, AutoIndexSlugStatus>} slugs
	 * @param {boolean} [isAdmin]
	 * @param {boolean} [running] - whether any run (own or another's) is currently active server-side
	 * @param {boolean} [pendingStart] - a just-triggered run hasn't updated per-slug
	 *   status yet; show "pending" for every row instead of each slug's stale
	 *   leftover status (e.g. a rate-limit skip message) from the previous run
	 * @returns {void}
	 */
	renderLibraries(slugs, isAdmin = false, running = false, pendingStart = false) {
		const container = document.getElementById('libraries-container');
		const emptyState = document.getElementById('empty-state');
		if (!container || !emptyState) return;
		container.innerHTML = '';

		const slugNames = Object.keys(slugs);
		if (slugNames.length === 0) {
			emptyState.style.display = '';
			return;
		}
		emptyState.style.display = 'none';

		for (const slug of slugNames.sort()) {
			const info = pendingStart
				? { status: 'pending', library_name: slugs[slug].library_name }
				: slugs[slug];
			const row = document.createElement('div');
			row.className = 'library-row';

			const header = document.createElement('div');
			header.className = 'library-row-header';

			const nameSpan = document.createElement('span');
			nameSpan.className = 'library-name';
			nameSpan.textContent = (info.library_name && info.library_name !== slug)
				? info.library_name
				: slug;
			header.appendChild(nameSpan);

			// Push the badge and admin buttons to the right as one group,
			// so the badge lines up with them consistently whether or not
			// the run/skip buttons are present for this row.
			const spacer = document.createElement('span');
			spacer.className = 'library-row-spacer';
			header.appendChild(spacer);

			const badge = document.createElement('span');
			badge.className = `library-status-badge ${info.status}`;
			badge.textContent = info.status;
			header.appendChild(badge);

			if (isAdmin) {
				const runButton = document.createElement('button');
				runButton.type = 'button';
				runButton.className = 'dialog-button library-run-button';
				const isPending = this._updatePendingRunState(slug, info);
				runButton.textContent = isPending ? 'Starting…' : 'Index';
				runButton.disabled = running || isPending;
				runButton.dataset.runSlug = slug;
				runButton.addEventListener('click', () => this.runSlug(slug));
				header.appendChild(runButton);
			}

			if (isAdmin && (info.status === 'pending' || info.status === 'indexing')) {
				const skipButton = document.createElement('button');
				skipButton.type = 'button';
				skipButton.className = 'dialog-button library-skip-button';
				skipButton.textContent = 'Skip';
				skipButton.disabled = !running;
				skipButton.dataset.skipSlug = slug;
				skipButton.addEventListener('click', () => this.skipSlug(slug));
				header.appendChild(skipButton);
			}

			row.appendChild(header);

			const progress = /** @type {HTMLProgressElement} */ (document.createElement('progress'));
			progress.className = 'library-progress';
			if (info.items_total) {
				progress.max = info.items_total;
				progress.value = info.items_processed || 0;
			} else {
				progress.removeAttribute('value');
			}
			row.appendChild(progress);

			const meta = document.createElement('div');
			meta.className = 'library-meta';
			const parts = [];
			if (typeof info.items_processed === 'number' && typeof info.items_total === 'number') {
				parts.push(`${info.items_processed} / ${info.items_total} items`);
			}
			if (typeof info.chunks_added === 'number') {
				parts.push(`${info.chunks_added} chunks added`);
			}
			meta.textContent = parts.join(' — ');
			row.appendChild(meta);

			if (info.error || info.skip_reason || info.items_failed) {
				const errorDiv = document.createElement('div');
				errorDiv.className = 'library-error';
				errorDiv.textContent = this._formatSkipOrErrorReason(info);
				row.appendChild(errorDiv);
			}

			container.appendChild(row);
		}
	},

	/**
	 * Render the "Problems" list from key_issues.
	 * @param {AutoIndexKeyIssue[]} issues
	 * @returns {void}
	 */
	renderProblems(issues) {
		const section = document.getElementById('problems-section');
		const list = document.getElementById('problems-list');
		if (!section || !list) return;
		list.innerHTML = '';
		if (issues.length === 0) {
			section.style.display = 'none';
			return;
		}
		section.style.display = '';
		for (const issue of issues) {
			const row = document.createElement('div');
			row.className = 'problem-row';
			row.textContent = issue.reason;
			list.appendChild(row);
		}
	},

	/**
	 * Pick a severity class for a metric given "higher is worse" thresholds.
	 * @param {number} value
	 * @param {number} warnAt
	 * @param {number} criticalAt
	 * @returns {''|'warn'|'critical'}
	 */
	_severityHighIsBad(value, warnAt, criticalAt) {
		if (value >= criticalAt) return 'critical';
		if (value >= warnAt) return 'warn';
		return '';
	},

	/**
	 * Render the admin-only system health panel (host CPU/memory/swap/disk
	 * plus Kreuzberg/Qdrant reachability+latency) — lets an admin tell a
	 * stuck run apart from a slow one without leaving the dialog.
	 * @param {SystemHealth|undefined} health
	 * @returns {void}
	 */
	renderSystemHealth(health) {
		const section = document.getElementById('system-health-section');
		const content = document.getElementById('system-health-content');
		if (!section || !content) return;
		if (!health) {
			section.style.display = 'none';
			return;
		}
		section.style.display = '';
		content.innerHTML = '';

		/**
		 * @param {string} label
		 * @param {string} value
		 * @param {''|'warn'|'critical'} [severity]
		 */
		const addItem = (label, value, severity = '') => {
			const item = document.createElement('span');
			item.className = 'health-item';
			const labelSpan = document.createElement('span');
			labelSpan.className = 'health-label';
			labelSpan.textContent = `${label}: `;
			item.appendChild(labelSpan);
			const valueSpan = document.createElement('span');
			valueSpan.className = `health-value${severity ? ` ${severity}` : ''}`;
			valueSpan.textContent = value;
			item.appendChild(valueSpan);
			content.appendChild(item);
		};

		addItem('CPU', `${health.cpu_percent.toFixed(0)}%`, this._severityHighIsBad(health.cpu_percent, 80, 95));
		addItem(
			'Memory',
			`${health.memory.used_gb.toFixed(1)} / ${health.memory.total_gb.toFixed(1)} GB (${health.memory.percent.toFixed(0)}%)`,
			this._severityHighIsBad(health.memory.percent, 75, 90),
		);
		addItem(
			'Swap',
			`${health.swap.used_gb.toFixed(1)} / ${health.swap.total_gb.toFixed(1)} GB (${health.swap.percent.toFixed(0)}%)`,
			this._severityHighIsBad(health.swap.percent, 50, 80),
		);
		if (health.disk) {
			// "low is bad" for free space, so invert: treat it as a 100-x
			// high-is-bad value against the same threshold helper.
			const severity = this._severityHighIsBad(100 - health.disk.free_percent, 80, 90);
			addItem('Disk free', `${health.disk.free_percent.toFixed(0)}% (${health.disk.free_gb.toFixed(0)} GB)`, severity);
		}
		for (const [name, label] of [['kreuzberg', 'Kreuzberg'], ['qdrant', 'Qdrant']]) {
			const sidecar = health.sidecars && health.sidecars[name];
			if (!sidecar) continue;
			const ok = sidecar.status === 'ok' || sidecar.status === 'local-mode';
			const text = sidecar.latency_ms !== undefined
				? `${sidecar.status} (${sidecar.latency_ms}ms)`
				: sidecar.status;
			addItem(label, text, ok ? '' : 'critical');
		}
	},
};

if (document.readyState === 'loading') {
	document.addEventListener('DOMContentLoaded', () => {
		ZoteroRAGAutoIndexStatus.init();
	});
} else {
	ZoteroRAGAutoIndexStatus.init();
}
