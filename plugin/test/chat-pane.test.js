// Tests for plugin/src/chat-pane.js's conversation-state logic — the part
// with no Zotero dependency. Loaded into a bare vm context, same technique
// as plugin/test/task_queue.test.js. Zotero.ItemPaneManager registration and
// DOM rendering are covered separately (manual verification, not unit tests
// — see docs/superpowers/plans/2026-07-22-note-followup-chat.md Task 14).

const assert = require('node:assert');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SOURCE_PATH = path.join(__dirname, '..', 'src', 'chat-pane.js');

/**
 * @param {any} [zoteroStub]
 * @returns {any} the ChatPane object
 */
function loadChatPane(zoteroStub = { ItemPaneManager: { registerSection: () => {} }, Items: { trashTx: () => {} } }) {
	const src = fs.readFileSync(SOURCE_PATH, 'utf8');
	const context = { Zotero: zoteroStub, console };
	vm.createContext(context);
	vm.runInContext(src, context, { filename: 'chat-pane.js' });
	return context.ChatPane;
}

test('seedConversation stores library ids and turns for a note', () => {
	const ChatPane = loadChatPane();
	ChatPane.seedConversation(101, ['1'], [{ question: 'Q0', answer: 'A0', agents_used: ['rag'], source_refs: ['c1'], query_plan: null }]);

	assert.deepStrictEqual(ChatPane.getTurns(101), [
		{ question: 'Q0', answer: 'A0', agents_used: ['rag'], source_refs: ['c1'], query_plan: null },
	]);
});

test('getTurns returns an empty array for an unseeded note', () => {
	const ChatPane = loadChatPane();
	// Array.from() normalizes the vm-context (foreign-realm) array returned
	// by getTurns() into a main-realm array — assert.deepStrictEqual treats
	// cross-realm arrays as non-equal even with identical contents (see
	// plugin/test/task_queue.test.js's identical note).
	assert.deepStrictEqual(Array.from(ChatPane.getTurns(999)), []);
});

test('recordTurn appends to an existing conversation', () => {
	const ChatPane = loadChatPane();
	ChatPane.seedConversation(101, ['1'], [{ question: 'Q0', answer: 'A0', agents_used: [], source_refs: [], query_plan: null }]);
	ChatPane.recordTurn(101, ['1'], { question: 'Q1', answer: 'A1', agents_used: [], source_refs: [], query_plan: null });

	assert.strictEqual(ChatPane.getTurns(101).length, 2);
	assert.strictEqual(ChatPane.getTurns(101)[1].question, 'Q1');
});

test('recordTurn on a note with no prior conversation starts a new one', () => {
	const ChatPane = loadChatPane();
	ChatPane.recordTurn(202, ['1'], { question: 'Q0', answer: 'A0', agents_used: [], source_refs: [], query_plan: null });

	assert.strictEqual(ChatPane.getTurns(202).length, 1);
});

test('buildFollowUpPayload reads the stored library ids and turns', () => {
	const ChatPane = loadChatPane();
	ChatPane.seedConversation(101, ['1', '2'], [{ question: 'Q0', answer: 'A0', agents_used: [], source_refs: [], query_plan: null }]);

	const payload = ChatPane.buildFollowUpPayload(101, 'Follow-up');

	assert.strictEqual(payload.question, 'Follow-up');
	assert.deepStrictEqual(payload.libraryIds, ['1', '2']);
	assert.strictEqual(payload.conversationHistory.length, 1);
	assert.strictEqual(payload.forceFreshRetrieval, false);
});

test('buildFollowUpPayload sets forceFreshRetrieval when requested', () => {
	const ChatPane = loadChatPane();
	ChatPane.seedConversation(101, ['1'], []);
	const payload = ChatPane.buildFollowUpPayload(101, 'Q', { forceFresh: true });
	assert.strictEqual(payload.forceFreshRetrieval, true);
});

test('buildFollowUpPayload on an unseeded note returns empty history and libraryIds', () => {
	const ChatPane = loadChatPane();
	const payload = ChatPane.buildFollowUpPayload(999, 'Q');
	// See the Array.from() note above — payload.libraryIds/conversationHistory
	// are foreign-realm arrays created inside the vm context.
	assert.deepStrictEqual(Array.from(payload.libraryIds), []);
	assert.deepStrictEqual(Array.from(payload.conversationHistory), []);
});

test('init registers an item pane section with a trash button', () => {
	/** @type {any[]} */
	const registered = [];
	const zoteroStub = {
		ItemPaneManager: { registerSection: (opts) => registered.push(opts) },
		Items: { trashTx: () => {} },
	};
	const ChatPane = loadChatPane(zoteroStub);

	ChatPane.init({ pluginID: 'zotero-rag@example.com' });

	assert.strictEqual(registered.length, 1);
	assert.strictEqual(registered[0].paneID, 'zotero-rag-chat');
	assert.strictEqual(registered[0].pluginID, 'zotero-rag@example.com');
	assert.strictEqual(registered[0].sectionButtons.length, 1);
	assert.strictEqual(registered[0].sectionButtons[0].type, 'zotero-rag-trash-note');
});

