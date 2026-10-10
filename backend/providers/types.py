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

Side = Literal["embedding", "llm"]
Scope = Literal["user", "shared", "managed"]
HealthStatus = Literal["ready", "cold", "throttled", "paused", "unreachable"]

SIDES: tuple[Side, ...] = ("embedding", "llm")


class ProviderConfigError(ValueError):
    """A preset's provider configuration breaks a load-time rule.

    The message names the rule and the side, so the log and the preset
    listing can say exactly why the preset is unavailable.
    """


class ProvisionError(RuntimeError):
    """Raised by a provider when creating, waking or pausing an endpoint fails."""


class ProvisionTimeout(ProvisionError):
    """The job ran past its deadline."""


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
    """

    status: HealthStatus
    detail: str = ""


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


class Credentials(BaseModel):
    """The key of whoever owns the resource, and the endpoint URL if known."""

    api_key: Optional[str] = None
    base_url: Optional[str] = None


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


class ProvisioningDescriptor(BaseModel):
    credential: Optional[CredentialDescriptor] = None
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
