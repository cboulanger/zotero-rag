"""
API integration tests for push-based indexing functionality.

These tests verify the complete push-based indexing workflow via HTTP API:
- Uploading documents via POST /api/index/document
- Checking indexed status via POST /api/libraries/{id}/check-indexed
- Library metadata and status endpoints
- Hard reset functionality

Requires: running backend + API keys (no Zotero needed)
"""

import io
import json
import os
import pytest
import httpx
from pathlib import Path
from backend.tests.conftest import (
    get_test_library_id,
    get_test_library_type,
)

# Path to a small test PDF fixture
FIXTURE_PDF = Path(__file__).parent / "fixtures" / "10.5771__2699-1284-2024-3-149.pdf"


@pytest.mark.api
@pytest.mark.asyncio
async def test_pull_indexing_endpoint_removed(api_environment_validated):
    """Verify that the deprecated pull-based indexing endpoint returns 410."""
    base_url = api_environment_validated
    library_id = get_test_library_id()

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            f"{base_url}/api/index/library/{library_id}"
        )
        assert response.status_code == 410


@pytest.mark.api
@pytest.mark.asyncio
async def test_index_status_before_indexing(api_environment_validated):
    """Test that index status returns 404 for a never-indexed library."""
    base_url = api_environment_validated

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            f"{base_url}/api/libraries/nonexistent_library_xyz/index-status"
        )
        assert response.status_code == 404


@pytest.mark.api
@pytest.mark.asyncio
async def test_upload_document(api_environment_validated):
    """Test uploading a document via POST /api/index/document."""
    base_url = api_environment_validated
    library_id = get_test_library_id()
    library_type = get_test_library_type()

    if not FIXTURE_PDF.exists():
        pytest.skip(f"Test fixture not found: {FIXTURE_PDF}")

    metadata = {
        "library_id": library_id,
        "library_type": library_type,
        "item_key": "TESTITEM1",
        "attachment_key": "TESTATTACH1",
        "mime_type": "application/pdf",
        "title": "Test Paper",
        "authors": ["Test Author"],
        "year": 2024,
        "item_type": "journalArticle",
        "item_version": 1,
        "attachment_version": 1,
    }

    async with httpx.AsyncClient(timeout=120.0) as client:
        with open(FIXTURE_PDF, "rb") as f:
            response = await client.post(
                f"{base_url}/api/index/document",
                files={"file": ("test.pdf", f, "application/pdf")},
                data={"metadata": json.dumps(metadata)},
            )

        assert response.status_code == 200
        result = response.json()
        assert "status" in result
        assert result["status"] in ("indexed", "duplicate", "skipped")


@pytest.mark.api
@pytest.mark.asyncio
async def test_check_indexed_new_attachment(api_environment_validated):
    """Test check-indexed returns needs_indexing=True for unknown attachments."""
    base_url = api_environment_validated
    library_id = get_test_library_id()

    request_body = {
        "library_id": library_id,
        "library_type": get_test_library_type(),
        "attachments": [
            {
                "item_key": "UNKNOWN_ITEM",
                "attachment_key": "UNKNOWN_ATTACH",
                "mime_type": "application/pdf",
                "item_version": 1,
                "attachment_version": 1,
            }
        ],
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{base_url}/api/libraries/{library_id}/check-indexed",
            json=request_body,
        )

        assert response.status_code == 200
        data = response.json()
        assert "statuses" in data
        results = data["statuses"]
        assert isinstance(results, list)
        assert len(results) == 1
        assert results[0]["needs_indexing"] is True


@pytest.mark.api
@pytest.mark.asyncio
async def test_index_status_after_upload(api_environment_validated):
    """Test that index status is populated after uploading a document."""
    base_url = api_environment_validated
    library_id = get_test_library_id()
    library_type = get_test_library_type()

    if not FIXTURE_PDF.exists():
        pytest.skip(f"Test fixture not found: {FIXTURE_PDF}")

    metadata = {
        "library_id": library_id,
        "library_type": library_type,
        "item_key": "STATUSTEST1",
        "attachment_key": "STATUSATTACH1",
        "mime_type": "application/pdf",
        "title": "Status Test Paper",
        "authors": ["Status Author"],
        "year": 2024,
        "item_type": "journalArticle",
        "item_version": 1,
        "attachment_version": 1,
    }

    async with httpx.AsyncClient(timeout=120.0) as client:
        # Upload document
        with open(FIXTURE_PDF, "rb") as f:
            await client.post(
                f"{base_url}/api/index/document",
                files={"file": ("test.pdf", f, "application/pdf")},
                data={"metadata": json.dumps(metadata)},
            )

        # Check index status
        response = await client.get(
            f"{base_url}/api/libraries/{library_id}/index-status"
        )

        assert response.status_code == 200
        status_data = response.json()
        assert status_data["library_id"] == library_id
        assert "last_indexed_version" in status_data
        assert "total_chunks" in status_data


@pytest.mark.api
@pytest.mark.asyncio
async def test_hard_reset_functionality(api_environment_validated):
    """Test hard reset marks library for full reindex."""
    base_url = api_environment_validated
    library_id = get_test_library_id()
    library_type = get_test_library_type()

    if not FIXTURE_PDF.exists():
        pytest.skip(f"Test fixture not found: {FIXTURE_PDF}")

    metadata = {
        "library_id": library_id,
        "library_type": library_type,
        "item_key": "RESETTEST1",
        "attachment_key": "RESETATTACH1",
        "mime_type": "application/pdf",
        "title": "Reset Test Paper",
        "authors": [],
        "year": None,
        "item_type": "journalArticle",
        "item_version": 1,
        "attachment_version": 1,
    }

    async with httpx.AsyncClient(timeout=120.0) as client:
        # Upload a document first to create library metadata
        with open(FIXTURE_PDF, "rb") as f:
            await client.post(
                f"{base_url}/api/index/document",
                files={"file": ("test.pdf", f, "application/pdf")},
                data={"metadata": json.dumps(metadata)},
            )

        # Verify force_reindex is False
        status1 = await client.get(
            f"{base_url}/api/libraries/{library_id}/index-status"
        )
        if status1.status_code == 200:
            assert status1.json()["force_reindex"] is False

        # Request hard reset
        reset_response = await client.post(
            f"{base_url}/api/libraries/{library_id}/reset-index"
        )
        assert reset_response.status_code == 200
        reset_result = reset_response.json()
        assert "hard reset" in reset_result["message"].lower()
        assert reset_result["force_reindex"] is True

        # Verify force_reindex flag is now True
        status2 = await client.get(
            f"{base_url}/api/libraries/{library_id}/index-status"
        )
        assert status2.status_code == 200
        assert status2.json()["force_reindex"] is True


@pytest.mark.api
@pytest.mark.asyncio
async def test_list_indexed_libraries(api_environment_validated):
    """Test listing all indexed libraries."""
    base_url = api_environment_validated

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(f"{base_url}/api/libraries/indexed")

        assert response.status_code == 200
        libraries = response.json()
        assert isinstance(libraries, list)

        for lib in libraries:
            assert "library_id" in lib
            assert "library_type" in lib
            assert "last_indexed_version" in lib
            assert "total_chunks" in lib
