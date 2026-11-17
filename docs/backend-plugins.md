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
| `kisski` | `inference.provider:kisski`, `presets:kisski` | KISSKI / SAIA (GWDG Academic Cloud). |
| `mpcdf` | `inference.provider:mpcdf`, `presets:mpcdf` | MPCDF LLM Inference Service; opens the per-user sessions at startup and closes them at shutdown. Needs the optional `mpcdf-llm` library. |
| `runpod` | `inference.provider:runpod`, `presets:runpod` | RunPod serverless endpoints. |
| `huggingface` | `inference.provider:huggingface`, `presets:huggingface` | Hugging Face Inference Endpoints. |
| `google` | `inference.provider:google`, `presets:google` | Google Gemini. |
| `openai` | `inference.provider:openai`, `presets:openai` | OpenAI. |
| `anthropic` | `inference.provider:anthropic` | Anthropic (answering side only). |
| `local_torch` | `inference.local:torch:embedding`, `torch:llm`, `torch:rerank` | In-process inference with torch (sentence-transformers, transformers); CPU, CUDA or Apple MPS. Needs the `local-models` extra. |

Each vendor plugin also ships the bundled presets that need its provider (its
`presets` capability); core ships only the provider-free presets (`cpu-only`,
`high-memory`, `apple-silicon-32gb`). Removing or disabling a plugin therefore
also removes its presets.

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

### `presets`

A `PresetDirectory(path=<directory>)` adds the `*.json` presets in that directory
to the bundled presets, which are seeded into `<data_path>/presets` at startup
like core's own (see [Presets](presets.md)). A preset belongs to the plugin whose
provider it needs. A name that core or an earlier plugin already ships is skipped
with a warning.

```python
PresetDirectory(id="acme", path=Path(__file__).parent / "presets")
```

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

## Tests

A plugin's tests ship with the plugin, in `<plugin>/tests/` (a package with
`test_*.py` files); core tests in `backend/tests` never test a plugin's own code.
`uv run pytest` collects `backend/tests` and every shipped plugin's tests, and a
core test checks that each shipped plugin has a `tests` package.

Core supplies what plugin tests need, so they import nothing from `backend/tests`:

- `backend.providers.testing`: `FakeHTTP`, `FakeResponse` and `DeadHTTP` (stand-ins
  for a provider's management-API client), `make_preset()` and `preset_with()`
  (minimal presets), and `ProviderContractSuite` with `ContractFixture`, the
  contract every provider must pass. A plugin that registers a provider runs it
  in `tests/test_contract.py`:

  ```python
  class TestContract(ProviderContractSuite, unittest.TestCase):
      fixture = ContractFixture(provider_id="acme", model_kwargs={"api_key_env": "ACME_API_KEY"})
  ```

- `backend.pytest_plugin`: the test environment (testing mode, a throwaway
  `AUTOINDEX_SECRET`, `.env.test`) and shared fixtures. Shipped plugins get it
  through `backend/conftest.py`; run an external plugin's tests with
  `uv run pytest -p backend.pytest_plugin /path/to/my_plugins/acme/tests`. The
  host imports an external plugin as `zotero_rag_plugins.<directory>`, so in its
  tests look registered classes up through the registry (`PROVIDERS["acme"]`)
  rather than comparing them with a separately imported class. It also skips
  the tests of a shipped plugin that `DISABLED_BACKEND_PLUGINS` turns off.

Core tests never use a shipped plugin. They run on a test-only plugin,
`backend/tests/fake_plugins/testvendor`, which `backend/tests/conftest.py` adds to
`BACKEND_PLUGIN_DIRS`: fake providers in the shapes of real vendors (`testvendor`
free with per-user keys and quotas, `testother` paid, `testmessages` on the
Anthropic protocol, `testsession` with per-user login sessions, `testcloud`
with provisioned endpoints) and presets that combine them (`test-remote` is the
core tests' default preset). `backend/tests/fakes.py` has the helpers. CI runs
`backend/tests` a second time with every shipped plugin disabled, so core keeps
working without any of them:

```bash
DISABLED_BACKEND_PLUGINS=kisski,mpcdf,runpod,huggingface,google,openai,anthropic,local_torch uv run pytest backend/tests
```

## Status routes

- `GET /status` lists `plugins` (id, name, version, status `loaded`, `disabled`
  or `failed`, source directory, error, capability ids), every registered
  capability with whether it is available on this machine (and why not), and the
  plugin directories searched.
- `GET /sides` lists the default preset's sides; each entry's `served_by` names
  the capability and plugin serving it.
