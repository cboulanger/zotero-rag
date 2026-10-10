# Provider layer - Phases 5 and 6 (provider descriptors and plugin UI)

Part of PR 3 of `docs/superpowers/plans/2026-10-10-provider-layer.md`.

## Done

- 5.1 `GET /api/config/providers`: one descriptor per side of the caller's effective preset (`model_type`, `provider` or `null` for a local side). `operable_by_caller` follows the credential scope: `user` any signed-in caller (or loopback), `managed` admins, `shared` nobody; no provider operable means false. `GET /api/required-keys` entries now carry `sides`.
- 5.2 Per-side job state, progress, retry-by-`sides` and deadlines were already in place from phases 2/3; added the deadline test.
- 6.1/6.2 `plugin/src/provider-sections.js` (no provider names; a test greps `plugin/src`): pure `buildModel`, a DOM layer built once and updated in place (a half-typed one-time key survives refreshes), and `createController` (refresh, poll, provision/retry of one side with the one-time key under the descriptor's credential name, inline 401/403/409/network errors, resume of a running job on opening the pane). `preferences.xhtml/js` render two sections and put each required key under the first side that uses it.
- 6.3 Preset group: `buildPresetControls` - "My preset" (PUT `/api/config/my-preset`, presets needing a key flagged and the key asked for before choosing), admin-only "Server default" (POST `/api/config`), and a single control on a loopback server. `GET /api/config/my-preset` now reports `loopback`/`is_admin`; `selectable_presets` lists every compatible preset with `credentials` and `missing_keys`, counting only the caller's own header keys (never other users' stored keys). The old `provisionable` field and the fixed provisioning rows are gone.
- 6.4 Setup wizard: the key step states which preset the user starts on, asks only for the keys it needs and mentions the optional upgrade (`keysIntro`).
- Docs: `plugin-settings.md`, `presets.md`.

## Not done

- 6.5 manual verification in the dev Zotero (hot-reloading plugin, `groups/6297749` only): switch the live backend between `remote-kisski` and a mixed preset and check sections, usage bars and the preset group. `initPrefPane` itself (DOM wiring) has no automated test; the logic it uses is covered by `provider-sections.test.js`.
- Pause/Resume buttons and the suspend API are Phase 8; Hugging Face provider is Phase 7.
