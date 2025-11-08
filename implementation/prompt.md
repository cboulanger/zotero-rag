# Implementation prompt

Create a plan for implementing the following application:

A Zotero-based app that uses the metadata and the PDF attachments of one or more zotero libraries as a resource for a RAG system that you can ask questions. The app consist of a FastAPI backend and a Zotero plugin.

The backend leverages a local open weight model (depending on the hardware capabilities) and handles embedding, the vector database, and exposes an API for data ingestion and querying, following current best practices. 

The zotero plugin should add a menu item under "Tools". It shows a dialog where the user can ask a question that should be answered about the content of the currrently selected library. The answer will be added as a standalone note item in the collection that is currently selected. The dialog consists of a text input where the user can enter the question, and a button which allows to choose the libraries to include into the search in a dropdown with a checkbox-style list (default is currently selected library). If a library has not been indexed in the vector database, show a non-blocking progress bar which advances as the backend is working, this could be implemented using a SSE endpoint at the backend. 

Create a master implementation plan (`implementation/master.md`) which gives a high-level plan how to implement this plugin/backend architecture. In particular, specify what information and dependencies are required to implement it (i.e. Zotero API details, RAG and LLM libraries), what steps are necesary, and what the open questions are. If you need to consult specifications on APIs that cannot reliable retrieve from your knowledge, read them from the web and summarize relevant information in documents in the `implementation` folder for reference during implementation, linked from the master plan. 

Further guidelines:
- you have Python 3.13 using `uv` and Node 23 to work with. Always use `uv` and the virtual environment for python commands at the command line.
- omit information on implementation times and any other information that is not strictly needed for your coding 
- organize the code in reusable modules to keep the main business logic clean and lightweight
- always add tests for the library methods using the nodejs and python built-in unit test libraries
- Information on how to create a zotero plugin are in `zotero-sample-plugin/README.md` - we only need to support Zotero 7  and the 2.0 version of the plugin architecture
- Information on client coding is here: https://www.zotero.org/support/dev/client_coding
- target Zotero v8 (https://www.zotero.org/support/dev/zotero_8_for_developers)
- Queries to the Zotero API from the backend can be made using the local API endpoint
- You can have a look at https://github.com/krishanr/zoterorag/blob/main/zotero_rag.ipynb if that already provides some reusable impelementation parts
- the backend should be as modular as possible, so that it should be possible to pick a local LLM from huggingface as well a remote inference endpoint using an API key. 
- Put general coding rules, which are unrelated to the application as such, i.e. on using the virtual environment or on testing, into `CLAUDE.md`, not in the master plan. 
- If there are unresolved implementation questions or information items neccessary to solve them, add these to the master plan as checkbox items to be resolved.

