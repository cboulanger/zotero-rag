var ZoteroRAG;
var chromeHandle;

function log(msg) {
	Services.console.logStringMessage("Zotero RAG: " + msg);
}

function install() {
	log("Installed");
}

async function startup({ id, version, rootURI }) {
	log(`Starting version ${version}`);

	// This bootstrap scope is a privileged JS component global, not a DOM
	// window — unlike dialog.xhtml's own window-scoped load of
	// remote_indexer.js, several Web APIs it relies on aren't present here by
	// default even though fetch/setTimeout already are. RemoteIndexer's
	// _uploadAttachment() (now loaded eagerly below, not just per-dialog)
	// needs FormData/Blob to build its multipart upload body and
	// AbortController for its per-request timeout handling (AbortSignal
	// itself isn't an importable name here — see remote_indexer.js's
	// _timeoutSignal()/_combineSignals(), which avoid AbortSignal.timeout()/
	// .any() entirely since those also require a DOM window at call time,
	// not just at import time).
	Components.utils.importGlobalProperties(["FormData", "Blob", "AbortController"]);

	// Register chrome:// protocol
	var aomStartup = Components.classes[
		"@mozilla.org/addons/addon-manager-startup;1"
	].getService(Components.interfaces.amIAddonManagerStartup);
	var manifestURI = Services.io.newURI(rootURI + "manifest.json");
	chromeHandle = aomStartup.registerChrome(manifestURI, [
		["content", "zotero-rag", rootURI]
	]);

	// Register preferences pane
	// stylesheets must be listed explicitly here — Zotero.PreferencePanes.register()
	// does not process the <linkset> inside preferences.xhtml itself (that's an
	// inert legacy XUL pattern this pane's markup happened to include), so without
	// this, none of preferences.css ever actually loads in the pane's document.
	Zotero.PreferencePanes.register({
		pluginID: 'zotero-rag@cboulanger.github.io',
		src: rootURI + 'preferences.xhtml',
		image: rootURI + 'icons/ask-rag.svg',
		stylesheets: [rootURI + 'preferences.css']
	});

	// Load Zotero Plugin Toolkit bundle
	Services.scriptloader.loadSubScript(rootURI + 'toolkit.bundle.js');

	// Load the generic task queue before zotero-rag.js, which registers a
	// metadata dispatcher on it and starts it during init().
	Services.scriptloader.loadSubScript(rootURI + 'task_queue.js');

	// Eager, plugin-lifetime script — loaded once at startup, not per dialog
	// window. dialog.js (loaded separately inside dialog.xhtml for that
	// window's own scope) depends on MentionSearch for its two-phase
	// "needs_client_evidence" protocol.
	Services.scriptloader.loadSubScript(rootURI + 'mentions.js');

	// Also eager, plugin-lifetime — ZoteroRAGPlugin's own methods (e.g.
	// retryTimeoutSkippedAttachment(), called from the Fix Unavailable dialog)
	// reference RemoteIndexer directly and must work even if the main search
	// dialog (which separately loads its own window-scoped copy into
	// dialog.xhtml for ZoteroRAGDialog's use) has never been opened.
	Services.scriptloader.loadSubScript(rootURI + 'remote_indexer.js');

	// Plugin-lifetime too: polls the backend for attachment indexed/un-indexed
	// events and keeps the emoji "indexed" tag in sync; also driven by the
	// Preferences pane's Refresh button. Must load before zotero-rag.js starts it.
	Services.scriptloader.loadSubScript(rootURI + 'indexed-tags.js');

	// Load main plugin script and preferences pane logic
	Services.scriptloader.loadSubScript(rootURI + 'zotero-rag.js');
	Services.scriptloader.loadSubScript(rootURI + 'preferences.js');
	ZoteroRAG.init({ id, version, rootURI });
	Zotero.ZoteroRAG = ZoteroRAG;
	ZoteroRAG.addToAllWindows();
	await ZoteroRAG.main();
}

function onMainWindowLoad({ window }) {
	ZoteroRAG.addToWindow(window);
}

function onMainWindowUnload({ window }) {
	ZoteroRAG.removeFromWindow(window);
}

function shutdown() {
	log("Shutting down");

	if (ZoteroRAG) {
		ZoteroRAG.removeFromAllWindows();
		ZoteroRAG = undefined;
		Zotero.ZoteroRAG = undefined;
	}

	// Unregister chrome:// protocol
	if (chromeHandle) {
		chromeHandle.destruct();
		chromeHandle = null;
	}
}

function uninstall() {
	log("Uninstalled");
}
