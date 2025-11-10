"""
Real integration tests with live dependencies.

These tests validate the complete system with:
- Real Zotero instance (test group 6297749)
- Real embedding models
- Real LLM service (KISSKI Academic Cloud)
- Actual vector database persistence

Industry best practices implemented:
1. Environment-based test gating (pytest.mark.integration)
2. Dedicated test configuration (.env.test)
3. Test data cleanup and isolation
4. Reproducible test collections
5. Resource availability checks
"""

import os
import asyncio
import tempfile
import shutil
from pathlib import Path
import pytest
from typing import Optional

from backend.services.document_processor import DocumentProcessor
from backend.services.rag_engine import RAGEngine
from backend.services.embeddings import create_embedding_service
from backend.services.llm import create_llm_service
from backend.db.vector_store import VectorStore
from backend.zotero.local_api import ZoteroLocalAPI
from backend.config.settings import Settings
from backend.config.presets import get_preset


# ============================================================================
# Test Configuration
# ============================================================================

# Test group URL: https://www.zotero.org/groups/6297749/test-rag-plugin
TEST_LIBRARY_ID = "6297749"
TEST_LIBRARY_TYPE = "group"

# Expected test data characteristics (update when test data changes)
EXPECTED_MIN_ITEMS = 5  # Minimum PDFs in test collection
EXPECTED_MIN_CHUNKS = 50  # Minimum chunks expected from indexing


# ============================================================================
# Pytest Markers and Fixtures
# ============================================================================

# Mark tests that require real external services
pytestmark = pytest.mark.integration


@pytest.fixture
def integration_config():
    """
    Load configuration for integration tests.

    Uses MODEL_PRESET environment variable (default: remote-kisski).
    Expects KISSKI_API_KEY in environment for remote-kisski preset.
    """
    preset_name = os.getenv("MODEL_PRESET", "remote-kisski")
    preset = get_preset(preset_name)

    if preset is None:
        pytest.skip(f"Preset '{preset_name}' not found")

    return preset


@pytest.fixture
def integration_settings(integration_config):
    """Create settings instance for integration tests."""
    settings = Settings(model_preset=integration_config.name)
    return settings


@pytest.fixture
async def temp_vector_store(integration_config):
    """
    Create temporary vector store for test isolation.

    Each test gets a fresh vector store that is cleaned up after the test.
    """
    temp_dir = tempfile.mkdtemp(prefix="zotero_rag_test_")
    vector_store_path = Path(temp_dir) / "test_vector_store"

    # Create embedding service to get dimension
    # Extract API key from model_kwargs if present
    api_key = None
    if "api_key_env" in integration_config.embedding.model_kwargs:
        api_key_env = integration_config.embedding.model_kwargs["api_key_env"]
        api_key = os.getenv(api_key_env)

    embedding_service = create_embedding_service(
        config=integration_config.embedding,
        api_key=api_key
    )
    embedding_dim = embedding_service.get_embedding_dim()

    # Create vector store
    vector_store = VectorStore(
        storage_path=vector_store_path,
        embedding_dim=embedding_dim
    )

    yield vector_store

    # Cleanup: Close Qdrant client first to release file locks
    try:
        if hasattr(vector_store, 'client') and vector_store.client:
            vector_store.client.close()
    except Exception:
        pass  # Ignore errors during client close

    # Remove temporary directory (ignore Windows file lock errors)
    if Path(temp_dir).exists():
        try:
            shutil.rmtree(temp_dir)
        except PermissionError:
            # Windows file locking issue - not critical, ignore
            pass


@pytest.fixture
async def zotero_client():
    """
    Create Zotero client and verify connectivity.

    Skips tests if Zotero is not running.
    """
    client = ZoteroLocalAPI()

    # Check if Zotero is running and accessible
    try:
        libraries = await client.list_libraries()
        if not libraries:
            pytest.skip("No Zotero libraries found - is Zotero running?")
    except Exception as e:
        pytest.skip(f"Cannot connect to Zotero: {e}")

    return client


