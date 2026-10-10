// Per-side configuration sections of the Preferences pane (embedding and answering model).
//
// Everything vendor-specific comes from the backend's provider descriptors
// (`GET /api/config/providers`): this file contains no provider names. It has two
// halves: pure functions that turn descriptors, health and job state into a view
// model, and a small DOM layer that builds each section once and then only updates it
// (so a half-typed key is never wiped by a status refresh).

// @ts-check

/// <reference path='./zotero-rag.js' />

/**
 * Credential a side's provisioning accepts for a single run.
 * @typedef {Object} ProvisioningCredential
 * @property {string} env
 * @property {string} label
 * @property {string} [help]
 * @property {string|null} [pattern]
 * @property {boolean} [optional]
 */

/**
 * @typedef {Object} ProviderDescriptor
 * @property {string} id
 * @property {string} label
 * @property {'user'|'managed'|'shared'} key_scope
 * @property {boolean} operable_by_caller
 * @property {boolean} supports_provisioning
 * @property {boolean} supports_suspend
 * @property {{credential?: ProvisioningCredential, hint?: string}|null} [provisioning]
 * @property {string|null} [unavailable_hint]
 */

/**
 * @typedef {Object} SideInfo
 * @property {'local'|'remote'} model_type
 * @property {ProviderDescriptor|null} provider
 */

/**
 * @typedef {{status: string, detail: string}|null} SideHealth
 */

/**
 * @typedef {Object} JobState
 * @property {'idle'|'running'|'succeeded'|'failed'} status
 * @property {string|null} [message]
 * @property {string[]} [progress]
 * @property {Record<string, {status: string, message: string|null}>} [sides]
 */

/**
 * @typedef {Object} ActionView
 * @property {boolean} visible
 * @property {string} label
 * @property {boolean} disabled
 * @property {string} reason - why it is disabled, '' otherwise
 */

/**
 * @typedef {Object} SectionModel
 * @property {'embedding'|'llm'} side
 * @property {string} title
 * @property {string} providerText - e.g. "Provider: X (your own account)", '' for a local model
 * @property {boolean} local
 * @property {{text: string, color: string, title: string}|null} health
 * @property {string} hint - advice shown while the side is not ready, '' otherwise
 * @property {ActionView} provision
 * @property {ActionView} retry
 * @property {ActionView} pause - stops billing and wake-ups; shown for a ready or cold side
 * @property {{visible: boolean, label: string, help: string, pattern: string}} credential
 * @property {string[]} progress - this side's progress lines
 * @property {string} status - outcome message of this side's last job, '' if none
 * @property {boolean} readOnlyNote - true when the side needs provisioning but the caller may not do it
 */

