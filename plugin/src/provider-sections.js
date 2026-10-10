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
				provision: hidden, retry: hidden,
				credential: { visible: false, label: '', help: '', pattern: '' },
				progress: [], status: '', readOnlyNote: false,
			};
		}

		const running = !!job && job.status === 'running';
		const canProvision = provider.supports_provisioning && provider.operable_by_caller;
		const status = health ? health.status : '';
		const needsAction = this.NEEDS_PROVISIONING.has(status);
		const sideJob = job && job.sides ? job.sides[side] : undefined;
		const failed = !!sideJob && sideJob.status === 'failed';
		const busyReason = running ? 'A provisioning job is already running.' : '';

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
	 * @param {{onProvision: (side: 'embedding'|'llm', oneTimeKey: string) => void, onRetry: (side: 'embedding'|'llm', oneTimeKey: string) => void}} handlers
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
			actions.append(provision, retry);

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

			section.append(title, provider, health, hint, keys, credRow, credHelp, actions, note, progress, status);
			container.appendChild(section);
			refs[side] = { section, title, provider, health, hint, keys, credRow, credLabel, credInput, credHelp, provision, retry, note, progress, status };
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
			r.note.hidden = !m.readOnlyNote;
			r.note.textContent = m.readOnlyNote ? 'This endpoint is not available. Ask the server admin to provision it.' : '';
			r.progress.textContent = m.progress.join('\n');
			r.progress.hidden = m.progress.length === 0;
			r.status.textContent = m.status;
			r.status.hidden = !m.status;
		}
	},
};
