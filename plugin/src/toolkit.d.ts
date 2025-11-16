/**
 * Type definitions for the Zotero Plugin Toolkit wrapper
 */

/**
 * Toolkit configuration
 */
export interface ToolkitConfig {
	id: string;
	version: string;
	rootURI: string;
}

/**
 * Toolkit instance
 */
export interface Toolkit {
	basicTool: any;
	uiTool: any;
	progressHelper: any;

	/**
	 * Show an alert dialog
	 * @param message - Dialog message
	 */
	showAlert(message: string): void;

	/**
	 * Show an error dialog
	 * @param message - Error message
	 */
	showError(message: string): void;

	/**
	 * Show a progress notification
	 * @param message - Message to display
	 * @param type - Type of notification: 'success', 'error', or 'default'
	 */
	showNotification(message: string, type?: string): void;
}

/**
 * Toolkit module interface
 */
export interface ZoteroPluginToolkitModule {
	/**
	 * Create and initialize toolkit instance
	 * @param config - Plugin configuration
	 * @returns Toolkit instance
	 */
	createToolkit(config: ToolkitConfig): Toolkit;
}

/**
 * Global ZoteroPluginToolkit variable created by the bundle
 */
declare var ZoteroPluginToolkit: ZoteroPluginToolkitModule;