var ZoteroRAGProviderSections = {
	/** @type {Array<'embedding'|'llm'>} */
	SIDES: ['embedding', 'llm'],

	/** @type {Record<string, string>} */
	SIDE_TITLES: { embedding: 'Embedding model', llm: 'Answering model (LLM)' },

	/** @type {Record<string, string>} */
	HEALTH_COLORS: { ready: 'green', cold: 'orange', paused: 'orange', throttled: 'red', unreachable: 'red' },

	/** Statuses that provisioning (create, wake or resume) can fix; "cold" wakes by itself. */
	NEEDS_PROVISIONING: new Set(['unreachable', 'throttled', 'paused']),

	/** @type {Record<string, string>} */
	SCOPE_NOTES: {
		user: 'your own account',
		managed: 'operated by the server admin',
		shared: 'provided by your institution',
	},

	/**
	 * Build the view model of one side (pure).
	 * @param {'embedding'|'llm'} side
	 * @param {SideInfo|undefined} info - from GET /api/config/providers
	 * @param {SideHealth|undefined} health - from GET /api/config/health
	 * @param {JobState|null|undefined} job - the caller's provisioning job
	 * @returns {SectionModel}
	 */
	buildModel(side, info, health, job) {
		const title = this.SIDE_TITLES[side];
		const provider = info && info.provider;
		/** @type {ActionView} */
		const hidden = { visible: false, label: '', disabled: false, reason: '' };
		if (!info || !provider) {
			return {
				side, title, local: true,
				providerText: '',
				health: null, hint: '',
				provision: hidden, retry: hidden, pause: hidden,
				credential: { visible: false, label: '', help: '', pattern: '' },
				progress: [], status: '', readOnlyNote: false,
			};
		}

		// Sides are independent: only this side's own job disables its buttons.
		const sideState = job && job.sides ? job.sides[side] : undefined;
		const running = !!sideState && (sideState.status === 'running' || sideState.status === 'pending');
		const canProvision = provider.supports_provisioning && provider.operable_by_caller;
		const status = health ? health.status : '';
		const needsAction = this.NEEDS_PROVISIONING.has(status);
		const sideJob = job && job.sides ? job.sides[side] : undefined;
		const failed = !!sideJob && sideJob.status === 'failed';
		const busyReason = running ? 'A job for this endpoint is already running.' : '';

		const scope = this.SCOPE_NOTES[provider.key_scope] || '';
		const credential = provider.provisioning && provider.provisioning.credential;
		return {
			side, title, local: false,
			providerText: `Provider: ${provider.label}${scope ? ` (${scope})` : ''}`,
			health: health
				? {
					text: `● ${health.status}${health.status !== 'ready' && health.detail ? ` (${health.detail})` : ''}`,
					color: this.HEALTH_COLORS[health.status] || '',
					title: health.detail || '',
				}
				: null,
			hint: health && health.status !== 'ready' && provider.unavailable_hint ? provider.unavailable_hint : '',
			provision: {
				visible: canProvision && needsAction,
				label: status === 'paused' ? 'Resume' : 'Provision endpoint',
				disabled: running,
				reason: busyReason,
			},
			retry: {
				visible: canProvision && failed && !running,
				label: 'Retry',
				disabled: running,
				reason: busyReason,
			},
			pause: {
				visible: provider.supports_suspend && provider.operable_by_caller && (status === 'ready' || status === 'cold'),
				label: 'Pause',
				disabled: running,
				reason: busyReason,
			},
			credential: {
				visible: canProvision,
				label: credential ? credential.label : 'Key for this run',
				help: credential && credential.help ? credential.help : '',
				pattern: credential && credential.pattern ? credential.pattern : '',
			},
			progress: job && job.progress ? job.progress.filter((l) => l.startsWith(`${side}: `)).map((l) => l.slice(side.length + 2)) : [],
			status: sideJob && sideJob.status === 'failed' ? `Failed: ${sideJob.message || 'unknown error'}`
				: sideJob && sideJob.status === 'succeeded' ? 'Done.' : '',
			readOnlyNote: provider.supports_provisioning && !provider.operable_by_caller && needsAction,
		};
	},

	/**
	 * Build the skeleton of both sections once; returns references for `update`.
	 * @param {Document} doc
	 * @param {HTMLElement} container
	 * @param {{onProvision: (side: 'embedding'|'llm', oneTimeKey: string) => void, onRetry: (side: 'embedding'|'llm', oneTimeKey: string) => void, onPause?: (side: 'embedding'|'llm') => void}} handlers
	 * @returns {Record<'embedding'|'llm', Record<string, any>>}
	 */
	ensureSections(doc, container, handlers) {
		const NS = 'http://www.w3.org/1999/xhtml';
		/** @type {(tag: string, cls?: string) => any} */
		const el = (tag, cls = '') => {
			const node = doc.createElementNS(NS, tag);
			if (cls) node.className = cls;
			return node;
		};
		if (container.replaceChildren) container.replaceChildren();
		/** @type {Record<string, Record<string, any>>} */
		const refs = {};
		for (const side of this.SIDES) {
			const section = el('div', 'provider-section');
			section.id = `zotero-rag-side-${side}`;
			const title = el('div', 'provider-section-title');
			const provider = el('div', 'setting-description');
			const health = el('div', 'setting-description');
			const hint = el('div', 'setting-description');
			const keys = el('div', 'provider-section-keys');
			keys.id = `zotero-rag-side-${side}-keys`;

			const credRow = el('div', 'setting-row');
			const credLabel = el('label');
			const credInput = el('input', 'setting-input');
			credInput.type = 'password';
			credInput.id = `zotero-rag-side-${side}-credential`;
			credInput.placeholder = 'Optional — used once, never stored';
			credLabel.setAttribute('for', credInput.id);
			const credHelp = el('div', 'setting-description');
			credRow.append(credLabel, credInput);

			const actions = el('div', 'setting-row');
			const provision = el('button');
			provision.id = `zotero-rag-side-${side}-provision`;
			const retry = el('button');
			retry.id = `zotero-rag-side-${side}-retry`;
			const pause = el('button');
			pause.id = `zotero-rag-side-${side}-pause`;
			actions.append(provision, retry, pause);

			const note = el('div', 'setting-description');
			const progress = el('div', 'setting-description');
			const status = el('div', 'setting-description');

			const oneTimeKey = () => {
				const value = String(credInput.value || '').trim();
				credInput.value = ''; // never kept, never saved
				return value;
			};
			provision.addEventListener('click', () => handlers.onProvision(side, oneTimeKey()));
			retry.addEventListener('click', () => handlers.onRetry(side, oneTimeKey()));
			pause.addEventListener('click', () => handlers.onPause && handlers.onPause(side));

			section.append(title, provider, health, hint, keys, credRow, credHelp, actions, note, progress, status);
			container.appendChild(section);
			refs[side] = { section, title, provider, health, hint, keys, credRow, credLabel, credInput, credHelp, provision, retry, pause, note, progress, status };
		}
		return /** @type {any} */ (refs);
	},

	/**
	 * Apply view models to the skeleton built by `ensureSections`.
	 * @param {Record<'embedding'|'llm', Record<string, any>>} refs
	 * @param {Record<'embedding'|'llm', SectionModel>} models
	 * @returns {void}
	 */
	update(refs, models) {
		for (const side of this.SIDES) {
			const r = refs[side];
			const m = models[side];
			r.title.textContent = m.title;
			r.provider.textContent = m.local ? 'Runs on this server; nothing to configure here.' : m.providerText;
			r.health.textContent = m.health ? `${m.title}: ${m.health.text}` : '';
			r.health.style.color = m.health ? m.health.color : '';
			r.health.title = m.health ? m.health.title : '';
			r.hint.textContent = m.hint;
			r.hint.hidden = !m.hint;
			r.credRow.hidden = !m.credential.visible;
			r.credHelp.hidden = !m.credential.visible || !m.credential.help;
			r.credLabel.textContent = m.credential.visible ? `${m.credential.label}:` : '';
			r.credHelp.textContent = m.credential.help;
			if (m.credential.pattern) r.credInput.pattern = m.credential.pattern;
			r.provision.hidden = !m.provision.visible;
			r.provision.textContent = m.provision.label;
			r.provision.disabled = m.provision.disabled;
			r.provision.title = m.provision.reason;
			r.retry.hidden = !m.retry.visible;
			r.retry.textContent = m.retry.label;
			r.retry.disabled = m.retry.disabled;
			r.retry.title = m.retry.reason;
			r.pause.hidden = !m.pause.visible;
			r.pause.textContent = m.pause.label;
			r.pause.disabled = m.pause.disabled;
			r.pause.title = m.pause.reason;
			r.note.hidden = !m.readOnlyNote;
			r.note.textContent = m.readOnlyNote ? 'This endpoint is not available. Ask the server admin to provision it.' : '';
			r.progress.textContent = m.progress.join('\n');
			r.progress.hidden = m.progress.length === 0;
			r.status.textContent = m.status;
			r.status.hidden = !m.status;
		}
	},
};

