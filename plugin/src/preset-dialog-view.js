// "Configure preset" dialog: renders GET /api/config/preset-view and runs its actions.
//
// The backend builds the whole view model (backend/providers/ui.py): one tab per side of
// the pipeline, one account card per service provider, and every action as a request to
// make. This file contains no provider names. It has three parts: pure helpers (overall
// state, Start all / Pause all request merging), a DOM renderer and a controller that
// talks to the backend through injected functions, so all of it runs under node --test.

// @ts-check

/**
 * @typedef {'ready'|'starting'|'down'|'unknown'|'off'} SideState
 *
 * @typedef {Object} UiField
 * @property {string} id
 * @property {string} label
 * @property {'text'|'secret'|'number'|'select'|'readonly'|'toggle'} type
 * @property {any} [value]
 * @property {any} [default]
 * @property {string} [placeholder]
 * @property {string} [help]
 * @property {string|null} [pattern]
 * @property {string[]} [options]
 * @property {number|null} [min]
 * @property {number|null} [max]
 * @property {'none'|'client_pref'|'client_secret'|'shared'|'session'|'login'} storage
 * @property {string|null} [key]
 * @property {string|null} [header]
 * @property {boolean|null} [is_set]
 * @property {boolean} [optional]
 * @property {boolean} [editable]
 * @property {string|null} [docs_url]
 * @property {string[]} [required_for]
 *
 * @typedef {{level: 'ok'|'info'|'warn'|'error', text: string, link?: string|null}} UiMessage
 * @typedef {{label: string, value: string}} UiFact
 * @typedef {{method: 'POST', path: string, body: Record<string, any>}} UiRequest
 *
 * @typedef {Object} UiAction
 * @property {string} id
 * @property {string} label
 * @property {'primary'|'normal'|'danger'} kind
 * @property {boolean} enabled
 * @property {string} reason
 * @property {'start'|'pause'|null} [workflow]
 * @property {UiRequest|null} [request]
 * @property {string|null} [account]
 * @property {boolean} [sends_login] - a card action that submits the card's `login` fields as `body.values`
 *
 * @typedef {{id: string, unit: string, period?: string|null, limit: number, remaining: number, resets_at?: string|null}} QuotaMeter
 * @typedef {{used_pct: number, level: 'ok'|'warn'|'error', text: string, meters: QuotaMeter[]}} UiQuota
 *
 * @typedef {Object} SideView
 * @property {string} side
 * @property {string} title
 * @property {boolean} optional
 * @property {boolean} enabled
 * @property {{id: string, label: string}|null} provider
 * @property {string|null} account
 * @property {SideState} state
 * @property {string} summary
 * @property {UiField[]} fields
 * @property {UiMessage[]} messages
 * @property {UiFact[]} facts
 * @property {string[]} log
 * @property {UiAction[]} actions
 * @property {UiQuota|null} [quota] - combined by the provider; `meters` are the details
 *
 * @typedef {Object} AccountCard
 * @property {string} id
 * @property {string} label
 * @property {string[]} serves
 * @property {'ready'|'needs_input'} state
 * @property {string} scope_note
 * @property {UiField[]} fields
 * @property {UiMessage[]} messages
 * @property {UiAction[]} [actions] - log in / log out of a provider with an account login
 * @property {boolean} auto_start
 * @property {UiQuota|null} [quota] - the most used quota of the sides it serves
 *
 * @typedef {Object} PresetView
 * @property {{name: string, description: string, is_admin: boolean, default_options: string[], default_preset: string}} preset
 * @property {SideView[]} sides
 * @property {AccountCard[]} accounts
 * @property {boolean} busy
 *
 * @typedef {{get: (env: string) => string, set: (env: string, value: string) => void, forget: (env: string) => void}} SessionKeys
 *
 * @typedef {{preset: string, cost: string, label: string, needs: string[]}} StandinOption
 * @typedef {{provider: string, message: string, standins: StandinOption[]}} StandinOffer - GET /api/config/standins `offer` (fields used here)
 * @typedef {{side: string, provider: string, choices: Array<{preset: string, text: string, ready?: boolean}>}} StandinList - the offer shown on a side
 *
 * @typedef {Object} LocalState - what the dialog keeps between refreshes
 * @property {string} tab
 * @property {Record<string, boolean>} logOpen
 * @property {Record<string, string>} drafts
 * @property {Record<string, {level: string, text: string}>} notes
 * @property {StandinList|null} [standins]
 */

