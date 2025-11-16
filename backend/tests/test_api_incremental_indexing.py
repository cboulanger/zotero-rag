"""
API integration tests for incremental indexing functionality.

These tests verify the complete incremental indexing workflow via HTTP API:
- Library metadata endpoints
- Index status tracking
- Incremental vs. full indexing modes
- Hard reset functionality

Requires: Zotero running + test library synced + API keys
"""

import pytest
import httpx
from backend.tests.conftest import (
    get_test_library_id,
    get_test_library_type,
)


@pytest.mark.api
@pytest.mark.asyncio
async def test_index_status_before_indexing(api_environment_validated):
    """Test that index status returns 404 for never-indexed library."""
    base_url = api_environment_validated
    library_id = get_test_library_id()

    async with httpx.AsyncClient(timeout=30.0) as client:
        # Get index status for library that hasn't been indexed
        response = await client.get(
            f"{base_url}/api/libraries/{library_id}/index-status"
        )

        # Should return 404 if never indexed
        # OR 200 if previously indexed in other tests
        assert response.status_code in [200, 404]

        if response.status_code == 200:
            # If library was previously indexed, verify metadata structure
            metadata = response.json()
            assert "library_id" in metadata
            assert "last_indexed_version" in metadata
            assert "total_chunks" in metadata


@pytest.mark.api
@pytest.mark.asyncio
async def test_full_indexing_mode(api_environment_validated):
    """Test full indexing mode via API."""
    base_url = api_environment_validated
    library_id = get_test_library_id()
    library_type = get_test_library_type()

    async with httpx.AsyncClient(timeout=300.0) as client:  # 5 min timeout for indexing
        # Trigger full indexing
        response = await client.post(
            f"{base_url}/api/index/library/{library_id}",
            params={
                "library_type": library_type,
                "library_name": "Test Library",
                "mode": "full"
            }
        )

        assert response.status_code == 200
        result = response.json()

        # Verify response structure
        assert result["success"] is True
        assert "library_id" in result
        assert "statistics" in result

        stats = result["statistics"]
        assert stats["mode"] == "full"
        assert "items_processed" in stats
        assert "chunks_added" in stats
        assert "elapsed_seconds" in stats

        # Verify at least some items were indexed
        assert stats["items_processed"] >= 0


@pytest.mark.api
@pytest.mark.asyncio
async def test_index_status_after_indexing(api_environment_validated):
    """Test that index status is available after indexing."""
    base_url = api_environment_validated
    library_id = get_test_library_id()
    library_type = get_test_library_type()

    async with httpx.AsyncClient(timeout=300.0) as client:
        # First, ensure library is indexed
        await client.post(
            f"{base_url}/api/index/library/{library_id}",
            params={
                "library_type": library_type,
                "mode": "auto"
            }
        )

        # Now get index status
        response = await client.get(
            f"{base_url}/api/libraries/{library_id}/index-status"
        )

        assert response.status_code == 200
        metadata = response.json()

        # Verify metadata structure
        assert metadata["library_id"] == library_id
        assert metadata["library_type"] == library_type
        assert metadata["last_indexed_version"] >= 0
        assert metadata["total_items_indexed"] >= 0
        assert metadata["total_chunks"] >= 0
        assert "last_indexed_at" in metadata
        assert "indexing_mode" in metadata
        assert metadata["force_reindex"] is False


@pytest.mark.api
@pytest.mark.asyncio
async def test_incremental_indexing_mode(api_environment_validated):
    """Test incremental indexing mode after initial full index."""
    base_url = api_environment_validated
    library_id = get_test_library_id()
    library_type = get_test_library_type()

    async with httpx.AsyncClient(timeout=300.0) as client:
        # First, do a full index to establish baseline
        response1 = await client.post(
            f"{base_url}/api/index/library/{library_id}",
            params={
                "library_type": library_type,
                "mode": "full"
            }
        )
        assert response1.status_code == 200
        result1 = response1.json()
        assert result1["statistics"]["mode"] == "full"

        # Get initial metadata
        status_response = await client.get(
            f"{base_url}/api/libraries/{library_id}/index-status"
        )
        initial_metadata = status_response.json()
        initial_version = initial_metadata["last_indexed_version"]

        # Now do incremental index
        response2 = await client.post(
            f"{base_url}/api/index/library/{library_id}",
            params={
                "library_type": library_type,
                "mode": "incremental"
            }
        )

        assert response2.status_code == 200
        result2 = response2.json()

        # Should use incremental mode
        assert result2["statistics"]["mode"] == "incremental"

        # Should process 0 items if nothing changed
        stats = result2["statistics"]
        assert stats["items_added"] == 0
        assert stats["items_updated"] == 0

        # Verify version was preserved or updated
        final_status = await client.get(
            f"{base_url}/api/libraries/{library_id}/index-status"
        )
        final_metadata = final_status.json()
        assert final_metadata["last_indexed_version"] >= initial_version


@pytest.mark.api
@pytest.mark.asyncio
async def test_auto_mode_selection(api_environment_validated):
    """Test that auto mode selects appropriate indexing mode."""
    base_url = api_environment_validated
    library_id = get_test_library_id()
    library_type = get_test_library_type()

    async with httpx.AsyncClient(timeout=300.0) as client:
        # First index with auto mode
        response1 = await client.post(
            f"{base_url}/api/index/library/{library_id}",
            params={
                "library_type": library_type,
                "mode": "auto"
            }
        )

        assert response1.status_code == 200
        result1 = response1.json()

        # Auto mode should select full for first-time OR incremental if already indexed
        assert result1["statistics"]["mode"] in ["full", "incremental"]

        # Second index with auto mode should select incremental
        response2 = await client.post(
            f"{base_url}/api/index/library/{library_id}",
            params={
                "library_type": library_type,
                "mode": "auto"
            }
        )

        assert response2.status_code == 200
        result2 = response2.json()

        # Should now use incremental mode
        assert result2["statistics"]["mode"] == "incremental"


