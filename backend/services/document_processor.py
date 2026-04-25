"""
Document processing pipeline for indexing Zotero libraries.

This module handles document extraction, chunking, embedding generation,
and vector database indexing with support for incremental indexing.
"""

import hashlib
import logging
import re
from datetime import datetime, UTC
from typing import Callable, Optional, Literal

from backend.zotero.local_api import ZoteroLocalAPI
from backend.services.embeddings import EmbeddingService
from backend.services.extraction import DocumentExtractor, create_document_extractor
from backend.services.extraction.kreuzberg import KreuzbergTimeoutError
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

# MIME types that will be downloaded and indexed
INDEXABLE_MIME_TYPES = {
    "application/pdf",
    "text/html",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/epub+zip",
}


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

        logger.info("Initialized DocumentProcessor")

    async def index_library(
        self,
        library_id: str,
        library_type: str = "user",
        library_name: str = "Unknown",
        mode: Literal["auto", "incremental", "full"] = "auto",
        progress_callback: Optional[Callable[[int, int], None]] = None,
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
            progress_callback: Optional callback for progress updates (current, total).
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

        logger.info(f"Selected indexing mode: {effective_mode}")

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

        logger.info(f"Indexing complete: {stats}")
        return stats

    async def _index_library_incremental(
        self,
        library_id: str,
        library_type: str,
        metadata: LibraryIndexMetadata,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        cancellation_check: Optional[Callable[[], bool]] = None,
        max_items: Optional[int] = None
    ) -> dict:
        """Incremental indexing: only process new/modified items."""
        logger.info(f"Incremental index from version {metadata.last_indexed_version}")

        # Fetch items modified since last index
        since_version = metadata.last_indexed_version
        items = await self.zotero_client.get_library_items_since(
            library_id=library_id,
            library_type=library_type,
            since_version=since_version
        )

        logger.info(f"Found {len(items)} items modified since version {since_version}")

        if not items:
            return {
                "items_processed": 0,
                "items_added": 0,
                "items_updated": 0,
                "chunks_added": 0,
                "chunks_deleted": 0
            }

        # Filter to items with indexable attachments
        items_with_attachments = await self._filter_indexed_attachments(items, library_id, library_type)

        # Limit items if max_items is specified
        if max_items is not None and max_items > 0:
            items_with_attachments = items_with_attachments[:max_items]
            logger.info(f"Limited to {len(items_with_attachments)} items (max_items={max_items})")

        items_added = 0
        items_updated = 0
        chunks_added = 0
        chunks_deleted = 0
        max_version_seen = metadata.last_indexed_version
        total_items = len(items_with_attachments)

        # Report initial progress
        if progress_callback:
            progress_callback(0, total_items)

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

                if existing_version is None:
                    # New item
                    logger.info(f"Indexing new item {item_key} (version {item_version})")
                    chunk_count = await self._index_item(item, library_id, library_type)
                    items_added += 1
                    chunks_added += chunk_count
                elif existing_version < item_version:
                    # Updated item - delete old chunks and reindex
                    logger.info(f"Reindexing updated item {item_key} ({existing_version} -> {item_version})")
                    deleted = self.vector_store.delete_item_chunks(library_id, item_key)
                    chunk_count = await self._index_item(item, library_id, library_type)
                    items_updated += 1
                    chunks_deleted += deleted
                    chunks_added += chunk_count
                else:
                    # Already up-to-date (shouldn't happen with ?since, but defensive)
                    logger.debug(f"Item {item_key} already up-to-date (version {item_version})")

            except Exception as e:
                logger.error(f"Error processing item in incremental mode: {e}", exc_info=True)
            finally:
                # Always report progress
                if progress_callback:
                    progress_callback(idx + 1, total_items)

        # Update metadata with new version
        metadata.last_indexed_version = max_version_seen
        metadata.total_items_indexed = metadata.total_items_indexed + items_added

        return {
            "items_processed": len(items_with_attachments),
            "items_added": items_added,
            "items_updated": items_updated,
            "chunks_added": chunks_added,
            "chunks_deleted": chunks_deleted,
            "last_version": max_version_seen
        }

    async def _index_library_full(
        self,
        library_id: str,
        library_type: str,
        metadata: LibraryIndexMetadata,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        cancellation_check: Optional[Callable[[], bool]] = None,
        max_items: Optional[int] = None
    ) -> dict:
        """Full indexing: delete all chunks and reindex entire library."""
        logger.info(f"Full reindex for library {library_id}")

        # Delete all existing chunks for this library
        logger.info("Deleting all existing chunks...")
        chunks_deleted = self.vector_store.delete_library_chunks(library_id)
        logger.info(f"Deleted {chunks_deleted} existing chunks")

        # Also delete deduplication records to allow reprocessing of PDFs
        logger.info("Deleting deduplication records...")
        dedup_deleted = self.vector_store.delete_library_deduplication_records(library_id)
        logger.info(f"Deleted {dedup_deleted} deduplication records")

        # Fetch all items
        items = await self.zotero_client.get_library_items_since(
            library_id=library_id,
            library_type=library_type,
            since_version=None  # Get all items
        )

        logger.info(f"Retrieved {len(items)} total items")

        # Filter to items with indexable attachments
        items_with_attachments = await self._filter_indexed_attachments(items, library_id, library_type)

        logger.info(f"Found {len(items_with_attachments)} items with indexable attachments")

        # Limit items if max_items is specified
        if max_items is not None and max_items > 0:
            items_with_attachments = items_with_attachments[:max_items]
            logger.info(f"Limited to {len(items_with_attachments)} items (max_items={max_items})")

        # Index all items
        chunks_added = 0
        max_version_seen = 0
        total_items = len(items_with_attachments)

        # Report initial progress
        if progress_callback:
            progress_callback(0, total_items)

        for idx, item in enumerate(items_with_attachments):
            # Check for cancellation
            if cancellation_check and cancellation_check():
                logger.info(f"Cancellation requested during full indexing of library {library_id}")
                raise RuntimeError("Indexing cancelled by user")

            try:
                item_key = item["data"]["key"]
                item_version = item["version"]
                max_version_seen = max(max_version_seen, item_version)

                # Index item
                chunk_count = await self._index_item(item, library_id, library_type)
                chunks_added += chunk_count

            except Exception as e:
                logger.error(f"Error processing item in full mode: {e}", exc_info=True)
            finally:
                # Always report progress
                if progress_callback:
                    progress_callback(idx + 1, total_items)

        # Update metadata
        metadata.last_indexed_version = max_version_seen
        metadata.total_items_indexed = len(items_with_attachments)

        return {
            "items_processed": len(items_with_attachments),
            "items_added": len(items_with_attachments),
            "items_updated": 0,
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
        )

        # Get attachments
        attachments = await self.zotero_client.get_item_children(
            library_id=library_id,
            item_key=item_key,
            library_type=library_type
        )

        indexable_attachments = [
            att for att in attachments
            if att.get("data", {}).get("contentType") in INDEXABLE_MIME_TYPES
        ]

        abstract_note = item["data"].get("abstractNote", "")

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

    async def _process_attachment_bytes(
        self,
        file_bytes: bytes,
        mime_type: str,
        doc_metadata: "DocumentMetadata",
        item_version: int,
        attachment_version: int,
        item_modified: str,
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

        # Step 1: same-library dedup (existing behaviour)
        if self.vector_store.check_duplicate(content_hash, library_id=library_id):
            logger.info(f"Skipping duplicate attachment {attachment_key} (hash: {content_hash[:8]})")
            return AttachmentProcessingResult(chunks_written=0, status="skipped_duplicate")

        # Step 2: cross-library content-hash copy — reuse chunks from another library
        cross_record = self.vector_store.find_cross_library_duplicate(content_hash, library_id)
        if cross_record:
            source_chunks = self.vector_store.get_item_chunks(cross_record.library_id, cross_record.item_key)
            if source_chunks:
                copied = self.vector_store.copy_chunks_cross_library(
                    source_library_id=cross_record.library_id,
                    source_item_key=cross_record.item_key,
                    target_library_id=library_id,
                    target_item_key=item_key,
                    target_attachment_key=attachment_key,
                    target_doc_metadata=doc_metadata,
                    target_item_version=item_version,
                    target_attachment_version=attachment_version,
                    target_item_modified=item_modified,
                )
                if copied > 0:
                    self.vector_store.add_deduplication_record(DeduplicationRecord(
                        content_hash=content_hash,
                        library_id=library_id,
                        item_key=item_key,
                        relation_uri=None,
                    ))
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

        # Extract text and chunk
        try:
            chunks = await self.document_extractor.extract_and_chunk(file_bytes, mime_type)
        except KreuzbergTimeoutError as e:
            logger.warning(f"Skipping attachment {attachment_key}: {e}")
            return 0
        except Exception as e:
            logger.error(f"Failed to extract text from attachment {attachment_key}: {e}")
            raise RuntimeError(f"Document extraction failed for {attachment_key}: {e}") from e

        if not chunks:
            logger.warning(f"No text extracted from attachment {attachment_key}")
            return 0

        # Generate embeddings
        chunk_texts = [chunk.text for chunk in chunks]
        embeddings = await self.embedding_service.embed_batch(chunk_texts)

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

        # Store in vector database
        self.vector_store.add_chunks_batch(doc_chunks)

        # Record in deduplication table
        dedup_record = DeduplicationRecord(
            content_hash=content_hash,
            library_id=library_id,
            item_key=item_key,
            relation_uri=None
        )
        self.vector_store.add_deduplication_record(dedup_record)

        logger.info(f"Indexed {len(doc_chunks)} chunks for attachment {attachment_key}")
        return AttachmentProcessingResult(chunks_written=len(doc_chunks), status="indexed_fresh")

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
        if self.vector_store.check_duplicate(content_hash, library_id=library_id):
            logger.info(f"Skipping duplicate abstract for {item_key}")
            return 0

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

        self.vector_store.add_chunks_batch(doc_chunks)
        self.vector_store.add_deduplication_record(DeduplicationRecord(
            content_hash=content_hash,
            library_id=library_id,
            item_key=item_key,
            relation_uri=None,
        ))

        logger.info(f"Indexed {len(doc_chunks)} abstract chunks for {item_key}")
        return len(doc_chunks)

    async def _filter_indexed_attachments(
        self,
        items: list[dict],
        library_id: str,
        library_type: str
    ) -> list[dict]:
        """Filter items to those with at least one indexable attachment or a substantial abstract."""
        min_words = get_settings().min_abstract_words
        items_with_content = []

        for item in items:
            # Skip if not a regular item (skip attachments, notes, etc.)
            if "data" not in item:
                continue

            item_type = item["data"].get("itemType")
            if item_type in ["attachment", "note"]:
                continue

            # Check if item has any indexable attachments
            item_key = item["data"]["key"]
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

        return items_with_content

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
