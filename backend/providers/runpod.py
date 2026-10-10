"""RunPod serverless provider (``https://rest.runpod.io/v1``).

Each side owns one template and one endpoint in the key owner's RunPod
account, named ``zotero-rag-embedding`` / ``zotero-rag-llm``. The logic is the
former ``bin/provision_runpod_endpoints.py``, per side: ensure the template and
endpoint exist (idempotent, by name), send a warm-up request, report the
endpoint's OpenAI-compatible base URL. Plain ``httpx`` REST; no RunPod SDK.

Facts established in the Phase 0 spike (docs/history/implementation/
provider-layer-phase-0-spike.md): an endpoint cannot be created with
``workersMax=0`` (HTTP 500); the data-plane ``/health`` does not show a paused
endpoint, only the management API does.
"""

import logging
import re
import time
from typing import Any, Callable, Optional

from pydantic import Field

from backend.providers.base import Provider
from backend.providers.types import (
    CredentialDescriptor,
    Credentials,
    Health,
    ProviderDescriptor,
    ProvisionContext,
    ProvisioningDescriptor,
    ProvisionError,
    ProviderOptions,
)

logger = logging.getLogger(__name__)

REST_BASE_URL = "https://rest.runpod.io/v1"
DATA_PLANE_URL = "https://api.runpod.ai/v2"

# runpod/worker-infinity-embedding bundles a PyTorch that crashes on Blackwell
# GPUs, and runpod/worker-vllm is abandoned; the actively maintained
# worker-v1-vllm serves /openai/v1/embeddings for this (XLM-RoBERTa) model in
# pooling mode and supports current GPU generations. Pin a version.
DEFAULT_IMAGE = "runpod/worker-v1-vllm:v2.28.0"
DEFAULT_GPU = "NVIDIA RTX A5000"
#: MAX_MODEL_LEN of the embedding worker: the model's own position-embedding limit.
EMBEDDING_MAX_MODEL_LEN = "512"
CONTAINER_DISK_GB = {"embedding": 20, "llm": 40}

WARMUP_MAX_SECONDS = 180
WARMUP_RETRY_INTERVAL_SECONDS = 5
HEALTH_TIMEOUT_SECONDS = 10.0

KEY_PATTERN = r"^rpa_[A-Za-z0-9]+$"
BASE_URL_PATTERN = r"^https://api\.runpod\.ai/v2/[A-Za-z0-9]+/openai/v1$"
_ENDPOINT_ID_RE = re.compile(r"/v2/([^/]+)/")

# Indirection so tests can run the retry loop without really waiting.
_sleep: Callable[[float], None] = time.sleep
_monotonic: Callable[[], float] = time.monotonic


class RunPodOptions(ProviderOptions):
    gpu: str = DEFAULT_GPU
    workers_max: int = Field(default=1, ge=1)
    idle_timeout: int = Field(default=60, ge=1)
    data_centers: Optional[list[str]] = None
    image: str = DEFAULT_IMAGE
    container_disk_gb: Optional[int] = Field(default=None, ge=1)


def endpoint_base_url(endpoint_id: str) -> str:
    """OpenAI-compatible base URL of a RunPod serverless endpoint."""
    return f"{DATA_PLANE_URL}/{endpoint_id}/openai/v1"


