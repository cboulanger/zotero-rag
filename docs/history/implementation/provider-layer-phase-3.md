# Provider layer - Phase 3 (credential scopes, no provider keys in the environment)

Part of PR 2 of `docs/superpowers/plans/2026-10-10-provider-layer.md` (phases 3-4).

## Done

- 3.1 Scope vs key fields: `get_providers` rejects a side whose `model_kwargs` contradict the scope (`user` with `shared_api_key_env`, `managed`/`shared` with `api_key_env`). The plan's `Provider.key_requirements()` was not added: `apply_defaults()` already derives the key kind from the scope and the services keep reading `model_kwargs`, so validation is enough.
- 3.2 No provider keys from the environment: `resolve_shared_value`, the embedding and LLM services, `GET /api/config` credential checks, `required-keys` and the model-status route read only the request, the key store and the encrypted admin store. `HF_TOKEN` for local model weights stays an environment setting. Docs (`presets.md`, `architecture.md`, `cli.md`, `README.md`, `.env.dist`) describe the new rule; `scripts/` keep their own CLI/env inputs.
- 3.3 Per-key endpoint lookup: `Provider.derives_endpoint_url` + `endpoint_url(key)`; `RunPodProvider` finds `zotero-rag-<side>` in the key owner's account; `backend/services/endpoint_cache.py` caches per `(provider, side, key fingerprint)` (found 300 s, missing 15 s), invalidated by provisioning and on connection failure. The services look the endpoint up off the event loop (`_ensure_endpoint`) when the preset has no URL.
- 3.4 Caller-scoped health and provisioning: `GET /api/config/health` uses the caller's header key for `user` scope; `POST /api/config/provision` lets any signed-in user provision on their own key (401 without identity), admins only for `managed`; job slots are per user for `user` scope and global for `managed`; `GET /api/config/provision/status` returns the caller's slot. The bundled `runpod.json` is now scope `user` with `api_key_env`; the institution-funded variant is documented in `presets.md` and built for tests by `backend/tests/runpod_variants.py`.
- 3.5 Key store: each user's provider keys are stored per key name (`embedding_keys`), with their own status and rate-limit state; the resolver picks the key the active preset names, reports a missing one, and keeps the others. No migration of the old single-key shape: after upgrade a user's first auto-index run reports "No <KEY> configured" and they re-enter the key. `bin/debug_get_zotero_key.py --key-name` selects a stored key.

## Verification

- Backend suite: all passing (Kreuzberg extractor file excluded: it allocates 10 GB).
- Not run here: container smoke test (`uv run pytest -m container -v -s`, settings changed) - no container daemon in the authoring sandbox.

## Not done

- Plugin UI for provisioning by non-admins and for per-side sections is Phase 5/6.
- Hugging Face provider (`derives_endpoint_url`) is Phase 7.
