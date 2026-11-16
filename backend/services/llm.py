"""
LLM service for text generation.

Remote APIs (OpenAI-compatible, Anthropic) are implemented here; local models come
from the ``inference.local`` capabilities of backend plugins (for example
``local_torch``, transformers on torch), chosen by the side's ``engine`` key.
"""

import json
import logging
import random
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional

from backend.config.settings import Settings
from backend.services.embeddings import KEYLESS_API_KEY, env_var_to_header, docs_url_for, _extract_error_detail
from backend.services.endpoint_errors import classify_status_error, unavailable_message
from backend.services.usage_meters import (
    RateLimitStillActive,
    extract_retry_after,
    key_fingerprint,
    recorder as usage_recorder,
    wait_out_known_rate_limit,
)

#: Server-supplied 429 wait this short is slept through and retried in-process;
#: a longer or missing one raises LLMRateLimitExhaustedError immediately (mirrors
#: RemoteEmbeddingService._create_embeddings_with_backoff's max_rate_limit_wait).
_MAX_RATE_LIMIT_WAIT = 60.0
_MAX_RATE_LIMIT_ATTEMPTS = 5

logger = logging.getLogger(__name__)


class LLMConfigurationError(Exception):
    """Raised when the remote LLM client can't even be constructed because a
    required API key or base URL isn't configured — e.g. a preset's
    ``shared_api_key_env``/``shared_base_url_env`` value was never set via
    POST /api/config/remote-fields, or the personal key env var the preset's
    provider names (for example ``OPENAI_API_KEY``) is unset.

    Distinct from LLMEndpointUnavailableError (which means a working
    config couldn't *reach* the endpoint): this means the config itself is
    incomplete. Still a known, classified upstream-provider problem rather
    than an internal bug, so it gets the same 503 treatment in
    backend.api.query.
    """


class LLMEndpointUnavailableError(Exception):
    """Raised when the remote LLM endpoint can't be reached at all — a
    transport-level ``openai.APIConnectionError``, no HTTP status involved.

    Mirrors backend.services.embeddings.EmbeddingEndpointUnavailableError:
    most commonly means a self-provisioned serverless endpoint (e.g.
    RunPod) is cold or was never provisioned. Not retryable here — a cold
    worker can take minutes to spin up, so the caller should abort and
    point the admin at GET /api/config/health / the "Provision endpoints"
    button rather than block the request retrying.
    """


class LLMRateLimitExhaustedError(Exception):
    """Raised when the remote LLM API's rate limit cannot be resolved within the call.

    Mirrors ``backend.services.embeddings.EmbeddingRateLimitExhaustedError``:
    ``available_at`` is the UTC datetime after which the provider is expected
    to accept requests again (from the response's ``retry-after``-style
    header, or a default estimate when none is given).
    """

    def __init__(self, message: str, available_at: datetime):
        super().__init__(message)
        self.available_at = available_at


class LLMService(ABC):
    """Abstract base class for LLM services."""

    @staticmethod
    def required_client_fields(settings: "Settings") -> list[dict]:
        """Return the client-configurable fields required by this service (empty for local services)."""
        return []

    @property
    def model_name(self) -> str:
        """Name of the LLM model in use. Subclasses should override this."""
        return "unknown"

    @abstractmethod
    async def generate(
        self,
        prompt: str,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None
    ) -> str:
        """
        Generate text completion for a prompt.

        Args:
            prompt: Input prompt text.
            max_tokens: Maximum tokens to generate.
            temperature: Sampling temperature.

        Returns:
            Generated text completion.
        """
        pass


