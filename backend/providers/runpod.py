"""RunPod serverless provider (management API ``https://api.runpod.io/v2``).

Each side owns one endpoint in the key owner's RunPod account, named
``zotero-rag-embedding`` / ``zotero-rag-llm`` / ``zotero-rag-rerank``, with its
worker image, environment and disk set on the endpoint itself (v2 copies a
template into an endpoint only once, at creation, so none is kept). Per side:
ensure the endpoint exists (idempotent, by name), send a warm-up request, report
the endpoint's OpenAI-compatible base URL. Plain ``httpx`` REST; no RunPod SDK.

Facts established in the Phase 0 spike (docs/history/implementation/
provider-layer-phase-0-spike.md): an endpoint cannot be created with at most 0
workers (HTTP 500); the data-plane ``/health`` does not show a paused endpoint,
only the management API does.
"""

import urllib.parse

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

REST_BASE_URL = "https://api.runpod.io/v2"
DATA_PLANE_URL = "https://api.runpod.ai/v2"

# runpod/worker-infinity-embedding bundles a PyTorch that crashes on Blackwell
# GPUs, and runpod/worker-vllm is abandoned; the actively maintained
# worker-v1-vllm serves /openai/v1/embeddings for this (XLM-RoBERTa) model in
# pooling mode and supports current GPU generations. Pin a version.
DEFAULT_IMAGE = "runpod/worker-v1-vllm:v2.28.0"
#: A GPU type id (or the catalog's short name) of GET /v2/catalog/gpus. v2 places an
#: endpoint on a pool of GPU types; the type's pool with its other types excluded keeps
#: the endpoint on that type.
DEFAULT_GPU = "NVIDIA RTX A5000"
#: MAX_MODEL_LEN of the embedding worker: the model's own position-embedding limit.
EMBEDDING_MAX_MODEL_LEN = "512"
CONTAINER_DISK_GB = {"embedding": 20, "llm": 40, "rerank": 20}
#: MAX_MODEL_LEN of the re-ranking worker: one (question, passage) pair.
RERANK_MAX_MODEL_LEN = "1024"

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
    #: Name of this side's template and endpoint; default ``zotero-rag-<side>``. Lets a
    #: second preset on the same account (a larger LLM, say) keep its own endpoint.
    name: Optional[str] = Field(default=None, min_length=1, max_length=60)
    #: ``MAX_MODEL_LEN`` of an LLM worker. Without it vLLM sizes the KV cache for the
    #: model's full context, which a large model's GPU may not hold.
    max_model_len: Optional[int] = Field(default=None, ge=256)


def endpoint_base_url(endpoint_id: str) -> str:
    """OpenAI-compatible base URL of a RunPod serverless endpoint."""
    return f"{DATA_PLANE_URL}/{endpoint_id}/openai/v1"


