"""Build the "Configure preset" dialog's view model (see backend/providers/ui.py).

One tab per side of the pipeline, in the order a document and a query run
through it (extraction, embedding, retrieval, re-ranking when the preset has
it, answering), and one account card
per service provider whose credentials those sides need. Remote sides are
described by their provider's hooks (``Provider.side_facts``, ``side_messages``,
``side_actions``, ``account_fields``); extraction and retrieval are server
components and are described here. Nothing in this module knows a vendor.

``build_preset_view`` is pure: the API layer collects health, permissions and
probe results first (they need the request and blocking I/O) and passes them in.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from backend.providers.types import OPTIONAL_SIDES, Health, Meter, preset_sides, side_config
from backend.services.standin import side_noun
from backend.providers.ui import (
    AccountCard,
    PresetHeader,
    PresetView,
    SideProvider,
    SideState,
    SideView,
    UiFact,
    UiAction,
    UiField,
    UiMessage,
)

#: Tab titles of the sides.
TITLES = {
    "extraction": "Extraction",
    "embedding": "Embedding",
    "retrieval": "Retrieval",
    "rerank": "Re-ranking",
    "llm": "Answering (LLM)",
}

#: Plugin preference behind the re-ranking switch (sent as ``enable_reranking`` with each query).
RERANK_PREF = "enableReranking"

#: Plugin preference for the re-ranking pool size (sent as ``rerank_pool_size`` with each query).
RERANK_POOL_PREF = "rerankPoolSize"

#: Provider health -> tab indicator.
_HEALTH_STATE: dict[str, SideState] = {
    "ready": "ready",
    "cold": "starting",
    "throttled": "down",
    "paused": "down",
    "unreachable": "down",
}

_HEALTH_SUMMARY = {
    "ready": "Ready",
    "cold": "Idle: wakes on the next request",
    "throttled": "Down: no capacity right now",
    "paused": "Paused",
    "unreachable": "Down",
}

#: Client-side retrieval tuning sent with each query (plugin preference -> request field).
#: Defaults mirror backend/services/rag_engine.py.
RETRIEVAL_TUNING = (
    ("diversityFloor", "Diversity floor", "_DIVERSITY_FLOOR",
     "Minimum distinct documents required before retrieval broadens its search."),
    ("diversityEscalationFactor", "Escalation factor", "_DIVERSITY_ESCALATION_FACTOR",
     "Multiplier applied to the search size when broadening."),
    ("diversityEscalationMaxTopK", "Escalation ceiling", "_DIVERSITY_ESCALATION_MAX_TOP_K",
     "Maximum search size after broadening."),
    ("maxChunksPerDocument", "Max passages per document", "_MAX_CHUNKS_PER_DOCUMENT",
     "Passages from a single document allowed into the answer context."),
    ("lowDiversityAvailableFloor", "Single-source retry floor", "_LOW_DIVERSITY_AVAILABLE_FLOOR",
     "Minimum sources available before an answer citing only one is retried."),
)


@dataclass
class Probe:
    """Result of a reachability check of a server component (Kreuzberg, Qdrant)."""

    ok: bool
    detail: str = ""


@dataclass
class ViewInputs:
    """Everything ``build_preset_view`` needs, collected by the API layer."""

    preset: Any
    #: side -> Provider for each side of ``preset_sides``; empty when the preset's providers are invalid.
    providers: dict
    health: dict  # side -> Optional[Health]
    operable: dict  # side -> bool
    job: dict  # the caller's provisioning job state (backend.services.provisioning.get_job_state)
    required_keys: list  # ApiKeyRequirement dumps (GET /api/required-keys)
    header_present: Callable[[str], bool]
    is_admin: bool = False
    default_options: list = field(default_factory=list)
    default_preset: str = ""
    extractor_backend: str = "legacy"
    #: None when no check applies (legacy extractor), else the Kreuzberg / Docling probe.
    extraction_probe: Optional[Probe] = None
    #: None means embedded vector store (in the backend process).
    vector_probe: Optional[Probe] = None
    vector_detail: str = ""
    #: provider id -> the caller's ``session_status`` for providers with an account login.
    sessions: dict = field(default_factory=dict)
    #: side -> ``backend.services.side_probe.SideProbe``: test request (for sides without a
    #: status check) and quota meters.
    probes: dict = field(default_factory=dict)
    #: side -> ``available`` / ``busy`` / ``very busy`` of its model, when the provider knows.
    availability: dict = field(default_factory=dict)


# -- sides --------------------------------------------------------------------


def _job_side(job: dict, side: str) -> dict:
    return (job.get("sides") or {}).get(side) or {}


def _job_log(job: dict, side: str) -> list[str]:
    prefix = f"{side}: "
    return [line[len(prefix):] for line in job.get("progress") or [] if line.startswith(prefix)]


def _rerank_extras(view: SideView, cfg: Any) -> None:
    """The re-ranking tab's switch and pool size (the switch is a per-user preference)."""
    view.optional = True
    view.fields.insert(0, UiField(
        id=RERANK_PREF, label="Use re-ranking", type="toggle", default=True,
        storage="client_pref", key=RERANK_PREF,
        help="Rescores the best passages before the answer is written: more relevant sources, "
             "a few seconds slower. Applies to your own queries; the Query dialog can also skip it once.",
    ))
    reference = cfg.pool_reference_chunks
    automatic = (f"the best {cfg.pool_size} passages per question, more when the searched libraries "
                 f"hold over {reference:,} passages (up to 500)") if reference else (
                 f"the best {cfg.pool_size} passages per question")
    view.fields.insert(1, UiField(
        id=RERANK_POOL_PREF, label="Passages to rescore", type="number", min=1, max=500,
        placeholder=f"Automatic ({cfg.pool_size}+)" if reference else f"{cfg.pool_size} (preset default)",
        storage="client_pref", key=RERANK_POOL_PREF,
        help="How many of the best-matching passages the re-ranker rescores per question. "
             "Large libraries profit from more (relevant passages can rank low in the first search), "
             "small ones may get more generic passages; each 100 adds a fraction of a second. "
             "Leave empty to let the size of the searched libraries decide. Applies to your own queries.",
    ))
    view.facts.append(UiFact(label="Rescored by default", value=automatic))


