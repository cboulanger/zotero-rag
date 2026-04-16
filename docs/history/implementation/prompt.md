# Implementation

## Initial prompt

Create a plan for implementing the following application:

A Zotero-based app that uses the metadata and the PDF attachments of one or more zotero libraries as a resource for a RAG system that you can ask questions. The app consist of a FastAPI backend and a Zotero plugin.

The backend leverages a local open weight model (depending on the hardware capabilities) and handles embedding, the vector database, and exposes an API for data ingestion and querying, following current best practices.

The zotero plugin should add a menu item under "Tools". It shows a dialog where the user can ask a question that should be answered about the content of the currrently selected library. The answer will be added as a standalone note item in the collection that is currently selected. The dialog consists of a text input where the user can enter the question, and a button which allows to choose the libraries to include into the search in a dropdown with a checkbox-style list (default is currently selected library). If a library has not been indexed in the vector database, show a non-blocking progress bar which advances as the backend is working, this could be implemented using a SSE endpoint at the backend.

Create a master implementation plan (`implementation/master.md`) which gives a high-level plan how to implement this plugin/backend architecture. In particular, specify what information and dependencies are required to implement it (i.e. Zotero API details, RAG and LLM libraries), what steps are necesary, and what the open questions are. If you need to consult specifications on APIs that cannot reliable retrieve from your knowledge, read them from the web and summarize relevant information in documents in the `implementation` folder for reference during implementation, linked from the master plan.

Further guidelines:

- you have Python 3.12 using `uv` and Node 23 to work with. Always use `uv` and the virtual environment for python commands at the command line.
- omit information on implementation times and any other information that is not strictly needed for your coding
- organize the code in reusable modules to keep the main business logic clean and lightweight
- always add tests for the library methods using the nodejs and python built-in unit test libraries
- Information on how to create a zotero plugin are in `zotero-sample-plugin/README.md` - we only need to support Zotero 7  and the 2.0 version of the plugin architecture
- Information on client coding is here: <https://www.zotero.org/support/dev/client_coding>
- target Zotero v8 (<https://www.zotero.org/support/dev/zotero_8_for_developers>)
- Queries to the Zotero API from the backend can be made using the local API endpoint
- You can have a look at <https://github.com/krishanr/zoterorag/blob/main/zotero_rag.ipynb> if that already provides some reusable impelementation parts
- the backend should be as modular as possible, so that it should be possible to pick a local LLM from huggingface as well a remote inference endpoint using an API key.
- Put general coding rules, which are unrelated to the application as such, i.e. on using the virtual environment or on testing, into `CLAUDE.md`, not in the master plan.
- If there are unresolved implementation questions or information items neccessary to solve them, add these to the master plan as checkbox items to be resolved.

## Update 1: Answers to the open questions

- **Model Selection Strategy**: should be by user configuration. Provide (extensible) named default configurations that can be chosen from for a variety of scenarios, one of which an MacMini M4 with 16GB RAM. It must be possible to store the model weights at an abitrary location (external SSD).
  
- **Vector Database Storage**: Default to in-memory or persistent? Where to store persistent data?
  -> Persistent by default in user data directory, with configuration option for location

- **Chunking Strategy**: What chunk size and overlap for academic papers? -> Can chunking be done on the paragraph or even sentence level?

- **Authentication**: Assume local-only trusted access through Zotero, API Token security can be added later.

- **Multi-library Merging**: How to handle duplicate documents across libraries?
-> Zotero metadata contains a "relations" property, which is set when items are copied between libraries

```
        "relations": {
            "owl:sameAs": "http://zotero.org/groups/36222/items/E6IGUT5Z"
        },
```

Deduplication can also be done via content hashing

- **Caching**: Should we cache embeddings to avoid re-computing on re-indexing? ->Yes, cache based on document hash

### Plugin

- **Backend Discovery**: How does plugin find backend? Hardcoded localhost:8000 or configurable?
-> Configurable in plugin preferences, default localhost:8119

- **Concurrent Queries**: Allow multiple simultaneous queries or queue them? -> Allow concurrent, limit to reasonable number (3-5)

- **Note Format**: Plain text, Markdown, or HTML notes? Include citations?
-> Notes are stored as HTML. Links to items can  be done using `<a href="zotero://select/library/items/BLLKX4YA">`

- **Progress Granularity**: What level of progress detail? Per-document, per-batch, percentage only? -> Percentage with current document count

- **Offline Behavior**: What happens if backend is unreachable? -> Fail with clear error message, don't queue

- **Zotero 8 Compatibility**: Are there breaking changes from Zotero 7 to 8 we need to handle?
-> if Zotero 8 provides an API that makes it easier to code something, use that. The Plugin can only be used on machines that allow user-installed software so we don't need to have BC

### Cross-Cutting

- **Data Privacy**: Should we include privacy/data retention policies? Is data stored beyond vector DB?
-> Document that all data stays local, PDF are published documents

- **Logging**: What level of logging? User-accessible logs? -> Configurable log level, logs to standard locations

- **Updates**: How to handle backend/plugin version mismatches? -> Version check API endpoint, warn user

## Update 2

### Implementation Details

- **Mac Mini M4 16GB Preset**: Which specific models (embedding + LLM) should be recommended for this configuration? -> make an informed decision, can be optimized later

- **Semantic Chunking Implementation**: Which library to use for paragraph/sentence segmentation? -> spaCy

- **Citation Extraction**: Should we track which specific chunks contributed to the answer for citation links? -> We only need to link the PDFs which are the source for a given part of the answer. No need to further extract references from those PDFs. Linking to the specific part of the text would ideally be done with page number but I don't know if that is possible during chunking. Otherwise, would the first 5 words of the chunk work as an anchor that can be used to find the chunk later?

- **Multimodal Support**: Should we include image/figure processing from PDFs in initial implementation? -> no, just text