var ZoteroRAGPresetDialog = {
	/** @type {Record<SideState, string>} */
	STATE_LABELS: { ready: 'Ready', starting: 'Starting', down: 'Down', unknown: 'Not checked', off: 'Off' },

	/** Preference branch of client-side settings (`client_pref` fields). */
	PREF_PREFIX: 'extensions.zotero-rag.',

	/** Preference branch of personal keys (`client_secret` fields). */
	SECRET_PREFIX: 'extensions.zotero-rag.serviceApiKey.',

	/**
	 * Overall state of the preset and a one-line summary, ignoring switched-off sides.
	 * @param {SideView[]} sides
	 * @returns {{state: SideState, text: string}}
	 */
	overall(sides) {
		const active = sides.filter((s) => s.state !== 'off');
		/** @param {SideState} st */
		const n = (st) => active.filter((s) => s.state === st).length;
		if (!active.length) return { state: 'unknown', text: 'Not connected' };
		if (n('down')) return { state: 'down', text: this._countText(active.length, n) };
		if (n('starting')) return { state: 'starting', text: this._countText(active.length, n) };
		if (n('unknown')) return { state: 'ready', text: `${n('ready')} of ${active.length} ready, ${n('unknown')} not checked` };
		return { state: 'ready', text: 'All ready' };
	},

	/**
	 * @param {number} total
	 * @param {(st: SideState) => number} n
	 * @returns {string}
	 */
	_countText(total, n) {
		const parts = [`${n('ready')} of ${total} ready`];
		if (n('starting')) parts.push(`${n('starting')} starting`);
		if (n('down')) parts.push(`${n('down')} down`);
		return parts.join(', ');
	},

	/**
	 * The requests Start all / Pause all make: every enabled action of that workflow on
	 * the given sides, with requests to the same path merged (their `sides` joined), so
	 * one provisioning job covers several sides.
	 * @param {PresetView} view
	 * @param {'start'|'pause'} kind
	 * @param {string[]} [onlySides] - limit to these sides (an account card's `serves`)
	 * @returns {Array<{path: string, body: Record<string, any>, accounts: string[]}>}
	 */
	workflowRequests(view, kind, onlySides) {
		/** @type {Map<string, {path: string, body: Record<string, any>, accounts: string[]}>} */
		const merged = new Map();
		for (const side of view.sides) {
			if (onlySides && !onlySides.includes(side.side)) continue;
			// A switched-off side is never started; pausing it still stops its billing.
			if (kind === 'start' && side.state === 'off') continue;
			for (const action of side.actions) {
				if (action.workflow !== kind || !action.enabled || !action.request) continue;
				const { path, body } = action.request;
				const entry = merged.get(path);
				if (!entry) {
					merged.set(path, {
						path,
						body: JSON.parse(JSON.stringify(body || {})),
						accounts: action.account ? [action.account] : [],
					});
					continue;
				}
				if (Array.isArray(entry.body.sides) && Array.isArray(body.sides)) {
					for (const s of body.sides) if (!entry.body.sides.includes(s)) entry.body.sides.push(s);
				}
				if (action.account && !entry.accounts.includes(action.account)) entry.accounts.push(action.account);
			}
		}
		return [...merged.values()];
	},

	/**
	 * Fill client-side settings (`client_pref` fields) from this Zotero's preferences and
	 * mark an optional side whose switch (a `toggle` field) is off as `off`. Mutates `view`.
	 * @param {PresetView} view
	 * @param {(name: string) => any} getPref
	 * @returns {PresetView}
	 */
	applyLocalState(view, getPref) {
		for (const side of view.sides) {
			for (const field of side.fields) {
				if (field.storage !== 'client_pref' || !field.key) continue;
				const stored = getPref(this.PREF_PREFIX + field.key);
				if (field.type === 'toggle') {
					field.value = stored === undefined || stored === null ? field.default !== false : stored !== false;
					if (side.optional && field.value === false) {
						side.enabled = false;
						side.state = 'off';
						side.summary = 'Off: switched off for your queries';
					}
				} else {
					field.value = stored === undefined || stored === null ? '' : stored;
				}
			}
		}
		return view;
	},

	/**
	 * The login form values typed into an account card, by field key (login fields only).
	 * @param {AccountCard} card
	 * @param {Record<string, string>} drafts
	 * @returns {Record<string, string>}
	 */
	loginValues(card, drafts) {
		/** @type {Record<string, string>} */
		const values = {};
		for (const field of card.fields) {
			if (field.storage !== 'login' || !field.key) continue;
			const draft = drafts[`account:${card.id}/${field.id}`];
			if (draft !== undefined && draft.trim()) values[field.key] = draft.trim();
		}
		return values;
	},

	/**
	 * One-time credentials (memory only) of the given account cards, for a request's `keys`.
	 * @param {PresetView} view
	 * @param {string[]} accountIds
	 * @param {SessionKeys} sessionKeys
	 * @returns {Record<string, string>}
	 */
	sessionKeysFor(view, accountIds, sessionKeys) {
		/** @type {Record<string, string>} */
		const keys = {};
		for (const card of view.accounts) {
			if (!accountIds.includes(card.id)) continue;
			for (const field of card.fields) {
				if (field.storage !== 'session' || !field.key) continue;
				const value = sessionKeys.get(field.key);
				if (value) keys[field.key] = value;
			}
		}
		return keys;
	},

	/**
	 * One stand-in of an offer as a line: preset, free/paid, label and what it still needs
	 * (pure). A `key:<NAME>` need is dropped when the user has that key stored.
	 * @param {StandinOption} choice
	 * @param {(keyName: string) => boolean} hasKey
	 * @returns {string}
	 */
	describeStandin(choice, hasKey) {
		const needs = (choice.needs || [])
			.filter((n) => !(n.startsWith('key:') && hasKey(n.slice(4))))
			.map((n) => {
				const cut = n.indexOf(':');
				const [kind, what] = [n.slice(0, cut), n.slice(cut + 1)];
				return kind === 'login' ? `${what.toUpperCase()} login` : what;
			});
		return `${choice.preset} (${choice.cost}): ${choice.label}; ${needs.length ? `needs ${needs.join(', ')}` : 'ready'}`;
	},

	/**
	 * Bar and label of one quota meter (pure). Amber from 75 % used, red from 95 %.
	 * @param {QuotaMeter} meter
	 * @returns {{usedPct: number, level: 'ok'|'warn'|'error', text: string}|null}
	 */
	describeMeter(meter) {
		if (!meter || !meter.limit) return null;
		const usedPct = Math.max(0, Math.min(100, Math.round((meter.limit - meter.remaining) / meter.limit * 100)));
		const level = usedPct >= 95 ? 'error' : usedPct >= 75 ? 'warn' : 'ok';
		const per = meter.period ? ` per ${meter.period}` : '';
		return { usedPct, level, text: `${meter.remaining} of ${meter.limit} ${meter.unit} left${per}` };
	},

	/**
	 * Check a typed value against a field before saving it.
	 * @param {UiField} field
	 * @param {string} raw
	 * @returns {string} an error message, '' when the value is fine
	 */
	validate(field, raw) {
		const value = raw.trim();
		if (!value) return '';
		if (field.type === 'number') {
			const n = Number(value);
			if (!Number.isFinite(n) || !Number.isInteger(n)) return `${field.label} must be a whole number.`;
			if (field.min !== undefined && field.min !== null && n < field.min) return `${field.label} must be at least ${field.min}.`;
			if (field.max !== undefined && field.max !== null && n > field.max) return `${field.label} must be at most ${field.max}.`;
		}
		if (field.pattern && !new RegExp(`^(?:${field.pattern})$`).test(value)) {
			return `${field.label} does not have the expected format.`;
		}
		return '';
	},
};

