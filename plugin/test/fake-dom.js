// Minimal DOM stand-in for tests of code that builds rows with createElementNS.
// Not a test file: it only exports helpers (node --test runs it as a no-op).

/** @returns {any} a fake element that records its children */
function fakeNode(tag = 'div') {
	return {
		tag, style: {}, className: '', textContent: '', children: [],
		appendChild(child) { this.children.push(child); return child; },
		append(...nodes) { this.children.push(...nodes); },
		replaceChildren(...nodes) { this.children = [...nodes]; },
		attrs: {},
		setAttribute(name, value) { this.attrs[name] = value; },
		listeners: {},
		addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
		/** Fire the click handlers (a disabled or hidden control does nothing, like a real one). */
		click() { if (!this.disabled && !this.hidden) (this.listeners.click || []).forEach((fn) => fn({ target: this })); },
	};
}

/** `createElementNS` for a fake document. */
const createElementNS = (_ns, tag) => fakeNode(tag);

/** Texts and bar widths of the rows painted into a bars container. */
function readRows(container) {
	return container.children.map((row) => ({
		text: row.children[1].textContent,
		width: row.children[0].children[0].style.width,
		color: row.children[0].children[0].style.backgroundColor,
	}));
}

module.exports = { fakeNode, createElementNS, readRows };
