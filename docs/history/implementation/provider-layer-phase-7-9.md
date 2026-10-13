# Provider layer - Phases 7 to 9 (Hugging Face, Pause/Resume, final docs)

Part of PR 4 of `docs/superpowers/plans/2026-10-10-provider-layer.md`.

## Done

- 7.1 `backend/providers/huggingface.py` and the bundled `huggingface.json` preset. Plain `httpx` against `https://api.endpoints.huggingface.cloud/v2`; one endpoint per side named `zotero-rag-<side>` in the token owner's namespace (`options.namespace`, else `whoami-v2`); create payload, TEI `MAX_CLIENT_BATCH_SIZE`, opt-in scale to zero (15 to 2880 minutes) and the image variants follow the Phase 0 live runs. State to health mapping (`updating`/`updateFailed` included), `provision()` (create, resume, wait with bound and deadline, warm up, recreate by delete-then-wait), the 403 "payment method" message, `endpoint_url()` (works while paused), `classify_http_error()` (400 "endpoint is paused" to `paused`, 503 to `cold`, 401 never wakes), `suspend()`, `teardown()`. A TEI instance with no known image tag needs `options.image` (rejected at load otherwise); only the T4 tag was verified live.
- 7.2 Mixed presets (HF + Anthropic, local + Anthropic, HF on the LLM side only) load; each key is listed once under its own side; only the HF side is operable and provisionable.
- 8.1 `Provider.is_paused()`; RunPod reads `workersMax` 0 from the management API (also in `health()`), Hugging Face reads `paused`. The contract suite checks that suspend-capable providers are also provisionable, never raise from `is_paused()` and fail cleanly without a network.
- 8.2 `POST /api/config/suspend`: `_start_job` is shared with provisioning (same gating, credentials, slots, one-time keys); resuming is provisioning.
- 8.3 Paused handling: `PausedCache` (30 s) in `endpoint_cache.py`, invalidated by jobs; the embedding and LLM services fail fast with the paused message before any call; the cron indexer skips only the paused owner's libraries (`embedding_paused`) before draining uploads or touching key status. The paused error is an `EmbeddingEndpointUnavailableError`, so it never counts toward the pending-upload quarantine (#70). The status dialog explains the skip reason.
- 8.4 Plugin: Pause button (ready or cold, operable, suspend-capable), Resume for a paused side (provision path), neutral orange status, inline errors.
- 9.1 Docs: `presets.md` (Hugging Face, mixed presets, pause and resume), `providers.md`, `cron-indexing.md`, `CLAUDE.md`; the two 2026-10-09 specs are marked superseded.

## Not done (needs you)

- 7.3 Live smoke test with a real Hugging Face account (spec C5): provision from the Preferences sections with progress, close and reopen mid-run, re-run, wake from scale to zero via a query, pause and resume, teardown via `bin/provision.py --teardown`, repeat with the mixed preset and with a second user's token. Also confirm the token regex `^hf_[A-Za-z0-9]+$` against a real fine-grained token, the 15-minute scale-to-zero floor and the T4 image tag at provisioning time.
- 6.5 and the container smoke test from the earlier phases.
- Restricted-key lookups (RunPod, Hugging Face) and the MPCDF `/models` probe, see the Phase 0 note.
