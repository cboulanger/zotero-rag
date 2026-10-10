"""Hugging Face Inference Endpoints (dedicated) provider.

Each side owns one endpoint named ``zotero-rag-<side>`` in the token owner's
namespace. Unlike RunPod there is no template: the image and environment live in
the endpoint itself, readiness is the endpoint's ``status.state`` from the
management API, and the endpoint URL is derived from the token (no URL is
stored). One Hugging Face token serves both management and inference.

Facts this module relies on were measured live (see
docs/history/implementation/provider-layer-phase-0-spike.md): TEI rejects client
batches above 32 unless ``MAX_CLIENT_BATCH_SIZE`` is set in ``model.env``; scale to
zero is opt-in (``minReplica`` 0 plus an explicit timeout of at least 15 minutes);
a scaled-to-zero or starting endpoint answers 503, a paused one answers 400
"endpoint is paused"; vLLM serves the repository id while TEI answers
``/repository``. TEI image tags for GPUs other than the T4 are not verified live.

Routes (all under ``/endpoint/{namespace}``): ``POST`` create, ``GET /{name}``,
``DELETE /{name}``, ``POST /{name}/pause|resume``.
"""

import logging
import time
from typing import Any, Callable, Literal, Optional

from pydantic import Field

from backend.providers.base import Provider
from backend.providers.types import (
    CredentialDescriptor,
    Credentials,
    Health,
    ProviderConfigError,
    ProviderDescriptor,
    ProviderOptions,
    ProvisionContext,
    ProvisioningDescriptor,
    ProvisionError,
)

logger = logging.getLogger(__name__)

API_URL = "https://api.endpoints.huggingface.cloud/v2"
WHOAMI_URL = "https://huggingface.co/api/whoami-v2"
TOKEN_PATTERN = r"^hf_[A-Za-z0-9]+$"
TOKEN_ENV = "HF_TOKEN"
TOKENS_PAGE = "https://huggingface.co/settings/tokens"

#: Poll interval and overall bound while an endpoint starts.
POLL_SECONDS = 10
WAIT_MAX_SECONDS = 20 * 60
WARMUP_MAX_SECONDS = 180
WARMUP_RETRY_SECONDS = 5
#: TEI's default client batch limit is 32; raised so the preset's batch size fits.
MIN_CLIENT_BATCH_SIZE = 128

# Indirection so tests can run the loops without really waiting.
_sleep: Callable[[float], None] = time.sleep
_monotonic: Callable[[], float] = time.monotonic

#: Endpoint states -> vendor-neutral health. ``paused`` was stopped on purpose.
_STATE_HEALTH: dict[str, str] = {
    "running": "ready",
    "scaledToZero": "cold",
    "pending": "cold",
    "initializing": "cold",
    "updating": "cold",
    "paused": "paused",
    "failed": "unreachable",
    "updateFailed": "unreachable",
}

#: TEI image tag per GPU architecture (only ``nvidia-t4`` was verified live).
_TEI_TAGS = {
    "nvidia-t4": "turing-1.8",
    "nvidia-a10g": "86-1.8",
    "nvidia-l4": "89-1.8",
    "nvidia-l40s": "89-1.8",
    "nvidia-a100": "1.8",
}
_TEI_IMAGE = "ghcr.io/huggingface/text-embeddings-inference"

_SIDE_DEFAULTS = {
    "embedding": {"engine": "tei", "instance": "nvidia-t4", "task": "sentence-embeddings"},
    "llm": {"engine": "vllm", "instance": "nvidia-a10g", "task": "text-generation"},
}


class HuggingFaceOptions(ProviderOptions):
    #: Account (user or organisation) that owns the endpoint; default: the token's own user.
    namespace: Optional[str] = None
    vendor: str = "aws"
    region: str = "eu-west-1"
    engine: Optional[Literal["tei", "vllm", "tgi"]] = None
    instance: Optional[str] = None
    instance_size: str = "x1"
    #: Idle minutes before scaling to zero; the API accepts 15 to 2880.
    scale_to_zero_timeout_min: int = Field(default=15, ge=15, le=2880)
    min_replica: int = Field(default=0, ge=0)
    max_replica: int = Field(default=1, ge=1)
    #: Container image override (otherwise chosen from the engine and instance).
    image: Optional[str] = None


