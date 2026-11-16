"""Fake providers for core tests. Their state lives in class attributes so tests can
set it (``TestCloudProvider.paused``, ``TestSessionProvider.sessions``) and patch
methods (``patch.object``). None of them makes a network call on its own.

The five cover the provider shapes core has to handle:

- ``testvendor``: a free service with per-user keys, a live model list and
  hour/day quota headers (``x-ratelimit-{limit,remaining}-{hour,day}``).
- ``testother``: a paid service with per-user keys and nothing else.
- ``testmessages``: a paid LLM-only service on the Anthropic Messages protocol.
- ``testsession``: a free service whose endpoints run in each user's own login
  session (no key; provisioning starts the session's endpoint).
- ``testcloud``: a paid service whose endpoints live in the key owner's account,
  provisioned, paused and resumed on demand, the URL derived from the key. Unlike
  real pay-per-use vendors it also allows the ``managed`` scope, so core's
  admin-managed paths have a provider to run on.
"""

from typing import Callable, ClassVar, Literal, Mapping, Optional

from backend.providers import Credentials, Health, ModelInfo, Provider, ProvisionContext
from backend.providers.base import _utc_now
from backend.providers.types import (
    CredentialDescriptor,
    Meter,
    ProviderDescriptor,
    ProvisionError,
    ProvisioningDescriptor,
    SessionLoginDescriptor,
    SessionLoginField,
)
from backend.providers.usage import lowercase_headers, make_meter

#: Key format of ``testcloud`` keys (checked like a real vendor's key pattern).
TESTCLOUD_KEY_PATTERN = r"^tck_[A-Za-z0-9]+$"
#: Endpoint URL format of ``testcloud`` (``https://testcloud.invalid/<endpoint>/v1``).
TESTCLOUD_BASE_URL_PATTERN = r"^https://testcloud\.invalid/[A-Za-z0-9_-]+/v1$"


class TestVendorProvider(Provider):
    """A free service with per-user keys, a live model list and hour/day quota headers."""

    __test__ = False  # not a pytest test class
    id = "testvendor"
    label = "Test vendor"
    cost = "free"
    key_scopes = frozenset({"user"})
    default_scope = "user"
    has_live_models = True
    #: The live model list ``live_models`` reports (tests may replace it).
    models: ClassVar[list[str]] = ["test-llm-large", "test-llm-small"]

    def default_key_env(self) -> Optional[str]:
        return "TESTVENDOR_API_KEY"

    def key_docs_url(self, env_var: str) -> Optional[str]:
        if env_var == "TESTVENDOR_API_KEY":
            return "https://testvendor.invalid/keys"
        return super().key_docs_url(env_var)

    def live_models(self, base_url: str, api_key: str) -> Optional[list[ModelInfo]]:
        if self.side != "llm":
            return None
        return [ModelInfo(id=m, availability="available") for m in self.models]

    def quota_summary(self, meters: list[Meter]):
        """Hour and day limits; the one closer to running out counts (as some vendors do)."""
        from backend.providers.base import quota_from

        periods = [m for m in meters if m.period in ("hour", "day")]
        return quota_from(periods, "hourly and daily quota combined") if periods else super().quota_summary(meters)

    def parse_usage(
        self,
        headers: Mapping[str, str],
        *,
        as_of: Optional[str] = None,
        source: Literal["run", "cache"] = "run",
    ) -> list[Meter]:
        stamp = as_of or _utc_now()
        meters = super().parse_usage(headers, as_of=stamp, source=source)
        h = lowercase_headers(headers or {})
        for period in ("hour", "day"):
            try:
                limit = int(float(h.get(f"x-ratelimit-limit-{period}", "")))
                remaining = int(float(h.get(f"x-ratelimit-remaining-{period}", "")))
            except ValueError:
                continue
            meter = make_meter(side=self.side, unit="requests", period=period, limit=limit,
                               remaining=remaining, as_of=stamp, source=source)
            if meter:
                meters.append(meter)
        return meters


class TestOtherProvider(Provider):
    """A paid service with per-user keys and no extras."""

    __test__ = False
    id = "testother"
    label = "Test other"
    cost = "paid"
    key_scopes = frozenset({"user"})
    default_scope = "user"

    def default_key_env(self) -> Optional[str]:
        return "TESTOTHER_API_KEY"


class TestMessagesProvider(Provider):
    """A paid LLM-only service that speaks the Anthropic Messages protocol."""

    __test__ = False
    id = "testmessages"
    label = "Test messages"
    cost = "paid"
    supported_sides = frozenset({"llm"})
    key_scopes = frozenset({"user"})
    default_scope = "user"
    llm_api = "anthropic"

    def default_key_env(self) -> Optional[str]:
        return "TESTMESSAGES_API_KEY"


LOGIN = SessionLoginDescriptor(
    title="Test account",
    help="The endpoints run in your own test session.",
    fields=[
        SessionLoginField(name="username", label="Username", autocomplete="username"),
        SessionLoginField(name="password", label="Password", type="password", autocomplete="current-password"),
    ],
)