test('the trash section button calls Zotero.Items.trashTx with the item id', () => {
	/** @type {any[]} */
	const registered = [];
	/** @type {number[][]} */
	const trashedCalls = [];
	const zoteroStub = {
		ItemPaneManager: { registerSection: (opts) => registered.push(opts) },
		Items: { trashTx: (ids) => trashedCalls.push(ids) },
	};
	const ChatPane = loadChatPane(zoteroStub);
	ChatPane.init({ pluginID: 'zotero-rag@example.com' });

	registered[0].sectionButtons[0].onClick({ item: { id: 42 } });

	// The array passed to trashTx() is created inside the vm-executed source,
	// so it belongs to a different realm's Array than this file's literal —
	// normalize with Array.from() (see the getTurns() test above for the
	// same cross-realm quirk).
	assert.deepStrictEqual(trashedCalls.map((ids) => Array.from(ids)), [[42]]);
});

test('onItemChange enables the section only for notes tagged RAG Query Result', () => {
	/** @type {any[]} */
	const registered = [];
	const zoteroStub = {
		ItemPaneManager: { registerSection: (opts) => registered.push(opts) },
		Items: { trashTx: () => {} },
	};
	const ChatPane = loadChatPane(zoteroStub);
	ChatPane.init({ pluginID: 'zotero-rag@example.com' });

	/** @type {boolean[]} */
	const enabledCalls = [];
	const setEnabled = (v) => enabledCalls.push(v);

	const taggedNote = { isNote: () => true, hasTag: (t) => t === 'RAG Query Result' };
	registered[0].onItemChange({ item: taggedNote, setEnabled });
	assert.strictEqual(enabledCalls[0], true);

	const untaggedNote = { isNote: () => true, hasTag: () => false };
	registered[0].onItemChange({ item: untaggedNote, setEnabled });
	assert.strictEqual(enabledCalls[1], false);

	registered[0].onItemChange({ item: null, setEnabled });
	assert.strictEqual(enabledCalls[2], false);
});

test('submitFollowUp records the turn and appends it to the note', async () => {
	const zoteroStub = {
		ItemPaneManager: { registerSection: () => {} },
		Items: { trashTx: () => {} },
	};
	const ChatPane = loadChatPane(zoteroStub);
	ChatPane.seedConversation(101, ['1'], []);

	/** @type {any[]} */
	const submittedOptions = [];
	const fakeZoteroRAG = {
		submitQuery: async (question, libraryIds, options) => {
			submittedOptions.push(options);
			return {
				status: 'complete', answer: 'The answer.', answer_format: 'text',
				sources: [], agents_used: ['continuation'], source_refs: ['c1'], query_plan: null,
			};
		},
		formatTurnHTML: () => '<h2>Q</h2><p>The answer.</p>',
		buildLibraryMap: () => new Map(),
	};
	/** @type {string[]} */
	const notedHtml = [];
	const noteStub = {
		id: 101,
		getNote: () => '<div>existing</div>',
		setNote: (html) => notedHtml.push(html),
		saveTx: async () => {},
	};

	const result = await ChatPane.submitFollowUp(fakeZoteroRAG, noteStub, 'Follow-up question');

	assert.strictEqual(result.answer, 'The answer.');
	assert.strictEqual(submittedOptions[0].conversationHistory.length, 0);
	assert.strictEqual(ChatPane.getTurns(101).length, 1);
	assert.strictEqual(ChatPane.getTurns(101)[0].question, 'Follow-up question');
	assert.strictEqual(notedHtml.length, 1);
	assert.ok(notedHtml[0].includes('The answer.'));
});

test('submitFollowUp records a needs_clarification turn using the clarification message as the answer', async () => {
	const zoteroStub = { ItemPaneManager: { registerSection: () => {} }, Items: { trashTx: () => {} } };
	const ChatPane = loadChatPane(zoteroStub);
	ChatPane.seedConversation(101, ['1'], []);

	const fakeZoteroRAG = {
		submitQuery: async () => ({
			status: 'needs_clarification', answer: '', answer_format: 'text', sources: [],
			agents_used: [], source_refs: [], query_plan: { agents_to_use: ['metadata'] },
			clarification_message: 'Please narrow by year.',
		}),
		formatTurnHTML: () => '<p>Please narrow by year.</p>',
		buildLibraryMap: () => new Map(),
	};
	const noteStub = { id: 101, getNote: () => '', setNote: () => {}, saveTx: async () => {} };

	await ChatPane.submitFollowUp(fakeZoteroRAG, noteStub, 'What has Luhmann written?');

	const turn = ChatPane.getTurns(101)[0];
	assert.strictEqual(turn.answer, 'Please narrow by year.');
	assert.deepStrictEqual(turn.source_refs, []);
});