// -- DOM ---------------------------------------------------------------------------

/**
 * Build the dialog's fixed skeleton inside `root`; returns the containers `paint` fills.
 * @param {Document} doc
 * @param {HTMLElement} root
 * @returns {Record<string, any>}
 */
ZoteroRAGPresetDialog.mount = function (doc, root) {
	const el = this._el(doc);
	const header = el('div', 'pd-header');
	const name = el('div', 'pd-name');
	const title = el('h2', 'pd-title');
	const description = el('div', 'pd-description');
	const defaultRow = el('div', 'pd-default-row');
	const defaultLabel = el('label', 'pd-muted');
	defaultLabel.textContent = 'Server default (admin):';
	defaultLabel.setAttribute('for', 'pd-default-select');
	const defaultSelect = el('select', 'pd-select');
	defaultSelect.id = 'pd-default-select';
	defaultRow.append(defaultLabel, defaultSelect);
	name.append(title, description, defaultRow);
	const overall = el('div', 'pd-overall');
	const headerActions = el('div', 'pd-header-actions');
	const startAll = el('button', 'pd-button pd-primary');
	startAll.id = 'pd-start-all';
	startAll.textContent = 'Start all';
	startAll.title = 'Start, wake or resume every endpoint that is not running';
	const pauseAll = el('button', 'pd-button');
	pauseAll.id = 'pd-pause-all';
	pauseAll.textContent = 'Pause all';
	pauseAll.title = 'Pause every endpoint that can be paused (stops billing)';
	const checkNow = el('button', 'pd-button');
	checkNow.id = 'pd-check-now';
	checkNow.textContent = 'Check now';
	headerActions.append(startAll, pauseAll, checkNow);
	const headerNote = el('div', 'pd-header-note');
	header.append(name, overall, headerActions, headerNote);

	const tabs = el('div', 'pd-tabs');
	tabs.setAttribute('role', 'tablist');
	const panel = el('div', 'pd-panel');
	panel.setAttribute('role', 'tabpanel');

	const footer = el('div', 'pd-footer');
	const footerHead = el('div', 'pd-footer-head');
	const footerTitle = el('h3');
	footerTitle.textContent = 'Service accounts';
	const footerHint = el('span', 'pd-muted');
	footerHint.textContent = 'One set of credentials per provider. Saving them starts every endpoint that uses them.';
	footerHead.append(footerTitle, footerHint);
	const cards = el('div', 'pd-cards');
	const noCards = el('div', 'pd-muted');
	noCards.textContent = 'This preset needs no service account.';
	footer.append(footerHead, cards, noCards);

	if (root.replaceChildren) root.replaceChildren();
	root.append(header, tabs, panel, footer);
	return { title, description, defaultRow, defaultSelect, overall, startAll, pauseAll, checkNow, headerNote, tabs, panel, cards, noCards };
};

/**
 * @param {Document} doc
 * @returns {(tag: string, cls?: string) => any}
 */
ZoteroRAGPresetDialog._el = function (doc) {
	const NS = 'http://www.w3.org/1999/xhtml';
	return (tag, cls = '') => {
		const node = doc.createElementNS(NS, tag);
		if (cls) node.className = cls;
		return node;
	};
};

/**
 * A status dot.
 * @param {(tag: string, cls?: string) => any} el
 * @param {SideState|string} state
 * @returns {any}
 */
ZoteroRAGPresetDialog._dot = function (el, state) {
	const dot = el('span', `pd-dot pd-${state}`);
	dot.title = this.STATE_LABELS[/** @type {SideState} */ (state)] || state;
	return dot;
};

/**
 * Paint a view into the containers from `mount`. `local` holds what the dialog keeps
 * between refreshes (selected tab, open logs, drafts, transient notes); handlers run actions.
 * A section that has keyboard focus is not rebuilt, so a refresh never eats typing.
 * @param {Document} doc
 * @param {Record<string, any>} refs
 * @param {PresetView} view
 * @param {LocalState} local
 * @param {{onTab: (side: string) => void, onToggleLog: (side: string) => void,
 *   onAction: (action: UiAction) => void, onFieldChange: (field: UiField, value: string, scope: string) => void,
 *   onSaveAccount: (card: AccountCard) => void, onDefaultChange: (name: string) => void, onDraft?: () => void,
 *   onCardAction?: (card: AccountCard, action: UiAction) => void, onUseStandin?: (preset: string) => void}} handlers
 */
ZoteroRAGPresetDialog.paint = function (doc, refs, view, local, handlers) {
	const el = this._el(doc);
	/** @param {any} node */
	const hasFocus = (node) => {
		const active = doc.activeElement;
		return !!active && !!node && typeof node.contains === 'function' && node !== active && node.contains(active);
	};

	// Header
	refs.title.textContent = view.preset.name;
	refs.description.textContent = view.preset.description || '';
	const showDefault = view.preset.is_admin && view.preset.default_options.length > 0;
	refs.defaultRow.hidden = !showDefault;
	if (showDefault && !hasFocus(refs.defaultRow)) {
		refs.defaultSelect.replaceChildren();
		for (const option of view.preset.default_options) {
			const o = el('option');
			o.value = option;
			o.textContent = option;
			refs.defaultSelect.appendChild(o);
		}
		refs.defaultSelect.value = view.preset.default_preset;
		refs.defaultSelect.onchange = () => handlers.onDefaultChange(refs.defaultSelect.value);
	}
	const overall = this.overall(view.sides);
	refs.overall.replaceChildren(this._dot(el, overall.state), doc.createTextNode ? doc.createTextNode(` ${overall.text}`) : overall.text);
	refs.startAll.disabled = this.workflowRequests(view, 'start').length === 0;
	refs.pauseAll.disabled = this.workflowRequests(view, 'pause').length === 0;
	const headerNote = local.notes.header;
	refs.headerNote.textContent = headerNote ? headerNote.text : '';
	refs.headerNote.className = `pd-header-note pd-msg-${headerNote ? headerNote.level : 'info'}`;
	refs.headerNote.hidden = !headerNote;

	// Tabs
	if (!view.sides.some((s) => s.side === local.tab)) local.tab = view.sides.length ? view.sides[0].side : '';
	refs.tabs.replaceChildren();
	for (const side of view.sides) {
		const tab = el('button', 'pd-tab');
		tab.setAttribute('role', 'tab');
		tab.setAttribute('aria-selected', String(side.side === local.tab));
		tab.append(this._dot(el, side.state), el('span'));
		tab.children[1].textContent = side.title;
		tab.title = `${side.title}: ${side.summary}`;
		tab.addEventListener('click', () => handlers.onTab(side.side));
		refs.tabs.appendChild(tab);
	}

	// Panel of the selected side
	const side = view.sides.find((s) => s.side === local.tab);
	if (side) this._paintSide(doc, refs.panel, side, local, handlers, hasFocus);

	// Account cards
	refs.noCards.hidden = view.accounts.length > 0;
	if (!hasFocus(refs.cards)) {
		refs.cards.replaceChildren();
		for (const card of view.accounts) refs.cards.appendChild(this._card(doc, card, view, local, handlers));
	}
};

