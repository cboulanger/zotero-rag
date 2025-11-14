# Testing Guide

This document describes the testing strategy for the Zotero RAG system.

## Test Levels

### Unit Tests (Default - Fast)

- **No external dependencies** (mocked)
- **Fast execution** (<10 seconds total)
- **Run by default** with `npm run test:backend`
- **Coverage:** Configuration, Zotero API, embeddings, vector store, PDF extraction, chunking, document processor, LLM service, RAG engine, API endpoints

### Integration Tests (Real Dependencies)

- **Requires:** Zotero running + test group synced + API keys
- **Slower execution** (5-15 minutes)
- **Opt-in via markers:** `@pytest.mark.integration`
- **Coverage:** End-to-end indexing, RAG queries, error handling with real services

### API Integration Tests (Backend Server + Real Dependencies)

- **Requires:** Same as integration tests (Zotero + API keys)
- **Test server:** Automatically started/stopped by `conftest.py` on port 8219
- **Opt-in via markers:** `@pytest.mark.api`
- **Coverage:** Full HTTP API endpoints with real backend server
- **Isolation:** Uses test configuration from `.env.test` with separate log files

---

## Running Tests

### Quick Commands

```bash
# Unit tests only (default - no setup required)
npm run test:backend

# Quick health check (~30 seconds)
npm run test:integration:quick

# Full integration suite (5-15 minutes)
npm run test:integration

# Everything (unit + integration, 10-20 minutes)
npm run test:all
```

### Direct pytest Commands

```bash
# Unit tests
uv run pytest

# Integration tests
uv run pytest -m integration -v -s

# Specific test
uv run pytest backend/tests/test_embeddings.py::TestEmbeddingService::test_cache -v

# Tests matching keyword
uv run pytest -k "embedding" -v

# With coverage report
uv run pytest --cov=backend --cov-report=html
```

---

## Integration Test Setup

### Prerequisites

1. **Zotero running** with test group synced:
   - Test group: <https://www.zotero.org/groups/6297749/test-rag-plugin>
   - Verify: `curl http://localhost:23119/api/users` (should return JSON)

2. **Environment configured** - Create `.env` file:

```bash
# Copy template
cp .env.dist .env

# Edit and add:
MODEL_PRESET=remote-kisski
KISSKI_API_KEY=your_api_key_here
```

Or set directly:

```bash
# Windows PowerShell
$env:MODEL_PRESET="remote-kisski"
$env:KISSKI_API_KEY="your_key_here"

# Linux/macOS
export MODEL_PRESET=remote-kisski
export KISSKI_API_KEY=your_key_here
```

### Verify Setup

```bash
# Quick health check to validate environment
npm run test:integration:quick
```

If tests are **skipped** with an error message, check:
- Zotero is running
- Test group is synced
- API key is set correctly

---

## Writing Tests

### Unit Test Template

```python
import unittest
from unittest.mock import Mock, AsyncMock

class TestMyFeature(unittest.IsolatedAsyncioTestCase):
    async def test_my_function(self):
        """Test description."""
        # Setup
        mock_dep = AsyncMock(return_value="expected")

        # Execute
        result = await my_function(mock_dep)

        # Verify
        self.assertEqual(result, "expected")
        mock_dep.assert_called_once()
```

### Integration Test Template

```python
import pytest

@pytest.mark.integration
@pytest.mark.asyncio
async def test_my_integration(zotero_client, integration_config):
    """Test with real dependencies."""
    service = MyService(config=integration_config)
    result = await service.process_real_data()

    assert result.success
    assert len(result.items) > 0
```

---

## Test Organization

**File:** [backend/tests/conftest.py](../backend/tests/conftest.py)
- Session-level fixtures for integration tests
- Environment validation with pre-flight checks
- Automatic test skipping with helpful error messages
- Test server management for API tests

**Files:** `backend/tests/test_*.py`
- Unit tests: Mock all external dependencies
- Integration tests: Use `@pytest.mark.integration` marker
- API tests: Use `@pytest.mark.api` marker

**Test Markers:**

- `@pytest.mark.integration` - Requires real Zotero + API keys
- `@pytest.mark.api` - Requires test server + Zotero + API keys
- `@pytest.mark.slow` - Long-running tests (optional)

---

## Integration Test Infrastructure

### Overview

The integration test system is managed by [backend/tests/conftest.py](../backend/tests/conftest.py), which provides:

1. **Automatic environment validation** before running integration/API tests
2. **Test server lifecycle management** for API tests
3. **Session-level fixtures** for shared test resources
4. **Graceful test skipping** when prerequisites are not met

### Test Server Management

For tests marked with `@pytest.mark.api`, the test infrastructure automatically:

1. **Starts a test server** on port 8219 (separate from development server on 8000)
2. **Loads test configuration** from `.env.test` (falls back to `.env`)
3. **Waits for server readiness** (health check with timeout)
4. **Captures server logs** to `logs/test_server.log` for debugging
5. **Stops server cleanly** after all tests complete (even on failure)

**Example API test:**

