# Zotero RAG Plugin

<!-- markdownlint-disable MD033 -->

[![CI](https://github.com/cboulanger/zotero-rag/actions/workflows/ci.yml/badge.svg)](https://github.com/cboulanger/zotero-rag/actions/workflows/ci.yml)
[![Release](https://github.com/cboulanger/zotero-rag/actions/workflows/release.yml/badge.svg)](https://github.com/cboulanger/zotero-rag/actions/workflows/release.yml)
![Status: Beta](https://img.shields.io/badge/status-beta-yellow)
![API: unstable](https://img.shields.io/badge/API-unstable-orange)

This plugin implements a RAG (Retrieval-Augmented-Generation) System for Zotero which allows to ask questions on the literature in a library and get a response with links to the sources.

> **Beta:** The API and feature set are still evolving. Breaking changes may occur between releases.

## Why Zotero RAG?

Most document-chat tools require you to manually upload PDFs to a cloud service. Zotero RAG works differently:

- **Multi-user by design** — one backend deployment can serve an entire research group or institution from a single shared index, instead of everyone running their own local copy. Each member authenticates with their own read-only Zotero API key, authorized automatically via Zotero group membership — no separate accounts or credentials to manage.
- **Always in sync** — the plugin reads directly from your local Zotero library. When you add, update, or delete an item in Zotero, the index stays current without any manual export or re-upload step.
- **Bibliographic awareness** — because the index carries full Zotero metadata (authors, year, item type, title), you can ask questions that go beyond document content: *"List all books by Luhmann in my library"* or *"What journal articles on systems theory were published between 1975 and 1990?"* — answered instantly from the metadata index, without reading a single PDF. See [Query Routing](docs/query-routing.md) for how metadata and content questions are told apart.
- **Multi-library** — query across several Zotero libraries in a single question.
- **Rich source citations** — answers include page numbers and text anchors (first words of the source passage), not just titles, making it easy to locate the original passage.
- **Public web interface** — optionally expose a browser-accessible query UI for publicly readable Zotero libraries, so collaborators or readers can search your library without installing the plugin. See [Public Web Interface](docs/public-web-interface.md).
- **No file uploads to third parties** — the plugin sends file bytes only to the backend you control. With a [local model preset](docs/presets.md), nothing ever leaves your machine.
- **Open source & self-hosted** — no subscription, no closed-source backend, no vendor lock-in. Deploy it once on infrastructure you control and own your data and model choices outright.

- **Multi-format** — indexes PDFs, HTML snapshots, EPUB, and DOCX attachments — not PDFs only.
- **Abstracts indexed automatically** — for items without a locally available attachment, the abstract is indexed so the item still shows up in relevant search results.
- **Attachment health tooling** — the built-in Fix Unavailable Attachments tool lists all items whose local file is missing (e.g. due to an incomplete sync) and attempts to recover them automatically through multiple strategies, keeping your index complete. See [Fix Unavailable Attachments](docs/fix-unavailable-attachments.md).
- **Automatic (scheduled) indexing, administered from the plugin** — enable a single toggle in Preferences to have the server keep your libraries indexed on a recurring schedule, with no need to keep Zotero open or index manually. Group admins can additionally pause, resume, trigger, or monitor indexing runs for the whole server entirely from the plugin UI — no shell or server access required. Server operators can alternatively drive the same indexer headlessly via script or cron. See [Automatic Indexing Setup](docs/auto-indexing-setup.md) (or [Cron / Headless Indexing](docs/cron-indexing.md) for the server-side script).
- **Fully configurable** — every step of the pipeline (chunking strategy, embedding model, LLM, retrieval parameters) is controlled through a preset in your `.env` file. Swap models or switch between local and remote inference without changing any code. See [Presets](docs/presets.md).

## Why THIS Zotero RAG?

Several other tools let you chat with a Zotero library — among them
[Beaver](https://github.com/jlegewie/beaver-zotero),
[PapersGPT](https://github.com/papersgpt/papersgpt-for-zotero),
[BibGenie](https://www.bibgenie.com/),
[Zotero-RAG-Assistant](https://github.com/aahepburn/Zotero-RAG-Assistant),
the Rust [zotero-rag](https://github.com/zotero-rag/zotero-rag) CLI,
[Zotero-ChatPDF](https://github.com/ljeagle/zotero-chatpdf), and various
[Zotero MCP servers](https://github.com/54yyyu/zotero-mcp). A full writeup
of how each compares is in [Zotero RAG Landscape](docs/comparison.md); the
short version:

Every alternative surveyed falls into one of two camps — either it has a
richly configurable RAG pipeline but is strictly single-user and local
(PapersGPT, Zotero-RAG-Assistant, the Rust CLI, Zotero-ChatPDF), or it
offers some shared/hosted backend but with a fixed pipeline and no
self-hostable multi-tenant option (Beaver, BibGenie). None combine a
**self-hosted server that authorizes many users against a Zotero group**
with **plugin-controlled administration** of that server's indexing (no
shell access needed) and a **fully swappable local-or-remote RAG
pipeline**. This project is built around exactly that combination: one
deployment, run once by whoever administers it, serving and configurable
by an entire research group from inside Zotero itself.

## Quick Start

### Install the dependencies

- [Install `uv`](https://docs.astral.sh/uv/getting-started/installation/) if you don't have it already.
- Install the python dependencies: `uv sync`
- Install a recent version of NodeJS (It's strictly only necessary for development)

For presets that run embedding models locally (`apple-silicon-32gb`, `high-memory`, `cpu-only`) you also need:

```bash
uv sync --extra local-models
```

Remote presets (`remote-kisski`, `remote-openai`, etc.) do **not** require these packages — see [docs/presets.md](docs/presets.md) for the full comparison.

### 2. Configure a Preset

Copy `.env.dist` to `.env` and set `MODEL_PRESET`:

```bash
# Recommended — fully remote, no local GPU or heavy dependencies
MODEL_PRESET=remote-openai       # requires OPENAI_API_KEY

# Local inference (requires uv sync --extra local-models)
MODEL_PRESET=apple-silicon-32gb  # Apple Silicon Mac, 32 GB RAM
MODEL_PRESET=cpu-only            # CPU only / low memory, will have bad performance
```

See [docs/presets.md](docs/presets.md) for all presets and a dependency overview.

### 3. Start the Backend Server

The plugin requires a locally or remotely deployed server to process your questions. The server URL is configured in the plugin's Preferences pane (`http://localhost:8119` by default). When using a remote server, set an API key there and enter it in the plugin preferences.

**Option A — direct (development):**

```bash
# with NodeJS:
npm run server:start
# without NodeJS:
uv run python scripts/server.py start
```

Locally, the server will run at <http://localhost:8119>. You can check if it's running:

```bash
npm run server:status
curl http://localhost:8119/health
```

To stop the server:

```bash
npm run server:stop
```

**Option B — Docker container:**

```bash
# Build image and start container (requires Docker or Podman)
node bin/container.mjs start --data-dir ./data

# Or with a deployment env file (for servers, works only with Podman):
node bin/deploy.mjs .env.deploy.example
```

See [docs/container-deployment.md](docs/container-deployment.md) for full Docker setup, including remote server deployment with nginx and SSL.

### 4. Install the Plugin in Zotero

1. Download the `zotero-rag-X.Y.Z.xpi` file from <https://github.com/cboulanger/zotero-rag/releases/latest>
2. Open Zotero
3. Go to **Tools → Add-ons**
4. Click the gear icon and select **Install Add-on From File**
5. Select the downloaded `.xpi` file
6. The plugin will be installed and will auto-update if so configured.

### 5. Configure the Plugin for a Remote Server

The first time the plugin connects to a backend it isn't already configured for, a setup wizard walks you through three steps. You can also revisit these settings any time in **Zotero → Settings → Zotero RAG**:

- **Server URL** — the full URL of the remote server (e.g. `https://rag.example.com`)
- **Zotero API Key** — your own personal key from <https://www.zotero.org/settings/keys>- Do not reuse an existing key, generate a new key, which **must** be read-only and should only cover the libraries that you want to query. A local server (`localhost`) needs none of this and skips straight past it; a remote server uses this key to identify you and check that you're authorized to use that instance (e.g. membership in a designated Zotero group). The Preferences pane shows a live status (username + accessible library count) once a valid key is entered.
- **Service API Keys** — if the backend preset uses a remote LLM or embedding service (e.g. OpenAI, KISSKI), enter the corresponding API key here so the plugin can pass it to the server

### 6. Using the Plugin

<img src="./docs/images/dialog.png" width="300" alt="Screenshot of the RAG dialog">

Once installed:

1. Open your Zotero library
2. Select a library (user or group)
3. Open the "Tools" menu and then click on the "Zotero RAG" menu item
4. In the dialog, the current library will be pre-selected, but you can add additional ones to search (this works only if all of them have already been indexed)
5. If the library index is outdated or it has not been indexed yet, you will not be able to ask a question on this library. Indexing might take from minutes to days depending on the size of the library and the number of unindexed items. Configure [server-side auto-indexing](docs/auto-indexing-setup.md) to do this automatically and without having to keep Zotero running. From then on, you only need to index new and changed items in your library. If you have enabled auto-indexing on the server, you only have to index what you added since the last run. 
6. Once indexed, you can ask questions that can be answered by the PDF documents contained in the selected libraries. The plugin will search through your documents and provide answers with source citations.

The plugin uses AI to understand your questions and retrieve relevant information from your Zotero library, making it easy to find insights across multiple papers. Answers are backed by transparent [query routing](docs/query-routing.md) between semantic and metadata search, and can optionally be saved as a note:

<img src="./docs/images/note.png" width="300" alt="Screenshot of a result note">

The plugin automatically creates a **RAG Results** saved search in your library the first time a result note or indexing report is generated, collecting every note tagged `RAG Query Result` or `RAG Indexing Report` in one place.

From here, see [User Documentation](#user-documentation) below for automatic indexing, the public web interface, and the attachment-repair tool.

## Versioning and Release Policy

The project uses [Semantic Release](https://github.com/semantic-release/semantic-release) workflow, which relies on [Semantic Versioning](https://semver.org/) principles. This determines the version number. The major version increases each time a backwards-incompatible change is being merged in terms of the frontend (plugin client) and backend (server) communication. All backend and frontend instances with the same major version number should be compatible and will handle missing new features gracefully.

Exception: version 1.x.y is beta, anything can change anytime. v2.0.0 will be the first stable release.

## User Documentation

- **[Automatic (scheduled) indexing setup](docs/auto-indexing-setup.md)**
- **[Public web interface](docs/public-web-interface.md)**
- **[Fix unavailable attachments](docs/fix-unavailable-attachments.md)**

## Developer Documentation

- **[Application architecture](docs/architecture.md)**
- **[Query routing & agent system](docs/query-routing.md)**
- **[Debugging RAG queries](docs/debugging-rag-queries.md)**
- **[Plugin development & hot reload](https://github.com/cboulanger/zotero-skills/)**
- **[Testing Guide](docs/testing.md)**
- **[CLI commands](docs/cli.md)**
- **[Setup CI/CD](docs/setup-ci-cd.md)**
- **[Resources for coding agents](docs/agents.md)**
- **[Cron / headless indexing](docs/cron-indexing.md)**
- **[Import from OpenAlex](docs/openalex-import.md)**

## License

The code has been generated by Claude Code with an prompts and guidance by @cboulanger (documented [here](./docs/history/)). It is therefore in the Public Domain as far as the code is fully machine-generated, otherwise it is licensed under Mozilla Public License (MPL) version 2.0.