class TestSessionProvider(Provider):
    """A free service whose endpoints run in each user's own login session."""

    __test__ = False
    id = "testsession"
    label = "Test session service"
    cost = "free"
    key_scopes = frozenset({"user"})
    default_scope = "user"
    supported_sides = frozenset({"embedding", "llm", "rerank"})
    supports_provisioning = True
    supports_suspend = True
    provisioning_needs_key = False
    uses_session_endpoints = True
    #: Session state per user id (``{"state": "ok"}`` when logged in).
    sessions: ClassVar[dict] = {}
    #: Sides whose endpoint runs, as ``(user_id, side)``.
    running: ClassVar[set] = set()

    def describe(self) -> ProviderDescriptor:
        descriptor = super().describe()
        descriptor.provisioning = ProvisioningDescriptor(login=LOGIN, hint="Starting takes a moment.")
        return descriptor

    def session_login(self, user_id: Optional[int], values: dict[str, str]) -> dict:
        self.sessions[user_id] = {"state": "ok", "username": values.get("username", "")}
        return self.sessions[user_id]

    def session_logout(self, user_id: Optional[int]) -> None:
        self.sessions.pop(user_id, None)

    def session_status(self, user_id: Optional[int]) -> Optional[dict]:
        return self.sessions.get(user_id)

    def provision(self, ctx: ProvisionContext, progress: Callable[[str], None]) -> dict[str, str]:
        user_id = ctx.extra.get("user_id")
        if (self.sessions.get(user_id) or {}).get("state") != "ok":
            raise ProvisionError("Log in with your test account first.")
        progress(f"starting {ctx.side}")
        self.running.add((user_id, ctx.side))
        return {}

    def suspend(self, ctx: ProvisionContext, progress: Callable[[str], None]) -> None:
        self.running.discard((ctx.extra.get("user_id"), ctx.side))
        progress("endpoint stopped")

    def session_endpoint(self, user_id: Optional[int]) -> Optional[Credentials]:
        if (user_id, self.side) not in self.running:
            return None
        return Credentials(base_url=f"https://testsession.invalid/{user_id}/{self.side}/v1", api_key="session",
                           user_id=user_id)

    def unavailable_hint(self) -> Optional[str]:
        return "Log in with your test account (which starts the endpoints)."

    def health(self, creds: Credentials) -> Optional[Health]:
        if (creds.user_id, self.side) in self.running:
            return Health(status="ready")
        return Health(status="unreachable", detail="log in first")


class TestCloudProvider(Provider):
    """A paid service whose endpoints live in the key owner's account: provisioned,
    paused and resumed on demand, the URL derived from the key."""

    __test__ = False
    id = "testcloud"
    label = "Test cloud"
    cost = "paid"
    supported_sides = frozenset({"embedding", "llm", "rerank"})
    key_scopes = frozenset({"user", "managed"})
    default_scope = "user"
    supports_provisioning = True
    supports_suspend = True
    derives_endpoint_url = True
    #: Keys whose endpoints are paused, as ``(side, key)`` (shared by every instance).
    paused: ClassVar[set] = set()
    #: Sides provisioned so far, as ``(side, credential)``.
    provisioned: ClassVar[list] = []

    def default_key_env(self) -> Optional[str]:
        return "TESTCLOUD_API_KEY"

    def apply_defaults(self) -> None:
        kwargs = self.side_config.model_kwargs
        if self.scope == "user":
            kwargs.setdefault("api_key_pattern", TESTCLOUD_KEY_PATTERN)
        else:
            kwargs.setdefault("shared_api_key_pattern", TESTCLOUD_KEY_PATTERN)
            if "shared_base_url_env" in kwargs:
                kwargs.setdefault("shared_base_url_pattern", TESTCLOUD_BASE_URL_PATTERN)
        super().apply_defaults()

    def describe(self) -> ProviderDescriptor:
        descriptor = super().describe()
        descriptor.provisioning = ProvisioningDescriptor(
            credential=CredentialDescriptor(env="TESTCLOUD_API_KEY", label="Provisioning API key", optional=True),
            hint="A cold start takes a moment.",
        )
        return descriptor

    def endpoint_url(self, api_key: str) -> Optional[str]:
        return f"https://testcloud.invalid/{self.side}/v1" if api_key else None

    def is_paused(self, api_key: str) -> bool:
        return (self.side, api_key) in self.paused

    def health(self, creds: Credentials) -> Optional[Health]:
        if not creds.api_key:
            return Health(status="unreachable", detail="not configured")
        if not creds.base_url:
            return Health(status="unreachable", detail="not provisioned")
        if self.is_paused(creds.api_key):
            return Health(status="paused")
        return Health(status="ready")

    def unavailable_hint(self) -> Optional[str]:
        return "Start the endpoint in the Configure preset dialog."

    def classify_http_error(self, status: int, body: str) -> Optional[Literal["cold", "paused"]]:
        if status == 409 and "PAUSED" in (body or ""):
            return "paused"
        return None

    def provision(self, ctx: ProvisionContext, progress: Callable[[str], None]) -> dict[str, str]:
        progress(f"provisioning {ctx.side}")
        self.paused.discard((ctx.side, ctx.credential))
        self.provisioned.append((ctx.side, ctx.credential))
        env = self.side_config.model_kwargs.get("shared_base_url_env")
        return {env: f"https://testcloud.invalid/{ctx.side}/v1"} if env else {}

    def suspend(self, ctx: ProvisionContext, progress: Callable[[str], None]) -> None:
        progress(f"pausing {ctx.side}")
        self.paused.add((ctx.side, ctx.credential))

    def teardown(self, ctx: ProvisionContext) -> None:
        self.paused.discard((ctx.side, ctx.credential))


def reset_fake_state() -> None:
    """Clear the fakes' class-level state (call from a test's setUp)."""
    TestCloudProvider.paused.clear()
    TestCloudProvider.provisioned.clear()
    TestSessionProvider.sessions.clear()
    TestSessionProvider.running.clear()
    TestVendorProvider.models = ["test-llm-large", "test-llm-small"]