```python
import pytest
import httpx

@pytest.mark.api
@pytest.mark.asyncio
async def test_library_indexing_api(api_environment_validated):
    """Test full indexing workflow via API."""
    base_url = api_environment_validated  # Test server URL

    async with httpx.AsyncClient() as client:
        # Call API endpoint
        response = await client.post(
            f"{base_url}/api/index/library/1?mode=auto"
        )

        assert response.status_code == 200
        result = response.json()
        assert result["success"] is True
```

### Environment Validation

Before running integration/API tests, the system validates:

- **Zotero connectivity** - Can connect to `http://localhost:23119`
- **Test library access** - Test group library is synced
- **API keys** - Required keys are set in environment
- **Test data** - Minimum expected items/PDFs are present

If any validation fails, all integration/API tests are **automatically skipped** with a helpful error message.

### Test Configuration Files

**`.env.test`** (optional, for test-specific overrides):
```bash
# Test configuration (lowest priority)
MODEL_PRESET=remote-kisski
LOG_LEVEL=INFO
QDRANT_COLLECTION=test_documents
```

**`.env`** (normal configuration):
```bash
# Development/production config (medium priority)
MODEL_PRESET=cpu-only
KISSKI_API_KEY=your_key_here
```

**Environment variables** (highest priority):
```bash
# Override everything
export MODEL_PRESET=remote-kisski
export KISSKI_API_KEY=your_key_here
```

**Priority order:** Environment variables > `.env` > `.env.test`

### Fixtures Available

**For integration tests:**

- `integration_environment_validated` - Validates environment (Zotero + API keys)

**For API tests:**

- `test_server` - Starts/stops test server, returns base URL
- `api_environment_validated` - Validates environment + ensures server is running

**Helper functions (from `test_environment` module):**

- `get_test_library_id()` - Get test library ID
- `get_test_library_type()` - Get library type ("user" or "group")
- `get_backend_base_url()` - Get test server base URL
- `get_expected_min_items()` - Minimum expected items in test library
- `get_expected_min_chunks()` - Minimum expected chunks after indexing

### Server Log Management

Test server logs are written to `logs/test_server.log` and include:

- All HTTP requests/responses
- Application logs (backend processing)
- Error tracebacks (if any)

After tests complete, if errors occurred, relevant log excerpts are printed to console.

**View full logs:**
```bash
cat logs/test_server.log
```

### Debugging Integration Tests

**Run with verbose output and live logs:**
```bash
uv run pytest -m api -v -s
```

**Check test server logs:**
```bash
# Tail logs while tests run (separate terminal)
tail -f logs/test_server.log
```

**Debug specific test:**
```bash
uv run pytest backend/tests/test_api_incremental_indexing.py::test_incremental_mode_api -vv -s
```

**Skip environment validation (for debugging fixtures):**
```python
# In your test file
pytestmark = pytest.mark.skipif(
    os.getenv("SKIP_VALIDATION") == "1",
    reason="Manual debugging mode"
)
```

---

## CI/CD Strategy

### Fast CI (Every Commit)

```bash
# Run unit tests only (~10 seconds)
uv run pytest -m "not integration" --cov=backend
```

### Nightly/Pre-Release CI

```bash
# Run integration tests (5-15 minutes)
uv run pytest -m integration -v
```

### GitHub Actions Example

```yaml
name: Tests
on: [push, pull_request]
jobs:
  unit-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - name: Install uv
        run: curl -LsSf https://astral.sh/uv/install.sh | sh
      - name: Run unit tests
        run: uv run pytest -m "not integration" --cov=backend
```

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Tests skipped: "Cannot connect to Zotero" | Start Zotero desktop app, verify `curl http://localhost:23119/api/users` |
| Tests skipped: "API key not found" | Set `KISSKI_API_KEY` in `.env` or environment variables |
| Tests skipped: "Test library not found" | Join and sync test group in Zotero |
| Tests very slow | Use faster model preset or check network connectivity |
| Memory issues | Close other apps, use smaller model (`cpu-only` preset) |

### Detailed Diagnostics

```bash
# Run with verbose output
uv run pytest -vv --tb=long

# Run specific test with full output
uv run pytest backend/tests/test_real_integration.py::test_rag_query_end_to_end -vv -s

# Stop on first failure
uv run pytest -x
```

---

## Best Practices

**Unit Tests:**
- Mock all external dependencies (Zotero, LLMs, file system)
- Keep tests fast (<1 second per test)
- Test edge cases and error paths
- Use descriptive test names

**Integration Tests:**
- Use temporary vector store (auto-cleaned)
- Make tests idempotent (can run multiple times)
- Clean up resources in `finally` blocks
- Skip gracefully if dependencies unavailable

**General:**
- Run unit tests before committing
- Run integration tests before releasing
- Keep test code clean and documented
- Update tests when requirements change

---

## Test Data

**Test Library:** <https://www.zotero.org/groups/6297749/test-rag-plugin>
- Small, curated collection (~20 PDFs)
- Public group for reproducible testing
- Update test constants if library content changes

**Test Fixtures:** [backend/tests/fixtures/](../backend/tests/fixtures/)
- Sample PDFs for unit tests
- Deterministic test data

---

## References

- [pytest documentation](https://docs.pytest.org/)
- [pytest-asyncio](https://pytest-asyncio.readthedocs.io/)
- [Architecture Documentation](architecture.md)
- [CLI Commands](cli.md)
