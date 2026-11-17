"""Shared types for the provider layer (see backend/providers/base.py).

These are the data shapes providers exchange with core: health, usage meters,
credentials, the provisioning context and the descriptor the plugin renders.
Everything here is vendor-neutral.
"""

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:  # pragma: no cover - typing only
    from backend.config.presets import HardwarePreset

Side = Literal["embedding", "llm", "rerank"]
Scope = Literal["user", "shared", "managed"]
HealthStatus = Literal["ready", "cold", "throttled", "paused", "unreachable"]

#: The sides every preset has.
SIDES: tuple[Side, ...] = ("embedding", "llm")
#: Sides a preset may leave out (``preset.rerank`` is None when it has no re-ranking step).
OPTIONAL_SIDES: tuple[Side, ...] = ("rerank",)


def side_config(preset: "HardwarePreset", side: Side):
    """The ``EmbeddingConfig``, ``LLMConfig`` or ``RerankConfig`` of one side (None for an absent optional side)."""
    if side == "embedding":
        return preset.embedding
    if side == "llm":
        return preset.llm
    return getattr(preset, side, None)


def preset_sides(preset: "HardwarePreset") -> tuple[Side, ...]:
    """The sides ``preset`` actually has, in display order."""
    return SIDES + tuple(s for s in OPTIONAL_SIDES if side_config(preset, s) is not None)


class ProviderConfigError(ValueError):
    """A preset's provider configuration breaks a load-time rule.

    The message names the rule and the side, so the log and the preset
    listing can say exactly why the preset is unavailable.
    """


class ProvisionError(RuntimeError):
    """Raised by a provider when creating, waking or pausing an endpoint fails."""


class ProvisionTimeout(ProvisionError):
    """The job ran past its deadline."""


class SessionsUnavailable(RuntimeError):
    """A provider's per-user sessions cannot be used here (a library is missing, or the server is not running)."""


class LoginRateLimited(RuntimeError):
    """Too many session login attempts for one user in a short time."""


class SessionLoginFailed(RuntimeError):
    """The provider rejected a session login (wrong username, password or one-time code)."""


class ProviderOptions(BaseModel):
    """Base of every provider's ``Options`` model: an unknown option is an error.

    A typo in a preset's ``provider.options`` must be rejected at load time
    rather than silently ignored.
    """

    model_config = ConfigDict(extra="forbid")


class NoOptions(ProviderOptions):
    """Options model for providers that take none."""


class Health(BaseModel):
    """Readiness of one side's endpoint, in vendor-neutral terms.

    ``cold`` wakes on the next request by itself; ``throttled`` means the
    provider has no capacity right now; ``paused`` was stopped on purpose and
    will not wake until resumed; ``unreachable`` includes "nothing provisioned
    yet" and any provider-side failure.

    ``waking`` qualifies ``cold``: the endpoint is already coming up (a worker is
    loading, a start job runs, a wake-up request was just sent), so another
    request to wake it would change nothing.
    """

    status: HealthStatus
    detail: str = ""
    waking: bool = False


class ModelInfo(BaseModel):
    """One model from a provider's live model list (preferred model first)."""

    id: str
    demand: Optional[int] = None
    availability: Optional[str] = None


class Meter(BaseModel):
    """One quota the UI can draw as a bar ("123 requests left/hour")."""

    id: str
    side: Side
    unit: str
    period: Optional[str] = None
    limit: int
    remaining: int
    resets_at: Optional[str] = None
    as_of: Optional[str] = None
    source: Literal["run", "cache"] = "run"


class Usage(BaseModel):
    """What one inference call consumed, as measured by core (the input of cost estimates)."""

    side: Side
    model: str
    requests: int = 1
    input_tokens: int = 0
    output_tokens: int = 0
    #: Wall-clock seconds of the call (what time-billed endpoints charge for, at least).
    seconds: float = 0.0


