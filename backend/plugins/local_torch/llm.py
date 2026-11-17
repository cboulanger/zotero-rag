"""Local text generation with Hugging Face transformers on torch, with optional quantization."""

import logging
import os
from typing import Optional

from backend.config.settings import Settings
from backend.services.llm import LLMService

logger = logging.getLogger(__name__)


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
