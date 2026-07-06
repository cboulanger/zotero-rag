// @ts-check
// Dialog controller for the Zotero RAG setup wizard (Server -> Zotero identity -> Service API keys).

;(function() {
	/** @type {Record<string, number>} */
	const nsFlags = { warn: 0x1, error: 0x0 };
	const makeLogger = (/** @type {string} */ level) => (/** @type {any[]} */ ...args) => {
		const msg = "[Zotero RAG / setup-wizard] " + args.join(" ");
		if (level === "log" || level === "info") {
			// @ts-ignore
			Services.console.logStringMessage(msg);
		} else {
			// @ts-ignore
			const e = Cc["@mozilla.org/scripterror;1"].createInstance(Ci.nsIScriptError);
			// @ts-ignore
			e.init(msg, "", null, 0, 0, nsFlags[level], "chrome javascript");
			// @ts-ignore
			Services.console.logMessage(e);
		}
	};
	// @ts-ignore
	if (typeof console === "undefined") {
		// @ts-ignore
		globalThis.console = { log: makeLogger("log"), info: makeLogger("info"), warn: makeLogger("warn"), error: makeLogger("error") };
	} else {
		["log", "info", "warn", "error"].forEach(level => { /** @type {any} */ (console)[level] = makeLogger(level); });
	}
})();

