"""Backend plugin: local inference on torch, in the backend process.

Offers the ``torch`` engine for all three sides of a ``local`` preset section:
embeddings with sentence-transformers, answering with Hugging Face transformers
(optionally quantized with bitsandbytes) and re-ranking with a sentence-transformers
``CrossEncoder``. Runs on CPU, CUDA and Apple MPS; needs the ``local-models`` extra
(``uv sync --extra local-models``). Without it the capabilities stay registered and
``/status`` names the missing packages.
"""

from backend.pluginhost import BackendPlugin, LocalInference


def _embedding(config, cache_dir=None, hf_token=None):
    from .embedding import LocalEmbeddingService

    return LocalEmbeddingService(config, cache_dir=cache_dir, hf_token=hf_token)


def _llm(settings, cache_dir=None, hf_token=None, model_name_override=None):
    from .llm import LocalLLMService

    return LocalLLMService(settings, cache_dir=cache_dir, hf_token=hf_token, model_name_override=model_name_override)


def _rerank(config):
    from .rerank import LocalCrossEncoderReranker

    return LocalCrossEncoderReranker(config.model_name, config.max_length, config.batch_size, config.model_kwargs)


def register() -> BackendPlugin:
    return BackendPlugin(
        id="local_torch",
        name="Local inference (torch)",
        version="1.0",
        description="Embeddings, answering and re-ranking in the backend process with torch "
                    "(sentence-transformers / transformers; CPU, CUDA or Apple MPS).",
        capabilities=[
            LocalInference(side="embedding", engine="torch", create=_embedding, requires=("sentence_transformers",),
                           label="sentence-transformers embeddings"),
            LocalInference(side="llm", engine="torch", create=_llm, requires=("transformers", "torch"),
                           label="transformers text generation"),
            LocalInference(side="rerank", engine="torch", create=_rerank, requires=("sentence_transformers",),
                           label="sentence-transformers cross-encoder"),
        ],
    )
