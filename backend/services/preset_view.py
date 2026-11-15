"""Build the "Configure preset" dialog's view model (see backend/providers/ui.py).

One tab per side of the pipeline, in the order a document and a query run
through it (extraction, embedding, retrieval, answering), and one account card
per service provider whose credentials those sides need. Remote sides are
described by their provider's hooks (``Provider.side_facts``, ``side_messages``,
``side_actions``, ``account_fields``); extraction and retrieval are server
components and are described here. Nothing in this module knows a vendor.

``build_preset_view`` is pure: the API layer collects health, permissions and
probe results first (they need the request and blocking I/O) and passes them in.
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from backend.providers.types import Health
from backend.providers.ui import (
    AccountCard,
    PresetHeader,
    PresetView,
    SideProvider,
    SideState,
    SideView,
    UiFact,
    UiField,
    UiMessage,
)

#: Tab titles of the sides.
TITLES = {
    "extraction": "Extraction",
    "embedding": "Embedding",
    "retrieval": "Retrieval",
    "llm": "Answering (LLM)",
}

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
    #: side -> Provider for "embedding" and "llm"; empty when the preset's providers are invalid.
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


# -- sides --------------------------------------------------------------------


def _job_side(job: dict, side: str) -> dict:
    return (job.get("sides") or {}).get(side) or {}


def _job_log(job: dict, side: str) -> list[str]:
    prefix = f"{side}: "
    return [line[len(prefix):] for line in job.get("progress") or [] if line.startswith(prefix)]


def _model_side(inputs: ViewInputs, side: str, account: Optional[str]) -> SideView:
    """Embedding or answering side."""
    cfg = inputs.preset.embedding if side == "embedding" else inputs.preset.llm
    provider = inputs.providers.get(side)
    view = SideView(side=side, title=TITLES[side])
    if cfg.model_type != "remote" or provider is None:
        model = cfg.model_name
        view.state = "ready"
        view.summary = "Runs on this server"
        view.facts = [UiFact(label="Model", value=str(model))]
        view.messages = [UiMessage(level="ok", text="This model runs on the server; there is nothing to configure here.")]
        return view

    health: Optional[Health] = inputs.health.get(side)
    job_side = _job_side(inputs.job, side)
    job_status = job_side.get("status")
    operable = bool(inputs.operable.get(side))
    view.provider = SideProvider(id=provider.id, label=provider.label)
    view.account = account
    view.facts = provider.side_facts()
    view.messages = provider.side_messages(health)
    view.actions = provider.side_actions(health, job_status, operable)
    view.log = _job_log(inputs.job, side)

    if job_status in ("running", "pending"):
        view.state = "starting"
        view.summary = "Starting: " + (view.log[-1] if view.log else "job running")
    elif health is None:
        view.state = "unknown"
        view.summary = "No status check for this provider"
    else:
        view.state = _HEALTH_STATE.get(health.status, "unknown")
        view.summary = _HEALTH_SUMMARY.get(health.status, health.status)
        if health.status != "ready" and health.detail:
            view.summary += f" ({health.detail})"
    if job_status == "failed":
        view.messages.insert(0, UiMessage(level="error", text=f"Last job failed: {job_side.get('message') or 'unknown error'}"))
    elif job_status == "succeeded" and view.state != "ready":
        view.messages.insert(0, UiMessage(level="info", text="The last job finished; waiting for the endpoint to answer."))
    if provider.supports_provisioning and not operable and view.state == "down":
        view.messages.append(UiMessage(level="info", text="Ask the server admin to start this endpoint."))
    if account and view.state == "down" and not any(a.workflow == "start" and a.enabled for a in view.actions):
        view.messages.append(UiMessage(level="info", text="Check the credentials in the account card below."))
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
    for side in ("embedding", "llm"):
        cfg = inputs.preset.embedding if side == "embedding" else inputs.preset.llm
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
        card.fields = providers[pid].account_fields(card.fields, operable[pid])
        missing = [f for f in card.fields if f.storage in ("client_secret", "shared") and not f.optional and not f.is_set]
        card.state = "needs_input" if missing else "ready"
        if missing and any(not f.editable for f in missing):
            card.messages.append(UiMessage(level="warn", text="The server admin has not configured this service yet."))
        elif missing:
            card.messages.append(UiMessage(level="warn", text="Enter the missing key to start the sides that use it."))
    return [cards[pid] for pid in cards]


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

    model_sides = {side: _model_side(inputs, side, by_side.get(side)) for side in ("embedding", "llm")}
    # A provider without a health check still cannot work while its credentials are missing.
    missing = {card.id for card in accounts if card.state == "needs_input"}
    for view in model_sides.values():
        if view.state == "unknown" and view.account in missing:
            view.state = "down"
            view.summary = "Down: credentials missing"
        elif view.state == "unknown" and view.account:
            view.summary = "Credentials entered; this provider has no status check"

    sides = [
        _extraction_side(inputs, docling_account, docling_key_set),
        model_sides["embedding"],
        _retrieval_side(inputs),
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
