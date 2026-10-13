"""The ``Provider`` base class, which is also the ``generic`` provider.

One instance serves one side (embedding or LLM) of one preset. Every hook has
a default that implements the generic behaviour for "any OpenAI-compatible
HTTP API" (``/v1/embeddings``, ``/v1/chat/completions``, bearer-key auth), so
``GenericProvider`` is just this class registered under the id ``generic``.
Vendor providers subclass it and override only what differs, calling
``super()`` for the rest.

See docs/superpowers/specs/2026-10-10-huggingface-preset-and-provisioner-adapters-design.md
(section A2) for the contract and docs/providers.md for how to add one.
"""

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, ClassVar, Literal, Mapping, Optional

from pydantic import BaseModel

from backend.providers.types import (
    Credentials,
    Health,
    Meter,
    ModelInfo,
    NoOptions,
    ProviderDescriptor,
    ProvisionContext,
    Scope,
    Side,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from backend.config.presets import HardwarePreset

#: Registry of provider classes by id, filled by ``Provider.__init_subclass__``.
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
    #: True when ``live_models()`` can return a list (so a per-request model name is accepted).
    has_live_models: ClassVar[bool] = False
    #: Wire protocol of the LLM side; core implements the protocols, not vendors.
    llm_api: ClassVar[Literal["openai", "anthropic"]] = "openai"
    #: Timeout (seconds) of the management-API client created by ``_http()``.
    http_timeout: ClassVar[float] = 30.0

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        # Only register a class that declares its own id.
        if "id" in cls.__dict__:
            if cls.id in PROVIDERS and PROVIDERS[cls.id] is not cls:
                raise ValueError(f"Duplicate provider id {cls.id!r}")
            PROVIDERS[cls.id] = cls

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
        """The ``EmbeddingConfig`` or ``LLMConfig`` this instance serves."""
        return self.preset.embedding if self.side == "embedding" else self.preset.llm

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

        Appended to "endpoint unavailable" errors and shown in Preferences.
        Core text stays vendor-neutral.
        """
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


# The base class is the generic provider; register it explicitly because
# __init_subclass__ only fires for subclasses.
PROVIDERS[Provider.id] = Provider
GenericProvider = Provider
