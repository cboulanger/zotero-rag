// Client-side citation/mention search over Zotero's local full-text index.
//
// Loaded by dialog.xhtml before dialog.js so ZoteroRAGDialog can call it.
// See docs/query-routing.md for the two-phase "needs_client_evidence" protocol
// this module implements the client half of.

// @ts-check

// Budget for the evidence payload shipped back to the backend: capped so a query
// with many matches still fits comfortably in the synthesis LLM's context window
// (empirically, ~3 snippets/target/doc across ~40 docs stays in the low tens of
// thousands of tokens even for a multi-target query).
const MENTION_SNIPPET_CHARS = 240;
const MENTION_MAX_SNIPPETS_PER_TARGET = 3;
const MENTION_MAX_EVIDENCE_ITEMS = 40;

/**
 * Strip diacritics for variant-tolerant matching (NFD decompose, drop combining marks).
 * @param {string} s
 * @returns {string}
 */
function foldDiacritics(s) {
	return s.normalize('NFD').replace(/[̀-ͯ]/g, '');
}

/**
 * German-transliterated form (ö -> oe, etc.) — a second common OCR/typing variant
 * beyond simple diacritic folding.
 * @param {string} s
 * @returns {string}
 */
function transliterateGerman(s) {
	return s
		.replace(/ö/g, 'oe').replace(/Ö/g, 'Oe')
		.replace(/ä/g, 'ae').replace(/Ä/g, 'Ae')
		.replace(/ü/g, 'ue').replace(/Ü/g, 'Ue')
		.replace(/ß/g, 'ss');
}

/**
 * Distinct lowercase spelling variants of a name/word worth searching for.
 * `fulltextWord` matching in Zotero is a left-bound (prefix) match, so suffix
 * variants (plurals, possessives) don't need to be listed separately.
 * @param {string} word
 * @returns {Array<string>}
 */
function expandVariants(word) {
	const lower = word.toLowerCase();
	return [...new Set([lower, foldDiacritics(lower), transliterateGerman(lower)])]
		.filter(v => v.trim().length > 0);
}

/**
 * All search terms for one citation target: author-name variants, plus any
 * distinctive title keywords (matches short-form citations that name the
 * work without repeating the author nearby).
 * @param {{author: string, year?: number|null, title_keywords?: Array<string>}} target
 * @returns {Array<string>}
 */
function buildSearchTerms(target) {
	const terms = expandVariants(target.author);
	for (const kw of target.title_keywords || []) {
		terms.push(...expandVariants(kw));
	}
	return [...new Set(terms)];
}

/**
 * Count occurrences of any of `terms` in `text` and collect up to `maxSnippets`
 * surrounding-context excerpts (case-insensitive substring match).
 * @param {string} text
 * @param {Array<string>} terms
 * @param {number} [maxSnippets]
 * @param {number} [windowChars]
 * @returns {{count: number, snippets: Array<string>}}
 */
function extractSnippets(text, terms, maxSnippets = MENTION_MAX_SNIPPETS_PER_TARGET, windowChars = MENTION_SNIPPET_CHARS) {
	if (!terms.length) return { count: 0, snippets: [] };
	const pattern = new RegExp(terms.map(t => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|'), 'gi');
	const snippets = [];
	let count = 0;
	let match;
	while ((match = pattern.exec(text)) !== null) {
		count++;
		if (snippets.length < maxSnippets) {
			const start = Math.max(0, match.index - windowChars / 2);
			const end = Math.min(text.length, match.index + match[0].length + windowChars / 2);
			snippets.push(text.slice(start, end).replace(/\s+/g, ' ').trim());
		}
		if (match.index === pattern.lastIndex) pattern.lastIndex++;
	}
	return { count, snippets };
}

/**
 * True when a candidate document's own metadata identifies it as the cited
 * work itself (author + title overlap with the target), rather than a
 * publication citing that work.
 * @param {Array<string>} itemAuthors - "First Last" strings
 * @param {string} itemTitle
 * @param {{author: string, title_keywords?: Array<string>}} target
 * @returns {boolean}
 */
function isSelfCitation(itemAuthors, itemTitle, target) {
	const authorVariants = expandVariants(target.author);
	const authorMatches = itemAuthors.some(a => {
		const folded = foldDiacritics(a.toLowerCase());
		return authorVariants.some(v => folded.includes(foldDiacritics(v)));
	});
	if (!authorMatches) return false;
	if (!target.title_keywords || target.title_keywords.length === 0) return true;
	const foldedTitle = foldDiacritics(itemTitle.toLowerCase());
	return target.title_keywords.some(kw => foldedTitle.includes(foldDiacritics(kw.toLowerCase())));
}

/**
 * Sort by total (non-self) match count across all targets, descending, and
 * cap to `maxItems`.
 * @param {Array<any>} items - MentionEvidenceItem-shaped plain objects
 * @param {number} maxItems
 * @returns {{items: Array<any>, truncated: boolean, total_candidates: number}}
 */
function rankAndCap(items, maxItems = MENTION_MAX_EVIDENCE_ITEMS) {
	const scored = items.map(item => {
		const score = Object.values(item.target_matches)
			.filter((/** @type {any} */ m) => !m.is_self)
			.reduce((sum, /** @type {any} */ m) => sum + m.count, 0);
		return { item, score };
	});
	scored.sort((a, b) => b.score - a.score);
	const capped = scored.slice(0, maxItems).map(s => s.item);
	return {
		items: capped,
		truncated: items.length > maxItems,
		total_candidates: items.length,
	};
}

/**
 * Merge a newly-found attachment's per-target matches into an already-seen
 * parent item's accumulated evidence (an item can have multiple matching
 * attachments, e.g. two language versions).
 * @param {Record<string, any>} existing
 * @param {Record<string, any>} incoming
 * @param {number} [maxSnippets]
 * @returns {void}
 */
function mergeTargetMatches(existing, incoming, maxSnippets = MENTION_MAX_SNIPPETS_PER_TARGET) {
	for (const [key, match] of Object.entries(incoming)) {
		if (!existing[key]) {
			existing[key] = { ...match, snippets: [...match.snippets] };
			continue;
		}
		existing[key].count += match.count;
		existing[key].is_self = existing[key].is_self || match.is_self;
		existing[key].snippets = existing[key].snippets.concat(match.snippets).slice(0, maxSnippets);
	}
}

var MentionSearch = {
	MENTION_SNIPPET_CHARS,
	MENTION_MAX_SNIPPETS_PER_TARGET,
	MENTION_MAX_EVIDENCE_ITEMS,
	foldDiacritics,
	transliterateGerman,
	expandVariants,
	buildSearchTerms,
	extractSnippets,
	isSelfCitation,
	rankAndCap,
	mergeTargetMatches,
};
