// Auto-indexing status monitoring dialog for Zotero RAG.

// @ts-check

/// <reference path='./zotero-rag.js' />

/**
 * @typedef {Object} AutoIndexSlugStatus
 * @property {string} status - pending|indexing|done|error|skipped|crashed|aborted
 * @property {number} [items_processed]
 * @property {number} [items_total]
 * @property {number} [chunks_added]
 * @property {string} [error]
 * @property {string} [skip_reason]
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
 * @property {{active: boolean, interval_minutes: number|null, paused: boolean}} [scheduler]
 */

var ZoteroRAGAutoIndexStatus = {
	/** @type {ZoteroRAGPlugin|null} */
	plugin: null,
	/** @type {number|null} */
	refreshTimer: null,
	/** @type {'own'|'all'} */
	adminScope: 'own',

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

		window.addEventListener('unload', () => {
			if (this.refreshTimer !== null) {
				clearInterval(this.refreshTimer);
				this.refreshTimer = null;
			}
		});

		this.fetchAndRender();
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
		if (data.aborted) {
			this.renderBanner('The last automatic indexing run was stopped by an admin.', 'idle');
		} else if (data.crashed) {
			this.renderBanner('The last automatic indexing run crashed unexpectedly.', 'crashed');
		} else if (data.running && ownSlugCount === 0) {
			// A run is active, but none of it is this caller's own libraries —
			// most likely another user's manual trigger or a shared-lock cron tick.
			this.renderBanner('Indexing server currently busy, please wait and try again later.', 'running');
		} else if (data.running) {
			this.renderBanner(`Running since ${this.formatTime(data.started_at)}…`, 'running');
		} else if (data.finished_at) {
			this.renderBanner(`Idle. Last run finished ${this.formatTime(data.finished_at)}.`, 'idle');
		} else {
			this.renderBanner('Idle. No automatic indexing run has happened yet.', 'idle');
		}

		this.renderLibraries(data.slugs || {}, data.is_admin === true, data.running === true);
		this.renderProblems(data.key_issues || []);
		this.updateRunNowButtonState(data);
		this.updateAdminControlsVisibility(data);
	},

	/**
	 * Enable/disable the "Run now" button based on server- and client-side
	 * indexing state.
	 * @param {AutoIndexStatusResponse} data
	 * @returns {void}
	 */
	updateRunNowButtonState(data) {
		const busy = data.running === true || (this.plugin && this.plugin.isClientIndexingActive());

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
		try {
			const response = await fetch(`${this.plugin.backendURL}/api/autoindex/scheduler/run-now`, {
				method: 'POST',
				headers: this.plugin.getAuthHeaders(),
			});
			if (!response.ok) {
				const body = await response.json().catch(() => ({}));
				this.renderBanner(body.detail || `Could not start indexing (HTTP ${response.status}).`, 'crashed');
				if (button) {
					button.disabled = false;
					this.setButtonLabel(button, 'Run full index now (all libraries)');
				}
				return;
			}
			await this.fetchAndRender();
		} catch (e) {
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
					button.textContent = 'Skip this job';
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
		try {
			const response = await fetch(`${this.plugin.backendURL}/api/autoindex/scheduler/run-slug`, {
				method: 'POST',
				headers: { ...this.plugin.getAuthHeaders(), 'Content-Type': 'application/json' },
				body: JSON.stringify({ slug }),
			});
			if (!response.ok) {
				const body = await response.json().catch(() => ({}));
				this.renderBanner(body.detail || `Could not start indexing (HTTP ${response.status}).`, 'crashed');
				if (button) {
					button.disabled = false;
					button.textContent = 'Run this library';
				}
				return;
			}
			await this.fetchAndRender();
		} catch (e) {
			this.renderBanner(`Error: ${e}`, 'crashed');
		}
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
		try {
			const response = await fetch(`${this.plugin.backendURL}/api/autoindex/run`, {
				method: 'POST',
				headers: this.plugin.getAuthHeaders(),
			});
			if (!response.ok) {
				const body = await response.json().catch(() => ({}));
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
	 * Render one row per library with a progress bar reflecting its status.
	 * @param {Record<string, AutoIndexSlugStatus>} slugs
	 * @param {boolean} [isAdmin]
	 * @param {boolean} [running] - whether any run (own or another's) is currently active server-side
	 * @returns {void}
	 */
	renderLibraries(slugs, isAdmin = false, running = false) {
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
			const info = slugs[slug];
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
				runButton.textContent = 'Run this library';
				runButton.disabled = running;
				runButton.dataset.runSlug = slug;
				runButton.addEventListener('click', () => this.runSlug(slug));
				header.appendChild(runButton);
			}

			if (isAdmin && (info.status === 'pending' || info.status === 'indexing')) {
				const skipButton = document.createElement('button');
				skipButton.type = 'button';
				skipButton.className = 'dialog-button library-skip-button';
				skipButton.textContent = 'Skip this job';
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

			if (info.error || info.skip_reason) {
				const errorDiv = document.createElement('div');
				errorDiv.className = 'library-error';
				errorDiv.textContent = info.error || info.skip_reason || '';
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
};

if (document.readyState === 'loading') {
	document.addEventListener('DOMContentLoaded', () => {
		ZoteroRAGAutoIndexStatus.init();
	});
} else {
	ZoteroRAGAutoIndexStatus.init();
}
