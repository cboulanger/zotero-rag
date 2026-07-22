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

/**
 * Register the item-pane section. Called once from bootstrap.js after
 * ZoteroRAG.init() (this module's registerSection call doesn't depend on
 * ZoteroRAG itself — only on the plugin id it was given at startup).
 * @param {{pluginID: string}} opts
 */
ChatPane.init = function ({ pluginID }) {
	Zotero.ItemPaneManager.registerSection({
		paneID: 'zotero-rag-chat',
		pluginID,
		header: { l10nID: 'zotero-rag-chat-header', icon: 'chrome://zotero-rag/content/icons/chat16.svg' },
		sidenav: { l10nID: 'zotero-rag-chat-sidenav', icon: 'chrome://zotero-rag/content/icons/chat20.svg' },
		onItemChange: ({ item, setEnabled }) => {
			setEnabled(!!item && item.isNote() && item.hasTag('RAG Query Result'));
		},
		onRender: ({ body, item }) => ChatPane._render(body, item),
		sectionButtons: [
			{
				type: 'zotero-rag-trash-note',
				icon: 'chrome://zotero/skin/16/universal/trash.svg',
				l10nID: 'zotero-rag-chat-trash-button',
				onClick: ({ item }) => Zotero.Items.trashTx([item.id]),
			},
		],
	});
};

/**
 * Render the transcript + input box into the section body. DOM-only — not
 * unit tested (see plugin/test/chat-pane.test.js's header comment);
 * verified manually per this plan's final task.
 * @param {Element} body
 * @param {any} item
 */
ChatPane._render = function (body, item) {
	body.textContent = '';
	const doc = body.ownerDocument;

	const transcript = doc.createElement('div');
	for (const turn of ChatPane.getTurns(item.id)) {
		const q = doc.createElement('p');
		q.textContent = `Q: ${turn.question}`;
		const a = doc.createElement('p');
		a.textContent = `A: ${turn.answer}`;
		transcript.appendChild(q);
		transcript.appendChild(a);
	}
	body.appendChild(transcript);

	const input = doc.createElement('textarea');
	body.appendChild(input);

	const askButton = doc.createElement('button');
	askButton.textContent = 'Ask follow-up';
	askButton.addEventListener('click', async () => {
		const question = input.value.trim();
		if (!question) return;
		input.value = '';
		await ChatPane.submitFollowUp(ZoteroRAG, item, question);
		ChatPane._render(body, item);
	});
	body.appendChild(askButton);

	const freshButton = doc.createElement('button');
	freshButton.textContent = 'Start fresh search';
	freshButton.addEventListener('click', async () => {
		const question = input.value.trim();
		if (!question) return;
		input.value = '';
		await ChatPane.submitFollowUp(ZoteroRAG, item, question, { forceFresh: true });
		ChatPane._render(body, item);
	});
	body.appendChild(freshButton);
};

/**
 * Submit a follow-up turn, handling the needs_client_evidence two-phase
 * round trip the same way dialog.js does for the very first question, then
 * record and persist the turn.
 * @param {any} zoteroRAG - ZoteroRAG (passed explicitly for testability)
 * @param {any} note
 * @param {string} question
 * @param {{forceFresh?: boolean}} [opts]
 * @returns {Promise<any>} the final QueryResponse
 */
ChatPane.submitFollowUp = async function (zoteroRAG, note, question, { forceFresh = false } = {}) {
	const payload = ChatPane.buildFollowUpPayload(note.id, question, { forceFresh });

	let result = await zoteroRAG.submitQuery(question, payload.libraryIds, {
		conversationHistory: payload.conversationHistory,
		forceFreshRetrieval: payload.forceFreshRetrieval,
	});

	if (result.status === 'needs_client_evidence') {
		const zoteroLibraryIDs = payload.libraryIds
			.map((/** @type {string} */ id) => zoteroRAG._resolveZoteroLibraryID(id))
			.filter((/** @type {number|null} */ id) => id !== null);
		const evidence = await MentionSearch.findMentionEvidence(result.citation_targets, zoteroLibraryIDs);
		result = await zoteroRAG.submitQuery(question, payload.libraryIds, {
			conversationHistory: payload.conversationHistory,
			forceFreshRetrieval: payload.forceFreshRetrieval,
			clientEvidence: evidence,
			queryPlan: result.query_plan,
		});
	}

	const turn = {
		question,
		answer: result.status === 'needs_clarification' ? result.clarification_message : result.answer,
		agents_used: result.agents_used || [],
		source_refs: result.source_refs || [],
		query_plan: result.query_plan || null,
	};
	ChatPane.recordTurn(note.id, payload.libraryIds, turn);

	const libraryMap = zoteroRAG.buildLibraryMap(payload.libraryIds);
	const turnHtml = zoteroRAG.formatTurnHTML(question, result, libraryMap);
	note.setNote(note.getNote() + turnHtml);
	await note.saveTx();

	return result;
};
