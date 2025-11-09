"""
Text chunking strategies for document processing.

Implements semantic chunking at paragraph and sentence levels using spaCy.
"""

import logging
import hashlib
from typing import Optional
from dataclasses import dataclass

import spacy
from spacy.language import Language


logger = logging.getLogger(__name__)


@dataclass
class TextChunk:
    """A chunk of text with metadata."""

    text: str
    page_number: Optional[int]
    chunk_index: int
    start_char: int  # Start character position in source text
    end_char: int    # End character position in source text

    @property
    def content_hash(self) -> str:
        """Compute SHA256 hash of chunk content."""
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    @property
    def text_preview(self) -> str:
        """Get first 5 words as citation anchor."""
        words = self.text.split()[:5]
        return " ".join(words)

    def __repr__(self):
        preview = self.text[:50] + "..." if len(self.text) > 50 else self.text
        return f"TextChunk(index={self.chunk_index}, page={self.page_number}, text='{preview}')"


class TextChunker:
    """
    Semantic text chunker using spaCy.

    Chunks text at paragraph and sentence boundaries, respecting
    maximum chunk size while preserving semantic coherence.
    """

    def __init__(
        self,
        max_chunk_size: int = 512,
        overlap_size: int = 50,
        model_name: str = "en_core_web_sm",
    ):
        """
        Initialize text chunker.

        Args:
            max_chunk_size: Maximum number of characters per chunk
            overlap_size: Number of characters to overlap between chunks
            model_name: spaCy model name to use
        """
        self.max_chunk_size = max_chunk_size
        self.overlap_size = overlap_size
        self.model_name = model_name
        self._nlp: Optional[Language] = None

        logger.info(f"Initialized TextChunker (max_size={max_chunk_size}, overlap={overlap_size})")

    def _load_model(self):
        """Lazy load spaCy model with auto-download if needed."""
        if self._nlp is None:
            try:
                logger.info(f"Loading spaCy model: {self.model_name}")
                self._nlp = spacy.load(self.model_name)
                # Disable unnecessary components for performance (keep only sentence segmentation)
                # Most models have 'parser' which does sentence segmentation
                disabled = []
                for pipe_name in self._nlp.pipe_names:
                    if pipe_name not in ["parser", "sentencizer", "senter"]:
                        disabled.append(pipe_name)
                if disabled:
                    self._nlp.disable_pipes(*disabled)
                    logger.debug(f"Disabled spaCy pipes: {disabled}")
            except OSError:
                # Model not found, try to download it using uv
                logger.warning(f"spaCy model '{self.model_name}' not found. Attempting to download...")
                try:
                    import subprocess
                    import sys

                    # Determine the model URL based on model name
                    # For en_core_web_sm 3.8.0 (matches spaCy 3.8.x)
                    model_urls = {
                        "en_core_web_sm": "https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl",
                    }

                    if self.model_name not in model_urls:
                        raise ValueError(f"No download URL configured for model: {self.model_name}")

                    model_url = model_urls[self.model_name]
                    logger.info(f"Downloading {self.model_name} from {model_url}")

                    # Use uv pip install to download the model
                    result = subprocess.run(
                        ["uv", "pip", "install", model_url],
                        check=True,
                        capture_output=True,
                        text=True
                    )
                    logger.info(f"Successfully downloaded {self.model_name}")
                    logger.debug(f"Install output: {result.stdout}")

                    # Try loading again
                    self._nlp = spacy.load(self.model_name)
                    # Disable unnecessary components
                    disabled = []
                    for pipe_name in self._nlp.pipe_names:
                        if pipe_name not in ["parser", "sentencizer", "senter"]:
                            disabled.append(pipe_name)
                    if disabled:
                        self._nlp.disable_pipes(*disabled)
                except Exception as e:
                    logger.error(f"Failed to download spaCy model '{self.model_name}': {e}")
                    raise RuntimeError(
                        f"spaCy model '{self.model_name}' not found and could not be downloaded. "
                        f"Install it manually with: uv pip install https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl"
                    ) from e

    def chunk_text(
        self,
        text: str,
        page_number: Optional[int] = None,
        start_index: int = 0,
    ) -> list[TextChunk]:
        """
        Chunk text into semantic units.

        Args:
            text: Text to chunk
            page_number: Optional page number for all chunks
            start_index: Starting index for chunk numbering

        Returns:
            List of text chunks
        """
        if not text or not text.strip():
            return []

        self._load_model()

        # Process text with spaCy
        doc = self._nlp(text)

        # Get sentences
        sentences = list(doc.sents)
        if not sentences:
            # Fallback: treat entire text as one chunk if no sentences detected
            return [TextChunk(
                text=text.strip(),
                page_number=page_number,
                chunk_index=start_index,
                start_char=0,
                end_char=len(text),
            )]

        # Build chunks from sentences
        chunks = []
        current_chunk_text = ""
        current_chunk_start = 0
        chunk_sentences = []

        for sent in sentences:
            sent_text = sent.text.strip()
            if not sent_text:
                continue

            # Check if adding this sentence would exceed max size
            potential_text = current_chunk_text + " " + sent_text if current_chunk_text else sent_text

            if len(potential_text) > self.max_chunk_size and current_chunk_text:
                # Finalize current chunk
                chunks.append(TextChunk(
                    text=current_chunk_text,
                    page_number=page_number,
                    chunk_index=start_index + len(chunks),
                    start_char=current_chunk_start,
                    end_char=current_chunk_start + len(current_chunk_text),
                ))

                # Start new chunk with overlap
                # Use last sentence as overlap if it fits
                if chunk_sentences and len(chunk_sentences[-1]) < self.overlap_size:
                    overlap_text = chunk_sentences[-1]
                    current_chunk_text = overlap_text + " " + sent_text
                    # Keep roughly the same start position for continuity
                    current_chunk_start = max(0, current_chunk_start + len(current_chunk_text) - len(overlap_text) - len(sent_text) - 1)
                    chunk_sentences = [overlap_text, sent_text]
                else:
                    current_chunk_text = sent_text
                    current_chunk_start = sent.start_char
                    chunk_sentences = [sent_text]
            else:
                # Add sentence to current chunk
                if not current_chunk_text:
                    current_chunk_start = sent.start_char
                current_chunk_text = potential_text
                chunk_sentences.append(sent_text)

        # Add final chunk if any text remains
        if current_chunk_text:
            chunks.append(TextChunk(
                text=current_chunk_text,
                page_number=page_number,
                chunk_index=start_index + len(chunks),
                start_char=current_chunk_start,
                end_char=current_chunk_start + len(current_chunk_text),
            ))

        logger.info(f"Chunked text into {len(chunks)} chunks (page {page_number or 'unknown'})")
        return chunks

    def chunk_pages(
        self,
        pages: list[tuple[int, str]],
    ) -> list[TextChunk]:
        """
        Chunk multiple pages of text.

        Args:
            pages: List of (page_number, text) tuples

        Returns:
            List of text chunks from all pages
        """
        all_chunks = []
        chunk_index = 0

        for page_num, page_text in pages:
            page_chunks = self.chunk_text(
                page_text,
                page_number=page_num,
                start_index=chunk_index,
            )
            all_chunks.extend(page_chunks)
            chunk_index += len(page_chunks)

        logger.info(f"Chunked {len(pages)} pages into {len(all_chunks)} total chunks")
        return all_chunks


def create_simple_chunks(
    text: str,
    max_size: int = 512,
    overlap: int = 50,
    page_number: Optional[int] = None,
) -> list[TextChunk]:
    """
    Create simple character-based chunks (fallback without spaCy).

    Args:
        text: Text to chunk
        max_size: Maximum chunk size
        overlap: Overlap between chunks
        page_number: Optional page number

    Returns:
        List of text chunks
    """
    if not text:
        return []

    chunks = []
    start = 0
    chunk_index = 0

    while start < len(text):
        end = start + max_size

        # Try to break at word boundary
        if end < len(text):
            # Look for last space before max_size
            space_pos = text.rfind(" ", start, end)
            if space_pos > start:
                end = space_pos

        chunk_text = text[start:end].strip()
        if chunk_text:
            chunks.append(TextChunk(
                text=chunk_text,
                page_number=page_number,
                chunk_index=chunk_index,
                start_char=start,
                end_char=end,
            ))
            chunk_index += 1

        # Move start position with overlap
        start = end - overlap if end - overlap > start else end

    return chunks