/**
 * @param {Document} doc
 * @param {any} panel
 * @param {SideView} side
 * @param {LocalState} local
 * @param {any} handlers
 * @param {(node: any) => boolean} hasFocus
 */
ZoteroRAGPresetDialog._paintSide = function (doc, panel, side, local, handlers, hasFocus) {
	const el = this._el(doc);
	// Rebuild only when the side changed or nothing in its configuration has focus.
	let sections = panel.children && panel.children[0] && panel.children[0].dataset && panel.children[0].dataset.side === side.side
		? panel.children : null;
	if (!sections) {
		panel.replaceChildren();
		const config = this._section(el, 'Configuration');
		const status = this._section(el, 'Status');
		const actions = this._section(el, 'Actions');
		if (config.dataset) config.dataset.side = side.side;
		panel.append(config, status, actions);
		sections = panel.children;
	}
	const [config, status, actions] = sections;

	// Configuration
	if (!hasFocus(config)) {
		const body = config.children[1];
		body.replaceChildren();
		config.children[0].children[1].textContent = side.provider ? side.provider.label : '';
		for (const field of side.fields) body.appendChild(this._field(doc, field, `side:${side.side}`, local, handlers));
		const owner = el('div', 'pd-muted');
		owner.textContent = side.account
			? 'Credentials: the account card below.'
			: side.fields.length ? '' : 'Nothing to configure for this side.';
		if (owner.textContent) body.appendChild(owner);
	}

	// Status
	const statusBody = status.children[1];
	status.children[0].children[1].replaceChildren(this._dot(el, side.state), el('span'));
	status.children[0].children[1].children[1].textContent = ` ${side.summary}`;
	statusBody.replaceChildren();
	const note = local.notes[`side:${side.side}`];
	for (const message of note ? [note, ...side.messages] : side.messages) {
		statusBody.appendChild(this._message(el, /** @type {UiMessage} */ (message)));
	}
	if (side.facts.length) {
		const facts = el('div', 'pd-facts');
		for (const fact of side.facts) {
			const item = el('span');
			const value = el('b');
			value.textContent = fact.value;
			item.append(doc.createTextNode ? doc.createTextNode(`${fact.label}: `) : `${fact.label}: `, value);
			facts.appendChild(item);
		}
		statusBody.appendChild(facts);
	}
	if (side.quota) statusBody.appendChild(this._quota(el, side.quota));
	if (side.log.length) {
		const toggle = el('button', 'pd-link');
		const open = !!local.logOpen[side.side];
		toggle.textContent = `${open ? 'Hide' : 'Show'} log (${side.log.length} ${side.log.length === 1 ? 'line' : 'lines'})`;
		toggle.addEventListener('click', () => handlers.onToggleLog(side.side));
		statusBody.appendChild(toggle);
		if (open) {
			const log = el('pre', 'pd-log');
			log.textContent = side.log.join('\n');
			statusBody.appendChild(log);
		}
	}

	// Actions
	actions.hidden = side.actions.length === 0;
	const actionsBody = actions.children[1];
	actionsBody.replaceChildren();
	const row = el('div', 'pd-actions');
	for (const action of side.actions) {
		const button = el('button', `pd-button${action.kind === 'primary' ? ' pd-primary' : action.kind === 'danger' ? ' pd-danger' : ''}`);
		button.textContent = action.label;
		button.disabled = !action.enabled;
		button.title = action.reason || '';
		button.addEventListener('click', () => handlers.onAction(action));
		row.appendChild(button);
	}
	for (const reason of new Set(side.actions.filter((a) => !a.enabled && a.reason).map((a) => a.reason))) {
		const why = el('span', 'pd-muted');
		why.textContent = reason;
		row.appendChild(why);
	}
	actionsBody.appendChild(row);
	// The stand-ins the "standin" action fetched for this side, each with Use.
	const offer = local.standins;
	if (offer && offer.side === side.side) {
		const list = el('div', 'pd-standins');
		for (const choice of offer.choices) {
			const item = el('div', 'pd-standin');
			const label = el('span');
			label.textContent = choice.text;
			const use = el('button', 'pd-button');
			use.textContent = 'Use';
			use.addEventListener('click', () => { if (handlers.onUseStandin) handlers.onUseStandin(choice.preset); });
			item.append(label, use);
			list.appendChild(item);
		}
		actionsBody.appendChild(list);
	}
};

/**
 * A side's quota: the provider's combined value and one bar per meter.
 * @param {(tag: string, cls?: string) => any} el
 * @param {UiQuota} quota
 * @returns {any}
 */
ZoteroRAGPresetDialog._quota = function (el, quota) {
	const box = el('div', 'pd-quota');
	const head = el('div', `pd-quota-head pd-msg-${quota.level}`);
	head.textContent = `Quota: ${quota.text}`;
	box.appendChild(head);
	for (const meter of quota.meters || []) {
		const info = this.describeMeter(meter);
		if (!info) continue;
		const row = el('div', 'pd-quota-row');
		const track = el('div', 'pd-quota-track');
		const bar = el('div', `pd-quota-bar pd-quota-${info.level}`);
		bar.style.width = `${info.usedPct}%`;
		track.appendChild(bar);
		const label = el('span', 'pd-muted');
		label.textContent = info.text;
		row.append(track, label);
		box.appendChild(row);
	}
	return box;
};

