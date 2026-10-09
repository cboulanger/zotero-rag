// Shared rate-limit bars used by the Ask dialog and the auto-indexing status dialog.

// @ts-check

/// <reference path='./zotero-rag.js' />

/**
 * Raw `x-ratelimit-{limit,remaining}-{hour,day}` header values as returned by
 * the backend's `GET /api/rate-limits`.
 * @typedef {Record<string, string>} RateLimitHeaders
 */

/**
 * @typedef {Object} RateLimitDescription
 * @property {number} limit - Total quota for the period
 * @property {number} remaining - Requests left in the period
 * @property {number} usedPct - Percentage used, rounded (0-100)
 * @property {string} color - CSS colour for the bar
 * @property {string} text - Label such as "123 requests left/hour"
 */

var ZoteroRAGRateLimitWidget = {
	/** Periods rendered, in display order. */
	PERIODS: ['hour', 'day'],

	/**
	 * Fetch `GET /api/rate-limits`.
	 * @param {{backendURL: string, getAuthHeaders: () => Record<string, string>}|null} plugin
	 * @returns {Promise<RateLimitHeaders|null>} headers, or null if unavailable/failed
	 */
	async fetch(plugin) {
		if (!plugin) return null;
		try {
			const response = await fetch(`${plugin.backendURL}/api/rate-limits`, {
				headers: plugin.getAuthHeaders(),
			});
			if (!response.ok) return null;
			const data = /** @type {{available?: boolean, limits?: RateLimitHeaders}} */ (await response.json());
			return data.available && data.limits ? data.limits : null;
		} catch (_) {
			// non-fatal — display stays empty
			return null;
		}
	},

	/**
	 * Compute the bar width/colour/label for one period (pure).
	 * Thresholds: amber at >= 75 % used, red at >= 95 % used.
	 * @param {RateLimitHeaders|null|undefined} headers
	 * @param {string} period - 'hour' or 'day'
	 * @returns {RateLimitDescription|null} null when the limit is missing or zero
	 */
	describe(headers, period) {
		if (!headers) return null;
		const limit = parseInt(headers[`x-ratelimit-limit-${period}`] || '0', 10);
		const remaining = parseInt(headers[`x-ratelimit-remaining-${period}`] || '0', 10);
		if (!limit) return null;
		const usedPct = Math.round((limit - remaining) / limit * 100);
		const color = usedPct >= 95 ? '#cc3300' : usedPct >= 75 ? '#e6a817' : '#2e9e4f';
		return { limit, remaining, usedPct, color, text: `${remaining} requests left/${period}` };
	},

	/**
	 * Paint the bars into the standard rate-limit markup.
	 * Element ids are `<prefix>rate-limit-section`, `<prefix>rate-limit-bar-<period>`
	 * and `<prefix>rate-limit-text-<period>`.
	 * @param {Document} doc
	 * @param {RateLimitHeaders|null|undefined} headers
	 * @param {{visible: boolean, prefix?: string}} options
	 * @returns {void}
	 */
	render(doc, headers, { visible, prefix = '' }) {
		const section = doc.getElementById(`${prefix}rate-limit-section`);
		if (!section) return;
		section.style.display = visible ? '' : 'none';
		if (!visible || !headers) return;
		for (const period of this.PERIODS) {
			const info = this.describe(headers, period);
			const bar = /** @type {HTMLElement|null} */ (doc.getElementById(`${prefix}rate-limit-bar-${period}`));
			const text = doc.getElementById(`${prefix}rate-limit-text-${period}`);
			if (!info || !bar || !text) continue;
			bar.style.width = `${info.usedPct}%`;
			bar.style.backgroundColor = info.color;
			text.textContent = info.text;
		}
	},
};
