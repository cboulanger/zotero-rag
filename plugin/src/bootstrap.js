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

	// Eager, plugin-lifetime scripts — loaded once at startup, not per dialog
	// window. mentions.js is also loaded separately inside dialog.xhtml for
	// that window's own separate scope; this is a second, independent load
	// into the plugin-lifetime scope, needed because chat-pane.js's
	// ChatPane.submitFollowUp calls MentionSearch.findMentionEvidence(...)
	// from this scope.
	Services.scriptloader.loadSubScript(rootURI + 'mentions.js');
	Services.scriptloader.loadSubScript(rootURI + 'chat-pane.js');

	// Load main plugin script and preferences pane logic
	Services.scriptloader.loadSubScript(rootURI + 'zotero-rag.js');
	Services.scriptloader.loadSubScript(rootURI + 'preferences.js');
	ZoteroRAG.init({ id, version, rootURI });
	ChatPane.init({ pluginID: id });
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
