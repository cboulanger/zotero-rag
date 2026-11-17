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
 * The estimated cost of what one side consumed (`costs` in query and upload responses,
 * and per library in the auto-indexing status). Computed from list prices.
 * @typedef {Object} UsageCost
 * @property {'embedding'|'llm'|'rerank'} side
 * @property {number} amount
 * @property {string} currency - ISO 4217 code, e.g. "USD"
 * @property {number} [requests]
 * @property {number} [input_tokens]
 * @property {number} [output_tokens]
 * @property {number} [seconds]
 * @property {boolean} [estimated]
 * @property {string} [note] - What the estimate leaves out
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

	/**
	 * Add `more` to running totals, merging entries of the same side and currency (pure).
	 * @param {UsageCost[]} totals
	 * @param {UsageCost[]|null|undefined} more
	 * @returns {UsageCost[]} new totals; the inputs are not changed
	 */
	addCosts(totals, more) {
		/** @type {UsageCost[]} */
		const result = totals.map((c) => ({ ...c }));
		for (const cost of more || []) {
			if (!cost || typeof cost.amount !== 'number') continue;
			const same = result.find((c) => c.side === cost.side && c.currency === cost.currency);
			if (!same) {
				result.push({ ...cost });
				continue;
			}
			same.amount += cost.amount;
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
	 * The widget's text for running cost totals (pure).
	 * @param {UsageCost[]} costs
	 * @param {string} [scope] - What the total covers, e.g. "this session"
	 * @returns {{total: string, detail: string, title: string}}
	 */
	describeCosts(costs, scope = 'this session') {
		/** @type {Record<string, number>} */
		const byCurrency = {};
		for (const c of costs) byCurrency[c.currency] = (byCurrency[c.currency] || 0) + c.amount;
		const currencies = Object.keys(byCurrency);
		const sum = currencies.length
			? currencies.map((cur) => this.formatAmount(byCurrency[cur], cur)).join(' + ')
			: this.formatAmount(0, 'USD');
		const total = `Est. cost ${scope}: ${sum}`;
		const detail = costs
			.map((c) => `${this.SIDE_LABELS[c.side] || c.side} ${this.formatAmount(c.amount, c.currency)}`)
			.join(' \u00b7 ');
		const lines = costs.map((c) => {
			const parts = [];
			if (c.input_tokens || c.output_tokens) {
				parts.push(`${(c.input_tokens || 0).toLocaleString('en-US')} input / ${(c.output_tokens || 0).toLocaleString('en-US')} output tokens`);
			}
			if (c.seconds) parts.push(`${c.seconds.toFixed(1)} s`);
			if (c.requests) parts.push(`${c.requests} calls`);
			return `${this.SIDE_LABELS[c.side] || c.side}: ${this.formatAmount(c.amount, c.currency)}${parts.length ? ` (${parts.join(', ')})` : ''}`;
		});
		const notes = [...new Set(costs.map((c) => c.note).filter(Boolean))];
		const title = [total, ...lines, 'Estimated from list prices; your bill may differ.', ...notes].join('\n');
		return { total, detail, title };
	},

	/**
	 * Paint running cost totals into `<prefix>rate-limit-bars` (the same fixed-size widget as the quota bars).
	 * @param {Document} doc
	 * @param {UsageCost[]} costs
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
