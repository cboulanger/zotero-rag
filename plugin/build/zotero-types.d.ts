/**
 * TypeScript declarations for Zotero plugin API.
 * This file provides type hints for Zotero-specific globals and APIs.
 */

// Zotero global object
declare const Zotero: {
	debug(message: string): void;
	getMainWindows(): Window[];
	getActiveZoteroPane(): ZoteroPane | null;

	Prefs: {
		get(pref: string, global?: boolean): any;
		set(pref: string, value: any, global?: boolean): void;
	};

	Libraries: {
		userLibraryID: number;
		get(id: number): ZoteroLibrary;
	};

	Groups: {
		getAll(): ZoteroGroup[];
	};

	Collections: {
		getAsync(id: number): Promise<ZoteroCollection>;
	};

	Item: new (type: string) => ZoteroItem;
};

interface ZoteroPane {
	getSelectedLibraryID(): number | null;
	getSelectedCollection(): ZoteroCollection | null;
}

interface ZoteroLibrary {
	id: number;
	libraryID: number;
	name: string;
}

interface ZoteroGroup {
	id: number;
	libraryID: number;
	name: string;
}

interface ZoteroCollection {
	id: number;
	addItem(itemId: number): Promise<void>;
}

interface ZoteroItem {
	id: number;
	libraryID: number | null;
	setNote(html: string): void;
	saveTx(): Promise<void>;
}

// Plugin global
declare var ZoteroRAG: any;

// XUL/Firefox extension APIs
interface Document {
	createXULElement(tagName: string): Element;
}

interface Window {
	ZoteroPane?: ZoteroPane;
	openDialog(url: string, name: string, features: string, args: any): Window | null;
	arguments?: any[];
}

// Components (XPCOM)
declare const Components: {
	classes: { [key: string]: any };
	interfaces: { nsIPromptService: any };
};
