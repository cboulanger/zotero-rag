# Plugin Settings Reference

A complete reference for every setting exposed by the Zotero RAG plugin — both the
persistent **Preferences** pane (`Zotero → Settings → Zotero RAG`) and the
**per-query "Advanced options"** in the search dialog itself.

Most users only need to get through the first-run setup wizard (see the main
[README](../README.md#5-configure-the-plugin-for-a-remote-server)) and never touch
anything below. This page is for when you want to understand or change a specific
setting.

## Preferences pane

### Backend Server

| Setting | What it does |
|---|---|
| **Server URL** | The address of the backend the plugin talks to, e.g. `http://localhost:8119` for a local server or `https://rag.example.com` for a remote one. |
| **Zotero API Key** | Your personal [Zotero API key](https://www.zotero.org/settings/keys), used to authenticate you to a *remote* server and determine which libraries you're allowed to query. Not needed for a local (`localhost`) server — the field shows "Not required for a local server." in that case. A live status line confirms the key was accepted and shows how many libraries it can access. |
| **Run Setup Wizard…** | Re-opens the same three-step wizard shown on first connection to a new server, in case you need to reconfigure from scratch. |

### Model preset

**My preset** (**Preset** on a server running on your own machine) chooses the model set you use. Everyone starts on the server's default preset; you can switch to any other preset that works with the server's vector store. A preset that needs a key you have not entered is marked "needs a key": choosing it asks for the key first. A saved choice that stops being available is reported and you fall back to the default.

Below the chooser, one dot per side shows whether its service is ready (green), starting (yellow) or down (red). **Configure preset…** opens the dialog described in [Configure preset](#configure-preset).

### Automatic indexing

A single toggle: **Enable automatic indexing of my libraries**. When on, the backend
periodically re-indexes your libraries on its own schedule using the Zotero API key
configured above — you don't need to keep Zotero open or index manually. **View
indexing status** opens a dialog showing recent run results, the remaining embedding
rate limits (when the active embedding service reports them) and, for server admins,
a dropdown to switch the active preset. The same dialog opens from the **Indexing
status** button in the footer of the Ask dialog, which is shown only when the server
has auto-indexing configured. See
[Automatic Indexing Setup](auto-indexing-setup.md) for the full picture, including
what group admins can additionally control.

### Performance

| Setting | What it does |
|---|---|
| **Max Concurrent Queries** | Caps how many queries the plugin will run at the same time (1–10). Mainly relevant if you fire off several follow-up questions in quick succession. |

### Library Visibility

A checklist of every library the plugin knows about, controlling which ones appear
as options in the search dialog. Libraries the server auto-indexes are marked with
a clock icon. Use **Select all / none** to toggle everything at once. This only
affects what's *offered* in the dialog — it doesn't index or un-index anything.

### Data

**Clear local index cache** removes the local files that track which items have
already been indexed. Use this if the plugin thinks everything is up to date but
the backend reports no data (e.g. after the backend's index was wiped) — the next
indexing run will re-check every item against the backend from scratch.

## Configure preset

Opened with **Configure preset…** next to the preset chooser. Everything about how your preset's models run lives here.

**Header.** The preset's name and description, an overall status and three buttons that act on every side at once: **Start all** starts, wakes or resumes every endpoint that is not running, **Pause all** pauses every endpoint that can be paused (stops billing), and **Check now** checks again. Server admins also get **Server default**, which sets the preset everyone starts on.

**Tabs.** One tab per step of the pipeline, in the order a document and a question pass through it: **Extraction**, **Embedding**, **Retrieval** and **Answering (LLM)**. The dot on each tab shows whether the service behind it is ready (green), starting (yellow: being created, woken or resumed), down (red: not running, paused, out of capacity or missing credentials) or has no status check (grey). Each tab has the same three sections:

- **Configuration**: the settings of that step only. Credentials are not entered here but in the account cards below.
- **Status**: a summary line, messages (for example what to do while the endpoint is down), short facts such as the model, and **Show log** with the progress of the last start or pause.
- **Actions**: for example **Start endpoint**, **Resume**, **Retry**, **Wake up** or **Pause**. A disabled button says why.

**Service accounts.** The footer has one card per provider your preset uses, listing which tabs it serves. It holds that provider's keys: your own personal keys, which are stored in Zotero and sent with your requests (never stored on the server), and keys the server admin sets for everyone, which only admins can change. For providers whose endpoints you start yourself, an optional management key can be entered; it is kept in memory until Zotero quits and never saved. **Save and start** saves what you entered and starts the endpoints that use it.

### Retrieval tab

Advanced knobs controlling how broadly the backend searches when an answer looks
too narrow, and when it retries an answer that only cites one source despite
several being available. Leave any field blank to reset it to its default — most
users never need to change these.

| Setting | Default | What it does |
|---|---|---|
| **Diversity floor** | 3 | Minimum number of distinct documents a search must turn up before the backend is satisfied. Below this, it broadens the search automatically. |
| **Escalation factor** | 3 | When broadening a search (see above), the search size is multiplied by this factor. |
| **Escalation ceiling** | 30 | The largest a broadened search is allowed to grow to, keeping response time and cost bounded. |
| **Max passages per document** | 4 | Caps how many excerpts from a single document can be included in the material sent to the AI model, so one very large or finely-indexed document can't crowd out everything else. |
| **Single-source retry floor** | 3 | If at least this many distinct sources were found but the generated answer only cites one of them, the backend asks the model to double-check whether the others are relevant too and revise the answer once. |

These same values are recorded in a query's debug trace (see **Include debugging
information in note** below), so if an answer still looks too narrow or too broad
after adjusting a setting, the trace shows exactly what was used.

## Query dialog "Advanced options"

Collapsed by default in the search dialog, under the library list. These apply to
the query you're about to run, not saved as a persistent preference (with the
exception of the LLM model and routing choices, which are remembered for your next
query).

| Setting | What it does |
|---|---|
| **LLM to use for RAG** | Only shown when the backend preset offers more than one model. Picks which model generates the answer. |
| **Similarity** | How closely a passage must match your question to be considered — lower values cast a wider net but pull in less relevant material. |
| **Sources** | How many passages to retrieve before answering (this is `top_k` in the API; see also the [Retrieval tab](#retrieval-tab) of Configure preset) — higher values search more broadly but take longer and cost more per query. |
| **Disable query routing** | Skips the routing step that decides between content search, catalog search, and citation search, going straight to a content search. Faster, but loses metadata-filter extraction, catalog listings, and citation/mentions questions. See [Query Routing & Agent System](query-routing.md) for what routing normally does. |
| **Include debugging information in note** | Attaches the full execution trace (routing decision, retrieval scores, every LLM call and prompt) to the saved result note and makes it available for export from the result view. Useful when reporting an answer-quality issue. |

## See also

- [README: Configure the Plugin for a Remote Server](../README.md#5-configure-the-plugin-for-a-remote-server) — the first-run setup wizard
- [Query Routing & Agent System](query-routing.md) — how a question gets from the dialog to an answer
- [Automatic Indexing Setup](auto-indexing-setup.md)
- [Debugging RAG Queries](debugging-rag-queries.md) — for developers reproducing a reported answer-quality issue
