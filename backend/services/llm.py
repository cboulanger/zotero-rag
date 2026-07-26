"""
LLM service for text generation.

Supports both local models (via transformers) and remote APIs (OpenAI, Anthropic).
"""

import json
import logging
import os
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional

from backend.config.settings import Settings
from backend.services.embeddings import env_var_to_header, docs_url_for_key

logger = logging.getLogger(__name__)


class LLMService(ABC):
    """Abstract base class for LLM services."""

    @staticmethod
    def required_api_keys(settings: "Settings") -> list[dict]:
        """Return API keys required by this service (empty for local services)."""
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
        temperature: Optional[float] = None,
        is_valid: Optional[Callable[[str], bool]] = None,
    ) -> str:
        """
        Generate text completion for a prompt.

        Args:
            prompt: Input prompt text.
            max_tokens: Maximum tokens to generate.
            temperature: Sampling temperature.
            is_valid: Optional caller-supplied check on the raw response
                (e.g. "does this parse as the JSON shape I expect?").
                Single-model implementations accept and ignore this -- it
                exists purely so a caller can opt into AutoSelectLLMService's
                model-rotation-on-bad-response behavior (see that class)
                without every call site needing to know which concrete
                LLMService it was handed.

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
        temperature: Optional[float] = None,
        is_valid: Optional[Callable[[str], bool]] = None,
    ) -> str:
        """Generate text using local model. `is_valid` is accepted for
        interface compatibility but ignored -- a single fixed local model
        has no alternative to rotate to."""
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
    def required_api_keys(settings: Settings) -> list[dict]:
        """Return the API key required by this remote LLM service."""
        config = settings.get_hardware_preset().llm
        if config.model_type != "remote":
            return []
        if "api_key_env" in config.model_kwargs:
            api_key_env = config.model_kwargs["api_key_env"]
        elif "claude" in config.model_name.lower() or "anthropic" in config.model_name.lower():
            api_key_env = "ANTHROPIC_API_KEY"
        else:
            api_key_env = "OPENAI_API_KEY"
        return [{
            "key_name": api_key_env,
            "header_name": env_var_to_header(api_key_env),
            "description": f"API key for remote LLM ({config.model_name})",
            "docs_url": docs_url_for_key(api_key_env),
            "required_for": ["querying"],
        }]

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

                # Get API key from config or default environment variable
                api_key_env = self.llm_config.model_kwargs.get("api_key_env", "OPENAI_API_KEY")
                api_key = self.api_key or os.getenv(api_key_env)
                if not api_key:
                    raise ValueError(f"API key not found in environment variable: {api_key_env}")

                # Check for custom base URL (for OpenAI-compatible APIs)
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

    def _get_anthropic_client(self):
        """Lazy initialize Anthropic client."""
        if self._anthropic_client is None:
            try:
                from anthropic import AsyncAnthropic

                api_key = self.api_key or os.getenv("ANTHROPIC_API_KEY")
                if not api_key:
                    raise ValueError("Anthropic API key not provided")

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
        temperature: Optional[float] = None,
        is_valid: Optional[Callable[[str], bool]] = None,
    ) -> str:
        """Generate text using remote API. `is_valid` is accepted for
        interface compatibility but ignored -- a single fixed remote model
        has no alternative to rotate to; see AutoSelectLLMService for the
        implementation that actually honors it."""
        # Use config defaults if not specified
        if max_tokens is None:
            max_tokens = 512
        if temperature is None:
            temperature = self.llm_config.temperature

        model_name = self._model_name.lower()

        try:
            # Determine provider based on model name or base_url
            # If base_url is set, assume OpenAI-compatible API
            has_base_url = "base_url" in self.llm_config.model_kwargs

            if has_base_url or "gpt" in model_name or "openai" in model_name or "llama" in model_name:
                return await self._generate_openai(prompt, max_tokens, temperature)
            elif "claude" in model_name or "anthropic" in model_name:
                return await self._generate_anthropic(prompt, max_tokens, temperature)
            else:
                raise ValueError(f"Unsupported remote model: {self._model_name}")

        except Exception as e:
            logger.error(f"Error during remote generation: {e}", exc_info=True)
            raise RuntimeError(f"Remote LLM generation failed: {e}") from e

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
        self._dump_inference_request(payload)

        response = await client.chat.completions.create(**payload)

        generated_text = response.choices[0].message.content
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

        response = await client.messages.create(**payload)

        generated_text = response.content[0].text
        logger.info(f"Generated {len(generated_text)} characters")
        self._dump_inference_response(generated_text)
        return generated_text.strip()


class MockLLMService(LLMService):
    """Returns a canned response. Used when TESTING=true — no model or API key needed."""

    async def generate(
        self,
        prompt: str,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        is_valid: Optional[Callable[[str], bool]] = None,
    ) -> str:
        logger.info("MockLLMService: returning canned response")
        return "This is a mock response generated in testing mode."


class AutoSelectLLMService(LLMService):
    """Wraps an ordered list of candidate model names (most-available/
    least-busy first) and retries .generate() against the next candidate
    whenever a call either raises (network error, remote 500, context-
    length overflow, etc.) or -- when the caller supplies `is_valid` --
    returns a response the caller can't actually use (e.g. a model
    hallucinating a fictitious tool call instead of the requested JSON).

    Never hardcodes a model name itself: candidates are resolved by the
    caller (see make_llm_service's auto_select_model path) from whatever
    the active preset/provider actually reports as available, so this
    class works for any provider with more than one selectable model, not
    just KISSKI.
    """

    def __init__(self, candidates: List[str], service_factory: Callable[[str], LLMService]):
        if not candidates:
            raise ValueError("AutoSelectLLMService requires at least one candidate model")
        self._candidates = candidates
        self._services = {name: service_factory(name) for name in candidates}
        self._last_used_model: Optional[str] = None

    @property
    def model_name(self) -> str:
        return self._last_used_model or f"auto({len(self._candidates)} candidates)"

    async def generate(
        self,
        prompt: str,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        is_valid: Optional[Callable[[str], bool]] = None,
    ) -> str:
        last_exc: Optional[Exception] = None
        last_failed_model: Optional[str] = None
        for name in self._candidates:
            service = self._services[name]
            try:
                raw = await service.generate(prompt=prompt, max_tokens=max_tokens, temperature=temperature)
            except Exception as exc:  # noqa: BLE001 — try the next candidate
                logger.warning("AutoSelectLLMService: model %r failed, trying next candidate: %s", name, exc)
                last_exc = exc
                last_failed_model = name
                continue
            if is_valid is not None and not is_valid(raw):
                logger.warning("AutoSelectLLMService: model %r returned an unusable response, trying next candidate", name)
                last_exc = None
                last_failed_model = name
                continue
            self._last_used_model = name
            return raw
        # Both branches below surface how many models were tried and which
        # one failed last -- a bare re-raise of the last exception would
        # otherwise look like a single-model failure to anything that only
        # logs str(exc) (e.g. this app's job-status error field).
        detail = f"all {len(self._candidates)} candidate models exhausted (last tried: {last_failed_model!r})"
        if last_exc is not None:
            raise RuntimeError(f"AutoSelectLLMService: {detail}: {last_exc}") from last_exc
        raise RuntimeError(f"AutoSelectLLMService: {detail}, all returned unusable responses")


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
