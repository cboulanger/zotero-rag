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
    max_answer_tokens: int = Field(default=2048, description="Maximum tokens for generated answers")
    temperature: float = Field(default=0.7, description="Sampling temperature")
    model_kwargs: dict = Field(default_factory=dict, description="Additional model parameters")


class RAGConfig(BaseModel):
    """Configuration for RAG retrieval."""

    top_k: int = Field(default=5, description="Number of chunks to retrieve")
    score_threshold: float = Field(default=0.3, description="Minimum similarity score (0.0-1.0)")
    max_chunk_size: int = Field(default=512, description="Maximum characters per chunk (passed to chunker as max_characters)")


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
    "apple-silicon-32gb": HardwarePreset(
        name="apple-silicon-32gb",
        description="Optimized for Apple Silicon Macs with 32GB RAM — local multilingual embeddings via MPS",
        embedding=EmbeddingConfig(
            model_type="local",
            model_name="intfloat/multilingual-e5-large-instruct",  # 1024-dim, same model as KISSKI remote; MPS-accelerated on Apple Silicon
            batch_size=64,
        ),
        llm=LLMConfig(
            model_type="local",
            model_name="mistralai/Mistral-7B-Instruct-v0.3",
            quantization="4bit",
            max_context_length=8192,
            max_answer_tokens=2048,
            temperature=0.7,
            model_kwargs={"device_map": "auto", "trust_remote_code": True},
        ),
        rag=RAGConfig(
            top_k=10,
            score_threshold=0.35,
            max_chunk_size=800,  # multilingual-e5-large-instruct has 512-token limit; ~2 chars/token for dense text
        ),
        memory_budget_gb=10.0,
    ),

    "high-memory": HardwarePreset(
        name="high-memory",
        description="For systems with >24GB RAM (GPU or Apple Silicon)",
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
            max_answer_tokens=2048,  # Larger model can handle more
            temperature=0.7,
            model_kwargs={"device_map": "auto"},
        ),
        rag=RAGConfig(
            top_k=10,
            score_threshold=0.4,  # all-mpnet-base-v2 produces higher quality scores
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
            max_answer_tokens=512,  # Small model, limited capacity
            temperature=0.7,
            model_kwargs={"device_map": "cpu"},
        ),
        rag=RAGConfig(
            top_k=5,
            score_threshold=0.3,  # all-MiniLM-L6-v2 tends to have lower absolute scores
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
            max_answer_tokens=4096,  # Large context window allows comprehensive answers
            temperature=0.7,
        ),
        rag=RAGConfig(
            top_k=10,
            score_threshold=0.5,  # OpenAI embeddings are well-calibrated, can use higher threshold
            max_chunk_size=1024,
        ),
        memory_budget_gb=1.0,  # Minimal local memory needed
    ),

    "apple-silicon-kisski": HardwarePreset(
        name="apple-silicon-kisski",
        description="Apple Silicon (16-32GB) with KISSKI remote embeddings + LLM (fully remote, no torch)",
        embedding=EmbeddingConfig(
            model_type="remote",
            model_name="multilingual-e5-large-instruct",  # KISSKI: good for multilingual academic content
            batch_size=64,
            model_kwargs={
                "base_url": "https://chat-ai.academiccloud.de/v1",
                "api_key_env": "KISSKI_API_KEY",
            },
        ),
        llm=LLMConfig(
            model_type="remote",
            model_name="llama-3.3-70b-instruct",  # KISSKI: high quality, 128k context
            max_context_length=128000,
            max_answer_tokens=4096,
            temperature=0.7,
            model_kwargs={
                "base_url": "https://chat-ai.academiccloud.de/v1",
                "api_key_env": "KISSKI_API_KEY",
            },
        ),
        rag=RAGConfig(
            top_k=10,
            score_threshold=0.35,
            max_chunk_size=800,  # multilingual-e5-large-instruct has 512-token limit; ~2 chars/token for dense text
        ),
        memory_budget_gb=0.5,  # Fully remote — minimal local footprint
    ),

    "remote-kisski": HardwarePreset(
        name="remote-kisski",
        description="Fully remote via GWDG KISSKI/SAIA Academic Cloud (no local GPU or torch required)",
        embedding=EmbeddingConfig(
            model_type="remote",
            model_name="multilingual-e5-large-instruct",  # KISSKI: 1024-dim, multilingual
            batch_size=256,  # Send more texts per API call to reduce round-trips
            model_kwargs={
                "base_url": "https://chat-ai.academiccloud.de/v1",
                "api_key_env": "KISSKI_API_KEY",
            },
        ),
        llm=LLMConfig(
            model_type="remote",
            model_name="llama-3.3-70b-instruct",  # KISSKI: high quality, 128k context
            max_context_length=128000,
            max_answer_tokens=4096,
            temperature=0.7,
            model_kwargs={
                "base_url": "https://chat-ai.academiccloud.de/v1",
                "api_key_env": "KISSKI_API_KEY",
            },
        ),
        rag=RAGConfig(
            top_k=10,
            score_threshold=0.35,  # multilingual-e5-large-instruct scores
            max_chunk_size=800,    # multilingual-e5-large-instruct has 512-token limit; ~2 chars/token for dense text
        ),
        memory_budget_gb=0.5,  # Fully remote — no local model weights
    ),

    "cloud-server-kisski": HardwarePreset(
        name="cloud-server-kisski",
        description="Cloud server (16GB RAM, 4 vCPU, no GPU): local multilingual embeddings + KISSKI LLM",
        embedding=EmbeddingConfig(
            model_type="local",
            model_name="intfloat/multilingual-e5-small",  # ~470MB, CPU-friendly, multilingual
            batch_size=16,  # Conservative batch size for CPU-only inference
        ),
        llm=LLMConfig(
            model_type="remote",
            model_name="llama-3.3-70b-instruct",  # KISSKI: high quality, 128k context
            max_context_length=128000,
            max_answer_tokens=4096,
            temperature=0.7,
            model_kwargs={
                "base_url": "https://chat-ai.academiccloud.de/v1",
                "api_key_env": "KISSKI_API_KEY",
            },
        ),
        rag=RAGConfig(
            top_k=10,
            score_threshold=0.3,  # e5-small tends toward lower absolute scores
            max_chunk_size=768,
        ),
        memory_budget_gb=2.0,  # ~470MB model + overhead
    ),

    "windows-test": HardwarePreset(
        name="windows-test",
        description="Windows-compatible: fully remote via KISSKI (avoids PyTorch/CUDA setup)",
        embedding=EmbeddingConfig(
            model_type="remote",
            model_name="multilingual-e5-large-instruct",  # KISSKI embeddings
            batch_size=64,
            model_kwargs={
                "base_url": "https://chat-ai.academiccloud.de/v1",
                "api_key_env": "KISSKI_API_KEY",
            },
        ),
        llm=LLMConfig(
            model_type="remote",
            model_name="llama-3.3-70b-instruct",  # KISSKI LLM
            max_context_length=128000,
            max_answer_tokens=4096,
            temperature=0.7,
            model_kwargs={
                "base_url": "https://chat-ai.academiccloud.de/v1",
                "api_key_env": "KISSKI_API_KEY",
            },
        ),
        rag=RAGConfig(
            top_k=10,
            score_threshold=0.35,
            max_chunk_size=800,  # multilingual-e5-large-instruct has 512-token limit; ~2 chars/token for dense text
        ),
        memory_budget_gb=0.5,  # Fully remote
    ),
}


def get_preset(name: str) -> HardwarePreset:
    """
    Get a hardware preset by name.

    Args:
        name: Preset name (e.g., "apple-silicon-32gb")

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
