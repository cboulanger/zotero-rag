# Scripts

## Server & Development

| Script | Purpose |
| --- | --- |
| [server.py](server.py) | Start/stop/restart/status the FastAPI backend. `--dev` flag also launches the plugin dev server with hot-reload. Used by all `npm run server:*` and `npm start` commands. |
| [zotero_plugin.py](zotero_plugin.py) | Start/stop the plugin development server (zotero-plugin-scaffold), which controls a Zotero instance via Remote Debugging Protocol. Called by `npm run dev:plugin:*`. |
| [strip_ansi.py](strip_ansi.py) | Filter stdin → stdout, removing ANSI escape codes. Piped by `server.py` to keep plugin server log files clean. |

## Build & Release

| Script | Purpose |
| --- | --- |
| [build_plugin.py](build_plugin.py) | Package the plugin source into a distributable `.xpi` archive. Used by `npm run plugin:build` and the release pipeline (`.releaserc.json`). |
| [build_toolkit.js](build_toolkit.js) | Bundle Zotero Plugin Toolkit into a single JS file using esbuild. Used by `npm run plugin:build:toolkit`. |
| [version.py](version.py) | Synchronise version numbers across `package.json`, `pyproject.toml`, `plugin/src/manifest.json`, and `backend/__version__.py`. Used by the release pipeline. |

## Git Hooks

| Script | Purpose |
| --- | --- |
| [validate_commit_msg.py](validate_commit_msg.py) | Validate commit messages against the Conventional Commits spec. Installed as the `commit-msg` git hook. |
| [setup_hooks.py](setup_hooks.py) | Install the `commit-msg` hook into `.git/hooks/`. Run once when setting up a new local clone. |

## Diagnostics & Evaluation

| Script | Purpose |
| --- | --- |
| [test_kisski_api.py](test_kisski_api.py) | Verify KISSKI Chat-AI API key, endpoint reachability, available models, and chat completion. Run manually to diagnose remote-API connectivity issues. |
| [check_embedding_compat.py](check_embedding_compat.py) | Check whether two embedding presets produce compatible vectors (cosine similarity ≥ 0.999). Run before switching presets to confirm existing vectors remain usable. |
| [eval_embeddings.py](eval_embeddings.py) | Benchmark and compare embedding quality and encode throughput between two presets. Supports a built-in multilingual pair corpus, custom JSONL pairs, and published IR benchmarks (`--mteb-task scifact\|nfcorpus\|fiqa`). IR mode reports nDCG@10, MRR@10, Recall@{1,5,10}; requires `uv sync --extra eval` (installs `ir-datasets`). |

## Attic

One-off and debug scripts that are no longer part of the active workflow have been moved to [attic/](attic/).