@pytest.fixture
async def llm_service(integration_settings):
    """
    Create LLM service with API key validation.

    Skips tests if API key is not available.
    """
    # Check for required API key
    preset = integration_settings.get_hardware_preset()
    if preset.llm.model_type == "remote":
        api_key_env = preset.llm.model_kwargs.get("api_key_env", "KISSKI_API_KEY")
        api_key = integration_settings.get_api_key(api_key_env)

        if not api_key:
            pytest.skip(f"API key not found in environment: {api_key_env}")

    # Create service
    service = create_llm_service(settings=integration_settings)
    return service


# ============================================================================
# Health Check Tests
# ============================================================================

@pytest.mark.asyncio
async def test_zotero_connectivity(zotero_client):
    """
    Verify Zotero is running and test library is accessible.

    This is a smoke test to validate the test environment.
    """
    # Get all libraries
    libraries = await zotero_client.list_libraries()

    assert libraries is not None
    assert len(libraries) > 0

    # Check if test library is accessible
    library_ids = [lib["id"] for lib in libraries]

    # Note: Test library may not be in user's Zotero if not synced
    # This is informational, not a hard requirement
    if TEST_LIBRARY_ID not in library_ids:
        pytest.skip(
            f"Test library {TEST_LIBRARY_ID} not found. "
            "Please sync test group: https://www.zotero.org/groups/6297749/test-rag-plugin"
        )


@pytest.mark.asyncio
async def test_embedding_service(integration_config):
    """Verify embedding service can generate embeddings."""
    # Extract API key if present
    api_key = None
    if "api_key_env" in integration_config.embedding.model_kwargs:
        api_key_env = integration_config.embedding.model_kwargs["api_key_env"]
        api_key = os.getenv(api_key_env)

    service = create_embedding_service(
        config=integration_config.embedding,
        api_key=api_key
    )

    test_text = "This is a test document for embedding generation."
    embedding = await service.embed_text(test_text)

    assert embedding is not None
    assert len(embedding) == service.get_embedding_dim()
    assert all(isinstance(x, float) for x in embedding)


@pytest.mark.asyncio
async def test_llm_service_connectivity(llm_service):
    """Verify LLM service can generate text."""
    test_prompt = "Say 'hello' and nothing else."

    response = await llm_service.generate(
        prompt=test_prompt,
        max_tokens=10,
        temperature=0.0
    )

    assert response is not None
    assert len(response) > 0
    assert isinstance(response, str)


# ============================================================================
# Indexing Integration Tests
# ============================================================================

@pytest.mark.asyncio
async def test_index_real_library(
    zotero_client,
    temp_vector_store,
    integration_config
):
    """
    Test indexing with real Zotero test group.

    This test:
    1. Fetches real items from test library
    2. Downloads actual PDFs
    3. Extracts text with page tracking
    4. Generates real embeddings
    5. Stores in vector database

    Expected duration: 1-5 minutes depending on library size and model.
    """
    # Create services
    embedding_service = LocalEmbeddingService(config=integration_config.embedding)

    document_processor = DocumentProcessor(
        zotero_client=zotero_client,
        embedding_service=embedding_service,
        vector_store=temp_vector_store,
        max_chunk_size=512,
        chunk_overlap=50,
    )

    # Track progress
    progress_updates = []
    def track_progress(current: int, total: int):
        progress_updates.append((current, total))
        print(f"  Progress: {current}/{total} items processed")

    # Index the library
    print(f"\nIndexing library {TEST_LIBRARY_ID}...")
    result = await document_processor.index_library(
        library_id=TEST_LIBRARY_ID,
        library_type=TEST_LIBRARY_TYPE,
        force_reindex=True,  # Always reindex for test
        progress_callback=track_progress
    )

    # Validate results
    print(f"\nIndexing results: {result}")

    assert result["status"] == "completed"
    assert result["library_id"] == TEST_LIBRARY_ID
    assert result["items_processed"] >= EXPECTED_MIN_ITEMS, \
        f"Expected at least {EXPECTED_MIN_ITEMS} items, got {result['items_processed']}"
    assert result["chunks_created"] >= EXPECTED_MIN_CHUNKS, \
        f"Expected at least {EXPECTED_MIN_CHUNKS} chunks, got {result['chunks_created']}"

    # Validate progress tracking
    assert len(progress_updates) > 0, "Progress callback was not invoked"

    # Validate vector store
    info = temp_vector_store.get_collection_info()
    assert info["chunks_count"] >= EXPECTED_MIN_CHUNKS

    print(f"\n✅ Successfully indexed {result['items_processed']} items, "
          f"created {result['chunks_created']} chunks")


