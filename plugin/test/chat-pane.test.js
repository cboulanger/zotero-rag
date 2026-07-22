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
