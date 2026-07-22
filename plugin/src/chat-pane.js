// Follow-up chat panel attached to RAG-result notes via Zotero.ItemPaneManager.
// See docs/superpowers/specs/2026-07-22-note-followup-chat-design.md.
//
// Conversation state is session-only and lives entirely in this module's
// in-memory map — nothing is persisted beyond the note's own saved HTML
// (appended per turn by ZoteroRAG.formatTurnHTML(), see zotero-rag.js).
// A Zotero restart or plugin reload loses live continuation state; the next
// follow-up on that note just starts with empty history (full routing runs,
// no error — see docs/query-routing.md's "Follow-up conversations" section).

/**
 * @typedef {Object} ChatTurn
 * @property {string} question
 * @property {string} answer
 * @property {string[]} agents_used
 * @property {string[]} source_refs
 * @property {Object|null} query_plan
 */

var ChatPane = {
	/** @type {Map<number, {libraryIds: string[], turns: ChatTurn[]}>} */
	_conversations: new Map(),

	/**
	 * Seed (or replace) the conversation for a note — called once right after
	 * ZoteroRAG.createResultNote() saves the note, using the first turn's own
	 * result so continuation context (source_refs, query_plan) is available
	 * immediately, with no need to parse it back out of the note's HTML.
	 * @param {number} noteID
	 * @param {string[]} libraryIds
	 * @param {ChatTurn[]} turns
	 */
	seedConversation(noteID, libraryIds, turns) {
		this._conversations.set(noteID, { libraryIds: libraryIds.slice(), turns: turns.slice() });
	},

	/**
	 * @param {number} noteID
	 * @returns {ChatTurn[]}
	 */
	getTurns(noteID) {
		const conv = this._conversations.get(noteID);
		return conv ? conv.turns.slice() : [];
	},

	/**
	 * Append a turn, starting a new conversation entry if none exists yet.
	 * @param {number} noteID
	 * @param {string[]} libraryIds
	 * @param {ChatTurn} turn
	 */
	recordTurn(noteID, libraryIds, turn) {
		let conv = this._conversations.get(noteID);
		if (!conv) {
			conv = { libraryIds: libraryIds.slice(), turns: [] };
			this._conversations.set(noteID, conv);
		}
		conv.turns.push(turn);
	},

	/**
	 * Build the /api/query follow-up request payload from stored conversation state.
	 * @param {number} noteID
	 * @param {string} question
	 * @param {{forceFresh?: boolean}} [opts]
	 * @returns {{question: string, libraryIds: string[], conversationHistory: ChatTurn[], forceFreshRetrieval: boolean}}
	 */
	buildFollowUpPayload(noteID, question, { forceFresh = false } = {}) {
		const conv = this._conversations.get(noteID);
		return {
			question,
			libraryIds: conv ? conv.libraryIds.slice() : [],
			conversationHistory: conv ? conv.turns.slice() : [],
			forceFreshRetrieval: !!forceFresh,
		};
	},
};
