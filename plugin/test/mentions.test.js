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

test('expandVariants filters out blank variants', () => {
	const M = loadMentionSearch();
	// Array.from normalizes the vm-context array to this realm's Array before
	// comparing — deepStrictEqual otherwise treats same-content arrays from
	// different vm realms as unequal (differing Array.prototype identity).
	assert.deepStrictEqual(Array.from(M.expandVariants('')), []);
	assert.deepStrictEqual(Array.from(M.expandVariants('   ')), []);
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

test('extractSnippets with an empty term list matches nothing (not everything)', () => {
	const M = loadMentionSearch();
	const { count, snippets } = M.extractSnippets('abcde', M.expandVariants(''));
	assert.strictEqual(count, 0);
	assert.deepStrictEqual(Array.from(snippets), []);
});

test('extractSnippets treats regex metacharacters in terms as literal text', () => {
	const M = loadMentionSearch();
	const { count } = M.extractSnippets('see Müller (Hrsg.) 2020 for details', ['müller (hrsg.)']);
	assert.strictEqual(count, 1);
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

// --- findMentionEvidence (Zotero-dependent orchestration) --------------

/**
 * Build a fake Zotero/IOUtils environment: one library, a set of attachments
 * (each with fixed full-text content) and their resolved parent items. Every
 * doc gets a synthetic parent (self-referential when `parentKey` is omitted)
 * — true parentless standalone attachments aren't modeled here, they're rare
 * and orthogonal to the logic under test.
 * @param {Array<{id: number, key: string, parentKey?: string, title: string, authors: Array<string>, text: string, indexedPages?: number, totalPages?: number}>} docs
 */
function makeEvidenceStubs(docs) {
	const files = {};
	const attachmentsByID = new Map();
	const parentsByKey = new Map();

	for (const doc of docs) {
		files[`/cache/${doc.key}`] = doc.text;
		const parentKey = doc.parentKey || doc.key;
		if (!parentsByKey.has(parentKey)) {
			parentsByKey.set(parentKey, {
				key: parentKey, libraryID: 1,
				getField: (/** @type {string} */ f) => (f === 'title' ? doc.title : ''),
			});
		}
		attachmentsByID.set(doc.id, {
			id: doc.id, key: doc.key, libraryID: 1,
			parentItemID: `parent-${parentKey}`,
		});
	}

	const zotero = {
		Search: function () {
			const conditions = [];
			return {
				libraryID: null,
				addCondition(...args) { conditions.push(args); },
				async search() {
					const term = conditions.find(c => c[0] === 'fulltextWord')[2].toLowerCase();
					return docs.filter(d => d.text.toLowerCase().includes(term)).map(d => d.id);
				},
			};
		},
		Items: {
			async getAsync(idsOrId) {
				if (Array.isArray(idsOrId)) {
					return idsOrId.map(id => attachmentsByID.get(id)).filter(Boolean);
				}
				if (typeof idsOrId === 'string' && idsOrId.startsWith('parent-')) {
					return parentsByKey.get(idsOrId.replace('parent-', ''));
				}
				return attachmentsByID.get(idsOrId);
			},
		},
		FullText: {
			getItemCacheFile: (/** @type {any} */ att) => ({ path: `/cache/${att.key}` }),
			getPages: async (/** @type {number} */ id) => {
				const doc = docs.find(d => d.id === id);
				return { indexedPages: doc.indexedPages ?? 1, total: doc.totalPages ?? 1 };
			},
			// By default, simulate a FAILED retry — a real re-index pass can fail too
			// (e.g. the underlying PDF file is itself missing locally). Tests that want
			// to exercise the retry-succeeds path override this per-test.
			indexItems: async () => { throw new Error('reindex failed'); },
		},
		ZoteroRAG: {
			_extractAuthors: (/** @type {any} */ item) => {
				const doc = docs.find(d => (d.parentKey || d.key) === item.key);
				return doc ? doc.authors : [];
			},
			_extractYear: () => null,
			getBackendLibraryId: (/** @type {number} */ id) => `u${id}`,
		},
	};
	const ioUtils = {
		readUTF8: async (/** @type {string} */ p) => {
			if (!(p in files)) throw new Error('ENOENT');
			return files[p];
		},
	};
	return { zotero, ioUtils };
}

test('findMentionEvidence intersects candidates across multiple citation targets', async () => {
	const docs = [
		{ id: 1, key: 'BOTH', title: 'Neue Theorien des Rechts', authors: ['A Buckel'],
			text: 'Wiethölter and Teubner and Bukowina appear here together.' },
		{ id: 2, key: 'ONLY_WIETHOLTER', title: 'Other Work', authors: ['B Other'],
			text: 'Only Wiethölter is mentioned here, nothing else relevant.' },
	];
	const { zotero, ioUtils } = makeEvidenceStubs(docs);
	const M = loadMentionSearch(zotero, ioUtils);

	const result = await M.findMentionEvidence(
		[{ author: 'wiethölter' }, { author: 'teubner', title_keywords: ['bukowina'] }],
		[1]
	);

	assert.strictEqual(result.items.length, 1);
	assert.strictEqual(result.items[0].item_key, 'BOTH');
});

test('findMentionEvidence flags an item as self-citation for a matching target', async () => {
	const docs = [
		{ id: 1, key: 'SELF', title: 'Globale Bukowina', authors: ['Gunther Teubner'],
			text: 'This is Teubner\'s own Bukowina article, citing Wiethölter once.' },
	];
	const { zotero, ioUtils } = makeEvidenceStubs(docs);
	const M = loadMentionSearch(zotero, ioUtils);

	const result = await M.findMentionEvidence(
		[{ author: 'teubner', title_keywords: ['bukowina'] }],
		[1]
	);

	assert.strictEqual(result.items[0].target_matches['0'].is_self, true);
});

test('findMentionEvidence flags partial indexing from Zotero.FullText.getPages', async () => {
	const docs = [
		{ id: 1, key: 'PARTIAL', title: 'T', authors: [], text: 'mentions Wiethölter here',
			indexedPages: 5, totalPages: 20 },
	];
	const { zotero, ioUtils } = makeEvidenceStubs(docs);
	const M = loadMentionSearch(zotero, ioUtils);

	const result = await M.findMentionEvidence([{ author: 'wiethölter' }], [1]);

	assert.strictEqual(result.items[0].partial_index, true);
});

test('findMentionEvidence skips a document whose cache file is unreadable, without crashing', async () => {
	const docs = [
		{ id: 1, key: 'READABLE', title: 'T1', authors: [], text: 'mentions Wiethölter here' },
		{ id: 2, key: 'UNREADABLE', title: 'T2', authors: [], text: 'also mentions Wiethölter' },
	];
	const { zotero, ioUtils } = makeEvidenceStubs(docs);
	// Deliberately break the cache read for doc 2's attachment only.
	const originalReadUTF8 = ioUtils.readUTF8;
	ioUtils.readUTF8 = async (/** @type {string} */ p) => {
		if (p === '/cache/UNREADABLE') throw new Error('ENOENT');
		return originalReadUTF8(p);
	};
	const M = loadMentionSearch(zotero, ioUtils);

	const result = await M.findMentionEvidence([{ author: 'wiethölter' }], [1]);

	assert.strictEqual(result.items.length, 1);
	assert.strictEqual(result.items[0].item_key, 'READABLE');
});

test('findMentionEvidence retries via Zotero.FullText.indexItems when the cache file is initially missing, and recovers', async () => {
	const docs = [
		{ id: 1, key: 'LAZY_INDEX', title: 'T', authors: [], text: 'mentions Wiethölter here' },
	];
	const { zotero, ioUtils } = makeEvidenceStubs(docs);

	// Simulate: Zotero's word index already has this item (makeEvidenceStubs' Zotero.Search
	// stub reflects that regardless of cache-file state), but the cache TEXT file doesn't
	// exist yet — until indexItems is called, which "regenerates" it, mirroring a real
	// re-index pass populating the file from the still-present local PDF.
	const cachePath = '/cache/LAZY_INDEX';
	const realText = docs[0].text;
	let indexItemsCalled = false;

	// First read attempt fails (simulating the missing file before indexItems runs).
	ioUtils.readUTF8 = async (/** @type {string} */ p) => {
		if (p === cachePath) throw new Error('ENOENT');
		throw new Error('ENOENT');
	};
	zotero.FullText.indexItems = async (/** @type {Array<number>} */ ids) => {
		indexItemsCalled = true;
		// Regenerate the cache file the way a real re-index pass would.
		ioUtils.readUTF8 = async (/** @type {string} */ p) => {
			if (p === cachePath) return realText;
			throw new Error('ENOENT');
		};
	};

	const M = loadMentionSearch(zotero, ioUtils);
	const result = await M.findMentionEvidence([{ author: 'wiethölter' }], [1]);

	assert.strictEqual(indexItemsCalled, true);
	assert.strictEqual(result.items.length, 1);
	assert.strictEqual(result.items[0].item_key, 'LAZY_INDEX');
});

test('findMentionEvidence returns an empty result when nothing matches', async () => {
	const { zotero, ioUtils } = makeEvidenceStubs([]);
	const M = loadMentionSearch(zotero, ioUtils);

	const result = await M.findMentionEvidence([{ author: 'nobody' }], [1]);

	// Field-by-field rather than a single deepStrictEqual(result, {...}) — the
	// object (and its nested empty array) is constructed inside the vm context
	// that loadMentionSearch evaluates mentions.js in, and deepStrictEqual
	// treats same-content objects/arrays from different vm realms as unequal
	// (differing prototype identity), same quirk as the Array.from() normalization
	// used elsewhere in this file.
	assert.deepStrictEqual(Array.from(result.items), []);
	assert.strictEqual(result.truncated, false);
	assert.strictEqual(result.total_candidates, 0);
});
