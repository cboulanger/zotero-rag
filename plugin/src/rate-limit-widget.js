// Shared usage display used by the Ask dialog and the auto-indexing status dialog:
// quota bars for rate-limited services, or the estimated costs of paid ones.

// @ts-check

/// <reference path='./zotero-rag.js' />

/**
 * One quota as reported by the backend (`meters` in `GET /api/rate-limits`),
 * already parsed by the side's provider. Quota only: no billing figures.
 * @typedef {Object} UsageMeter
 * @property {string} id - Stable id such as "requests/hour"
 * @property {'embedding'|'llm'} side
 * @property {string} unit - "requests", "tokens", ...
 * @property {string|null} [period] - "minute", "hour", "day", ... or null
 * @property {number} limit - Total quota for the period
 * @property {number} remaining - Left in the period
 * @property {string|null} [resets_at] - ISO timestamp the quota refills, when known
 * @property {string|null} [as_of] - ISO timestamp the numbers were captured
 */

/**
 * What one side of one paid provider consumed (`costs` in query and upload responses, and
 * per library in the auto-indexing status). The plugin prices it with the user's Billing
 * settings (`priceCosts`); `amount` and `pricing` are the provider's list prices.
 * @typedef {Object} UsageCost
 * @property {'embedding'|'llm'|'rerank'} side
 * @property {string} [provider] - provider id
 * @property {string} [provider_label]
 * @property {string} [model]
 * @property {number|null} [amount] - at list prices from the server; at the user's prices after `priceCosts`
 * @property {string} currency - ISO 4217 code, e.g. "USD"
 * @property {number} [requests]
 * @property {number} [input_tokens]
 * @property {number} [output_tokens]
 * @property {number} [seconds]
 * @property {{input_per_million_tokens?: number, output_per_million_tokens?: number, per_second?: number, per_request?: number}|null} [pricing]
 * @property {boolean} [estimated]
 * @property {string} [note] - What the estimate leaves out
 */

/**
 * A paid provider of the active preset (`paid_providers` of GET /api/config).
 * @typedef {{id: string, label: string, sides: string[]}} PaidProvider
 */

/**
 * @typedef {Object} MeterDescription
 * @property {number} usedPct - Percentage used, rounded (0-100)
 * @property {string} color - CSS colour for the bar
 * @property {string} text - Label such as "123 requests left/hour"
 */