@pytest.mark.asyncio
async def test_incremental_indexing(
    zotero_client,
    temp_vector_store,
    integration_config
):
    """
    Test incremental indexing (deduplication).

    Validates that re-indexing the same library skips duplicates.
    """
    embedding_service = LocalEmbeddingService(config=integration_config.embedding)

    document_processor = DocumentProcessor(
        zotero_client=zotero_client,
        embedding_service=embedding_service,
        vector_store=temp_vector_store,
        max_chunk_size=512,
        chunk_overlap=50,
    )

    # First indexing
    print("\n1st indexing (full)...")
    result1 = await document_processor.index_library(
        library_id=TEST_LIBRARY_ID,
        library_type=TEST_LIBRARY_TYPE,
        force_reindex=True,
    )

    chunks_first = result1["chunks_created"]

    # Second indexing without force_reindex (should skip duplicates)
    print("\n2nd indexing (incremental)...")
    result2 = await document_processor.index_library(
        library_id=TEST_LIBRARY_ID,
        library_type=TEST_LIBRARY_TYPE,
        force_reindex=False,
    )

    # Should have skipped duplicates
    assert result2["duplicates_skipped"] > 0, \
        "Expected duplicates to be skipped on second indexing"
    assert result2["chunks_created"] < chunks_first, \
        "Expected fewer chunks created on incremental indexing"

    print(f"\n✅ Incremental indexing working: "
          f"{result2['duplicates_skipped']} duplicates skipped")


# ============================================================================
# RAG Query Integration Tests
# ============================================================================

@pytest.mark.asyncio
async def test_rag_query_end_to_end(
    zotero_client,
    temp_vector_store,
    integration_config,
    llm_service
):
    """
    Test complete RAG pipeline with real data and real LLM.

    This is the most comprehensive integration test:
    1. Index real library
    2. Query with real embedding model
    3. Retrieve from vector store
    4. Generate answer with real LLM
    5. Validate source citations

    Expected duration: 2-10 minutes depending on models.
    """
    # Setup: Index the library
    embedding_service = LocalEmbeddingService(config=integration_config.embedding)

    document_processor = DocumentProcessor(
        zotero_client=zotero_client,
        embedding_service=embedding_service,
        vector_store=temp_vector_store,
        max_chunk_size=512,
        chunk_overlap=50,
    )

    print(f"\nIndexing library {TEST_LIBRARY_ID} for RAG test...")
    index_result = await document_processor.index_library(
        library_id=TEST_LIBRARY_ID,
        library_type=TEST_LIBRARY_TYPE,
        force_reindex=True,
    )

    print(f"Indexed {index_result['items_processed']} items, "
          f"{index_result['chunks_created']} chunks")

    # Create RAG engine
    rag_engine = RAGEngine(
        embedding_service=embedding_service,
        llm_service=llm_service,
        vector_store=temp_vector_store,
    )

    # Test query - customize based on your test collection content
    test_question = "What are the main topics covered in these documents?"

    print(f"\nQuerying: '{test_question}'")

    # Execute query
    result = await rag_engine.query(
        question=test_question,
        library_ids=[TEST_LIBRARY_ID],
        top_k=5,
        min_score=0.3,
    )

    # Validate results
    print(f"\nAnswer: {result.answer[:200]}...")
    print(f"Sources: {len(result.sources)} citations")

    assert result.answer is not None
    assert len(result.answer) > 50, "Answer seems too short"

    assert len(result.sources) > 0, "No sources cited"

    # Validate source structure
    for source in result.sources:
        assert source.item_id is not None
        assert source.title is not None
        assert source.score > 0

        # Check page number or text anchor
        assert source.page_number is not None or source.text_anchor is not None

        print(f"  - {source.title} (p. {source.page_number}, score: {source.score:.3f})")

    print(f"\n✅ RAG query successful with {len(result.sources)} sources")


