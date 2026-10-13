# Provider layer - Phase 4 (server default preset and per-user choice)

Part of PR 2 of `docs/superpowers/plans/2026-10-10-provider-layer.md`.

## Done

- 4.1 Default preset: `admin_settings.json` stores `default_preset` (replaces `active_preset_override`); resolution is stored value, then `MODEL_PRESET`, then the built-in `remote-kisski` (the `Settings.model_preset` default changed from `cpu-only`). `Settings.get_default_preset()` is the server default; `POST /api/config` and the compatibility rule operate on it. The strict compatibility rule (both sides remote, same embedding model by basename) moved to `backend/services/effective_preset.py` (`is_compatible`).
- 4.2 `backend/services/user_settings.py`: `<data_path>/system/user_settings.json`, atomic and file-locked, keyed by Zotero **user id** (stable across key rotation; the plan said "identity fingerprint").
- 4.3 Effective preset. Deliberate deviation from the plan's call-site sweep: instead of editing about 30 call sites, the auth middleware resolves the user's own preset once per request (`get_user_preset`) and binds it in a `ContextVar` (`use_preset`); `Settings.get_hardware_preset()` returns it, so all request-path code (services, threads started with `asyncio.to_thread`, tasks) follows it with no plumbing, while cron/CLI/startup see the default. Only a user's *own* preset is bound, so everybody else follows the live default (an admin switch takes effect inside the same request). Admin-level code that means the default calls `get_default_preset()`. `remote-fields` accepts the shared fields of any available preset. The cron indexer's resolver records each owner's effective preset in the target (`preset_name`), and `cron_indexer`/`bin/reindex_oversized_items.py` build the embedding service from it. The vector store stays a singleton on the default's embedding model; `is_compatible` is what keeps other embedding models out.
- 4.4 `GET`/`PUT /api/config/my-preset`; `GET /api/config` adds `default_preset`, `selectable_presets`, `preset_fell_back`. A choice needs usable credentials (user-scope key in the request header, or the shared key set); a loopback request without identity cannot choose (HTTP 400).

## Not done

- Plugin UI for choosing a preset (Phase 6.3).
- The `/api/config` `switchable_presets` (admin switch) keeps its shape.
