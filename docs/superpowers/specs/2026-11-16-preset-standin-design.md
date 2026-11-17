# Stand-in presets when a provider's quota is exhausted

Design, 2026-11-16.

## Intent

When a query fails because a provider's quota is used up (observed: KISSKI's embedding quota,
HTTP 503 "Embedding rate limit still in effect ... available at 23:00 UTC", for the rest of the
day), the user should be **offered** an equivalent preset to use in the meantime, never switched
automatically: a stand-in may cost money (RunPod) or need a login (MPCDF), so the decision stays
with the user. Which presets can stand in for which, and in which order of preference (free ones
first), must follow from what presets and providers declare, not from hard-coded pairs.

Measured on 2026-11-16: a stand-in built from MPCDF embedding and re-ranking plus Gemma 4 31B
(4-bit) on a RunPod A6000 reached judged gold-set quality close to KISSKI's (recall 82% vs 83%,
overall 4.2 vs 4.3), at about $0.005 per warm question, $0.07 per cold start and a median of
15.5 s per question (KISSKI 8.3 s).

## Goals

- When a query fails with a quota error, the plugin shows which side and provider ran out, when
  it resets, and a ranked list of stand-in presets the user can switch to with one action.
- The switch is **temporary**: it lasts until the exhausted quota resets, then the user's own
  preset applies again by itself.
- Stand-ins and their order are derived from declarations; a new or custom preset takes part
  without further configuration.
- A stand-in that needs something first (an MPCDF login, a paused endpoint started, a RunPod key)
  says so, and the offer leads to the place where it is done (the Configure preset dialog).

## Non-goals

- Switching automatically, including for background work: the cron indexer and the built-in
  scheduler keep failing on an exhausted quota as today (they retry on their next run).
- Indexing on a stand-in, including indexing and document uploads started from the plugin: they
  keep running on the user's own preset and fail on an exhausted quota as today. *(Decided by the
  user: a large batch on a paid stand-in can cost far more than a few questions, unnoticed.)*
- Detecting a quota *before* it runs out (the rate-limit meters already show usage).
- Stopping a paid stand-in's endpoint when the switch ends: RunPod endpoints scale to zero after
  their idle timeout (60 s) and MPCDF jobs end after their idle time, so nothing keeps billing.
- Renaming presets (deferred).

## Anti-goals

- Offering a stand-in that does not share the vector store (a different embedding model): its
  answers would come from an empty or wrong index.
- Offering a stand-in that depends on the exhausted provider for the exhausted side.
- A paid stand-in that keeps being used after the quota is back without the user knowing.

## Approaches

1. **Derived (chosen by the user).** Stand-in = the existing compatibility rule plus two checks;
   order = a cost class declared per provider plus optional preset fields.
2. Named groups with ranks per preset (`standin_group`, `standin_rank`): explicit, but the group
   mostly repeats the embedding model and can contradict it; custom presets join only if tagged.
3. Explicit fallback lists per preset: full control, N×N maintenance, lists can name incompatible
   presets.

Approach 1 was chosen because it reuses the compatibility rule the backend already enforces, adds
only one declaration per provider and two optional preset fields, and cannot offer a stand-in
that breaks the index.

## Design

### Declarations

- **Provider cost class:** `Provider.cost: ClassVar[Literal["free", "paid"]]`.
  `kisski` and `mpcdf`: `free`. `runpod`, `huggingface`, `openai`, `google`, `anthropic`: `paid`.
  The generic OpenAI-compatible provider: `paid` (unknown cost counts as paid). A side with
  `model_type: "local"` counts as `free`.
- **Preset cost:** `free` when every side is free, else `paid`. A preset may override it with
  `"cost": "free" | "paid"` (for an institutional account that is free for its users).
- **Optional preset fields:** `"standin": false` keeps a preset out of every offer (test or
  experimental presets); `"standin_priority": <int>` (default 0, higher first) orders presets of
  the same cost class.

### Who can stand in

For a caller whose effective preset P failed with a quota error on side S of provider X, a preset
C is a stand-in when all hold:

1. `C != P`, `C.standin` is not false, and C is available on this platform.
2. `is_compatible(default, C)` (`backend/services/effective_preset.py`): fully remote and the same
   embedding model as the server default, i.e. the same vector store.
3. C's side S is not served by provider X (for the embedding side; for the LLM side likewise;
   a stand-in may still use X for the *other* sides, since quotas are per side).
4. C is not excluded for missing preparation: a side that needs a personal key the server has not
   seen, a login or a started endpoint is marked with what it needs (see the offer). *(Builder's
   call: the plugin sends only the current preset's keys, so excluding presets with unseen keys
   would hide every RunPod stand-in.)*

Order: free before paid, then `standin_priority` descending, then preset name.

### Quota errors carry structure