class Pricing(BaseModel):
    """List prices of one model, in ``currency``. Every rate that applies is added up.

    A preset can set it per side in ``model_kwargs.pricing``: either one ``Pricing``
    object, or an object mapping model-name glob patterns to one (the first matching
    pattern wins). Without it the provider's ``default_pricing`` is used.
    """

    model_config = ConfigDict(extra="forbid")

    currency: str = "USD"
    input_per_million_tokens: float = Field(default=0.0, ge=0)
    output_per_million_tokens: float = Field(default=0.0, ge=0)
    per_request: float = Field(default=0.0, ge=0)
    #: For endpoints billed by compute time (e.g. serverless GPU workers).
    per_second: float = Field(default=0.0, ge=0)

    def price(self, usage: "Usage") -> float:
        """The cost of ``usage`` at these rates."""
        return (
            usage.input_tokens * self.input_per_million_tokens / 1_000_000
            + usage.output_tokens * self.output_per_million_tokens / 1_000_000
            + usage.requests * self.per_request
            + usage.seconds * self.per_second
        )


class Cost(BaseModel):
    """What one side of one paid provider consumed (one call or a sum of calls).

    Whether it costs money, and how much, depends on the user's contract: the plugin
    prices it with the billing mode and prices the user set (see the account card's
    Billing fields), falling back to ``pricing``, the provider's list prices.
    """

    side: Side
    provider: str = ""
    provider_label: str = ""
    model: str = ""
    #: The cost at list prices; None when the provider knows no price for the model.
    amount: Optional[float] = None
    currency: str = "USD"
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    #: The list prices ``amount`` was computed with, the defaults of the user's own prices.
    pricing: Optional[Pricing] = None
    #: Always true: computed from prices, never read from the provider's bill.
    estimated: bool = True
    #: What the estimate leaves out (e.g. idle time a GPU worker is billed for).
    note: str = ""


class Credentials(BaseModel):
    """The key of whoever owns the resource, and the endpoint URL if known."""

    api_key: Optional[str] = None
    base_url: Optional[str] = None
    #: Zotero user id of the caller, for a provider whose endpoints belong to a per-user session.
    user_id: Optional[int] = None


@dataclass
class ProvisionContext:
    """Everything a provider needs to provision, pause or tear down one side."""

    side: Side
    preset: "HardwarePreset"
    credential: Optional[str]
    data_path: Path
    recreate: bool = False
    skip_warmup: bool = False
    #: Absolute ``time.monotonic()`` value after which the job must stop.
    deadline: Optional[float] = None
    #: Extra fields reserved for providers (never interpreted by core).
    extra: dict = field(default_factory=dict)

    def remaining(self) -> Optional[float]:
        """Seconds left before the deadline, or None when there is none."""
        if self.deadline is None:
            return None
        return self.deadline - time.monotonic()

    def check_deadline(self) -> None:
        """Raise ProvisionTimeout if the deadline has passed."""
        left = self.remaining()
        if left is not None and left <= 0:
            raise ProvisionTimeout(f"{self.side} job exceeded its deadline")


class CredentialDescriptor(BaseModel):
    """The one-time credential the provisioning UI asks for."""

    env: str
    label: str
    help: str = ""
    pattern: Optional[str] = None
    optional: bool = True


class SessionLoginField(BaseModel):
    """One input of a session login form (rendered by the plugin as given)."""

    name: str
    label: str
    #: HTML input type: ``text`` or ``password``.
    type: Literal["text", "password"] = "text"
    #: HTML ``autocomplete`` value, so password managers and one-time-code autofill work.
    autocomplete: Optional[str] = None
    optional: bool = False
    help: str = ""


class SessionLoginDescriptor(BaseModel):
    """A provider whose endpoints run in a per-user session the user logs in to.

    The plugin renders these fields instead of a key field and posts them to
    ``POST /api/config/session/login``; ``GET /api/config/session`` reports the state.
    """

    title: str
    fields: list[SessionLoginField]
    help: str = ""


class ProvisioningDescriptor(BaseModel):
    credential: Optional[CredentialDescriptor] = None
    login: Optional[SessionLoginDescriptor] = None
    hint: Optional[str] = None


class ProviderDescriptor(BaseModel):
    """What the plugin needs to render one side's section. Never holds secrets."""

    id: str
    label: str
    key_scope: Scope = "user"
    #: Filled in per caller by the API layer (admin / user / shared rules).
    operable_by_caller: bool = False
    supports_provisioning: bool = False
    supports_suspend: bool = False
    provisioning: Optional[ProvisioningDescriptor] = None
    unavailable_hint: Optional[str] = None
