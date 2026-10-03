// @ts-check
// DebugReport: collects diagnostics for the Fix Unavailable dialog's
// "Download debugging information" option. DOM-free so it can be unit-tested
// under node. All redaction/truncation is centralised in finalize(), so call
// sites cannot forget it.

/**
 * @typedef {object} StepRecord
 * @property {string} phase
 * @property {string} started_at
 * @property {number} duration_ms
 * @property {string} outcome
 * @property {any} plugin
 * @property {any} backend
 * @property {string|null} backend_note
 */

/**
 * @typedef {object} ItemRecord
 * @property {string} item_key
 * @property {string} attachment_key
 * @property {string} title
 * @property {string} mime_type
 * @property {any} file
 * @property {any} initial_state
 * @property {Array<StepRecord>} steps
 * @property {{css_class: string, text: string}|null} final_row_status
 * @property {Array<{where: string, message: string, stack?: string}>} errors
 */

/**
 * @typedef {object} ItemHandle
 * @property {(phase: string) => StepHandle} addStep
 * @property {(phase: string, reason: string) => void} skip
 * @property {(where: string, err: any) => void} addError
 * @property {(cssClass: string, text: string) => void} setFinalStatus
 */

/**
 * @typedef {object} StepHandle
 * @property {(name: string, data?: any) => void} note - Append to the step's plugin-side trail
 * @property {(outcome: string, pluginData?: any, backend?: any, backendNote?: string|null) => void} finish
 */

