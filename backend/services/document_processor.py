"""
Document processing pipeline for indexing Zotero libraries.

This module handles document extraction, chunking, embedding generation,
and vector database indexing with support for incremental indexing.
"""

import asyncio
import ctypes
import gc
import hashlib
import json
import logging
import os
import re
import sys
import tempfile
import time
from datetime import datetime, UTC
from multiprocessing import Process, Queue as MPQueue
from typing import Callable, Optional, Literal

from backend.zotero.local_api import ZoteroLocalAPI
from backend.services.embeddings import (
    EmbeddingService,
    EmbeddingAuthenticationError,
    EmbeddingRateLimitExhaustedError,
)
from backend.services.extraction import DocumentExtractor, create_document_extractor
from backend.services.extraction.base import ExtractionChunk
from backend.services.extraction.kreuzberg import KreuzbergTimeoutError, KreuzbergParsingError
from backend.services.chunking import TextChunker
from backend.config.settings import get_settings
from backend.db.vector_store import VectorStore
from backend.models.document import (
    DocumentMetadata,
    ChunkMetadata,
    DocumentChunk,
    DeduplicationRecord,
    AttachmentProcessingResult,
)
from backend.models.library import LibraryIndexMetadata

logger = logging.getLogger(__name__)

# Fatal embedding errors abort the whole indexing run — they are never swallowed
# per-item. An expired/invalid API key or an exhausted quota affects every item
# equally, so continuing the loop only produces a stream of identical failures and
# a run that "completes" with zero chunks while reporting success.
_FATAL_EMBEDDING_ERRORS = (
    EmbeddingAuthenticationError,
    EmbeddingRateLimitExhaustedError,
)

# MIME types that will be downloaded and indexed
INDEXABLE_MIME_TYPES = {
    "application/pdf",
    "text/html",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/epub+zip",
}

# Trigger gc + malloc_trim when process RSS exceeds this (MB). Keeps long
# full-sync runs from hitting the OOM killer on memory-constrained hosts.
_GC_RSS_THRESHOLD_MB = 3000

_libc = ctypes.CDLL("libc.so.6") if sys.platform == "linux" else None

SUBPROCESS_BATCH_SIZE = int(os.environ.get("INDEX_BATCH_SIZE", "300"))


async def _item_has_indexed_content(vector_store, library_id: str, item_key: str) -> bool:
    """True if the item has at least one chunk already stored in the vector store.

    Used to disambiguate a zero-chunk `_index_item` result: it can mean a real
    failure (dead attachment download link, extraction error) or a legitimate
    no-op where `_handle_same_library_duplicate` skipped re-writing content
    that's already correctly indexed under this exact item_key. Both look
    identical from the returned chunk count alone.
    """
    return await asyncio.to_thread(vector_store.get_item_version, library_id, item_key) is not None


def _subprocess_index_batch(
    items: list[dict],
    library_id: str,
    library_type: str,
    indexed_versions: dict[str, int],
    zotero_api_key: Optional[str],
    embedding_api_key: Optional[str],
) -> dict:
    """Process a batch of items in an isolated subprocess.

    Re-initialises all clients so the subprocess is completely independent of the
    parent's heap. Called via multiprocessing.Process on Linux (fork context) —
    inherits all env vars from the parent process, but the Zotero key and the
    embedding key must be passed explicitly: under per-user auto-indexing each
    library is fetched and embedded with its owner's own keys, and there is no
    longer a single global env var to fall back on for either.

    Returns a stats dict: chunks_added, items_added, items_updated, items_skipped.
    Raises EmbeddingAuthenticationError / EmbeddingRateLimitExhaustedError on fatal
    embedding failures so the main process can abort the full run.
    """
    import asyncio as _asyncio

    from backend.dependencies import make_vector_store
    from backend.services.embeddings import create_embedding_service
    from backend.zotero.web_api import ZoteroWebAPI

    async def _run() -> dict:
        if not zotero_api_key:
            raise RuntimeError("No Zotero API key available — cannot initialise ZoteroWebAPI in subprocess")

        settings = get_settings()
        preset = settings.get_hardware_preset()
        embedding_service = create_embedding_service(
            preset.embedding,
            cache_dir=str(settings.model_weights_path),
            api_key=embedding_api_key,
            hf_token=settings.get_api_key("HF_TOKEN"),
        )
        vector_store = make_vector_store()
        web_api = ZoteroWebAPI(api_key=zotero_api_key)

        chunks_added = items_added = items_updated = items_skipped = items_failed = 0
        async with web_api:
            processor = DocumentProcessor(
                zotero_client=web_api,
                embedding_service=embedding_service,
                vector_store=vector_store,
            )
            for item in items:
                item_key = item["data"]["key"]
                item_version = item.get("version", 0)
                existing = indexed_versions.get(item_key)
                try:
                    if existing is None:
                        n = await processor._index_item(item, library_id, library_type)
                        if n == 0 and not await _item_has_indexed_content(vector_store, library_id, item_key):
                            # Candidate was selected as indexable but produced zero chunks
                            # (e.g. a dead attachment download link) and has no existing
                            # content either — never actually indexed, so it's a failure.
                            items_failed += 1
                        else:
                            chunks_added += n
                            items_added += 1
                    elif existing < item_version:
                        if await processor._try_metadata_only_update(item, library_id, library_type):
                            items_updated += 1
                            continue
                        vector_store.delete_item_chunks(library_id, item_key)
                        n = await processor._index_item(item, library_id, library_type)
                        if n == 0 and not await _item_has_indexed_content(vector_store, library_id, item_key):
                            items_failed += 1
                        else:
                            chunks_added += n
                            items_updated += 1
                    else:
                        items_skipped += 1
                except _FATAL_EMBEDDING_ERRORS:
                    raise
                except Exception as e:
                    logger.error("Error processing item %s in subprocess batch: %s", item_key, e, exc_info=True)
                    items_failed += 1

        return {
            "chunks_added": chunks_added,
            "items_added": items_added,
            "items_updated": items_updated,
            "items_skipped": items_skipped,
            "items_failed": items_failed,
        }

    return _asyncio.run(_run())


def _run_subprocess_batch(
    items: list[dict],
    library_id: str,
    library_type: str,
    indexed_versions: dict[str, int],
    result_queue,  # multiprocessing.Queue
    zotero_api_key: Optional[str],
    embedding_api_key: Optional[str],
) -> None:
    """Target for multiprocessing.Process. Runs _subprocess_index_batch and puts
    result on result_queue. On fatal embedding error puts {'fatal': True, ...}."""
    try:
        result = _subprocess_index_batch(
            items, library_id, library_type, indexed_versions, zotero_api_key, embedding_api_key
        )
        result_queue.put({"fatal": False, **result})
    except _FATAL_EMBEDDING_ERRORS as e:
        result_queue.put({"fatal": True, "error": repr(e), "error_type": type(e).__name__})
    except Exception as e:
        logger.error("Subprocess batch worker raised unexpected error: %s", e, exc_info=True)
        result_queue.put({"fatal": False, "chunks_added": 0, "items_added": 0,
                          "items_updated": 0, "items_skipped": 0})


def _rss_mb() -> int:
    """Return current process RSS in MB (Linux only, 0 elsewhere)."""
    if sys.platform != "linux":
        return 0
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    return 0


