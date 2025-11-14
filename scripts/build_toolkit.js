#!/usr/bin/env node

/**
 * Bundle the Zotero Plugin Toolkit for use in the plugin
 * Uses esbuild to create a single bundled file
 */

const esbuild = require('esbuild');
const path = require('path');

const inputFile = path.join(__dirname, '../plugin/src/toolkit.js');
const outputFile = path.join(__dirname, '../plugin/src/toolkit.bundle.js');

async function build() {
	try {
		console.log('[BUILD] Bundling Zotero Plugin Toolkit...');

		await esbuild.build({
			entryPoints: [inputFile],
			bundle: true,
			format: 'iife',
			globalName: 'ZoteroPluginToolkit',
			outfile: outputFile,
			platform: 'browser',
			target: 'firefox115', // Zotero 7 uses Firefox ESR 115
			sourcemap: true,
			minify: false, // Keep readable for debugging
			logLevel: 'info',
		});

		console.log('[OK] Toolkit bundled successfully');
		console.log(`[OK] Output: ${outputFile}`);
	} catch (error) {
		console.error('[FAIL] Build failed:', error);
		process.exit(1);
	}
}

build();
