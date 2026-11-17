// Guards Zotero 11 forward compatibility: Firefox's baseline chrome CSP
// (`script-src chrome: resource: moz-src:`), which Zotero plans to stop disabling,
// blocks inline <script> elements and inline event-handler attributes in chrome:
// documents. Every plugin script must come from a file. See issue #44.

const assert = require('node:assert');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');

const SRC = path.join(__dirname, '..', 'src');
const XHTML = fs.readdirSync(SRC).filter((f) => f.endsWith('.xhtml'));

test('no xhtml document has an inline <script>', () => {
	for (const f of XHTML) {
		const text = fs.readFileSync(path.join(SRC, f), 'utf8');
		for (const m of text.matchAll(/<(?:html:)?script\b([^>]*?)(\/?)>/g)) {
			assert.match(m[1], /\ssrc="chrome:\/\/[^"]+"/, `${f}: <script> without a chrome: src`);
			if (!m[2]) {
				const rest = text.slice(m.index + m[0].length);
				assert.match(rest, /^\s*<\/(?:html:)?script>/, `${f}: <script> with inline content`);
			}
		}
	}
});

test('no xhtml document has an inline event-handler attribute', () => {
	for (const f of XHTML) {
		const text = fs.readFileSync(path.join(SRC, f), 'utf8');
		assert.doesNotMatch(text, /\son[a-z]+\s*=\s*["']/, f);
	}
});

test('every loader script referenced by an xhtml document exists', () => {
	for (const f of XHTML) {
		const text = fs.readFileSync(path.join(SRC, f), 'utf8');
		for (const m of text.matchAll(/src="chrome:\/\/zotero-rag\/content\/([^"]+\.js)"/g)) {
			assert.ok(fs.existsSync(path.join(SRC, m[1])), `${f}: missing ${m[1]}`);
		}
	}
});