var ZoteroSetupWizard = {
	/** @type {any} */
	plugin: null,

	/** @type {string[]} */
	steps: ['wizard-step-server', 'wizard-step-identity', 'wizard-step-keys'],

	/** @type {number} */
	currentStep: 0,

	/** @type {boolean} */
	identityValidated: false,

	/**
	 * Initialise the dialog. Called automatically after DOMContentLoaded loads this script.
	 * @returns {void}
	 */
	init() {
		// @ts-ignore - window.arguments is available in XUL/Firefox extension context
		if (!window.arguments || !window.arguments[0]) {
			console.error("No arguments passed to setup wizard dialog");
			return;
		}
		// @ts-ignore
		const args = window.arguments[0];
		this.plugin = args.plugin;

		const backendUrlInput = /** @type {HTMLInputElement} */ (document.getElementById('wizard-server-url'));
		backendUrlInput.value = this.plugin.backendURL || '';

		document.getElementById('wizard-cancel-btn').addEventListener('click', () => window.close());
		document.getElementById('wizard-back-btn').addEventListener('click', () => this.goToStep(this.currentStep - 1));
		document.getElementById('wizard-next-btn').addEventListener('click', () => this.onNext());
		document.getElementById('wizard-create-key-btn').addEventListener('click', () => {
			Zotero.launchURL('https://www.zotero.org/settings/keys/new');
		});
		document.getElementById('wizard-validate-btn').addEventListener('click', () => this.validateIdentity());

		// External links inside the wizard (including the dynamically-rendered "Get key"
		// links for Service API Keys in #wizard-service-keys-container) don't open in the
		// system browser on their own (target="_blank" is a no-op here). Route http(s)
		// links through Zotero.launchURL so they open in the user's default browser.
		// Same pattern as the click handler on #zotero-rag-prefs-container in preferences.js.
		document.addEventListener('click', (e) => {
			const anchor = /** @type {Element} */ (e.target)?.closest?.('a[href]');
			if (!anchor) return;
			const href = anchor.getAttribute('href');
			if (href && /^https?:\/\//i.test(href)) {
				e.preventDefault();
				Zotero.launchURL(href);
			}
		});

		this.goToStep(0);
	},

	/**
	 * @param {number} index
	 * @returns {void}
	 */
	goToStep(index) {
		if (index < 0 || index >= this.steps.length) return;
		this.currentStep = index;
		for (let i = 0; i < this.steps.length; i++) {
			document.getElementById(this.steps[i]).classList.toggle('active', i === index);
		}
		const backBtn = /** @type {HTMLButtonElement} */ (document.getElementById('wizard-back-btn'));
		const nextBtn = /** @type {HTMLButtonElement} */ (document.getElementById('wizard-next-btn'));
		backBtn.disabled = index === 0;
		nextBtn.textContent = index === this.steps.length - 1 ? 'Finish' : 'Next';
	},

	/**
	 * Handle the Next/Finish button for the current step.
	 * @returns {Promise<void>}
	 */
	async onNext() {
		if (this.currentStep === 0) {
			await this.confirmServer();
			return;
		}
		if (this.currentStep === 1) {
			if (!this.plugin.isLoopbackBackend() && !this.identityValidated) {
				const status = document.getElementById('wizard-identity-status');
				status.textContent = 'Please validate your Zotero API key first.';
				status.className = 'wizard-status status-error';
				return;
			}
			const enteredKeysStep = await this.enterKeysStep();
			if (!enteredKeysStep) return;
			this.goToStep(2);
			return;
		}
		// Step 3 (keys): Finish
		window.close();
	},

	/**
	 * Save + ping the server URL, then decide whether Step 2 (Zotero identity)
	 * is needed, based on whether the URL points at a loopback address.
	 * @returns {Promise<void>}
	 */
	async confirmServer() {
		const input = /** @type {HTMLInputElement} */ (document.getElementById('wizard-server-url'));
		const status = document.getElementById('wizard-server-status');
		const url = input.value.trim().replace(/\/+$/, '');
		if (!url) {
			status.textContent = 'Please enter a server URL.';
			status.className = 'wizard-status status-error';
			return;
		}
		try {
			new URL(url);
		} catch (_) {
			status.textContent = 'That does not look like a valid URL.';
			status.className = 'wizard-status status-error';
			return;
		}
		status.textContent = 'Checking server...';
		status.className = 'wizard-status';
		Zotero.Prefs.set('extensions.zotero-rag.backendURL', url, true);
		this.plugin.backendURL = url;
		try {
			await this.plugin.checkBackendVersion();
			status.textContent = '';
		} catch (e) {
			// @ts-ignore
			if (e && (e.status === 401 || e.status === 403)) {
				// Reachable, just not authenticated yet — expected, continue to Step 2.
				status.textContent = '';
			} else {
				status.textContent = `Could not reach server: ${e instanceof Error ? e.message : String(e)}`;
				status.className = 'wizard-status status-error';
				return;
			}
		}

		const identityIntro = document.getElementById('wizard-identity-intro');
		const autoindexRow = document.getElementById('wizard-autoindex-row');

		// Reset Step 2 identity UI to its default (non-loopback) state before deciding
		// again below. Without this, a prior confirmServer() call against a loopback URL
		// would leave the Zotero API key inputs hidden and identityValidated stuck at
		// true even after the user goes Back and re-confirms a non-loopback URL.
		identityIntro.textContent = 'This server requires your personal Zotero API key to authenticate you and determine which libraries you can access.';
		document.getElementById('wizard-create-key-btn').removeAttribute('hidden');
		document.getElementById('wizard-zotero-key').removeAttribute('hidden');
		document.getElementById('wizard-validate-btn').removeAttribute('hidden');
		this.identityValidated = false;

		if (this.plugin.isLoopbackBackend()) {
			identityIntro.textContent = 'This server runs on your own machine — no Zotero API key is required.';
			document.getElementById('wizard-create-key-btn').setAttribute('hidden', 'true');
			document.getElementById('wizard-zotero-key').setAttribute('hidden', 'true');
			document.getElementById('wizard-validate-btn').setAttribute('hidden', 'true');
			autoindexRow.style.display = 'none';
			this.identityValidated = true;
			this.goToStep(1);
			await this.enterKeysStep();
			this.goToStep(2);
			return;
		}
		this.goToStep(1);
	},

	/**
	 * Validate the pasted key against the backend without saving it first.
	 * @returns {Promise<void>}
	 */
	async validateIdentity() {
		const input = /** @type {HTMLInputElement} */ (document.getElementById('wizard-zotero-key'));
		const status = document.getElementById('wizard-identity-status');
		const key = input.value.trim();
		if (!key) {
			status.textContent = 'Please paste your Zotero API key.';
			status.className = 'wizard-status status-error';
			return;
		}
		status.textContent = 'Validating...';
		status.className = 'wizard-status';
		try {
			const result = await this.plugin.checkZoteroIdentity(key);
			Zotero.Prefs.set('extensions.zotero-rag.zoteroApiKey', key, true);
			this.plugin.zoteroApiKey = key;
			this.identityValidated = true;
			const count = Array.isArray(result.targets) ? result.targets.length : 0;
			status.textContent = `✓ Authenticated as ${result.username} — ${count} librar${count === 1 ? 'y' : 'ies'} accessible.`;
			status.className = 'wizard-status status-ok';
			document.getElementById('wizard-autoindex-row').style.display = 'flex';
		} catch (e) {
			this.identityValidated = false;
			status.textContent = e instanceof Error ? e.message : String(e);
			status.className = 'wizard-status status-error';
		}
	},

	/**
	 * Populate Step 3 (Service API Keys) and, if requested, submit the
	 * auto-indexing checkbox state before moving on.
	 * @returns {Promise<boolean>} False if the auto-indexing submission failed — the
	 *   caller should stay on the current step and let the user see the error rather
	 *   than silently proceeding as if it had succeeded.
	 */
	async enterKeysStep() {
		const autoindexCheckbox = /** @type {HTMLInputElement} */ (document.getElementById('wizard-autoindex-checkbox'));
		if (autoindexCheckbox && autoindexCheckbox.checked && this.plugin.zoteroApiKey) {
			const status = document.getElementById('wizard-identity-status');
			try {
				const response = await fetch(`${this.plugin.backendURL}/api/autoindex/keys`, {
					method: 'POST',
					headers: this.plugin.getAuthHeaders({ 'Content-Type': 'application/json' }),
					body: JSON.stringify({ api_key: this.plugin.zoteroApiKey }),
				});
				if (!response.ok) {
					const err = await response.json().catch(() => ({}));
					status.textContent = `Could not enable automatic indexing: ${err.detail || response.status}`;
					status.className = 'wizard-status status-error';
					return false;
				}
			} catch (e) {
				status.textContent = `Could not enable automatic indexing: ${e instanceof Error ? e.message : String(e)}`;
				status.className = 'wizard-status status-error';
				return false;
			}
		}

		await this.plugin.fetchRequiredApiKeys();
		const container = document.getElementById('wizard-service-keys-container');
		const placeholder = document.getElementById('wizard-service-keys-placeholder');
		this.plugin.renderServiceApiKeyFields(document, container, placeholder, this.plugin.requiredApiKeys);
		return true;
	},
};

ZoteroSetupWizard.init();
