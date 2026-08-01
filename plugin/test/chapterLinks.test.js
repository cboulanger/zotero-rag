// plugin/test/chapterLinks.test.js
const assert = require('node:assert');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SOURCE_PATH = path.join(__dirname, '..', 'src', 'chapterLinks.js');

function loadChapterLinks(zoteroStub) {
	const src = fs.readFileSync(SOURCE_PATH, 'utf8');
	const context = { Zotero: zoteroStub };
	vm.createContext(context);
	vm.runInContext(src, context, { filename: 'chapterLinks.js' });
	return context.ChapterLinks;
}

/** In-memory stand-in for a zotero-plugin-toolkit ExtraFieldTool instance. */
function makeExtraFieldToolStub() {
	const store = new Map(); // item.key -> { fieldName -> string[] }
	return {
		getExtraField(item, key, all = false) {
			const map = store.get(item.key) || {};
			const values = map[key];
			if (!values) return undefined;
			return all ? values : values[0];
		},
		async setExtraField(item, key, value, options = {}) {
			const map = store.get(item.key) || {};
			if (value === '' || value === undefined) {
				delete map[key];
			} else {
				map[key] = Array.isArray(value) ? value : [value];
			}
			store.set(item.key, map);
		},
	};
}

function makeItem(key, libraryID = 1) {
	return {
		key,
		libraryID,
		_related: [],
		_saved: 0,
		addRelatedItem(other) { this._related.push(other.key); },
		async saveTx() { this._saved += 1; },
	};
}

test('getZoteroSlug returns users/<id> for the personal library', () => {
	const ChapterLinks = loadChapterLinks({
		Libraries: { userLibraryID: 1 },
		Users: { getCurrentUserID: () => 42 },
		Groups: {},
	});
	assert.strictEqual(ChapterLinks.getZoteroSlug(1), 'users/42');
});

test('getZoteroSlug returns groups/<id> for a group library', () => {
	const ChapterLinks = loadChapterLinks({
		Libraries: { userLibraryID: 1 },
		Users: {},
		Groups: { getByLibraryID: () => ({ id: 6297749 }) },
	});
	assert.strictEqual(ChapterLinks.getZoteroSlug(5), 'groups/6297749');
});

test('hasContainedByLink reflects the item\'s raw Extra field, not the native schema-restricted getExtraField', () => {
	const ChapterLinks = loadChapterLinks({ Libraries: {}, Users: {}, Groups: {} });
	assert.strictEqual(ChapterLinks.hasContainedByLink({ getField: () => '' }), false);
	assert.strictEqual(ChapterLinks.hasContainedByLink({ getField: () => 'Citation Key: foo2020' }), false);
	assert.strictEqual(ChapterLinks.hasContainedByLink({ getField: () => 'X-Contained-By: users/42:BOOK1' }), true);
	assert.strictEqual(ChapterLinks.hasContainedByLink({ getField: () => 'Citation Key: foo2020\nX-Contained-By: users/42:BOOK1' }), true);
});

test('writeLink sets X-Contains/X-Contained-By and native relations both ways', async () => {
	const ChapterLinks = loadChapterLinks({
		Libraries: { userLibraryID: 1 },
		Users: { getCurrentUserID: () => 42 },
		Groups: {},
	});
	const extraFieldTool = makeExtraFieldToolStub();
	const book = makeItem('BOOK1', 1);
	const chapter = makeItem('CHAP1', 1);

	await ChapterLinks.writeLink(extraFieldTool, book, chapter);

	assert.strictEqual(extraFieldTool.getExtraField(book, 'X-Contains'), 'users/42:CHAP1');
	assert.strictEqual(extraFieldTool.getExtraField(chapter, 'X-Contained-By'), 'users/42:BOOK1');
	assert.deepStrictEqual(book._related, ['CHAP1']);
	assert.deepStrictEqual(chapter._related, ['BOOK1']);
	assert.strictEqual(book._saved, 1);
	assert.strictEqual(chapter._saved, 1);
});

test('writeLink appends a second chapter to the same book without duplicating', async () => {
	const ChapterLinks = loadChapterLinks({
		Libraries: { userLibraryID: 1 },
		Users: { getCurrentUserID: () => 42 },
		Groups: {},
	});
	const extraFieldTool = makeExtraFieldToolStub();
	const book = makeItem('BOOK1', 1);
	const chapter1 = makeItem('CHAP1', 1);
	const chapter2 = makeItem('CHAP2', 1);

	await ChapterLinks.writeLink(extraFieldTool, book, chapter1);
	await ChapterLinks.writeLink(extraFieldTool, book, chapter2);
	assert.strictEqual(extraFieldTool.getExtraField(book, 'X-Contains'), 'users/42:CHAP1,users/42:CHAP2');

	// re-linking the same chapter is idempotent
	await ChapterLinks.writeLink(extraFieldTool, book, chapter1);
	assert.strictEqual(extraFieldTool.getExtraField(book, 'X-Contains'), 'users/42:CHAP1,users/42:CHAP2');
});
