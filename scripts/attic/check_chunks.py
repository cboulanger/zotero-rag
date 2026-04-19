#!/usr/bin/env python
"""Quick script to check chunks in the vector database."""

from pathlib import Path
from backend.db.vector_store import VectorStore
from qdrant_client.models import Distance

# Initialize vector store
db_path = Path("/Volumes/Data-SSD/cache/zotero-rag/db")
vector_store = VectorStore(
    storage_path=db_path,
    embedding_dim=384,
    distance=Distance.COSINE
)

# Get collection info
info = vector_store.get_collection_info()
print(f"Collection info: {info}")

# Count chunks for library 6297749
count = vector_store.count_library_chunks("6297749")
print(f"Chunks for library 6297749: {count}")

# Try to scroll through some chunks to see their library_id
from qdrant_client.models import Filter, FieldCondition, MatchValue

results, _ = vector_store.client.scroll(
    collection_name=vector_store.CHUNKS_COLLECTION,
    limit=5,
)

print(f"\nFirst 5 chunks:")
for point in results:
    print(f"  ID: {point.id}")
    print(f"  Library ID: {point.payload.get('library_id')}")
    print(f"  Item key: {point.payload.get('item_key')}")
    print(f"  Title: {point.payload.get('title', '')[:50]}")
    print()

vector_store.close()
