"""MPCDF LLM Inference Service provider (``llm.mpcdf.mpg.de``).

Each endpoint is an ephemeral Slurm job of up to 8 hours. Every side is per user
(scope ``user``): each user logs in with their own MPCDF account (username,
password, one-time code) and the endpoint runs as a job on that account, started,
renewed and idle-cancelled through the ``mpcdf-llm`` library
(``backend.services.mpcdf_sessions``). The service's terms do not allow one
account to serve other people, so there is no admin-shared variant.
"""

import logging
from datetime import timedelta
from typing import Callable, Optional

from pydantic import Field

from backend.providers.base import Provider
from backend.providers.types import (
    Credentials,
    Health,
    ProviderDescriptor,
    ProviderOptions,
    ProvisionContext,
    ProvisionError,
    ProvisioningDescriptor,
    SessionLoginDescriptor,
    SessionLoginField,
)

logger = logging.getLogger(__name__)

SERVICE_URL = "https://llm.mpcdf.mpg.de"


class MpcdfOptions(ProviderOptions):
    """Job settings of a per-user endpoint. Checked against the
    service's form options when the endpoint is first started."""

    machine: str = "Viper"
    gpu_type: str = "mi300a"
    gpus: int = Field(default=1, ge=1, le=2)
    #: Slurm run time per job; the library renews the job before it ends while it is in use.
    time_hours: int = Field(default=8, ge=1, le=8)
    #: Cancel the endpoint after this long without a query.
    idle_minutes: int = Field(default=60, ge=10)
    build_timeout_minutes: int = Field(default=30, ge=5)
    image: Optional[str] = None
    #: vLLM arguments after the model; no quotes (the service strips them).
    #: CUDA graph capture is very slow on the MI300A, hence ``--enforce-eager``.
    extra_args: list[str] = Field(default_factory=lambda: ["--enforce-eager"])


LOGIN = SessionLoginDescriptor(
    title="MPCDF account",
    help=(
        "The endpoints run as jobs on your own MPCDF account. The password and one-time code are "
        "used for this login only; the session lasts about 12 hours and logging in to the MPCDF web "
        "interface ends it."
    ),
    fields=[
        SessionLoginField(name="username", label="MPCDF username", autocomplete="username"),
        SessionLoginField(name="password", label="Password", type="password", autocomplete="current-password"),
        SessionLoginField(name="otp", label="One-time code", autocomplete="one-time-code"),
        SessionLoginField(
            name="hf_token", label="Hugging Face token", type="password", optional=True,
            help="Every vLLM job on the service needs one to download the model (read access is enough). "
                 "Stored encrypted for renewals; leave empty to keep the one entered before.",
        ),
    ],
)


