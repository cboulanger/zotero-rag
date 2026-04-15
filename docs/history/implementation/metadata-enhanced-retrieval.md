# Metadata-Enhanced Retrieval for Zotero RAG

## Document Status

- **Created:** January 2025
- **Version:** 1.0
- **Related Documents:**
  - [indexing-and-embedding-strategies.md](indexing-and-embedding-strategies.md)
  - [architecture.md](../architecture.md)

---

## Table of Contents

- [Overview](#overview)
- [Current State Analysis](#current-state-analysis)
- [Problem Statement](#problem-statement)
- [Use Cases](#use-cases)
- [Proposed Solution](#proposed-solution)
- [Implementation Details](#implementation-details)
- [Performance Considerations](#performance-considerations)
- [Privacy & Security](#privacy--security)
- [Alternatives Considered](#alternatives-considered)
- [Implementation Roadmap](#implementation-roadmap)
- [Open Questions](#open-questions)

---

## Overview

This document analyzes the addition of rich metadata to chunk embeddings to enable sophisticated queries that combine semantic search with structured metadata filtering. This would allow questions like:

- "Where does John Doe speak about the problem of nothingness?" (author filter)
- "Which articles in the Journal of Complete Nonsense mention works by William Shakespeare?" (publication filter + semantic search)
- "What papers from 2023 discuss climate change?" (year + topic)
- "Show me conference papers tagged 'machine learning' about transformer architectures" (item type + tags + semantic)

---

## Current State Analysis

### Existing Metadata in Vector Database

From [backend/models/document.py](../../backend/models/document.py) and [backend/db/vector_store.py](../../backend/db/vector_store.py):

**Currently Stored in Qdrant Payloads:**

```python
{
    "text": str,                    # Chunk content
    "chunk_id": str,                # Unique chunk ID
    "library_id": str,              # Zotero library ID
    "item_key": str,                # Zotero item key
    "attachment_key": str,          # PDF attachment key
    "title": str,                   # Document title
    "authors": list[str],           # Author names
    "year": int,                    # Publication year
    "page_number": int,             # Page number in PDF
    "text_preview": str,            # First 5 words
    "chunk_index": int,             # Position in document
    "content_hash": str,            # SHA256 of chunk
}
```

**Currently Available in DocumentMetadata:**

```python
class DocumentMetadata(BaseModel):
    library_id: str
    item_key: str
    title: Optional[str]
    authors: list[str]
    year: Optional[int]
    item_type: Optional[str]      # NOT currently stored in vector DB
    attachment_key: Optional[str]
```

### What's Missing

Key metadata fields available from Zotero but **not** currently stored:

1. **Item Type:** `journalArticle`, `conferencePaper`, `book`, `bookSection`, etc.
2. **Publication Details:**
   - `publicationTitle` (journal/conference name)
   - `volume`, `issue`, `pages`
   - `journalAbbreviation`
3. **Identifiers:**
   - `DOI`, `ISBN`, `ISSN`
   - `url`
4. **Tags:** User-defined keywords
5. **Collections:** Zotero collection membership
6. **Abstract:** `abstractNote`
7. **Language:** `language`
8. **Publisher:** `publisher`
9. **Extra metadata:** `extra` (custom fields)

### Current Query Capabilities

From [backend/db/vector_store.py:195-224](../../backend/db/vector_store.py#L195-L224):

```python
def search(
    self,
    query_vector: list[float],
    limit: int = 5,
    score_threshold: Optional[float] = None,
    library_ids: Optional[list[str]] = None,  # ONLY library filtering supported
) -> list[SearchResult]:
```

**Limitations:**

- Only semantic similarity + library filtering
- No author, year, publication, tag, or type filtering
- No combining multiple metadata filters
- No support for complex queries (AND/OR conditions)

---

## Problem Statement

**Goal:** Enable sophisticated queries that combine semantic search with rich metadata filtering.

**Challenges:**

1. **Storage Overhead:** Adding metadata increases payload size in vector database
2. **Query Complexity:** Need flexible filtering API without overcomplicating interface
3. **Backward Compatibility:** Existing indexed data needs migration path
4. **Indexing Performance:** More metadata to extract and store during indexing
5. **LLM Context:** Should metadata be included in context sent to LLM for better answers?

---

## Use Cases

### 1. Author-Based Queries

**Query:** "Where does John Doe speak about the problem of nothingness?"

**Filter:**

```python
authors contains "John Doe" AND semantic_search("problem of nothingness")
```

**Use Case:** Find specific discussions by known researchers

---

### 2. Publication-Based Queries

**Query:** "Which articles in the Journal of Complete Nonsense mention works by William Shakespeare?"

**Filter:**

```python
publicationTitle = "Journal of Complete Nonsense" AND
semantic_search("William Shakespeare")
```

**Use Case:** Literature review within specific journals/conferences

---

### 3. Temporal Queries

**Query:** "What did recent papers (2023-2024) say about climate change?"

**Filter:**

```python
year >= 2023 AND year <= 2024 AND semantic_search("climate change")
```

**Use Case:** Finding recent developments in a field

---

### 4. Item Type Queries

**Query:** "Show me conference papers about transformer architectures"

**Filter:**

```python
itemType = "conferencePaper" AND semantic_search("transformer architectures")
```

**Use Case:** Filtering by publication venue type

---

### 5. Tag-Based Queries

**Query:** "Papers tagged 'machine learning' discussing attention mechanisms"

**Filter:**

```python
tags contains "machine learning" AND semantic_search("attention mechanisms")
```

**Use Case:** Leveraging user's manual categorization

---

### 6. Complex Multi-Filter Queries

**Query:** "Books or book chapters from 2020+ about deep learning by authors including 'Goodfellow'"

**Filter:**

```python
(itemType = "book" OR itemType = "bookSection") AND
year >= 2020 AND
authors contains "Goodfellow" AND
semantic_search("deep learning")
```

**Use Case:** Highly specific literature searches

---

## Proposed Solution

### Enhanced Metadata Schema

Extend the vector database payload to include:

```python
# CURRENT (keep these)
{
    "text": str,
    "chunk_id": str,
    "library_id": str,
    "item_key": str,
    "attachment_key": str,
    "title": str,
    "authors": list[str],
    "year": int,
    "page_number": int,
    "text_preview": str,
    "chunk_index": int,
    "content_hash": str,
}

# NEW ADDITIONS
{
    # Item classification
    "item_type": str,                      # journalArticle, book, etc.

    # Publication details
    "publication_title": str,              # Journal/conference name
    "volume": str,                         # Volume number
    "issue": str,                          # Issue number
    "pages": str,                          # Page range
    "publisher": str,                      # Publisher name

    # Identifiers
    "doi": str,                            # DOI
    "isbn": str,                           # ISBN (books)
    "issn": str,                           # ISSN (journals)
    "url": str,                            # URL

    # Categorization
    "tags": list[str],                     # User tags
    "collections": list[str],              # Collection names/IDs

    # Content metadata
    "abstract": str,                       # Abstract text (for context)
    "language": str,                       # Language code

    # Versioning (for incremental indexing)
    "item_version": int,                   # Zotero item version
    "attachment_version": int,             # Attachment version

    # Timestamps
    "date_added": str,                     # ISO datetime
    "date_modified": str,                  # ISO datetime
}
```

### API Design

#### 1. Enhanced Search Method

```python
# backend/db/vector_store.py

from typing import Optional, Literal, Any
from qdrant_client.models import Filter, FieldCondition, MatchValue, MatchAny, Range

class MetadataFilter(BaseModel):
    """Metadata filtering options for search."""

    # Library filtering (existing)
    library_ids: Optional[list[str]] = None

    # Author filtering
    authors_include: Optional[list[str]] = None      # ANY of these authors
    authors_exclude: Optional[list[str]] = None      # NONE of these authors

    # Publication filtering
    publication_titles: Optional[list[str]] = None   # Specific journals/conferences
    item_types: Optional[list[str]] = None           # journalArticle, book, etc.

    # Temporal filtering
    year_min: Optional[int] = None
    year_max: Optional[int] = None
    date_added_after: Optional[str] = None           # ISO datetime
    date_modified_after: Optional[str] = None

    # Tag filtering
    tags_include: Optional[list[str]] = None         # ANY of these tags
    tags_exclude: Optional[list[str]] = None         # NONE of these tags

    # Collection filtering
    collections: Optional[list[str]] = None          # Specific collections

    # Identifier filtering
    dois: Optional[list[str]] = None

    # Language filtering
    languages: Optional[list[str]] = None            # en, de, fr, etc.


def search(
    self,
    query_vector: list[float],
    limit: int = 5,
    score_threshold: Optional[float] = None,
    metadata_filter: Optional[MetadataFilter] = None,  # NEW PARAMETER
) -> list[SearchResult]:
    """
    Search for similar chunks with metadata filtering.

    Args:
        query_vector: Query embedding vector
        limit: Maximum number of results
        score_threshold: Minimum similarity score
        metadata_filter: Optional metadata filtering criteria

    Returns:
        List of search results with chunks and scores
    """
    # Build Qdrant filter from metadata_filter
    qdrant_filter = self._build_qdrant_filter(metadata_filter)

    # Execute search with filter
    results = self.client.query_points(
        collection_name=self.CHUNKS_COLLECTION,
        query=query_vector,
        limit=limit,
        score_threshold=score_threshold,
        query_filter=qdrant_filter,
        with_payload=True,
    ).points

    return self._convert_to_search_results(results)


def _build_qdrant_filter(
    self,
    metadata_filter: Optional[MetadataFilter]
) -> Optional[Filter]:
    """Convert MetadataFilter to Qdrant Filter object."""
    if not metadata_filter:
        return None

    must_conditions = []
    should_conditions = []
    must_not_conditions = []

    # Library filtering (existing logic)
    if metadata_filter.library_ids:
        should_conditions.extend([
            FieldCondition(key="library_id", match=MatchValue(value=lib_id))
            for lib_id in metadata_filter.library_ids
        ])

    # Author filtering (NEW)
    if metadata_filter.authors_include:
        # Match if ANY author in the list
        should_conditions.extend([
            FieldCondition(key="authors", match=MatchAny(any=metadata_filter.authors_include))
        ])

    if metadata_filter.authors_exclude:
        # Exclude if ANY of these authors present
        must_not_conditions.extend([
            FieldCondition(key="authors", match=MatchAny(any=metadata_filter.authors_exclude))
        ])

    # Publication filtering (NEW)
    if metadata_filter.publication_titles:
        should_conditions.extend([
            FieldCondition(key="publication_title", match=MatchValue(value=pub))
            for pub in metadata_filter.publication_titles
        ])

    if metadata_filter.item_types:
        should_conditions.extend([
            FieldCondition(key="item_type", match=MatchValue(value=itype))
            for itype in metadata_filter.item_types
        ])

    # Temporal filtering (NEW)
    if metadata_filter.year_min or metadata_filter.year_max:
        must_conditions.append(
            FieldCondition(
                key="year",
                range=Range(
                    gte=metadata_filter.year_min,
                    lte=metadata_filter.year_max,
                )
            )
        )

    # Tag filtering (NEW)
    if metadata_filter.tags_include:
        should_conditions.extend([
            FieldCondition(key="tags", match=MatchAny(any=metadata_filter.tags_include))
        ])

    if metadata_filter.tags_exclude:
        must_not_conditions.extend([
            FieldCondition(key="tags", match=MatchAny(any=metadata_filter.tags_exclude))
        ])

    # Collection filtering (NEW)
    if metadata_filter.collections:
        should_conditions.extend([
            FieldCondition(key="collections", match=MatchAny(any=metadata_filter.collections))
        ])

    # Language filtering (NEW)
    if metadata_filter.languages:
        should_conditions.extend([
            FieldCondition(key="language", match=MatchValue(value=lang))
            for lang in metadata_filter.languages
        ])

    # Build final filter
    if not must_conditions and not should_conditions and not must_not_conditions:
        return None

    return Filter(
        must=must_conditions if must_conditions else None,
        should=should_conditions if should_conditions else None,
        must_not=must_not_conditions if must_not_conditions else None,
    )
```

#### 2. Enhanced RAG Query API

```python
# backend/services/rag_engine.py

class QueryRequest(BaseModel):
    """RAG query request with metadata filtering."""
    question: str
    library_ids: list[str]
    top_k: int = 5
    min_score: float = 0.5

    # NEW: Metadata filtering options
    filter_authors: Optional[list[str]] = None
    filter_publication_titles: Optional[list[str]] = None
    filter_item_types: Optional[list[str]] = None
    filter_year_min: Optional[int] = None
    filter_year_max: Optional[int] = None
    filter_tags: Optional[list[str]] = None
    filter_collections: Optional[list[str]] = None
    filter_languages: Optional[list[str]] = None


async def query(
    self,
    request: QueryRequest,  # Now takes structured request
) -> QueryResult:
    """
    Answer a question using RAG with metadata filtering.
    """
    logger.info(f"Processing RAG query: {request.question}")

    # Generate query embedding
    query_embedding = await self.embedding_service.embed_text(request.question)

    # Build metadata filter from request
    metadata_filter = MetadataFilter(
        library_ids=request.library_ids,
        authors_include=request.filter_authors,
        publication_titles=request.filter_publication_titles,
        item_types=request.filter_item_types,
        year_min=request.filter_year_min,
        year_max=request.filter_year_max,
        tags_include=request.filter_tags,
        collections=request.filter_collections,
        languages=request.filter_languages,
    )

    # Search with metadata filtering
    search_results = self.vector_store.search(
        query_vector=query_embedding,
        limit=request.top_k,
        score_threshold=request.min_score,
        metadata_filter=metadata_filter,
    )

    # Rest of RAG pipeline...
    return self._generate_answer(request.question, search_results)
```

#### 3. REST API Endpoint

```python
# backend/api/query.py

@router.post("/query", response_model=QueryResult)
async def query_rag(request: QueryRequest):
    """
    Submit a RAG query with optional metadata filtering.

    Example with filtering:
    {
        "question": "What did John Doe say about climate change?",
        "library_ids": ["1"],
        "filter_authors": ["John Doe"],
        "filter_year_min": 2020,
        "filter_tags": ["climate", "environment"]
    }
    """
    return await rag_engine.query(request)
```

---

## Implementation Details

### 1. Update Data Models

**File:** [backend/models/document.py](../../backend/models/document.py)

```python
class DocumentMetadata(BaseModel):
    """Metadata for a source document (Zotero item)."""

    # EXISTING FIELDS (keep all)
    library_id: str
    item_key: str
    title: Optional[str] = None
    authors: list[str] = Field(default_factory=list)
    year: Optional[int] = None
    item_type: Optional[str] = None
    attachment_key: Optional[str] = None

    # NEW FIELDS
    publication_title: Optional[str] = Field(None, description="Journal/conference name")
    volume: Optional[str] = Field(None, description="Volume number")
    issue: Optional[str] = Field(None, description="Issue number")
    pages: Optional[str] = Field(None, description="Page range")
    publisher: Optional[str] = Field(None, description="Publisher name")

    doi: Optional[str] = Field(None, description="DOI")
    isbn: Optional[str] = Field(None, description="ISBN")
    issn: Optional[str] = Field(None, description="ISSN")
    url: Optional[str] = Field(None, description="Item URL")

    tags: list[str] = Field(default_factory=list, description="User tags")
    collections: list[str] = Field(default_factory=list, description="Collection IDs")

    abstract: Optional[str] = Field(None, description="Abstract text")
    language: Optional[str] = Field(None, description="Language code (en, de, etc.)")

    item_version: Optional[int] = Field(None, description="Zotero item version")
    attachment_version: Optional[int] = Field(None, description="Attachment version")

    date_added: Optional[str] = Field(None, description="ISO datetime added")
    date_modified: Optional[str] = Field(None, description="ISO datetime modified")
```

### 2. Extract Metadata from Zotero

**File:** [backend/services/document_processor.py](../../backend/services/document_processor.py)

Add metadata extraction logic:

```python
def _extract_metadata(self, item: dict, attachment: dict) -> DocumentMetadata:
    """Extract rich metadata from Zotero item."""
    data = item.get("data", {})

    # Extract creators
    authors = []
    for creator in data.get("creators", []):
        if creator.get("creatorType") == "author":
            first = creator.get("firstName", "")
            last = creator.get("lastName", "")
            name = f"{first} {last}".strip()
            if name:
                authors.append(name)

    # Extract year from date field
    year = None
    date_str = data.get("date", "")
    if date_str:
        # Try to parse year (handle formats like "2023", "2023-01-01", "January 2023")
        import re
        year_match = re.search(r'\b(19|20)\d{2}\b', date_str)
        if year_match:
            year = int(year_match.group(0))

    # Extract tags
    tags = [tag.get("tag", "") for tag in data.get("tags", [])]
    tags = [t for t in tags if t]  # Remove empty strings

    # Extract collections (available in item metadata)
    collections = data.get("collections", [])

    return DocumentMetadata(
        library_id=item["library"]["id"],
        item_key=item["key"],
        title=data.get("title"),
        authors=authors,
        year=year,
        item_type=data.get("itemType"),
        attachment_key=attachment.get("key"),

        # NEW FIELDS
        publication_title=data.get("publicationTitle"),
        volume=data.get("volume"),
        issue=data.get("issue"),
        pages=data.get("pages"),
        publisher=data.get("publisher"),

        doi=data.get("DOI"),
        isbn=data.get("ISBN"),
        issn=data.get("ISSN"),
        url=data.get("url"),

        tags=tags,
        collections=collections,

        abstract=data.get("abstractNote"),
        language=data.get("language"),

        item_version=item.get("version"),
        attachment_version=attachment.get("version"),

        date_added=data.get("dateAdded"),
        date_modified=data.get("dateModified"),
    )
```

### 3. Update Vector Store Payload

**File:** [backend/db/vector_store.py](../../backend/db/vector_store.py)

Update `add_chunk()` and `add_chunks_batch()` to include new fields:

```python
def add_chunk(self, chunk: DocumentChunk) -> str:
    """Add a document chunk to the vector store."""
    if chunk.embedding is None:
        raise ValueError("Chunk must have an embedding")

    point_id = str(uuid.uuid4())
    doc_meta = chunk.metadata.document_metadata

    # Build payload with ALL metadata fields
    payload = {
        # EXISTING FIELDS (keep all)
        "text": chunk.text,
        "chunk_id": chunk.metadata.chunk_id,
        "library_id": doc_meta.library_id,
        "item_key": doc_meta.item_key,
        "attachment_key": doc_meta.attachment_key,
        "title": doc_meta.title,
        "authors": doc_meta.authors,
        "year": doc_meta.year,
        "page_number": chunk.metadata.page_number,
        "text_preview": chunk.metadata.text_preview,
        "chunk_index": chunk.metadata.chunk_index,
        "content_hash": chunk.metadata.content_hash,

        # NEW FIELDS
        "item_type": doc_meta.item_type,
        "publication_title": doc_meta.publication_title,
        "volume": doc_meta.volume,
        "issue": doc_meta.issue,
        "pages": doc_meta.pages,
        "publisher": doc_meta.publisher,
        "doi": doc_meta.doi,
        "isbn": doc_meta.isbn,
        "issn": doc_meta.issn,
        "url": doc_meta.url,
        "tags": doc_meta.tags,
        "collections": doc_meta.collections,
        "abstract": doc_meta.abstract,  # May be long, but useful for context
        "language": doc_meta.language,
        "item_version": doc_meta.item_version,
        "attachment_version": doc_meta.attachment_version,
        "date_added": doc_meta.date_added,
        "date_modified": doc_meta.date_modified,
    }

    point = PointStruct(
        id=point_id,
        vector=chunk.embedding,
        payload=payload,
    )

    self.client.upsert(
        collection_name=self.CHUNKS_COLLECTION,
        points=[point],
    )

    return point_id
```

### 4. Migration Strategy

**Option A: Soft Migration (Recommended)**

- New fields are optional (`Optional[...]` in Pydantic models)
- Old data continues to work (missing fields = `None`)
- Re-indexing gradually adds new metadata
- No forced migration required

**Option B: Hard Migration with Script**

Create migration script: `scripts/migrate_add_metadata.py`

```python
"""Migrate vector database to include new metadata fields."""

import asyncio
from backend.db.vector_store import VectorStore
from backend.zotero.local_api import ZoteroLocalAPI
from backend.config.settings import get_settings

async def migrate():
    settings = get_settings()
    vector_store = VectorStore(...)
    zotero_api = ZoteroLocalAPI()

    # Fetch all chunks from vector DB
    all_chunks = vector_store.client.scroll(
        collection_name=vector_store.CHUNKS_COLLECTION,
        limit=10000,  # Batch size
        with_payload=True,
        with_vectors=True,
    )

    # For each chunk, fetch fresh Zotero metadata
    for chunk in all_chunks:
        payload = chunk.payload
        item_key = payload["item_key"]
        library_id = payload["library_id"]

        # Fetch current item data from Zotero
        item = await zotero_api.get_item(library_id, item_key)
        if not item:
            continue

        # Extract new metadata
        new_metadata = _extract_new_fields(item)

        # Update payload
        payload.update(new_metadata)

        # Upsert back to vector DB with same ID
        vector_store.client.upsert(
            collection_name=vector_store.CHUNKS_COLLECTION,
            points=[PointStruct(
                id=chunk.id,
                vector=chunk.vector,
                payload=payload,
            )]
        )

    print("[PASS] Migration complete")

if __name__ == "__main__":
    asyncio.run(migrate())
```

**Recommendation:** Use **Option A** for simplicity. Users will get new metadata when they re-index libraries after update.

---

## Performance Considerations

### Storage Overhead

**Current Payload Size (estimated):**

```
text: ~500 chars × 2 bytes = 1 KB
metadata: ~200 bytes
Total per chunk: ~1.2 KB
```

**New Payload Size (estimated):**

```
text: ~500 chars × 2 bytes = 1 KB
metadata (enhanced): ~600 bytes
Total per chunk: ~1.6 KB
```

**Overhead:** +33% per chunk

**Impact on 10,000 chunks:**

- Current: ~12 MB
- Enhanced: ~16 MB
- Additional: ~4 MB (negligible)

**Conclusion:** Storage overhead is minimal (Qdrant handles this efficiently).

---

### Query Performance

**Qdrant Filtering Performance:**

- Qdrant is optimized for payload filtering
- Indexed fields enable fast lookups
- Combining vector search + filters is efficient

**Recommended Qdrant Indexes:**

```python
# Create indexes on frequently filtered fields
client.create_payload_index(
    collection_name="document_chunks",
    field_name="item_type",
    field_schema="keyword",
)

client.create_payload_index(
    collection_name="document_chunks",
    field_name="authors",
    field_schema="keyword",
)

client.create_payload_index(
    collection_name="document_chunks",
    field_name="year",
    field_schema="integer",
)

client.create_payload_index(
    collection_name="document_chunks",
    field_name="tags",
    field_schema="keyword",
)

client.create_payload_index(
    collection_name="document_chunks",
    field_name="publication_title",
    field_schema="keyword",
)
```

**Expected Query Time:**

- Pure semantic search: 10-50ms
- Semantic + metadata filter: 15-70ms
- Impact: +5-20ms (acceptable)

---

### Indexing Performance

**Additional Metadata Extraction Time:**

- Metadata is already fetched from Zotero API
- Extracting additional fields: +1-5ms per item (negligible)
- Parsing tags/collections: +1-2ms per item

**Total Impact:** <1% slowdown in indexing

---

## Privacy & Security

### Sensitive Metadata

**Potentially Sensitive Fields:**

- `abstract` - May reveal private research interests
- `tags` - User-created, may be sensitive
- `collections` - Organizational structure may be private

**Mitigation:**

- Keep data local-only (existing architecture)
- If cloud sync added later, encrypt payloads
- Provide opt-out for sensitive fields

### Abstract Storage

**Question:** Should we store full abstracts in vector DB?

**Pros:**

- Useful for LLM context (more metadata → better answers)
- Helps with filtering (semantic search on abstract)

**Cons:**

- Increases storage by ~500-1000 bytes/chunk
- Duplicate data (abstract same for all chunks of a paper)

**Recommendation:**

- Store abstracts (benefits outweigh costs)
- Consider deduplication optimization later if needed

---

## Alternatives Considered

### Alternative 1: Store Metadata Separately (Rejected)

**Approach:** Keep vector DB minimal, fetch metadata from Zotero on-demand after search.

**Pros:**

- Smaller vector DB
- Always fresh metadata

**Cons:**

- **No metadata filtering** (major drawback)
- Additional API calls during query (slower)
- Requires Zotero to be running

**Verdict:** Rejected - metadata filtering is core feature.

---

### Alternative 2: Metadata-Only Collection (Considered)

**Approach:** Create separate Qdrant collection for metadata, join results.

**Pros:**

- Cleaner separation
- Can index metadata without vectors

**Cons:**

- Complex join logic
- Two searches per query
- Harder to implement AND/OR filters

**Verdict:** Not necessary for current scale.

---

### Alternative 3: Embedding Metadata in Text (Rejected)

**Approach:** Prepend metadata to chunk text before embedding.

Example: `"[Author: John Doe] [Year: 2023] [Journal: Nature] Actual chunk text..."`

**Pros:**

- Metadata influences semantic search
- No separate filtering logic

**Cons:**

- Pollutes embeddings with structured data
- Hurts semantic search quality
- Can't filter precisely (fuzzy matches)
- Wastes embedding capacity

**Verdict:** Rejected - wrong approach for structured metadata.

---

### Alternative 4: Hybrid Metadata + Text Embeddings (Future Enhancement)

**Approach:** Generate separate embeddings for metadata and text, combine scores.

**Formula:** `final_score = α × text_similarity + β × metadata_similarity`

**Pros:**

- Metadata contributes to ranking
- Flexible weighting

**Cons:**

- Complex implementation
- Requires tuning α, β
- Double embedding cost

**Verdict:** Interesting for v2.0, overkill for v1.0.

---

## Implementation Roadmap

### Phase 1: Core Metadata Enhancement (1-2 weeks)

**Tasks:**

1. **Update Data Models** (1 day)
   - Extend `DocumentMetadata` in [backend/models/document.py](../../backend/models/document.py)
   - Add new fields with `Optional` types
   - Update Pydantic validation

2. **Enhance Metadata Extraction** (2 days)
   - Update `document_processor.py` to extract all new fields
   - Add tag, collection, identifier parsing
   - Add date/year parsing logic
   - Write unit tests

3. **Update Vector Store** (2 days)
   - Modify `add_chunk()` to include new fields
   - Modify `add_chunks_batch()` similarly
   - Update `search()` to handle metadata in payloads
   - Ensure backward compatibility with old data

4. **Testing** (2 days)
   - Unit tests for metadata extraction
   - Integration tests with real Zotero items
   - Verify old data still works

**Deliverable:** Metadata is captured and stored in vector DB

---

### Phase 2: Metadata Filtering (1-2 weeks)

**Tasks:**

1. **Design Filtering API** (1 day)
   - Define `MetadataFilter` model
   - Design filter composition logic (AND/OR)
   - Plan Qdrant filter translation

2. **Implement Qdrant Filtering** (3 days)
   - Implement `_build_qdrant_filter()` method
   - Add support for all filter types:
     - Author filtering
     - Publication filtering
     - Year range filtering
     - Tag filtering
     - Item type filtering
   - Handle edge cases (empty filters, None values)

3. **Create Qdrant Indexes** (1 day)
   - Add payload indexes for performance
   - Test query speed with/without indexes
   - Document recommended indexes

4. **Update RAG Engine** (2 days)
   - Modify `RAGEngine.query()` to accept filters
   - Pass filters to vector store
   - Update query prompt to include metadata context

5. **Testing** (2 days)
   - Unit tests for filter building
   - Integration tests with various filter combinations
   - Performance benchmarking

**Deliverable:** Metadata filtering works in backend

---

### Phase 3: API & UI (1 week)

**Tasks:**

1. **Update REST API** (2 days)
   - Extend `/api/query` endpoint with filter parameters
   - Add filter validation
   - Update API documentation

2. **Update Plugin UI** (3 days)
   - Add metadata filter UI in dialog
   - Author autocomplete (from indexed items)
   - Tag selector
   - Year range picker
   - Item type checkboxes
   - Update dialog.js to pass filters to backend

3. **Testing** (2 days)
   - End-to-end testing with plugin
   - Test various filter combinations
   - UI/UX validation

**Deliverable:** Full metadata filtering available to users

---

### Phase 4: Polish & Documentation (3-5 days)

**Tasks:**

1. **Performance Optimization** (2 days)
   - Profile query performance
   - Add caching if needed
   - Optimize Qdrant filter building

2. **Documentation** (2 days)
   - Update architecture.md
   - Add filtering examples to user guide
   - Document API endpoints
   - Add migration guide (if needed)

3. **User Feedback** (1 day)
   - Manual testing
   - Gather feedback on filter UX
   - Iterate on design

**Deliverable:** Production-ready metadata-enhanced retrieval

---

### Total Estimated Time: 3-5 weeks

**Breakdown:**

- Phase 1: 1-2 weeks (backend metadata capture)
- Phase 2: 1-2 weeks (filtering logic)
- Phase 3: 1 week (API & UI)
- Phase 4: 3-5 days (polish)

**Dependencies:**

- Requires incremental indexing (from previous document) for version tracking
- Should be done before cloud sync (if implemented)

---

## Open Questions

### 1. Abstract Storage Strategy

**Question:** Should we store the full abstract in every chunk's payload?

**Options:**

- **A:** Store in every chunk (current approach)
  - Pro: Available in search results
  - Con: Redundant storage (~500-1000 bytes × chunks_per_paper)
- **B:** Store in first chunk only
  - Pro: Saves storage
  - Con: Complex logic to fetch abstract from related chunks
- **C:** Don't store abstract
  - Pro: Minimal storage
  - Con: Can't use abstract in LLM context

**Recommendation Needed:** Preference?

---

### 2. Collection Storage

**Question:** Store collection IDs or collection names?

**Options:**

- **A:** Store collection IDs (opaque strings like "ABC123")
  - Pro: Stable identifiers
  - Con: Need to resolve to names for display
- **B:** Store collection names (human-readable like "Machine Learning")
  - Pro: Easier to filter and display
  - Con: May change if user renames collection
- **C:** Store both
  - Pro: Best of both worlds
  - Con: More storage

**Current Zotero API:** Returns collection IDs only in item data

**Recommendation Needed:** Fetch collection names during indexing?

---

### 3. Metadata in LLM Context

**Question:** Should we include metadata in the context sent to the LLM?

**Current Approach:** Only chunk text is sent to LLM

**Enhanced Approach:**

```
Context:
1. [Author: John Doe, Year: 2023, Publication: Nature, Page: 15]
   "Chunk text here..."

2. [Author: Jane Smith, Year: 2024, Publication: Science, Page: 42]
   "Another chunk text..."
```

**Pros:**

- LLM can reference authors/publications in answer
- Better citation formatting
- Helps disambiguation (multiple authors with same ideas)

**Cons:**

- Uses more context tokens (~50-100 tokens per chunk)
- May confuse LLM with structured data

**Recommendation Needed:** Include metadata in context or not?

---

### 4. Filter UI Complexity

**Question:** How much filtering UI should we expose in the plugin?

**Options:**

- **A:** Simple filters (author, year, tags)
  - Pro: Easy to implement, not overwhelming
  - Con: Limits advanced queries
- **B:** Advanced filters (all metadata fields)
  - Pro: Maximum flexibility
  - Con: Complex UI, may confuse users
- **C:** Two modes (simple + advanced)
  - Pro: Best UX for both novice and power users
  - Con: More development effort

**Recommendation Needed:** Which approach?

---

### 5. Filter Persistence

**Question:** Should we save user's filter preferences?

**Options:**

- **A:** Save per session (lost on restart)
- **B:** Save to Zotero preferences (persistent)
- **C:** Save as named filter profiles (like "Recent ML Papers")

**Recommendation Needed:** Persistence strategy?

---

### 6. Natural Language Filter Parsing

**Question:** Should we support natural language filter extraction?

**Example:** User asks "What did papers from 2023 say about climate change?"

**System could:**

1. Detect year mention → set `filter_year_min=2023, filter_year_max=2023`
2. Semantic search on "climate change"

**Pros:**

- More natural user experience
- No manual filter selection needed

**Cons:**

- Requires NLP parsing (regex or LLM)
- May misinterpret queries
- Adds latency

**Recommendation Needed:** Implement automatic filter extraction or keep manual?

---

### 7. Zotero Local API Limitations

**Question:** Does Zotero Local API provide all needed metadata?

**Known Limitations:**

- Collection names may need separate API call
- Tag data should be available in item data
- Version numbers available

**Action Needed:** Validate that all desired fields are accessible via Local API

---

### 8. Indexing Existing Libraries

**Question:** How to handle partial re-indexing for metadata?

**Scenario:** User has 1000 items already indexed (without new metadata)

**Options:**

- **A:** Force full re-index
  - Pro: Clean, all data fresh
  - Con: Time-consuming
- **B:** Background metadata refresh
  - Pro: Non-disruptive
  - Con: Complex implementation
- **C:** On-demand enrichment (fetch metadata when searched)
  - Pro: Lazy loading
  - Con: Inconsistent data, slower queries

**Recommendation Needed:** Migration strategy for existing users?

---

## Conclusion

### Summary

Adding rich metadata to chunk embeddings will significantly enhance the Zotero RAG system's capabilities, enabling sophisticated queries that combine semantic search with structured filtering. The implementation is straightforward, storage overhead is minimal, and performance impact is acceptable.

### Recommended Approach

1. **Extend `DocumentMetadata` model** with ~15-20 new fields
2. **Update indexing pipeline** to extract metadata from Zotero items
3. **Enhance vector store** to include metadata in payloads
4. **Implement flexible filtering API** using Qdrant's filter system
5. **Update REST API and plugin UI** to expose filtering capabilities
6. **Use soft migration** (optional fields, backward compatible)

### Key Benefits

- Enable author, publication, year, tag, and type filtering
- Support complex multi-filter queries
- Better LLM context with metadata
- Minimal performance impact
- Backward compatible with existing data

### Next Steps

1. Review this document and answer open questions
2. Prioritize features (core vs. nice-to-have)
3. Begin Phase 1 implementation (metadata capture)
4. Iterate based on user feedback

---

## References

- [Architecture Documentation](../architecture.md)
- [Indexing and Embedding Strategies](indexing-and-embedding-strategies.md)
- [Zotero API Documentation](https://www.zotero.org/support/dev/web_api/v3/basics)
- [Qdrant Filtering Documentation](https://qdrant.tech/documentation/concepts/filtering/)
- [Current Vector Store Implementation](../../backend/db/vector_store.py)
- [Current Data Models](../../backend/models/document.py)

---

**Document Version:** 1.0
**Last Updated:** January 2025
**Status:** Proposal - Awaiting Review