Only a quota that stays exhausted for a while is worth a stand-in: the offer is made when the
reset is at least **15 minutes** away. Shorter throttles (a 429 without or with a short
retry-after) keep today's plain error. *(Builder's call; a one-minute switch could cost a paid
cold start for nothing.)*

`EmbeddingRateLimitExhaustedError` and `LLMRateLimitExhaustedError` already carry `available_at`.
They gain the side and the provider id. `POST /api/query` answers them with HTTP 503 and a
structured detail (other 503s keep their string detail):

```json
{"detail": {
  "code": "quota_exhausted",
  "message": "KISSKI's embedding quota is used up until 23:00 UTC.",
  "side": "embedding", "provider": "kisski", "available_at": "2026-11-16T23:00:00Z",
  "standins": [
    {"preset": "remote-mpcdf", "cost": "free", "label": "...", "needs": ["login:mpcdf"]},
    {"preset": "mpcdf-gemma-runpod", "cost": "paid", "label": "...", "needs": ["login:mpcdf", "start:llm"]}
  ]
}}
```

`needs` lists what must happen before the stand-in can answer: `login:<provider>` (no session),
`key:<KEY_NAME>` (a personal key the server has not seen; the plugin sends it if it has one stored,
else the Use flow asks for it as the Configure preset dialog does), `start:<side>` (endpoint paused, cold or not provisioned, from the provider
health check). An empty list means it is ready.

### The temporary switch

- `PUT /api/config/my-preset/standin` with `{"preset_name": C}` stores a stand-in for the caller
  next to their own choice in `user_settings.json`
  (`standin: {"preset": C, "until": ISO datetime, "replaces": P}`). The server takes the exhausted
  side, provider and `until` from its **own** rate-limit state for the caller's current preset
  (the gate that raises "rate limit still in effect"), never from the client, so a stand-in cannot
  outlast the reset the server observed. It refuses (409) when no quota is exhausted any more
  (also after a restart that lost the state) or when C is not a stand-in under the rules above.
  *(Builder's call.)*
- `get_user_preset` returns the stand-in while `now < until` and it is still compatible; after
  that it ignores and removes it, so the user's own choice (or the default) applies again.
- Choosing a preset by hand (`PUT /api/config/my-preset`) clears a stand-in.
  `DELETE /api/config/my-preset/standin` ends it early.
- `GET /api/config` reports an active stand-in (`standin: {preset, until, replaces}`), so the
  plugin can show it.
- **Loopback** (no identity, single user, e.g. a local dev instance): the stand-in is stored
  once for the server (a `loopback` entry in `user_settings.json`), replaces the server default
  for requests without identity, and has the same expiry. Changing the server default clears it.
  Instances with identities never store a global stand-in. *(Builder's call.)*
- **Only queries use a stand-in.** It applies to `POST /api/query` and to the configuration
  endpoints that show and prepare the caller's active preset (`/api/config`, `/api/config/preset-view`,
  provisioning, warm-up, health, session login), so the Configure preset dialog can log in to and
  start the stand-in. Indexing and upload endpoints, the auto-index resolver
  (`backend/services/autoindex_resolver.py`), the cron indexer and the scheduler resolve presets
  without it: stand-in resolution is opt-in per request (e.g. a flag the auth middleware sets from
  the route, read by `get_user_preset`), never the default.

### Plugin

- **Query dialog, on a `quota_exhausted` error:** instead of the plain error, a notice:
  "KISSKI's embedding quota is used up until 23:00 (in 4 h). Use a stand-in until then?" with the
  ranked stand-ins (name, "free"/"paid", what each needs) and the buttons **Use** (per stand-in)
  and **Not now**. **Use** on a ready stand-in stores the switch and re-sends the question. On a
  stand-in that needs a login or a start, it stores the switch and opens the Configure preset
  dialog (which then shows the stand-in, since it is now the caller's preset); the question is
  re-sent when the dialog closes with the pipeline ready (the query dialog's existing open-time
  check).
- **While a stand-in is active:** the query dialog shows a line "Using remote-mpcdf until 23:00
  (KISSKI quota) — End now". The Preferences pane's preset chooser shows the same line.
- **When it ends:** nothing to do; the next request runs on the user's own preset again. The
  dialog's line disappears on the next config refresh.

## Constraints

- A query that fails with a quota error must not be retried automatically.
- No secret appears in the offer, the stored stand-in or the logs.
- Async API handlers must not block the event loop (CLAUDE.md); the stand-in computation reads
  files and may call provider health checks, so it runs in a thread.
- User-facing docs describe current behaviour only (CLAUDE.md).

## Left to the builder

- Exact wording and layout of the notice; where the active-stand-in line sits.
- Whether the health checks for `needs: start:*` run on the offer request (slower, accurate) or
  only when the user picks a stand-in.
- How the plugin learns that the Configure preset dialog finished with the pipeline ready (reuse
  of the existing open-time check is the intended way).
- Test layout.