class RunPodProvider(Provider):
    id = "runpod"
    label = "RunPod"
    Options = RunPodOptions
    #: Each user's own key only: RunPod's terms do not allow sharing an account.
    key_scopes = frozenset({"user"})
    default_scope = "user"
    supports_provisioning = True
    supports_suspend = True
    derives_endpoint_url = True
    supported_sides = frozenset({"embedding", "llm", "rerank"})
    http_timeout = 30.0

    # -- naming -----------------------------------------------------------------

    @property
    def resource_name(self) -> str:
        """Name shared by this side's template and endpoint."""
        return self.options.name or f"zotero-rag-{self.side}"

    @property
    def model(self) -> str:
        """The model this side serves: the preset's own model name."""
        cfg = self.side_config
        return cfg.model_names[0] if self.side == "llm" else cfg.model_name

    def worker_env(self) -> dict[str, str]:
        env = {"MODEL_NAME": self.model}
        if self.side == "embedding":
            env["MAX_MODEL_LEN"] = EMBEDDING_MAX_MODEL_LEN
        elif self.side == "rerank":
            env["MAX_MODEL_LEN"] = RERANK_MAX_MODEL_LEN
        elif self.options.max_model_len:
            env["MAX_MODEL_LEN"] = str(self.options.max_model_len)
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
                label="Provisioning API key",
                help=(
                    "Kept in memory until Zotero closes, never saved. Creating, replacing, resuming or pausing "
                    "endpoints needs a full-access RunPod API key; the stored key used for queries may be "
                    "restricted to the endpoints, which cannot do this. Leave empty to use the stored key."
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
            "provisioned - check its status and start or wake it in the Configure preset dialog."
        )

    def classify_http_error(self, status: int, body: str) -> Optional[str]:
        # RunPod rejects every request to an endpoint paused at 0 workers.
        if status == 409 and "ENDPOINT_PAUSED" in (body or ""):
            return "paused"
        return None

    # -- REST helpers --------------------------------------------------------------

    def _request(self, api_key: str, method: str, path: str, json_body: Optional[dict] = None) -> Any:
        """Authenticated call against the RunPod management API; parsed JSON (None for no body).

        Raises ProvisionError on a non-2xx response or when the API cannot be reached.
        """
        try:
            response = self._http().request(
                method,
                f"{REST_BASE_URL}{path}",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=json_body,
                timeout=self.http_timeout,
            )
        except OSError as exc:  # DNS, refused, reset, TLS: say which service, not just errno
            host = urllib.parse.urlsplit(REST_BASE_URL).hostname
            raise ProvisionError(f"Could not reach the RunPod API ({host}): {exc}") from exc
        if not (200 <= response.status_code < 300):
            raise ProvisionError(f"RunPod API {method} {path} failed: {response.status_code} {response.text}")
        if response.status_code == 204 or not response.text:
            return None
        return response.json()

    def _list(self, api_key: str, resource: str, key: str) -> list[dict]:
        """Every item of a paginated v2 listing (``{key: [...], "pagination": {...}}``)."""
        items: list[dict] = []
        path = f"/{resource}"
        while True:
            page = self._request(api_key, "GET", path) or {}
            items.extend(page.get(key) or [])
            cursor = (page.get("pagination") or {}).get("nextCursor")
            if not cursor:
                return items
            path = f"/{resource}?cursor={urllib.parse.quote(cursor, safe='')}"

    def _find_endpoint(self, api_key: str) -> Optional[dict]:
        """This side's endpoint (by name), or None."""
        return next((e for e in self._list(api_key, "serverless", "endpoints") if e.get("name") == self.resource_name), None)

    def _gpu_spec(self, api_key: str) -> dict:
        """The ``gpu`` of this side's endpoint: the configured type's pool, its other types excluded."""
        wanted = self.options.gpu
        gpus = (self._request(api_key, "GET", "/catalog/gpus") or {}).get("gpus") or []
        match = next((g for g in gpus if wanted in (g.get("id"), g.get("name"))), None)
        if match is None or not match.get("pool"):
            raise ProvisionError(
                f"RunPod serverless has no GPU type '{wanted}'. Set provider option 'gpu' to a GPU type id "
                "of RunPod's catalog, e.g. 'NVIDIA RTX A5000'.")
        pool = match["pool"]
        others = sorted(g["id"] for g in gpus if g.get("pool") == pool and g.get("id") != match["id"])
        return {"pools": [pool], "excludedTypes": others, "count": 1}

    @staticmethod
    def _max_workers(endpoint: dict) -> Optional[int]:
        return (endpoint.get("workers") or {}).get("max")

    def _ensure_endpoint(self, api_key: str, ctx: ProvisionContext) -> dict:
        """Find this side's endpoint by name or create it; resume it when paused.

        One whose image, environment or GPU differs is kept with a warning unless
        ``ctx.recreate`` is set (replacing an endpoint in use costs a cold start).
        """
        name, opts = self.resource_name, self.options
        image, env, disk = opts.image, self.worker_env(), opts.container_disk_gb or CONTAINER_DISK_GB[self.side]
        existing = self._find_endpoint(api_key)
        gpu = self._gpu_spec(api_key)
        if existing is not None:
            have_gpu = existing.get("gpu") or {}
            diffs = []
            if existing.get("image") != image:
                diffs.append(f"image={existing.get('image')!r} vs {image!r}")
            if existing.get("env") != env:
                diffs.append(f"env={existing.get('env')!r} vs {env!r}")
            if have_gpu.get("pools") != gpu["pools"] or opts.gpu in (have_gpu.get("excludedTypes") or []):
                diffs.append(f"gpu={have_gpu!r} vs {gpu!r}")
            if diffs and ctx.recreate:
                self._request(api_key, "DELETE", f"/serverless/{existing['id']}")
            else:
                if diffs:
                    logger.warning(
                        "Endpoint '%s' exists but its config differs from requested (%s). Keeping the "
                        "existing endpoint - re-run with recreate to replace it.", name, "; ".join(diffs))
                if self._max_workers(existing) == 0:  # paused: resume (also a kept, differing one)
                    return self._request(api_key, "PATCH", f"/serverless/{existing['id']}", {
                        "workers": {"min": 0, "max": opts.workers_max, "idleTimeout": opts.idle_timeout}})
                return existing
        body: dict[str, Any] = {
            "name": name,
            "type": "QUEUE",
            "image": image,
            "env": env,
            "disk": disk,
            "gpu": gpu,
            # Scale to zero: no worker runs (or bills) until a request arrives. RunPod rejects
            # creation with at most 0 workers, so a pause is a later PATCH.
            "workers": {"min": 0, "max": opts.workers_max, "idleTimeout": opts.idle_timeout},
            "scaling": {"type": "QUEUE_DELAY", "queueDelay": 4},
            "flashboot": "FLASHBOOT",
        }
        if opts.data_centers:
            body["dataCenterIds"] = list(opts.data_centers)
        return self._request(api_key, "POST", "/serverless", body)

    # -- warm-up --------------------------------------------------------------------

    def _warmup_request(self, base_url: str) -> tuple[str, dict]:
        """URL and JSON body of the cheapest request that triggers a cold start."""
        if self.side == "rerank":
            return self.rerank_http(base_url, {"model": self.model, "query": "ping", "documents": ["ping"]})
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
            raise ProvisionError("No RunPod API key available. Enter one in the Configure preset dialog or set the shared key.")
        progress(f"Checking the {self.side} endpoint")
        endpoint = self._ensure_endpoint(api_key, ctx)
        ctx.check_deadline()
        base_url = endpoint_base_url(endpoint["id"])
        if not ctx.skip_warmup:
            self._warm_up(api_key, ctx, base_url, endpoint["id"], progress)
        progress(f"{self.side.capitalize()} endpoint ready: {base_url}")
        url_env = self.side_config.model_kwargs.get("shared_base_url_env")
        return {url_env: base_url} if url_env else {}

    # -- re-ranking side ------------------------------------------------------------

    def rerank_credentials(self, request, refresh: bool = False) -> Optional[Credentials]:
        """The caller's own endpoint, found from their key (cached per key)."""
        from backend.services.endpoint_cache import endpoint_cache

        env = self.side_config.model_kwargs.get("api_key_env") or "RUNPOD_API_KEY"
        api_key = request.client_keys.get(env)
        if not api_key:
            return None
        if refresh:
            endpoint_cache.invalidate(self.id, self.side, api_key)
        base_url = endpoint_cache.resolve(self, self.side, api_key)
        return Credentials(api_key=api_key, base_url=base_url) if base_url else None

    def rerank_http(self, base_url: str, body: dict) -> tuple[str, dict]:
        """The worker's platform route does not pass ``/rerank`` through, so the request goes
        through ``/runsync`` with the worker's generic route proxy (worker-v1-vllm handler)."""
        match = _ENDPOINT_ID_RE.search(base_url + "/")
        endpoint = match.group(1) if match else base_url.rstrip("/").rsplit("/", 1)[-1]
        return f"{DATA_PLANE_URL}/{endpoint}/runsync", {"input": {"route": "/v1/rerank", "body": body}}

    def rerank_unwrap(self, data: Any) -> Any:
        """The vLLM ``/rerank`` response inside a ``/runsync`` result.

        Raises RerankerUnavailable while the job still waits for a worker (cold start)
        and ValueError for a worker error.
        """
        from backend.services.rerank import RerankerUnavailable

        if not isinstance(data, dict):
            raise ValueError("unexpected /runsync response")
        status = data.get("status")
        if status in ("IN_QUEUE", "IN_PROGRESS"):
            raise RerankerUnavailable("the re-ranking endpoint is starting")
        if status not in (None, "COMPLETED"):
            raise ValueError(f"RunPod job {status}: {str(data.get('error') or '')[:200]}")
        output = data.get("output")
        if isinstance(output, list):  # the worker aggregates its (single) yield
            output = output[0] if output else None
        if isinstance(output, dict) and "error" in output and "results" not in output:
            raise ValueError(f"worker error: {str(output['error'])[:200]}")
        return output

    def endpoint_url(self, api_key: str) -> Optional[str]:
        """OpenAI-compatible base URL of this side's endpoint in the key owner's account.

        Looked up by name (``zotero-rag-<side>``). None when there is no such
        endpoint, the key cannot list endpoints, or RunPod cannot be reached.
        """
        try:
            endpoint = self._find_endpoint(api_key)
        except Exception as exc:
            logger.warning("Could not look up the RunPod %s endpoint: %s", self.side, exc)
            return None
        return endpoint_base_url(endpoint["id"]) if endpoint else None

    def suspend(self, ctx: ProvisionContext, progress: Callable[[str], None]) -> None:
        """Pause the endpoint by setting its maximum workers to 0 (requests then get 409)."""
        api_key = ctx.credential
        if not api_key:
            raise ProvisionError("No RunPod API key available.")
        endpoint = self._find_endpoint(api_key)
        if endpoint is None:
            raise ProvisionError(f"No endpoint '{self.resource_name}' to pause.")
        self._request(api_key, "PATCH", f"/serverless/{endpoint['id']}", {"workers": {"min": 0, "max": 0}})
        progress(f"Paused {self.side} endpoint {endpoint['id']}")

    def teardown(self, ctx: ProvisionContext) -> None:
        """Delete this side's endpoint, and a template of the same name an earlier version
        created; absent ones are a no-op."""
        api_key = ctx.credential
        if not api_key:
            raise ProvisionError("No RunPod API key available.")
        endpoint = self._find_endpoint(api_key)
        if endpoint is not None:
            self._request(api_key, "DELETE", f"/serverless/{endpoint['id']}")
            logger.info("Deleted endpoint '%s' (%s)", self.resource_name, endpoint["id"])
        templates = self._list(api_key, "templates", "templates")
        template = next((t for t in templates if t.get("name") == self.resource_name), None)
        if template is not None:
            self._request(api_key, "DELETE", f"/templates/{template['id']}")
            logger.info("Deleted template '%s' (%s)", self.resource_name, template["id"])

    def is_paused(self, api_key: str) -> bool:
        """True when the endpoint's maximum workers is 0 (read from the management API only)."""
        try:
            endpoint = self._find_endpoint(api_key)
        except Exception:
            return False
        return bool(endpoint) and self._max_workers(endpoint) == 0

    def health(self, creds: Credentials) -> Optional[Health]:
        """Readiness from RunPod's data-plane ``/health`` (worker and job counts).

        A paused endpoint (maximum workers 0) is invisible to ``/health``, so the management
        API is asked first; if that is not allowed for the key, the data plane decides.

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
            managed = self._request(creds.api_key, "GET", f"/serverless/{endpoint_id}")
            if managed and self._max_workers(managed) == 0:
                return Health(status="paused", detail="paused; resume it to use it again")
        except Exception:
            pass  # a restricted key may not read the management API: fall back to the data plane
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
            if ready > 0:
                return Health(status="ready", detail=f"{ready} ready, {running} running workers")
            if running > 0:
                # A worker counts as "running" from the moment its container starts, while the
                # model is still loading: queued jobs that no worker has picked up yet mean it
                # is not serving. One that is busy with a job (or has an empty queue) is.
                in_queue = int(jobs.get("inQueue") or 0)
                in_progress = int(jobs.get("inProgress") or 0)
                if in_queue > 0 and in_progress == 0:
                    return Health(status="cold", detail=f"worker starting, {in_queue} job(s) waiting for it")
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