/**
 * Drives the sections against the backend. Dependencies are injected so the logic is
 * testable without a browser: `get(path)` resolves parsed JSON or null on failure,
 * `post(path, body)` resolves `{ok, status, data}`.
 * @param {{
 *   refs: Record<'embedding'|'llm', Record<string, any>>,
 *   get: (path: string) => Promise<any>,
 *   post: (path: string, body: any) => Promise<{ok: boolean, status: number, data: any}>,
 *   sleep?: (ms: number) => Promise<void>,
 *   pollMs?: number,
 * }} deps
 */
ZoteroRAGProviderSections.createController = function (deps) {
	const S = ZoteroRAGProviderSections;
	const sleep = deps.sleep || ((/** @type {number} */ ms) => new Promise((resolve) => setTimeout(resolve, ms)));
	const pollMs = deps.pollMs === undefined ? 5000 : deps.pollMs;
	/** @type {{providers: any, health: any, job: any}} */
	const state = { providers: null, health: null, job: null };
	let polling = false;

	/** Re-fetch providers, endpoint health and the caller's job, and repaint. */
	const refresh = async () => {
		const [providers, health, job] = await Promise.all([
			deps.get('/api/config/providers'),
			deps.get('/api/config/health'),
			deps.get('/api/config/provision/status'),
		]);
		if (providers) state.providers = providers;
		if (health) state.health = health;
		if (job) state.job = job;
		/** @type {any} */
		const models = {};
		for (const side of S.SIDES) {
			models[side] = S.buildModel(
				side,
				state.providers && state.providers.sides ? state.providers.sides[side] : undefined,
				state.health ? state.health[side] : undefined,
				state.job,
			);
		}
		S.update(deps.refs, models);
	};

	/** Refresh now and, while the caller's job runs, every few seconds (this also resumes a job started earlier). */
	const poll = async () => {
		if (polling) return;
		polling = true;
		try {
			for (;;) {
				await refresh();
				if (!state.job || state.job.status !== 'running') break;
				await sleep(pollMs);
			}
		} finally {
			polling = false;
		}
	};

	/**
	 * Provision (create, wake or resume) or retry one side. Only that side is sent; the
	 * one-time key goes with this request only and is never kept.
	 * @param {'embedding'|'llm'} side
	 * @param {string} oneTimeKey
	 */
	const startJob = async (path, side, oneTimeKey, failure) => {
		const status = deps.refs[side].status;
		const show = (/** @type {string} */ text) => { status.textContent = text; status.hidden = !text; };
		const provider = state.providers && state.providers.sides && state.providers.sides[side] && state.providers.sides[side].provider;
		const credential = provider && provider.provisioning && provider.provisioning.credential;
		if (oneTimeKey && credential && credential.pattern && !new RegExp(credential.pattern).test(oneTimeKey)) {
			show('The key does not look right.');
			return;
		}
		/** @type {{sides: string[], keys?: Record<string,string>}} */
		const body = { sides: [side] };
		if (oneTimeKey && credential) body.keys = { [credential.env]: oneTimeKey };
		show('Starting\u2026');
		try {
			const result = await deps.post(path, body);
			if (!result.ok) {
				const detail = result.data && result.data.detail ? result.data.detail : `HTTP ${result.status}`;
				show(`${failure}: ${detail}`);
				return;
			}
			show('');
			await poll();
		} catch (e) {
			show(`${failure}: ${e}`);
		}
	};

	const provision = (/** @type {'embedding'|'llm'} */ side, /** @type {string} */ oneTimeKey) =>
		startJob('/api/config/provision', side, oneTimeKey, 'Provisioning failed');
	/** Pause one side (stops billing and wake-ups; resuming is provisioning again). */
	const pause = (/** @type {'embedding'|'llm'} */ side) => startJob('/api/config/suspend', side, '', 'Pausing failed');

	return { state, refresh, poll, provision, pause };
};