def _model_side(inputs: ViewInputs, side: str, account: Optional[str]) -> SideView:
    """Embedding, re-ranking or answering side."""
    cfg = side_config(inputs.preset, side)
    provider = inputs.providers.get(side)
    view = SideView(side=side, title=TITLES[side])
    if cfg.model_type != "remote" or provider is None:
        view.state = "ready"
        view.summary = "Runs on this server"
        view.facts = [UiFact(label="Model", value=str(cfg.model_name))]
        if side in OPTIONAL_SIDES:
            _rerank_extras(view, cfg)
        else:
            view.messages = [UiMessage(level="ok", text="This model runs on the server; there is nothing to configure here.")]
        return view

    health: Optional[Health] = inputs.health.get(side)
    job_side = _job_side(inputs.job, side)
    job_status = job_side.get("status")
    operable = bool(inputs.operable.get(side))
    view.provider = SideProvider(id=provider.id, label=provider.label)
    view.account = account
    view.facts = provider.side_facts()
    # The provider's advice says how to get the side going; while a job does that, it is noise.
    view.messages = [] if job_status in ("running", "pending") else provider.side_messages(health)
    view.actions = provider.side_actions(health, job_status, operable)
    view.log = _job_log(inputs.job, side)

    probe = inputs.probes.get(side)
    if probe is not None and getattr(probe, "model", None):  # the model actually tested
        view.facts = [UiFact(label="Model", value=probe.model) if f.label == "Model" else f for f in view.facts]
    if job_status in ("running", "pending"):
        view.state = "starting"
        view.summary = "Starting: " + (view.log[-1] if view.log else "job running")
    elif health is None and probe is not None and probe.ok is not None:
        # No status check of its own: a test request tells whether the model answers.
        view.state = "ready" if probe.ok else "down"
        view.summary = "Ready: answered a test request" if probe.ok else f"Down: {probe.detail or 'the test request failed'}"
        if not probe.ok and not probe.rate_limited:
            view.messages.append(UiMessage(level="error", text=f"A test request failed ({probe.detail}). "
                                                                "Check the key in the account card below."))
    elif health is None:
        view.state = "unknown"
        view.summary = "No status check for this provider"
    else:
        view.state = _HEALTH_STATE.get(health.status, "unknown")
        view.summary = "Starting up" if health.status == "cold" and health.waking else _HEALTH_SUMMARY.get(health.status, health.status)
        if health.status != "ready" and health.detail:
            view.summary += f" ({health.detail})"
    if job_status == "failed":
        view.messages.insert(0, UiMessage(level="error", text=f"Last job failed: {job_side.get('message') or 'unknown error'}"))
    elif job_status == "succeeded" and view.state != "ready":
        view.messages.insert(0, UiMessage(level="info", text="The last job finished; waiting for the endpoint to answer."))
    if provider.supports_provisioning and not operable and view.state == "down":
        view.messages.append(UiMessage(level="info", text="Ask the server admin to start this endpoint."))
    if probe is not None and probe.meters:
        view.quota = provider.quota_summary([Meter(**m) for m in probe.meters])
        if view.quota and view.quota.level == "error" and view.state == "ready" and view.quota.used_pct >= 100:
            view.state = "down"
            view.summary = "Down: quota exhausted"
        if view.quota and view.quota.level != "ok":
            view.messages.append(UiMessage(level=view.quota.level, text=f"Quota: {view.quota.text}."))
    availability = inputs.availability.get(side)
    if availability:
        view.facts.append(UiFact(label="Demand", value=availability))
        if availability == "very busy":
            view.messages.append(UiMessage(level="warn", text="The model is very busy right now; answers may be slow "
                                                              "or fail. Another model of the preset may respond faster."))
    if side in OPTIONAL_SIDES:
        _rerank_extras(view, cfg)
        if view.state == "down":
            view.messages.append(UiMessage(
                level="info", text="Start all leaves this endpoint alone; start it here when you want re-ranking. "
                                   "Until it is up, queries skip re-ranking."))
    return view


