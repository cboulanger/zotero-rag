"""
Collection vector service for smart filing.

Computes per-item vectors (A2: title+abstract, falling back to A1: mean of chunk vectors)
and per-collection centroid vectors (mean of member item vectors).

Provides full-library batch sync and incremental per-item update.
"""

import asyncio
import logging
from typing import Callable, Optional

import numpy as np

from backend.db.vector_store import VectorStore
from backend.services.embeddings import EmbeddingService

logger = logging.getLogger(__name__)


def _mean_vector(vectors: list[list[float]]) -> list[float]:
    """Compute element-wise mean of a list of equal-length vectors using numpy."""
    arr = np.array(vectors, dtype=np.float64)
    return arr.mean(axis=0).tolist()


class CollectionVectorService:
    """
    Service for computing and storing item and collection embedding vectors.

    Item vectors are computed either from title+abstract (A2, primary) or as the
    arithmetic mean of all indexed chunk vectors (A1, fallback).

    Collection vectors are computed as the arithmetic mean of all member item vectors.
    """

    def __init__(
        self,
        vector_store: VectorStore,
        embedding_service: EmbeddingService,
        min_abstract_chars: int = 50,
    ):
        """
        Initialise the service.

        Args:
            vector_store: VectorStore instance for reading/writing vectors.
            embedding_service: EmbeddingService used for A2 text embeddings.
            min_abstract_chars: Minimum character count for an abstract to be
                considered usable in the A2 path.  Abstracts shorter than this
                threshold trigger the A1 (chunk-mean) fallback.
        """
        self.vector_store = vector_store
        self.embedding_service = embedding_service
        self.min_abstract_chars = min_abstract_chars

    # ------------------------------------------------------------------
    # Item vector computation
    # ------------------------------------------------------------------

    async def compute_item_vector(
        self,
        library_id: str,
        item_key: str,
        title: str,
        abstract: str,
        collection_ids: list[str],
    ) -> Optional[str]:
        """
        Compute and persist an item vector.

        Strategy:
        1. A2 (primary): embed ``title + " " + abstract``.  If the abstract is
           too short (< ``min_abstract_chars``), embed only the title.
        2. A1 (fallback): compute the mean of all chunk vectors stored for the
           item in Qdrant.  Applied when the A2 text is empty or when the
           embedding service returns an empty vector.

        Args:
            library_id: Zotero library ID.
            item_key: Zotero item key.
            title: Item title (may be empty but should not be None).
            abstract: Item abstract (may be empty).
            collection_ids: Collection IDs the item belongs to.

        Returns:
            Qdrant point UUID string if the vector was stored, ``None`` if no
            data was available to produce a vector.
        """
        # --- A2: title + abstract ---
        clean_abstract = (abstract or "").strip()
        clean_title = (title or "").strip()

        if clean_abstract and len(clean_abstract) >= self.min_abstract_chars:
            embed_text = f"{clean_title} {clean_abstract}".strip()
        else:
            embed_text = clean_title

        vector: Optional[list[float]] = None
        source: Optional[str] = None

        if embed_text:
            vector = await self.embedding_service.embed_text(embed_text)
            # Treat an all-zero or empty result as unusable
            if vector and any(v != 0.0 for v in vector):
                source = "abstract"
            else:
                vector = None

        # --- A1 fallback: mean-pool chunk vectors ---
        if vector is None:
            chunk_vectors = await asyncio.to_thread(
                self.vector_store.get_chunk_vectors_for_item, library_id, item_key
            )
            if not chunk_vectors:
                logger.debug(
                    "No text or chunks available for item %s/%s — skipping.",
                    library_id,
                    item_key,
                )
                return None
            vector = _mean_vector(chunk_vectors)
            source = "chunks"

        point_id = await asyncio.to_thread(
            self.vector_store.upsert_item_vector,
            library_id,
            item_key,
            vector,
            collection_ids,
            source,
            clean_title or item_key,
        )
        logger.debug("Stored item vector (%s) for %s/%s", source, library_id, item_key)
        return point_id

    # ------------------------------------------------------------------
    # Collection centroid computation
    # ------------------------------------------------------------------

    def compute_collection_centroid(
        self,
        library_id: str,
        collection_id: str,
        collection_name: str,
        item_vectors: list[tuple[str, list[float], list[str]]],
    ) -> Optional[str]:
        """
        Compute and persist a collection centroid vector.

        Args:
            library_id: Zotero library ID.
            collection_id: Zotero collection ID.
            collection_name: Human-readable collection name.
            item_vectors: Full output of
                ``vector_store.get_item_vectors_for_library(library_id)``.
                Each element is ``(item_key, vector, collection_ids)``.

        Returns:
            Qdrant point UUID string, or ``None`` when no items belong to this
            collection (centroid is undefined).
        """
        member_vectors = [
            vec
            for _item_key, vec, col_ids in item_vectors
            if collection_id in col_ids
        ]
        if not member_vectors:
            logger.debug(
                "Collection %s/%s has no member item vectors — skipping centroid.",
                library_id,
                collection_id,
            )
            return None

        centroid = _mean_vector(member_vectors)
        point_id = self.vector_store.upsert_collection_vector(
            library_id=library_id,
            collection_id=collection_id,
            collection_name=collection_name,
            vector=centroid,
            item_count=len(member_vectors),
        )
        logger.debug(
            "Stored centroid for collection %s/%s (%d items)",
            library_id,
            collection_id,
            len(member_vectors),
        )
        return point_id

    # ------------------------------------------------------------------
    # Full-library batch sync
    # ------------------------------------------------------------------

    async def sync_library(
        self,
        library_id: str,
        collection_map: dict[str, list[str]],
        collection_names: dict[str, str],
        item_metadata: Optional[dict[str, dict]] = None,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
    ) -> dict:
        """
        Full sync: compute item vectors for all items, then collection centroids.

        Args:
            library_id: Zotero library ID.
            collection_map: ``{item_key: [collection_id, ...]}``.
            collection_names: ``{collection_id: name}``.
            item_metadata: Optional ``{item_key: {"title": ..., "abstract": ...}}``.
                If provided, title and abstract are taken from here; otherwise
                empty strings are used and the A1 fallback will apply.
            progress_callback: Called after each item with
                ``(completed_count, total_count, current_item_key)``.

        Returns:
            Stats dict with keys:
            ``items_computed``, ``items_skipped``, ``collections_computed``,
            ``collections_skipped``.
        """
        items_computed = 0
        items_skipped = 0

        item_keys = list(collection_map.keys())
        total = len(item_keys)

        for idx, item_key in enumerate(item_keys):
            meta = (item_metadata or {}).get(item_key, {})
            title = meta.get("title", "")
            abstract = meta.get("abstract", "")
            col_ids = collection_map.get(item_key, [])

            result = await self.compute_item_vector(
                library_id=library_id,
                item_key=item_key,
                title=title,
                abstract=abstract,
                collection_ids=col_ids,
            )
            if result is not None:
                items_computed += 1
            else:
                items_skipped += 1

            if progress_callback:
                progress_callback(idx + 1, total, item_key)

        # --- Collection centroids ---
        # Reload all item vectors at once (single Qdrant scroll) for efficiency.
        all_item_vectors = await asyncio.to_thread(
            self.vector_store.get_item_vectors_for_library, library_id
        )

        collections_computed = 0
        collections_skipped = 0

        for collection_id, cname in collection_names.items():
            result = self.compute_collection_centroid(
                library_id=library_id,
                collection_id=collection_id,
                collection_name=cname,
                item_vectors=all_item_vectors,
            )
            if result is not None:
                collections_computed += 1
            else:
                collections_skipped += 1

        stats = {
            "items_computed": items_computed,
            "items_skipped": items_skipped,
            "collections_computed": collections_computed,
            "collections_skipped": collections_skipped,
        }
        logger.info(
            "sync_library %s complete: %s",
            library_id,
            stats,
        )
        return stats

    # ------------------------------------------------------------------
    # Incremental update
    # ------------------------------------------------------------------

    async def update_item(
        self,
        library_id: str,
        item_key: str,
        title: str,
        abstract: str,
        collection_ids: list[str],
    ) -> bool:
        """
        Incremental update: recompute an item vector and refresh affected collection centroids.

        After the item vector is (re)computed, all collections that contain this
        item have their centroids recomputed using the full current set of item
        vectors for the library.

        Args:
            library_id: Zotero library ID.
            item_key: Zotero item key.
            title: Current item title.
            abstract: Current item abstract.
            collection_ids: Collection IDs the item currently belongs to.

        Returns:
            ``True`` if the item vector was successfully computed and stored,
            ``False`` if no data was available (item was skipped).
        """
        result = await self.compute_item_vector(
            library_id=library_id,
            item_key=item_key,
            title=title,
            abstract=abstract,
            collection_ids=collection_ids,
        )
        if result is None:
            return False

        # Refresh centroids for all affected collections.
        if collection_ids:
            all_item_vectors = await asyncio.to_thread(
                self.vector_store.get_item_vectors_for_library, library_id
            )
            for collection_id in collection_ids:
                # Attempt to look up a stored name; fall back to the ID itself.
                existing = self.vector_store.get_collection_vector(library_id, collection_id)
                if existing is not None:
                    _vec, payload = existing
                    cname = payload.get("collection_name", collection_id)
                else:
                    cname = collection_id
                self.compute_collection_centroid(
                    library_id=library_id,
                    collection_id=collection_id,
                    collection_name=cname,
                    item_vectors=all_item_vectors,
                )

        return True
