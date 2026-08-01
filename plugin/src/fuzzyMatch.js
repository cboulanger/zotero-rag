// plugin/src/fuzzyMatch.js
// Pure, dependency-free book-title/year similarity scoring for the "Match
// Chapter" dialog (chapterActions.js). Deliberately independent from
// backend/services/chapter_retrofit.py's rapidfuzz-based scorer -- see
// docs/superpowers/specs/2026-07-31-book-chapter-menu-actions-design.md
// for why this isn't shared code.

/**
 * @param {string} a
 * @param {string} b
 * @returns {number} Levenshtein edit distance
 */
function _levenshtein(a, b) {
	const m = a.length, n = b.length;
	if (m === 0) return n;
	if (n === 0) return m;
	let prev = new Array(n + 1);
	let curr = new Array(n + 1);
	for (let j = 0; j <= n; j++) prev[j] = j;
	for (let i = 1; i <= m; i++) {
		curr[0] = i;
		for (let j = 1; j <= n; j++) {
			const cost = a[i - 1] === b[j - 1] ? 0 : 1;
			curr[j] = Math.min(curr[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost);
		}
		[prev, curr] = [curr, prev];
	}
	return prev[n];
}

/**
 * @param {string} str
 * @returns {string}
 */
function _normalizeTitle(str) {
	return (str || '').toLowerCase().trim().replace(/\s+/g, ' ');
}

/**
 * @param {string} str
 * @returns {string} words lowercased, sorted, space-joined
 */
function _sortedTokens(str) {
	return _normalizeTitle(str).split(' ').filter(Boolean).sort().join(' ');
}

/**
 * @param {string} a
 * @param {string} b
 * @returns {number} 0-100
 */
function _levenshteinRatio(a, b) {
	a = a || ''; b = b || '';
	const dist = _levenshtein(a, b);
	const maxLen = Math.max(a.length, b.length);
	if (maxLen === 0) return 100;
	return Math.round((1 - dist / maxLen) * 100);
}

var FuzzyMatch = {
	/**
	 * Token-sort-ratio-style title similarity: order-independent, 0-100.
	 * @param {string} a
	 * @param {string} b
	 * @returns {number}
	 */
	titleSimilarity(a, b) {
		return _levenshteinRatio(_sortedTokens(a), _sortedTokens(b));
	},

	/**
	 * @param {string} chapterTitle
	 * @param {number|null} chapterYear
	 * @param {{title: string, year: number|null}} book
	 * @returns {number} 0-100
	 */
	scoreCandidate(chapterTitle, chapterYear, book) {
		let score = this.titleSimilarity(chapterTitle, book.title);
		if (chapterYear != null && book.year != null) {
			const diff = Math.abs(chapterYear - book.year);
			if (diff === 0) {
				// no penalty
			} else if (diff <= 1) {
				score -= 2;
			} else {
				score -= Math.min(20, diff * 3);
			}
		}
		return Math.max(0, Math.min(100, Math.round(score)));
	},

	/**
	 * @param {string} chapterTitle
	 * @param {number|null} chapterYear
	 * @param {Array<{title: string, year: number|null}>} books
	 * @param {number} [topN]
	 * @returns {Array<object>} `books` entries with a `score` property, sorted descending, truncated to topN
	 */
	rankCandidates(chapterTitle, chapterYear, books, topN = 5) {
		return books
			.map(book => Object.assign({}, book, { score: this.scoreCandidate(chapterTitle, chapterYear, book) }))
			.sort((a, b) => b.score - a.score)
			.slice(0, topN);
	},
};
