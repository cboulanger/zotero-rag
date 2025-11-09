"""
Configuration presets for different hardware scenarios.

Each preset defines the optimal models and settings for different hardware configurations.
"""

from typing import Literal, Optional
from pydantic import BaseModel, Field


class EmbeddingConfig(BaseModel):
    """Configuration for embedding models."""

    model_type: Literal["local", "remote"] = "local"
    model_name: str = Field(..., description="Model identifier or API endpoint")
    model_kwargs: dict = Field(default_factory=dict, description="Additional model parameters")
    batch_size: int = Field(default=32, description="Batch size for embedding generation")
    cache_enabled: bool = Field(default=True, description="Enable content-hash based caching")


class LLMConfig(BaseModel):
    """Configuration for LLM models."""

    model_type: Literal["local", "remote"] = "local"
    model_name: str = Field(..., description="Model identifier or API endpoint")
    quantization: Optional[Literal["4bit", "8bit", "none"]] = None
    max_context_length: int = Field(default=4096, description="Maximum context window size")
    temperature: float = Field(default=0.7, description="Sampling temperature")
    model_kwargs: dict = Field(default_factory=dict, description="Additional model parameters")


class RAGConfig(BaseModel):
    """Configuration for RAG retrieval."""

    top_k: int = Field(default=5, description="Number of chunks to retrieve")
    score_threshold: float = Field(default=0.7, description="Minimum similarity score")
    max_chunk_size: int = Field(default=512, description="Maximum tokens per chunk")


class HardwarePreset(BaseModel):
    """Complete hardware-specific configuration preset."""

    name: str
    description: str
    embedding: EmbeddingConfig
    llm: LLMConfig
    rag: RAGConfig
    memory_budget_gb: float = Field(..., description="Estimated memory usage in GB")


# Define available presets
PRESETS = {
    "mac-mini-m4-16gb": HardwarePreset(
        name="mac-mini-m4-16gb",
        description="Optimized for Mac Mini M4 with 16GB RAM",
        embedding=EmbeddingConfig(
            model_type="local",
            model_name="nomic-ai/nomic-embed-text-v1.5",
            model_kwargs={"trust_remote_code": True},
            batch_size=32,
        ),
        llm=LLMConfig(
            model_type="local",
            model_name="Qwen/Qwen2.5-3B-Instruct",
            quantization="4bit",
            max_context_length=4096,
            temperature=0.7,
            model_kwargs={"device_map": "auto", "trust_remote_code": True},
        ),
        rag=RAGConfig(
            top_k=5,
            score_threshold=0.7,
            max_chunk_size=512,
        ),
        memory_budget_gb=6.0,
    ),

    "gpu-high-memory": HardwarePreset(
        name="gpu-high-memory",
        description="For systems with dedicated GPU and >24GB RAM",
        embedding=EmbeddingConfig(
            model_type="local",
            model_name="sentence-transformers/all-mpnet-base-v2",
            batch_size=64,
        ),
        llm=LLMConfig(
            model_type="local",
            model_name="mistralai/Mistral-7B-Instruct-v0.3",
            quantization="8bit",
            max_context_length=8192,
            temperature=0.7,
            model_kwargs={"device_map": "auto"},
        ),
        rag=RAGConfig(
            top_k=10,
            score_threshold=0.65,
            max_chunk_size=768,
        ),
        memory_budget_gb=16.0,
    ),

    "cpu-only": HardwarePreset(
        name="cpu-only",
        description="CPU-optimized smaller models",
        embedding=EmbeddingConfig(
            model_type="local",
            model_name="sentence-transformers/all-MiniLM-L6-v2",
            batch_size=16,
        ),
        llm=LLMConfig(
            model_type="local",
            model_name="TinyLlama/TinyLlama-1.1B-Chat-v1.0",
            quantization="4bit",
            max_context_length=2048,
            temperature=0.7,
            model_kwargs={"device_map": "cpu"},
        ),
        rag=RAGConfig(
            top_k=5,
            score_threshold=0.7,
            max_chunk_size=384,
        ),
        memory_budget_gb=3.0,
    ),

    "remote-openai": HardwarePreset(
        name="remote-openai",
        description="Using OpenAI/Anthropic remote inference endpoints",
        embedding=EmbeddingConfig(
            model_type="remote",
            model_name="openai",  # Will use OpenAI embeddings API
            batch_size=100,
        ),
        llm=LLMConfig(
            model_type="remote",
            model_name="gpt-4o-mini",  # Or anthropic/claude-3-5-sonnet
            max_context_length=128000,
            temperature=0.7,
        ),
        rag=RAGConfig(
            top_k=10,
            score_threshold=0.7,
            max_chunk_size=1024,
        ),
        memory_budget_gb=1.0,  # Minimal local memory needed
    ),

    "remote-kisski": HardwarePreset(
        name="remote-kisski",
        description="Using GWDG KISSKI OpenAI-compatible API (Academic Cloud)",
        embedding=EmbeddingConfig(
            model_type="local",  # Use local embeddings for privacy
            model_name="sentence-transformers/all-MiniLM-L6-v2",
            batch_size=32,
        ),
        llm=LLMConfig(
            model_type="remote",
            model_name="meta-llama/Llama-3.3-70B-Instruct",  # Fast, 128k context
            max_context_length=128000,
            temperature=0.7,
            model_kwargs={
                "base_url": "https://chat-ai.academiccloud.de/v1",
                "api_key_env": "KISSKI_API_KEY",  # Uses KISSKI_API_KEY from .env
            },
        ),
        rag=RAGConfig(
            top_k=10,
            score_threshold=0.7,
            max_chunk_size=1024,
        ),
        memory_budget_gb=1.0,  # Minimal local memory needed
    ),
}


def get_preset(name: str) -> HardwarePreset:
    """
    Get a hardware preset by name.

    Args:
        name: Preset name (e.g., "mac-mini-m4-16gb")

    Returns:
        HardwarePreset configuration

    Raises:
        ValueError: If preset name is not found
    """
    if name not in PRESETS:
        available = ", ".join(PRESETS.keys())
        raise ValueError(f"Unknown preset '{name}'. Available: {available}")

    return PRESETS[name]


def list_presets() -> list[str]:
    """List all available preset names."""
    return list(PRESETS.keys())
