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
	Zotero.PreferencePanes.register({
		pluginID: 'zotero-rag@cboulanger.github.io',
		src: rootURI + 'preferences.xhtml',
		image: rootURI + 'icons/ask-rag.svg'
	});

	// Load Zotero Plugin Toolkit bundle
	Services.scriptloader.loadSubScript(rootURI + 'toolkit.bundle.js');

	// Load main plugin script and preferences pane logic
	Services.scriptloader.loadSubScript(rootURI + 'zotero-rag.js');
	Services.scriptloader.loadSubScript(rootURI + 'preferences.js');

	// Load collections API client, filing suggestions UI, and item navigation
	Services.scriptloader.loadSubScript(rootURI + 'api/collections.js');
	Services.scriptloader.loadSubScript(rootURI + 'ui/collection_suggestions.js');
	Services.scriptloader.loadSubScript(rootURI + 'ui/item_navigation.js');

	ZoteroRAG.init({ id, version, rootURI });
	Zotero.ZoteroRAG = ZoteroRAG;
	ZoteroRAG.addToAllWindows();
	await ZoteroRAG.main();

	// Register the item pane section for filing suggestions
	registerFilingSuggestionsPane();
	// Register item navigation (Back/Forward buttons)
	registerItemNavigation();
}

function onMainWindowLoad({ window }) {
	ZoteroRAG.addToWindow(window);
}

function onMainWindowUnload({ window }) {
	ZoteroRAG.removeFromWindow(window);
}

function shutdown() {
	log("Shutting down");

	// Unregister the filing suggestions item pane section
	try { unregisterFilingSuggestionsPane(); } catch (_) {}
	// Unregister item navigation
	try { unregisterItemNavigation(); } catch (_) {}

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
