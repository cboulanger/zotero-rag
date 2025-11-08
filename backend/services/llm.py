"""
LLM service for text generation.

Supports both local models (via transformers) and remote APIs (OpenAI, Anthropic).
"""

import logging
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
    """LLM service using local models via transformers."""

    def __init__(self, settings: Settings):
        """
        Initialize local LLM service.

        Args:
            settings: Application settings.
        """
        self.settings = settings
        self.preset = settings.get_hardware_preset()
        self.model = None  # Lazy loading

    async def generate(
        self,
        prompt: str,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None
    ) -> str:
        """Generate text using local model."""
        # TODO: Implement actual local model inference
        # This is a stub implementation
        logger.info(f"Generating completion for prompt (length: {len(prompt)})")
        return "This is a placeholder response. LLM service not yet implemented."


class RemoteLLMService(LLMService):
    """LLM service using remote APIs (OpenAI, Anthropic, etc.)."""

    def __init__(self, settings: Settings):
        """
        Initialize remote LLM service.

        Args:
            settings: Application settings.
        """
        self.settings = settings
        self.preset = settings.get_hardware_preset()

    async def generate(
        self,
        prompt: str,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None
    ) -> str:
        """Generate text using remote API."""
        # TODO: Implement actual remote API calls
        logger.info(f"Generating completion via remote API (length: {len(prompt)})")
        return "This is a placeholder response. Remote LLM service not yet implemented."


def create_llm_service(settings: Settings) -> LLMService:
    """
    Factory function to create the appropriate LLM service.

    Args:
        settings: Application settings.

    Returns:
        LLM service instance (local or remote).
    """
    preset = settings.get_hardware_preset()

    if preset.llm.model_type == "local":
        return LocalLLMService(settings)
    else:
        return RemoteLLMService(settings)
