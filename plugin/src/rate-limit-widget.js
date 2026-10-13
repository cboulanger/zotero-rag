// Shared usage meters (quota bars) used by the Ask dialog and the auto-indexing status dialog.

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
 * @typedef {Object} MeterDescription
 * @property {number} usedPct - Percentage used, rounded (0-100)
 * @property {string} color - CSS colour for the bar
 * @property {string} text - Label such as "123 requests left/hour"
 */

var ZoteroRAGRateLimitWidget = {
	/** @type {Record<string, string>} */
	SIDE_LABELS: { embedding: 'Embedding', llm: 'Answering' },

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
	 * Paint one bar per meter into `<prefix>rate-limit-bars`, replacing earlier rows.
	 * A side label is added only when meters of both sides are shown.
	 * @param {Document} doc
	 * @param {UsageMeter[]|null|undefined} meters
	 * @param {{visible: boolean, prefix?: string}} options
	 * @returns {void}
	 */
	render(doc, meters, { visible, prefix = '' }) {
		const section = doc.getElementById(`${prefix}rate-limit-section`);
		if (!section) return;
		section.style.display = visible ? '' : 'none';
		const container = doc.getElementById(`${prefix}rate-limit-bars`);
		if (!container) return;
		container.replaceChildren();
		if (!visible || !meters) return;
		const bothSides = new Set(meters.map((m) => m.side)).size > 1;
		for (const meter of meters) {
			const info = this.describe(meter);
			if (!info) continue;
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
			text.textContent = bothSides ? `${this.SIDE_LABELS[meter.side] || meter.side}: ${info.text}` : info.text;
			row.append(track, text);
			container.appendChild(row);
		}
	},
};