def _extraction_side(inputs: ViewInputs, docling_account: Optional[str], docling_key_set: Optional[bool]) -> SideView:
    backend = inputs.extractor_backend
    view = SideView(side="extraction", title=TITLES["extraction"])
    labels = {
        "kreuzberg": "Kreuzberg (server sidecar)",
        "docling": "Docling conversion service",
        "legacy": "Built-in PDF text extraction",
    }
    view.provider = SideProvider(id=backend, label=labels.get(backend, backend))
    view.fields = [UiField(id="extractor", label="Extractor", type="readonly",
                           value=f"{labels.get(backend, backend)}, set by the preset")]
    view.facts = [UiFact(label="Shared by", value="all users of this server")]
    if backend == "legacy":
        view.state = "ready"
        view.summary = "Ready: runs inside the backend"
        return view
    probe = inputs.extraction_probe
    if backend == "docling":
        view.account = docling_account
        if docling_key_set is False:
            view.state = "down"
            view.summary = "Down: no key for the conversion service"
            view.messages.append(UiMessage(level="warn", text="Enter the conversion service's key in the account card below."))
            return view
    if probe is None:
        view.state = "unknown"
        view.summary = "Checked when indexing runs"
    elif probe.ok:
        view.state = "ready"
        view.summary = "Ready" + (f" ({probe.detail})" if probe.detail else "")
    else:
        view.state = "down"
        view.summary = "Down" + (f" ({probe.detail})" if probe.detail else "")
        view.messages.append(UiMessage(level="error", text="Documents cannot be indexed until the extraction service answers. "
                                                          "Queries still work."))
    return view