class RunPodProvider(Provider):
    id = "runpod"
    label = "RunPod"
    Options = RunPodOptions
    key_scopes = frozenset({"user", "managed"})
    default_scope = "user"
    supports_provisioning = True
    supports_suspend = True
    derives_endpoint_url = True
    http_timeout = 30.0

    # -- naming -----------------------------------------------------------------

    @property
    def resource_name(self) -> str:
        """Name shared by this side's template and endpoint."""
        return f"zotero-rag-{self.side}"

    @property
    def model(self) -> str:
        """The model this side serves: the preset's own model name."""
        cfg = self.side_config
        return cfg.model_name if self.side == "embedding" else cfg.model_names[0]

    def template_env(self) -> dict[str, str]:
        env = {"MODEL_NAME": self.model}
        if self.side == "embedding":
            env["MAX_MODEL_LEN"] = EMBEDDING_MAX_MODEL_LEN
        return env

    # -- configuration and description ------------------------------------------

    def apply_defaults(self) -> None:
        kwargs = self.side_config.model_kwargs
        if self.scope == "user":
            kwargs.setdefault("api_key_pattern", KEY_PATTERN)
        else:
            kwargs.setdefault("shared_api_key_pattern", KEY_PATTERN)
            if "shared_base_url_env" in kwargs:
                kwargs.setdefault("shared_base_url_pattern", BASE_URL_PATTERN)
        super().apply_defaults()

    def describe(self) -> ProviderDescriptor:
        env = (
            self.side_config.model_kwargs.get("shared_api_key_env" if self.scope != "user" else "api_key_env")
            or "RUNPOD_API_KEY"
        )
        descriptor = super().describe()
        descriptor.provisioning = ProvisioningDescriptor(
            credential=CredentialDescriptor(
                env=env,
                label="RunPod API key",
                help=(
                    "Creating endpoints may need a key with broader rights than the one used for "
                    "queries (an endpoint-restricted key cannot create or replace endpoints). "
                    "Leave empty to use the stored key; a value entered here is used for this run only."
                ),
                pattern=KEY_PATTERN,
                optional=True,
            ),
            hint="A cold start can take a minute or more while the worker loads the model.",
        )
        return descriptor

    def key_docs_url(self, env_var: str) -> Optional[str]:
        if env_var == "RUNPOD_API_KEY":
            return "https://www.runpod.io/console/user/settings"
        return super().key_docs_url(env_var)

    def unavailable_hint(self) -> Optional[str]:
        return (
            "This preset uses a RunPod serverless endpoint, which may be cold or not yet "
            "provisioned - check its status and provision or wake it from Preferences."
        )

    def classify_http_error(self, status: int, body: str) -> Optional[str]:
        # RunPod rejects every request to an endpoint whose workersMax is 0.
        if status == 409 and "ENDPOINT_PAUSED" in (body or ""):
            return "paused"
        return None

    # -- REST helpers --------------------------------------------------------------

    def _request(self, api_key: str, method: str, path: str, json_body: Optional[dict] = None) -> Any:
        """Authenticated call against the RunPod REST API; parsed JSON (None for no body).

        Raises ProvisionError on a non-2xx response.
        """
        response = self._http().request(
            method,
            f"{REST_BASE_URL}{path}",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=json_body,
            timeout=self.http_timeout,
        )
        if not (200 <= response.status_code < 300):
            raise ProvisionError(f"RunPod API {method} {path} failed: {response.status_code} {response.text}")
        if response.status_code == 204 or not response.text:
            return None
        return response.json()

    def _find_by_name(self, api_key: str, resource: str, name: str) -> Optional[dict]:
        for item in self._request(api_key, "GET", f"/{resource}") or []:
            if item.get("name") == name:
                return item
        return None

    def _ensure_template(self, api_key: str, ctx: ProvisionContext) -> dict:
        """Find the template by name or create it.

        A template whose image/env differs is kept with a warning unless
        ``ctx.recreate`` is set (deleting an in-use template is destructive
        and costs money to respin).
        """
        name, opts = self.resource_name, self.options
        image, env = opts.image, self.template_env()
        existing = self._find_by_name(api_key, "templates", name)
        if existing is not None:
            image_differs = existing.get("imageName") != image
            env_differs = existing.get("env") != env
            if not (image_differs or env_differs):
                return existing
            if not ctx.recreate:
                diffs = []
                if image_differs:
                    diffs.append(f"image={existing.get('imageName')!r} vs {image!r}")
                if env_differs:
                    diffs.append(f"env={existing.get('env')!r} vs {env!r}")
                logger.warning(
                    "Template '%s' exists but its config differs from requested (%s). "
                    "Keeping existing template - re-run with recreate to replace it.",
                    name, "; ".join(diffs),
                )
                return existing
            # RunPod refuses to delete a template still associated with an
            # endpoint; the endpoint referencing it is the one with the same name.
            referencing = self._find_by_name(api_key, "endpoints", name)
            if referencing is not None:
                self._request(api_key, "DELETE", f"/endpoints/{referencing['id']}")
            self._request(api_key, "DELETE", f"/templates/{existing['id']}")
        return self._request(api_key, "POST", "/templates", {
            "name": name,
            "imageName": image,
            "env": env,
            "containerDiskInGb": opts.container_disk_gb or CONTAINER_DISK_GB[self.side],
            "isServerless": True,
        })

    def _ensure_endpoint(self, api_key: str, ctx: ProvisionContext, template_id: str) -> dict:
        """Find the endpoint by name or create it referencing ``template_id``."""
        name, opts = self.resource_name, self.options
        existing = self._find_by_name(api_key, "endpoints", name)
        gpus = [opts.gpu]
        if existing is not None:
            existing_gpus = existing.get("gpuTypeIds")
            gpu_mismatch = existing_gpus is not None and set(existing_gpus) != set(gpus)
            # The listing may not report gpuTypeIds; an explicit recreate must
            # then still replace it, since the placement cannot be verified.
            gpu_unverifiable = ctx.recreate and existing_gpus is None
            mismatched = existing.get("templateId") != template_id or gpu_mismatch or gpu_unverifiable
            if not mismatched:
                if existing.get("workersMax") == 0:  # paused: resume
                    return self._request(
                        api_key, "PATCH", f"/endpoints/{existing['id']}", {"workersMax": opts.workers_max}
                    )
                return existing
            if not ctx.recreate:
                logger.warning(
                    "Endpoint '%s' exists but its config differs from requested "
                    "(template_id=%r vs %r, gpuTypeIds=%r vs %r). Keeping existing "
                    "endpoint - re-run with recreate to replace it.",
                    name, existing.get("templateId"), template_id, existing_gpus, gpus,
                )
                return existing
            self._request(api_key, "DELETE", f"/endpoints/{existing['id']}")
        body: dict[str, Any] = {
            "name": name,
            "templateId": template_id,
            "gpuTypeIds": gpus,
            # RunPod rejects creation with workersMax=0 (HTTP 500), so create
            # with at least 1; a pause is a later PATCH.
            "workersMin": 0,
            "workersMax": opts.workers_max,
            "idleTimeout": opts.idle_timeout,
        }
        if opts.data_centers:
            body["dataCenterIds"] = list(opts.data_centers)
        return self._request(api_key, "POST", "/endpoints", body)

    # -- warm-up --------------------------------------------------------------------

    def _warmup_request(self, base_url: str) -> tuple[str, dict]:
        """URL and JSON body of the cheapest request that triggers a cold start."""
        if self.side == "embedding":
            return f"{base_url}/embeddings", {"model": self.model, "input": "ping"}
        return f"{base_url}/chat/completions", {
            "model": self.model,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1,
        }

    def _purge_queue(self, api_key: str, endpoint_id: str) -> None:
        """Best-effort: drop jobs still queued for this endpoint.

        A warm-up request we gave up on is not cancelled server side; purging
        between retries stops a slow cold start accumulating duplicate pings,
        each of which still burns GPU time once picked up.
        """
        try:
            self._http().request(
                "POST", f"{DATA_PLANE_URL}/{endpoint_id}/purge-queue",
                headers={"Authorization": f"Bearer {api_key}"}, timeout=10.0,
            )
        except Exception:
            pass

    def _warm_up(self, api_key: str, ctx: ProvisionContext, base_url: str, endpoint_id: str,
                 progress: Callable[[str], None]) -> None:
        """Retry the warm-up request until it succeeds or the time budget ends.

        Never raises on a failing warm-up: the endpoint exists and will warm up
        on the next real request anyway. A non-retryable 4xx (other than 429)
        stops at once. Honours ``ctx.deadline``.
        """
        url, body = self._warmup_request(base_url)
        start = _monotonic()
        attempt = 0
        while True:
            ctx.check_deadline()
            attempt += 1
            progress(f"Warming up the endpoint (attempt {attempt})")
            non_retryable = False
            try:
                response = self._http().request(
                    "POST", url,
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json=body, timeout=30.0,
                )
            except Exception as exc:
                status_desc = f"connection error ({exc})"
            else:
                if 200 <= response.status_code < 300:
                    return
                status_desc = f"{response.status_code} {response.text[:200]}"
                non_retryable = 400 <= response.status_code < 500 and response.status_code != 429
            if non_retryable:
                logger.warning(
                    "Endpoint at %s returned a non-retryable error: %s. "
                    "It still exists and will warm up on the next real request.", url, status_desc)
                return
            if _monotonic() - start >= WARMUP_MAX_SECONDS:
                logger.warning(
                    "Endpoint at %s did not warm up within %ds (last status: %s). "
                    "It still exists and will warm up on the next real request.",
                    url, WARMUP_MAX_SECONDS, status_desc)
                return
            self._purge_queue(api_key, endpoint_id)
            _sleep(WARMUP_RETRY_INTERVAL_SECONDS)

    # -- Provider API ---------------------------------------------------------------

    def provision(self, ctx: ProvisionContext, progress: Callable[[str], None]) -> dict[str, str]:
        """Create or wake this side's endpoint (idempotent, by name).

        Returns ``{shared_base_url_env: url}`` when the side stores its URL in
        the shared config, ``{}`` otherwise.
        """
        api_key = ctx.credential
        if not api_key:
            raise ProvisionError("No RunPod API key available. Enter one in Preferences or set the shared key.")
        progress(f"Checking the {self.side} template")
        template = self._ensure_template(api_key, ctx)
        ctx.check_deadline()
        progress(f"Checking the {self.side} endpoint")
        endpoint = self._ensure_endpoint(api_key, ctx, template["id"])
        base_url = endpoint_base_url(endpoint["id"])
        if not ctx.skip_warmup:
            self._warm_up(api_key, ctx, base_url, endpoint["id"], progress)
        progress(f"{self.side.capitalize()} endpoint ready: {base_url}")
        url_env = self.side_config.model_kwargs.get("shared_base_url_env")
        return {url_env: base_url} if url_env else {}

    def endpoint_url(self, api_key: str) -> Optional[str]:
        """OpenAI-compatible base URL of this side's endpoint in the key owner's account.

        Looked up by name (``zotero-rag-<side>``). None when there is no such
        endpoint, the key cannot list endpoints, or RunPod cannot be reached.
        """
        try:
            endpoint = self._find_by_name(api_key, "endpoints", self.resource_name)
        except Exception as exc:
            logger.warning("Could not look up the RunPod %s endpoint: %s", self.side, exc)
            return None
        return endpoint_base_url(endpoint["id"]) if endpoint else None

    def suspend(self, ctx: ProvisionContext, progress: Callable[[str], None]) -> None:
        """Pause the endpoint by setting ``workersMax`` to 0 (requests then get 409)."""
        api_key = ctx.credential
        if not api_key:
            raise ProvisionError("No RunPod API key available.")
        endpoint = self._find_by_name(api_key, "endpoints", self.resource_name)
        if endpoint is None:
            raise ProvisionError(f"No endpoint '{self.resource_name}' to pause.")
        self._request(api_key, "PATCH", f"/endpoints/{endpoint['id']}", {"workersMax": 0})
        progress(f"Paused {self.side} endpoint {endpoint['id']}")

    def teardown(self, ctx: ProvisionContext) -> None:
        """Delete this side's endpoint and template; absent ones are a no-op."""
        api_key = ctx.credential
        if not api_key:
            raise ProvisionError("No RunPod API key available.")
        endpoint = self._find_by_name(api_key, "endpoints", self.resource_name)
        if endpoint is not None:
            self._request(api_key, "DELETE", f"/endpoints/{endpoint['id']}")
            logger.info("Deleted endpoint '%s' (%s)", self.resource_name, endpoint["id"])
        template = self._find_by_name(api_key, "templates", self.resource_name)
        if template is not None:
            self._request(api_key, "DELETE", f"/templates/{template['id']}")
            logger.info("Deleted template '%s' (%s)", self.resource_name, template["id"])

    def health(self, creds: Credentials) -> Optional[Health]:
        """Readiness from RunPod's data-plane ``/health`` (worker and job counts).

        ``cold`` means scaled to zero or a worker initializing (it wakes on the
        next request); ``throttled`` means RunPod has no capacity for the GPU
        type right now. Never raises: this is a display-only signal.
        """
        if not creds.api_key:
            return Health(status="unreachable", detail="not configured")
        if not creds.base_url:
            return Health(status="unreachable", detail="not provisioned")
        match = _ENDPOINT_ID_RE.search(creds.base_url)
        if not match:
            return Health(status="unreachable", detail=f"Not a RunPod endpoint URL: {creds.base_url!r}")
        endpoint_id = match.group(1)
        try:
            response = self._http().get(
                f"{DATA_PLANE_URL}/{endpoint_id}/health",
                headers={"Authorization": f"Bearer {creds.api_key}"},
                timeout=HEALTH_TIMEOUT_SECONDS,
            )
            if response.status_code in (401, 403):
                # An endpoint-restricted key that does not cover this endpoint
                # (e.g. after a recreate gave it a new ID) gets 403.
                return Health(
                    status="unreachable",
                    detail=f"HTTP {response.status_code}: the API key has no access to endpoint {endpoint_id}",
                )
            if not 200 <= response.status_code < 300:
                return Health(status="unreachable", detail=f"HTTP {response.status_code}")
            payload = response.json() or {}
            workers = payload.get("workers") or {}
            jobs = payload.get("jobs") or {}
            ready = int(workers.get("ready") or 0)
            running = int(workers.get("running") or 0)
            if ready > 0 or running > 0:
                return Health(status="ready", detail=f"{ready} ready, {running} running workers")
            throttled = int(workers.get("throttled") or 0)
            if throttled > 0:
                in_queue = int(jobs.get("inQueue") or 0)
                return Health(
                    status="throttled",
                    detail=f"{throttled} worker(s) throttled (no available capacity), {in_queue} "
                           "job(s) queued - try a different GPU type or wait for capacity",
                )
            initializing = int(workers.get("initializing") or 0)
            if initializing > 0:
                return Health(status="cold", detail=f"{initializing} worker(s) initializing")
            return Health(status="cold", detail="No ready or running workers (scaled to zero)")
        except Exception as exc:  # display-only signal: must never break the health route
            return Health(status="unreachable", detail=str(exc) or type(exc).__name__)