var ZoteroRAGRateLimitWidget = {
	/** @type {Record<string, string>} */
	SIDE_LABELS: { embedding: 'Embedding', llm: 'Answering', rerank: 'Re-ranking' },

	/** Rows the fixed-size widget has room for; the rest are in its tooltip. */
	MAX_ROWS: 2,

	/**
	 * Fetch `GET /api/rate-limits`.
	 * @param {{backendURL: string, getAuthHeaders: () => Record<string, string>}|null} plugin
	 * @returns {Promise<UsageMeter[]|null>} meters, or null if unavailable/failed
	 */
	async fetch(plugin) {
		if (!plugin) return null;
		try {
			const response = await fetch(`${plugin.backendURL}/api/rate-limits`, {
				headers: plugin.getAuthHeaders(),
			});
			if (!response.ok) return null;
			const data = /** @type {{available?: boolean, meters?: UsageMeter[]}} */ (await response.json());
			return data.available && data.meters && data.meters.length ? data.meters : null;
		} catch (_) {
			// non-fatal — display stays empty
			return null;
		}
	},

	/**
	 * Compute the bar width/colour/label for one meter (pure).
	 * Thresholds: amber at >= 75 % used, red at >= 95 % used.
	 * @param {UsageMeter|null|undefined} meter
	 * @returns {MeterDescription|null} null when the limit is missing or zero
	 */
	describe(meter) {
		if (!meter || !meter.limit) return null;
		const usedPct = Math.round((meter.limit - meter.remaining) / meter.limit * 100);
		const color = usedPct >= 95 ? '#cc3300' : usedPct >= 75 ? '#e6a817' : '#2e9e4f';
		const per = meter.period ? `/${meter.period}` : '';
		return { usedPct, color, text: `${meter.remaining} ${meter.unit} left${per}` };
	},

	/**
	 * Paint one bar per meter (at most `maxRows`) into `<prefix>rate-limit-bars`, replacing earlier rows.
	 * A side label is added only when meters of both sides are shown.
	 * @param {Document} doc
	 * @param {UsageMeter[]|null|undefined} meters
	 * @param {{visible: boolean, prefix?: string, maxRows?: number}} options - `maxRows` defaults to
	 *   the fixed-size widget's `MAX_ROWS`; the meters left out are listed in the tooltip
	 * @returns {void}
	 */
	render(doc, meters, { visible, prefix = '', maxRows = this.MAX_ROWS }) {
		const section = doc.getElementById(`${prefix}rate-limit-section`);
		if (!section) return;
		section.style.display = visible ? '' : 'none';
		const container = doc.getElementById(`${prefix}rate-limit-bars`);
		if (!container) return;
		container.replaceChildren();
		if (!visible || !meters) return;
		const bothSides = new Set(meters.map((m) => m.side)).size > 1;
		const label = (/** @type {UsageMeter} */ meter, /** @type {MeterDescription} */ info) =>
			bothSides ? `${this.SIDE_LABELS[meter.side] || meter.side}: ${info.text}` : info.text;
		const described = meters
			.map((meter) => ({ meter, info: this.describe(meter) }))
			.filter((/** @type {{meter: UsageMeter, info: MeterDescription|null}} */ d) => d.info !== null);
		// The widget has a fixed size: show the meters closest to running out, in their own order.
		const shown = new Set([...described]
			.sort((a, b) => /** @type {MeterDescription} */ (b.info).usedPct - /** @type {MeterDescription} */ (a.info).usedPct)
			.slice(0, maxRows));
		section.title = described.map((d) => label(d.meter, /** @type {MeterDescription} */ (d.info))).join('\n');
		for (const d of described) {
			if (!shown.has(d)) continue;
			const meter = d.meter;
			const info = /** @type {MeterDescription} */ (d.info);
			const row = doc.createElementNS('http://www.w3.org/1999/xhtml', 'div');
			row.className = 'rate-limit-row';
			const track = doc.createElementNS('http://www.w3.org/1999/xhtml', 'div');
			track.className = 'rate-limit-bar-track';
			const bar = doc.createElementNS('http://www.w3.org/1999/xhtml', 'div');
			bar.className = 'rate-limit-bar';
			bar.style.width = `${info.usedPct}%`;
			bar.style.backgroundColor = info.color;
			track.appendChild(bar);
			const text = doc.createElementNS('http://www.w3.org/1999/xhtml', 'span');
			text.className = 'rate-limit-text';
			text.textContent = label(meter, info);
			row.append(track, text);
			container.appendChild(row);
		}
	},

	// -- costs of paid providers ------------------------------------------------

	/** Billing types of an account card's Billing select (backend/services/preset_view.py). */
	BILLING: { NOT_SET: 'Not set', SUBSCRIPTION: 'Subscription', TOKENS: 'Token-based', TIME: 'Time-based' },

	/**
	 * Read `extensions.zotero-rag.<name>` from Zotero's preferences (undefined outside Zotero).
	 * @param {string} name
	 * @returns {any}
	 */
	zoteroPref(name) {
		try {
			// @ts-ignore - Zotero is a global in the plugin's windows
			return typeof Zotero !== 'undefined' ? Zotero.Prefs.get(`extensions.zotero-rag.${name}`, true) : undefined;
		} catch (_) {
			return undefined;
		}
	},

	/**
	 * How the user is billed for a provider: the Billing select of its account card.
	 * @param {string|undefined} provider
	 * @param {(name: string) => any} getPref - reads `extensions.zotero-rag.<name>`
	 * @returns {string}
	 */
	billingMode(provider, getPref) {
		if (!provider) return this.BILLING.NOT_SET;
		try {
			const mode = getPref(`billing.${provider}.mode`);
			return typeof mode === 'string' && mode ? mode : this.BILLING.NOT_SET;
		} catch (_) {
			return this.BILLING.NOT_SET;
		}
	},

	/**
	 * Whether any of these providers is billed per token or per second, so costs are shown instead of quota.
	 * @param {PaidProvider[]|null|undefined} providers
	 * @param {(name: string) => any} getPref
	 * @returns {boolean}
	 */
	showsCosts(providers, getPref) {
		return (providers || []).some((p) => {
			const mode = this.billingMode(p.id, getPref);
			return mode === this.BILLING.TOKENS || mode === this.BILLING.TIME;
		});
	},

	/**
	 * The user's price for one rate, else the list price, else 0.
	 * @param {any} stored - the preference value (a decimal string), or undefined
	 * @param {number|undefined} listPrice
	 * @returns {number}
	 */
	_rate(stored, listPrice) {
		const n = typeof stored === 'string' && stored.trim() ? Number(stored) : typeof stored === 'number' ? stored : NaN;
		if (Number.isFinite(n) && n >= 0) return n;
		return typeof listPrice === 'number' && Number.isFinite(listPrice) ? listPrice : 0;
	},

	/**
	 * Price usage records with the user's Billing settings (pure apart from `getPref`).
	 * Token-based: input and output tokens at the side's prices; time-based: seconds at the
	 * side's price per second. Records of providers billed by subscription or not set are left out.
	 * @param {UsageCost[]|null|undefined} usage
	 * @param {(name: string) => any} getPref
	 * @returns {UsageCost[]} new records with `amount` at the user's prices
	 */
	priceCosts(usage, getPref) {
		/** @type {UsageCost[]} */
		const priced = [];
		for (const u of usage || []) {
			if (!u) continue;
			const mode = this.billingMode(u.provider, getPref);
			const base = `billing.${u.provider}.${u.side}`;
			const list = u.pricing || {};
			/** @param {string} name */
			const pref = (name) => { try { return getPref(`${base}.${name}`); } catch (_) { return undefined; } };
			let amount;
			if (mode === this.BILLING.TOKENS) {
				amount = (u.input_tokens || 0) * this._rate(pref('input'), list.input_per_million_tokens) / 1e6
					+ (u.output_tokens || 0) * this._rate(pref('output'), list.output_per_million_tokens) / 1e6;
			} else if (mode === this.BILLING.TIME) {
				amount = (u.seconds || 0) * this._rate(pref('second'), list.per_second);
			} else {
				continue;
			}
			priced.push({ ...u, amount, currency: u.currency || 'USD' });
		}
		return priced;
	},

	/**
	 * Add usage records to running totals, merging records of the same side, provider, model
	 * and currency (pure). `amount`s add up where present.
	 * @param {UsageCost[]} totals
	 * @param {UsageCost[]|null|undefined} more
	 * @returns {UsageCost[]} new totals; the inputs are not changed
	 */
	addCosts(totals, more) {
		/** @type {UsageCost[]} */
		const result = totals.map((c) => ({ ...c }));
		for (const cost of more || []) {
			if (!cost || !cost.side) continue;
			const same = result.find((c) => c.side === cost.side && (c.provider || '') === (cost.provider || '')
				&& (c.model || '') === (cost.model || '') && c.currency === cost.currency);
			if (!same) {
				result.push({ ...cost });
				continue;
			}
			if (typeof cost.amount === 'number') same.amount = (same.amount || 0) + cost.amount;
			for (const key of /** @type {const} */ (['requests', 'input_tokens', 'output_tokens', 'seconds'])) {
				same[key] = (same[key] || 0) + (cost[key] || 0);
			}
			if (cost.note && !(same.note || '').includes(cost.note)) same.note = `${same.note || ''} ${cost.note}`.trim();
		}
		return result;
	},

	/**
	 * Format an amount of money; amounts below one cent keep four decimals so they don't read as zero.
	 * @param {number} amount
	 * @param {string} currency
	 * @returns {string}
	 */
	formatAmount(amount, currency) {
		const digits = amount > 0 && amount < 0.01 ? 4 : 2;
		try {
			return new Intl.NumberFormat('en-US', {
				style: 'currency', currency, minimumFractionDigits: digits, maximumFractionDigits: digits,
			}).format(amount);
		} catch (_) {
			return `${amount.toFixed(digits)} ${currency}`;
		}
	},

	/**
	 * The widget's text for priced totals (pure).
	 * @param {UsageCost[]} costs - priced (see `priceCosts`)
	 * @param {string} [scope] - What the total covers, e.g. "this session"
	 * @returns {{total: string, detail: string, title: string}}
	 */
	describeCosts(costs, scope = 'this session') {
		/** @type {Record<string, number>} */
		const byCurrency = {};
		/** @type {Map<string, {currency: string, amount: number}>} */
		const bySide = new Map();
		for (const c of costs) {
			const amount = c.amount || 0;
			byCurrency[c.currency] = (byCurrency[c.currency] || 0) + amount;
			const key = `${c.side}|${c.currency}`;
			const side = bySide.get(key) || { currency: c.currency, amount: 0 };
			side.amount += amount;
			bySide.set(key, side);
		}
		const currencies = Object.keys(byCurrency);
		const sum = currencies.length
			? currencies.map((cur) => this.formatAmount(byCurrency[cur], cur)).join(' + ')
			: this.formatAmount(0, 'USD');
		const total = `Estimated cost ${scope}: ${sum}`;
		const detail = [...bySide.entries()]
			.map(([key, v]) => `${this.SIDE_LABELS[key.split('|')[0]] || key.split('|')[0]} ${this.formatAmount(v.amount, v.currency)}`)
			.join(' \u00b7 ');
		const lines = costs.map((c) => {
			const parts = [];
			if (c.input_tokens || c.output_tokens) {
				parts.push(`${(c.input_tokens || 0).toLocaleString('en-US')} input / ${(c.output_tokens || 0).toLocaleString('en-US')} output tokens`);
			}
			if (c.seconds) parts.push(`${c.seconds.toFixed(1)} s`);
			if (c.requests) parts.push(`${c.requests} calls`);
			const who = c.provider_label || c.provider;
			return `${this.SIDE_LABELS[c.side] || c.side}${who ? ` (${who})` : ''}: ${this.formatAmount(c.amount || 0, c.currency)}${parts.length ? ` (${parts.join(', ')})` : ''}`;
		});
		const notes = [...new Set(costs.map((c) => c.note).filter(Boolean))];
		const title = [total, ...lines, 'This is an estimate from the prices in Configure preset, not your bill.', ...notes].join('\n');
		return { total, detail, title };
	},

	/**
	 * Paint priced totals into `<prefix>rate-limit-bars` (the same fixed-size widget as the quota bars).
	 * @param {Document} doc
	 * @param {UsageCost[]} costs - priced (see `priceCosts`)
	 * @param {{visible: boolean, prefix?: string, scope?: string}} options
	 * @returns {void}
	 */
	renderCosts(doc, costs, { visible, prefix = '', scope }) {
		const section = doc.getElementById(`${prefix}rate-limit-section`);
		if (!section) return;
		section.style.display = visible ? '' : 'none';
		const container = doc.getElementById(`${prefix}rate-limit-bars`);
		if (!container) return;
		container.replaceChildren();
		if (!visible) return;
		const info = this.describeCosts(costs || [], scope);
		section.title = info.title;
		const total = doc.createElementNS('http://www.w3.org/1999/xhtml', 'div');
		total.className = 'usage-cost-total';
		total.textContent = info.total;
		container.appendChild(total);
		if (info.detail) {
			const detail = doc.createElementNS('http://www.w3.org/1999/xhtml', 'div');
			detail.className = 'usage-cost-detail';
			detail.textContent = info.detail;
			container.appendChild(detail);
		}
	},
};
