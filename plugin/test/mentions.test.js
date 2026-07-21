// Tests for plugin/src/mentions.js — client-side citation/mention search over
// Zotero's local full-text index.
//
// Same technique as plugin/test/remote_indexer.test.js: evaluate the source in
// a vm context with stubbed Zotero/IOUtils globals, then pull the top-level
// `MentionSearch` object back out.

const assert = require('node:assert');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SOURCE_PATH = path.join(__dirname, '..', 'src', 'mentions.js');

/**
 * @param {any} [zoteroStub]
 * @param {any} [ioUtilsStub]
 * @returns {any} the MentionSearch object
 */
function loadMentionSearch(zoteroStub = {}, ioUtilsStub = {}) {
	const src = fs.readFileSync(SOURCE_PATH, 'utf8');
	const context = { Zotero: zoteroStub, IOUtils: ioUtilsStub, console };
	vm.createContext(context);
	vm.runInContext(src, context, { filename: 'mentions.js' });
	return context.MentionSearch;
}

test('expandVariants includes original, diacritic-folded, and transliterated forms', () => {
	const M = loadMentionSearch();
	const variants = M.expandVariants('Wiethölter');
	assert.ok(variants.includes('wiethölter'));
	assert.ok(variants.includes('wietholter'));
	assert.ok(variants.includes('wiethoelter'));
});

test('buildSearchTerms combines author variants and title keyword variants', () => {
	const M = loadMentionSearch();
	const terms = M.buildSearchTerms({ author: 'Teubner', title_keywords: ['Bukowina'] });
	assert.ok(terms.includes('teubner'));
	assert.ok(terms.includes('bukowina'));
});

test('extractSnippets counts all occurrences but caps snippet collection', () => {
	const M = loadMentionSearch();
	const text = 'a Wiethölter b Wiethölter c Wiethölter d Wiethölter e';
	const { count, snippets } = M.extractSnippets(text, ['wiethölter'], 2, 10);
	assert.strictEqual(count, 4);
	assert.strictEqual(snippets.length, 2);
});

test('extractSnippets is case-insensitive and matches multiple terms', () => {
	const M = loadMentionSearch();
	const { count } = M.extractSnippets('WIETHÖLTER and bukowina', ['wiethölter', 'bukowina']);
	assert.strictEqual(count, 2);
});

test('isSelfCitation is true when author and title keyword both match', () => {
	const M = loadMentionSearch();
	const isSelf = M.isSelfCitation(
		['Gunther Teubner'],
		'Globale Bukowina. Zur Emergenz eines transnationalen Rechtspluralismus',
		{ author: 'teubner', title_keywords: ['bukowina'] }
	);
	assert.strictEqual(isSelf, true);
});

test('isSelfCitation is false when only the author matches a different work', () => {
	const M = loadMentionSearch();
	const isSelf = M.isSelfCitation(
		['Gunther Teubner'], 'Fragmentierung des Weltrechts',
		{ author: 'teubner', title_keywords: ['bukowina'] }
	);
	assert.strictEqual(isSelf, false);
});

test('isSelfCitation is false when the author does not match at all', () => {
	const M = loadMentionSearch();
	const isSelf = M.isSelfCitation(
		['Niklas Luhmann'], 'Globale Bukowina',
		{ author: 'teubner', title_keywords: ['bukowina'] }
	);
	assert.strictEqual(isSelf, false);
});

test('rankAndCap sorts by descending non-self match count and flags truncation', () => {
	const M = loadMentionSearch();
	const items = [
		{ item_key: 'low', target_matches: { 0: { count: 1, is_self: false } } },
		{ item_key: 'high', target_matches: { 0: { count: 9, is_self: false } } },
		{ item_key: 'self-only', target_matches: { 0: { count: 99, is_self: true } } },
	];
	const result = M.rankAndCap(items, 2);
	assert.deepStrictEqual(result.items.map(i => i.item_key), ['high', 'low']);
	assert.strictEqual(result.truncated, true);
	assert.strictEqual(result.total_candidates, 3);
});

test('mergeTargetMatches sums counts and unions snippets across attachments of one item', () => {
	const M = loadMentionSearch();
	const existing = { 0: { count: 2, snippets: ['a'], is_self: false } };
	M.mergeTargetMatches(existing, { 0: { count: 3, snippets: ['b'], is_self: false } }, 5);
	assert.strictEqual(existing[0].count, 5);
	assert.deepStrictEqual(existing[0].snippets, ['a', 'b']);
});