/**
 * A section box with a heading row (label + right-hand slot) and a body.
 * @param {(tag: string, cls?: string) => any} el
 * @param {string} label
 * @returns {any}
 */
ZoteroRAGPresetDialog._section = function (el, label) {
	const section = el('div', 'pd-section');
	const head = el('div', 'pd-section-head');
	const tag = el('span', 'pd-section-tag');
	tag.textContent = label;
	const slot = el('span', 'pd-section-slot');
	head.append(tag, slot);
	const body = el('div', 'pd-section-body');
	section.append(head, body);
	return section;
};

/**
 * @param {(tag: string, cls?: string) => any} el
 * @param {UiMessage} message
 * @returns {any}
 */
ZoteroRAGPresetDialog._message = function (el, message) {
	const node = el('div', `pd-msg pd-msg-${message.level}`);
	node.textContent = message.text;
	return node;
};

/**
 * One field row. Drafts (typed but not yet saved) survive a repaint.
 * @param {Document} doc
 * @param {UiField} field
 * @param {string} scope - "side:<side>" or "account:<id>"
 * @param {{drafts: Record<string, string>}} local
 * @param {any} handlers
 * @returns {any}
 */
ZoteroRAGPresetDialog._field = function (doc, field, scope, local, handlers) {
	const el = this._el(doc);
	const row = el('div', 'pd-field');
	const label = el('label');
	const id = `pd-${scope.replace(/[^a-zA-Z0-9]+/g, '-')}-${field.id.replace(/[^a-zA-Z0-9]+/g, '-')}`;
	label.textContent = field.label;
	label.setAttribute('for', id);
	/** @type {any} */
	let control;
	if (field.type === 'readonly' || field.editable === false) {
		control = el('input', 'pd-input');
		control.readOnly = true;
		control.value = field.type === 'readonly' ? String(field.value ?? '') : (field.placeholder || '');
	} else if (field.type === 'toggle') {
		control = el('input', 'pd-toggle');
		control.type = 'checkbox';
		control.checked = field.value !== false;
		control.addEventListener('change', () => handlers.onFieldChange(field, control.checked ? 'true' : 'false', scope));
	} else if (field.type === 'select') {
		control = el('select', 'pd-input');
		for (const option of field.options || []) {
			const o = el('option');
			o.value = option;
			o.textContent = option;
			control.appendChild(o);
		}
		control.value = field.value ?? '';
		control.addEventListener('change', () => handlers.onFieldChange(field, control.value, scope));
	} else {
		control = el('input', 'pd-input');
		control.type = field.type === 'secret' ? 'password' : field.type === 'number' ? 'number' : 'text';
		if (field.min !== undefined && field.min !== null) control.min = String(field.min);
		if (field.max !== undefined && field.max !== null) control.max = String(field.max);
		const draftKey = `${scope}/${field.id}`;
		control.value = local.drafts[draftKey] !== undefined ? local.drafts[draftKey]
			: field.value !== undefined && field.value !== null ? String(field.value) : '';
		control.placeholder = field.placeholder
			|| (field.default !== undefined && field.default !== null ? `Default: ${field.default}` : '')
			|| (field.is_set ? 'Set; type to replace' : '');
		if (field.pattern) control.title = `Expected format: ${field.pattern}`;
		control.addEventListener('input', () => {
			local.drafts[draftKey] = control.value;
			if (handlers.onDraft) handlers.onDraft();
		});
		// Side fields save on change; account fields wait for the card's Save button.
		if (scope.startsWith('side:')) {
			control.addEventListener('change', () => handlers.onFieldChange(field, control.value, scope));
		}
	}
	control.id = id;
	row.append(label, control);
	if (field.is_set !== undefined && field.is_set !== null && field.storage !== 'session') {
		const badge = el('span', `pd-badge ${field.is_set ? 'pd-badge-ok' : 'pd-badge-missing'}`);
		badge.textContent = field.is_set ? 'set' : field.optional ? 'optional' : 'missing';
		row.appendChild(badge);
	}
	if (field.help || field.docs_url) {
		const help = el('div', 'pd-help');
		help.textContent = field.help || '';
		if (field.docs_url) {
			const link = el('a', 'pd-link');
			link.href = field.docs_url;
			link.textContent = field.help ? ' Get a key' : 'Get a key';
			help.appendChild(link);
		}
		row.appendChild(help);
	}
	return row;
};

/**
 * The preset and each side's state as one comparable string.
 * @param {PresetView} view
 * @returns {string}
 */
ZoteroRAGPresetDialog.sideStates = function (view) {
	return [view.preset.name, ...view.sides.map((s) => `${s.side}:${s.state}`)].join('|');
};

/**
 * True when a value was typed into one of the card's savable fields (blanks do not count).
 * @param {AccountCard} card
 * @param {Record<string, string>} drafts
 * @returns {boolean}
 */
ZoteroRAGPresetDialog.hasAccountInput = function (card, drafts) {
	return card.fields.some((f) => {
		if (f.editable === false || f.type === 'readonly' || f.storage === 'login') return false;
		const draft = drafts[`account:${card.id}/${f.id}`];
		return typeof draft === 'string' && draft.trim() !== '' && draft.trim() !== String(f.value ?? '');
	});
};

/**
 * One account card.
 * @param {Document} doc
 * @param {AccountCard} card
 * @param {PresetView} view
 * @param {any} local
 * @param {any} handlers
 * @returns {any}
 */
