# Zotero RAG Landscape: How This Project Compares

This is a survey of other tools that let you ask questions about a Zotero
library using an LLM (retrieval-augmented generation, "chat with your PDFs",
or MCP-based library access). It was compiled in July 2026 from each
project's README, website, and public documentation — not from hands-on
testing — so treat feature claims as "documented" rather than "verified",
and expect some of this to drift out of date as these projects evolve.

Three questions guided the comparison, because they're where this project
makes deliberate, opinionated choices:

1. **Multi-user server** — can one deployment serve an entire team or
   institution from a single shared index, with per-user authorization, or
   is it inherently single-user/local?
2. **Plugin-controlled administration** — can a non-technical group member
   or admin configure and monitor server-side indexing entirely from inside
   the Zotero UI, with no shell/server access?
3. **Pipeline configurability** — can the embedding model, LLM, chunking
   strategy, and retrieval parameters be swapped per deployment (e.g. fully
   local vs. fully remote), or are they fixed by the tool's authors?

## Comparison table

| Project | Type | Open source | Multi-user shared server | Plugin-controlled admin | Pipeline configurability |
|---|---|---|---|---|---|
| **This project** (cboulanger/zotero-rag) | Zotero plugin + self-hosted backend | Yes (MPL-2.0) | **Yes** — one backend serves many users/groups, authorized per-user via their own read-only Zotero API key and Zotero group membership | **Yes** — enable/monitor/pause/run-now/abort scheduled indexing entirely from Preferences, no server access needed | **Yes** — chunking, embedding model, LLM, and retrieval parameters set per deployment preset (local or remote) |
| [Beaver](https://github.com/jlegewie/beaver-zotero) | Zotero plugin, hybrid SaaS | Yes (AGPL-3.0), but depends on non-self-hostable cloud services (Supabase-backed API) | No — one library per user account | N/A (no self-hosted server to administer) | No — fixed retrieval/search strategy; paid tiers unlock hosted models, no local pipeline swap |
| [PapersGPT](https://github.com/papersgpt/papersgpt-for-zotero) | Zotero plugin, self-hosted/local | Yes (AGPL-3.0) | No — each install indexes only that user's own local library | N/A (no server component) | Yes — many LLM choices; embeddings/vector DB/rerank run locally, but no shared server to configure |
| [BibGenie](https://www.bibgenie.com/) (formerly Zotero Copilot) | Zotero plugin, hybrid SaaS | No — closed source, binary releases only | No — single-user, subscription-gated cloud features | N/A | Partial — choice of LLM provider/local inference (Ollama/LM Studio); embedding/chunking not user-configurable |
| [Zotero-RAG-Assistant](https://github.com/aahepburn/Zotero-RAG-Assistant) | Standalone Electron desktop app (not a Zotero plugin) | Yes (Apache-2.0) | No — local, single-user workspaces | N/A (no Zotero-plugin integration at all) | Yes — multiple embedding models, 8 LLM providers, configurable chunk size/overlap |
| [zotero-rag/zotero-rag](https://github.com/zotero-rag/zotero-rag) (Rust) | CLI tool | Yes (MIT) | No — single-user, runs on your own machine | N/A (no plugin, no server) | Yes — LLM/embedding/reranker provider chosen via TOML config | 
| [Zotero-ChatPDF](https://github.com/ljeagle/zotero-chatpdf) | Zotero plugin, self-hosted/local | Yes | No — single-user local library | N/A | Yes — several commercial and local LLM choices; embeddings/vector DB/rerank run locally |
| [zotero-mcp](https://github.com/54yyyu/zotero-mcp) and similar MCP servers | MCP server (no Zotero UI; used from Claude Desktop, etc.) | Yes (MIT) | No — one server instance per user's local Zotero | N/A | Partial — a few embedding-model choices for semantic search; no chat UI or citation-anchored answers inside Zotero |
| Smaller/course projects (e.g. [Masterchiefm](https://github.com/Masterchiefm/Zotero-RAG), [windfollowingheart](https://github.com/windfollowingheart/Zotero-RAG), [AesZenz](https://github.com/AesZenz/zotero-rag-assistant), [deulofeu1](https://github.com/deulofeu1/LLM-RAG-Zotero), [Graph-RAG](https://github.com/zjkhurry/Graph-RAG)) | Mostly scripts/notebooks | Yes | No | N/A | Varies, generally minimal | 

*(A DIY route also exists: pointing [Open WebUI](https://blog.stephenturner.us/p/local-rag-app-open-webui-zotero-library) at an exported Zotero PDF folder. It's fully configurable because you're assembling the whole stack yourself, but it isn't a Zotero-integrated product and has no notion of "user" at all.)*

## Naming note

[zotero-rag/zotero-rag](https://github.com/zotero-rag/zotero-rag) (a Rust CLI project) is an unrelated project that happens to share this repository's name. If you found this page while looking for that one, its comparison row above should help you tell them apart quickly.

## Detailed notes

### Beaver

A Zotero plugin from a Harvard-affiliated team. Chats with your library,
explains selected text/equations inside the PDF reader, and can search
~240M external papers via OpenAlex. Source is published under AGPL-3.0, but
the plugin talks to Beaver's own hosted Supabase/API backend rather than a
backend you run yourself — so "open source" here doesn't mean
"self-hostable multi-user server." Each user connects their own Zotero
library; there's no concept of a shared index serving a research group from
one deployment. Free tier covers local metadata/title/abstract search; a
paid subscription unlocks hosted-model chat without your own API key.

### PapersGPT

An actively developed, richly multi-model Zotero plugin (supports a long
list of commercial and open-weight LLMs, plus its own MCP server so
external clients can reach your library too). Fully open source and
self-hosted in the sense that everything — embeddings, vector store,
reranking — runs on your own machine. What it doesn't have is a *server*:
it's built around one person's local Zotero install, so there's nothing to
deploy for a team, no per-user authorization model, and no admin/scheduling
surface — each user manages their own instance.

### BibGenie

A polished, actively marketed commercial product (subscription-based
credits, closed-source distribution). Runs inside the Zotero sidebar and
can bring your own API key to avoid the metered cloud tiers. Like Beaver,
it's single-user by design — the "multi-client" angle here is its own MCP
server letting *other AI tools* reach into your one Zotero library, not
multiple *people* sharing one indexed library.

### Zotero-RAG-Assistant

The most configurable of the alternatives surveyed — swappable embedding
models, eight LLM providers, and adjustable chunking — but it's a separate
Electron desktop app, not a Zotero plugin: you don't work inside Zotero at
all, you just point the app at your Zotero SQLite database and PDF storage.
Being local/single-user by design, it has no equivalent to a shared
server, group authorization, or plugin-controlled scheduling.

### zotero-rag (Rust CLI)

A well-engineered, provider-agnostic CLI (LanceDB-backed vector store, TOML
config selecting among several embedding/LLM/reranker providers). No GUI is
planned by design ("headless mode" is meant to let others build a UI on top
of it), and it's explicitly single-user/local — there is no server or
multi-tenant story here at all.

### Zotero-ChatPDF

Despite the name, doesn't call the ChatPDF service — it runs its own local
embeddings/vector-DB/rerank pipeline against a configurable set of LLMs.
Single-user, plugin-only, no server component.

### MCP-based servers (zotero-mcp and similar)

A different category: these expose Zotero library contents as *tools* for
a general-purpose MCP client (Claude Desktop, Cursor, etc.) rather than
providing a chat experience inside Zotero itself. Some (e.g. `54yyyu/zotero-mcp`)
include a semantic-search embedding step with a couple of model choices, but
none of the surveyed servers provide grounded, citation-anchored answers
rendered inside the Zotero UI, and each server instance is tied to one
person's local Zotero library — there's no shared multi-user index.

## Summary

Across every alternative surveyed, the design space splits into two camps:
tools with a **rich, swappable RAG pipeline** but strictly **single-user/
local** scope (PapersGPT, Zotero-RAG-Assistant, the Rust `zotero-rag`
CLI, Zotero-ChatPDF), and tools with some notion of a **shared/hosted
backend** but a **fixed pipeline and no self-hosted multi-tenant option**
(Beaver, BibGenie). None combine a self-hosted server that authorizes many
users against a Zotero group and lets them administer that server's
indexing schedule from the plugin UI, with a fully swappable local-or-remote
RAG pipeline underneath. That combination — one deployment serving a whole
research group, configured once by whoever runs it, controlled day-to-day
by group members from inside Zotero itself — is this project's core bet.
