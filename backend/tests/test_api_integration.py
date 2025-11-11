"""
API Integration Tests - Testing FastAPI endpoints with real backend server.

These tests validate the complete API by making HTTP requests to a running
FastAPI backend server. They test the full request-response cycle including:
- Request validation
- Service orchestration
- Response formatting
- Error handling

Prerequisites:
- Backend server running (npm run server:start)
- Zotero desktop running with test library synced
- API keys configured (e.g., KISSKI_API_KEY)
- Valid MODEL_PRESET configured

Run these tests with:
    npm run test:api
    # or
    uv run pytest -m api -v -s
"""

import pytest
import httpx
import asyncio
import json
from typing import Optional
from pathlib import Path
import tempfile
import shutil

from backend.tests.conftest import (
    get_test_library_id,
    get_backend_base_url,
    get_expected_min_items,
)


# ============================================================================
# Test Configuration
# ============================================================================

@pytest.fixture(scope="module")
def base_url() -> str:
    """Get the backend server base URL."""
    return get_backend_base_url()


@pytest.fixture(scope="module")
def test_library_id() -> str:
    """Get the test library ID."""
    return get_test_library_id()


# ============================================================================
# Health & Configuration Tests
# ============================================================================

@pytest.mark.api
@pytest.mark.asyncio
async def test_health_endpoint(api_environment_validated, base_url: str):
    """Test the health check endpoint."""
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{base_url}/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"


@pytest.mark.api
@pytest.mark.asyncio
async def test_root_endpoint(api_environment_validated, base_url: str):
    """Test the root endpoint."""
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{base_url}/")

        assert response.status_code == 200
        data = response.json()
        assert data["service"] == "Zotero RAG API"
        assert data["status"] == "running"
        assert "version" in data


@pytest.mark.api
@pytest.mark.asyncio
async def test_get_config(api_environment_validated, base_url: str):
    """Test GET /api/config endpoint."""
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{base_url}/api/config")

        assert response.status_code == 200
        data = response.json()

        # Validate response structure (actual API response fields)
        assert "preset_name" in data
        assert "api_version" in data
        assert "embedding_model" in data
        assert "llm_model" in data
        assert "vector_db_path" in data
        assert "model_cache_dir" in data
        assert "available_presets" in data

        # Validate types
        assert isinstance(data["available_presets"], list)
        assert len(data["available_presets"]) > 0


@pytest.mark.api
@pytest.mark.asyncio
async def test_get_version(api_environment_validated, base_url: str):
    """Test GET /api/version endpoint."""
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{base_url}/api/version")

        assert response.status_code == 200
        data = response.json()

        # Actual API response fields
        assert "api_version" in data
        assert "service" in data
        assert isinstance(data["api_version"], str)
        assert data["service"] == "Zotero RAG API"


# ============================================================================
# Library Management Tests
# ============================================================================

@pytest.mark.api
@pytest.mark.asyncio
async def test_list_libraries(api_environment_validated, base_url: str, test_library_id: str):
    """Test GET /api/libraries endpoint."""
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{base_url}/api/libraries")

        if response.status_code != 200:
            print(f"\n[ERROR] Response status: {response.status_code}")
            print(f"[ERROR] Response body: {response.text}")

        assert response.status_code == 200
        libraries = response.json()

        # Should be a list
        assert isinstance(libraries, list)
        assert len(libraries) > 0

        # Should contain test library (API returns library_id not id)
        library_ids = [lib["library_id"] for lib in libraries]
        assert test_library_id in library_ids

        # Validate library structure (LibraryInfo model fields)
        test_lib = next(lib for lib in libraries if lib["library_id"] == test_library_id)
        assert "library_id" in test_lib
        assert "name" in test_lib
        assert "type" in test_lib
        assert "version" in test_lib


@pytest.mark.api
@pytest.mark.asyncio
async def test_library_status_before_indexing(
    api_environment_validated,
    base_url: str,
    test_library_id: str
):
    """Test GET /api/libraries/{id}/status before indexing."""
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{base_url}/api/libraries/{test_library_id}/status")

        assert response.status_code == 200
        data = response.json()

        # Validate response structure (LibraryStatusResponse model fields)
        assert "library_id" in data
        assert "indexed" in data
        assert "total_items" in data  # Not item_count
        assert "indexed_items" in data
        assert "last_indexed" in data

        assert data["library_id"] == test_library_id
        assert isinstance(data["indexed"], bool)
        # These can be null initially
        assert data["total_items"] is None or isinstance(data["total_items"], int)
        assert data["indexed_items"] is None or isinstance(data["indexed_items"], int)


# ============================================================================
# Indexing Tests
# ============================================================================

