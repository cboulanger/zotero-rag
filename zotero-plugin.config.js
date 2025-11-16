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
  test: {
    prefs: {
      // Override scaffold's default port (23124) to use standard Zotero port (23119)
      // This ensures compatibility with Zotero browser add-ons
      "extensions.zotero.httpServer.port": 23119
    }
  }
});