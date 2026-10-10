"""
LLM service for text generation.

Supports both local models (via transformers) and remote APIs (OpenAI, Anthropic).
"""

import json
import logging
import os
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from backend.config.settings import Settings
from backend.services.embeddings import KEYLESS_API_KEY, env_var_to_header, docs_url_for, _extract_error_detail
from backend.services.endpoint_errors import classify_status_error, unavailable_message
from backend.services.usage_meters import key_fingerprint, recorder as usage_recorder

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


class LocalLLMService(LLMService):
    """
    LLM service using local models via transformers.

    Uses HuggingFace transformers for local inference with optional quantization.
    """

    def __init__(
        self,
        settings: Settings,
        cache_dir: Optional[str] = None,
        hf_token: Optional[str] = None,
        model_name_override: Optional[str] = None,
    ):
        """
        Initialize local LLM service.

        Args:
            settings: Application settings.
            cache_dir: Directory to cache model weights.
            hf_token: HuggingFace token for model downloads.
            model_name_override: Override the preset default model name.
        """
        self.settings = settings
        self.preset = settings.get_hardware_preset()
        self.llm_config = self.preset.llm
        self.cache_dir = cache_dir
        self.hf_token = hf_token
        self._model_name = model_name_override or self.llm_config.model_name

        self._model = None
        self._tokenizer = None

        logger.info(f"Initialized LocalLLMService with model: {self._model_name}")

    @property
    def model_name(self) -> str:
        return self._model_name

    def _load_model(self):
        """Lazy load the LLM model and tokenizer."""
        if self._model is not None:
            return

        logger.info(f"Loading LLM model: {self._model_name}")

        try:
            from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
            import torch

            # Set HuggingFace token if provided
            if self.hf_token:
                os.environ["HF_TOKEN"] = self.hf_token

            # Build model kwargs
            model_kwargs = self.llm_config.model_kwargs.copy()

            # Add cache directory if provided
            if self.cache_dir:
                model_kwargs["cache_dir"] = self.cache_dir

            # Configure quantization if specified
            if self.llm_config.quantization and self.llm_config.quantization != "none":
                logger.info(f"Configuring {self.llm_config.quantization} quantization")

                if self.llm_config.quantization == "4bit":
                    bnb_config = BitsAndBytesConfig(
                        load_in_4bit=True,
                        bnb_4bit_use_double_quant=True,
                        bnb_4bit_quant_type="nf4",
                        bnb_4bit_compute_dtype=torch.float16,
                    )
                    model_kwargs["quantization_config"] = bnb_config
                    model_kwargs["device_map"] = "auto"

                elif self.llm_config.quantization == "8bit":
                    bnb_config = BitsAndBytesConfig(
                        load_in_8bit=True,
                    )
                    model_kwargs["quantization_config"] = bnb_config
                    model_kwargs["device_map"] = "auto"

            # Load tokenizer
            logger.info("Loading tokenizer...")
            self._tokenizer = AutoTokenizer.from_pretrained(
                self._model_name,
                cache_dir=self.cache_dir,
                token=self.hf_token,
            )

            # Load model
            logger.info("Loading model weights...")
            self._model = AutoModelForCausalLM.from_pretrained(
                self._model_name,
                token=self.hf_token,
                **model_kwargs
            )

            logger.info("Model loaded successfully")

        except ImportError as e:
            logger.error(f"Missing required dependencies for local LLM: {e}")
            logger.error("Install with: uv add transformers torch bitsandbytes accelerate")
            raise RuntimeError("Missing dependencies for local LLM inference") from e
        except Exception as e:
            logger.error(f"Failed to load LLM model: {e}", exc_info=True)
            raise

    async def generate(
        self,
        prompt: str,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None
    ) -> str:
        """Generate text using local model."""
        self._load_model()

        # Use config defaults if not specified
        if max_tokens is None:
            max_tokens = 512  # Reasonable default for answers
        if temperature is None:
            temperature = self.llm_config.temperature

        logger.info(f"Generating completion (max_tokens={max_tokens}, temp={temperature})")

        try:
            # Tokenize input
            inputs = self._tokenizer(prompt, return_tensors="pt")

            # Move to same device as model
            if hasattr(self._model, "device"):
                inputs = {k: v.to(self._model.device) for k, v in inputs.items()}

            # Generate
            outputs = self._model.generate(
                **inputs,
                max_new_tokens=max_tokens,
                temperature=temperature,
                do_sample=temperature > 0,
                pad_token_id=self._tokenizer.eos_token_id,
            )

            # Decode output
            # Skip the input tokens to get only the generated part
            generated_ids = outputs[0][inputs["input_ids"].shape[1]:]
            generated_text = self._tokenizer.decode(generated_ids, skip_special_tokens=True)

            logger.info(f"Generated {len(generated_text)} characters")
            return generated_text.strip()

        except Exception as e:
            logger.error(f"Error during generation: {e}", exc_info=True)
            raise RuntimeError(f"LLM generation failed: {e}") from e


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

    def _get_openai_client(self):
        """Lazy initialize OpenAI client."""
        if self._openai_client is None:
            try:
                from openai import AsyncOpenAI

                shared_url_env = self.llm_config.model_kwargs.get("shared_base_url_env")
                shared_key_env = self.llm_config.model_kwargs.get("shared_api_key_env")
                if shared_url_env or shared_key_env:
                    from backend.services.admin_settings_store import resolve_shared_value

                if shared_key_env:
                    api_key = self.api_key or resolve_shared_value(shared_key_env, self.settings.data_path)
                    if not api_key:
                        raise LLMConfigurationError(
                            f"API key not configured. POST it to /api/config/remote-fields as "
                            f'{{"values": {{"{shared_key_env}": ...}}}}.'
                        )
                else:
                    api_key_env = self.llm_config.model_kwargs.get("api_key_env")
                    if api_key_env:
                        api_key = self.api_key
                        if not api_key:
                            raise LLMConfigurationError(
                                f"No API key for {api_key_env}: enter it in the plugin's preferences "
                                "(it is sent with each request, never read from the environment)."
                            )
                    else:
                        # No key declared: an OpenAI-compatible server that needs none.
                        api_key = self.api_key or KEYLESS_API_KEY

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
                    base_url = self.llm_config.model_kwargs.get("base_url")

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
            # The provider declares the wire protocol; core knows protocols, not vendors.
            if self._llm_api() == "anthropic":
                return await self._generate_anthropic(prompt, max_tokens, temperature)
            return await self._generate_openai(prompt, max_tokens, temperature)

        except (LLMConfigurationError, LLMEndpointUnavailableError):
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
        return LocalLLMService(settings, cache_dir=cache_dir, hf_token=hf_token, model_name_override=model_name_override)
    else:
        return RemoteLLMService(settings, api_key=api_key, model_name_override=model_name_override)