@pytest.mark.api
@pytest.mark.asyncio
@pytest.mark.slow
async def test_index_library(api_environment_validated, base_url: str, test_library_id: str):
    """Test POST /api/index/library/{id} endpoint and monitor via SSE.

    This test triggers library indexing and monitors progress via Server-Sent Events (SSE).
    It validates that:
    1. Indexing can be triggered successfully
    2. SSE progress events are emitted correctly
    3. Indexing completes without errors
    """
    # Use longer timeout for both client and stream (10 minutes for large libraries)
    timeout = httpx.Timeout(600.0, connect=10.0)

    async with httpx.AsyncClient(timeout=timeout) as client:
        # Trigger indexing with max_items limit for faster testing
        print(f"\n[INFO] Triggering indexing for library {test_library_id} (limited to 5 items)")
        response = await client.post(
            f"{base_url}/api/index/library/{test_library_id}",
            params={"max_items": 5}
        )

        # If already indexing (409), that's okay - just monitor the existing job
        if response.status_code == 409:
            print(f"[INFO] Library {test_library_id} is already being indexed")
        else:
            if response.status_code != 200:
                print(f"\n[ERROR] Response status: {response.status_code}")
                print(f"[ERROR] Response body: {response.text}")

            assert response.status_code == 200
            data = response.json()

            # Validate response
            assert data["status"] == "started"
            assert data["library_id"] == test_library_id
            print(f"[INFO] Indexing started for library {test_library_id}")

        # Monitor progress via SSE endpoint
        print("[INFO] Monitoring indexing progress via SSE...")

        async with client.stream(
            'GET',
            f"{base_url}/api/index/library/{test_library_id}/progress",
            timeout=timeout
        ) as sse_response:
            assert sse_response.status_code == 200
            assert "text/event-stream" in sse_response.headers.get("content-type", "")

            completed = False
            last_progress = 0

            async for line in sse_response.aiter_lines():
                if not line or line.startswith(':'):
                    continue

                if line.startswith('data: '):
                    data_str = line[6:]  # Remove 'data: ' prefix
                    try:
                        event_data = json.loads(data_str)
                        event_type = event_data.get('event', '')
                        message = event_data.get('message', '')
                        progress = event_data.get('progress', 0)

                        if event_type == 'started':
                            print(f"  [STARTED] {message}")
                        elif event_type == 'progress':
                            # Print every progress update to show activity
                            if progress != last_progress or (progress % 10 == 0):
                                current = event_data.get('current_item', 0)
                                total = event_data.get('total_items', 0)
                                print(f"  [PROGRESS] {progress:.1f}% - {message} ({current}/{total})")
                                last_progress = progress
                        elif event_type == 'completed':
                            print(f"  [COMPLETED] {message}")
                            completed = True
                            break
                        elif event_type == 'error':
                            print(f"  [ERROR] {message}")
                            pytest.fail(f"Indexing failed: {message}")
                    except json.JSONDecodeError:
                        continue

            assert completed, "Indexing did not complete (SSE stream ended without 'completed' event)"
            print(f"[PASS] Indexing completed successfully")




# ============================================================================
# Query Tests
# ============================================================================

