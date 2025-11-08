#!/usr/bin/env node

/**
 * Build script for Zotero RAG plugin
 * Creates an XPI file from the plugin source directory
 */

const fs = require('fs');
const path = require('path');
const { execSync } = require('child_process');

const PLUGIN_DIR = path.join(__dirname, '../plugin');
const SRC_DIR = path.join(PLUGIN_DIR, 'src');
const LOCALE_DIR = path.join(PLUGIN_DIR, 'locale');
const BUILD_DIR = path.join(PLUGIN_DIR, 'build');
const DIST_DIR = path.join(PLUGIN_DIR, 'dist');

/**
 * Clean build directories
 */
function clean() {
	console.log('Cleaning build directories...');
	if (fs.existsSync(BUILD_DIR)) {
		fs.rmSync(BUILD_DIR, { recursive: true });
	}
	if (fs.existsSync(DIST_DIR)) {
		fs.rmSync(DIST_DIR, { recursive: true });
	}
	fs.mkdirSync(BUILD_DIR, { recursive: true });
	fs.mkdirSync(DIST_DIR, { recursive: true });
}

/**
 * Copy directory recursively
 */
function copyDir(src, dest) {
	fs.mkdirSync(dest, { recursive: true });
	const entries = fs.readdirSync(src, { withFileTypes: true });

	for (let entry of entries) {
		const srcPath = path.join(src, entry.name);
		const destPath = path.join(dest, entry.name);

		if (entry.isDirectory()) {
			copyDir(srcPath, destPath);
		} else {
			fs.copyFileSync(srcPath, destPath);
		}
	}
}

/**
 * Copy plugin files to build directory
 */
function copyFiles() {
	console.log('Copying plugin files...');

	// Copy manifest
	fs.copyFileSync(
		path.join(PLUGIN_DIR, 'manifest.json'),
		path.join(BUILD_DIR, 'manifest.json')
	);

	// Copy source files
	copyDir(SRC_DIR, BUILD_DIR);

	// Copy locale files
	const localeDestDir = path.join(BUILD_DIR, 'locale');
	copyDir(LOCALE_DIR, localeDestDir);

	console.log('Files copied to build directory');
}

/**
 * Create XPI archive
 */
function createXPI() {
	console.log('Creating XPI archive...');

	const manifest = JSON.parse(
		fs.readFileSync(path.join(PLUGIN_DIR, 'manifest.json'), 'utf8')
	);
	const version = manifest.version;
	const xpiName = `zotero-rag-${version}.xpi`;
	const xpiPath = path.join(DIST_DIR, xpiName);

	// Change to build directory and create zip
	const cwd = process.cwd();
	process.chdir(BUILD_DIR);

	try {
		// Get all files recursively
		const files = [];
		function getFiles(dir) {
			const entries = fs.readdirSync(dir, { withFileTypes: true });
			for (let entry of entries) {
				const fullPath = path.join(dir, entry.name);
				if (entry.isDirectory()) {
					getFiles(fullPath);
				} else {
					files.push(path.relative(BUILD_DIR, fullPath));
				}
			}
		}
		getFiles(BUILD_DIR);

		// Create zip using system zip command
		const fileList = files.join(' ');
		execSync(`zip -r "${xpiPath}" ${fileList}`, { stdio: 'inherit' });

		console.log(`XPI created: ${xpiPath}`);
	} finally {
		process.chdir(cwd);
	}

	return xpiPath;
}

/**
 * Main build function
 */
function build() {
	console.log('Building Zotero RAG plugin...\n');

	try {
		clean();
		copyFiles();
		const xpiPath = createXPI();

		console.log('\n✓ Build successful!');
		console.log(`\nTo install the plugin:`);
		console.log(`1. Open Zotero`);
		console.log(`2. Go to Tools > Add-ons`);
		console.log(`3. Click the gear icon > Install Add-on From File`);
		console.log(`4. Select: ${xpiPath}`);
	} catch (error) {
		console.error('\n✗ Build failed:', error.message);
		process.exit(1);
	}
}

// Watch mode (simple implementation)
if (process.argv.includes('--watch')) {
	console.log('Watch mode not yet implemented. Use npm run plugin:build for now.');
	process.exit(1);
}

// Run build
build();
