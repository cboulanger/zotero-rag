"""
Document processing pipeline for indexing Zotero libraries.

This module handles PDF extraction, chunking, embedding generation,
and vector database indexing with support for incremental indexing.
"""

import hashlib
import logging
from datetime import datetime
from io import BytesIO
from typing import Callable, Optional, Literal

from backend.zotero.local_api import ZoteroLocalAPI
from backend.services.embeddings import EmbeddingService
from backend.services.pdf_extractor import PDFExtractor
from backend.services.chunking import TextChunker
from backend.db.vector_store import VectorStore
from backend.models.document import (
    DocumentMetadata,
    ChunkMetadata,
    DocumentChunk,
    DeduplicationRecord,
)
from backend.models.library import LibraryIndexMetadata

logger = logging.getLogger(__name__)


class DocumentProcessor:
    """
    Document processing pipeline for indexing Zotero libraries.

    Coordinates PDF extraction, text chunking, embedding generation,
    and vector database indexing.
    """

    def __init__(
        self,
        zotero_client: ZoteroLocalAPI,
        embedding_service: EmbeddingService,
        vector_store: VectorStore,
        max_chunk_size: int = 512,
        chunk_overlap: int = 50,
    ):
        """
        Initialize document processor.

        Args:
            zotero_client: Zotero API client.
            embedding_service: Service for generating embeddings.
            vector_store: Vector database for storing embeddings.
            max_chunk_size: Maximum characters per chunk.
            chunk_overlap: Overlap between chunks.
        """
        self.zotero_client = zotero_client
        self.embedding_service = embedding_service
        self.vector_store = vector_store

        # Initialize PDF extraction and chunking services
        self.pdf_extractor = PDFExtractor()
        self.text_chunker = TextChunker(
            max_chunk_size=max_chunk_size,
            overlap_size=chunk_overlap,
        )

        logger.info("Initialized DocumentProcessor")

    async def index_library(
        self,
        library_id: str,
        library_type: str = "user",
        library_name: str = "Unknown",
        mode: Literal["auto", "incremental", "full"] = "auto",
        progress_callback: Optional[Callable[[int, int], None]] = None,
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
            max_items: Optional maximum number of items to process (for testing).

        Returns:
            Indexing statistics with counts, timing, and mode used.
        """
        logger.info(f"Starting indexing for library {library_id} (mode={mode})")
        start_time = datetime.utcnow()

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
                library_id, library_type, metadata, progress_callback, max_items
            )
        else:
            stats = await self._index_library_incremental(
                library_id, library_type, metadata, progress_callback, max_items
            )

        # Update library metadata
        metadata.indexing_mode = effective_mode
        metadata.last_indexed_at = datetime.utcnow().isoformat()
        metadata.total_chunks = self.vector_store.count_library_chunks(library_id)
        self.vector_store.update_library_metadata(metadata)

        elapsed = (datetime.utcnow() - start_time).total_seconds()
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

        # Filter to items with PDF attachments
        items_with_pdfs = await self._filter_items_with_pdfs(items, library_id, library_type)

        # Limit items if max_items is specified
        if max_items is not None and max_items > 0:
            items_with_pdfs = items_with_pdfs[:max_items]
            logger.info(f"Limited to {len(items_with_pdfs)} items (max_items={max_items})")

        items_added = 0
        items_updated = 0
        chunks_added = 0
        chunks_deleted = 0
        max_version_seen = metadata.last_indexed_version
        total_items = len(items_with_pdfs)

        # Report initial progress
        if progress_callback:
            progress_callback(0, total_items)

        for idx, item in enumerate(items_with_pdfs):
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
            "items_processed": len(items_with_pdfs),
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
        max_items: Optional[int] = None
    ) -> dict:
        """Full indexing: delete all chunks and reindex entire library."""
        logger.info(f"Full reindex for library {library_id}")

        # Delete all existing chunks for this library
        logger.info("Deleting all existing chunks...")
        chunks_deleted = self.vector_store.delete_library_chunks(library_id)
        logger.info(f"Deleted {chunks_deleted} existing chunks")

        # Fetch all items
        items = await self.zotero_client.get_library_items_since(
            library_id=library_id,
            library_type=library_type,
            since_version=None  # Get all items
        )

        logger.info(f"Retrieved {len(items)} total items")

        # Filter to items with PDFs
        items_with_pdfs = await self._filter_items_with_pdfs(items, library_id, library_type)

        logger.info(f"Found {len(items_with_pdfs)} items with PDFs")

        # Limit items if max_items is specified
        if max_items is not None and max_items > 0:
            items_with_pdfs = items_with_pdfs[:max_items]
            logger.info(f"Limited to {len(items_with_pdfs)} items (max_items={max_items})")

        # Index all items
        chunks_added = 0
        max_version_seen = 0
        total_items = len(items_with_pdfs)

        # Report initial progress
        if progress_callback:
            progress_callback(0, total_items)

        for idx, item in enumerate(items_with_pdfs):
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
        metadata.total_items_indexed = len(items_with_pdfs)

        return {
            "items_processed": len(items_with_pdfs),
            "items_added": len(items_with_pdfs),
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
        Index a single item with all its PDF attachments.

        Returns:
            Number of chunks created
        """
        item_key = item["data"]["key"]
        item_version = item["version"]
        item_modified = item["data"].get("dateModified", datetime.utcnow().isoformat())

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

        pdf_attachments = [
            att for att in attachments
            if att.get("data", {}).get("contentType") == "application/pdf"
        ]

        if not pdf_attachments:
            logger.debug(f"Item {item_key} has no PDF attachments")
            return 0

        total_chunks = 0

        for attachment in pdf_attachments:
            attachment_key = attachment["data"]["key"]
            attachment_version = attachment.get("version", item_version)
            doc_metadata.attachment_key = attachment_key

            # Download PDF
            pdf_bytes = await self.zotero_client.get_attachment_file(
                library_id=library_id,
                item_key=attachment_key,
                library_type=library_type
            )

            if not pdf_bytes:
                logger.warning(f"Could not download PDF for attachment {attachment_key}")
                continue

            # Check deduplication (content hash)
            content_hash = hashlib.sha256(pdf_bytes).hexdigest()
            if self.vector_store.check_duplicate(content_hash):
                logger.info(f"Skipping duplicate PDF {attachment_key} (hash: {content_hash[:8]})")
                continue

            # Extract text
            try:
                pages = self.pdf_extractor.extract_from_bytes(pdf_bytes)
            except Exception as e:
                logger.error(f"Failed to extract text from PDF {attachment_key}: {e}")
                continue

            if not pages:
                logger.warning(f"No text extracted from PDF {attachment_key}")
                continue

            # Convert pages to list of tuples for chunker
            page_tuples = [(page.page_number, page.text) for page in pages]

            # Chunk text
            chunks = self.text_chunker.chunk_pages(page_tuples)

            if not chunks:
                logger.warning(f"No chunks created from PDF {attachment_key}")
                continue

            # Generate embeddings
            chunk_texts = [chunk.text for chunk in chunks]
            embeddings = await self.embedding_service.embed_batch(chunk_texts)

            # Create chunk metadata with version info
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
                    indexed_at=datetime.utcnow().isoformat(),
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

            total_chunks += len(doc_chunks)
            logger.info(f"Indexed {len(doc_chunks)} chunks for attachment {attachment_key}")

        return total_chunks

    async def _filter_items_with_pdfs(
        self,
        items: list[dict],
        library_id: str,
        library_type: str
    ) -> list[dict]:
        """Filter items to only those with PDF attachments."""
        items_with_pdfs = []

        for item in items:
            # Skip if not a regular item (skip attachments, notes, etc.)
            if "data" not in item:
                continue

            item_type = item["data"].get("itemType")
            if item_type in ["attachment", "note"]:
                continue

            # Check if item has PDF attachments
            item_key = item["data"]["key"]
            attachments = await self.zotero_client.get_item_children(
                library_id=library_id,
                item_key=item_key,
                library_type=library_type
            )

            has_pdf = any(
                att.get("data", {}).get("contentType") == "application/pdf"
                for att in attachments
            )

            if has_pdf:
                items_with_pdfs.append(item)

        return items_with_pdfs

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
        import re
        year_match = re.search(r'\b(19|20)\d{2}\b', date_str)
        if year_match:
            return int(year_match.group(0))

        return None
