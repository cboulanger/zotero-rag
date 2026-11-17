// Preset helpers shared by the Preferences pane and the setup wizard: the preset chooser's
// options, the header that carries a provider key, and the wizard's key-step text.
//
// Per-side endpoint configuration lives in the "Configure preset" dialog
// (preset-dialog-view.js), which renders the backend's view model.

// @ts-check

/// <reference path='./zotero-rag.js' />

var ZoteroRAGProviderSections = {};

/**
 * Request header that carries a provider key: KEY_NAME -> X-Key-Name (the backend's
 * env_var_to_header; header names are case-insensitive).
 * @param {string} keyName
 * @returns {string}
 */
ZoteroRAGProviderSections.headerForKey = function (keyName) {
	return 'X-' + keyName.split('_').map((p) => p.charAt(0).toUpperCase() + p.slice(1).toLowerCase()).join('-');
};

/**
 * What the preset controls show (pure).
 *
 * A loopback server has no per-user choice: one control changes the server default. Elsewhere
 * every user has "My preset" (their own choice among compatible presets, those needing a key
 * marked) and admins also get a "Server default" control.
 * @param {{loopback?: boolean, is_admin?: boolean, default: string, effective: string, fell_back?: string|null,
 *   selectable?: Array<{name: string, credentials: string, missing_keys?: string[]}>}} my - GET /api/config/my-preset
 * @param {{compatible_presets?: string[], default_preset?: string, preset_name: string,
 *   switchable_presets?: Array<{name: string}>}} cfg - GET /api/config
 * @returns {{mode: 'loopback'|'user', label: string, options: Array<{value: string, text: string}>, selected: string,
 *   showDefault: boolean, defaultOptions: Array<{value: string, text: string}>, defaultSelected: string, note: string}}
 */
ZoteroRAGProviderSections.buildPresetControls = function (my, cfg) {
	const fellBack = my.fell_back
		? `Your saved preset "${my.fell_back}" is not available any more; using the server default.`
		: '';
	if (my.loopback) {
		const selected = cfg.default_preset || cfg.preset_name;
		return {
			mode: 'loopback', label: 'Preset:',
			options: (cfg.compatible_presets || []).map((n) => ({ value: n, text: n })), selected,
			showDefault: false, defaultOptions: [], defaultSelected: '', note: '',
		};
	}
	const names = new Set([cfg.default_preset || my.default, ...(cfg.switchable_presets || []).map((p) => p.name)]);
	return {
		mode: 'user', label: 'My preset:',
		options: (my.selectable || []).map((p) => ({
			value: p.name,
			text: p.name + (p.name === my.default ? ' (server default)' : '') + (p.credentials === 'missing' ? ' \u2014 needs a key' : ''),
		})),
		selected: my.effective,
		showDefault: !!my.is_admin,
		defaultOptions: [...names].map((n) => ({ value: n, text: n })),
		defaultSelected: cfg.default_preset || my.default,
		note: fellBack,
	};
};

/**
 * Introduction text of the setup wizard's key step: names the preset the user starts on
 * and only the keys it needs, and points at the optional upgrade to their own account.
 * @param {{preset_name: string, preset_description?: string, default_preset?: string}} cfg - GET /api/config
 * @param {Array<{key_name: string}>} keys - GET /api/required-keys
 * @returns {string}
 */
ZoteroRAGProviderSections.keysIntro = function (cfg, keys) {
	const description = cfg.preset_description ? ` \u2014 ${cfg.preset_description}` : '';
	const needs = keys.length
		? `It needs ${keys.length === 1 ? 'this key' : 'these keys'}, which you can get from the provider's website.`
		: 'It needs no key from you.';
	return `You start on the server's preset "${cfg.preset_name}"${description}. ${needs} ` +
		'Later, under Preferences \u2192 Model preset \u2192 Configure preset, you can add your own provider account to speed things up.';
};
