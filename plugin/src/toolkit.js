/**
 * Zotero Plugin Toolkit wrapper module
 * This file imports and exposes the toolkit for use in the plugin
 */

import { BasicTool, UITool, ProgressWindowHelper } from 'zotero-plugin-toolkit';

/**
 * Initialize and return toolkit instance
 * @param {object} config - Plugin configuration
 * @param {string} config.id - Plugin ID
 * @param {string} config.version - Plugin version
 * @param {string} config.rootURI - Plugin root URI
 * @returns {object} Toolkit instance with helpers
 */
export function createToolkit(config) {
	const basicTool = new BasicTool();
	const uiTool = new UITool();
	const progressHelper = new ProgressWindowHelper(config.id, 'Zotero RAG');

	return {
		basicTool,
		uiTool,
		progressHelper,

		/**
		 * Show an alert dialog using Services.prompt
		 * @param {string} message - Dialog message
		 */
		showAlert(message) {
			Services.prompt.alert(null, 'Zotero RAG', message);
		},

		/**
		 * Show an error dialog using Services.prompt
		 * @param {string} message - Error message
		 */
		showError(message) {
			Services.prompt.alert(null, 'Zotero RAG Error', message);
		},

		/**
		 * Show a progress notification
		 * @param {string} message - Message to display
		 * @param {string} type - Type of notification: 'success', 'error', or 'default'
		 */
		showNotification(message, type = 'default') {
			new progressHelper.createLine({
				text: message,
				type: type,
				progress: 100,
			}).show();
		}
	};
}