def _retrieval_side(inputs: ViewInputs) -> SideView:
    from backend.services import rag_engine

    rag = inputs.preset.rag
    view = SideView(side="retrieval", title=TITLES["retrieval"])
    view.provider = SideProvider(id="vector-store", label="Vector store (server)")
    view.fields = [
        UiField(id="top_k", label="Top k", type="readonly", value=f"{rag.top_k}, set by the preset"),
        UiField(id="score_threshold", label="Score threshold", type="readonly",
                value=f"{rag.score_threshold}, set by the preset"),
    ]
    for pref, label, const, help_text in RETRIEVAL_TUNING:
        view.fields.append(UiField(
            id=pref, label=label, type="number", min=1, help=help_text,
            default=getattr(rag_engine, const), storage="client_pref", key=pref,
        ))
    view.messages = [UiMessage(level="info", text="Your tuning applies to your own queries. Leave a field blank to use its default.")]
    if inputs.vector_detail:
        view.facts = [UiFact(label="Store", value=inputs.vector_detail)]
    probe = inputs.vector_probe
    if probe is None:
        view.state = "ready"
        view.summary = "Ready: embedded in the backend"
    elif probe.ok:
        view.state = "ready"
        view.summary = "Ready" + (f" ({probe.detail})" if probe.detail else "")
    else:
        view.state = "down"
        view.summary = "Down" + (f" ({probe.detail})" if probe.detail else "")
        view.messages.insert(0, UiMessage(level="error", text="The vector store does not answer; queries and indexing fail until it is back."))
    return view


# -- accounts -----------------------------------------------------------------


def _credential_field(key: dict, inputs: ViewInputs) -> UiField:
    """A UiField for one entry of the preset's required client fields."""
    kind = key.get("kind", "api_key")
    name = key["key_name"]
    if kind == "api_key":
        return UiField(
            id=name, label=name, type="secret", help=key.get("description") or "",
            pattern=key.get("pattern"), storage="client_secret", key=name, header=key.get("header_name"),
            is_set=inputs.header_present(key.get("header_name") or ""), docs_url=key.get("docs_url"),
            required_for=list(key.get("required_for") or []),
            placeholder="" if inputs.header_present(key.get("header_name") or "") else "Your personal key",
        )
    shared_set = bool(key.get("is_set"))
    return UiField(
        id=name, label=name, type="text" if kind == "shared_base_url" else "secret",
        help=key.get("description") or "", pattern=key.get("pattern"), storage="shared", key=name,
        is_set=shared_set, docs_url=key.get("docs_url"), editable=inputs.is_admin,
        required_for=list(key.get("required_for") or []),
        placeholder=("Configured; enter a new value to replace it" if shared_set else "Not configured yet")
        if inputs.is_admin else ("Set by the server admin" if shared_set else "Not configured; ask the server admin"),
    )


