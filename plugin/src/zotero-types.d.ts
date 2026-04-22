/**
 * TypeScript declarations for Zotero plugin API.
 * This file provides type hints for Zotero-specific globals and APIs.
 */

// Zotero global object
declare const Zotero: {
	CreatorTypes: any;
	Users: any;
	debug(message: string): void;
	getMainWindows(): Window[];
	getActiveZoteroPane(): ZoteroPane | null;

	Prefs: {
		get(pref: string, global?: boolean): any;
		set(pref: string, value: any, global?: boolean): void;
		clear(pref: string, global?: boolean): void;
	};

	DataDirectory: {
		dir: string;
	};

	Libraries: {
		userLibraryID: number;
		get(id: number): ZoteroLibrary;
	};

	Groups: {
		getAll(): ZoteroGroup[];
		get(id: number): ZoteroGroup | null;
		getByLibraryID(libraryID: number): ZoteroGroup | null;
	};

	Collections: {
		getAsync(id: number): Promise<ZoteroCollection>;
	};

	Item: new (type: string) => ZoteroItem;

	Items: {
		get(id: number): ZoteroItem;
		get(ids: number[]): ZoteroItem[];
		getAsync(id: number): Promise<ZoteroItem>;
		getAsync(ids: number[]): Promise<ZoteroItem[]>;
	};

	Search: new () => ZoteroSearch;

	Sync: {
		Storage: {
			Local: {
				getEnabledForLibrary(libraryID: number): boolean;
			};
		};
		Runner: {
			downloadFile(attachment: ZoteroItem): Promise<void>;
		};
	};

	Notifier: {
		registerObserver(
			observer: {
				notify(event: string, type: string, ids: number[], extraData: Record<number, { libraryID: number; key: string }>): void;
			},
			types: string[],
			id?: string,
			priority?: number
		): string;
		unregisterObserver(id: string): void;
	};
};

interface ZoteroPane {
	getSelectedLibraryID(): number | null;
	getSelectedCollection(): ZoteroCollection | null;
	openNoteWindow(itemID: number, col?: number, parentKey?: string): void;
	findNoteWindow(itemID: number): Window | undefined;
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
	isNote(): boolean;
	getField: any;
	version: number;
	parentItemID: any;
	id: number;
	key: string;
	libraryID: number | null;
	attachmentContentType: string | null;
	/** 0=imported_file, 1=imported_url, 2=linked_file, 3=linked_url */
	attachmentLinkMode: number | null;
	itemType: string | null;
	dateModified: string | null;
	setNote(html: string): void;
	saveTx(): Promise<void>;
	isAttachment(): boolean;
	isRegularItem(): boolean;
	getAttachments(): number[];
	getFilePathAsync(): Promise<string | null>;
	/** Ensures all item fields are loaded from the local Zotero database. No-op if already loaded. */
	loadAllData(): Promise<void>;
}

interface ZoteroSearch {
	libraryID: number;
	search(): Promise<number[]>;
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

// Firefox/Gecko globals available in the extension context
declare const PathUtils: {
	join(...parts: string[]): string;
};

declare const IOUtils: {
	readUTF8(path: string): Promise<string>;
	writeUTF8(path: string, data: string): Promise<void>;
	makeDirectory(path: string, options?: { createAncestors?: boolean }): Promise<void>;
	read(path: string): Promise<Uint8Array>;
};

// Components (XPCOM)
declare const Components: {
	classes: { [key: string]: any };
	interfaces: { nsIPromptService: any };
};
