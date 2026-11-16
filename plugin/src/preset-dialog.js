// Window glue of the "Configure preset" dialog (preset-dialog.xhtml).
//
// Rendering and backend logic live in preset-dialog-view.js; this file wires them to the
// plugin object passed by ZoteroRAGPlugin.openPresetDialog (backend URL, auth headers,
// preferences, the in-memory store of one-time credentials).

// @ts-check

/// <reference path='./zotero-rag.js' />
/// <reference path='./preset-dialog-view.js' />

var ZoteroRAGPresetDialogWindow = {
	/** @type {any} */
	plugin: null,
	/** @type {ReturnType<typeof ZoteroRAGPresetDialog.createController> | null} */
	controller: null,

	init() {
		// @ts-ignore - window.arguments is available in XUL/Firefox extension context
		this.plugin = window.arguments && window.arguments[0] ? window.arguments[0].plugin : null;
		const root = /** @type {HTMLElement} */ (document.getElementById('pd-root'));
		const closeButton = document.getElementById('pd-close');
		if (closeButton) closeButton.addEventListener('click', () => window.close());
		if (!this.plugin) {
			root.textContent = 'This dialog must be opened from the Zotero RAG settings.';
			return;
		}
		const plugin = this.plugin;
		const D = ZoteroRAGPresetDialog;
		const refs = D.mount(document, root);

		/** Why the last GET failed, shown in the dialog's header. */
		let lastError = '';
		/** @param {string} path */
		const get = async (path) => {
			lastError = '';
			let response;
			try {
				response = await fetch(`${plugin.backendURL}${path}`, { headers: plugin.getAuthHeaders() });
			} catch (e) {
				plugin.log(`Could not fetch ${path}: ${e}`);
				lastError = `The server at ${plugin.backendURL} did not answer. Check the server URL in Settings.`;
				return null;
			}
			if (response.ok) return await response.json();
			const data = await response.json().catch(() => ({}));
			const detail = data && typeof data.detail === 'string' ? `: ${data.detail}` : '';
			plugin.log(`GET ${path} failed: HTTP ${response.status}${detail}`);
			lastError = response.status === 404
				? 'This server does not have the Configure preset dialog yet (HTTP 404). Update and restart the backend.'
				: `The server answered HTTP ${response.status}${detail}.`;
			return null;
		};
		/** @param {string} path @param {any} body */
		const post = async (path, body) => {
			const response = await fetch(`${plugin.backendURL}${path}`, {
				method: 'POST',
				headers: plugin.getAuthHeaders({ 'Content-Type': 'application/json' }),
				body: JSON.stringify(body || {}),
			});
			return { ok: response.ok, status: response.status, data: await response.json().catch(() => ({})) };
		};

		const controller = D.createController({
			get,
			lastError: () => lastError,
			post,
			prefs: {
				get: (name) => Zotero.Prefs.get(name, true),
				set: (name, value) => Zotero.Prefs.set(name, value, true),
				clear: (name) => Zotero.Prefs.clear(name, true),
			},
			saveShared: (keyName, value) => plugin.setSharedRemoteField(keyName, value),
			// One-time credentials live on the plugin object: kept while Zotero runs, never written anywhere.
			sessionKeys: {
				get: (env) => (plugin._sessionKeys && plugin._sessionKeys[env]) || '',
				set: (env, value) => { plugin._sessionKeys = { ...(plugin._sessionKeys || {}), [env]: value }; },
				forget: (env) => { if (plugin._sessionKeys) delete plugin._sessionKeys[env]; },
			},
			onKeysChanged: async (fields) => {
				// New personal keys must ride on the next requests, and the auto-indexer keeps its own copy.
				await plugin.fetchRequiredApiKeys();
				for (const field of fields) {
					if (field.storage === 'client_secret' && (field.required_for || []).includes('indexing') && field.key) {
						const value = Zotero.Prefs.get(`${D.SECRET_PREFIX}${field.key}`, true) || '';
						await plugin.syncAutoindexEmbeddingKey(value);
					}
				}
				plugin.notifyPresetChanged('preset-dialog');
			},
			onPresetChanged: () => plugin.notifyPresetChanged('preset-dialog'),
			fetchStandinOffer: () => plugin.fetchStandinOffer(),
			// Throws the server's message on a refusal (409), which the controller shows.
			useStandin: (preset, provider, startEndpoints) => plugin.useStandin(preset, provider, startEndpoints),
			paint: (view, local) => D.paint(document, refs, view, local, {
				onTab: (side) => controller.selectTab(side),
				onToggleLog: (side) => controller.toggleLog(side),
				onAction: (action) => {
					const side = view.sides.find((s) => s.actions.includes(action));
					controller.runAction(side ? side.side : '', action);
				},
				onFieldChange: (field, value, scope) => controller.setSideField(field, value, scope),
				onSaveAccount: (card) => controller.saveAccount(card),
				onCardAction: (card, action) => controller.runCardAction(card, action),
				onDefaultChange: (name) => controller.setServerDefault(name),
				onUseStandin: (preset) => controller.useStandin(preset),
			}),
		});
		this.controller = controller;

		refs.startAll.addEventListener('click', () => controller.runWorkflow('start'));
		refs.pauseAll.addEventListener('click', () => controller.runWorkflow('pause'));
		refs.checkNow.addEventListener('click', () => controller.poll());

		// External links (key portals) open in the system browser.
		root.addEventListener('click', (e) => {
			const anchor = /** @type {Element} */ (e.target)?.closest?.('a[href]');
			const href = anchor && anchor.getAttribute('href');
			if (href && /^https?:\/\//i.test(href)) {
				e.preventDefault();
				Zotero.launchURL(href);
			}
		});

		// A preset switch made in Settings or another dialog changes everything shown here.
		plugin.observePresetChanged(window, 'preset-dialog', () => controller.poll());
		controller.poll();
	},
};

if (document.readyState === 'loading') {
	document.addEventListener('DOMContentLoaded', () => ZoteroRAGPresetDialogWindow.init());
} else {
	ZoteroRAGPresetDialogWindow.init();
}