ZoteroRAGPresetDialog._card = function (doc, card, view, local, handlers) {
	const el = this._el(doc);
	const node = el('div', `pd-card${card.state === 'needs_input' ? ' pd-card-needs' : ''}`);
	const head = el('div', 'pd-card-head');
	const title = el('h3');
	title.textContent = card.label;
	const serves = el('span', 'pd-muted');
	const titles = Object.fromEntries(view.sides.map((s) => [s.side, s.title]));
	serves.textContent = card.serves.map((s) => titles[s] || s).join(' · ');
	head.append(title, serves);
	node.appendChild(head);
	const actions = card.actions || [];
	const hasLogin = actions.some((a) => a.sends_login || a.id === 'logout');
	const state = el('div', 'pd-card-state');
	state.append(this._dot(el, card.state === 'ready' ? 'ready' : 'down'), el('span'));
	const stateText = hasLogin
		? (card.state === 'ready' ? 'Logged in' : 'Not logged in')
		: (card.state === 'ready' ? 'Credentials complete' : 'Credentials missing');
	state.children[1].textContent = ` ${stateText}${card.scope_note ? ` (${card.scope_note})` : ''}`;
	node.appendChild(state);
	if (card.quota) {
		const quota = el('div', `pd-card-quota pd-msg-${card.quota.level}`);
		quota.append(this._dot(el, card.quota.level === 'ok' ? 'ready' : card.quota.level === 'warn' ? 'starting' : 'down'), el('span'));
		quota.children[1].textContent = ` Quota: ${card.quota.text}`;
		node.appendChild(quota);
	}
	const scope = `account:${card.id}`;
	/** @type {HTMLButtonElement|null} Enabled once a value is typed (see below). */
	let save = null;
	const updateSave = () => { if (save) save.disabled = !this.hasAccountInput(card, local.drafts); };
	const fieldHandlers = { ...handlers, onDraft: updateSave };
	for (const field of card.fields) node.appendChild(this._field(doc, field, scope, local, fieldHandlers));
	const note = local.notes[scope];
	for (const message of note ? [note, ...card.messages] : card.messages) node.appendChild(this._message(el, message));
	const row = el('div', 'pd-actions');
	if (card.fields.some((f) => f.editable !== false && f.type !== 'readonly' && f.storage !== 'login')) {
		save = el('button', `pd-button${hasLogin ? '' : ' pd-primary'}`);
		save.textContent = card.auto_start ? 'Save and start' : 'Save';
		save.title = card.auto_start ? 'Save the entered values, then start the endpoints that use them' : '';
		save.addEventListener('click', () => handlers.onSaveAccount(card));
		// Nothing typed, nothing to save: an emptied key field must not clear a stored key.
		updateSave();
		row.appendChild(save);
	}
	for (const action of actions) {
		const button = el('button', `pd-button${action.kind === 'primary' ? ' pd-primary' : action.kind === 'danger' ? ' pd-danger' : ''}`);
		button.textContent = action.label;
		button.disabled = action.enabled === false;
		button.title = action.reason || '';
		button.addEventListener('click', () => handlers.onCardAction && handlers.onCardAction(card, action));
		row.appendChild(button);
	}
	if (row.children.length) node.appendChild(row);
	return node;
};

// -- controller --------------------------------------------------------------------

/**
 * Drives the dialog against the backend. Every dependency is injected.
 * @param {{
 *   get: (path: string) => Promise<any>,
 *   lastError?: () => string,
 *   post: (path: string, body: any) => Promise<{ok: boolean, status: number, data: any}>,
 *   prefs: {get: (name: string) => any, set: (name: string, value: any) => void, clear: (name: string) => void},
 *   saveShared: (keyName: string, value: string) => Promise<{ok: boolean, error?: string}>,
 *   sessionKeys: SessionKeys,
 *   onKeysChanged?: (fields: UiField[]) => Promise<void>,
 *   onPresetChanged?: () => void,
 *   fetchStandinOffer?: () => Promise<StandinOffer|null>,
 *   useStandin?: (preset: string, provider: string, startEndpoints: boolean) => Promise<void>,
 *   paint: (view: PresetView, local: any) => void,
 *   sleep?: (ms: number) => Promise<void>,
 *   pollMs?: number,
 * }} deps
 */