def _accounts(inputs: ViewInputs) -> list[AccountCard]:
    """One card per remote provider, holding the credentials of every side it serves."""
    cards: dict[str, AccountCard] = {}
    providers: dict[str, Any] = {}
    operable: dict[str, bool] = {}
    # Pipeline order, as the tabs (re-ranking runs before answering).
    order = sorted(preset_sides(inputs.preset), key=lambda s: ("embedding", "rerank", "llm").index(s))
    for side in order:
        cfg = side_config(inputs.preset, side)
        provider = inputs.providers.get(side)
        if cfg.model_type != "remote" or provider is None:
            continue
        card = cards.setdefault(provider.id, AccountCard(
            id=provider.id, label=provider.label, scope_note=provider.SCOPE_NOTES.get(provider.scope, ""),
        ))
        card.serves.append(side)
        providers.setdefault(provider.id, provider)
        operable[provider.id] = operable.get(provider.id, False) or bool(inputs.operable.get(side))

    for key in inputs.required_keys:
        sides = key.get("sides") or ["embedding"]
        target = next((cards[p.id] for s in sides if (p := inputs.providers.get(s)) is not None and p.id in cards), None)
        if target is None:
            continue
        if not any(f.id == key["key_name"] for f in target.fields):
            target.fields.append(_credential_field(key, inputs))

    for pid, card in cards.items():
        provider = providers[pid]
        if provider.cost != "free":
            card.billing = _billing_fields(provider, list(card.serves), inputs.preset)
        descriptor = provider.describe()
        has_login = bool(descriptor.provisioning and descriptor.provisioning.login)
        if not provider.supports_provisioning:
            card.auto_start = False  # nothing to start: the button only saves
        session = inputs.sessions.get(pid) if has_login else None
        card.fields = provider.account_fields(card.fields, operable[pid], session)
        missing = [f for f in card.fields if f.storage in ("client_secret", "shared") and not f.optional and not f.is_set]
        if has_login:
            card.auto_start = False  # the login request starts the sides itself
            messages, card.actions = provider.account_session(session)
            card.messages.extend(messages)
            logged_in = bool(session and session.get("state") == "ok")
            card.state = "ready" if logged_in and not missing else "needs_input"
            if missing:
                card.messages.append(UiMessage(level="warn", text="Enter the missing key as well."))
            continue
        card.state = "needs_input" if missing else "ready"
        if missing and any(not f.editable for f in missing):
            card.messages.append(UiMessage(level="warn", text="The server admin has not configured this service yet."))
        elif missing:
            card.messages.append(UiMessage(level="warn", text="Enter the missing key to start the sides that use it."))
    return [cards[pid] for pid in cards]


#: Choices of an account card's Billing select. The plugin shows estimated costs for the
#: token- and time-based ones and the quota for a subscription (plugin/src/rate-limit-widget.js).
BILLING_MODES = ("Not set", "Subscription", "Token-based", "Time-based")

#: A price: a non-negative decimal number.
PRICE_PATTERN = r"\d+(\.\d+)?"

#: Short side names in the Billing fields' labels.
_SIDE_NOUNS = {"embedding": "Embedding", "llm": "Answering", "rerank": "Re-ranking"}


def _price(value: Optional[float]) -> str:
    """A list price as the plugin shows it (no float noise, no exponent)."""
    return f"{value:.6f}".rstrip("0").rstrip(".") if value is not None else ""


def _billing_fields(provider: Any, sides: list[str], preset: Any) -> list[UiField]:
    """The Billing fields of a paid provider's account card (Zotero preferences of the caller).

    A select for how the service bills the user, then the user's prices per side, shown
    for the matching billing type. An empty price uses the provider's list price.
    """
    pid = provider.id
    mode_id = "billing.mode"
    fields = [UiField(
        id=mode_id, label="How this service bills you", type="select", options=list(BILLING_MODES),
        default=BILLING_MODES[0], storage="client_pref", key=f"billing.{pid}.mode",
        help="Token- and time-based billing show the estimated cost of your queries and indexing; "
             "Subscription shows the quota instead (when the service reports one). Not set shows neither.",
    )]
    for side in sides:
        side_provider = provider if provider.side == side else None
        try:
            from backend.providers import get_providers

            side_provider = side_provider or get_providers(preset)[side]
            model = side_provider.configured_model()
            prices = side_provider.pricing(model)
        except Exception:
            model, prices = "", None
        noun = _SIDE_NOUNS.get(side, side)

        def price_field(suffix: str, label: str, list_price: Optional[float], mode: str) -> UiField:
            shown = _price(list_price) if list_price else ""
            return UiField(
                id=f"billing.{side}.{suffix}", label=label, type="text", pattern=PRICE_PATTERN,
                storage="client_pref", key=f"billing.{pid}.{side}.{suffix}", optional=True,
                placeholder=f"List price: {shown}" if shown else "Enter your price",
                help=f"Empty uses the list price for {model}." if shown
                else "No list price is known: enter the price of your contract.",
                show_if={mode_id: [mode]},
            )

        fields.append(price_field("input", f"{noun}: input, USD per 1M tokens",
                                  prices.input_per_million_tokens if prices else None, "Token-based"))
        if side == "llm":
            fields.append(price_field("output", f"{noun}: output, USD per 1M tokens",
                                      prices.output_per_million_tokens if prices else None, "Token-based"))
        fields.append(price_field("second", f"{noun}: USD per second", prices.per_second if prices else None,
                                  "Time-based"))
    return fields