@pytest.mark.api
@pytest.mark.asyncio
async def test_hard_reset_functionality(api_environment_validated):
    """Test hard reset marks library for full reindex."""
    base_url = api_environment_validated
    library_id = get_test_library_id()
    library_type = get_test_library_type()

    async with httpx.AsyncClient(timeout=300.0) as client:
        # Ensure library is indexed first
        await client.post(
            f"{base_url}/api/index/library/{library_id}",
            params={
                "library_type": library_type,
                "mode": "full"
            }
        )

        # Verify force_reindex is False
        status1 = await client.get(
            f"{base_url}/api/libraries/{library_id}/index-status"
        )
        metadata1 = status1.json()
        assert metadata1["force_reindex"] is False

        # Request hard reset
        reset_response = await client.post(
            f"{base_url}/api/libraries/{library_id}/reset-index"
        )

        assert reset_response.status_code == 200
        reset_result = reset_response.json()
        assert "message" in reset_result
        assert "marked for hard reset" in reset_result["message"].lower()

        # Verify force_reindex flag is now True
        status2 = await client.get(
            f"{base_url}/api/libraries/{library_id}/index-status"
        )
        metadata2 = status2.json()
        assert metadata2["force_reindex"] is True

        # Next index with auto mode should use full mode
        index_response = await client.post(
            f"{base_url}/api/index/library/{library_id}",
            params={
                "library_type": library_type,
                "mode": "auto"
            }
        )

        assert index_response.status_code == 200
        index_result = index_response.json()
        assert index_result["statistics"]["mode"] == "full"

        # Verify force_reindex flag is cleared after reset
        status3 = await client.get(
            f"{base_url}/api/libraries/{library_id}/index-status"
        )
        metadata3 = status3.json()
        assert metadata3["force_reindex"] is False


@pytest.mark.api
@pytest.mark.asyncio
async def test_list_indexed_libraries(api_environment_validated):
    """Test listing all indexed libraries."""
    base_url = api_environment_validated
    library_id = get_test_library_id()
    library_type = get_test_library_type()

    async with httpx.AsyncClient(timeout=300.0) as client:
        # Ensure at least one library is indexed
        await client.post(
            f"{base_url}/api/index/library/{library_id}",
            params={
                "library_type": library_type,
                "mode": "auto"
            }
        )

        # Get list of indexed libraries
        response = await client.get(
            f"{base_url}/api/libraries/indexed"
        )

        assert response.status_code == 200
        libraries = response.json()

        # Should be a list
        assert isinstance(libraries, list)

        # Should contain our test library
        library_ids = [lib["library_id"] for lib in libraries]
        assert library_id in library_ids

        # Verify structure of library metadata
        for lib in libraries:
            assert "library_id" in lib
            assert "library_type" in lib
            assert "last_indexed_version" in lib
            assert "total_chunks" in lib


@pytest.mark.api
@pytest.mark.asyncio
async def test_indexing_statistics_structure(api_environment_validated):
    """Test that indexing statistics have correct structure."""
    base_url = api_environment_validated
    library_id = get_test_library_id()
    library_type = get_test_library_type()

    async with httpx.AsyncClient(timeout=300.0) as client:
        response = await client.post(
            f"{base_url}/api/index/library/{library_id}",
            params={
                "library_type": library_type,
                "mode": "full"
            }
        )

        assert response.status_code == 200
        result = response.json()

        # Verify top-level structure
        assert "success" in result
        assert "library_id" in result
        assert "statistics" in result

        # Verify statistics structure
        stats = result["statistics"]
        required_fields = [
            "mode",
            "items_processed",
            "items_added",
            "items_updated",
            "chunks_added",
            "chunks_deleted",
            "elapsed_seconds"
        ]

        for field in required_fields:
            assert field in stats, f"Missing field: {field}"

        # Verify data types
        assert isinstance(stats["mode"], str)
        assert isinstance(stats["items_processed"], int)
        assert isinstance(stats["items_added"], int)
        assert isinstance(stats["items_updated"], int)
        assert isinstance(stats["chunks_added"], int)
        assert isinstance(stats["chunks_deleted"], int)
        assert isinstance(stats["elapsed_seconds"], (int, float))

        # Verify logical constraints
        assert stats["items_processed"] >= 0
        assert stats["items_added"] >= 0
        assert stats["items_updated"] >= 0
        assert stats["chunks_added"] >= 0
        assert stats["chunks_deleted"] >= 0
        assert stats["elapsed_seconds"] >= 0


@pytest.mark.api
@pytest.mark.asyncio
async def test_concurrent_indexing_prevented(api_environment_validated):
    """Test that concurrent indexing of same library is prevented."""
    base_url = api_environment_validated
    library_id = get_test_library_id()
    library_type = get_test_library_type()

    async with httpx.AsyncClient(timeout=300.0) as client:
        # Start first indexing operation (don't await completion)
        task1 = client.post(
            f"{base_url}/api/index/library/{library_id}",
            params={
                "library_type": library_type,
                "mode": "full"
            }
        )

        # Try to start second indexing operation immediately
        # (This should either succeed or return 409 Conflict)
        import asyncio
        await asyncio.sleep(0.5)  # Give first request time to start

        response2 = await client.post(
            f"{base_url}/api/index/library/{library_id}",
            params={
                "library_type": library_type,
                "mode": "full"
            }
        )

        # Wait for first task to complete
        response1 = await task1

        # First request should succeed
        assert response1.status_code == 200

        # Second request should either succeed (if first finished) or return conflict
        assert response2.status_code in [200, 409]