ZoteroRAGPresetDialog.createController = function (deps) {
	const D = ZoteroRAGPresetDialog;
	const sleep = deps.sleep || ((/** @type {number} */ ms) => new Promise((resolve) => setTimeout(resolve, ms)));
	const pollMs = deps.pollMs === undefined ? 5000 : deps.pollMs;
	/** Refreshes while something is starting, after which polling stops (about 10 minutes). */
	const MAX_POLLS = 120;
	/** @type {{view: PresetView|null, local: LocalState}} */
	const state = { view: null, local: { tab: '', logOpen: {}, drafts: {}, notes: {}, standins: null } };
	let polling = false;
	// The stand-ins are offered by themselves once per dialog, the first time a side shows
	// a used-up quota; afterwards only the side's button asks again.
	let standinsOffered = false;

	/** @type {PresetView} Shown until the backend has answered once. */
	const EMPTY = {
		preset: { name: 'Not connected', description: '', is_admin: false, default_options: [], default_preset: '' },
		sides: [], accounts: [], busy: false,
	};
	const repaint = () => deps.paint(state.view || EMPTY, state.local);

	/**
	 * @param {string} scope
	 * @param {'ok'|'info'|'warn'|'error'} level
	 * @param {string} text
	 */
	const note = (scope, level, text) => {
		if (text) state.local.notes[scope] = { level, text };
		else delete state.local.notes[scope];
		repaint();
	};

	/** Fetch the view and repaint; false when the backend did not answer. */
	const refresh = async () => {
		const view = await deps.get('/api/config/preset-view');
		if (!view) {
			const why = deps.lastError ? deps.lastError() : '';
			note('header', 'error', why || 'The server did not answer. Check the server URL in Settings.');
			return false;
		}
		// Client-side settings live in this Zotero's preferences, not on the server.
		const before = state.view ? D.sideStates(state.view) : null;
		state.view = D.applyLocalState(view, deps.prefs.get);
		// A side came up, went down or started: the query dialog follows at once.
		if (before !== null && before !== D.sideStates(state.view) && deps.onPresetChanged) deps.onPresetChanged();
		if (state.local.notes.header && state.local.notes.header.level === 'error') delete state.local.notes.header;
		repaint();
		const quotaSide = standinsOffered ? null : state.view.sides.find((s) => s.actions.some((a) => a.id === 'standin'));
		if (quotaSide) {
			standinsOffered = true;
			state.local.tab = quotaSide.side;
			const offer = await showStandins(quotaSide.side);
			if (offer) note('header', 'info', `${offer.message} Choose a stand-in on the ${quotaSide.title} tab to use until then.`);
		}
		return true;
	};

	/** Refresh now and keep refreshing while a job runs or an endpoint is starting (bounded). */
	const poll = async () => {
		if (polling) return;
		polling = true;
		try {
			for (let round = 0; ; round++) {
				const ok = await refresh();
				const view = state.view;
				const active = !!view && (view.busy || view.sides.some((s) => s.state === 'starting'));
				if (!ok || !active || round >= MAX_POLLS) break;
				await sleep(pollMs);
			}
		} finally {
			polling = false;
		}
	};

	/**
	 * POST one request with the session credentials of its accounts.
	 * @param {{path: string, body: Record<string, any>, accounts: string[]}} request
	 * @returns {Promise<string>} '' on success, else the error text
	 */
	const send = async (request) => {
		if (!state.view) return 'Not loaded yet.';
		const keys = D.sessionKeysFor(state.view, request.accounts, deps.sessionKeys);
		const body = Object.keys(keys).length ? { ...request.body, keys } : request.body;
		try {
			const result = await deps.post(request.path, body);
			if (result.ok) return '';
			// Starting what is already starting is what was asked for: no failure to report.
			if (result.status === 409 && request.path === '/api/config/provision') return '';
			// A one-time credential the provider refused is not worth keeping.
			if (result.status === 401 || result.status === 403) for (const env of Object.keys(keys)) deps.sessionKeys.forget(env);
			return result.data && result.data.detail ? String(result.data.detail) : `HTTP ${result.status}`;
		} catch (e) {
			return String(e);
		}
	};

	/**
	 * Run one side's action.
	 * @param {string} side
	 * @param {UiAction} action
	 */
	const runAction = async (side, action) => {
		if (action.id === 'standin') return showStandins(side);
		if (!action.request) return;
		note(`side:${side}`, 'info', `${action.label}…`);
		const error = await send({ path: action.request.path, body: action.request.body, accounts: action.account ? [action.account] : [] });
		note(`side:${side}`, error ? 'error' : 'info', error ? `${action.label} failed: ${error}` : '');
		await poll();
	};

	/**
	 * The "standin" action of a side down for a used-up quota: fetch the offer and list
	 * its stand-ins on that side. The offer is gone when the quota resets soon.
	 * @param {string} side
	 * @returns {Promise<StandinOffer|null>} the offer listed, null when none is on offer
	 */
	const showStandins = async (side) => {
		const scope = `side:${side}`;
		note(scope, 'info', 'Looking for stand-ins…');
		const offer = deps.fetchStandinOffer ? await deps.fetchStandinOffer().catch(() => null) : null;
		if (!offer || !offer.standins || !offer.standins.length) {
			state.local.standins = null;
			note(scope, 'info', 'No stand-in is on offer right now.');
			return null;
		}
		const hasKey = (/** @type {string} */ name) => !!deps.prefs.get(D.SECRET_PREFIX + name);
		state.local.standins = {
			side, provider: offer.provider,
			choices: offer.standins.map((c) => ({
				preset: c.preset, text: D.describeStandin(c, hasKey),
				// Nothing to do first (keys it needs are stored): its endpoints can start right away.
				ready: c.needs.every((n) => n.startsWith('key:') && hasKey(n.slice(4))),
			})),
		};
		note(scope, 'info', 'Choose a stand-in to use until the quota resets.');
		return offer;
	};

	/**
	 * Use one of the listed stand-ins, then show it (the view follows the stand-in). A
	 * refusal (e.g. the quota is no longer used up) is shown and nothing is switched.
	 * @param {string} preset
	 */
	const useStandin = async (preset) => {
		const offer = state.local.standins;
		if (!offer || !deps.useStandin) return;
		const scope = `side:${offer.side}`;
		note(scope, 'info', `Switching to ${preset}…`);
		try {
			const choice = offer.choices.find((c) => c.preset === preset);
			await deps.useStandin(preset, offer.provider, !!choice && !!choice.ready);
		} catch (e) {
			note(scope, 'error', e && typeof (/** @type {any} */ (e)).message === 'string' ? /** @type {any} */ (e).message : String(e));
			return;
		}
		state.local.standins = null;
		note(scope, 'info', '');
		note('header', 'info', ''); // the used-up quota is no longer the caller's concern
		await poll();
	};

	/**
	 * Start all / Pause all (optionally only for some sides).
	 * @param {'start'|'pause'} kind
	 * @param {string[]} [onlySides]
	 * @returns {Promise<number>} how many requests were sent
	 */
	const runWorkflow = async (kind, onlySides) => {
		if (!state.view) return 0;
		const requests = D.workflowRequests(state.view, kind, onlySides);
		if (!requests.length) {
			if (!onlySides) note('header', 'info', kind === 'start' ? 'Nothing to start.' : 'Nothing to pause.');
			return 0;
		}
		note('header', 'info', kind === 'start' ? 'Starting…' : 'Pausing…');
		const errors = [];
		for (const request of requests) {
			const error = await send(request);
			if (error) errors.push(error);
		}
		note('header', errors.length ? 'error' : 'info', errors.length ? errors.join(' ') : '');
		await poll();
		// The job runs in the background: a side it failed on says so only in its own tab,
		// which may not be the one shown. Name it here and show it.
		const asked = new Set(requests.flatMap((r) => r.body.sides || []));
		const failed = !errors.length && state.view
			? state.view.sides.filter((s) => asked.has(s.side) && s.messages.some((m) => m.level === 'error' && m.text.startsWith('Last job failed')))
			: [];
		if (failed.length) {
			const reason = /** @type {UiMessage} */ (failed[0].messages.find((m) => m.level === 'error' && m.text.startsWith('Last job failed')));
			state.local.tab = failed[0].side;
			note('header', 'error', `${kind === 'start' ? 'Starting' : 'Pausing'} ${failed.map((s) => s.title).join(', ')} failed. ${reason.text}`);
		}
		return requests.length;
	};

	/**
	 * Save a value typed into a side's configuration field (client preferences only).
	 * @param {UiField} field
	 * @param {string} raw
	 * @param {string} scope
	 */
	const setSideField = async (field, raw, scope) => {
		if (field.storage !== 'client_pref' || !field.key) return;
		const error = D.validate(field, raw);
		if (error) {
			note(scope, 'error', error);
			return;
		}
		const name = D.PREF_PREFIX + field.key;
		const value = raw.trim();
		if (field.type === 'toggle') {
			deps.prefs.set(name, value === 'true');
			note(scope, 'ok', `${field.label}: ${value === 'true' ? 'on' : 'off'}.`);
			await refresh();
			return;
		}
		if (!value) deps.prefs.clear(name);
		else deps.prefs.set(name, field.type === 'number' ? Number(value) : value);
		delete state.local.drafts[`${scope}/${field.id}`];
		note(scope, 'ok', `${field.label} saved.`);
	};

	/**
	 * Save an account card's typed values, then start the sides it serves.
	 * @param {AccountCard} card
	 */
	const saveAccount = async (card) => {
		const scope = `account:${card.id}`;
		/** @type {Array<[UiField, string]>} */
		const entered = [];
		for (const field of card.fields) {
			const draft = state.local.drafts[`${scope}/${field.id}`];
			if (draft === undefined || field.editable === false || field.storage === 'login') continue;
			const error = D.validate(field, draft);
			if (error) {
				note(scope, 'error', error);
				return;
			}
			entered.push([field, draft.trim()]);
		}
		if (!entered.length) {
			note(scope, 'info', 'Nothing new to save.');
		}
		const changed = [];
		for (const [field, value] of entered) {
			if (field.storage === 'client_secret' && field.key) {
				if (value) deps.prefs.set(D.SECRET_PREFIX + field.key, value);
				else deps.prefs.clear(D.SECRET_PREFIX + field.key);
				changed.push(field);
			} else if (field.storage === 'session' && field.key) {
				if (value) deps.sessionKeys.set(field.key, value);
				else deps.sessionKeys.forget(field.key);
			} else if (field.storage === 'shared' && field.key && value) {
				const result = await deps.saveShared(field.key, value);
				if (!result.ok) {
					note(scope, 'error', `${field.label}: ${result.error || 'not saved'}`);
					return;
				}
				changed.push(field);
			}
			delete state.local.drafts[`${scope}/${field.id}`];
		}
		if (changed.length && deps.onKeysChanged) await deps.onKeysChanged(changed);
		if (entered.length) note(scope, 'ok', 'Saved.');
		await refresh();
		if (card.auto_start) {
			const started = await runWorkflow('start', card.serves);
			if (started) note(scope, 'ok', 'Saved. Starting the endpoints that use it.');
		}
	};

	/**
	 * Run an account card's action: log in (sends the login form once, then clears it) or log out.
	 * @param {AccountCard} card
	 * @param {UiAction} action
	 */
	const runCardAction = async (card, action) => {
		if (!action.request) return;
		const scope = `account:${card.id}`;
		/** @type {Record<string, any>} */
		let body = action.request.body || {};
		if (action.sends_login) {
			const values = D.loginValues(card, state.local.drafts);
			const missing = card.fields.filter((f) => f.storage === 'login' && !f.optional && f.key && !values[f.key]);
			if (missing.length) {
				note(scope, 'error', `Enter ${missing.map((f) => f.label).join(', ')}.`);
				return;
			}
			// The password and one-time code are sent once and never kept.
			for (const field of card.fields) if (field.storage === 'login') delete state.local.drafts[`${scope}/${field.id}`];
			// The login also starts the account's re-ranking endpoint unless the "Use re-ranking"
			// switch (a client preference the server cannot see) is off.
			const startRerank = deps.prefs.get(D.PREF_PREFIX + 'enableReranking') !== false;
			body = { ...body, values, start_rerank: startRerank };
		}
		note(scope, 'info', `${action.label}…`);
		let result;
		try {
			result = await deps.post(action.request.path, body);
		} catch (e) {
			note(scope, 'error', `${action.label} failed: ${String(e)}`);
			return;
		}
		if (!result.ok) {
			note(scope, 'error', `${action.label} failed: ${result.data && result.data.detail ? result.data.detail : `HTTP ${result.status}`}`);
			await refresh();
			return;
		}
		const data = result.data || {};
		if (data.provisioning_error) note(scope, 'warn', `Logged in, but the endpoints did not start: ${data.provisioning_error}`);
		else if (action.sends_login) note(scope, 'ok', data.provisioning ? 'Logged in. Starting the endpoints…' : 'Logged in.');
		else note(scope, 'ok', 'Done.');
		await poll();
	};

	/**
	 * Change the server default preset (admins).
	 * @param {string} name
	 */
	const setServerDefault = async (name) => {
		note('header', 'info', 'Switching the server default…');
		const result = await deps.post('/api/config', { preset_name: name });
		if (!result.ok) {
			note('header', 'error', `Could not switch: ${result.data && result.data.detail ? result.data.detail : `HTTP ${result.status}`}`);
		} else {
			note('header', 'ok', `Server default is now ${name}.`);
			if (deps.onPresetChanged) deps.onPresetChanged();
		}
		await refresh();
	};

	/** @param {string} side */
	const selectTab = (side) => { state.local.tab = side; repaint(); };
	/** @param {string} side */
	const toggleLog = (side) => { state.local.logOpen[side] = !state.local.logOpen[side]; repaint(); };

	return { state, refresh, poll, runAction, useStandin, runWorkflow, setSideField, saveAccount, runCardAction, setServerDefault, selectTab, toggleLog };
};

if (typeof module !== 'undefined') module.exports = { ZoteroRAGPresetDialog };
