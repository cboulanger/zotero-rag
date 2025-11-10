"""
Document processing pipeline for indexing Zotero libraries.

This module handles PDF extraction, chunking, embedding generation,
and vector database indexing.
"""

import hashlib
import logging
from io import BytesIO
from typing import Callable, Optional

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
        force_reindex: bool = False,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        max_items: Optional[int] = None
    ) -> dict:
        """
        Index all documents in a Zotero library.

        Args:
            library_id: Zotero library ID to index.
            library_type: Library type ("user" or "group").
            force_reindex: If True, reindex all items even if already indexed.
            progress_callback: Optional callback for progress updates (current, total).
            max_items: Optional maximum number of items to process (for testing).

        Returns:
            Indexing statistics (items processed, chunks created, errors, etc.).
        """
        logger.info(f"Starting indexing for library {library_id} (type={library_type})")

        stats = {
            "library_id": library_id,
            "items_processed": 0,
            "items_skipped": 0,
            "chunks_created": 0,
            "errors": 0,
            "duplicates_skipped": 0,
            "status": "in_progress",
        }

        try:
            # Delete existing chunks if force_reindex is True
            if force_reindex:
                logger.info(f"Force reindex: deleting existing chunks for library {library_id}")
                deleted_count = self.vector_store.delete_library_chunks(library_id)
                logger.info(f"Deleted {deleted_count} existing chunks")

            # Get all items from the library
            logger.info(f"Fetching items from library {library_id}")
            items = await self.zotero_client.get_library_items(
                library_id=library_id,
                library_type=library_type,
            )

            if not items:
                logger.warning(f"No items found in library {library_id}")
                stats["status"] = "completed"
                return stats

            logger.info(f"Found {len(items)} items in library")

            # Filter for items with PDF attachments
            items_with_pdfs = []
            for item in items:
                # Check if item has data field and is a regular item (not attachment/note)
                if "data" not in item:
                    continue

                item_data = item["data"]
                item_type = item_data.get("itemType", "")

                # Skip attachments and notes - we'll get them as children
                if item_type in ["attachment", "note"]:
                    continue

                # Get children to find PDF attachments
                item_key = item_data.get("key")
                if item_key:
                    items_with_pdfs.append(item)

            logger.info(f"Found {len(items_with_pdfs)} potential items with attachments")

            # Limit items if max_items is specified
            if max_items is not None and max_items > 0:
                items_with_pdfs = items_with_pdfs[:max_items]
                logger.info(f"Limited to {len(items_with_pdfs)} items (max_items={max_items})")

            total_items = len(items_with_pdfs)

            # Report initial progress
            if progress_callback:
                progress_callback(0, total_items)

            # Process each item
            for idx, item in enumerate(items_with_pdfs):
                try:
                    item_data = item["data"]
                    item_key = item_data.get("key")

                    # Extract document metadata
                    doc_metadata = DocumentMetadata(
                        library_id=library_id,
                        item_key=item_key,
                        title=item_data.get("title", "Untitled"),
                        authors=self._extract_authors(item_data),
                        year=self._extract_year(item_data),
                        item_type=item_data.get("itemType"),
                    )

                    # Get child attachments
                    children = await self.zotero_client.get_item_children(
                        library_id=library_id,
                        item_key=item_key,
                        library_type=library_type,
                    )

                    # Find PDF attachments
                    pdf_attachments = []
                    for child in children:
                        if "data" not in child:
                            continue
                        child_data = child["data"]
                        if (child_data.get("itemType") == "attachment" and
                            child_data.get("contentType") == "application/pdf"):
                            pdf_attachments.append(child)

                    if not pdf_attachments:
                        logger.debug(f"No PDF attachments for item {item_key}")
                        stats["items_skipped"] += 1
                        continue

                    # Process each PDF attachment
                    for pdf_attachment in pdf_attachments:
                        attachment_key = pdf_attachment["data"].get("key")
                        doc_metadata.attachment_key = attachment_key

                        # Download PDF content
                        pdf_bytes = await self.zotero_client.get_attachment_file(
                            library_id=library_id,
                            item_key=attachment_key,
                            library_type=library_type,
                        )

                        if not pdf_bytes:
                            logger.warning(f"Could not download PDF for attachment {attachment_key}")
                            continue

                        # Check for duplicate using content hash
                        content_hash = hashlib.sha256(pdf_bytes).hexdigest()

                        if not force_reindex:
                            duplicate = self.vector_store.check_duplicate(content_hash)
                            if duplicate:
                                logger.info(f"Duplicate PDF found: {item_key} (same as {duplicate.item_key})")
                                stats["duplicates_skipped"] += 1
                                continue

                        # Extract text from PDF
                        try:
                            pages = self.pdf_extractor.extract_from_bytes(pdf_bytes)
                        except Exception as e:
                            logger.error(f"Failed to extract text from PDF {attachment_key}: {e}")
                            stats["errors"] += 1
                            continue

                        if not pages:
                            logger.warning(f"No text extracted from PDF {attachment_key}")
                            continue

                        # Convert pages to list of tuples for chunker
                        page_tuples = [(page.page_number, page.text) for page in pages]

                        # Chunk the text
                        chunks = self.text_chunker.chunk_pages(page_tuples)

                        if not chunks:
                            logger.warning(f"No chunks created from PDF {attachment_key}")
                            continue

                        # Create DocumentChunk objects with metadata
                        doc_chunks = []
                        for chunk in chunks:
                            chunk_id = f"{library_id}:{item_key}:{attachment_key}:{chunk.chunk_index}"

                            chunk_metadata = ChunkMetadata(
                                chunk_id=chunk_id,
                                document_metadata=doc_metadata,
                                page_number=chunk.page_number,
                                text_preview=chunk.text_preview,
                                chunk_index=chunk.chunk_index,
                                content_hash=chunk.content_hash,
                            )

                            doc_chunk = DocumentChunk(
                                text=chunk.text,
                                metadata=chunk_metadata,
                                embedding=None,  # Will be generated next
                            )

                            doc_chunks.append(doc_chunk)

                        # Generate embeddings for all chunks
                        logger.info(f"Generating embeddings for {len(doc_chunks)} chunks")
                        chunk_texts = [chunk.text for chunk in doc_chunks]
                        embeddings = await self.embedding_service.embed_batch(chunk_texts)

                        # Attach embeddings to chunks
                        for doc_chunk, embedding in zip(doc_chunks, embeddings):
                            doc_chunk.embedding = embedding

                        # Store chunks in vector database
                        logger.info(f"Storing {len(doc_chunks)} chunks in vector database")
                        self.vector_store.add_chunks_batch(doc_chunks)

                        # Add deduplication record
                        dedup_record = DeduplicationRecord(
                            content_hash=content_hash,
                            library_id=library_id,
                            item_key=item_key,
                            relation_uri=None,  # TODO: Extract from item relations
                        )
                        self.vector_store.add_deduplication_record(dedup_record)

                        stats["chunks_created"] += len(doc_chunks)
                        logger.info(f"Successfully indexed item {item_key} ({len(doc_chunks)} chunks)")

                    stats["items_processed"] += 1

                except Exception as e:
                    logger.error(f"Error processing item {item_key}: {e}", exc_info=True)
                    stats["errors"] += 1

                finally:
                    # Always report progress, even if item was skipped or errored
                    if progress_callback:
                        progress_callback(idx + 1, total_items)

            stats["status"] = "completed"
            logger.info(f"Completed indexing for library {library_id}: {stats}")

        except Exception as e:
            logger.error(f"Fatal error during indexing: {e}", exc_info=True)
            stats["status"] = "failed"
            stats["error_message"] = str(e)

        return stats

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