@pytest.mark.asyncio
async def test_multi_query_consistency(
    zotero_client,
    temp_vector_store,
    integration_config,
    llm_service
):
    """
    Test consistency across multiple queries.

    Validates that the system produces reasonable results for different questions.
    """
    # Setup: Index once
    embedding_service = LocalEmbeddingService(config=integration_config.embedding)

    document_processor = DocumentProcessor(
        zotero_client=zotero_client,
        embedding_service=embedding_service,
        vector_store=temp_vector_store,
        max_chunk_size=512,
        chunk_overlap=50,
    )

    await document_processor.index_library(
        library_id=TEST_LIBRARY_ID,
        library_type=TEST_LIBRARY_TYPE,
        force_reindex=True,
    )

    rag_engine = RAGEngine(
        embedding_service=embedding_service,
        llm_service=llm_service,
        vector_store=temp_vector_store,
    )

    # Test multiple queries
    test_questions = [
        "What methodologies are discussed?",
        "What are the key findings?",
        "Who are the main authors cited?",
    ]

    print("\nTesting multiple queries...")

    for question in test_questions:
        print(f"\nQ: {question}")

        result = await rag_engine.query(
            question=question,
            library_ids=[TEST_LIBRARY_ID],
            top_k=3,
            min_score=0.3,
        )

        assert result.answer is not None
        assert len(result.answer) > 20

        print(f"A: {result.answer[:100]}...")
        print(f"   {len(result.sources)} sources")

    print("\n✅ All queries returned valid results")


# ============================================================================
# Performance and Stress Tests
# ============================================================================

@pytest.mark.asyncio
@pytest.mark.slow
async def test_large_batch_indexing(
    zotero_client,
    temp_vector_store,
    integration_config
):
    """
    Test indexing performance with full library.

    Marked as 'slow' - run with: pytest -m slow
    """
    pytest.skip("Slow test - enable manually for performance testing")

    # This would test with larger libraries
    # Useful for validating memory usage and performance


# ============================================================================
# Error Handling Tests
# ============================================================================

@pytest.mark.asyncio
async def test_invalid_library_id(
    zotero_client,
    temp_vector_store,
    integration_config
):
    """Test handling of invalid library ID."""
    embedding_service = LocalEmbeddingService(config=integration_config.embedding)

    document_processor = DocumentProcessor(
        zotero_client=zotero_client,
        embedding_service=embedding_service,
        vector_store=temp_vector_store,
    )

    # Try to index non-existent library
    result = await document_processor.index_library(
        library_id="99999999",
        library_type="group",
        force_reindex=True,
    )

    # Should handle gracefully
    assert result["status"] in ["completed", "error"]
    assert result["items_processed"] == 0


@pytest.mark.asyncio
async def test_query_unindexed_library(
    temp_vector_store,
    integration_config,
    llm_service
):
    """Test querying a library that hasn't been indexed."""
    embedding_service = LocalEmbeddingService(config=integration_config.embedding)

    rag_engine = RAGEngine(
        embedding_service=embedding_service,
        llm_service=llm_service,
        vector_store=temp_vector_store,
    )

    # Query without indexing first
    result = await rag_engine.query(
        question="What is this about?",
        library_ids=["nonexistent_library"],
        top_k=5,
    )

    # Should handle gracefully with no sources
    assert result.answer is not None
    # May have empty sources or fallback message


# ============================================================================
# Test Runner Configuration
# ============================================================================

if __name__ == "__main__":
    """
    Run integration tests directly.

    Usage:
        # Set up environment
        export MODEL_PRESET=remote-kisski
        export KISSKI_API_KEY=your_key_here

        # Run all integration tests
        uv run pytest backend/tests/test_real_integration.py -v -s

        # Run specific test
        uv run pytest backend/tests/test_real_integration.py::test_rag_query_end_to_end -v -s

        # Run with coverage
        uv run pytest backend/tests/test_real_integration.py --cov=backend -v -s
    """
    pytest.main([__file__, "-v", "-s"])
