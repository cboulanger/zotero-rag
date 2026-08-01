// plugin/test/chapterActions.test.js
const assert = require('node:assert');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const CHAPTER_LINKS_PATH = path.join(__dirname, '..', 'src', 'chapterLinks.js');
const CHAPTER_ACTIONS_PATH = path.join(__dirname, '..', 'src', 'chapterActions.js');

/**
 * ChapterActions references the global ChapterLinks (both are loaded into
 * the same window scope by bootstrap.js), so load chapterLinks.js into the
 * same vm context first, exactly matching the real load order.
 * @param {any} zoteroStub
 * @returns {any} the ChapterActions object
 */
function loadChapterActions(zoteroStub) {
	const context = { Zotero: zoteroStub, console };
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(CHAPTER_LINKS_PATH, 'utf8'), context, { filename: 'chapterLinks.js' });
	vm.runInContext(fs.readFileSync(CHAPTER_ACTIONS_PATH, 'utf8'), context, { filename: 'chapterActions.js' });
	return context.ChapterActions;
}

// computeEnablement runs inside the vm context and returns a freshly-built
// object literal there, so it belongs to that context's own realm --
// assert.deepStrictEqual would report a spurious mismatch against an
// object literal built in this (outer) realm, even with identical
// key/value contents, because it also compares [[Prototype]]. Compare the
// two boolean fields individually instead.
/**
 * @param {{segmentEnabled: boolean, matchEnabled: boolean}} actual
 * @param {boolean} segmentEnabled
 * @param {boolean} matchEnabled
 */
function assertEnablement(actual, segmentEnabled, matchEnabled) {
	assert.strictEqual(actual.segmentEnabled, segmentEnabled);
	assert.strictEqual(actual.matchEnabled, matchEnabled);
}

test('computeEnablement disables both actions unless exactly one item is selected', () => {
	const ChapterActions = loadChapterActions({});
	assertEnablement(ChapterActions.computeEnablement([]), false, false);
	assertEnablement(ChapterActions.computeEnablement([{ itemType: 'book' }, { itemType: 'book' }]), false, false);
});

test('computeEnablement enables Segment Book only for a single book item', () => {
	const ChapterActions = loadChapterActions({});
	assertEnablement(ChapterActions.computeEnablement([{ itemType: 'book' }]), true, false);
	assertEnablement(ChapterActions.computeEnablement([{ itemType: 'journalArticle' }]), false, false);
});

test('computeEnablement enables Match Chapter only for a single unlinked bookSection item', () => {
	const ChapterActions = loadChapterActions({});
	const unlinked = { itemType: 'bookSection', getExtraField: () => '' };
	const linked = { itemType: 'bookSection', getExtraField: () => 'users/42:BOOK1' };
	assertEnablement(ChapterActions.computeEnablement([unlinked]), false, true);
	assertEnablement(ChapterActions.computeEnablement([linked]), false, false);
});

test('findBookPdfAttachmentID returns the first PDF attachment id', () => {
	const items = {
		101: { id: 101, attachmentContentType: 'text/html' },
		102: { id: 102, attachmentContentType: 'application/pdf' },
	};
	const ChapterActions = loadChapterActions({ Items: { get: (id) => items[id] } });
	const item = { getAttachments: () => [101, 102] };
	assert.strictEqual(ChapterActions.findBookPdfAttachmentID(item), 102);
});

test('findBookPdfAttachmentID returns null when there is no PDF attachment', () => {
	const items = { 101: { id: 101, attachmentContentType: 'text/html' } };
	const ChapterActions = loadChapterActions({ Items: { get: (id) => items[id] } });
	const item = { getAttachments: () => [101] };
	assert.strictEqual(ChapterActions.findBookPdfAttachmentID(item), null);
});

test('getPageCount prefers the cached FullText page count', async () => {
	const ChapterActions = loadChapterActions({
		FullText: { getPages: async () => ({ indexedPages: 10, total: 250 }) },
		PDFWorker: { getFullText: async () => { throw new Error('should not be called'); } },
	});
	assert.strictEqual(await ChapterActions.getPageCount(999), 250);
});

test('getPageCount falls back to PDFWorker when FullText has no cached total', async () => {
	const ChapterActions = loadChapterActions({
		FullText: { getPages: async () => undefined },
		PDFWorker: { getFullText: async () => ({ totalPages: 42 }) },
	});
	assert.strictEqual(await ChapterActions.getPageCount(999), 42);
});

test('extractYear pulls the first 4-digit year out of a Zotero date string', () => {
	const ChapterActions = loadChapterActions({});
	assert.strictEqual(ChapterActions.extractYear('2020-05-01'), 2020);
	assert.strictEqual(ChapterActions.extractYear(''), null);
	assert.strictEqual(ChapterActions.extractYear(undefined), null);
});
