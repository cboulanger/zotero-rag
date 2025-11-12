# Zotero RAG Plugin

A Zotero plugin that enables semantic search and question-answering over your research library using Retrieval-Augmented Generation (RAG).

## Quick Start

### 1. Install Plugin

```bash
# Build the plugin XPI
npm run plugin:build

# In Zotero:
# Tools → Add-ons → Gear icon → Install Add-on From File
# Select: plugin/dist/zotero-rag-0.1.0.xpi
# Restart Zotero
```

### 2. Start Backend Server

```bash
# From project root
npm run server:start

# Server starts at http://localhost:8119
```

### 3. Configure Plugin (Optional)

In Zotero: **Edit → Preferences → Zotero RAG**
- Backend URL: `http://localhost:8119` (default)
- Max Concurrent Queries: `5` (default)

### 4. Ask a Question

1. **Tools → Ask Question...**
2. Enter your question
3. Select libraries to search
4. Click **Submit**

The plugin will:
- Index selected libraries (if needed) with real-time progress
- Query the indexed content using RAG
- Create a note in your collection with:
  - The generated answer
  - Citations linking to source PDFs (with page numbers)
  - Metadata (timestamp, libraries searched)

## Features

- **Natural Language Queries**: Ask questions about your research in plain English
- **Multi-Library Search**: Query across multiple Zotero libraries simultaneously
- **Automatic Indexing**: Background indexing with real-time progress tracking
- **Smart Citations**: Answers include clickable Zotero links to source PDFs with page numbers
- **Note Integration**: Results saved as formatted notes in your current collection

## How It Works

1. **Indexing**: PDFs are extracted, chunked semantically, embedded, and stored in a vector database
2. **Querying**: Your question is embedded and used to retrieve relevant text chunks
3. **Generation**: An LLM generates an answer based on retrieved context and cites sources
4. **Note Creation**: The answer and citations are formatted as an HTML note in Zotero

## Requirements

- **Zotero**: Version 7 or later
- **Backend Server**: FastAPI server must be running (see [docs/cli.md](../docs/cli.md))
- **PDFs**: Items in your library must have PDF attachments with extractable text

## Configuration

### Hardware Presets

Configure in `.env` file (project root):

```bash
MODEL_PRESET=mac-mini-m4-16gb  # Or: cpu-only, gpu-high-memory, remote-openai, remote-kisski
```

See [docs/architecture.md](../docs/architecture.md#configuration-system) for preset details.

### API Keys (for remote presets)

```bash
# In .env file
KISSKI_API_KEY=your_key_here      # For remote-kisski preset
OPENAI_API_KEY=sk-...             # For remote-openai preset
```

## Troubleshooting

| Problem | Solution |
|---------|----------|
| "Backend server is not available" | Start server: `npm run server:start` |
| "No results found" | Verify libraries are indexed and contain PDFs with text |
| Indexing fails | Check Zotero is running, PDFs are attached to items |
| Plugin doesn't appear | Restart Zotero after installation |

**Check server status:**
```bash
npm run server:status
curl http://localhost:8119/health
```

## Development

### Project Structure

```
plugin/
├── src/
│   ├── bootstrap.js         # Plugin lifecycle
│   ├── zotero-rag.js        # Main logic (HTTP, SSE, note creation)
│   ├── dialog.xhtml/js      # Query dialog UI
│   ├── preferences.xhtml/js # Settings UI
│   └── *.css                # Styling
├── locale/en-US/            # Localization
├── manifest.json            # Plugin metadata
└── dist/                    # Build output (XPI)
```

### Build

```bash
npm run plugin:build  # Creates plugin/dist/zotero-rag-{version}.xpi
```

### Plugin Architecture

- **HTML5 UI**: Modern HTML/CSS (no XUL dependency)
- **REST API**: Query submission, library status
- **SSE Streaming**: Real-time indexing progress
- **Note Formatting**: HTML with Zotero item links

## Documentation

- [Architecture Documentation](../docs/architecture.md) - System design and components
- [CLI Commands](../docs/cli.md) - Server management and testing
- [Testing Guide](../docs/testing.md) - Unit and integration tests
- [Implementation Progress](../implementation/master.md) - Development status

## License

ISC
