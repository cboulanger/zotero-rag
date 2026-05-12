"""
Vector database interface using Qdrant.

Handles storage and retrieval of document chunk embeddings with metadata.
"""

import json
import logging
import time
import warnings
from typing import Optional
from pathlib import Path
import uuid

from httpx import TimeoutException, ReadTimeout, WriteTimeout
from qdrant_client.http.exceptions import ResponseHandlingException

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    VectorParams,
    PointStruct,
    Filter,
    FieldCondition,
    MatchValue,
    MatchAny,
    MatchText,
    Range,
    SearchParams,
    TextIndexParams,
    TokenizerType,
)

from backend.models.document import DocumentChunk, ChunkMetadata, SearchResult, DeduplicationRecord, DocumentMetadata
from backend.models.filters import MetadataFilters
from backend.models.library import LibraryIndexMetadata


logger = logging.getLogger(__name__)


def _extract_lastnames(authors: list[str]) -> list[str]:
    """Return lowercase last names from a list of author strings.

    Handles "Last, First" (comma-separated) and "First Last" formats.
    Used as a keyword-indexed payload field for Qdrant-native author filtering.
    """
    lastnames = []
    for author in authors:
        name = author.strip()
        if "," in name:
            lastname = name.split(",", 1)[0].strip()
        else:
            parts = name.split()
            lastname = parts[-1] if parts else name
        if lastname:
            lastnames.append(lastname.lower())
    return lastnames


