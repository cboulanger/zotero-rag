#!/usr/bin/env python
"""Test search functionality."""

import asyncio
from pathlib import Path
from backend.db.vector_store import VectorStore
from backend.services.embeddings import LocalEmbeddingService, EmbeddingConfig
from backend.config.settings import get_settings
from qdrant_client.models import Distance

async def main():
    # Get settings
    settings = get_settings()

    # Initialize embedding service
    config = EmbeddingConfig(
        model_name="sentence-transformers/all-MiniLM-L6-v2",
        embedding_dim=384
    )
    embedding_service = LocalEmbeddingService(
        config=config,
        cache_dir=str(settings.model_weights_path)
    )

    # Initialize vector store
    db_path = Path("/Volumes/Data-SSD/cache/zotero-rag/db")
    vector_store = VectorStore(
        storage_path=db_path,
        embedding_dim=384,
        distance=Distance.COSINE
    )

    # Generate query embedding
    query = "research"
    print(f"Generating embedding for query: '{query}'")
    query_embedding = await embedding_service.embed_text(query)
    print(f"Embedding dimension: {len(query_embedding)}")

    # Test search without library filter
    print("\n1. Search WITHOUT library filter:")
    results = vector_store.search(
        query_vector=query_embedding,
        limit=5,
        score_threshold=None,
        library_ids=None
    )
    print(f"   Results: {len(results)}")
    for i, result in enumerate(results[:3]):
        doc_meta = result.chunk.metadata.document_metadata
        lib_id = doc_meta.get('library_id') if isinstance(doc_meta, dict) else doc_meta.library_id
        title = doc_meta.get('title', '') if isinstance(doc_meta, dict) else (doc_meta.title or '')
        print(f"   {i+1}. Score: {result.score:.3f}, Library: {lib_id}, Title: {title[:50]}")

    # Test search with library filter
    print("\n2. Search WITH library filter ['6297749']:")
    results = vector_store.search(
        query_vector=query_embedding,
        limit=5,
        score_threshold=None,
        library_ids=["6297749"]
    )
    print(f"   Results: {len(results)}")
    for i, result in enumerate(results[:3]):
        doc_meta = result.chunk.metadata.document_metadata
        lib_id = doc_meta.get('library_id') if isinstance(doc_meta, dict) else doc_meta.library_id
        title = doc_meta.get('title', '') if isinstance(doc_meta, dict) else (doc_meta.title or '')
        print(f"   {i+1}. Score: {result.score:.3f}, Library: {lib_id}, Title: {title[:50]}")

    vector_store.close()

if __name__ == "__main__":
    asyncio.run(main())