def _trim_memory_if_needed() -> None:
    """Run gc + malloc_trim when RSS exceeds the threshold."""
    if _rss_mb() >= _GC_RSS_THRESHOLD_MB:
        gc.collect()
        if _libc is not None:
            _libc.malloc_trim(0)


class DocumentProcessor:
    """
    Document processing pipeline for indexing Zotero libraries.

    Coordinates document extraction, text chunking, embedding generation,
    and vector database indexing.
    """

    def __init__(
        self,
        zotero_client: ZoteroLocalAPI,
        embedding_service: EmbeddingService,
        vector_store: VectorStore,
        document_extractor: Optional[DocumentExtractor] = None,
        max_chunk_size: int = 512,
        chunk_overlap: int = 50,
    ):
        """
        Initialize document processor.

        Args:
            zotero_client: Zotero API client.
            embedding_service: Service for generating embeddings.
            vector_store: Vector database for storing embeddings.
            document_extractor: Extraction + chunking backend.  When None a
                KreuzbergExtractor is created using max_chunk_size / chunk_overlap.
            max_chunk_size: Maximum characters per chunk (used when
                document_extractor is None).
            chunk_overlap: Overlap between chunks (used when document_extractor
                is None).
        """
        self.zotero_client = zotero_client
        self.embedding_service = embedding_service
        self.vector_store = vector_store

        if document_extractor is None:
            settings = get_settings()
            document_extractor = create_document_extractor(
                backend=settings.extractor_backend,
                max_chunk_size=max_chunk_size,
                chunk_overlap=chunk_overlap,
                ocr_enabled=settings.ocr_enabled,
                kreuzberg_url=settings.kreuzberg_url,
            )
        self.document_extractor = document_extractor

        logger.debug("Initialized DocumentProcessor")

    async def index_library(
        self,
        library_id: str,
        library_type: str = "user",
        library_name: str = "Unknown",
        mode: Literal["auto", "incremental", "full"] = "auto",
        progress_callback: Optional[Callable[[int, int, int], None]] = None,
        cancellation_check: Optional[Callable[[], bool]] = None,
        max_items: Optional[int] = None
    ) -> dict:
        """
        Index a library with intelligent mode selection.

        Args:
            library_id: Zotero library ID to index.
            library_type: Library type ("user" or "group").
            library_name: Human-readable library name.
            mode: Indexing mode:
                - "auto": Automatically choose best mode (recommended)
                - "incremental": Only index new/modified items
                - "full": Reindex entire library
            progress_callback: Optional callback for progress updates (current, total, chunks_added).
            cancellation_check: Optional callback that returns True if cancellation requested.
            max_items: Optional maximum number of items to process (for testing).

        Returns:
            Indexing statistics with counts, timing, and mode used.

        Raises:
            RuntimeError: If cancellation is requested during indexing.
        """
        logger.info(f"Starting indexing for library {library_id} (mode={mode})")
        start_time = datetime.now(UTC)

        # Get or create library metadata
        metadata = self.vector_store.get_library_metadata(library_id)
        if metadata is None:
            logger.info(f"First-time indexing for library {library_id}")
            metadata = LibraryIndexMetadata(
                library_id=library_id,
                library_type=library_type,
                library_name=library_name
            )
            effective_mode = "full"
        else:
            # Check for hard reset flag
            if metadata.force_reindex:
                logger.info(f"Hard reset requested for library {library_id}")
                effective_mode = "full"
                metadata.force_reindex = False  # Clear flag
            elif mode == "full":
                effective_mode = "full"
            elif mode == "incremental":
                effective_mode = "incremental"
            else:  # mode == "auto"
                # Auto-select based on library state
                effective_mode = "incremental" if metadata.last_indexed_version > 0 else "full"

        logger.debug(f"Selected indexing mode: {effective_mode}")

        # Execute indexing
        if effective_mode == "full":
            stats = await self._index_library_full(
                library_id, library_type, metadata, progress_callback, cancellation_check, max_items
            )
        else:
            stats = await self._index_library_incremental(
                library_id, library_type, metadata, progress_callback, cancellation_check, max_items
            )

        # Update library metadata
        metadata.indexing_mode = effective_mode
        metadata.last_indexed_at = datetime.now(UTC).isoformat()
        metadata.total_chunks = self.vector_store.count_library_chunks(library_id)
        self.vector_store.update_library_metadata(metadata)

        elapsed = (datetime.now(UTC) - start_time).total_seconds()
        stats["elapsed_seconds"] = elapsed
        stats["mode"] = effective_mode

        logger.info(
            f"Indexing complete: library={library_id} mode={stats['mode']} "
            f"items={stats['items_processed']} added={stats['items_added']} "
            f"updated={stats.get('items_updated', 0)} chunks={stats['chunks_added']} "
            f"elapsed={stats['elapsed_seconds']:.1f}s"
        )
        return stats

    async def _index_library_incremental(
        self,
        library_id: str,
        library_type: str,
        metadata: LibraryIndexMetadata,
        progress_callback: Optional[Callable[[int, int, int], None]] = None,
        cancellation_check: Optional[Callable[[], bool]] = None,
        max_items: Optional[int] = None
    ) -> dict:
        """Incremental indexing: only process new/modified items."""
        logger.debug(f"Incremental index from version {metadata.last_indexed_version}")

        # Fetch items modified since last index
        since_version = metadata.last_indexed_version
        items = await self.zotero_client.get_library_items_since(
            library_id=library_id,
            library_type=library_type,
            since_version=since_version
        )

        logger.debug(f"Found {len(items)} items modified since version {since_version}")

        # Advance the version checkpoint over ALL fetched items, including non-indexable ones.
        # Without this, items that are fetched but filtered out (e.g. no attachments, no
        # abstract) would cause the same items to be re-fetched on every subsequent run,
        # and any new library items added after their version would never be seen.
        max_version_seen = max(
            (item.get("version", 0) for item in items),
            default=metadata.last_indexed_version,
        )

        # Split into items with indexable content vs. catalog-only (no attachment,
        # no substantial abstract)
        items_with_attachments, catalog_only_items = (
            await self._split_indexable_and_catalog_only(items, library_id, library_type)
            if items else ([], [])
        )

        # Limit items if max_items is specified
        if max_items is not None and max_items > 0:
            items_with_attachments = items_with_attachments[:max_items]
            logger.debug(f"Limited to {len(items_with_attachments)} items (max_items={max_items})")
            catalog_only_items = []

        # Items whose only indexed record is a catalog-only stub — see the matching
        # comment in _index_library_full for why version comparison alone can miss
        # an item that just gained real content.
        stub_keys = self.vector_store.get_stub_item_keys(library_id)

        items_added = 0
        items_updated = 0
        items_failed = 0
        items_cataloged = 0
        chunks_added = 0
        chunks_deleted = 0
        total_items = len(items_with_attachments)

        # Report initial progress
        if progress_callback:
            progress_callback(0, total_items, 0)

        for idx, item in enumerate(items_with_attachments):
            # Check for cancellation
            if cancellation_check and cancellation_check():
                logger.info(f"Cancellation requested during incremental indexing of library {library_id}")
                raise RuntimeError("Indexing cancelled by user")

            try:
                item_key = item["data"]["key"]
                item_version = item["version"]
                max_version_seen = max(max_version_seen, item_version)

                # Check if item already indexed
                existing_version = self.vector_store.get_item_version(library_id, item_key)
                if item_key in stub_keys:
                    # Stub -> real transition: clear the stale stub and treat as new,
                    # regardless of version (the parent item's version may not have
                    # changed even though it just gained real content).
                    self.vector_store.delete_item_chunks(library_id, item_key)
                    existing_version = None

                if existing_version is None:
                    # New item
                    logger.debug(f"Indexing new item {item_key} (version {item_version})")
                    chunk_count = await self._index_item(item, library_id, library_type)
                    if chunk_count == 0:
                        # Candidate was selected as indexable but produced zero chunks
                        # (e.g. a dead attachment download link) — never actually
                        # indexed, so it's a failure, not a success.
                        items_failed += 1
                    else:
                        items_added += 1
                        chunks_added += chunk_count
                elif existing_version < item_version:
                    if await self._try_metadata_only_update(item, library_id, library_type):
                        items_updated += 1
                        continue
                    # Updated item - delete old chunks and reindex
                    logger.debug(f"Reindexing updated item {item_key} ({existing_version} -> {item_version})")
                    deleted = self.vector_store.delete_item_chunks(library_id, item_key)
                    chunk_count = await self._index_item(item, library_id, library_type)
                    chunks_deleted += deleted
                    if chunk_count == 0:
                        items_failed += 1
                    else:
                        items_updated += 1
                        chunks_added += chunk_count
                else:
                    # Already up-to-date (shouldn't happen with ?since, but defensive)
                    logger.debug(f"Item {item_key} already up-to-date (version {item_version})")

            except _FATAL_EMBEDDING_ERRORS:
                # Embedding key/quota failure affects every item — abort the run so
                # the caller surfaces an error instead of silently skipping everything.
                raise
            except Exception as e:
                logger.error(f"Error processing item in incremental mode: {e}", exc_info=True)
                items_failed += 1
            finally:
                # Always report progress
                if progress_callback:
                    progress_callback(idx + 1, total_items, chunks_added)

        # Purge chunks for items deleted from Zotero since the last indexed version
        try:
            deleted_keys = await self.zotero_client.get_deleted_item_keys(
                library_id=library_id,
                library_type=library_type,
                since_version=since_version,
            )
            if deleted_keys:
                logger.info(
                    "Incremental: removing %d deleted Zotero item(s): %s%s",
                    len(deleted_keys),
                    deleted_keys[:5],
                    "..." if len(deleted_keys) > 5 else "",
                )
                for key in deleted_keys:
                    deleted = self.vector_store.delete_item_chunks(library_id, key)
                    chunks_deleted += deleted
                    if deleted:
                        self.vector_store.delete_item_deduplication_records(library_id, key)
        except Exception as exc:
            logger.warning("Could not fetch deleted item keys: %s", exc)

        # Catalog-only items: no embedding/extraction needed, just a payload write.
        for item in catalog_only_items:
            item_key = item["data"]["key"]
            item_version = item.get("version", 0)
            max_version_seen = max(max_version_seen, item_version)
            existing_version = self.vector_store.get_item_version(library_id, item_key)
            if existing_version is None:
                self._add_catalog_stub(item, library_id)
                items_cataloged += 1
            elif existing_version < item_version:
                self.vector_store.delete_item_chunks(library_id, item_key)
                self._add_catalog_stub(item, library_id)
                items_cataloged += 1
            # else: already up to date, nothing to do

        # Update metadata with new version
        metadata.last_indexed_version = max_version_seen
        metadata.total_items_indexed = metadata.total_items_indexed + items_added

        if items_failed:
            logger.warning(
                "Incremental sync for library %s: %d item(s) failed to process and "
                "were skipped (see errors above); they remain candidates for the next scan",
                library_id, items_failed,
            )

        return {
            "items_processed": len(items_with_attachments),
            "items_added": items_added,
            "items_updated": items_updated,
            "items_failed": items_failed,
            "items_cataloged": items_cataloged,
            "chunks_added": chunks_added,
            "chunks_deleted": chunks_deleted,
            "last_version": max_version_seen
        }

    async def _index_library_full(
        self,
        library_id: str,
        library_type: str,
        metadata: LibraryIndexMetadata,
        progress_callback: Optional[Callable[[int, int, int], None]] = None,
        cancellation_check: Optional[Callable[[], bool]] = None,
        max_items: Optional[int] = None
    ) -> dict:
        """Full sync: add new items, update changed items, delete orphaned chunks.

        Unlike the previous "wipe and rebuild" approach, chunks are never deleted
        upfront.  Existing chunks survive unless the parent Zotero item has been
        deleted or updated.  This makes interrupted runs safe: already-indexed items
        remain searchable while the sync continues.
        """
        logger.info(f"Full sync for library {library_id}")

        # Fetch all items from Zotero
        items = await self.zotero_client.get_library_items_since(
            library_id=library_id,
            library_type=library_type,
            since_version=None  # all items
        )
        logger.debug(f"Retrieved {len(items)} total items from Zotero")

        # Spill the full item list to a temp JSONL file and build a minimal
        # children lookup (only contentType) in one pass, then free the list.
        # This drops the ~3-4 GB in-memory list before the processing loop.
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".jsonl", prefix="zotero_items_")
        try:
            children_by_parent: dict[str, list[dict]] = {}
            with os.fdopen(tmp_fd, "w") as f:
                for _item in items:
                    f.write(json.dumps(_item) + "\n")
                    _parent = _item.get("data", {}).get("parentItem")
                    if _parent and _item.get("data", {}).get("itemType") == "attachment":
                        # Keep only contentType — avoids referencing full item dicts
                        children_by_parent.setdefault(_parent, []).append(
                            {"data": {"contentType": _item["data"].get("contentType")}}
                        )
            del items
            gc.collect()
            if sys.platform == "linux":
                ctypes.CDLL("libc.so.6").malloc_trim(0)
            logger.debug("Spilled items to JSONL; freed in-memory list")

            # Stream JSONL to filter to indexable parent items
            min_words = get_settings().min_abstract_words
            items_with_attachments: list[dict] = []
            # Regular bibliographic items with neither an indexable attachment nor a
            # substantial abstract — no text to embed, but still real catalog entries
            # (unlike notes/bare attachments, which are skipped above and never stubbed).
            catalog_only_items: list[dict] = []
            with open(tmp_path) as f:
                for line in f:
                    _item = json.loads(line)
                    if "data" not in _item:
                        continue
                    _item_type = _item["data"].get("itemType")
                    if _item_type == "note":
                        continue
                    if _item_type == "attachment":
                        # A standalone attachment (no parentItem) is itself the indexable
                        # unit — e.g. a PDF dropped straight into a collection with no
                        # bibliographic parent. Attachments that DO have a parent are
                        # handled below via children_by_parent, keyed on the parent.
                        if not _item["data"].get("parentItem") \
                                and _item["data"].get("contentType") in INDEXABLE_MIME_TYPES:
                            items_with_attachments.append(_item)
                        continue
                    _key = _item["data"]["key"]
                    _atts = children_by_parent.get(_key, [])
                    _has_indexable = any(
                        a.get("data", {}).get("contentType") in INDEXABLE_MIME_TYPES
                        for a in _atts
                    )
                    if _has_indexable:
                        items_with_attachments.append(_item)
                    elif _item["data"].get("abstractNote", "") and \
                            len(_item["data"]["abstractNote"].split()) >= min_words:
                        items_with_attachments.append(_item)
                    else:
                        catalog_only_items.append(_item)

            del children_by_parent
            gc.collect()
            if sys.platform == "linux":
                ctypes.CDLL("libc.so.6").malloc_trim(0)
            logger.debug(f"Found {len(items_with_attachments)} indexable items")

        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

        # Limit if max_items is set (test / partial run)
        if max_items is not None and max_items > 0:
            items_with_attachments = items_with_attachments[:max_items]
            logger.debug(f"Limited to {len(items_with_attachments)} items (max_items={max_items})")
            # Partial runs don't see the full catalog, so skip catalog-only
            # stub bookkeeping entirely (mirrors the orphan-purge guard below).
            catalog_only_items = []

        # Build a set of item keys currently in Zotero (indexable + catalog-only —
        # both are real, still-present items and must not be purged as orphaned)
        current_item_keys = {item["data"]["key"] for item in items_with_attachments} | \
            {item["data"]["key"] for item in catalog_only_items}

        # Single scroll to get all indexed item versions — avoids N+1 DB lookups
        indexed_versions = self.vector_store.get_all_indexed_item_versions(library_id)

        # Delete chunks for items no longer in Zotero (only when we have the full picture)
        chunks_deleted = 0
        orphaned_item_count = 0
        if max_items is None:
            orphaned_keys = set(indexed_versions) - current_item_keys
            if orphaned_keys:
                logger.info(
                    f"Removing {len(orphaned_keys)} orphaned item(s) from {library_id}: "
                    f"{list(orphaned_keys)[:5]}{'...' if len(orphaned_keys) > 5 else ''}"
                )
                for key in orphaned_keys:
                    chunks_deleted += self.vector_store.delete_item_chunks(library_id, key)
                    self.vector_store.delete_item_deduplication_records(library_id, key)
                orphaned_item_count = len(orphaned_keys)

        # Items whose only indexed record is a catalog-only stub. Zotero doesn't bump
        # a parent item's own version when a child attachment is added, so an item
        # that just gained real content can still show the same version we already
        # have indexed — plain version comparison would wrongly treat it as
        # up-to-date. Clear the stale stub now and drop it from indexed_versions so
        # the indexing loop below treats it as brand new.
        stub_keys = self.vector_store.get_stub_item_keys(library_id)
        indexable_keys = {item["data"]["key"] for item in items_with_attachments}
        for key in stub_keys & indexable_keys:
            self.vector_store.delete_item_chunks(library_id, key)
            indexed_versions.pop(key, None)

        # Pre-compute max_version_seen from the full item list — the subprocess
        # worker processes a slice and cannot update this value in the parent.
        max_version_seen = max(
            (item.get("version", 0) for item in items_with_attachments + catalog_only_items),
            default=0,
        )

        # Index items: skip unchanged, update outdated, add new
        chunks_added = 0
        items_added = 0
        items_updated = 0
        items_skipped = 0
        items_failed = 0
        total_items = len(items_with_attachments)

        if progress_callback:
            progress_callback(0, total_items, 0)

        use_subprocess = not get_settings().testing and SUBPROCESS_BATCH_SIZE > 0

        if use_subprocess:
            # --- Subprocess-isolated batch processing ---
            # Each batch runs in a fresh OS process; when it exits the OS reclaims
            # all memory unconditionally, bounding heap growth to one batch at a time.
            # The subprocess re-initialises its own ZoteroWebAPI and embedding client, so
            # it needs the same keys this processor was constructed with (falling back to
            # the global settings only for callers — e.g. tests, local-API mode — that
            # don't set one on the client/service).
            zotero_api_key = getattr(self.zotero_client, "api_key", None) or get_settings().zotero_api_key
            embedding_api_key = getattr(self.embedding_service, "api_key", None)
            items_processed_so_far = 0
            for batch_start in range(0, total_items, SUBPROCESS_BATCH_SIZE):
                if cancellation_check and cancellation_check():
                    logger.info("Cancellation requested during full sync of library %s", library_id)
                    raise RuntimeError("Indexing cancelled by user")

                batch = items_with_attachments[batch_start: batch_start + SUBPROCESS_BATCH_SIZE]
                batch_keys = {item["data"]["key"] for item in batch}
                batch_indexed = {k: v for k, v in indexed_versions.items() if k in batch_keys}

                result_q: MPQueue = MPQueue()
                proc = Process(
                    target=_run_subprocess_batch,
                    args=(batch, library_id, library_type, batch_indexed, result_q, zotero_api_key, embedding_api_key),
                    daemon=True,
                )
                proc.start()
                proc.join()

                if not result_q.empty():
                    result = result_q.get_nowait()
                    if result.get("fatal"):
                        # Re-raise fatal embedding error to abort the full run
                        from backend.services.embeddings import (
                            EmbeddingAuthenticationError,
                            EmbeddingRateLimitExhaustedError,
                        )
                        error_type = result.get("error_type", "")
                        if error_type == "EmbeddingAuthenticationError":
                            raise EmbeddingAuthenticationError(result.get("error", ""))
                        raise EmbeddingRateLimitExhaustedError(result.get("error", ""))
                    chunks_added += result.get("chunks_added", 0)
                    items_added += result.get("items_added", 0)
                    items_updated += result.get("items_updated", 0)
                    items_skipped += result.get("items_skipped", 0)
                    items_failed += result.get("items_failed", 0)
                elif proc.exitcode is not None and proc.exitcode < 0:
                    logger.warning(
                        "Batch %d–%d killed by signal %d (likely OOM); "
                        "items will be retried on next run",
                        batch_start, batch_start + len(batch), -proc.exitcode,
                    )
                else:
                    logger.warning(
                        "Batch %d–%d produced no result (exit code %s)",
                        batch_start, batch_start + len(batch), proc.exitcode,
                    )

                items_processed_so_far += len(batch)
                if progress_callback:
                    progress_callback(items_processed_so_far, total_items, chunks_added)

        else:
            # --- Inline processing (used when settings.testing=True) ---
            for idx, item in enumerate(items_with_attachments):
                if cancellation_check and cancellation_check():
                    logger.info("Cancellation requested during full sync of library %s", library_id)
                    raise RuntimeError("Indexing cancelled by user")

                try:
                    item_key = item["data"]["key"]
                    item_version = item.get("version", 0)
                    existing_version = indexed_versions.get(item_key)

                    if existing_version is None:
                        logger.debug("New item %s (version %s)", item_key, item_version)
                        chunk_count = await self._index_item(item, library_id, library_type)
                        if chunk_count == 0 and not await _item_has_indexed_content(
                            self.vector_store, library_id, item_key
                        ):
                            # Candidate was selected as indexable but produced zero
                            # chunks (e.g. a dead attachment download link) and has no
                            # existing content either — never actually indexed.
                            items_failed += 1
                        else:
                            items_added += 1
                            chunks_added += chunk_count
                    elif existing_version < item_version:
                        if await self._try_metadata_only_update(item, library_id, library_type):
                            items_updated += 1
                            continue
                        logger.debug("Updated item %s (%s -> %s)", item_key, existing_version, item_version)
                        self.vector_store.delete_item_chunks(library_id, item_key)
                        chunk_count = await self._index_item(item, library_id, library_type)
                        if chunk_count == 0 and not await _item_has_indexed_content(
                            self.vector_store, library_id, item_key
                        ):
                            items_failed += 1
                        else:
                            items_updated += 1
                            chunks_added += chunk_count
                    else:
                        items_skipped += 1

                except _FATAL_EMBEDDING_ERRORS:
                    raise
                except Exception as e:
                    logger.error("Error processing item in full sync mode: %s", e, exc_info=True)
                    items_failed += 1
                finally:
                    if progress_callback:
                        progress_callback(idx + 1, total_items, chunks_added)
                    _trim_memory_if_needed()

        # Catalog-only items: no embedding/extraction needed, just a payload write,
        # so this runs inline in the parent process regardless of use_subprocess.
        items_cataloged = 0
        for item in catalog_only_items:
            item_key = item["data"]["key"]
            item_version = item.get("version", 0)
            existing_version = indexed_versions.get(item_key)
            if existing_version is None:
                self._add_catalog_stub(item, library_id)
                items_cataloged += 1
            elif existing_version < item_version:
                self.vector_store.delete_item_chunks(library_id, item_key)
                self._add_catalog_stub(item, library_id)
                items_cataloged += 1
            # else: already up to date, nothing to do

        metadata.last_indexed_version = max_version_seen
        # Count only items that are actually indexed (newly added, updated, or already
        # current) — NOT len(items_with_attachments), which includes items that failed
        # to process.  Otherwise a scan that fails to embed everything records a full
        # count and masks an un-indexed library, defeating the cron under-indexed
        # auto-recovery in CronIndexer._resolve_mode.
        metadata.total_items_indexed = items_added + items_updated + items_skipped
        # last_full_scan_indexable is the scan floor: how many items had indexable
        # content this scan, used to detect legitimately low indexable ratios.
        metadata.last_full_scan_indexable = len(items_with_attachments)
        # Only a full scan re-examines every candidate, so this is the authoritative
        # "currently un-indexable" floor until the next full scan — incremental sync
        # never touches it (see _index_library_incremental, which only ever sees
        # items that changed, not previously-failed unchanged ones).
        metadata.last_full_scan_items_failed = items_failed

        if items_failed:
            logger.warning(
                "Full sync for library %s: %d item(s) failed to process and were "
                "skipped (see errors above); they remain candidates for the next scan",
                library_id, items_failed,
            )

        return {
            "items_processed": len(items_with_attachments),
            "items_added": items_added,
            "items_updated": items_updated,
            "items_skipped": items_skipped,
            "items_failed": items_failed,
            "items_cataloged": items_cataloged,
            "orphaned_items": orphaned_item_count,
            "chunks_added": chunks_added,
            "chunks_deleted": chunks_deleted,
            "last_version": max_version_seen
        }

    async def _index_item(
        self,
        item: dict,
        library_id: str,
        library_type: str
    ) -> int:
        """
        Index a single item with all its indexable attachments.

        Returns:
            Number of chunks created.
        """
        item_key = item["data"]["key"]
        item_version = item["version"]
        item_modified = item["data"].get("dateModified", datetime.now(UTC).isoformat())

        # Extract document metadata
        doc_metadata = DocumentMetadata(
            library_id=library_id,
            item_key=item_key,
            title=item["data"].get("title", "Untitled"),
            authors=self._extract_authors(item["data"]),
            year=self._extract_year(item["data"]),
            item_type=item["data"].get("itemType"),
            tags=self._extract_tags(item["data"]),
        )

        is_standalone_attachment = item["data"].get("itemType") == "attachment"
        if is_standalone_attachment:
            # A standalone attachment (no parentItem) IS the indexable unit — it has
            # no children to fetch and no abstract of its own.
            attachments = [item]
        else:
            attachments = await self.zotero_client.get_item_children(
                library_id=library_id,
                item_key=item_key,
                library_type=library_type
            )

        indexable_attachments = [
            att for att in attachments
            if att.get("data", {}).get("contentType") in INDEXABLE_MIME_TYPES
        ]

        abstract_note = "" if is_standalone_attachment else item["data"].get("abstractNote", "")

        total_chunks = 0

        if indexable_attachments:
            for attachment in indexable_attachments:
                attachment_key = attachment["data"]["key"]
                attachment_version = attachment.get("version", item_version)
                mime_type = attachment["data"].get("contentType", "application/pdf")
                doc_metadata.attachment_key = attachment_key

                # Download attachment
                file_bytes = await self.zotero_client.get_attachment_file(
                    library_id=library_id,
                    item_key=attachment_key,
                    library_type=library_type
                )

                if not file_bytes:
                    logger.warning(f"Could not download attachment {attachment_key}")
                    continue

                result = await self._process_attachment_bytes(
                    file_bytes=file_bytes,
                    mime_type=mime_type,
                    doc_metadata=doc_metadata,
                    item_version=item_version,
                    attachment_version=attachment_version,
                    item_modified=item_modified,
                )
                total_chunks += result.chunks_written

        # Fall back to abstractNote when no attachment is available or all downloads failed
        if total_chunks == 0 and abstract_note:
            abstract_chunks = await self._index_from_abstract(
                abstract_text=abstract_note,
                doc_metadata=doc_metadata,
                item_version=item_version,
                item_modified=item_modified,
            )
            if abstract_chunks > 0:
                logger.info(f"Indexed {abstract_chunks} abstract chunks for {item_key} (no usable attachment)")
            total_chunks += abstract_chunks
        elif not indexable_attachments:
            logger.debug(f"Item {item_key} has no indexable attachments and no abstract")

        return total_chunks

    async def _handle_same_library_duplicate(
        self,
        dup: DeduplicationRecord,
        library_id: str,
        item_key: str,
        attachment_key: str,
        doc_metadata: "DocumentMetadata",
        item_version: int,
        attachment_version: int,
        item_modified: str,
    ) -> Optional[AttachmentProcessingResult]:
        """
        Resolve a check_duplicate() hit within the same library.

        A hash match can belong to the item currently being processed (a genuine
        re-upload of already-indexed content) or to a *different* item that happens
        to share identical attachment bytes (e.g. a duplicate Zotero entry). Treating
        both cases as a blind skip left the second case with no chunks of its own —
        permanently "not indexed" from check-indexed's and count_indexed_items'
        point of view, since both key off chunk records tagged with this item_key.

        Returns an AttachmentProcessingResult if the duplicate was fully handled,
        or None if the caller should fall through to fresh extraction (the
        matching record turned out to be orphaned — no chunks left to reuse).
        """
        if dup.item_key == item_key:
            if await asyncio.to_thread(self.vector_store.get_item_version, library_id, item_key) is not None:
                logger.info(f"Skipping duplicate attachment {attachment_key} (hash: {dup.content_hash[:8]})")
                return AttachmentProcessingResult(chunks_written=0, status="skipped_duplicate")
            return None

        source_chunks = await asyncio.to_thread(self.vector_store.get_item_chunks, library_id, dup.item_key)
        if not source_chunks:
            return None

        copied = await asyncio.to_thread(
            self.vector_store.copy_chunks_cross_library,
            library_id, dup.item_key,
            library_id, item_key,
            attachment_key, doc_metadata,
            item_version, attachment_version, item_modified,
        )
        if copied == 0:
            return None

        await asyncio.to_thread(
            self.vector_store.add_deduplication_record,
            DeduplicationRecord(
                content_hash=dup.content_hash,
                library_id=library_id,
                item_key=item_key,
                relation_uri=None,
            ),
        )
        logger.info(
            f"Same-library copy: {copied} chunks from "
            f"{library_id}/{dup.item_key} -> {attachment_key}"
        )
        return AttachmentProcessingResult(
            chunks_written=copied,
            status="copied_same_library",
            source_library_id=library_id,
            source_item_key=dup.item_key,
        )

    async def _process_attachment_bytes(
        self,
        file_bytes: bytes,
        mime_type: str,
        doc_metadata: "DocumentMetadata",
        item_version: int,
        attachment_version: int,
        item_modified: str,
        on_progress: Optional[Callable[[str], None]] = None,
    ) -> AttachmentProcessingResult:
        """
        Extract, embed, and store chunks for a single attachment.

        This is the shared processing core used by both the Zotero-API-based
        local indexing path and the remote document-upload endpoint.

        Args:
            file_bytes: Raw bytes of the attachment file.
            mime_type: MIME type of the file.
            doc_metadata: Document metadata (must have attachment_key set).
            item_version: Zotero item version number.
            attachment_version: Zotero attachment version number.
            item_modified: ISO 8601 modification timestamp from Zotero.

        Returns:
            AttachmentProcessingResult with chunk count and processing status.
        """
        library_id = doc_metadata.library_id
        item_key = doc_metadata.item_key
        attachment_key = doc_metadata.attachment_key

        content_hash = hashlib.sha256(file_bytes).hexdigest()

        # Step 1: same-library dedup
        same_lib_dup = await asyncio.to_thread(self.vector_store.check_duplicate, content_hash, library_id)
        if same_lib_dup is not None:
            dup_result = await self._handle_same_library_duplicate(
                dup=same_lib_dup,
                library_id=library_id,
                item_key=item_key,
                attachment_key=attachment_key,
                doc_metadata=doc_metadata,
                item_version=item_version,
                attachment_version=attachment_version,
                item_modified=item_modified,
            )
            if dup_result is not None:
                return dup_result
            # else: the matching record's item has no chunks left (orphaned) —
            # fall through to Step 2 / fresh extraction.

        # Step 2: cross-library content-hash copy — reuse chunks from another library
        cross_record = await asyncio.to_thread(
            self.vector_store.find_cross_library_duplicate, content_hash, library_id
        )
        if cross_record:
            source_chunks = await asyncio.to_thread(
                self.vector_store.get_item_chunks, cross_record.library_id, cross_record.item_key
            )
            if source_chunks:
                copied = await asyncio.to_thread(
                    self.vector_store.copy_chunks_cross_library,
                    cross_record.library_id,
                    cross_record.item_key,
                    library_id,
                    item_key,
                    attachment_key,
                    doc_metadata,
                    item_version,
                    attachment_version,
                    item_modified,
                )
                if copied > 0:
                    await asyncio.to_thread(
                        self.vector_store.add_deduplication_record,
                        DeduplicationRecord(
                            content_hash=content_hash,
                            library_id=library_id,
                            item_key=item_key,
                            relation_uri=None,
                        ),
                    )
                    logger.info(
                        f"Cross-library copy: {copied} chunks from "
                        f"{cross_record.library_id}/{cross_record.item_key} -> {attachment_key}"
                    )
                    return AttachmentProcessingResult(
                        chunks_written=copied,
                        status="copied_cross_library",
                        source_library_id=cross_record.library_id,
                        source_item_key=cross_record.item_key,
                    )
            # Source dedup record exists but no chunks (abstract-only item): fall through to extraction

        # Extract text and chunk — split large PDFs to avoid kreuzberg OOM kills
        settings = get_settings()
        t_extract_start = time.monotonic()
        if mime_type == "application/pdf" and len(file_bytes) > settings.pdf_split_threshold:
            try:
                chunks = await self._extract_pdf_in_parts(
                    file_bytes, attachment_key, settings.pdf_split_target_part_size,
                    on_progress=on_progress,
                )
            except KreuzbergParsingError as e:
                logger.warning(f"Skipping attachment {attachment_key} (parse error — unsplittable PDF): {e}")
                return AttachmentProcessingResult(chunks_written=0, status="skipped_parse_error", error_detail=str(e))
        else:
            if on_progress:
                on_progress("Extracting text...")
            try:
                chunks = await self.document_extractor.extract_and_chunk(file_bytes, mime_type)
            except KreuzbergTimeoutError as e:
                logger.warning(f"Skipping attachment {attachment_key}: {e}")
                return AttachmentProcessingResult(chunks_written=0, status="skipped_timeout", error_detail=str(e))
            except KreuzbergParsingError as e:
                logger.warning(f"Skipping attachment {attachment_key} (parse error — binary data): {e}")
                return AttachmentProcessingResult(chunks_written=0, status="skipped_parse_error", error_detail=str(e))
            except Exception as e:
                logger.error(f"Failed to extract text from attachment {attachment_key}: {e}")
                raise RuntimeError(f"Document extraction failed for {attachment_key}: {e}") from e

        t_extract_done = time.monotonic()
        logger.info(
            f"[TIMING] {attachment_key}: extraction={t_extract_done - t_extract_start:.1f}s "
            f"chunks={len(chunks)} size_bytes={len(file_bytes)}"
        )

        if not chunks:
            logger.warning(f"No text extracted from attachment {attachment_key}")
            return AttachmentProcessingResult(chunks_written=0, status="skipped_empty")

        # Generate embeddings
        chunk_texts = [chunk.text for chunk in chunks]
        total_chunks = len(chunk_texts)
        if on_progress:
            on_progress(f"Generating embeddings (0/{total_chunks})")
        _on_embed_batch: Optional[Callable[[int, int], None]] = None
        if on_progress:
            def _on_embed_batch(done: int, total: int) -> None:
                if on_progress:
                    on_progress(f"Generating embeddings ({done}/{total})")
        embeddings = await self.embedding_service.embed_batch(chunk_texts, on_batch=_on_embed_batch)

        t_embed_done = time.monotonic()
        logger.info(
            f"[TIMING] {attachment_key}: embedding={t_embed_done - t_extract_done:.1f}s "
            f"chunks={len(chunk_texts)}"
        )

        # Build DocumentChunk objects with full metadata
        doc_chunks = []
        for i, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
            chunk_id = f"{library_id}:{item_key}:{attachment_key}:{i}"

            chunk_metadata = ChunkMetadata(
                chunk_id=chunk_id,
                document_metadata=doc_metadata,
                page_number=chunk.page_number,
                text_preview=chunk.text_preview,
                chunk_index=i,
                content_hash=content_hash,
                # Version fields
                item_version=item_version,
                attachment_version=attachment_version,
                indexed_at=datetime.now(UTC).isoformat(),
                zotero_modified=item_modified
            )

            doc_chunk = DocumentChunk(
                text=chunk.text,
                metadata=chunk_metadata,
                embedding=embedding
            )
            doc_chunks.append(doc_chunk)

        # Store in vector database — run in thread pool to avoid blocking the event loop
        if on_progress:
            on_progress(f"Storing chunks (0/{len(doc_chunks)})")
        await asyncio.to_thread(self.vector_store.add_chunks_batch, doc_chunks)

        # Record in deduplication table
        dedup_record = DeduplicationRecord(
            content_hash=content_hash,
            library_id=library_id,
            item_key=item_key,
            relation_uri=None
        )
        await asyncio.to_thread(self.vector_store.add_deduplication_record, dedup_record)

        t_store_done = time.monotonic()
        logger.info(
            f"[TIMING] {attachment_key}: store={t_store_done - t_embed_done:.1f}s "
            f"total={t_store_done - t_extract_start:.1f}s"
        )

        logger.info(f"Indexed {len(doc_chunks)} chunks for attachment {attachment_key}")
        return AttachmentProcessingResult(chunks_written=len(doc_chunks), status="indexed_fresh")

    async def _extract_pdf_in_parts(
        self,
        pdf_bytes: bytes,
        attachment_key: str,
        target_part_bytes: int,
        on_progress: Optional[Callable[[str], None]] = None,
    ) -> list[ExtractionChunk]:
        """Split a large PDF by target byte size and extract each part via kreuzberg.

        Page numbers returned by kreuzberg are 1-based within each part; adding
        page_offset (0-based pages before the part) converts them back to the
        original document's page numbers.
        """
        from backend.utils.pdf_splitter import split_pdf_bytes

        size_mb = len(pdf_bytes) / (1024 * 1024)
        if on_progress:
            on_progress(f"Splitting PDF ({size_mb:.0f} MB)...")
        t_split_start = time.monotonic()
        try:
            parts = await asyncio.to_thread(split_pdf_bytes, pdf_bytes, target_part_bytes)
        except ValueError as e:
            logger.warning(f"Could not split {attachment_key}: {e} — sending whole file.")
            parts = [(pdf_bytes, 0)]
        logger.info(
            f"[TIMING] {attachment_key}: pdf_split={time.monotonic() - t_split_start:.1f}s "
            f"parts={len(parts)}"
        )

        mb = target_part_bytes // (1024 * 1024)
        logger.info(f"Split {attachment_key} into {len(parts)} parts (~{mb} MB target each)")

        original_size = len(pdf_bytes)
        if len(parts) > 1 and original_size > target_part_bytes:
            inflated = [i for i, (b, _) in enumerate(parts) if len(b) >= original_size * 0.8]
            if inflated:
                raise KreuzbergParsingError(
                    f"PDF split produced inflated parts (parts {inflated} are ≥ 80% of "
                    f"the original {original_size // (1024*1024)} MB) — the PDF has "
                    "unsplittable shared resources and cannot be OCR-processed"
                )

        total_parts = len(parts)
        all_chunks: list[ExtractionChunk] = []
        for part_num, (part_bytes, page_offset) in enumerate(parts, 1):
            if on_progress:
                on_progress(f"Extracting text (part {part_num}/{total_parts})...")
            try:
                part_chunks = await self.document_extractor.extract_and_chunk(
                    part_bytes, "application/pdf"
                )
            except KreuzbergTimeoutError as e:
                logger.warning(
                    f"Part (offset={page_offset}) of {attachment_key} timed out: {e}"
                )
                continue
            except KreuzbergParsingError as e:
                logger.warning(
                    f"Part (offset={page_offset}) of {attachment_key} parse error: {e}"
                )
                continue

            for chunk in part_chunks:
                if chunk.page_number is not None:
                    chunk.page_number += page_offset
            all_chunks.extend(part_chunks)

        for i, chunk in enumerate(all_chunks):
            chunk.chunk_index = i

        return all_chunks

    async def _index_from_abstract(
        self,
        abstract_text: str,
        doc_metadata: DocumentMetadata,
        item_version: int,
        item_modified: str,
    ) -> int:
        """
        Index an item's abstractNote as a fallback when no attachment is available.

        Uses a virtual attachment key ``{item_key}:abstract`` so the chunks can be
        tracked and deduplicated independently of any real attachment.

        Returns:
            Number of chunks indexed (0 if abstract is too short or already indexed).
        """
        settings = get_settings()
        word_count = len(abstract_text.split())
        if word_count < settings.min_abstract_words:
            logger.debug(
                f"Abstract for {doc_metadata.item_key} too short "
                f"({word_count} words, min {settings.min_abstract_words})"
            )
            return 0

        abstract_key = f"{doc_metadata.item_key}:abstract"
        meta = doc_metadata.model_copy(update={"attachment_key": abstract_key})
        library_id = meta.library_id
        item_key = meta.item_key

        content_hash = hashlib.sha256(abstract_text.encode("utf-8")).hexdigest()
        abstract_dup = await asyncio.to_thread(self.vector_store.check_duplicate, content_hash, library_id)
        if abstract_dup is not None:
            dup_result = await self._handle_same_library_duplicate(
                dup=abstract_dup,
                library_id=library_id,
                item_key=item_key,
                attachment_key=abstract_key,
                doc_metadata=meta,
                item_version=item_version,
                attachment_version=0,
                item_modified=item_modified,
            )
            if dup_result is not None:
                if dup_result.status == "skipped_duplicate":
                    logger.info(f"Skipping duplicate abstract for {item_key}")
                return dup_result.chunks_written
            # else: fall through and index fresh (orphaned record)

        preset = settings.get_hardware_preset()
        chunker = TextChunker(max_chunk_size=preset.rag.max_chunk_size)
        chunks = chunker.chunk_text(abstract_text)

        if not chunks:
            return 0

        chunk_texts = [c.text for c in chunks]
        embeddings = await self.embedding_service.embed_batch(chunk_texts)

        doc_chunks = []
        for i, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
            chunk_id = f"{library_id}:{item_key}:{abstract_key}:{i}"
            chunk_metadata = ChunkMetadata(
                chunk_id=chunk_id,
                document_metadata=meta,
                page_number=None,
                text_preview=chunk.text_preview,
                chunk_index=i,
                content_hash=content_hash,
                item_version=item_version,
                attachment_version=0,
                indexed_at=datetime.now(UTC).isoformat(),
                zotero_modified=item_modified,
            )
            doc_chunks.append(DocumentChunk(
                text=chunk.text,
                metadata=chunk_metadata,
                embedding=embedding,
            ))

        await asyncio.to_thread(self.vector_store.add_chunks_batch, doc_chunks)
        await asyncio.to_thread(
            self.vector_store.add_deduplication_record,
            DeduplicationRecord(
                content_hash=content_hash,
                library_id=library_id,
                item_key=item_key,
                relation_uri=None,
            ),
        )

        logger.info(f"Indexed {len(doc_chunks)} abstract chunks for {item_key}")
        return len(doc_chunks)

    async def _filter_indexed_attachments(
        self,
        items: list[dict],
        library_id: str,
        library_type: str,
        children_by_parent: Optional[dict[str, list[dict]]] = None,
    ) -> list[dict]:
        """Filter items to those with at least one indexable attachment or a substantial abstract.

        When *children_by_parent* is provided (pre-built from a full item fetch), no
        additional Zotero API calls are made.  Without it, each parent item triggers one
        get_item_children() call (legacy path used by incremental sync).
        """
        items_with_content, _catalog_only = await self._split_indexable_and_catalog_only(
            items, library_id, library_type, children_by_parent
        )
        return items_with_content

    async def _split_indexable_and_catalog_only(
        self,
        items: list[dict],
        library_id: str,
        library_type: str,
        children_by_parent: Optional[dict[str, list[dict]]] = None,
    ) -> tuple[list[dict], list[dict]]:
        """Split items into (indexable, catalog-only).

        Indexable items have at least one indexable attachment or a substantial
        abstract. Catalog-only items are regular bibliographic items (not notes,
        not bare attachments) with neither — no text to embed, but still real
        catalog entries that deserve a stub record (see _add_catalog_stub).
        """
        min_words = get_settings().min_abstract_words
        items_with_content = []
        catalog_only_items = []

        for item in items:
            # Skip if not a regular item (skip notes; attachments handled below)
            if "data" not in item:
                continue

            item_type = item["data"].get("itemType")
            if item_type == "note":
                continue
            if item_type == "attachment":
                # A standalone attachment (no parentItem) is itself the indexable
                # unit — see the matching case in _index_library_full's filter.
                if not item["data"].get("parentItem") \
                        and item["data"].get("contentType") in INDEXABLE_MIME_TYPES:
                    items_with_content.append(item)
                continue

            # Check if item has any indexable attachments
            item_key = item["data"]["key"]
            if children_by_parent is not None:
                attachments = children_by_parent.get(item_key, [])
            else:
                attachments = await self.zotero_client.get_item_children(
                    library_id=library_id,
                    item_key=item_key,
                    library_type=library_type
                )

            has_indexable = any(
                att.get("data", {}).get("contentType") in INDEXABLE_MIME_TYPES
                for att in attachments
            )

            if has_indexable:
                items_with_content.append(item)
                continue

            # Fall back: include items with a substantial abstractNote
            abstract = item["data"].get("abstractNote", "")
            if abstract and len(abstract.split()) >= min_words:
                items_with_content.append(item)
            else:
                catalog_only_items.append(item)

        return items_with_content, catalog_only_items

    def _extract_authors(self, item_data: dict) -> list[str]:
        """Extract author names from Zotero item data."""
        authors = []
        creators = item_data.get("creators", [])
        for creator in creators:
            if creator.get("creatorType") in ["author", "editor"]:
                # Construct full name
                first_name = creator.get("firstName", "")
                last_name = creator.get("lastName", "")
                name = f"{first_name} {last_name}".strip()
                if name:
                    authors.append(name)
        return authors

    def _extract_year(self, item_data: dict) -> Optional[int]:
        """Extract publication year from Zotero item data."""
        date_str = item_data.get("date", "")
        if not date_str:
            return None

        # Try to extract year from date string
        # Common formats: "2024", "2024-01-15", "January 2024", etc.
        year_match = re.search(r'\b(19|20)\d{2}\b', date_str)
        if year_match:
            return int(year_match.group(0))

        return None

    def _extract_tags(self, item_data: dict) -> list[str]:
        """Extract tag/keyword names from Zotero item data.

        Zotero tags are a list of {"tag": name, "type": 0|1} dicts (type 1 =
        automatic tag). Both are extracted — no distinction is made here.
        """
        return [
            t["tag"] for t in item_data.get("tags", [])
            if t.get("tag")
        ]

    def _add_catalog_stub(self, item: dict, library_id: str) -> None:
        """Write a catalog-only stub record for a bibliographic item with no
        indexable attachment and no substantial abstract (see the caller's
        indexability filter)."""
        doc_metadata = DocumentMetadata(
            library_id=library_id,
            item_key=item["data"]["key"],
            title=item["data"].get("title", "Untitled"),
            authors=self._extract_authors(item["data"]),
            year=self._extract_year(item["data"]),
            item_type=item["data"].get("itemType"),
            tags=self._extract_tags(item["data"]),
        )
        self.vector_store.add_catalog_stub(
            doc_metadata,
            item_version=item.get("version", 0),
            zotero_modified=item["data"].get("dateModified", ""),
        )

    async def _try_metadata_only_update(
        self,
        item: dict,
        library_id: str,
        library_type: str,
    ) -> bool:
        """Attempt a cheap in-place metadata patch instead of a full reindex.

        Applies when an item's Zotero version increased but its indexed content
        (attachment bytes, or the fallback abstract text) is unchanged — i.e.
        only item-level fields (title, creators, tags, date, itemType) were
        edited. Standalone attachments and catalog-only stubs are out of scope
        (see the per-branch comments below) and always return False.

        Returns:
            True if the metadata patch was applied — caller must skip the
            delete-then-reindex path. False if a content change was detected,
            or the item isn't eligible, so the caller must fall through to
            the normal reindex path.
        """
        item_key = item["data"]["key"]

        # Standalone attachments (itemType == "attachment") ARE the indexable
        # unit, and Zotero bumps their own version for both a metadata-only
        # edit and a real file re-upload — no cheap signal distinguishes them
        # without downloading the file, so always fall through.
        if item["data"].get("itemType") == "attachment":
            return False

        existing_chunks = self.vector_store.get_item_chunks(library_id, item_key)
        if not existing_chunks:
            return False

        # Catalog-only stubs are already a cheap rewrite (see _add_catalog_stub);
        # this path is only for items with real indexed content.
        if any(not c["payload"].get("has_content", True) for c in existing_chunks):
            return False

        abstract_key = f"{item_key}:abstract"
        is_abstract_fallback = all(
            c["payload"].get("attachment_key") == abstract_key for c in existing_chunks
        )

        if is_abstract_fallback:
            abstract_text = item["data"].get("abstractNote", "")
            new_hash = hashlib.sha256(abstract_text.encode("utf-8")).hexdigest()
            if new_hash != existing_chunks[0]["payload"].get("content_hash"):
                return False
        else:
            stored_versions: dict[str, int] = {}
            for c in existing_chunks:
                att_key = c["payload"].get("attachment_key")
                if att_key is not None:
                    stored_versions[att_key] = c["payload"].get("attachment_version", 0)

            current_attachments = await self.zotero_client.get_item_children(
                library_id=library_id, item_key=item_key, library_type=library_type
            )
            current_indexable = {
                att["data"]["key"]: att.get("version", 0)
                for att in current_attachments
                if att.get("data", {}).get("contentType") in INDEXABLE_MIME_TYPES
            }

            if set(stored_versions) != set(current_indexable):
                return False
            if any(current_indexable[key] != version for key, version in stored_versions.items()):
                return False

        self.vector_store.update_item_bibliographic_metadata(
            library_id,
            item_key,
            title=item["data"].get("title", "Untitled"),
            authors=self._extract_authors(item["data"]),
            tags=self._extract_tags(item["data"]),
            year=self._extract_year(item["data"]),
            item_type=item["data"].get("itemType"),
            item_version=item.get("version", 0),
            zotero_modified=item["data"].get("dateModified", ""),
        )
        logger.info(f"Metadata-only update for item {item_key} (version {item.get('version')})")
        return True
