"""
LLM service for text generation.

Supports both local models (via transformers) and remote APIs (OpenAI, Anthropic).
"""

import logging
import os
from abc import ABC, abstractmethod
from typing import List, Optional

from backend.config.settings import Settings

logger = logging.getLogger(__name__)


class LLMService(ABC):
    """Abstract base class for LLM services."""

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
    ):
        """
        Initialize local LLM service.

        Args:
            settings: Application settings.
            cache_dir: Directory to cache model weights.
            hf_token: HuggingFace token for model downloads.
        """
        self.settings = settings
        self.preset = settings.get_hardware_preset()
        self.llm_config = self.preset.llm
        self.cache_dir = cache_dir
        self.hf_token = hf_token

        self._model = None
        self._tokenizer = None

        logger.info(f"Initialized LocalLLMService with model: {self.llm_config.model_name}")

    def _load_model(self):
        """Lazy load the LLM model and tokenizer."""
        if self._model is not None:
            return

        logger.info(f"Loading LLM model: {self.llm_config.model_name}")

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
                self.llm_config.model_name,
                cache_dir=self.cache_dir,
                token=self.hf_token,
            )

            # Load model
            logger.info("Loading model weights...")
            self._model = AutoModelForCausalLM.from_pretrained(
                self.llm_config.model_name,
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
    ):
        """
        Initialize remote LLM service.

        Args:
            settings: Application settings.
            api_key: API key for the remote service.
        """
        self.settings = settings
        self.preset = settings.get_hardware_preset()
        self.llm_config = self.preset.llm
        self.api_key = api_key

        # Initialize API clients
        self._openai_client = None
        self._anthropic_client = None

        logger.info(f"Initialized RemoteLLMService with model: {self.llm_config.model_name}")

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
                if base_url:
                    self._openai_client = AsyncOpenAI(api_key=api_key, base_url=base_url)
                    logger.info(f"Initialized OpenAI-compatible client with base_url: {base_url}")
                else:
                    self._openai_client = AsyncOpenAI(api_key=api_key)
                    logger.info("Initialized OpenAI client")

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
        temperature: Optional[float] = None
    ) -> str:
        """Generate text using remote API."""
        # Use config defaults if not specified
        if max_tokens is None:
            max_tokens = 512
        if temperature is None:
            temperature = self.llm_config.temperature

        model_name = self.llm_config.model_name.lower()

        try:
            # Determine provider based on model name or base_url
            # If base_url is set, assume OpenAI-compatible API
            has_base_url = "base_url" in self.llm_config.model_kwargs

            if has_base_url or "gpt" in model_name or "openai" in model_name or "llama" in model_name:
                return await self._generate_openai(prompt, max_tokens, temperature)
            elif "claude" in model_name or "anthropic" in model_name:
                return await self._generate_anthropic(prompt, max_tokens, temperature)
            else:
                raise ValueError(f"Unsupported remote model: {self.llm_config.model_name}")

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

        logger.info(f"Generating with OpenAI ({self.llm_config.model_name})")

        response = await client.chat.completions.create(
            model=self.llm_config.model_name,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=temperature,
        )

        generated_text = response.choices[0].message.content
        logger.info(f"Generated {len(generated_text)} characters")
        return generated_text.strip()

    async def _generate_anthropic(
        self,
        prompt: str,
        max_tokens: int,
        temperature: float
    ) -> str:
        """Generate using Anthropic API."""
        client = self._get_anthropic_client()

        logger.info(f"Generating with Anthropic ({self.llm_config.model_name})")

        response = await client.messages.create(
            model=self.llm_config.model_name,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=[{"role": "user", "content": prompt}]
        )

        generated_text = response.content[0].text
        logger.info(f"Generated {len(generated_text)} characters")
        return generated_text.strip()


def create_llm_service(
    settings: Settings,
    cache_dir: Optional[str] = None,
    api_key: Optional[str] = None,
    hf_token: Optional[str] = None,
) -> LLMService:
    """
    Factory function to create the appropriate LLM service.

    Args:
        settings: Application settings.
        cache_dir: Directory to cache model weights (for local models).
        api_key: API key (for remote services).
        hf_token: HuggingFace token (for local models).

    Returns:
        LLM service instance (local or remote).
    """
    preset = settings.get_hardware_preset()

    if preset.llm.model_type == "local":
        return LocalLLMService(settings, cache_dir=cache_dir, hf_token=hf_token)
    else:
        return RemoteLLMService(settings, api_key=api_key)