def _docling_key(inputs: ViewInputs) -> Optional[str]:
    extraction = getattr(inputs.preset, "extraction", None)
    options = getattr(extraction, "options", None) or {}
    return options.get("api_key_env")


def build_preset_view(inputs: ViewInputs) -> PresetView:
    """The whole dialog: header, tabs and account cards."""
    accounts = _accounts(inputs)
    by_side = {side: card.id for card in accounts for side in card.serves}

    docling_account: Optional[str] = None
    docling_key_set: Optional[bool] = None
    if inputs.extractor_backend == "docling":
        key_name = _docling_key(inputs)
        for card in accounts:
            match = next((f for f in card.fields if f.key == key_name and f.storage != "session"), None)
            if match is not None:
                docling_account = card.id
                docling_key_set = bool(match.is_set)
                card.serves.insert(0, "extraction")
                break

    model_sides = {side: _model_side(inputs, side, by_side.get(side)) for side in preset_sides(inputs.preset)}
    # A provider without a health check still cannot work while its credentials are missing.
    missing = {card.id for card in accounts if card.state == "needs_input"}
    has_login = {card.id for card in accounts if any(a.sends_login for a in card.actions)}
    for view in model_sides.values():
        if view.state == "unknown" and view.account in missing:
            view.state = "down"
            view.summary = "Down: credentials missing"
        elif view.state == "unknown" and view.account:
            view.summary = "Credentials entered; this provider has no status check"
        if view.account in missing:
            # Starting needs the account first: say so instead of offering a start that fails.
            reason = "Log in with the account card below first." if view.account in has_login \
                else "Enter the missing credentials in the account card below first."
            for action in view.actions:
                if action.id in ("start", "retry") and action.enabled:
                    action.enabled, action.reason = False, reason
            if view.state == "down":
                view.messages.append(UiMessage(level="info", text=reason))
        elif view.state == "down" and (probe := inputs.probes.get(view.side)) is not None and probe.rate_limited:
            # The credentials are fine: the provider's quota is used up. Offer the way out.
            match = re.search(r"until (\d{2}:\d{2}) UTC", probe.detail or "")
            when = match.group(1) if match else ""
            who = view.provider.label if view.provider else "The provider"
            view.messages.append(UiMessage(
                level="info", text=f"{who}'s {side_noun(view.side)} quota is used up" + (f" until {when} UTC." if when else ".")))
            view.actions.append(UiAction(id="standin", label=f"Use a stand-in until {when} UTC" if when else "Use a stand-in"))
        elif view.account and view.state == "down" and not any(a.id in ("start", "retry") and a.enabled for a in view.actions):
            view.messages.append(UiMessage(level="info", text="Check the credentials in the account card below."))

    for card in accounts:
        quotas = [v.quota for v in model_sides.values() if v.account == card.id and v.quota]
        if quotas:
            card.quota = max(quotas, key=lambda q: q.used_pct)

    sides = [
        _extraction_side(inputs, docling_account, docling_key_set),
        model_sides["embedding"],
        _retrieval_side(inputs),
        *([model_sides["rerank"]] if "rerank" in model_sides else []),
        model_sides["llm"],
    ]
    header = PresetHeader(
        name=inputs.preset.name,
        description=inputs.preset.description or "",
        is_admin=inputs.is_admin,
        default_options=list(inputs.default_options) if inputs.is_admin else [],
        default_preset=inputs.default_preset if inputs.is_admin else "",
    )
    return PresetView(preset=header, sides=sides, accounts=accounts, busy=inputs.job.get("status") == "running")
