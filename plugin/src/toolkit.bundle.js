var ZoteroPluginToolkit = (() => {
  var __defProp = Object.defineProperty;
  var __getOwnPropDesc = Object.getOwnPropertyDescriptor;
  var __getOwnPropNames = Object.getOwnPropertyNames;
  var __hasOwnProp = Object.prototype.hasOwnProperty;
  var __export = (target, all) => {
    for (var name in all)
      __defProp(target, name, { get: all[name], enumerable: true });
  };
  var __copyProps = (to, from, except, desc) => {
    if (from && typeof from === "object" || typeof from === "function") {
      for (let key of __getOwnPropNames(from))
        if (!__hasOwnProp.call(to, key) && key !== except)
          __defProp(to, key, { get: () => from[key], enumerable: !(desc = __getOwnPropDesc(from, key)) || desc.enumerable });
    }
    return to;
  };
  var __toCommonJS = (mod) => __copyProps(__defProp({}, "__esModule", { value: true }), mod);

  // plugin/src/toolkit.js
  var toolkit_exports = {};
  __export(toolkit_exports, {
    VirtualizedTableHelper: () => VirtualizedTableHelper,
    createToolkit: () => createToolkit
  });

  // node_modules/zotero-plugin-toolkit/dist/chunk-Cl8Af3a2.js
  var __defProp2 = Object.defineProperty;
  var __export2 = (target, all) => {
    for (var name in all) __defProp2(target, name, {
      get: all[name],
      enumerable: true
    });
  };

  // node_modules/zotero-plugin-toolkit/dist/src-DEKAOAwd.js
  var version = "5.2.0";
  var DebugBridge = class DebugBridge2 {
    static version = 2;
    static passwordPref = "extensions.zotero.debug-bridge.password";
    get version() {
      return DebugBridge2.version;
    }
    _disableDebugBridgePassword;
    get disableDebugBridgePassword() {
      return this._disableDebugBridgePassword;
    }
    set disableDebugBridgePassword(value) {
      this._disableDebugBridgePassword = value;
    }
    get password() {
      return BasicTool.getZotero().Prefs.get(DebugBridge2.passwordPref, true);
    }
    set password(v) {
      BasicTool.getZotero().Prefs.set(DebugBridge2.passwordPref, v, true);
    }
    constructor() {
      this._disableDebugBridgePassword = false;
      this.initializeDebugBridge();
    }
    static setModule(instance) {
      if (!instance.debugBridge?.version || instance.debugBridge.version < DebugBridge2.version) instance.debugBridge = new DebugBridge2();
    }
    initializeDebugBridge() {
      const debugBridgeExtension = {
        noContent: true,
        doAction: async (uri) => {
          const Zotero$1 = BasicTool.getZotero();
          const window$1 = Zotero$1.getMainWindow();
          const uriString = uri.spec.split("//").pop();
          if (!uriString) return;
          const params = {};
          uriString.split("?").pop()?.split("&").forEach((p) => {
            params[p.split("=")[0]] = decodeURIComponent(p.split("=")[1]);
          });
          const skipPasswordCheck = toolkitGlobal_default.getInstance()?.debugBridge.disableDebugBridgePassword;
          let allowed = false;
          if (skipPasswordCheck) allowed = true;
          else if (typeof params.password === "undefined" && typeof this.password === "undefined") allowed = window$1.confirm(`External App ${params.app} wants to execute command without password.
Command:
${(params.run || params.file || "").slice(0, 100)}
If you do not know what it is, please click Cancel to deny.`);
          else allowed = this.password === params.password;
          if (allowed) {
            if (params.run) try {
              const AsyncFunction = Object.getPrototypeOf(async () => {
              }).constructor;
              const f = new AsyncFunction("Zotero,window", params.run);
              await f(Zotero$1, window$1);
            } catch (e) {
              Zotero$1.debug(e);
              window$1.console.log(e);
            }
            if (params.file) try {
              Services.scriptloader.loadSubScript(params.file, {
                Zotero: Zotero$1,
                window: window$1
              });
            } catch (e) {
              Zotero$1.debug(e);
              window$1.console.log(e);
            }
          }
        },
        newChannel(uri) {
          this.doAction(uri);
        }
      };
      Services.io.getProtocolHandler("zotero").wrappedJSObject._extensions["zotero://ztoolkit-debug"] = debugBridgeExtension;
    }
  };
  var PluginBridge = class PluginBridge2 {
    static version = 1;
    get version() {
      return PluginBridge2.version;
    }
    constructor() {
      this.initializePluginBridge();
    }
    static setModule(instance) {
      if (!instance.pluginBridge?.version || instance.pluginBridge.version < PluginBridge2.version) instance.pluginBridge = new PluginBridge2();
    }
    initializePluginBridge() {
      const { AddonManager } = ChromeUtils.importESModule("resource://gre/modules/AddonManager.sys.mjs");
      const Zotero$1 = BasicTool.getZotero();
      const pluginBridgeExtension = {
        noContent: true,
        doAction: async (uri) => {
          try {
            const uriString = uri.spec.split("//").pop();
            if (!uriString) return;
            const params = {};
            uriString.split("?").pop()?.split("&").forEach((p) => {
              params[p.split("=")[0]] = decodeURIComponent(p.split("=")[1]);
            });
            if (params.action === "install" && params.url) {
              if (params.minVersion && Services.vc.compare(Zotero$1.version, params.minVersion) < 0 || params.maxVersion && Services.vc.compare(Zotero$1.version, params.maxVersion) > 0) throw new Error(`Plugin is not compatible with Zotero version ${Zotero$1.version}.The plugin requires Zotero version between ${params.minVersion} and ${params.maxVersion}.`);
              const addon = await AddonManager.getInstallForURL(params.url);
              if (addon && addon.state === AddonManager.STATE_AVAILABLE) {
                addon.install();
                hint("Plugin installed successfully.", true);
              } else throw new Error(`Plugin ${params.url} is not available.`);
            }
          } catch (e) {
            Zotero$1.logError(e);
            hint(e.message, false);
          }
        },
        newChannel(uri) {
          this.doAction(uri);
        }
      };
      Services.io.getProtocolHandler("zotero").wrappedJSObject._extensions["zotero://plugin"] = pluginBridgeExtension;
    }
  };
  function hint(content, success) {
    const progressWindow = new Zotero.ProgressWindow({ closeOnClick: true });
    progressWindow.changeHeadline("Plugin Toolkit");
    progressWindow.progress = new progressWindow.ItemProgress(success ? "chrome://zotero/skin/tick.png" : "chrome://zotero/skin/cross.png", content);
    progressWindow.progress.setProgress(100);
    progressWindow.show();
    progressWindow.startCloseTimer(5e3);
  }
  var ToolkitGlobal = class ToolkitGlobal2 {
    debugBridge;
    pluginBridge;
    prompt;
    currentWindow;
    constructor() {
      initializeModules(this);
      this.currentWindow = BasicTool.getZotero().getMainWindow();
    }
    /**
    * Get the global unique instance of `class ToolkitGlobal`.
    * @returns An instance of `ToolkitGlobal`.
    */
    static getInstance() {
      let _Zotero;
      try {
        if (typeof Zotero !== "undefined") _Zotero = Zotero;
        else _Zotero = BasicTool.getZotero();
      } catch {
      }
      if (!_Zotero) return void 0;
      let requireInit = false;
      if (!("_toolkitGlobal" in _Zotero)) {
        _Zotero._toolkitGlobal = new ToolkitGlobal2();
        requireInit = true;
      }
      const currentGlobal = _Zotero._toolkitGlobal;
      if (currentGlobal.currentWindow !== _Zotero.getMainWindow()) {
        checkWindowDependentModules(currentGlobal);
        requireInit = true;
      }
      if (requireInit) initializeModules(currentGlobal);
      return currentGlobal;
    }
  };
  function initializeModules(instance) {
    new BasicTool().log("Initializing ToolkitGlobal modules");
    setModule(instance, "prompt", {
      _ready: false,
      instance: void 0
    });
    DebugBridge.setModule(instance);
    PluginBridge.setModule(instance);
  }
  function setModule(instance, key, module) {
    if (!module) return;
    if (!instance[key]) instance[key] = module;
    for (const moduleKey in module) instance[key][moduleKey] ??= module[moduleKey];
  }
  function checkWindowDependentModules(instance) {
    instance.currentWindow = BasicTool.getZotero().getMainWindow();
    instance.prompt = void 0;
  }
  var toolkitGlobal_default = ToolkitGlobal;
  var BasicTool = class BasicTool2 {
    /**
    * configurations.
    */
    _basicOptions;
    _console;
    static _version = version;
    /**
    * Get version - checks subclass first, then falls back to parent
    */
    get _version() {
      return version;
    }
    get basicOptions() {
      return this._basicOptions;
    }
    /**
    *
    * @param data Pass an BasicTool instance to copy its options.
    */
    constructor(data) {
      this._basicOptions = {
        log: {
          _type: "toolkitlog",
          disableConsole: false,
          disableZLog: false,
          prefix: ""
        },
        get debug() {
          if (this._debug) return this._debug;
          this._debug = toolkitGlobal_default.getInstance()?.debugBridge || {
            disableDebugBridgePassword: false,
            password: ""
          };
          return this._debug;
        },
        api: { pluginID: "zotero-plugin-toolkit@windingwind.com" },
        listeners: {
          callbacks: {
            onMainWindowLoad: /* @__PURE__ */ new Set(),
            onMainWindowUnload: /* @__PURE__ */ new Set(),
            onPluginUnload: /* @__PURE__ */ new Set()
          },
          _mainWindow: void 0,
          _plugin: void 0
        }
      };
      try {
        const { ConsoleAPI } = ChromeUtils.importESModule("resource://gre/modules/Console.sys.mjs");
        this._console = new ConsoleAPI({ consoleID: `${this._basicOptions.api.pluginID}-${Date.now()}` });
      } catch {
      }
      this.updateOptions(data);
    }
    getGlobal(k) {
      if (typeof globalThis[k] !== "undefined") return globalThis[k];
      const _Zotero = BasicTool2.getZotero();
      try {
        const window$1 = _Zotero.getMainWindow();
        switch (k) {
          case "Zotero":
          case "zotero":
            return _Zotero;
          case "window":
            return window$1;
          case "windows":
            return _Zotero.getMainWindows();
          case "document":
            return window$1.document;
          case "ZoteroPane":
          case "ZoteroPane_Local":
            return _Zotero.getActiveZoteroPane();
          default:
            return window$1[k];
        }
      } catch (e) {
        Zotero.logError(e);
      }
    }
    /**
    * If it's an XUL element
    * @param elem
    */
    isXULElement(elem) {
      return elem.namespaceURI === "http://www.mozilla.org/keymaster/gatekeeper/there.is.only.xul";
    }
    /**
    * Create an XUL element
    * @param doc Document
    * @param type Element type
    * @example
    * Create a `<menuitem>`:
    * ```ts
    * const compat = new ZoteroCompat();
    * const doc = compat.getWindow().document;
    * const elem = compat.createXULElement(doc, "menuitem");
    * ```
    */
    createXULElement(doc, type) {
      return doc.createXULElement(type);
    }
    /**
    * Output to both Zotero.debug and console.log
    * @param data e.g. string, number, object, ...
    */
    log(...data) {
      if (data.length === 0) return;
      let _Zotero;
      try {
        if (typeof Zotero !== "undefined") _Zotero = Zotero;
        else _Zotero = BasicTool2.getZotero();
      } catch {
      }
      let options;
      if (data[data.length - 1]?._type === "toolkitlog") options = data.pop();
      else options = this._basicOptions.log;
      try {
        if (options.prefix) data.splice(0, 0, options.prefix);
        if (!options.disableConsole) {
          let _console;
          if (typeof console !== "undefined") _console = console;
          else if (_Zotero) _console = _Zotero.getMainWindow()?.console;
          if (!_console) {
            if (!this._console) return;
            _console = this._console;
          }
          if (_console.groupCollapsed) _console.groupCollapsed(...data);
          else _console.group(...data);
          _console.trace();
          _console.groupEnd();
        }
        if (!options.disableZLog) {
          if (typeof _Zotero === "undefined") return;
          _Zotero.debug(data.map((d) => {
            try {
              return typeof d === "object" ? JSON.stringify(d) : String(d);
            } catch {
              _Zotero.debug(d);
              return "";
            }
          }).join("\n"));
        }
      } catch (e) {
        if (_Zotero) Zotero.logError(e);
        else console.error(e);
      }
    }
    /**
    * Add a Zotero event listener callback
    * @param type Event type
    * @param callback Event callback
    */
    addListenerCallback(type, callback) {
      if (["onMainWindowLoad", "onMainWindowUnload"].includes(type)) this._ensureMainWindowListener();
      if (type === "onPluginUnload") this._ensurePluginListener();
      this._basicOptions.listeners.callbacks[type].add(callback);
    }
    /**
    * Remove a Zotero event listener callback
    * @param type Event type
    * @param callback Event callback
    */
    removeListenerCallback(type, callback) {
      this._basicOptions.listeners.callbacks[type].delete(callback);
      this._ensureRemoveListener();
    }
    /**
    * Remove all Zotero event listener callbacks when the last callback is removed.
    */
    _ensureRemoveListener() {
      const { listeners } = this._basicOptions;
      if (listeners._mainWindow && listeners.callbacks.onMainWindowLoad.size === 0 && listeners.callbacks.onMainWindowUnload.size === 0) {
        Services.wm.removeListener(listeners._mainWindow);
        delete listeners._mainWindow;
      }
      if (listeners._plugin && listeners.callbacks.onPluginUnload.size === 0) {
        Zotero.Plugins.removeObserver(listeners._plugin);
        delete listeners._plugin;
      }
    }
    /**
    * Ensure the main window listener is registered.
    */
    _ensureMainWindowListener() {
      if (this._basicOptions.listeners._mainWindow) return;
      const mainWindowListener = {
        onOpenWindow: (xulWindow) => {
          const domWindow = xulWindow.docShell.domWindow;
          const onload = async () => {
            domWindow.removeEventListener("load", onload, false);
            if (domWindow.location.href !== "chrome://zotero/content/zoteroPane.xhtml") return;
            for (const cbk of this._basicOptions.listeners.callbacks.onMainWindowLoad) try {
              cbk(domWindow);
            } catch (e) {
              this.log(e);
            }
          };
          domWindow.addEventListener("load", () => onload(), false);
        },
        onCloseWindow: async (xulWindow) => {
          const domWindow = xulWindow.docShell.domWindow;
          if (domWindow.location.href !== "chrome://zotero/content/zoteroPane.xhtml") return;
          for (const cbk of this._basicOptions.listeners.callbacks.onMainWindowUnload) try {
            cbk(domWindow);
          } catch (e) {
            this.log(e);
          }
        }
      };
      this._basicOptions.listeners._mainWindow = mainWindowListener;
      Services.wm.addListener(mainWindowListener);
    }
    /**
    * Ensure the plugin listener is registered.
    */
    _ensurePluginListener() {
      if (this._basicOptions.listeners._plugin) return;
      const pluginListener = { shutdown: (...args) => {
        for (const cbk of this._basicOptions.listeners.callbacks.onPluginUnload) try {
          cbk(...args);
        } catch (e) {
          this.log(e);
        }
      } };
      this._basicOptions.listeners._plugin = pluginListener;
      Zotero.Plugins.addObserver(pluginListener);
    }
    updateOptions(source) {
      if (!source) return this;
      if (source instanceof BasicTool2) this._basicOptions = source._basicOptions;
      else this._basicOptions = source;
      return this;
    }
    static getZotero() {
      if (typeof Zotero !== "undefined") return Zotero;
      const { Zotero: _Zotero } = ChromeUtils.importESModule("chrome://zotero/content/zotero.mjs");
      return _Zotero;
    }
  };
  var UITool = class extends BasicTool {
    get basicOptions() {
      return this._basicOptions;
    }
    /**
    * Store elements created with this instance
    *
    * @remarks
    * > What is this for?
    *
    * In bootstrap plugins, elements must be manually maintained and removed on exiting.
    *
    * This API does this for you.
    */
    elementCache;
    constructor(base) {
      super(base);
      this.elementCache = [];
      if (!this._basicOptions.ui) this._basicOptions.ui = {
        enableElementRecord: true,
        enableElementJSONLog: false,
        enableElementDOMLog: true
      };
    }
    /**
    * Remove all elements created by `createElement`.
    *
    * @remarks
    * > What is this for?
    *
    * In bootstrap plugins, elements must be manually maintained and removed on exiting.
    *
    * This API does this for you.
    */
    unregisterAll() {
      this.elementCache.forEach((e) => {
        try {
          e?.deref()?.remove();
        } catch (e$1) {
          this.log(e$1);
        }
      });
    }
    createElement(...args) {
      const doc = args[0];
      const tagName = args[1].toLowerCase();
      let props = args[2] || {};
      if (!tagName) return;
      if (typeof args[2] === "string") props = {
        namespace: args[2],
        enableElementRecord: args[3]
      };
      if (typeof props.enableElementJSONLog !== "undefined" && props.enableElementJSONLog || this.basicOptions.ui.enableElementJSONLog) this.log(props);
      props.properties = props.properties || props.directAttributes;
      props.children = props.children || props.subElementOptions;
      let elem;
      if (tagName === "fragment") {
        const fragElem = doc.createDocumentFragment();
        elem = fragElem;
      } else {
        let realElem = props.id && (props.checkExistenceParent ? props.checkExistenceParent : doc).querySelector(`#${props.id}`);
        if (realElem && props.ignoreIfExists) return realElem;
        if (realElem && props.removeIfExists) {
          realElem.remove();
          realElem = void 0;
        }
        if (props.customCheck && !props.customCheck(doc, props)) return void 0;
        if (!realElem || !props.skipIfExists) {
          let namespace = props.namespace;
          if (!namespace) {
            const mightHTML = HTMLElementTagNames.includes(tagName);
            const mightXUL = XULElementTagNames.includes(tagName);
            const mightSVG = SVGElementTagNames.includes(tagName);
            if (Number(mightHTML) + Number(mightXUL) + Number(mightSVG) > 1) this.log(`[Warning] Creating element ${tagName} with no namespace specified. Found multiply namespace matches.`);
            if (mightHTML) namespace = "html";
            else if (mightXUL) namespace = "xul";
            else if (mightSVG) namespace = "svg";
            else namespace = "html";
          }
          if (namespace === "xul") realElem = this.createXULElement(doc, tagName);
          else realElem = doc.createElementNS({
            html: "http://www.w3.org/1999/xhtml",
            svg: "http://www.w3.org/2000/svg"
          }[namespace], tagName);
          if (typeof props.enableElementRecord !== "undefined" ? props.enableElementRecord : this.basicOptions.ui.enableElementRecord) this.elementCache.push(new WeakRef(realElem));
        }
        if (props.id) realElem.id = props.id;
        if (props.styles && Object.keys(props.styles).length) Object.keys(props.styles).forEach((k) => {
          const v = props.styles[k];
          typeof v !== "undefined" && (realElem.style[k] = v);
        });
        if (props.properties && Object.keys(props.properties).length) Object.keys(props.properties).forEach((k) => {
          const v = props.properties[k];
          typeof v !== "undefined" && (realElem[k] = v);
        });
        if (props.attributes && Object.keys(props.attributes).length) Object.keys(props.attributes).forEach((k) => {
          const v = props.attributes[k];
          typeof v !== "undefined" && realElem.setAttribute(k, String(v));
        });
        if (props.classList?.length) realElem.classList.add(...props.classList);
        if (props.listeners?.length) props.listeners.forEach(({ type, listener, options }) => {
          listener && realElem.addEventListener(type, listener, options);
        });
        elem = realElem;
      }
      if (props.children?.length) {
        const subElements = props.children.map((childProps) => {
          childProps.namespace = childProps.namespace || props.namespace;
          return this.createElement(doc, childProps.tag, childProps);
        }).filter((e) => e);
        elem.append(...subElements);
      }
      if (typeof props.enableElementDOMLog !== "undefined" ? props.enableElementDOMLog : this.basicOptions.ui.enableElementDOMLog) this.log(elem);
      return elem;
    }
    /**
    * Append element(s) to a node.
    * @param properties See {@link ElementProps}
    * @param container The parent node to append to.
    * @returns A Node that is the appended child (aChild),
    *          except when aChild is a DocumentFragment,
    *          in which case the empty DocumentFragment is returned.
    */
    appendElement(properties, container) {
      return container.appendChild(this.createElement(container.ownerDocument, properties.tag, properties));
    }
    /**
    * Inserts a node before a reference node as a child of its parent node.
    * @param properties See {@link ElementProps}
    * @param referenceNode The node before which newNode is inserted.
    * @returns Node
    */
    insertElementBefore(properties, referenceNode) {
      if (referenceNode.parentNode) return referenceNode.parentNode.insertBefore(this.createElement(referenceNode.ownerDocument, properties.tag, properties), referenceNode);
      else this.log(`${referenceNode.tagName} has no parent, cannot insert ${properties.tag}`);
    }
    /**
    * Replace oldNode with a new one.
    * @param properties See {@link ElementProps}
    * @param oldNode The child to be replaced.
    * @returns The replaced Node. This is the same node as oldChild.
    */
    replaceElement(properties, oldNode) {
      if (oldNode.parentNode) return oldNode.parentNode.replaceChild(this.createElement(oldNode.ownerDocument, properties.tag, properties), oldNode);
      else this.log(`${oldNode.tagName} has no parent, cannot replace it with ${properties.tag}`);
    }
    /**
    * Parse XHTML to XUL fragment.
    * @param str xhtml raw text
    * @param entities dtd file list ("chrome://xxx.dtd")
    * @param defaultXUL true for default XUL namespace
    */
    parseXHTMLToFragment(str, entities = [], defaultXUL = true) {
      const parser = new DOMParser();
      const xulns = "http://www.mozilla.org/keymaster/gatekeeper/there.is.only.xul";
      const htmlns = "http://www.w3.org/1999/xhtml";
      const wrappedStr = `${entities.length ? `<!DOCTYPE bindings [ ${entities.reduce((preamble, url, index) => {
        return `${preamble}<!ENTITY % _dtd-${index} SYSTEM "${url}"> %_dtd-${index}; `;
      }, "")}]>` : ""}
      <html:div xmlns="${defaultXUL ? xulns : htmlns}"
          xmlns:xul="${xulns}" xmlns:html="${htmlns}">
      ${str}
      </html:div>`;
      this.log(wrappedStr, parser);
      const doc = parser.parseFromString(wrappedStr, "text/xml");
      this.log(doc);
      if (doc.documentElement.localName === "parsererror") throw new Error("not well-formed XHTML");
      const range = doc.createRange();
      range.selectNodeContents(doc.querySelector("div"));
      return range.extractContents();
    }
  };
  var HTMLElementTagNames = [
    "a",
    "abbr",
    "address",
    "area",
    "article",
    "aside",
    "audio",
    "b",
    "base",
    "bdi",
    "bdo",
    "blockquote",
    "body",
    "br",
    "button",
    "canvas",
    "caption",
    "cite",
    "code",
    "col",
    "colgroup",
    "data",
    "datalist",
    "dd",
    "del",
    "details",
    "dfn",
    "dialog",
    "div",
    "dl",
    "dt",
    "em",
    "embed",
    "fieldset",
    "figcaption",
    "figure",
    "footer",
    "form",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "head",
    "header",
    "hgroup",
    "hr",
    "html",
    "i",
    "iframe",
    "img",
    "input",
    "ins",
    "kbd",
    "label",
    "legend",
    "li",
    "link",
    "main",
    "map",
    "mark",
    "menu",
    "meta",
    "meter",
    "nav",
    "noscript",
    "object",
    "ol",
    "optgroup",
    "option",
    "output",
    "p",
    "picture",
    "pre",
    "progress",
    "q",
    "rp",
    "rt",
    "ruby",
    "s",
    "samp",
    "script",
    "section",
    "select",
    "slot",
    "small",
    "source",
    "span",
    "strong",
    "style",
    "sub",
    "summary",
    "sup",
    "table",
    "tbody",
    "td",
    "template",
    "textarea",
    "tfoot",
    "th",
    "thead",
    "time",
    "title",
    "tr",
    "track",
    "u",
    "ul",
    "var",
    "video",
    "wbr"
  ];
  var XULElementTagNames = [
    "action",
    "arrowscrollbox",
    "bbox",
    "binding",
    "bindings",
    "box",
    "broadcaster",
    "broadcasterset",
    "button",
    "browser",
    "checkbox",
    "caption",
    "colorpicker",
    "column",
    "columns",
    "commandset",
    "command",
    "conditions",
    "content",
    "deck",
    "description",
    "dialog",
    "dialogheader",
    "editor",
    "grid",
    "grippy",
    "groupbox",
    "hbox",
    "iframe",
    "image",
    "key",
    "keyset",
    "label",
    "listbox",
    "listcell",
    "listcol",
    "listcols",
    "listhead",
    "listheader",
    "listitem",
    "member",
    "menu",
    "menubar",
    "menuitem",
    "menulist",
    "menupopup",
    "menuseparator",
    "observes",
    "overlay",
    "page",
    "popup",
    "popupset",
    "preference",
    "preferences",
    "prefpane",
    "prefwindow",
    "progressmeter",
    "radio",
    "radiogroup",
    "resizer",
    "richlistbox",
    "richlistitem",
    "row",
    "rows",
    "rule",
    "script",
    "scrollbar",
    "scrollbox",
    "scrollcorner",
    "separator",
    "spacer",
    "splitter",
    "stack",
    "statusbar",
    "statusbarpanel",
    "stringbundle",
    "stringbundleset",
    "tab",
    "tabbrowser",
    "tabbox",
    "tabpanel",
    "tabpanels",
    "tabs",
    "template",
    "textnode",
    "textbox",
    "titlebar",
    "toolbar",
    "toolbarbutton",
    "toolbargrippy",
    "toolbaritem",
    "toolbarpalette",
    "toolbarseparator",
    "toolbarset",
    "toolbarspacer",
    "toolbarspring",
    "toolbox",
    "tooltip",
    "tree",
    "treecell",
    "treechildren",
    "treecol",
    "treecols",
    "treeitem",
    "treerow",
    "treeseparator",
    "triple",
    "vbox",
    "window",
    "wizard",
    "wizardpage"
  ];
  var SVGElementTagNames = [
    "a",
    "animate",
    "animateMotion",
    "animateTransform",
    "circle",
    "clipPath",
    "defs",
    "desc",
    "ellipse",
    "feBlend",
    "feColorMatrix",
    "feComponentTransfer",
    "feComposite",
    "feConvolveMatrix",
    "feDiffuseLighting",
    "feDisplacementMap",
    "feDistantLight",
    "feDropShadow",
    "feFlood",
    "feFuncA",
    "feFuncB",
    "feFuncG",
    "feFuncR",
    "feGaussianBlur",
    "feImage",
    "feMerge",
    "feMergeNode",
    "feMorphology",
    "feOffset",
    "fePointLight",
    "feSpecularLighting",
    "feSpotLight",
    "feTile",
    "feTurbulence",
    "filter",
    "foreignObject",
    "g",
    "image",
    "line",
    "linearGradient",
    "marker",
    "mask",
    "metadata",
    "mpath",
    "path",
    "pattern",
    "polygon",
    "polyline",
    "radialGradient",
    "rect",
    "script",
    "set",
    "stop",
    "style",
    "svg",
    "switch",
    "symbol",
    "text",
    "textPath",
    "title",
    "tspan",
    "use",
    "view"
  ];
  var wait_exports = {};
  __export2(wait_exports, {
    waitForReader: () => waitForReader,
    waitUntil: () => waitUntil,
    waitUntilAsync: () => waitUntilAsync,
    waitUtilAsync: () => waitUtilAsync
  });
  var basicTool = new BasicTool();
  function waitUntil(condition, callback, interval = 100, timeout = 1e4) {
    const start = Date.now();
    const intervalId = basicTool.getGlobal("setInterval")(() => {
      if (condition()) {
        basicTool.getGlobal("clearInterval")(intervalId);
        callback();
      } else if (Date.now() - start > timeout) basicTool.getGlobal("clearInterval")(intervalId);
    }, interval);
  }
  var waitUtilAsync = waitUntilAsync;
  function waitUntilAsync(condition, interval = 100, timeout = 1e4) {
    return new Promise((resolve, reject) => {
      const start = Date.now();
      const intervalId = basicTool.getGlobal("setInterval")(() => {
        if (condition()) {
          basicTool.getGlobal("clearInterval")(intervalId);
          resolve();
        } else if (Date.now() - start > timeout) {
          basicTool.getGlobal("clearInterval")(intervalId);
          reject(/* @__PURE__ */ new Error("timeout"));
        }
      }, interval);
    });
  }
  async function waitForReader(reader) {
    await reader._initPromise;
    await reader._lastView.initializedPromise;
    if (reader.type === "pdf") await reader._lastView._iframeWindow.PDFViewerApplication.initializedPromise;
  }
  var icons = {
    success: "chrome://zotero/skin/tick.png",
    fail: "chrome://zotero/skin/cross.png"
  };
  var ProgressWindowHelper = class {
    win;
    lines;
    closeTime;
    /**
    *
    * @param header window header
    * @param options
    * @param options.window
    * @param options.closeOnClick
    * @param options.closeTime
    * @param options.closeOtherProgressWindows
    */
    constructor(header, options = {
      closeOnClick: true,
      closeTime: 5e3
    }) {
      this.win = new (BasicTool.getZotero()).ProgressWindow(options);
      this.lines = [];
      this.closeTime = options.closeTime || 5e3;
      this.win.changeHeadline(header);
      if (options.closeOtherProgressWindows) BasicTool.getZotero().ProgressWindowSet.closeAll();
    }
    /**
    * Create a new line
    * @param options
    * @param options.type
    * @param options.icon
    * @param options.text
    * @param options.progress
    * @param options.idx
    */
    createLine(options) {
      const icon = this.getIcon(options.type, options.icon);
      const line = new this.win.ItemProgress(icon || "", options.text || "");
      if (typeof options.progress === "number") line.setProgress(options.progress);
      this.lines.push(line);
      return this;
    }
    /**
    * Change the line content
    * @param options
    * @param options.type
    * @param options.icon
    * @param options.text
    * @param options.progress
    * @param options.idx
    */
    changeLine(options) {
      if (this.lines?.length === 0) return this;
      const idx = typeof options.idx !== "undefined" && options.idx >= 0 && options.idx < this.lines.length ? options.idx : 0;
      const icon = this.getIcon(options.type, options.icon);
      if (icon) this.lines[idx].setItemTypeAndIcon(icon);
      options.text && this.lines[idx].setText(options.text);
      typeof options.progress === "number" && this.lines[idx].setProgress(options.progress);
      this.updateIcons();
      return this;
    }
    show(closeTime = void 0) {
      this.win.show();
      typeof closeTime !== "undefined" && (this.closeTime = closeTime);
      if (this.closeTime && this.closeTime > 0) this.win.startCloseTimer(this.closeTime);
      waitUtilAsync(() => Boolean(this.lines?.[0])).then(() => {
        this.updateIcons();
      });
      return this;
    }
    /**
    * Set custom icon uri for progress window
    * @param key
    * @param uri
    */
    static setIconURI(key, uri) {
      icons[key] = uri;
    }
    getIcon(type, defaultIcon) {
      return type && type in icons ? icons[type] : defaultIcon;
    }
    updateIcons() {
      try {
        this.lines.forEach((line) => {
          waitUtilAsync(() => Boolean(line._image)).then(() => {
            const box = line._image;
            const icon = box.dataset.itemType;
            if (icon && !box.style.backgroundImage.includes("progress_arcs")) box.style.backgroundImage = `url(${box.dataset.itemType})`;
          });
        });
      } catch {
      }
    }
    changeHeadline(text, icon, postText) {
      this.win.changeHeadline(text, icon, postText);
      return this;
    }
    addLines(labels, icons$1) {
      this.win.addLines(labels, icons$1);
      return this;
    }
    addDescription(text) {
      this.win.addDescription(text);
      return this;
    }
    startCloseTimer(ms, requireMouseOver) {
      this.win.startCloseTimer(ms, requireMouseOver);
      return this;
    }
    close() {
      this.win.close();
      return this;
    }
  };
  var VirtualizedTableHelper = class extends BasicTool {
    props;
    localeStrings;
    containerId;
    treeInstance;
    window;
    React;
    ReactDOM;
    VirtualizedTable;
    IntlProvider;
    constructor(win) {
      super();
      this.window = win;
      const Zotero$1 = this.getGlobal("Zotero");
      const _require = win.require;
      this.React = _require("react");
      this.ReactDOM = _require("react-dom");
      this.VirtualizedTable = _require("components/virtualized-table");
      this.IntlProvider = _require("react-intl").IntlProvider;
      this.props = {
        id: `vtable-${Zotero$1.Utilities.randomString()}-${(/* @__PURE__ */ new Date()).getTime()}`,
        getRowCount: () => 0
      };
      this.localeStrings = Zotero$1.Intl.strings;
    }
    setProp(...args) {
      if (args.length === 1) Object.assign(this.props, args[0]);
      else if (args.length === 2) this.props[args[0]] = args[1];
      return this;
    }
    /**
    * Set locale strings, which replaces the table header's label if matches. Default it's `Zotero.Intl.strings`
    * @param localeStrings
    */
    setLocale(localeStrings) {
      Object.assign(this.localeStrings, localeStrings);
      return this;
    }
    /**
    * Set container element id that the table will be rendered on.
    * @param id element id
    */
    setContainerId(id) {
      this.containerId = id;
      return this;
    }
    /**
    * Render the table.
    * @param selectId Which row to select after rendering
    * @param onfulfilled callback after successfully rendered
    * @param onrejected callback after rendering with error
    */
    render(selectId, onfulfilled, onrejected) {
      const refreshSelection = () => {
        this.treeInstance.invalidate();
        if (typeof selectId !== "undefined" && selectId >= 0) this.treeInstance.selection.select(selectId);
        else this.treeInstance.selection.clearSelection();
      };
      if (!this.treeInstance) new Promise((resolve) => {
        const vtableProps = Object.assign({}, this.props, { ref: (ref) => {
          this.treeInstance = ref;
          resolve(void 0);
        } });
        if (vtableProps.getRowData && !vtableProps.renderItem) Object.assign(vtableProps, { renderItem: this.VirtualizedTable.makeRowRenderer(vtableProps.getRowData) });
        const elem = this.React.createElement(this.IntlProvider, {
          locale: Zotero.locale,
          messages: Zotero.Intl.strings
        }, this.React.createElement(this.VirtualizedTable, vtableProps));
        const container = this.window.document.getElementById(this.containerId);
        this.ReactDOM.createRoot(container).render(elem);
      }).then(() => {
        this.getGlobal("setTimeout")(() => {
          refreshSelection();
        });
      }).then(onfulfilled, onrejected);
      else refreshSelection();
      return this;
    }
  };

  // plugin/src/toolkit.js
  function createToolkit(config) {
    const basicTool2 = new BasicTool();
    const uiTool = new UITool();
    const progressHelper = new ProgressWindowHelper(config.id, "Zotero RAG");
    return {
      basicTool: basicTool2,
      uiTool,
      progressHelper,
      /**
       * Show an alert dialog using Services.prompt
       * @param {string} message - Dialog message
       */
      showAlert(message) {
        Services.prompt.alert(null, "Zotero RAG", message);
      },
      /**
       * Show an error dialog using Services.prompt
       * @param {string} message - Error message
       */
      showError(message) {
        Services.prompt.alert(null, "Zotero RAG Error", message);
      },
      /**
       * Show a progress notification
       * @param {string} message - Message to display
       * @param {string} type - Type of notification: 'success', 'error', or 'default'
       */
      showNotification(message, type = "default") {
        new progressHelper.createLine({
          text: message,
          type,
          progress: 100
        }).show();
      }
    };
  }
  return __toCommonJS(toolkit_exports);
})();
//# sourceMappingURL=toolkit.bundle.js.map
