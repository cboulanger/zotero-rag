# Zotero RAG Plugin

A Zotero plugin that enables question-answering over your Zotero library using Retrieval-Augmented Generation (RAG).

## Features

- **Ask Questions**: Query your Zotero library using natural language
- **Multi-Library Search**: Search across multiple libraries simultaneously
- **Automatic Indexing**: Automatically indexes new libraries in the background
- **Progress Tracking**: Real-time progress updates during library indexing
- **Smart Citations**: Generated answers include links to source PDFs with page numbers
- **Note Integration**: Creates formatted notes in your current collection

## Installation

### Prerequisites

1. Zotero 7 or later
2. FastAPI backend server running (see [backend documentation](../backend/README.md))

### Installing the Plugin

1. Build the plugin:
   ```bash
   npm run plugin:build
   ```

2. In Zotero:
   - Go to **Tools > Add-ons**
   - Click the gear icon and select **Install Add-on From File**
   - Select `plugin/dist/zotero-rag-x.x.x.xpi`

3. Restart Zotero

## Configuration

### Backend URL

Configure the backend server URL in Zotero preferences:

1. Go to **Edit > Preferences** (or **Zotero > Preferences** on macOS)
2. Select the **Zotero RAG** tab
3. Enter your backend URL (default: `http://localhost:8119`)

### Performance Settings

- **Max Concurrent Queries**: Limit simultaneous queries (default: 5)

## Usage

### Asking Questions

1. Go to **Tools > Ask Question...**
2. Enter your question in the text area
3. Select the libraries you want to search
4. Click **Submit**

The plugin will:
- Check if selected libraries are indexed (and index them if needed)
- Show progress for any indexing operations
- Submit your query to the backend
- Create a note with the answer and source citations

### Note Format

Generated notes include:
- **Question**: Your original question
- **Answer**: The generated response
- **Sources**: Links to source PDFs with page numbers (when available)
- **Metadata**: Timestamp and libraries searched

### Citations

Citations are formatted as Zotero links:
- Click on a source link to jump to the source document in your library
- Page numbers are included when available (e.g., "Source, p. 42")
- Text anchors are shown when page numbers are unavailable

## Development

### Building from Source

```bash
# Build the plugin
npm run plugin:build

# The XPI file will be created in plugin/dist/
```

### Project Structure

```
plugin/
├── src/
│   ├── bootstrap.js      # Plugin lifecycle hooks
│   ├── zotero-rag.js     # Main plugin logic
│   ├── dialog.xhtml      # Query dialog UI
│   ├── dialog.js         # Dialog logic
│   ├── preferences.xhtml # Preferences UI
│   └── preferences.js    # Preferences logic
├── locale/
│   └── en-US/
│       └── zotero-rag.ftl # Localization strings
├── manifest.json         # Plugin manifest
└── README.md
```

## Troubleshooting

### Backend Not Available

If you see "Backend server is not available" errors:
1. Ensure the FastAPI backend is running (`npm run server:start`)
2. Check that the backend URL in preferences is correct
3. Verify the backend is accessible at the configured URL

### Indexing Errors

If library indexing fails:
1. Check that Zotero's local API is enabled (it should be by default)
2. Ensure PDFs are attached to items in your library
3. Check backend logs for detailed error messages

### No Results

If queries return no results:
- Verify that your libraries have been indexed successfully
- Try searching with different keywords
- Check that items in your library have PDF attachments with extractable text

## Architecture

The plugin communicates with the FastAPI backend via:
- **REST API**: For submitting queries and managing configuration
- **Server-Sent Events (SSE)**: For real-time indexing progress updates

## License

ISC

## Support

For issues and feature requests, please visit: https://github.com/yourusername/zotero-rag/issues