/**
 * Request header that carries a provider key: KEY_NAME -> X-Key-Name (the backend's
 * env_var_to_header; header names are case-insensitive).
 * @param {string} keyName
 * @returns {string}
 */
ZoteroRAGProviderSections.headerForKey = function (keyName) {
	return 'X-' + keyName.split('_').map((p) => p.charAt(0).toUpperCase() + p.slice(1).toLowerCase()).join('-');
};

/**
 * What the preset controls show (pure).
 *
 * A loopback server has no per-user choice: one control changes the server default. Elsewhere
 * every user has "My preset" (their own choice among compatible presets, those needing a key
 * marked) and admins also get a "Server default" control.
 * @param {{loopback?: boolean, is_admin?: boolean, default: string, effective: string, fell_back?: string|null,
 *   selectable?: Array<{name: string, credentials: string, missing_keys?: string[]}>}} my - GET /api/config/my-preset
 * @param {{compatible_presets?: string[], default_preset?: string, preset_name: string,
 *   switchable_presets?: Array<{name: string}>}} cfg - GET /api/config
 * @returns {{mode: 'loopback'|'user', label: string, options: Array<{value: string, text: string}>, selected: string,
 *   showDefault: boolean, defaultOptions: Array<{value: string, text: string}>, defaultSelected: string, note: string}}
 */
ZoteroRAGProviderSections.buildPresetControls = function (my, cfg) {
	const fellBack = my.fell_back
		? `Your saved preset "${my.fell_back}" is not available any more; using the server default.`
		: '';
	if (my.loopback) {
		const selected = cfg.default_preset || cfg.preset_name;
		return {
			mode: 'loopback', label: 'Preset:',
			options: (cfg.compatible_presets || []).map((n) => ({ value: n, text: n })), selected,
			showDefault: false, defaultOptions: [], defaultSelected: '', note: '',
		};
	}
	const names = new Set([cfg.default_preset || my.default, ...(cfg.switchable_presets || []).map((p) => p.name)]);
	return {
		mode: 'user', label: 'My preset:',
		options: (my.selectable || []).map((p) => ({
			value: p.name,
			text: p.name + (p.name === my.default ? ' (server default)' : '') + (p.credentials === 'missing' ? ' \u2014 needs a key' : ''),
		})),
		selected: my.effective,
		showDefault: !!my.is_admin,
		defaultOptions: [...names].map((n) => ({ value: n, text: n })),
		defaultSelected: cfg.default_preset || my.default,
		note: fellBack,
	};
};

/**
 * Introduction text of the setup wizard's key step: names the preset the user starts on
 * and only the keys it needs, and points at the optional upgrade to their own account.
 * @param {{preset_name: string, preset_description?: string, default_preset?: string}} cfg - GET /api/config
 * @param {Array<{key_name: string}>} keys - GET /api/required-keys
 * @returns {string}
 */
ZoteroRAGProviderSections.keysIntro = function (cfg, keys) {
	const description = cfg.preset_description ? ` \u2014 ${cfg.preset_description}` : '';
	const needs = keys.length
		? `It needs ${keys.length === 1 ? 'this key' : 'these keys'}, which you can get from the provider's website.`
		: 'It needs no key from you.';
	return `You start on the server's preset "${cfg.preset_name}"${description}. ${needs} ` +
		'Later, under Preferences \u2192 Model preset, you can add your own provider account to speed things up.';
};