class VectorStore:
    """
    Vector database interface using Qdrant.

    Manages collections for document chunks and deduplication records.
    """

    CHUNKS_COLLECTION = "document_chunks"
    DEDUP_COLLECTION = "deduplication"
    METADATA_COLLECTION = "library_metadata"

    _CONFIG_FILE = "embedding_config.json"

    def __init__(
        self,
        storage_path: Path,
        embedding_dim: int,
        embedding_model_name: str,
        distance: Distance = Distance.COSINE,
        url: Optional[str] = None,
        timeout: int = 30,
    ):
        """
        Initialize vector store.

        Args:
            storage_path: Path to Qdrant storage directory (local file mode)
            embedding_dim: Dimensionality of embeddings
            embedding_model_name: Identifier of the embedding model (e.g. model name/path)
            distance: Distance metric (COSINE, EUCLID, DOT)
            url: Qdrant server URL. When set, connects to a running Qdrant server
                 instead of using local file storage.
            timeout: Qdrant client request timeout in seconds (server mode only).
        """
        self.storage_path = storage_path
        self.embedding_dim = embedding_dim
        self.embedding_model_name = embedding_model_name
        self.distance = distance
        self.qdrant_timeout = timeout

        if url:
            self.client = QdrantClient(url=url, timeout=timeout)
            logger.info(f"Initialized VectorStore connected to Qdrant server at {url} (timeout={timeout}s)")
        else:
            # Ensure storage directory exists
            storage_path.mkdir(parents=True, exist_ok=True)
            self.client = QdrantClient(path=str(storage_path))
            logger.info(f"Initialized VectorStore at {storage_path}")

        # Create collections if they don't exist
        self._ensure_collections()

    def _load_embedding_config(self) -> Optional[dict]:
        """Read the persisted embedding config from the sidecar file."""
        config_file = self.storage_path / self._CONFIG_FILE
        if config_file.exists():
            try:
                return json.loads(config_file.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning(f"Could not read {self._CONFIG_FILE}: {e}")
        return None

    def _save_embedding_config(self):
        """Persist the current embedding model name and dim to the sidecar file."""
        config_file = self.storage_path / self._CONFIG_FILE
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text(
            json.dumps({"model_name": self.embedding_model_name, "embedding_dim": self.embedding_dim}),
            encoding="utf-8",
        )

    def _ensure_collections(self):
        """Create collections if they don't exist."""
        collections = self.client.get_collections().collections
        collection_names = [c.name for c in collections]
        has_chunks = self.CHUNKS_COLLECTION in collection_names

        # Write sidecar for human reference; model identity is guaranteed by the directory path
        if self._load_embedding_config() is None and has_chunks:
            logger.warning(
                "No embedding config file found for an existing database. "
                "Assuming current model '%s' and recording it.",
                self.embedding_model_name,
            )

        if has_chunks:
            # Secondary sanity-check: stored vector dim must match even if config file agrees
            info = self.client.get_collection(self.CHUNKS_COLLECTION)
            vectors_config = info.config.params.vectors
            existing_dim = (
                vectors_config.size
                if hasattr(vectors_config, "size")
                else next(iter(vectors_config.values())).size
            )
            if existing_dim != self.embedding_dim:
                raise ValueError(
                    f"Vector dimension mismatch: the '{self.CHUNKS_COLLECTION}' collection stores "
                    f"{existing_dim}-dim vectors but the current model produces "
                    f"{self.embedding_dim}-dim vectors. "
                    f"Re-index all libraries or clear the vector database before continuing."
                )

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

        if self.METADATA_COLLECTION not in collection_names:
            logger.info(f"Creating collection: {self.METADATA_COLLECTION}")
            self.client.create_collection(
                collection_name=self.METADATA_COLLECTION,
                vectors_config=VectorParams(
                    size=1,  # Dummy vector
                    distance=Distance.COSINE,
                ),
            )
            # Create index on library_id for fast lookups.
            # In-memory/local Qdrant ignores payload indexes; suppress the
            # expected UserWarning so it doesn't surface in test output.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                self.client.create_payload_index(
                    collection_name=self.METADATA_COLLECTION,
                    field_name="library_id",
                    field_schema="keyword"
                )

        # Ensure payload indexes on DEDUP_COLLECTION (idempotent)
        self._ensure_dedup_indexes()

        # Ensure payload indexes on CHUNKS_COLLECTION (idempotent — also applied to
        # existing collections so existing deployments benefit without a rebuild).
        self._ensure_chunks_indexes()

        # Persist (or refresh) the embedding config so future startups can validate it
        self._save_embedding_config()

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
                "author_lastnames": _extract_lastnames(chunk.metadata.document_metadata.authors),
                "year": chunk.metadata.document_metadata.year,
                "item_type": chunk.metadata.document_metadata.item_type,
                "page_number": chunk.metadata.page_number,
                "text_preview": chunk.metadata.text_preview,
                "chunk_index": chunk.metadata.chunk_index,
                "content_hash": chunk.metadata.content_hash,
                # Version tracking fields (schema v2+)
                "item_version": chunk.metadata.item_version,
                "attachment_version": chunk.metadata.attachment_version,
                "indexed_at": chunk.metadata.indexed_at,
                "zotero_modified": chunk.metadata.zotero_modified,
                "schema_version": chunk.metadata.schema_version,
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
                    "author_lastnames": _extract_lastnames(chunk.metadata.document_metadata.authors),
                    "year": chunk.metadata.document_metadata.year,
                    "item_type": chunk.metadata.document_metadata.item_type,
                    "page_number": chunk.metadata.page_number,
                    "text_preview": chunk.metadata.text_preview,
                    "chunk_index": chunk.metadata.chunk_index,
                    "content_hash": chunk.metadata.content_hash,
                    # Version tracking fields (schema v2+)
                    "item_version": chunk.metadata.item_version,
                    "attachment_version": chunk.metadata.attachment_version,
                    "indexed_at": chunk.metadata.indexed_at,
                    "zotero_modified": chunk.metadata.zotero_modified,
                    "schema_version": chunk.metadata.schema_version,
                },
            )
            points.append(point)

        batch_size = 100
        _UPSERT_MAX_RETRIES = 4
        for i in range(0, len(points), batch_size):
            batch = points[i : i + batch_size]
            for attempt in range(1, _UPSERT_MAX_RETRIES + 1):
                try:
                    self.client.upsert(
                        collection_name=self.CHUNKS_COLLECTION,
                        points=batch,
                    )
                    break
                except (TimeoutException, ReadTimeout, WriteTimeout) as exc:
                    if attempt == _UPSERT_MAX_RETRIES:
                        raise
                    delay = 2 ** (attempt - 1)  # 1s, 2s, 4s
                    logger.warning(
                        f"Qdrant upsert timeout (attempt {attempt}/{_UPSERT_MAX_RETRIES},"
                        f" batch {i // batch_size + 1}) — retrying in {delay}s: {exc}"
                    )
                    time.sleep(delay)
                except ResponseHandlingException as exc:
                    if "timed out" not in str(exc).lower() or attempt == _UPSERT_MAX_RETRIES:
                        raise
                    delay = 2 ** (attempt - 1)
                    logger.warning(
                        f"Qdrant upsert timeout (attempt {attempt}/{_UPSERT_MAX_RETRIES},"
                        f" batch {i // batch_size + 1}) — retrying in {delay}s: {exc}"
                    )
                    time.sleep(delay)

        logger.debug(f"Added {len(points)} chunks in batch")
        return point_ids

    def _build_metadata_must_conditions(self, filters: MetadataFilters) -> list:
        """Return Qdrant FieldCondition list for the given MetadataFilters (AND semantics)."""
        conditions = []
        if filters.year_min is not None or filters.year_max is not None:
            conditions.append(FieldCondition(
                key="year",
                range=Range(
                    gte=filters.year_min,
                    lte=filters.year_max,
                ),
            ))
        if filters.authors:
            # author_lastnames is a keyword-indexed list[str] — MatchAny works reliably in all modes
            conditions.append(FieldCondition(
                key="author_lastnames",
                match=MatchAny(any=[a.lower() for a in filters.authors]),
            ))
        if filters.item_types:
            conditions.append(FieldCondition(
                key="item_type",
                match=MatchAny(any=filters.item_types),
            ))
        for kw in filters.title_keywords:
            conditions.append(FieldCondition(key="title", match=MatchText(text=kw)))
        return conditions

    def search(
        self,
        query_vector: list[float],
        limit: int = 5,
        score_threshold: Optional[float] = None,
        library_ids: Optional[list[str]] = None,
        filters: Optional[MetadataFilters] = None,
    ) -> list[SearchResult]:
        """
        Search for similar chunks.

        Args:
            query_vector: Query embedding vector
            limit: Maximum number of results
            score_threshold: Minimum similarity score
            library_ids: Optional list of library IDs to filter by
            filters: Optional bibliographic metadata filters

        Returns:
            List of search results with chunks and scores
        """
        # library_ids → OR filter in should; metadata filters → AND filter in must
        should_conditions = []
        if library_ids:
            should_conditions = [
                FieldCondition(key="library_id", match=MatchValue(value=lib_id))
                for lib_id in library_ids
            ]

        must_conditions = []
        if filters and not filters.is_empty():
            must_conditions = self._build_metadata_must_conditions(filters)

        if should_conditions or must_conditions:
            query_filter = Filter(
                should=should_conditions or None,
                must=must_conditions or None,
            )
        else:
            query_filter = None

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
                    "item_type": payload.get("item_type"),
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

        logger.debug(f"Search returned {len(search_results)} results")
        return search_results

    def get_items_by_metadata(
        self,
        library_ids: Optional[list[str]],
        filters: MetadataFilters,
        limit: int = 50,
    ) -> list[dict]:
        """
        Return one representative payload dict per unique item matching the given filters.

        Uses Qdrant scroll (no query vector) — pure metadata lookup. Results are
        deduplicated by item_key so callers receive one entry per Zotero item, not per chunk.

        Args:
            library_ids: Libraries to restrict the search to (OR logic).
            filters: Bibliographic metadata filters.
            limit: Maximum number of distinct items to return.

        Returns:
            List of payload dicts, one per unique item_key.
        """
        must_conditions: list = self._build_metadata_must_conditions(filters)
        should_conditions: list = []
        if library_ids:
            should_conditions = [
                FieldCondition(key="library_id", match=MatchValue(value=lib_id))
                for lib_id in library_ids
            ]

        scroll_filter = Filter(
            should=should_conditions or None,
            must=must_conditions or None,
        ) if (should_conditions or must_conditions) else None

        seen_items: set[str] = set()
        results: list[dict] = []
        offset = None

        while len(results) < limit:
            batch, next_offset = self.client.scroll(
                collection_name=self.CHUNKS_COLLECTION,
                scroll_filter=scroll_filter,
                limit=500,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for point in batch:
                ik = point.payload.get("item_key")
                if ik and ik not in seen_items:
                    seen_items.add(ik)
                    results.append(point.payload)
                    if len(results) >= limit:
                        break
            if next_offset is None:
                break
            offset = next_offset

        logger.debug(f"get_items_by_metadata returned {len(results)} distinct items")
        return results

    def get_item_states_bulk(
        self,
        library_id: str,
        item_keys: list[str],
    ) -> dict[str, dict]:
        """
        Return item_version and schema_version for multiple items in one Qdrant scroll.

        Returns a dict mapping item_key → {"item_version": int, "schema_version": int}.
        Keys absent from the result have no indexed chunks (or only legacy chunks).
        """
        if not item_keys:
            return {}

        states: dict[str, dict] = {}
        offset = None

        while True:
            results, next_offset = self.client.scroll(
                collection_name=self.CHUNKS_COLLECTION,
                scroll_filter=Filter(must=[
                    FieldCondition(key="library_id", match=MatchValue(value=library_id)),
                    FieldCondition(key="item_key", match=MatchAny(any=item_keys)),
                ]),
                limit=1000,
                offset=offset,
                with_payload=True,
                with_vectors=False,
                timeout=self.qdrant_timeout,
            )
            for point in results:
                ik = point.payload.get("item_key")
                if ik and ik not in states and "item_version" in point.payload:
                    states[ik] = {
                        "item_version": point.payload["item_version"],
                        "schema_version": point.payload.get("schema_version", 2),
                    }
            if set(item_keys).issubset(states.keys()) or next_offset is None:
                break
            offset = next_offset

        return states

    def update_item_metadata(
        self,
        library_id: str,
        item_key: str,
        fields: dict,
    ) -> int:
        """
        Update payload fields for all chunks of an item without re-embedding.

        Used to backfill schema-outdated chunks (e.g. add item_type to existing chunks).

        Args:
            library_id: Library the item belongs to.
            item_key: Zotero item key.
            fields: Payload fields to set (merged into existing payload).

        Returns:
            Number of points updated.
        """
        chunks = self.client.scroll(
            collection_name=self.CHUNKS_COLLECTION,
            scroll_filter=Filter(must=[
                FieldCondition(key="library_id", match=MatchValue(value=library_id)),
                FieldCondition(key="item_key", match=MatchValue(value=item_key)),
            ]),
            limit=1000,
            with_payload=False,
            with_vectors=False,
        )
        point_ids = [p.id for p in chunks[0]]
        if not point_ids:
            return 0
        self.client.set_payload(
            collection_name=self.CHUNKS_COLLECTION,
            payload=fields,
            points=point_ids,
        )
        logger.debug(f"Updated {len(point_ids)} chunks for {library_id}/{item_key}")
        return len(point_ids)

    def check_duplicate(self, content_hash: str, library_id: Optional[str] = None) -> Optional[DeduplicationRecord]:
        """
        Check if a document with this content hash already exists.

        Args:
            content_hash: Content hash to check
            library_id: When provided, only match records for this library.

        Returns:
            Deduplication record if found, None otherwise
        """
        must_conditions: list = [
            FieldCondition(
                key="content_hash",
                match=MatchValue(value=content_hash),
            )
        ]
        if library_id is not None:
            must_conditions.append(
                FieldCondition(
                    key="library_id",
                    match=MatchValue(value=library_id),
                )
            )
        results = self.client.scroll(
            collection_name=self.DEDUP_COLLECTION,
            scroll_filter=Filter(must=must_conditions),
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

    def _ensure_dedup_indexes(self):
        """Create payload index on content_hash in DEDUP_COLLECTION (idempotent)."""
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                self.client.create_payload_index(
                    collection_name=self.DEDUP_COLLECTION,
                    field_name="content_hash",
                    field_schema="keyword",
                )
        except Exception:
            pass  # already exists or local in-memory instance

    def _ensure_chunks_indexes(self):
        """
        Create payload indexes on CHUNKS_COLLECTION (idempotent).

        Keyword indexes make version-bulk and library-filter queries efficient.
        Integer index on year enables range filters.
        Text indexes on authors and title enable substring matching.
        Called on every startup so existing deployments pick up indexes without a rebuild.
        """
        keyword_fields = ("library_id", "item_key", "item_type", "author_lastnames")
        for field in keyword_fields:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    self.client.create_payload_index(
                        collection_name=self.CHUNKS_COLLECTION,
                        field_name=field,
                        field_schema="keyword",
                    )
                logger.debug(f"Ensured keyword index on {self.CHUNKS_COLLECTION}.{field}")
            except Exception as exc:
                logger.debug(f"Payload index on {self.CHUNKS_COLLECTION}.{field}: {exc}")

        # Integer index for year range queries
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                self.client.create_payload_index(
                    collection_name=self.CHUNKS_COLLECTION,
                    field_name="year",
                    field_schema="integer",
                )
            logger.debug(f"Ensured integer index on {self.CHUNKS_COLLECTION}.year")
        except Exception as exc:
            logger.debug(f"Payload index on {self.CHUNKS_COLLECTION}.year: {exc}")

        # Text index for title (single string field — MatchText works reliably)
        text_index_params = TextIndexParams(
            type="text",
            tokenizer=TokenizerType.WORD,
            min_token_len=2,
            lowercase=True,
        )
        for field in ("title",):
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    self.client.create_payload_index(
                        collection_name=self.CHUNKS_COLLECTION,
                        field_name=field,
                        field_schema=text_index_params,
                    )
                logger.debug(f"Ensured text index on {self.CHUNKS_COLLECTION}.{field}")
            except Exception as exc:
                logger.debug(f"Payload index on {self.CHUNKS_COLLECTION}.{field}: {exc}")

    def find_cross_library_duplicate(
        self, content_hash: str, current_library_id: str
    ) -> Optional[DeduplicationRecord]:
        """
        Return a dedup record for content_hash that belongs to a *different* library.

        Used to detect cross-library duplicates so their chunks can be copied
        instead of re-extracted and re-embedded.
        """
        record = self.check_duplicate(content_hash, library_id=None)
        if record and record.library_id != current_library_id:
            return record
        return None

    def copy_chunks_cross_library(
        self,
        source_library_id: str,
        source_item_key: str,
        target_library_id: str,
        target_item_key: str,
        target_attachment_key: str,
        target_doc_metadata: DocumentMetadata,
        target_item_version: int,
        target_attachment_version: int,
        target_item_modified: str,
    ) -> int:
        """
        Clone all chunks from a source item into a target item in a different library.

        Preserves text and vector; replaces all identity fields (library_id, item_key,
        chunk_id, title, authors, year, timestamps). Returns the number of chunks written.
        """
        from datetime import datetime, UTC

        source_filter = Filter(must=[
            FieldCondition(key="library_id", match=MatchValue(value=source_library_id)),
            FieldCondition(key="item_key",   match=MatchValue(value=source_item_key)),
        ])
        new_points = []
        offset = None
        while True:
            batch, offset = self.client.scroll(
                collection_name=self.CHUNKS_COLLECTION,
                scroll_filter=source_filter,
                limit=500,
                offset=offset,
                with_payload=True,
                with_vectors=True,
            )
            for point in batch:
                p = point.payload
                i = p["chunk_index"]
                new_payload = {
                    # preserved verbatim
                    "text":          p["text"],
                    "text_preview":  p["text_preview"],
                    "chunk_index":   i,
                    "page_number":   p.get("page_number"),
                    "content_hash":  p["content_hash"],
                    "schema_version": p.get("schema_version", 2),
                    # replaced with target identity
                    "chunk_id":           f"{target_library_id}:{target_item_key}:{target_attachment_key}:{i}",
                    "library_id":         target_library_id,
                    "item_key":           target_item_key,
                    "attachment_key":     target_attachment_key,
                    "title":              target_doc_metadata.title,
                    "authors":            target_doc_metadata.authors,
                    "year":               target_doc_metadata.year,
                    "item_type":          target_doc_metadata.item_type,
                    "item_version":       target_item_version,
                    "attachment_version": target_attachment_version,
                    "indexed_at":         datetime.now(UTC).isoformat(),
                    "zotero_modified":    target_item_modified,
                }
                new_points.append(PointStruct(
                    id=str(uuid.uuid4()),
                    vector=point.vector,
                    payload=new_payload,
                ))
            if offset is None:
                break
        if new_points:
            self.client.upsert(collection_name=self.CHUNKS_COLLECTION, points=new_points)
        logger.info(
            f"Copied {len(new_points)} chunks "
            f"{source_library_id}/{source_item_key} -> "
            f"{target_library_id}/{target_item_key}"
        )
        return len(new_points)

    def delete_library_deduplication_records(self, library_id: str) -> int:
        """
        Delete all deduplication records for a specific library.

        Args:
            library_id: Library ID

        Returns:
            Number of deduplication records deleted
        """
        # Get count before deletion
        count_before = self.client.count(
            collection_name=self.DEDUP_COLLECTION,
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
            collection_name=self.DEDUP_COLLECTION,
            points_selector=Filter(
                must=[
                    FieldCondition(
                        key="library_id",
                        match=MatchValue(value=library_id),
                    )
                ]
            ),
        )

        logger.info(f"Deleted {count_before} deduplication records for library {library_id}")
        return count_before

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
        metadata_info = self.client.get_collection(self.METADATA_COLLECTION)

        return {
            "chunks_count": chunks_info.points_count,
            "dedup_count": dedup_info.points_count,
            "metadata_count": metadata_info.points_count,
            "embedding_dim": self.embedding_dim,
            "embedding_model_name": self.embedding_model_name,
            "distance": self.distance.value if hasattr(self.distance, 'value') else str(self.distance),
        }

    # Library Metadata Methods

    def _library_id_to_uuid(self, library_id: str) -> str:
        """
        Convert library ID to a consistent UUID.

        Uses UUID5 with a namespace to ensure the same library_id
        always produces the same UUID.

        Args:
            library_id: Library ID (e.g., "6297749")

        Returns:
            UUID string
        """
        # Use a custom namespace for library metadata
        namespace = uuid.UUID('12345678-1234-5678-1234-567812345678')
        return str(uuid.uuid5(namespace, library_id))

    def get_library_metadata(self, library_id: str) -> Optional[LibraryIndexMetadata]:
        """
        Get indexing metadata for a library.

        Args:
            library_id: Library ID

        Returns:
            Library metadata if found, None otherwise
        """
        try:
            point_id = self._library_id_to_uuid(library_id)
            points = self.client.retrieve(
                collection_name=self.METADATA_COLLECTION,
                ids=[point_id]
            )
            if points:
                return LibraryIndexMetadata(**points[0].payload)
            return None
        except Exception as e:
            logger.error(f"Error retrieving library metadata: {e}")
            return None

    def update_library_metadata(self, metadata: LibraryIndexMetadata):
        """
        Update or create library metadata.

        Args:
            metadata: Library metadata to store
        """
        point_id = self._library_id_to_uuid(metadata.library_id)
        point = PointStruct(
            id=point_id,
            vector=[0.0],  # Dummy vector
            payload=metadata.model_dump()
        )
        self.client.upsert(
            collection_name=self.METADATA_COLLECTION,
            points=[point]
        )
        logger.debug(f"Updated metadata for library {metadata.library_id}")

    def mark_library_for_reset(self, library_id: str):
        """
        Mark library for full reindex (hard reset).

        Args:
            library_id: Library ID
        """
        metadata = self.get_library_metadata(library_id)
        if metadata:
            metadata.force_reindex = True
            self.update_library_metadata(metadata)
            logger.info(f"Library {library_id} marked for hard reset")
        else:
            # Create new metadata with reset flag
            metadata = LibraryIndexMetadata(
                library_id=library_id,
                library_type="user",  # Will be updated during next index
                library_name="Unknown",
                force_reindex=True
            )
            self.update_library_metadata(metadata)

    def delete_library_metadata(self, library_id: str) -> bool:
        """
        Delete the metadata record for a library (marks it as never indexed).

        Args:
            library_id: Library ID

        Returns:
            True if the record existed and was deleted, False otherwise
        """
        try:
            point_id = self._library_id_to_uuid(library_id)
            points = self.client.retrieve(collection_name=self.METADATA_COLLECTION, ids=[point_id])
            if not points:
                return False
            self.client.delete(
                collection_name=self.METADATA_COLLECTION,
                points_selector=[point_id],
            )
            logger.info(f"Deleted metadata record for library {library_id}")
            return True
        except Exception as e:
            logger.error(f"Error deleting library metadata: {e}")
            return False

    def get_all_library_metadata(self) -> list[LibraryIndexMetadata]:
        """
        Get metadata for all indexed libraries.

        Returns:
            List of library metadata objects
        """
        try:
            results, _ = self.client.scroll(
                collection_name=self.METADATA_COLLECTION,
                limit=100  # Reasonable limit for number of libraries
            )
            return [LibraryIndexMetadata(**p.payload) for p in results]
        except Exception as e:
            logger.error(f"Error retrieving all library metadata: {e}")
            return []

    # Version-Aware Chunk Methods

    def get_item_chunks(self, library_id: str, item_key: str) -> list[dict]:
        """
        Get all chunks for a specific item.

        Args:
            library_id: Library ID
            item_key: Item key

        Returns:
            List of chunk dictionaries with id and payload
        """
        results = self.client.scroll(
            collection_name=self.CHUNKS_COLLECTION,
            scroll_filter=Filter(must=[
                FieldCondition(key="library_id", match=MatchValue(value=library_id)),
                FieldCondition(key="item_key", match=MatchValue(value=item_key))
            ]),
            limit=1000  # Max chunks per item
        )
        return [{"id": p.id, "payload": p.payload} for p in results[0]]

    def get_item_version(self, library_id: str, item_key: str) -> Optional[int]:
        """
        Get the indexed version of an item (from any of its chunks).

        Args:
            library_id: Library ID
            item_key: Item key

        Returns:
            Item version if found, None if not indexed or legacy chunk without version
        """
        chunks = self.get_item_chunks(library_id, item_key)
        if chunks and "item_version" in chunks[0]["payload"]:
            return chunks[0]["payload"]["item_version"]
        return None  # Not indexed or legacy chunk without version

    def get_item_versions_bulk(self, library_id: str, item_keys: list[str]) -> dict[str, int]:
        """
        Get the indexed version for multiple items in a single Qdrant query.

        Returns a dict mapping item_key → item_version for every key that has
        at least one indexed chunk with a version field.  Keys with no chunks
        (or only legacy chunks lacking the version field) are absent from the
        result.
        """
        if not item_keys:
            return {}

        versions: dict[str, int] = {}
        offset = None

        while True:
            results, next_offset = self.client.scroll(
                collection_name=self.CHUNKS_COLLECTION,
                scroll_filter=Filter(must=[
                    FieldCondition(key="library_id", match=MatchValue(value=library_id)),
                    FieldCondition(key="item_key", match=MatchAny(any=item_keys)),
                ]),
                limit=1000,
                offset=offset,
                with_payload=True,
                with_vectors=False,
                timeout=self.qdrant_timeout,
            )

            for point in results:
                ik = point.payload.get("item_key")
                if ik and ik not in versions and "item_version" in point.payload:
                    versions[ik] = point.payload["item_version"]

            if set(item_keys).issubset(versions.keys()) or next_offset is None:
                break
            offset = next_offset

        return versions

    def delete_item_chunks(self, library_id: str, item_key: str) -> int:
        """
        Delete all chunks for a specific item.

        Args:
            library_id: Library ID
            item_key: Item key

        Returns:
            Number of chunks deleted
        """
        chunks = self.get_item_chunks(library_id, item_key)
        if not chunks:
            return 0

        chunk_ids = [c["id"] for c in chunks]
        self.client.delete(
            collection_name=self.CHUNKS_COLLECTION,
            points_selector=chunk_ids
        )
        logger.info(f"Deleted {len(chunk_ids)} chunks for item {item_key}")
        return len(chunk_ids)

    def count_library_chunks(self, library_id: str) -> int:
        """
        Count total chunks for a library.

        Args:
            library_id: Library ID

        Returns:
            Number of chunks
        """
        try:
            result = self.client.count(
                collection_name=self.CHUNKS_COLLECTION,
                count_filter=Filter(must=[
                    FieldCondition(key="library_id", match=MatchValue(value=library_id))
                ])
            )
            return result.count
        except (IndexError, Exception) as e:
            # qdrant_client local storage raises IndexError on empty collections with filters
            logger.warning(f"Error counting chunks for library {library_id}, returning 0: {e}")
            return 0

    def count_indexed_items(self, library_id: str) -> int:
        """Count distinct item_keys with at least one indexed chunk for a library."""
        item_keys: set[str] = set()
        offset = None
        while True:
            results, next_offset = self.client.scroll(
                collection_name=self.CHUNKS_COLLECTION,
                scroll_filter=Filter(must=[
                    FieldCondition(key="library_id", match=MatchValue(value=library_id))
                ]),
                limit=1000,
                offset=offset,
                with_payload=["item_key"],
                with_vectors=False,
                timeout=self.qdrant_timeout,
            )
            for point in results:
                ik = point.payload.get("item_key")
                if ik:
                    item_keys.add(ik)
            if next_offset is None:
                break
            offset = next_offset
        return len(item_keys)

    def get_library_size_bytes(self, library_id: str) -> int:
        """
        Compute the total byte size of all text stored across chunks for a library.

        Scrolls the chunk collection with only the 'text' payload field to minimise
        data transfer, then sums UTF-8 encoded lengths.

        Args:
            library_id: Library ID

        Returns:
            Total bytes of stored chunk text
        """
        total = 0
        offset = None
        query_filter = Filter(must=[FieldCondition(key="library_id", match=MatchValue(value=library_id))])
        try:
            while True:
                batch, offset = self.client.scroll(
                    collection_name=self.CHUNKS_COLLECTION,
                    scroll_filter=query_filter,
                    limit=500,
                    offset=offset,
                    with_payload=["text"],
                    with_vectors=False,
                )
                for point in batch:
                    if point.payload and "text" in point.payload:
                        total += len(point.payload["text"].encode("utf-8"))
                if offset is None:
                    break
        except Exception as e:
            logger.warning(f"Error computing size for library {library_id}: {e}")
        return total

    def close(self):
        """
        Close the vector store and release resources.

        This method closes the Qdrant client connection and releases
        any locked database files. Call this before deleting the storage
        directory to avoid permission errors on Windows.
        """
        if hasattr(self, 'client') and self.client is not None:
            try:
                self.client.close()
                logger.debug("Closed VectorStore client")
            except Exception as e:
                logger.warning(f"Error closing VectorStore client: {e}")
            finally:
                self.client = None

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - ensures cleanup."""
        self.close()
        return False