class MpcdfProvider(Provider):
    id = "mpcdf"
    label = "MPCDF LLM Inference Service"
    key_scopes = frozenset({"user"})
    default_scope = "user"
    supported_sides = frozenset({"embedding", "llm", "rerank"})
    Options = MpcdfOptions
    #: A job either answers or it does not; keep the probe short.
    http_timeout = 10.0

    supports_provisioning = True
    supports_suspend = True
    #: Provisioning runs in the caller's own login session, not with a key.
    provisioning_needs_key = False
    uses_session_endpoints = True

    @property
    def preset_name(self) -> str:
        """Name of this side's preset in the user's ``mpcdf-llm`` store."""
        return f"zotero-rag-{self.side}"

    def _sessions(self):
        from backend.services import mpcdf_sessions

        if not mpcdf_sessions.available():
            raise ProvisionError("the mpcdf-llm library is not installed on this server")
        return mpcdf_sessions.get_sessions()

    def library_preset(self):
        """The ``mpcdf_llm`` preset for this side, from the preset's model and options."""
        from mpcdf_llm.models import Framework
        from mpcdf_llm.presets import Preset

        o: MpcdfOptions = self.options  # type: ignore[assignment]
        cfg = self.side_config
        model = getattr(cfg, "model_name", None) or cfg.model_names[0]
        return Preset(
            name=self.preset_name,
            description=f"zotero-rag {self.side} endpoint",
            framework=Framework.VLLM,
            model=model,
            image=o.image,
            extra_args=list(o.extra_args),
            machine=o.machine,
            gpu_type=o.gpu_type,
            gpus=o.gpus,
            time=timedelta(hours=o.time_hours),
            idle_timeout=timedelta(minutes=o.idle_minutes),
            build_timeout=timedelta(minutes=o.build_timeout_minutes),
        )

    def describe(self) -> ProviderDescriptor:
        descriptor = super().describe()
        descriptor.provisioning = ProvisioningDescriptor(
            login=LOGIN,
            hint="Starting a job takes a few minutes in the queue, plus loading the model.",
        )
        return descriptor

    def session_login(self, user_id: Optional[int], values: dict[str, str]) -> dict:
        sessions = self._sessions()
        return sessions.call(lambda: sessions.login(
            user_id, values.get("username", ""), values.get("password", ""), values.get("otp", ""),
            hf_token=values.get("hf_token") or None,
        ), timeout=60.0)

    def session_logout(self, user_id: Optional[int]) -> None:
        sessions = self._sessions()
        sessions.call(lambda: sessions.logout(user_id))

    def session_status(self, user_id: Optional[int]) -> Optional[dict]:
        sessions = self._sessions()
        return sessions.call(lambda: sessions.status(user_id))

    def provision(self, ctx: ProvisionContext, progress: Callable[[str], None]) -> dict[str, str]:
        import time as _time

        sessions = self._sessions()
        user_id = ctx.extra.get("user_id")
        status = sessions.call(lambda: sessions.status(user_id))
        if status["state"] != "ok":
            raise ProvisionError("Log in with your MPCDF account first.")
        if not status.get("hf_token_set"):
            raise ProvisionError("Enter a Hugging Face token with your MPCDF login; every vLLM job needs one.")
        preset = self.library_preset()
        progress(f"starting {preset.model} on {preset.gpus} x {preset.gpu_type}")
        sessions.call(lambda: sessions.put_preset(user_id, preset), timeout=60.0)
        left = ctx.remaining()
        wait = max(30.0, left) if left is not None else 3600.0
        deadline = _time.monotonic() + wait
        try:
            sessions.call(lambda: sessions.start_endpoint(user_id, preset.name, progress, deadline=deadline), timeout=wait + 5)
        except TimeoutError:
            progress("still starting; the endpoint is used as soon as it is up")
        except RuntimeError as exc:
            raise ProvisionError(str(exc)) from exc
        return {}

    def suspend(self, ctx: ProvisionContext, progress: Callable[[str], None]) -> None:
        sessions = self._sessions()
        user_id = ctx.extra.get("user_id")
        sessions.call(lambda: sessions.stop_endpoint(user_id, self.preset_name))
        progress("endpoint stopped")

    def session_endpoint(self, user_id: Optional[int]) -> Optional[Credentials]:
        """The caller's running endpoint (credentials change at every renewal, so never cached)."""
        sessions = self._sessions()
        creds = sessions.call(lambda: sessions.credentials(user_id, self.preset_name))
        if creds is None:
            return None
        return Credentials(base_url=creds.base_url, api_key=creds.api_key, user_id=user_id)

    def session_activity(self, user_id: Optional[int]) -> None:
        """Report use so the library keeps renewing the endpoint instead of idle-cancelling it."""
        sessions = self._sessions()
        sessions.call(lambda: sessions.touch(user_id, self.preset_name))

    def rerank_credentials(self, request, refresh: bool = False) -> Optional[Credentials]:
        from backend.services.mpcdf_sessions import SessionsUnavailable
        from backend.services.rerank import RerankerUnavailable

        try:
            return self.session_endpoint(request.user_id)
        except (SessionsUnavailable, ProvisionError) as exc:
            raise RerankerUnavailable(str(exc)) from exc

    def rerank_activity(self, request) -> None:
        self.session_activity(request.user_id)

    def unavailable_hint(self) -> Optional[str]:
        from backend.services import mpcdf_sessions

        if not mpcdf_sessions.available():
            return "This server cannot start MPCDF jobs (the mpcdf-llm library is not installed)."
        return "Log in with your MPCDF account and start the endpoint; it runs as a job on your own account."

    def health(self, creds: Credentials) -> Optional[Health]:
        """Readiness of the caller's endpoint, from their session.

        ``ready``, ``cold`` while its job starts, else ``unreachable`` with what to do.
        Never raises.
        """
        try:
            sessions = self._sessions()
            status, detail = sessions.call(lambda: sessions.endpoint_state(creds.user_id, self.preset_name))
        except Exception as exc:
            return Health(status="unreachable", detail=str(exc) or type(exc).__name__)
        return Health(status=status, detail=detail)