@pytest.mark.api
@pytest.mark.asyncio
@pytest.mark.slow
async def test_query_library(api_environment_validated, base_url: str, test_library_id: str):
    """Test POST /api/query endpoint with indexed library."""
    # Use longer timeout for indexing and queries (10 minutes)
    timeout = httpx.Timeout(600.0, connect=10.0)

    # Helper function to trigger indexing
    async def trigger_indexing(client):
        """Trigger library indexing and wait for completion."""
        print(f"\n[INFO] Triggering indexing (limited to 5 items)...")
        index_response = await client.post(
            f"{base_url}/api/index/library/{test_library_id}",
            params={"max_items": 5, "force_reindex": True}
        )
        assert index_response.status_code == 200

        # Monitor progress via SSE
        print("[INFO] Monitoring indexing progress via SSE...")
        async with client.stream(
            'GET',
            f"{base_url}/api/index/library/{test_library_id}/progress",
            timeout=timeout
        ) as sse_response:
            assert sse_response.status_code == 200

            completed = False
            async for line in sse_response.aiter_lines():
                if not line or line.startswith(':'):
                    continue

                if line.startswith('data: '):
                    data_str = line[6:]
                    try:
                        event_data = json.loads(data_str)
                        event_type = event_data.get('event', '')

                        if event_type == 'completed':
                            print(f"  [COMPLETED] Indexing done")
                            completed = True
                            break
                        elif event_type == 'error':
                            pytest.fail(f"Indexing failed: {event_data.get('message', '')}")
                    except json.JSONDecodeError:
                        continue

            assert completed, "Indexing did not complete before query test"

    # Ensure library is indexed with queryable data
    async with httpx.AsyncClient(timeout=timeout) as client:
        # Check if already indexed
        status_response = await client.get(
            f"{base_url}/api/libraries/{test_library_id}/status"
        )
        status_data = status_response.json()

        # If not indexed at all, index now
        if not status_data["indexed"] or status_data.get("indexed_items", 0) == 0:
            await trigger_indexing(client)
        else:
            # Library appears indexed - verify we have queryable chunks
            print(f"\n[INFO] Library indexed with {status_data.get('indexed_items', 0)} items")
            print("[INFO] Verifying we have queryable data...")

            # Try a simple query to check if we have chunks
            test_query_payload = {
                "question": "test",
                "library_ids": [test_library_id],
                "top_k": 1,
                "min_score": 0.0
            }

            test_response = await client.post(
                f"{base_url}/api/query",
                json=test_query_payload,
                timeout=300.0
            )

            if test_response.status_code == 200:
                test_data = test_response.json()
                if len(test_data.get("sources", [])) == 0:
                    # No chunks available - need to reindex
                    print("[INFO] No queryable chunks found, reindexing...")
                    await trigger_indexing(client)
                else:
                    print(f"[INFO] Found {len(test_data['sources'])} queryable chunks, skipping indexing")
            else:
                # Query failed - reindex to be safe
                print("[INFO] Query test failed, reindexing...")
                await trigger_indexing(client)

        # Now test the query endpoint
        query_payload = {
            "question": "What topics are covered in these documents?",
            "library_ids": [test_library_id],
            "top_k": 5,
            "min_score": 0.3
        }

        response = await client.post(
            f"{base_url}/api/query",
            json=query_payload,
            timeout=300.0  # Increased timeout for model loading on first query
        )

        if response.status_code != 200:
            print(f"\n[ERROR] Query failed with status {response.status_code}")
            print(f"[ERROR] Response: {response.text}")
        assert response.status_code == 200
        data = response.json()

        # Validate response structure
        assert "question" in data
        assert "answer" in data
        assert "sources" in data
        assert "library_ids" in data

        assert data["question"] == query_payload["question"]
        assert isinstance(data["answer"], str)
        assert len(data["answer"]) > 0
        assert isinstance(data["sources"], list)

        # Validate source citations
        if len(data["sources"]) > 0:
            source = data["sources"][0]
            assert "item_id" in source
            assert "title" in source
            assert "relevance_score" in source
            # Optional fields
            assert "page_number" in source or source.get("page_number") is None
            assert "text_anchor" in source or source.get("text_anchor") is None

        print(f"\n[PASS] Query returned answer with {len(data['sources'])} sources")
        print(f"  Answer length: {len(data['answer'])} characters")


@pytest.mark.api
@pytest.mark.asyncio
async def test_query_validation_errors(api_environment_validated, base_url: str):
    """Test query endpoint validation errors."""
    async with httpx.AsyncClient() as client:
        # Test empty question
        response = await client.post(
            f"{base_url}/api/query",
            json={
                "question": "",
                "library_ids": ["6297749"]
            }
        )
        assert response.status_code == 400

        # Test empty library list
        response = await client.post(
            f"{base_url}/api/query",
            json={
                "question": "Test question",
                "library_ids": []
            }
        )
        assert response.status_code == 400

        # Test missing required fields
        response = await client.post(
            f"{base_url}/api/query",
            json={"question": "Test"}
        )
        assert response.status_code == 422  # Pydantic validation error


# ============================================================================
# Error Handling Tests
# ============================================================================

@pytest.mark.api
@pytest.mark.asyncio
async def test_invalid_library_id(api_environment_validated, base_url: str):
    """Test handling of invalid library ID."""
    async with httpx.AsyncClient() as client:
        # Try to get status for non-existent library
        response = await client.get(f"{base_url}/api/libraries/999999999/status")

        # Note: Status endpoint is currently a placeholder that returns 200 for any library_id
        # TODO: Update this test when status endpoint validates library existence
        assert response.status_code == 200
        data = response.json()
        # Placeholder always returns indexed=False for unknown libraries
        assert data["indexed"] is False


@pytest.mark.api
@pytest.mark.asyncio
async def test_query_unindexed_library(api_environment_validated, base_url: str):
    """Test querying a library that hasn't been indexed."""
    # Use a library ID that exists but is unlikely to be indexed
    fake_lib_id = "999999999"

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{base_url}/api/query",
            json={
                "question": "Test question",
                "library_ids": [fake_lib_id]
            },
            timeout=10.0
        )

        # Should return error (400 or 500)
        assert response.status_code >= 400