class RemoteLLMService(LLMService):
    """LLM service using remote APIs (OpenAI, Anthropic, etc.)."""

    def __init__(
        self,
        settings: Settings,
        api_key: Optional[str] = None,
        model_name_override: Optional[str] = None,
    ):
        """
        Initialize remote LLM service.

        Args:
            settings: Application settings.
            api_key: API key for the remote service.
            model_name_override: Override the preset default model name.
        """
        self.settings = settings
        self.preset = settings.get_hardware_preset()
        self.llm_config = self.preset.llm
        self.api_key = api_key
        self._provider = None
        self._resolved_base_url: Optional[str] = None
        #: The caller's own endpoint (``SessionEndpoint``) for a provider that runs one per user session.
        self._session: Any = None
        self._session_key: Optional[str] = None
        self._model_name = model_name_override or self.llm_config.model_name

        # Initialize API clients
        self._openai_client = None
        self._anthropic_client = None

        logger.info(f"Initialized RemoteLLMService with model: {self._model_name}")

    @property
    def model_name(self) -> str:
        return self._model_name

    @staticmethod
    def required_client_fields(settings: Settings) -> list[dict]:
        """Return the fields required by this remote LLM service (see
        RemoteEmbeddingService.required_client_fields for the api_key/shared_*
        kinds, and for what the optional ``pattern`` entry means)."""
        return RemoteLLMService.required_client_fields_for_config(settings.get_hardware_preset().llm)

    @staticmethod
    def required_client_fields_for_config(config: Any) -> list[dict]:
        """Like ``required_client_fields`` but for an explicit LLMConfig (any preset,
        not just the active one)."""
        if config.model_type != "remote":
            return []
        fields: list[dict] = []
        if "api_key_env" in config.model_kwargs:
            env_var = config.model_kwargs["api_key_env"]
            fields.append({
                "key_name": env_var, "header_name": env_var_to_header(env_var), "kind": "api_key",
                "description": f"API key for remote LLM ({config.model_name})",
                "docs_url": docs_url_for(config.model_kwargs, env_var), "required_for": ["querying"],
                "pattern": config.model_kwargs.get("api_key_pattern"),
            })
        # See RemoteEmbeddingService.required_client_fields: the key is listed
        # before the base_url so it's the first field in the Preferences pane.
        if "shared_api_key_env" in config.model_kwargs:
            env_var = config.model_kwargs["shared_api_key_env"]
            fields.append({
                "key_name": env_var, "header_name": env_var_to_header(env_var), "kind": "shared_api_key",
                "description": f"Shared API key for remote LLM ({config.model_name})",
                "docs_url": docs_url_for(config.model_kwargs, env_var), "required_for": ["querying"],
                "pattern": config.model_kwargs.get("shared_api_key_pattern"),
            })
        if "shared_base_url_env" in config.model_kwargs:
            env_var = config.model_kwargs["shared_base_url_env"]
            fields.append({
                "key_name": env_var, "header_name": env_var_to_header(env_var), "kind": "shared_base_url",
                "description": f"Shared endpoint URL for remote LLM ({config.model_name})",
                "docs_url": docs_url_for(config.model_kwargs, env_var), "required_for": ["querying"],
                "pattern": config.model_kwargs.get("shared_base_url_pattern"),
            })
        return fields

    def _dump_inference_request(self, payload: Dict[str, Any]) -> None:
        """Write the inference request payload to logs/last-inference-request.json (overwrite)."""
        try:
            log_dir = self.settings.log_file.parent if self.settings.log_file else None
            if log_dir is None:
                return
            log_dir.mkdir(parents=True, exist_ok=True)
            dest = log_dir / "last-inference-request.json"
            dest.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.debug(f"Could not write last-inference-request.json: {e}")

    def _dump_inference_response(self, response_text: str) -> None:
        """Write the raw LLM response to logs/last-inference-response.json (overwrite)."""
        try:
            log_dir = self.settings.log_file.parent if self.settings.log_file else None
            if log_dir is None:
                return
            log_dir.mkdir(parents=True, exist_ok=True)
            dest = log_dir / "last-inference-response.json"
            dest.write_text(json.dumps({"response": response_text}, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.debug(f"Could not write last-inference-response.json: {e}")

    def _credential(self) -> str:
        """The API key for this side (request key, admin-set shared key, or the keyless placeholder)."""
        from backend.services.admin_settings_store import resolve_shared_value

        kwargs = self.llm_config.model_kwargs
        shared_key_env = kwargs.get("shared_api_key_env")
        if shared_key_env:
            api_key = self.api_key or resolve_shared_value(shared_key_env, self.settings.data_path)
            if not api_key:
                raise LLMConfigurationError(
                    f"API key not configured. POST it to /api/config/remote-fields as "
                    f'{{"values": {{"{shared_key_env}": ...}}}}.'
                )
            return api_key
        api_key_env = kwargs.get("api_key_env")
        if api_key_env:
            if not self.api_key:
                raise LLMConfigurationError(
                    f"No API key for {api_key_env}: enter it in the plugin's preferences "
                    "(it is sent with each request, never read from the environment)."
                )
            return self.api_key
        if self._session_endpoint() is not None:
            return getattr(self, "_session_key", None) or KEYLESS_API_KEY
        # No key declared: an OpenAI-compatible server that needs none.
        return self.api_key or KEYLESS_API_KEY

    def _session_endpoint(self):
        """The ``SessionEndpoint`` of a provider that runs one endpoint per user session, else None."""
        # getattr: tests build services with __new__ and skip __init__.
        if getattr(self, "_session", None) is None:
            from backend.services.session_endpoint import SessionEndpoint, uses_session_endpoints

            provider = self._llm_provider()
            self._session = SessionEndpoint(provider) if uses_session_endpoints(provider) else False
        return self._session or None

    def _llm_provider(self):
        if self._provider is None:
            from backend.providers import ProviderConfigError, get_providers

            try:
                self._provider = get_providers(self.preset)["llm"]
            except ProviderConfigError:
                return None
        return self._provider

    def _derives_endpoint(self) -> bool:
        """True when this side's URL is found from the key (or the caller's session), not the preset."""
        if self._session_endpoint() is not None:
            return True
        kwargs = self.llm_config.model_kwargs
        provider = self._llm_provider()
        return bool(
            provider is not None
            and getattr(provider, "derives_endpoint_url", False)
            and not kwargs.get("base_url")
            and not kwargs.get("shared_base_url_env")
        )

    def _forget_endpoint(self) -> None:
        """Drop a derived endpoint that stopped answering, so the next call looks it up again."""
        if self._session_endpoint() is not None:
            self._resolved_base_url = None
            self._session_key = None
            self._openai_client = None
            return
        if self._resolved_base_url and self._provider is not None:
            from backend.services.endpoint_cache import endpoint_cache

            endpoint_cache.invalidate(self._provider.id, "llm", self.api_key)
            self._resolved_base_url = None
            self._openai_client = None

    async def _ensure_not_paused(self) -> None:
        """Fail fast, without calling the endpoint, when its owner paused it."""
        provider = self._llm_provider()
        if provider is None or not getattr(provider, "supports_suspend", False):
            return
        import asyncio

        from backend.services.endpoint_cache import paused_cache

        try:
            key = self._credential()
        except LLMConfigurationError:
            return
        if await asyncio.to_thread(paused_cache.is_paused, provider, "llm", key):
            raise LLMEndpointUnavailableError(unavailable_message("llm", "The LLM endpoint is paused.", "paused"))

    async def _ensure_endpoint(self) -> None:
        """Look up the key-derived endpoint off the event loop, before the client is built."""
        if self._session_endpoint() is not None:
            await self._ensure_session_endpoint()
            return
        if self._openai_client is not None or self._resolved_base_url or not self._derives_endpoint():
            return
        import asyncio

        from backend.services.endpoint_cache import endpoint_cache

        url = await asyncio.to_thread(endpoint_cache.resolve, self._provider, "llm", self._credential())
        if not url:
            raise LLMEndpointUnavailableError(unavailable_message("llm", "The LLM endpoint is not provisioned."))
        self._resolved_base_url = url

    async def _ensure_session_endpoint(self) -> None:
        """Use the caller's own running endpoint (asked before every call: renewals change it)."""
        from backend.services.session_endpoint import SessionEndpointError

        try:
            creds = await self._session_endpoint().resolve()
        except SessionEndpointError as exc:
            raise LLMEndpointUnavailableError(
                unavailable_message("llm", f"The LLM endpoint is unavailable: {exc}.")
            ) from exc
        if creds is None or not creds.base_url:
            raise LLMEndpointUnavailableError(unavailable_message("llm", "Your LLM endpoint is not running."))
        if (creds.base_url, creds.api_key) != (self._resolved_base_url, getattr(self, "_session_key", None)):
            self._openai_client = None
        self._resolved_base_url = creds.base_url
        self._session_key = creds.api_key

    def _get_openai_client(self):
        """Lazy initialize OpenAI client."""
        if self._openai_client is None:
            try:
                from openai import AsyncOpenAI

                shared_url_env = self.llm_config.model_kwargs.get("shared_base_url_env")
                if shared_url_env:
                    from backend.services.admin_settings_store import resolve_shared_value
                api_key = self._credential()

                if shared_url_env:
                    base_url = resolve_shared_value(shared_url_env, self.settings.data_path)
                    if not base_url:
                        raise LLMConfigurationError(
                            f"Base URL not configured. POST it to /api/config/remote-fields as "
                            f'{{"values": {{"{shared_url_env}": ...}}}}.'
                        )
                    from backend.services.admin_settings_store import normalize_base_url
                    base_url = normalize_base_url(base_url)
                else:
                    base_url = self.llm_config.model_kwargs.get("base_url") or self._resolved_base_url

                # Allow per-preset timeout override via model_kwargs; default 120 s
                timeout = float(self.llm_config.model_kwargs.get("timeout", 120))
                if base_url:
                    self._openai_client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout)
                    logger.info(f"Initialized OpenAI-compatible client with base_url: {base_url}, timeout: {timeout}s")
                else:
                    self._openai_client = AsyncOpenAI(api_key=api_key, timeout=timeout)
                    logger.info(f"Initialized OpenAI client, timeout: {timeout}s")

            except ImportError:
                logger.error("OpenAI package not installed. Install with: uv add openai")
                raise RuntimeError("Missing openai package")

        return self._openai_client

    def _resolve_api_key(self) -> Optional[str]:
        """The key for this side: the request's key, else the admin-set shared store."""
        kwargs = self.llm_config.model_kwargs
        shared_key_env = kwargs.get("shared_api_key_env")
        if shared_key_env:
            from backend.services.admin_settings_store import resolve_shared_value

            return self.api_key or resolve_shared_value(shared_key_env, self.settings.data_path)
        return self.api_key

    def _record_usage(self, headers: Any) -> None:
        """Remember the response's rate-limit headers for the usage meters."""
        try:
            usage_recorder.record("llm", headers, key_fingerprint(self._resolve_api_key()))
        except Exception:  # display-only signal, never break a generation
            logger.debug("Could not record usage headers", exc_info=True)

    def _llm_api(self) -> str:
        """Wire protocol of the LLM side, as declared by its provider."""
        from backend.providers import ProviderConfigError, get_providers

        try:
            return get_providers(self.preset)["llm"].llm_api
        except ProviderConfigError:
            return "openai"

    def _get_anthropic_client(self):
        """Lazy initialize Anthropic client."""
        if self._anthropic_client is None:
            try:
                from anthropic import AsyncAnthropic

                api_key = self._resolve_api_key()
                if not api_key:
                    raise LLMConfigurationError("Anthropic API key not provided")

                self._anthropic_client = AsyncAnthropic(api_key=api_key)
                logger.info("Initialized Anthropic client")

            except ImportError:
                logger.error("Anthropic package not installed. Install with: uv add anthropic")
                raise RuntimeError("Missing anthropic package")

        return self._anthropic_client

    async def generate(
        self,
        prompt: str,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None
    ) -> str:
        """Generate text using remote API."""
        # Use config defaults if not specified
        if max_tokens is None:
            max_tokens = 512
        if temperature is None:
            temperature = self.llm_config.temperature

        try:
            await self._ensure_not_paused()
            await self._ensure_endpoint()
            # The provider declares the wire protocol; core knows protocols, not vendors.
            dispatch = self._generate_anthropic if self._llm_api() == "anthropic" else self._generate_openai
            return await self._generate_with_rate_limit_backoff(dispatch, prompt, max_tokens, temperature)

        except (LLMConfigurationError, LLMEndpointUnavailableError, LLMRateLimitExhaustedError):
            raise
        except Exception as e:
            connection_error_types: tuple = ()
            try:
                from openai import APIConnectionError as OpenAIAPIConnectionError
                connection_error_types += (OpenAIAPIConnectionError,)
            except ImportError:
                pass
            try:
                from anthropic import APIConnectionError as AnthropicAPIConnectionError
                connection_error_types += (AnthropicAPIConnectionError,)
            except ImportError:
                pass
            if connection_error_types and isinstance(e, connection_error_types):
                self._forget_endpoint()
                raise LLMEndpointUnavailableError(
                    unavailable_message("llm", f"Could not connect to the LLM API ({self._model_name}): {e}.")
                ) from e
            kind = classify_status_error("llm", e)
            if kind:
                raise LLMEndpointUnavailableError(
                    unavailable_message("llm", f"The LLM API ({self._model_name}) is not available.", kind)
                ) from e
            # _extract_error_detail also handles an HTML error page from an
            # upstream gateway (e.g. RunPod's openresty edge) — the openai/
            # anthropic SDKs use that raw markup verbatim as both exc.body
            # and str(exc) when a response isn't JSON, which would otherwise
            # leak straight into this message. hasattr-gated since it's only
            # meaningful for an APIStatusError-shaped exception (one with a
            # .body attribute); anything else falls back to plain str(e).
            detail = _extract_error_detail(e) if hasattr(e, "body") else str(e)
            logger.error(f"Error during remote generation: {e}", exc_info=True)
            raise RuntimeError(f"Remote LLM generation failed: {detail}") from e

    @staticmethod
    def _rate_limit_exception_types() -> tuple:
        """openai.RateLimitError and/or anthropic.RateLimitError, whichever is installed."""
        types: tuple = ()
        try:
            from openai import RateLimitError as OpenAIRateLimitError
            types += (OpenAIRateLimitError,)
        except ImportError:
            pass
        try:
            from anthropic import RateLimitError as AnthropicRateLimitError
            types += (AnthropicRateLimitError,)
        except ImportError:
            pass
        return types

    async def _generate_with_rate_limit_backoff(
        self,
        dispatch: Callable[[str, int, float], "Any"],
        prompt: str,
        max_tokens: int,
        temperature: float,
    ) -> str:
        """Call ``dispatch`` with the same rate-limit handling as
        ``RemoteEmbeddingService._create_embeddings_with_backoff``: a short
        server-supplied wait is slept through and retried in-process; a longer
        or missing one raises ``LLMRateLimitExhaustedError`` immediately.

        Also honors (and contributes to) the process-wide cooldown in
        ``usage_meters`` for this provider+key, so a 429 seen by the routing
        call, a previous question, or a concurrent request is respected here
        too instead of every call site re-discovering the same limit itself.
        """
        rate_limit_errors = self._rate_limit_exception_types()
        fp = key_fingerprint(self._resolve_api_key())

        for attempt in range(_MAX_RATE_LIMIT_ATTEMPTS):
            try:
                await wait_out_known_rate_limit("llm", fp, _MAX_RATE_LIMIT_WAIT)
            except RateLimitStillActive as exc:
                raise LLMRateLimitExhaustedError(
                    f"LLM rate limit still in effect (seen on an earlier request); "
                    f"available at {exc.available_at.isoformat()} UTC.",
                    available_at=exc.available_at,
                ) from exc

            if not rate_limit_errors:
                return await dispatch(prompt, max_tokens, temperature)

            try:
                result = await dispatch(prompt, max_tokens, temperature)
                usage_recorder.clear_rate_limit("llm", fp)
                return result
            except rate_limit_errors as exc:
                headers = getattr(exc, "response", None) and exc.response.headers
                if headers:
                    self._record_usage(headers)
                retry_after = extract_retry_after(headers) if headers else None

                if retry_after is None or retry_after > _MAX_RATE_LIMIT_WAIT:
                    available_at = datetime.now(timezone.utc) + timedelta(
                        seconds=retry_after or _MAX_RATE_LIMIT_WAIT
                    )
                    usage_recorder.mark_rate_limited("llm", available_at, fp)
                    raise LLMRateLimitExhaustedError(
                        f"LLM rate limit exceeded ({self._model_name}); "
                        f"available again at {available_at.isoformat()} UTC.",
                        available_at=available_at,
                    ) from exc

                retry_after += random.uniform(0, 1)  # small jitter to avoid thundering herd
                available_at = datetime.now(timezone.utc) + timedelta(seconds=retry_after)
                usage_recorder.mark_rate_limited("llm", available_at, fp)
                if attempt == _MAX_RATE_LIMIT_ATTEMPTS - 1:
                    raise LLMRateLimitExhaustedError(
                        f"LLM rate limit exceeded ({self._model_name}) after "
                        f"{_MAX_RATE_LIMIT_ATTEMPTS} attempts.",
                        available_at=available_at,
                    ) from exc
                logger.warning(
                    "LLM rate limit hit (attempt %d/%d). Waiting %.1fs (server-supplied).",
                    attempt + 1, _MAX_RATE_LIMIT_ATTEMPTS, retry_after,
                )
                # The next loop iteration's wait_out_known_rate_limit call (top of
                # the loop) performs the actual sleep for this cooldown — not here
                # — so a cooldown discovered mid-retry and one discovered by a
                # completely different call are both waited out through the same
                # one code path instead of this method sleeping a second time.
        # Unreachable: the loop above always returns or raises.
        raise LLMRateLimitExhaustedError(
            "LLM rate limit retries exhausted.", available_at=datetime.now(timezone.utc)
        )

    async def _generate_openai(
        self,
        prompt: str,
        max_tokens: int,
        temperature: float
    ) -> str:
        """Generate using OpenAI API."""
        client = self._get_openai_client()

        logger.info(f"Generating with OpenAI ({self._model_name}), prompt={len(prompt)} chars")
        payload = {
            "model": self._model_name,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        extra_body = self.llm_config.model_kwargs.get("extra_body")
        self._dump_inference_request({**payload, **({"extra_body": extra_body} if extra_body else {})})

        raw = await client.chat.completions.with_raw_response.create(**payload, extra_body=extra_body)
        self._record_usage(raw.headers)
        response = raw.parse()

        choice = response.choices[0]
        generated_text = choice.message.content
        if not generated_text:
            raise RuntimeError(
                f"Model '{self._model_name}' returned no content "
                f"(finish_reason={choice.finish_reason!r}) — the completion was likely "
                "truncated before producing an answer, e.g. by exhausting max_tokens "
                "while still in an internal reasoning phase"
            )
        logger.info(f"Generated {len(generated_text)} characters")
        self._dump_inference_response(generated_text)
        return generated_text.strip()

    async def _generate_anthropic(
        self,
        prompt: str,
        max_tokens: int,
        temperature: float
    ) -> str:
        """Generate using Anthropic API."""
        client = self._get_anthropic_client()

        logger.info(f"Generating with Anthropic ({self._model_name}), prompt={len(prompt)} chars")
        payload = {
            "model": self._model_name,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        self._dump_inference_request(payload)

        raw = await client.messages.with_raw_response.create(**payload)
        self._record_usage(raw.headers)
        response = raw.parse()

        generated_text = response.content[0].text
        if not generated_text:
            raise RuntimeError(
                f"Model '{self._model_name}' returned no text content "
                f"(stop_reason={response.stop_reason!r}) — the completion was likely "
                "truncated before producing an answer"
            )
        logger.info(f"Generated {len(generated_text)} characters")
        self._dump_inference_response(generated_text)
        return generated_text.strip()


class MockLLMService(LLMService):
    """Returns a canned response. Used when TESTING=true — no model or API key needed."""

    async def generate(self, prompt: str, max_tokens: Optional[int] = None, temperature: Optional[float] = None) -> str:
        logger.info("MockLLMService: returning canned response")
        return "This is a mock response generated in testing mode."


def create_llm_service(
    settings: Settings,
    cache_dir: Optional[str] = None,
    api_key: Optional[str] = None,
    hf_token: Optional[str] = None,
    model_name_override: Optional[str] = None,
) -> LLMService:
    """
    Factory function to create the appropriate LLM service.

    Args:
        settings: Application settings.
        cache_dir: Directory to cache model weights (for local models).
        api_key: API key (for remote services).
        hf_token: HuggingFace token (for local models).
        model_name_override: Override the preset default model name.

    Returns:
        LLM service instance (local or remote).
    """
    preset = settings.get_hardware_preset()

    if preset.llm.model_type == "local":
        from backend.pluginhost import select_local_engine

        engine = select_local_engine("llm", preset.llm.engine)
        return engine.create(settings, cache_dir=cache_dir, hf_token=hf_token, model_name_override=model_name_override)
    else:
        return RemoteLLMService(settings, api_key=api_key, model_name_override=model_name_override)
