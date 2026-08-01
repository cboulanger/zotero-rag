// plugin/test/fuzzyMatch.test.js
const assert = require('node:assert');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SOURCE_PATH = path.join(__dirname, '..', 'src', 'fuzzyMatch.js');

function loadFuzzyMatch() {
	const src = fs.readFileSync(SOURCE_PATH, 'utf8');
	const context = {};
	vm.createContext(context);
	vm.runInContext(src, context, { filename: 'fuzzyMatch.js' });
	return context.FuzzyMatch;
}

test('titleSimilarity scores identical titles as 100', () => {
	const FuzzyMatch = loadFuzzyMatch();
	assert.strictEqual(FuzzyMatch.titleSimilarity('The Great Book', 'The Great Book'), 100);
});

test('titleSimilarity is order-independent (token-sort)', () => {
	const FuzzyMatch = loadFuzzyMatch();
	assert.strictEqual(FuzzyMatch.titleSimilarity('Great The Book', 'The Great Book'), 100);
});

test('titleSimilarity scores a partial rewording lower but still similar', () => {
	const FuzzyMatch = loadFuzzyMatch();
	assert.strictEqual(
		FuzzyMatch.titleSimilarity('Handbook of Zotero Studies', 'The Zotero Studies Handbook'),
		74
	);
});

test('titleSimilarity scores unrelated titles low', () => {
	const FuzzyMatch = loadFuzzyMatch();
	assert.strictEqual(
		FuzzyMatch.titleSimilarity('Handbook of Zotero Studies', 'Introduction to Reference Managers'),
		26
	);
});

test('scoreCandidate applies no penalty for a matching year', () => {
	const FuzzyMatch = loadFuzzyMatch();
	const score = FuzzyMatch.scoreCandidate('Handbook of Zotero Studies', 2020, { title: 'Handbook of Zotero Studies', year: 2020 });
	assert.strictEqual(score, 100);
});

test('scoreCandidate applies a small penalty for a 1-year difference', () => {
	const FuzzyMatch = loadFuzzyMatch();
	const score = FuzzyMatch.scoreCandidate('Handbook of Zotero Studies', 2020, { title: 'Handbook of Zotero Studies', year: 2021 });
	assert.strictEqual(score, 98);
});

test('scoreCandidate applies a larger, capped penalty for a big year gap', () => {
	const FuzzyMatch = loadFuzzyMatch();
	const score = FuzzyMatch.scoreCandidate('Handbook of Zotero Studies', 2020, { title: 'Handbook of Zotero Studies', year: 2025 });
	assert.strictEqual(score, 85);
});

test('scoreCandidate skips the year penalty when either year is missing', () => {
	const FuzzyMatch = loadFuzzyMatch();
	const score = FuzzyMatch.scoreCandidate('Handbook of Zotero Studies', null, { title: 'Handbook of Zotero Studies', year: null });
	assert.strictEqual(score, 100);
});

test('rankCandidates sorts descending by score and truncates to topN', () => {
	const FuzzyMatch = loadFuzzyMatch();
	const books = [
		{ key: 'BOOK1', title: 'Handbook of Zotero Studies', year: 2020 },
		{ key: 'BOOK2', title: 'Introduction to Reference Managers', year: 2019 },
		{ key: 'BOOK3', title: 'The Zotero Studies Handbook', year: 2020 },
	];
	const ranked = FuzzyMatch.rankCandidates('Handbook of Zotero Studies', 2020, books, 5);
	assert.deepStrictEqual(ranked.map(b => b.key), ['BOOK1', 'BOOK3', 'BOOK2']);
	assert.strictEqual(ranked[0].score, 100);
	assert.strictEqual(ranked[1].score, 74);
	assert.strictEqual(ranked[2].score, 24);
});
