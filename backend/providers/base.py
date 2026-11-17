"""The ``Provider`` base class, which is also the ``generic`` provider.

One instance serves one side (embedding or LLM) of one preset. Every hook has
a default that implements the generic behaviour for "any OpenAI-compatible
HTTP API" (``/v1/embeddings``, ``/v1/chat/completions``, bearer-key auth), so
``GenericProvider`` is just this class registered under the id ``generic``.
Vendor providers subclass it and override only what differs, calling
``super()`` for the rest.

Vendor providers live in backend plugins (``backend/plugins/`` and the directories
in ``BACKEND_PLUGIN_DIRS``), which register them as ``inference.provider``
capabilities. See docs/superpowers/specs/2026-10-10-huggingface-preset-and-provisioner-adapters-design.md
(section A2) for the contract and docs/providers.md and docs/backend-plugins.md for how to add one.
"""

import fnmatch
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, ClassVar, Literal, Mapping, Optional, Sequence

from pydantic import BaseModel

from backend.providers.ui import UiAction, UiFact, UiField, UiMessage, UiQuota, UiRequest
from backend.providers.types import (
    Cost,
    Credentials,
    Health,
    Meter,
    ModelInfo,
    NoOptions,
    OPTIONAL_SIDES,
    Pricing,
    ProviderDescriptor,
    ProvisionContext,
    Scope,
    Side,
    side_config,
    Usage,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from backend.config.presets import HardwarePreset

#: Registry of provider classes by id, filled by the plugin host from the
#: ``inference.provider`` capabilities of the loaded backend plugins (see
#: ``backend.pluginhost``) through :func:`register_provider`.
PROVIDERS: dict[str, type["Provider"]] = {}


class Provider:
    """Generic OpenAI-compatible provider; base class of every vendor provider."""

    id: ClassVar[str] = "generic"
    label: ClassVar[str] = "Generic (OpenAI-compatible)"
    #: Validates the side's ``provider.options`` (opaque to core).
    Options: ClassVar[type[BaseModel]] = NoOptions
    supported_sides: ClassVar[frozenset[Side]] = frozenset({"embedding", "llm"})
    #: Scopes a preset may select for this provider (see spec A10).
    key_scopes: ClassVar[frozenset[Scope]] = frozenset({"user", "shared", "managed"})
    #: Used when the preset names none. ``user`` for anything that may cost money.
    default_scope: ClassVar[Scope] = "user"
    supports_provisioning: ClassVar[bool] = False
    #: True when ``endpoint_url()`` finds the side's URL from the key (no ``base_url`` in the preset).
    derives_endpoint_url: ClassVar[bool] = False
    supports_suspend: ClassVar[bool] = False
    #: False when provisioning runs in the caller's own login session instead of with a key.
    provisioning_needs_key: ClassVar[bool] = True
    #: True when each user runs their own endpoint for this side in a provider session
    #: (see ``session_endpoint``); the side then has no fixed URL or shared key.
    uses_session_endpoints: ClassVar[bool] = False
    #: True when ``live_models()`` can return a list (so a per-request model name is accepted).
    has_live_models: ClassVar[bool] = False
    #: Wire protocol of the LLM side; core implements the protocols, not vendors.
    llm_api: ClassVar[Literal["openai", "anthropic"]] = "openai"
    #: Timeout (seconds) of the management-API client created by ``_http()``.
    http_timeout: ClassVar[float] = 30.0
    #: Whether using this provider costs the user money. Unknown cost counts as ``paid``.
    cost: ClassVar[Literal["free", "paid"]] = "paid"

    def __init__(
        self,
        side: Side,
        preset: "HardwarePreset",
        options: Optional[BaseModel] = None,
        scope: Optional[Scope] = None,
    ) -> None:
        self.side: Side = side
        self.preset = preset
        self.options: BaseModel = options if options is not None else self.Options()
        self.scope: Scope = scope or self.default_scope
        self._http_client: Any = None

    # -- configuration helpers ------------------------------------------------

    @property
    def side_config(self):
        """The ``EmbeddingConfig``, ``LLMConfig`` or ``RerankConfig`` this instance serves."""
        return side_config(self.preset, self.side)

    def _http(self) -> Any:
        """Blocking ``httpx.Client`` for management-API calls (lazily created).

        Tests replace ``_http_client`` with a fake. Provider methods that use
        it are synchronous; core calls them through ``asyncio.to_thread``.
        """
        if self._http_client is None:
            import httpx

            self._http_client = httpx.Client(timeout=self.http_timeout)
        return self._http_client

    def apply_defaults(self) -> None:
        """Fill provider defaults into the side's ``model_kwargs`` (idempotent).

        Called once when a preset is loaded so the embedding and LLM services
        keep reading ``model_kwargs`` only. Never overwrites a value the preset
        sets explicitly. The base class fills in the provider's default key name
        (when the preset names none) and the key-portal links for the key and URL
        fields in use; subclasses add their patterns and call ``super()``.
        """
        cfg = self.side_config
        if cfg.model_type != "remote":
            return
        kwargs = cfg.model_kwargs
        env = self.default_key_env()
        if env and not kwargs.get("api_key_env") and not kwargs.get("shared_api_key_env"):
            kwargs["api_key_env" if self.scope == "user" else "shared_api_key_env"] = env
        docs: dict[str, str] = {}
        for field in ("api_key_env", "shared_api_key_env", "shared_base_url_env"):
            name = kwargs.get(field)
            if name:
                url = self.key_docs_url(name)
                if url:
                    docs[name] = url
        if docs:
            kwargs["key_docs_urls"] = {**docs, **(kwargs.get("key_docs_urls") or {})}

    # -- description ----------------------------------------------------------

    def describe(self) -> ProviderDescriptor:
        """What the plugin needs to render this side. Contains no secrets."""
        return ProviderDescriptor(
            id=self.id,
            label=self.label,
            key_scope=self.scope,
            supports_provisioning=self.supports_provisioning,
            supports_suspend=self.supports_suspend,
            unavailable_hint=self.unavailable_hint(),
        )

    # -- configuration dialog (see backend/providers/ui.py) ---------------------

    #: How the dialog describes who owns the credentials, by scope.
    SCOPE_NOTES: ClassVar[dict[str, str]] = {
        "user": "your own account",
        "managed": "operated by the server admin",
        "shared": "provided by your institution",
    }

    def side_facts(self) -> list[UiFact]:
        """Short facts for this side's Status section (model, provider, credential owner)."""
        cfg = self.side_config
        model = (getattr(cfg, "literal_model_names", None) or [cfg.model_name])[0]
        facts = [UiFact(label="Model", value=str(model))]
        note = self.SCOPE_NOTES.get(self.scope)
        if note:
            facts.append(UiFact(label="Account", value=note))
        return facts

    def side_messages(self, health: Optional[Health]) -> list[UiMessage]:
        """Status messages for a side; by default the provider's advice while it is not ready."""
        if health is None or health.status == "ready":
            return []
        hint = self.unavailable_hint()
        return [UiMessage(level="warn", text=hint)] if hint else []

    def side_actions(self, health: Optional[Health], job_status: Optional[str], operable: bool) -> list[UiAction]:
        """The Actions section of this side's tab.

        ``job_status`` is the status of this side's provisioning job (``running``,
        ``pending``, ``failed``, ``succeeded``) or None. ``operable`` says whether the
        caller may provision or pause this side. The default offers start (create,
        wake or resume), retry after a failure and pause, as the capability flags allow.
        """
        status = health.status if health else ""
        running = job_status in ("running", "pending")
        busy = "A job for this endpoint is already running." if running else ""
        # The optional re-ranking endpoint is only ever started on its own, never by Start all.
        start_workflow = None if self.side in OPTIONAL_SIDES else "start"
        actions: list[UiAction] = []
        if self.supports_provisioning and operable:
            if status in ("unreachable", "throttled", "paused"):
                actions.append(UiAction(
                    id="start", label="Resume" if status == "paused" else "Start endpoint", kind="primary",
                    enabled=not running, reason=busy, workflow=start_workflow, account=self.id,
                    request=UiRequest(path="/api/config/provision", body={"sides": [self.side]}),
                ))
            elif job_status == "failed":
                actions.append(UiAction(
                    id="retry", label="Retry", kind="primary", workflow=start_workflow, account=self.id,
                    request=UiRequest(path="/api/config/provision", body={"sides": [self.side]}),
                ))
        if status == "cold" and self.side not in OPTIONAL_SIDES:  # warmup wakes embedding and llm
            waking = bool(health and health.waking) or running
            actions.append(UiAction(
                id="wake", label="Wake up", workflow="start", enabled=not waking,
                reason="The endpoint is already starting up." if waking else "",
                request=UiRequest(path="/api/config/warmup"),
            ))
        if self.supports_suspend and operable and status in ("ready", "cold"):
            actions.append(UiAction(
                id="pause", label="Pause", enabled=not running, reason=busy, workflow="pause", account=self.id,
                request=UiRequest(path="/api/config/suspend", body={"sides": [self.side]}),
            ))
        return actions

    def account_fields(self, fields: list[UiField], operable: bool, session: Optional[dict] = None) -> list[UiField]:
        """The fields of this provider's account card.

        ``fields`` are the credentials the preset declares for the sides this provider
        serves (personal keys and admin-set shared values), already built by core.
        ``session`` is the caller's ``session_status`` for a provider with an account
        login (None otherwise). The default adds the login form while the caller is not
        logged in, else the one-time provisioning credential when the caller may
        provision or pause.
        """
        credential = None
        descriptor = self.describe()
        login = descriptor.provisioning.login if descriptor.provisioning else None
        if login is not None:
            if session and session.get("state") == "ok":
                return fields
            return [*fields, *(
                UiField(
                    id=f"login:{f.name}", label=f.label, type="secret" if f.type == "password" else "text",
                    help=f.help, optional=f.optional, storage="login", key=f.name,
                )
                for f in login.fields
            )]
        if operable and descriptor.provisioning and descriptor.provisioning.credential:
            credential = descriptor.provisioning.credential
        if credential is None:
            return fields
        return [*fields, UiField(
            id=f"session:{credential.env}", label=credential.label, type="secret",
            help=credential.help, pattern=credential.pattern, optional=credential.optional,
            storage="session", key=credential.env,
            placeholder="Optional, kept in memory only" if credential.optional else "",
        )]

    def quota_summary(self, meters: list[Meter]) -> Optional[UiQuota]:
        """One value for how much of this side's quota is used, from its meters.

        The default takes the meter closest to running out. Override to combine the
        periods that matter for the provider (e.g. hourly and daily). None without meters.
        """
        return quota_from(meters, "")

    def model_availability(self, creds: Credentials) -> Optional[str]:
        """``available``, ``busy`` or ``very busy`` for this side's model; None when unknown.

        The default asks ``live_models`` (providers with ``has_live_models``) for the
        answering side's model. Blocking; never raises.
        """
        if not self.has_live_models or self.side != "llm" or not creds.base_url or not creds.api_key:
            return None
        try:
            models = self.live_models(creds.base_url, creds.api_key) or []
        except Exception:
            return None
        cfg = self.side_config
        wanted = (getattr(cfg, "literal_model_names", None) or [cfg.model_name])[0]
        found = next((m for m in models if m.id == wanted), None)
        return found.availability if found else None

    def account_session(self, session: Optional[dict]) -> tuple[list[UiMessage], list[UiAction]]:
        """Status line and actions (log in, log out) of an account card with a login.

        Only called for a provider whose descriptor has ``provisioning.login``; the
        log-in request also starts the sides that run in the session.
        """
        descriptor = self.describe()
        login = descriptor.provisioning.login if descriptor.provisioning else None
        if session and session.get("state") == "ok":
            who = f" as {session['username']}" if session.get("username") else ""
            until = f" until {session['expires_at']}" if session.get("expires_at") else ""
            return (
                [UiMessage(level="ok", text=f"Logged in{who}{until}.")],
                [UiAction(id="logout", label="Log out",
                          request=UiRequest(path="/api/config/session/logout", body={"side": self.side}))],
            )
        messages = [UiMessage(level="info", text=login.help)] if login and login.help else []
        messages.append(UiMessage(level="warn", text="Not logged in. Logging in starts the endpoints."))
        return messages, [UiAction(
            id="login", label="Log in and start", kind="primary", sends_login=True,
            request=UiRequest(path="/api/config/session/login", body={"side": self.side}),
        )]

    # -- credentials and endpoints -------------------------------------------

    def endpoint_url(self, api_key: str) -> Optional[str]:
        """Base URL of a provisioned endpoint, derived from the owner's key.

        Only for providers whose endpoints live in an account (RunPod, HF).
        None when it does not exist or the provider has no such concept.
        """
        return None

    def is_paused(self, api_key: str) -> bool:
        """Whether this side's endpoint was paused on purpose (see ``suspend``).

        Only providers with ``supports_suspend`` override it. Must not raise; an
        unknown state counts as not paused.
        """
        return False

    def default_key_env(self) -> Optional[str]:
        """Env-var style name of the personal key for this side (e.g. ``OPENAI_API_KEY``)."""
        return None

    def key_docs_url(self, env_var: str) -> Optional[str]:
        """Where the user manages the key called ``env_var``, if known."""
        return None

    def unavailable_hint(self) -> Optional[str]:
        """Provider-specific advice for a side that is not ready.

        Appended to "endpoint unavailable" errors and shown in the Configure preset dialog.
        Core text stays vendor-neutral.
        """
        return None

    # -- re-ranking side -------------------------------------------------------

    def rerank_credentials(self, request: Any, refresh: bool = False) -> Optional[Credentials]:
        """URL and key of the calling user's ``/rerank`` endpoint, or None when there is none.

        ``request`` is the per-query ``backend.services.rerank.RerankRequest``
        (the caller's keys and user id). ``refresh`` asks for fresh values after
        the endpoint rejected the previous ones. The generic implementation uses
        the preset's fixed ``base_url`` and the caller's personal key; a provider
        whose endpoints live in the user's account derives or looks them up.
        """
        kwargs = self.side_config.model_kwargs
        base_url = kwargs.get("base_url")
        if not base_url:
            return None
        env = kwargs.get("api_key_env")
        api_key = request.client_keys.get(env) if env else None
        if env and not api_key:
            return None
        return Credentials(api_key=api_key, base_url=base_url)

    # -- per-user sessions --------------------------------------------------------

    def session_login(self, user_id: Optional[int], values: dict[str, str]) -> dict:
        """Log the caller in to the provider's per-user session (see ``SessionLoginDescriptor``).

        Returns the session state (``{"state": "ok"|"login_required", ...}``, no
        secrets). Only providers whose descriptor offers a login form implement it.
        """
        raise NotImplementedError(f"{self.id} has no per-user session")

    def session_logout(self, user_id: Optional[int]) -> None:
        raise NotImplementedError(f"{self.id} has no per-user session")

    def session_status(self, user_id: Optional[int]) -> Optional[dict]:
        """The caller's session state, or None when the provider has no sessions."""
        return None

    def session_endpoint(self, user_id: Optional[int]) -> Optional[Credentials]:
        """URL and key of the caller's running endpoint for this side, or None when it is not running.

        Only providers with ``uses_session_endpoints`` implement it. Credentials
        may change at any time (a job renewal), so callers ask before each use.
        """
        return None

    def session_activity(self, user_id: Optional[int]) -> None:
        """Report that the caller's endpoint is in use, so it is not stopped as idle."""
        return None

    # -- model list -----------------------------------------------------------

    def live_models(self, base_url: str, api_key: str) -> Optional[list[ModelInfo]]:
        """Live, availability-ordered model list (LLM side), or None when unsupported.

        The first entry is the preferred model. Must not raise: a network
        failure returns None so callers fall back to the preset's static list.
        """
        return None

    # -- usage meters ---------------------------------------------------------

    def parse_usage(
        self,
        headers: Mapping[str, str],
        *,
        as_of: Optional[str] = None,
        source: Literal["run", "cache"] = "run",
    ) -> list[Meter]:
        """Turn captured response headers into display meters (quota only).

        The generic implementation understands the OpenAI-style and the IETF
        ``RateLimit-*`` header dialects. Never raises; unrecognised headers
        yield ``[]``.
        """
        from backend.providers.usage import parse_standard_headers

        return parse_standard_headers(
            headers, side=self.side, as_of=as_of or _utc_now(), source=source
        )

    # -- costs ------------------------------------------------------------------

    #: Explains what a cost estimate of this provider leaves out (shown with the figure).
    cost_note: ClassVar[str] = ""

    def default_pricing(self, model: str) -> Optional[Pricing]:
        """List prices of ``model`` on this side, when the provider knows them.

        Paid providers override it with their price list. None means "unknown":
        no cost is shown rather than a wrong one.
        """
        return None

    def pricing(self, model: str) -> Optional[Pricing]:
        """The prices used for cost estimates of ``model``: the preset's, else the provider's.

        The side's ``model_kwargs.pricing`` is one ``Pricing`` object or a mapping of
        model-name glob patterns to one. A free provider has no prices. Never raises:
        a malformed preset value is ignored.
        """
        if self.cost == "free":
            return None
        cfg = self.side_config
        configured = (cfg.model_kwargs or {}).get("pricing") if cfg is not None else None
        if isinstance(configured, dict) and configured:
            try:
                if any(isinstance(v, dict) for v in configured.values()):
                    table = [(p, Pricing.model_validate(v)) for p, v in configured.items() if isinstance(v, dict)]
                    found = match_pricing(table, model)
                    if found is not None:
                        return found
                else:
                    return Pricing.model_validate(configured)
            except ValueError:
                pass
        return self.default_pricing(model)

    def configured_model(self) -> str:
        """The model this side uses by default (the answering side's first model)."""
        return str(self.side_config.model_name)

    def estimate_cost(self, usage: Usage) -> Optional[Cost]:
        """What ``usage`` consumed, with its cost at list prices when the model has one.

        None for a free provider: its calls are never billed. A paid provider's call is
        always recorded (``amount`` None without a known price), so the user's own prices
        can still be applied to it.
        """
        if self.cost == "free":
            return None
        prices = self.pricing(usage.model)
        return Cost(
            side=usage.side,
            provider=self.id,
            provider_label=self.label,
            model=usage.model,
            amount=prices.price(usage) if prices is not None else None,
            currency=prices.currency if prices is not None else "USD",
            requests=usage.requests,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            seconds=usage.seconds,
            pricing=prices,
            note=self.cost_note,
        )

    # -- health ----------------------------------------------------------------

    def health(self, creds: Credentials) -> Optional[Health]:
        """Readiness of this side. None means "this provider has no health concept"."""
        return None

    def classify_http_error(self, status: int, body: str) -> Optional[Literal["cold", "paused"]]:
        """Tell the query path what an HTTP error from the endpoint means, if known."""
        return None

    # -- provisioning and cost control -----------------------------------------

    def provision(
        self, ctx: ProvisionContext, progress: Callable[[str], None]
    ) -> dict[str, str]:
        """Create or wake this side's remote resources (idempotent).

        Returns ``{shared_base_url_env: url}`` only for a provider that cannot
        derive its URL from the key; ``{}`` otherwise. Must report steps
        through ``progress`` and respect ``ctx.deadline``.
        """
        raise NotImplementedError(f"{self.id} does not support provisioning")

    def teardown(self, ctx: ProvisionContext) -> None:
        """Delete this side's remote resources (decommissioning; CLI only)."""
        raise NotImplementedError(f"{self.id} does not support teardown")

    def suspend(self, ctx: ProvisionContext, progress: Callable[[str], None]) -> None:
        """Stop all billing and block wake-ups until resumed (idempotent).

        Keeps the resources and their URLs. Resume is ``provision()``.
        """
        raise NotImplementedError(f"{self.id} does not support suspend")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def match_pricing(table: Sequence[tuple[str, Pricing]], model: str) -> Optional[Pricing]:
    """The first entry of ``(model-name glob, prices)`` pairs that matches ``model`` (case-insensitive)."""
    name = (model or "").lower()
    for pattern, prices in table:
        if fnmatch.fnmatchcase(name, pattern.lower()):
            return prices
    return None


def register_provider(cls: type[Provider]) -> type[Provider]:
    """Add a provider class to the registry under its own ``id`` (idempotent).

    Plugins do not call this themselves: they list the class in an
    ``InferenceProvider`` capability and the plugin host registers it.

    Raises:
        ValueError: another class already uses the id, or the class declares none.
    """
    if "id" not in cls.__dict__ and cls is not Provider:
        raise ValueError(f"{cls.__name__} declares no provider id of its own")
    existing = PROVIDERS.get(cls.id)
    if existing is not None and existing is not cls:
        raise ValueError(f"Duplicate provider id {cls.id!r}")
    PROVIDERS[cls.id] = cls
    return cls


# The base class is the generic provider, part of core (the plugin host lists it
# under the built-in plugin "core").
register_provider(Provider)
GenericProvider = Provider


def quota_from(meters: list[Meter], note: str) -> Optional[UiQuota]:
    """A :class:`UiQuota` from the meter closest to running out (``note`` explains the combination)."""
    usable = [m for m in meters if m.limit > 0]
    if not usable:
        return None
    worst = max(usable, key=lambda m: (m.limit - m.remaining) / m.limit)
    used_pct = round((worst.limit - worst.remaining) * 100 / worst.limit)
    level = "error" if worst.remaining <= 0 or used_pct >= 95 else "warn" if used_pct >= 75 else "ok"
    period = {"minute": " per-minute", "hour": " hourly", "day": " daily", "month": " monthly"}.get(worst.period or "", "")
    text = f"{used_pct} % of the{period} quota used ({worst.remaining} {worst.unit} left)"
    if note:
        text += f"; {note}"
    return UiQuota(used_pct=used_pct, level=level, text=text, meters=[m.model_dump() for m in usable])
