"""
Vector database interface using Qdrant.

Handles storage and retrieval of document chunk embeddings with metadata.
"""

import logging
from typing import Optional
from pathlib import Path
import uuid

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    VectorParams,
    PointStruct,
    Filter,
    FieldCondition,
    MatchValue,
    SearchParams,
)

from backend.models.document import DocumentChunk, ChunkMetadata, SearchResult, DeduplicationRecord


logger = logging.getLogger(__name__)


class VectorStore:
    """
    Vector database interface using Qdrant.

    Manages collections for document chunks and deduplication records.
    """

    CHUNKS_COLLECTION = "document_chunks"
    DEDUP_COLLECTION = "deduplication"

    def __init__(
        self,
        storage_path: Path,
        embedding_dim: int,
        distance: Distance = Distance.COSINE,
    ):
        """
        Initialize vector store.

        Args:
            storage_path: Path to Qdrant storage directory
            embedding_dim: Dimensionality of embeddings
            distance: Distance metric (COSINE, EUCLID, DOT)
        """
        self.storage_path = storage_path
        self.embedding_dim = embedding_dim
        self.distance = distance

        # Ensure storage directory exists
        storage_path.mkdir(parents=True, exist_ok=True)

        # Initialize Qdrant client with persistent storage
        self.client = QdrantClient(path=str(storage_path))

        logger.info(f"Initialized VectorStore at {storage_path}")

        # Create collections if they don't exist
        self._ensure_collections()

    def _ensure_collections(self):
        """Create collections if they don't exist."""
        # Check if chunks collection exists
        collections = self.client.get_collections().collections
        collection_names = [c.name for c in collections]

        if self.CHUNKS_COLLECTION not in collection_names:
            logger.info(f"Creating collection: {self.CHUNKS_COLLECTION}")
            self.client.create_collection(
                collection_name=self.CHUNKS_COLLECTION,
                vectors_config=VectorParams(
                    size=self.embedding_dim,
                    distance=self.distance,
                ),
            )

        if self.DEDUP_COLLECTION not in collection_names:
            logger.info(f"Creating collection: {self.DEDUP_COLLECTION}")
            # Deduplication collection doesn't need vectors
            # We'll use it like a key-value store with Qdrant's payload
            self.client.create_collection(
                collection_name=self.DEDUP_COLLECTION,
                vectors_config=VectorParams(
                    size=1,  # Dummy vector
                    distance=Distance.COSINE,
                ),
            )

    def add_chunk(self, chunk: DocumentChunk) -> str:
        """
        Add a document chunk to the vector store.

        Args:
            chunk: Document chunk with embedding

        Returns:
            ID of the stored point

        Raises:
            ValueError: If chunk has no embedding
        """
        if chunk.embedding is None:
            raise ValueError("Chunk must have an embedding")

        # Generate unique ID
        point_id = str(uuid.uuid4())

        # Create point with vector and payload
        point = PointStruct(
            id=point_id,
            vector=chunk.embedding,
            payload={
                "text": chunk.text,
                "chunk_id": chunk.metadata.chunk_id,
                "library_id": chunk.metadata.document_metadata.library_id,
                "item_key": chunk.metadata.document_metadata.item_key,
                "attachment_key": chunk.metadata.document_metadata.attachment_key,
                "title": chunk.metadata.document_metadata.title,
                "authors": chunk.metadata.document_metadata.authors,
                "year": chunk.metadata.document_metadata.year,
                "page_number": chunk.metadata.page_number,
                "text_preview": chunk.metadata.text_preview,
                "chunk_index": chunk.metadata.chunk_index,
                "content_hash": chunk.metadata.content_hash,
            },
        )

        self.client.upsert(
            collection_name=self.CHUNKS_COLLECTION,
            points=[point],
        )

        logger.debug(f"Added chunk {chunk.metadata.chunk_id} with ID {point_id}")
        return point_id

    def add_chunks_batch(self, chunks: list[DocumentChunk]) -> list[str]:
        """
        Add multiple chunks in a batch.

        Args:
            chunks: List of document chunks with embeddings

        Returns:
            List of point IDs
        """
        if not chunks:
            return []

        points = []
        point_ids = []

        for chunk in chunks:
            if chunk.embedding is None:
                logger.warning(f"Skipping chunk {chunk.metadata.chunk_id} without embedding")
                continue

            point_id = str(uuid.uuid4())
            point_ids.append(point_id)

            point = PointStruct(
                id=point_id,
                vector=chunk.embedding,
                payload={
                    "text": chunk.text,
                    "chunk_id": chunk.metadata.chunk_id,
                    "library_id": chunk.metadata.document_metadata.library_id,
                    "item_key": chunk.metadata.document_metadata.item_key,
                    "attachment_key": chunk.metadata.document_metadata.attachment_key,
                    "title": chunk.metadata.document_metadata.title,
                    "authors": chunk.metadata.document_metadata.authors,
                    "year": chunk.metadata.document_metadata.year,
                    "page_number": chunk.metadata.page_number,
                    "text_preview": chunk.metadata.text_preview,
                    "chunk_index": chunk.metadata.chunk_index,
                    "content_hash": chunk.metadata.content_hash,
                },
            )
            points.append(point)

        self.client.upsert(
            collection_name=self.CHUNKS_COLLECTION,
            points=points,
        )

        logger.info(f"Added {len(points)} chunks in batch")
        return point_ids

    def search(
        self,
        query_vector: list[float],
        limit: int = 5,
        score_threshold: Optional[float] = None,
        library_ids: Optional[list[str]] = None,
    ) -> list[SearchResult]:
        """
        Search for similar chunks.

        Args:
            query_vector: Query embedding vector
            limit: Maximum number of results
            score_threshold: Minimum similarity score
            library_ids: Optional list of library IDs to filter by

        Returns:
            List of search results with chunks and scores
        """
        # Build filter if library_ids specified
        query_filter = None
        if library_ids:
            query_filter = Filter(
                should=[
                    FieldCondition(
                        key="library_id",
                        match=MatchValue(value=lib_id),
                    )
                    for lib_id in library_ids
                ]
            )

        # Search using query_points (replaces deprecated search method)
        results = self.client.query_points(
            collection_name=self.CHUNKS_COLLECTION,
            query=query_vector,
            limit=limit,
            score_threshold=score_threshold,
            query_filter=query_filter,
            with_payload=True,
        ).points

        # Convert to SearchResult objects
        search_results = []
        for result in results:
            payload = result.payload

            # Reconstruct chunk metadata
            metadata = ChunkMetadata(
                chunk_id=payload["chunk_id"],
                document_metadata={
                    "library_id": payload["library_id"],
                    "item_key": payload["item_key"],
                    "attachment_key": payload.get("attachment_key"),
                    "title": payload.get("title"),
                    "authors": payload.get("authors", []),
                    "year": payload.get("year"),
                    "item_type": None,  # Not stored in vector DB
                },
                page_number=payload.get("page_number"),
                text_preview=payload["text_preview"],
                chunk_index=payload["chunk_index"],
                content_hash=payload["content_hash"],
            )

            chunk = DocumentChunk(
                text=payload["text"],
                metadata=metadata,
                embedding=None,  # Don't return vectors in search results
            )

            search_results.append(
                SearchResult(
                    chunk=chunk,
                    score=result.score,
                )
            )

        logger.info(f"Search returned {len(search_results)} results")
        return search_results

    def check_duplicate(self, content_hash: str) -> Optional[DeduplicationRecord]:
        """
        Check if a document with this content hash already exists.

        Args:
            content_hash: Content hash to check

        Returns:
            Deduplication record if found, None otherwise
        """
        # Search in dedup collection
        results = self.client.scroll(
            collection_name=self.DEDUP_COLLECTION,
            scroll_filter=Filter(
                must=[
                    FieldCondition(
                        key="content_hash",
                        match=MatchValue(value=content_hash),
                    )
                ]
            ),
            limit=1,
        )

        if results[0]:
            payload = results[0][0].payload
            return DeduplicationRecord(
                content_hash=payload["content_hash"],
                library_id=payload["library_id"],
                item_key=payload["item_key"],
                relation_uri=payload.get("relation_uri"),
            )

        return None

    def add_deduplication_record(self, record: DeduplicationRecord):
        """
        Add a deduplication record.

        Args:
            record: Deduplication record to store
        """
        point_id = str(uuid.uuid4())

        point = PointStruct(
            id=point_id,
            vector=[0.0],  # Dummy vector
            payload={
                "content_hash": record.content_hash,
                "library_id": record.library_id,
                "item_key": record.item_key,
                "relation_uri": record.relation_uri,
            },
        )

        self.client.upsert(
            collection_name=self.DEDUP_COLLECTION,
            points=[point],
        )

        logger.debug(f"Added deduplication record for {record.item_key}")

    def delete_library_chunks(self, library_id: str) -> int:
        """
        Delete all chunks for a specific library.

        Args:
            library_id: Library ID

        Returns:
            Number of chunks deleted
        """
        # Get count before deletion
        count_before = self.client.count(
            collection_name=self.CHUNKS_COLLECTION,
            count_filter=Filter(
                must=[
                    FieldCondition(
                        key="library_id",
                        match=MatchValue(value=library_id),
                    )
                ]
            ),
        ).count

        # Delete points
        self.client.delete(
            collection_name=self.CHUNKS_COLLECTION,
            points_selector=Filter(
                must=[
                    FieldCondition(
                        key="library_id",
                        match=MatchValue(value=library_id),
                    )
                ]
            ),
        )

        logger.info(f"Deleted {count_before} chunks for library {library_id}")
        return count_before

    def get_collection_info(self) -> dict:
        """
        Get information about the vector store collections.

        Returns:
            Dictionary with collection statistics
        """
        chunks_info = self.client.get_collection(self.CHUNKS_COLLECTION)
        dedup_info = self.client.get_collection(self.DEDUP_COLLECTION)

        return {
            "chunks_count": chunks_info.points_count,
            "dedup_count": dedup_info.points_count,
            "embedding_dim": self.embedding_dim,
            "distance": self.distance.value if hasattr(self.distance, 'value') else str(self.distance),
        }
