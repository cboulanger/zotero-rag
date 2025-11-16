import { defineConfig } from "zotero-plugin-scaffold";

export default defineConfig({
  name: "Zotero RAG Plugin",
  id: "zotero-rag@example.com",
  namespace: "zotero_rag",
  source: [
    "plugin/src"
  ],
  build: {
    assets: [
        "plugin/src/**/*.*"
    ]
  },
  fluent: {
    dts: "plugin/typings/i10n.d.ts"
  },
  server: {
    prefs: {
      "extensions.zotero.httpServer.port": 23119,
      "extensions.zotero-plugin.dev-mode": true
    }
  }
});