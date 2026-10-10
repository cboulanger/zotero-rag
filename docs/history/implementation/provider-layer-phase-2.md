# Provider layer - Phase 2 (vendor providers and call-site swap)

Part of PR 1 of `docs/superpowers/plans/2026-10-10-provider-layer.md` (phases 0-2).
Design: `docs/superpowers/specs/2026-10-10-huggingface-preset-and-provisioner-adapters-design.md`.
User-facing docs: `docs/providers.md`, `docs/presets.md`.

## Done

- Providers: `kisski`, `openai`, `anthropic`, `mpcdf`, `runpod` (plus `generic` = base class). Core no longer branches on vendors: key/docs tables, model-name sniffing for the LLM protocol (`llm_api`), `HEALTH_CHECKS`, `fetch_kisski_rag_models` and `provisioning_script` are gone.
- Presets: all bundled presets are version 2 with provider blocks; `runpod.json` rewritten (per-side options, scope `managed`).
- Provisioning: in-process per-side jobs (`backend/services/provisioning.py`), `POST /api/config/provision` with `keys` (one-time, never stored) and `sides`; `bin/provision.py` replaces `bin/provision_runpod_endpoints.py`; RunPod pause (`workersMax` 0) and resume (provision on a paused endpoint).
- Usage meters: `backend/services/usage_meters.py` records rate-limit headers per side and key fingerprint (embedding and LLM responses); providers parse them into meters; `GET /api/rate-limits`, autoindex status and upload responses carry `meters`/`rate_limit_meters`; the cron indexer persists `last_usage[side]`; the plugin widget draws one bar per meter (fake DOM helper `plugin/test/fake-dom.js`).
- Errors: `backend/services/endpoint_errors.py` adds the provider's `classify_http_error` state and `unavailable_hint` to "endpoint unavailable" errors; a paused endpoint answering 400 is classified before the per-item `BadRequestError` handling. These errors stay `EmbeddingEndpointUnavailableError`/`LLMEndpointUnavailableError`, so they do not count toward the pending-upload quarantine.
- Deleted: `backend/utils/kisski.py`, `backend/utils/endpoint_health.py`, `bin/provision_runpod_endpoints.py` and their tests (behaviour now covered by `test_provider_*`, `test_provision_job.py`, `test_provision_cli.py`).

## Verification

- `uv run pytest backend/tests --ignore=backend/tests/test_kreuzberg_extractor.py`: all passing (that file allocates 10 GB and OOM-kills small sandboxes).
- `node --test` in `plugin/`: 290 passing.
- The container smoke test (`uv run pytest -m container -v -s`) could not run in the authoring sandbox (no container daemon); run it before merging.

## Not done / open

- Pause/Resume buttons in the plugin UI, per-side configuration sections and the per-user preset choice belong to later phases.
- The app still falls back to environment variables for provider keys; removing that is a later phase.
- Untested against live services: restricted-token endpoint lookup (RunPod, Hugging Face) and the MPCDF `/models` probe (see `provider-layer-phase-0-spike.md`, "Needs you").
- Spec notes: a non-generic provider on a local side is rejected (B7/A1); provider instances are built per call, not cached.
