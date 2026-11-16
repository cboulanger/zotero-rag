# Backend plugins

Everything vendor- or platform-specific that the backend can use for inference
comes from **backend plugins**. A plugin contributes one or more
**capabilities**; core asks the plugin host for capabilities of a kind and uses
them through that kind's interface, without importing any plugin module. A plugin
can therefore be added, removed or disabled without touching core.

Plugin authors use `backend.pluginhost` (plugin and capability types) and, for
inference providers, `backend.providers` (the `Provider` base class and its types).

## Where plugins live

The plugin host loads plugins once per process, from these directories in order:

1. `backend/plugins/`, the plugins shipped with the project;
2. every directory in `BACKEND_PLUGIN_DIRS`, separated by `:` (`;` on Windows).

Each subdirectory with an `__init__.py` is one plugin (names starting with `_`
or `.` are skipped). `DISABLED_BACKEND_PLUGINS` lists plugin ids (comma-separated)
that are not loaded; a disabled plugin is still listed by `GET /status`.

A plugin that fails to import or register, or whose plugin id or capability id
is already taken, is skipped, logged and listed by `GET /status` with its error.
It never stops the backend. A preset that needs a capability no loaded plugin
offers is unavailable, and its error names the missing provider or engine.

## Shipped plugins

| Plugin | Capabilities | Notes |
|---|---|---|
| `kisski` | `inference.provider:kisski` | KISSKI / SAIA (GWDG Academic Cloud). |
| `mpcdf` | `inference.provider:mpcdf` | MPCDF LLM Inference Service; opens the per-user sessions at startup and closes them at shutdown. Needs the optional `mpcdf-llm` library. |
| `runpod` | `inference.provider:runpod` | RunPod serverless endpoints. |
| `huggingface` | `inference.provider:huggingface` | Hugging Face Inference Endpoints. |
| `google` | `inference.provider:google` | Google Gemini. |
| `openai` | `inference.provider:openai` | OpenAI. |
| `anthropic` | `inference.provider:anthropic` | Anthropic (answering side only). |
| `local_torch` | `inference.local:torch:embedding`, `torch:llm`, `torch:rerank` | In-process inference with torch (sentence-transformers, transformers); CPU, CUDA or Apple MPS. Needs the `local-models` extra. |

The generic provider for any OpenAI-compatible endpoint (a local Ollama,
llama.cpp or vLLM server as much as a remote gateway) is part of core and listed
as the built-in plugin `core`.

`kisski` and `mpcdf` are tied to one institutional context. Since core never
imports them, either can move to its own repository and be installed as an
external plugin directory without code changes.

## Capability kinds

### `inference.provider`

An `InferenceProvider(provider=<Provider subclass>)` makes the class selectable
in presets by its `id` (`"provider": {"id": "<id>"}` on a side). The sides it
serves are its `supported_sides`. See [Providers](providers.md) for the provider
contract.

### `inference.local`

A `LocalInference` runs one side of a preset section whose `model_type` is
`local` inside the backend process, without a separate inference server:

```python
LocalInference(
    side="embedding",            # "embedding", "llm" or "rerank"
    engine="mlx",                # the name a preset uses: "engine": "mlx"
    create=make_service,         # builds the side's service (signatures below)
    requires=("mlx_embeddings",),  # importable modules it needs
    platforms=("darwin",),       # sys.platform values; empty means any
    priority=10,                 # higher wins when a preset names no engine
)
```

`create` receives, by side:

- `embedding`: `create(config, cache_dir=None, hf_token=None)` returning an `EmbeddingService`;
- `llm`: `create(settings, cache_dir=None, hf_token=None, model_name_override=None)` returning an `LLMService`;
- `rerank`: `create(config)` returning a `Reranker`.

A local side may name its engine (`"engine": "torch"`). Without one, the best
available engine on this machine is used: available engines before unavailable
ones, then by `priority`. When none is available the highest-priority one is used
anyway, so its own error (for example a missing package) reaches the user.

Local inference through a separate server needs no plugin: point a `remote` side
at its OpenAI-compatible URL with the `generic` provider.

### New kinds

A new kind is a `Capability` subclass with its own `kind` string, plus the core
code that looks it up with `get_plugin_host().capabilities("<kind>")`. The host
stores and lists capabilities of any kind, so the loader does not change.

## Writing a plugin

```text
my_plugins/
  acme/
    __init__.py      # defines register()
    provider.py
```

```python
# my_plugins/acme/__init__.py
from backend.pluginhost import BackendPlugin, InferenceProvider

from .provider import AcmeProvider


async def _startup() -> None:   # optional: runs when the API server starts
    ...


def register() -> BackendPlugin:
    return BackendPlugin(
        id="acme",
        name="Acme inference",
        version="1.0",
        description="Acme's hosted models.",
        capabilities=[InferenceProvider(provider=AcmeProvider)],
        on_startup=_startup,     # optional; on_shutdown likewise
    )
```

Then set `BACKEND_PLUGIN_DIRS=/path/to/my_plugins` and restart the backend.
External plugins are imported as `zotero_rag_plugins.<directory>`, so relative
imports inside the plugin work. `register()` may decide which capabilities to
offer (for example only those that can run on this platform). Startup and
shutdown hooks run only in the API server, not in command-line tools, and a
failing hook is logged without stopping the server.

## Status routes

- `GET /status` lists `plugins` (id, name, version, status `loaded`, `disabled`
  or `failed`, source directory, error, capability ids), every registered
  capability with whether it is available on this machine (and why not), and the
  plugin directories searched.
- `GET /sides` lists the default preset's sides; each entry's `served_by` names
  the capability and plugin serving it.