class HuggingFaceProvider(Provider):
    id = "huggingface"
    label = "Hugging Face"
    Options = HuggingFaceOptions
    key_scopes = frozenset({"user", "managed"})
    default_scope = "user"
    supports_provisioning = True
    supports_suspend = True
    derives_endpoint_url = True
    http_timeout = 30.0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._namespaces: dict[str, str] = {}
        opts = self.options
        defaults = _SIDE_DEFAULTS[self.side]
        self.engine: str = opts.engine or defaults["engine"]
        self.instance: str = opts.instance or defaults["instance"]
        if self.engine == "tei" and not opts.image and self.instance not in _TEI_TAGS:
            raise ProviderConfigError(
                f"{self.side}: no known text-embeddings-inference image for instance {self.instance!r}; "
                f"set provider.options.image (known instances: {', '.join(sorted(_TEI_TAGS))})"
            )

    # -- naming and payload ------------------------------------------------------

    @property
    def resource_name(self) -> str:
        return f"zotero-rag-{self.side}"

    @property
    def repository(self) -> str:
        cfg = self.side_config
        return cfg.model_name if self.side == "embedding" else cfg.model_names[0]

    def _image(self) -> dict[str, Any]:
        """The ``model.image`` variant for the engine (forwarded as-is by the API)."""
        override = self.options.image
        if self.engine == "tei":
            url = override or f"{_TEI_IMAGE}:{_TEI_TAGS[self.instance]}"
            return {"tei": {"url": url, "port": 80, "healthRoute": "/health", "maxBatchTokens": 16384}}
        if self.engine == "tgi":
            url = override or "ghcr.io/huggingface/text-generation-inference:latest"
            return {"tgi": {"url": url, "port": 80, "healthRoute": "/health"}}
        return {"vLLM": {"url": override or "vllm/vllm-openai:latest", "port": 8000, "healthRoute": "/health"}}

    def _env(self) -> dict[str, str]:
        if self.engine != "tei":
            return {}
        batch = max(MIN_CLIENT_BATCH_SIZE, int(getattr(self.side_config, "batch_size", 0) or 0))
        return {"MAX_CLIENT_BATCH_SIZE": str(batch)}

    def _payload(self) -> dict[str, Any]:
        opts = self.options
        model: dict[str, Any] = {
            "repository": self.repository,
            "framework": "pytorch",
            "task": _SIDE_DEFAULTS[self.side]["task"],
            "image": self._image(),
        }
        env = self._env()
        if env:
            model["env"] = env
        return {
            "name": self.resource_name,
            "type": "authenticated",
            "provider": {"vendor": opts.vendor, "region": opts.region},
            "compute": {
                "accelerator": "gpu",
                "instanceType": self.instance,
                "instanceSize": opts.instance_size,
                "scaling": {
                    "minReplica": opts.min_replica,
                    "maxReplica": opts.max_replica,
                    "scaleToZeroTimeout": opts.scale_to_zero_timeout_min,
                },
            },
            "model": model,
        }

    def _differs(self, existing: dict) -> list[str]:
        """What differs between a live endpoint and the requested configuration."""
        wanted = self._payload()
        diffs = []
        have_model = existing.get("model") or {}
        if have_model.get("repository") != wanted["model"]["repository"]:
            diffs.append(f"repository={have_model.get('repository')!r} vs {wanted['model']['repository']!r}")
        have_compute = existing.get("compute") or {}
        if have_compute.get("instanceType") != self.instance:
            diffs.append(f"instance={have_compute.get('instanceType')!r} vs {self.instance!r}")
        have_provider = existing.get("provider") or {}
        if have_provider.get("region") != self.options.region:
            diffs.append(f"region={have_provider.get('region')!r} vs {self.options.region!r}")
        return diffs

    # -- configuration and description --------------------------------------------

    def apply_defaults(self) -> None:
        kwargs = self.side_config.model_kwargs
        if self.scope == "user":
            kwargs.setdefault("api_key_pattern", TOKEN_PATTERN)
        else:
            kwargs.setdefault("shared_api_key_pattern", TOKEN_PATTERN)
        super().apply_defaults()

    def default_key_env(self) -> Optional[str]:
        return TOKEN_ENV

    def key_docs_url(self, env_var: str) -> Optional[str]:
        if env_var == TOKEN_ENV:
            return TOKENS_PAGE
        return super().key_docs_url(env_var)

    def describe(self) -> ProviderDescriptor:
        kwargs = self.side_config.model_kwargs
        env = kwargs.get("shared_api_key_env" if self.scope != "user" else "api_key_env") or TOKEN_ENV
        descriptor = super().describe()
        descriptor.provisioning = ProvisioningDescriptor(
            credential=CredentialDescriptor(
                env=env,
                label="Provisioning token",
                help=(
                    "Used once to create, resume or pause the endpoints; it is never stored. It needs more "
                    "rights than the HF_TOKEN above: a fine-grained token with \"Manage Inference Endpoints\" "
                    "(and \"Make calls to Inference Endpoints\") for your account, or a classic Write token, "
                    "and the account needs a billing method. Keep the stored HF_TOKEN limited to calling "
                    "endpoints (\"Make calls to Inference Endpoints\"). Leave this empty to use the stored "
                    "token if it already has the management rights."
                ),
                pattern=TOKEN_PATTERN,
                optional=True,
            ),
            hint="A first start can take several minutes while the model is downloaded and loaded.",
        )
        return descriptor

    def unavailable_hint(self) -> Optional[str]:
        return (
            "This preset uses a Hugging Face Inference Endpoint, which may be starting, scaled to zero "
            "or paused - check its status and provision or resume it from Preferences."
        )

    def classify_http_error(self, status: int, body: str) -> Optional[str]:
        text = (body or "").lower()
        if status == 400 and "endpoint is paused" in text:
            return "paused"
        if status == 503:
            return "cold"
        return None

    # -- REST helpers ---------------------------------------------------------------

    def _call(self, token: str, method: str, url: str, json_body: Optional[dict] = None) -> Any:
        return self._http().request(
            method, url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=json_body, timeout=self.http_timeout,
        )

    def _namespace(self, token: str) -> str:
        """The account endpoints live in: the option, else the token owner's user name."""
        if self.options.namespace:
            return self.options.namespace
        if token not in self._namespaces:
            response = self._call(token, "GET", WHOAMI_URL)
            if response.status_code != 200:
                raise ProvisionError(
                    f"Could not resolve the Hugging Face account for this token (HTTP {response.status_code})."
                )
            name = (response.json() or {}).get("name")
            if not name:
                raise ProvisionError("The Hugging Face token did not identify an account.")
            self._namespaces[token] = name
        return self._namespaces[token]

    def _endpoint_path(self, token: str, suffix: str = "") -> str:
        return f"{API_URL}/endpoint/{self._namespace(token)}{suffix}"

    def _get_endpoint(self, token: str) -> Optional[dict]:
        """The endpoint, or None if there is none. Raises ProvisionError on other failures."""
        response = self._call(token, "GET", self._endpoint_path(token, f"/{self.resource_name}"))
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise ProvisionError(f"Hugging Face API GET {self.resource_name} failed: {response.status_code} {response.text}")
        return response.json()

    def _action(self, token: str, verb: str) -> None:
        response = self._call(token, "POST", self._endpoint_path(token, f"/{self.resource_name}/{verb}"))
        if not (200 <= response.status_code < 300):
            raise ProvisionError(f"Hugging Face API {verb} {self.resource_name} failed: {response.status_code} {response.text}")

    def _create(self, token: str) -> None:
        response = self._call(token, "POST", self._endpoint_path(token), self._payload())
        if response.status_code == 403 and "payment method" in response.text.lower():
            raise ProvisionError(
                "Hugging Face needs a payment method on the account before it creates endpoints: "
                "add one in your Hugging Face billing settings and try again."
            )
        if not (200 <= response.status_code < 300):
            raise ProvisionError(f"Hugging Face API create {self.resource_name} failed: {response.status_code} {response.text}")

    def _delete(self, token: str) -> bool:
        """Delete the endpoint; True if it existed."""
        response = self._call(token, "DELETE", self._endpoint_path(token, f"/{self.resource_name}"))
        if response.status_code == 404:
            return False
        if not (200 <= response.status_code < 300):
            raise ProvisionError(f"Hugging Face API delete {self.resource_name} failed: {response.status_code} {response.text}")
        return True

    @staticmethod
    def _state(endpoint: dict) -> str:
        return ((endpoint.get("status") or {}).get("state")) or ""

    # -- lifecycle ---------------------------------------------------------------------

    def _wait_running(self, token: str, ctx: ProvisionContext, progress: Callable[[str], None]) -> dict:
        """Poll until the endpoint runs; fail on ``failed``/``updateFailed`` or after the bound."""
        start = _monotonic()
        last = None
        while True:
            ctx.check_deadline()
            endpoint = self._get_endpoint(token)
            if endpoint is None:
                raise ProvisionError(f"The {self.side} endpoint disappeared while starting.")
            state = self._state(endpoint)
            if state != last:
                progress(f"{self.side.capitalize()} endpoint is {state or 'starting'}")
                last = state
            if state == "running":
                return endpoint
            if state in ("failed", "updateFailed"):
                message = (endpoint.get("status") or {}).get("message") or "no reason given"
                raise ProvisionError(f"The {self.side} endpoint failed to start: {message}")
            if _monotonic() - start >= WAIT_MAX_SECONDS:
                raise ProvisionError(f"The {self.side} endpoint did not start within {WAIT_MAX_SECONDS // 60} minutes.")
            _sleep(POLL_SECONDS)

    def _warmup_request(self, base_url: str) -> tuple[str, dict]:
        if self.side == "embedding":
            return f"{base_url}/embeddings", {"model": self.repository, "input": "ping"}
        return f"{base_url}/chat/completions", {
            "model": self.repository,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1,
        }

    def _warm_up(self, token: str, ctx: ProvisionContext, base_url: str, progress: Callable[[str], None]) -> None:
        """Retry a cheap request until it answers; never raises (the endpoint exists and wakes on use)."""
        url, body = self._warmup_request(base_url)
        start = _monotonic()
        attempt = 0
        while True:
            ctx.check_deadline()
            attempt += 1
            progress(f"Warming up the endpoint (attempt {attempt})")
            try:
                response = self._call(token, "POST", url, body)
            except Exception as exc:
                detail = f"connection error ({exc})"
            else:
                if 200 <= response.status_code < 300:
                    return
                detail = f"{response.status_code} {response.text[:200]}"
                if 400 <= response.status_code < 500 and response.status_code != 429:
                    logger.warning("Endpoint %s answered a warm-up with %s; it will warm up on the next request.", url, detail)
                    return
            if _monotonic() - start >= WARMUP_MAX_SECONDS:
                logger.warning("Endpoint %s did not warm up within %ds (last: %s).", url, WARMUP_MAX_SECONDS, detail)
                return
            _sleep(WARMUP_RETRY_SECONDS)

    def provision(self, ctx: ProvisionContext, progress: Callable[[str], None]) -> dict[str, str]:
        """Create, resume or wake this side's endpoint (idempotent, by name). The URL is derived, so ``{}``."""
        token = ctx.credential
        if not token:
            raise ProvisionError("No Hugging Face token available. Enter one in Preferences or set the shared token.")
        progress(f"Checking the {self.side} endpoint")
        endpoint = self._get_endpoint(token)
        if endpoint is not None:
            diffs = self._differs(endpoint)
            if diffs and ctx.recreate:
                progress(f"Replacing the {self.side} endpoint ({'; '.join(diffs)})")
                self._delete(token)
                self._wait_absent(token, ctx)
                endpoint = None
            elif diffs:
                logger.warning(
                    "Endpoint '%s' exists but differs from the requested config (%s). Keeping it - "
                    "re-run with recreate to replace it.", self.resource_name, "; ".join(diffs))
        if endpoint is None:
            progress(f"Creating the {self.side} endpoint")
            self._create(token)
        else:
            state = self._state(endpoint)
            if state in ("paused", "scaledToZero"):
                progress(f"Resuming the {self.side} endpoint")
                self._action(token, "resume")
            elif state in ("failed", "updateFailed"):
                message = (endpoint.get("status") or {}).get("message") or "no reason given"
                raise ProvisionError(f"The {self.side} endpoint is in state {state}: {message}. Re-run with recreate to replace it.")
        ctx.check_deadline()
        endpoint = self._wait_running(token, ctx, progress)
        base_url = self._base_url(endpoint)
        if not ctx.skip_warmup and base_url:
            self._warm_up(token, ctx, base_url, progress)
        progress(f"{self.side.capitalize()} endpoint ready: {base_url or 'url pending'}")
        return {}

    def _wait_absent(self, token: str, ctx: ProvisionContext) -> None:
        """After a delete, wait until the name is free again (deletion is asynchronous)."""
        start = _monotonic()
        while self._get_endpoint(token) is not None:
            ctx.check_deadline()
            if _monotonic() - start >= 120:
                raise ProvisionError(f"The old {self.side} endpoint was not removed in time.")
            _sleep(3)

    @staticmethod
    def _base_url(endpoint: dict) -> Optional[str]:
        url = (endpoint.get("status") or {}).get("url")
        return f"{url.rstrip('/')}/v1" if url else None

    def endpoint_url(self, api_key: str) -> Optional[str]:
        """OpenAI-compatible base URL of this side's endpoint, from the management API.

        A paused endpoint keeps its URL. None when there is no endpoint, it has no URL
        yet, or Hugging Face cannot be reached.
        """
        try:
            endpoint = self._get_endpoint(api_key)
        except Exception as exc:
            logger.warning("Could not look up the Hugging Face %s endpoint: %s", self.side, exc)
            return None
        return self._base_url(endpoint) if endpoint else None

    def is_paused(self, api_key: str) -> bool:
        try:
            endpoint = self._get_endpoint(api_key)
        except Exception:
            return False
        return bool(endpoint) and self._state(endpoint) == "paused"

    def health(self, creds: Credentials) -> Optional[Health]:
        """Readiness from one management-API call; never raises (display-only)."""
        if not creds.api_key:
            return Health(status="unreachable", detail="not configured")
        try:
            endpoint = self._get_endpoint(creds.api_key)
        except Exception as exc:
            return Health(status="unreachable", detail=str(exc)[:200])
        if endpoint is None:
            return Health(status="unreachable", detail="not provisioned")
        state = self._state(endpoint)
        status = _STATE_HEALTH.get(state, "unreachable")
        # A rollout (``updating``/``pending``) keeps serving from its ready replicas.
        if status == "cold" and state != "scaledToZero" and ((endpoint.get("status") or {}).get("readyReplica") or 0) >= 1:
            status = "ready"
        detail = (endpoint.get("status") or {}).get("message") or "" if status != "ready" else ""
        if status == "cold" and state == "scaledToZero":
            detail = "scaled to zero; wakes on the next request"
        return Health(status=status, detail=detail or state)

    def suspend(self, ctx: ProvisionContext, progress: Callable[[str], None]) -> None:
        """Pause the endpoint: not billed, keeps its URL, wakes only through an explicit resume."""
        token = ctx.credential
        if not token:
            raise ProvisionError("No Hugging Face token available.")
        endpoint = self._get_endpoint(token)
        if endpoint is None:
            raise ProvisionError(f"No endpoint '{self.resource_name}' to pause.")
        if self._state(endpoint) != "paused":
            self._action(token, "pause")
        progress(f"Paused the {self.side} endpoint")

    def teardown(self, ctx: ProvisionContext) -> None:
        """Delete this side's endpoint; an absent one is a no-op."""
        token = ctx.credential
        if not token:
            raise ProvisionError("No Hugging Face token available.")
        if self._delete(token):
            logger.info("Deleted Hugging Face endpoint '%s'", self.resource_name)
