var ZoteroRAG;
var chromeHandle;

function log(msg) {
	Zotero.debug("Zotero RAG: " + msg);
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
		pluginID: 'zotero-rag@example.com',
		src: rootURI + 'preferences.xhtml',
		scripts: [rootURI + 'preferences.js']
	});

	// Load Zotero Plugin Toolkit bundle
	Services.scriptloader.loadSubScript(rootURI + 'toolkit.bundle.js');

	// Load main plugin script
	Services.scriptloader.loadSubScript(rootURI + 'zotero-rag.js');
	ZoteroRAG.init({ id, version, rootURI });
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