var ZoteroFixDebug = {
	/** Largest selection for which debug collection is offered. */
	MAX_ITEMS: 10,

	/** Max length of any single string in the report. */
	MAX_STRING: 8192,

	SENSITIVE_KEY: /authorization|api[-_]?key|secret|token|password|x-[a-z0-9-]*key/i,

	/**
	 * Replace known path prefixes (and generic user-home paths) so reports don't
	 * leak the local username or directory layout.
	 * @param {string} str
	 * @param {Array<{path: string, label: string}>} [prefixes]
	 * @returns {string}
	 */
	redactPaths(str, prefixes = []) {
		let out = String(str);
		const sorted = [...prefixes]
			.filter(p => p && p.path)
			.sort((a, b) => b.path.length - a.path.length);
		for (const { path, label } of sorted) {
			for (const variant of new Set([path, path.replace(/\\/g, '/'), path.replace(/\//g, '\\')])) {
				if (variant) out = out.split(variant).join(label);
			}
		}
		out = out.replace(/\/(?:Users|home)\/[^/\s"']+/g, '~');
		out = out.replace(/[A-Za-z]:\\Users\\[^\\\s"']+/g, '~');
		return out;
	},

	/**
	 * Mask secret-looking "key: value" patterns in free text.
	 * @param {string} str
	 * @returns {string}
	 */
	scrubSecrets(str) {
		return String(str)
			.replace(/(authorization\s*[:=]\s*)\S+(\s+\S+)?/gi, '$1[REDACTED]')
			.replace(/((?:api[_-]?key|secret|token|password)\s*[:=]\s*)[^\s,;'"]+/gi, '$1[REDACTED]')
			.replace(/(x-[a-z0-9-]*-?key\s*[:=]\s*)\S+/gi, '$1[REDACTED]');
	},

	/**
	 * Deep-sanitise a value: drop sensitive keys, redact paths/secrets in
	 * strings, cap string length, and make everything JSON-safe.
	 * @param {any} value
	 * @param {Array<{path: string, label: string}>} prefixes
	 * @param {number} [depth]
	 * @returns {any}
	 */
	sanitize(value, prefixes, depth = 0) {
		if (value === null || value === undefined) return value ?? null;
		if (depth > 12) return '[max depth]';
		if (typeof value === 'string') {
			let s = this.scrubSecrets(this.redactPaths(value, prefixes));
			if (s.length > this.MAX_STRING) s = s.slice(0, this.MAX_STRING) + `... [truncated ${s.length - this.MAX_STRING} chars]`;
			return s;
		}
		if (typeof value === 'number' || typeof value === 'boolean') return value;
		if (value instanceof Error) {
			return { name: value.name, message: this.sanitize(value.message, prefixes, depth + 1), stack: this.sanitize(value.stack || '', prefixes, depth + 1) };
		}
		if (Array.isArray(value)) return value.map(v => this.sanitize(v, prefixes, depth + 1));
		if (typeof value === 'object') {
			/** @type {Record<string, any>} */
			const out = {};
			for (const [k, v] of Object.entries(value)) {
				out[k] = this.SENSITIVE_KEY.test(k) ? '[REDACTED]' : this.sanitize(v, prefixes, depth + 1);
			}
			return out;
		}
		return String(value);
	},

	/**
	 * Create a report.
	 * @param {object} opts
	 * @param {any} [opts.plugin] - {version, zoteroVersion, platform}
	 * @param {any} [opts.backend] - {urlHost, isLocal}
	 * @param {any} [opts.library] - {backend_library_id, zotero_library_id, library_type}
	 * @param {number} opts.selectionCount
	 * @param {number} opts.totalRows
	 * @param {Array<{path: string, label: string}>} [opts.pathPrefixes]
	 * @param {() => Date} [opts.now]
	 * @returns {{startItem: (info: any, extra?: any) => ItemHandle, finalize: (summary: any) => any}}
	 */
	createReport({ plugin = {}, backend = {}, library = {}, selectionCount, totalRows, pathPrefixes = [], now = () => new Date() }) {
		const self = this;
		/** @type {Array<ItemRecord>} */
		const items = [];

		return {
			startItem(info, extra = {}) {
				const att = info.attachmentItem || {};
				/** @type {ItemRecord} */
				const rec = {
					item_key: info.parentItem?.key || att.key || '',
					attachment_key: att.key || '',
					title: info.title || '',
					mime_type: att.attachmentContentType || '',
					file: extra.file || {},
					initial_state: {
						type_label: extra.typeLabel || '',
						skip_reason: info.skipReason || null,
						is_parse_error: !!info.isParseError,
						server_download_failed: !!info.serverDownloadFailed,
						is_linked: !!info.isLinked,
					},
					steps: [],
					final_row_status: null,
					errors: [],
				};
				items.push(rec);
				return {
					addStep(phase) {
						const started = now();
						/** @type {StepRecord} */
						const step = {
							phase, started_at: started.toISOString(), duration_ms: 0,
							outcome: 'error', plugin: {}, backend: null, backend_note: null,
						};
						rec.steps.push(step);
						/** @type {Array<{name: string, data: any}>} */
						const trail = [];
						return {
							note(name, data) { trail.push({ name, data: data === undefined ? null : data }); },
							finish(outcome, pluginData = {}, backendPayload = null, backendNote = null) {
								step.outcome = outcome;
								step.duration_ms = now().getTime() - started.getTime();
								step.plugin = { ...pluginData, ...(trail.length ? { trail } : {}) };
								step.backend = backendPayload;
								step.backend_note = backendNote;
							},
						};
					},
					skip(phase, reason) {
						rec.steps.push({
							phase, started_at: now().toISOString(), duration_ms: 0, outcome: 'skipped',
							plugin: { reason }, backend: null, backend_note: null,
						});
					},
					addError(where, err) {
						rec.errors.push({
							where,
							message: err instanceof Error ? err.message : String(err),
							...(err instanceof Error && err.stack ? { stack: err.stack } : {}),
						});
					},
					setFinalStatus(cssClass, text) {
						rec.final_row_status = { css_class: cssClass, text };
					},
				};
			},

			finalize(summary) {
				const raw = {
					schema_version: 1,
					generated_at: now().toISOString(),
					tool: 'fix-unavailable',
					plugin: {
						version: plugin.version || null,
						zotero_version: plugin.zoteroVersion || null,
						platform: plugin.platform || null,
					},
					backend: { url_host: backend.urlHost || null, is_local: !!backend.isLocal },
					library,
					selection: { count: selectionCount, dialog_total_rows: totalRows },
					items,
					summary,
				};
				return self.sanitize(raw, pathPrefixes);
			},
		};
	},

	/**
	 * Save a finalized report as pretty-printed JSON via a native save dialog
	 * (same picker + IOUtils pattern as the query dialog's "Export debug info").
	 * @param {any} win - Window used to anchor the picker
	 * @param {any} data - Finalized report
	 * @param {string} defaultName
	 * @returns {Promise<string|null>} Saved file's basename, or null if the user cancelled
	 */
	async save(win, data, defaultName) {
		// @ts-ignore - Cc/Ci are globals in this privileged context
		const fp = Cc['@mozilla.org/filepicker;1'].createInstance(Ci.nsIFilePicker);
		fp.init(win.browsingContext, 'Save Debug Information', Ci.nsIFilePicker.modeSave);
		fp.appendFilter('JSON files', '*.json');
		fp.defaultString = defaultName;
		const rv = await new Promise((resolve) => fp.open(resolve));
		// @ts-ignore
		if (rv !== Ci.nsIFilePicker.returnOK && rv !== Ci.nsIFilePicker.returnReplace) return null;
		// @ts-ignore - IOUtils is a global in Firefox/Zotero
		await IOUtils.writeUTF8(fp.file.path, JSON.stringify(data, null, 2));
		return fp.file.leafName || defaultName;
	},

	/**
	 * Default file name for the saved report.
	 * @param {string} backendLibraryId
	 * @param {Date} [date]
	 * @returns {string}
	 */
	fileName(backendLibraryId, date = new Date()) {
		const pad = (/** @type {number} */ n) => String(n).padStart(2, '0');
		const stamp = `${date.getUTCFullYear()}${pad(date.getUTCMonth() + 1)}${pad(date.getUTCDate())}-${pad(date.getUTCHours())}${pad(date.getUTCMinutes())}${pad(date.getUTCSeconds())}`;
		return `zotero-rag-fix-debug-${String(backendLibraryId).replace(/[:/\\]/g, '-')}-${stamp}.json`;
	},
};
