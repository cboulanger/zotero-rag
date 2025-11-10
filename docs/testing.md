# Testing Guide

This document describes the testing strategy for the Zotero RAG system, covering unit tests, integration tests, and end-to-end testing with real dependencies.

## Test Levels

### 1. Unit Tests (Default)

**Purpose:** Fast, isolated tests with mocked dependencies

**Characteristics:**
- Run by default with `npm run test:backend` or `uv run pytest`
- No external dependencies required
- Deterministic and fast (<1 second per test)
- 100% mocked (Zotero, LLMs, file system where appropriate)

**Coverage:**
- Configuration system (15 tests)
- Zotero API client (15 tests)
- Embedding service (15 tests)
- Vector database (9 tests)
- PDF extraction (21 tests)
- Semantic chunking (26 tests)
- Document processor (15 tests)
- LLM service (12 tests)
- RAG engine (10 tests)
- API endpoints (13 tests)

**Total:** 151 unit tests

### 2. Integration Tests

**Purpose:** Test with real external services and live data

**Characteristics:**
- Require real Zotero instance with test group
- Require real API keys (e.g., KISSKI_API_KEY)
- Use actual embedding models and LLM services
- Slower (1-10 minutes depending on library size)
- Opt-in via pytest markers

**Prerequisites:**
1. Zotero running locally with test group synced
2. Environment variables configured (see below)
3. Internet connection for API calls
4. Disk space for models and vector store

**Test Coverage:**
- Environment health checks
- Real library indexing
- Incremental indexing (deduplication)
- End-to-end RAG queries
- Multi-query consistency
- Error handling with real services

## Running Tests

### Quick Start

```bash
# Run only unit tests (default - fast, no setup required)
npm run test:backend

# Or directly with pytest
uv run pytest
```

### Integration Tests

#### Setup Environment

1. **Start Zotero** with test group synced:
   - Test group: https://www.zotero.org/groups/6297749/test-rag-plugin
   - Make sure Zotero local API is accessible (localhost:23119)

2. **Set environment variables:**

```bash
# Copy and customize .env
cp .env.dist .env

# Edit .env and add:
MODEL_PRESET=remote-kisski
KISSKI_API_KEY=your_api_key_here
```

Or set them directly:

```bash
# Windows (PowerShell)
$env:MODEL_PRESET="remote-kisski"
$env:KISSKI_API_KEY="your_key_here"

# Windows (CMD)
set MODEL_PRESET=remote-kisski
set KISSKI_API_KEY=your_key_here

# Linux/macOS
export MODEL_PRESET=remote-kisski
export KISSKI_API_KEY=your_key_here
```

#### Run Integration Tests

```bash
# Run all integration tests
uv run pytest -m integration -v -s

# Run specific integration test
uv run pytest backend/tests/test_real_integration.py::test_rag_query_end_to_end -v -s

# Run integration tests with output (see progress)
uv run pytest -m integration -v -s --tb=short

# Run with coverage report
uv run pytest -m integration --cov=backend --cov-report=html -v -s
```

#### Run ALL Tests (Unit + Integration)

```bash
# Run everything (will take 5-15 minutes)
uv run pytest -m "" -v -s
```

### Test Selection Strategies

```bash
# Only fast unit tests (default)
uv run pytest

# Only integration tests
uv run pytest -m integration

# Everything except slow tests
uv run pytest -m "not slow"

# Only slow performance tests
uv run pytest -m slow

# Specific test file
uv run pytest backend/tests/test_document_processor.py -v

# Specific test function
uv run pytest backend/tests/test_real_integration.py::test_zotero_connectivity -v

# Tests matching pattern
uv run pytest -k "indexing" -v

# Stop on first failure
uv run pytest -x

# Verbose with full traceback
uv run pytest -vv --tb=long
```

## Integration Test Details

### Test Library

- **Group ID:** 6297749
- **URL:** https://www.zotero.org/groups/6297749/test-rag-plugin
- **Purpose:** Small, curated collection for reproducible testing
- **Content:** ~20 academic PDFs (update EXPECTED_MIN_ITEMS in test file if changed)

### Expected Behavior

Integration tests validate:

1. **Connectivity Tests** - Quick checks to verify environment setup:
   - Zotero is running and accessible
   - Test library is synced
   - Embedding model loads correctly
   - LLM API is accessible

2. **Indexing Tests** - Full document processing pipeline:
   - Fetches real items from Zotero
   - Downloads actual PDFs
   - Extracts text with page numbers
   - Generates embeddings
   - Stores in vector database
   - Tracks progress correctly
   - Handles errors gracefully

3. **RAG Query Tests** - Complete question-answering pipeline:
   - Generates query embeddings
   - Retrieves relevant chunks
   - Calls real LLM
   - Returns structured answers with citations
   - Includes page numbers and text anchors

4. **Error Handling** - Validates robustness:
   - Invalid library IDs
   - Unindexed libraries
   - Network failures

### Test Isolation

Each integration test:
- Uses a **temporary vector store** (auto-cleaned after test)
- Can run **independently** (no shared state)
- Cleans up resources in `finally` blocks

### Performance Expectations

Typical execution times on modern hardware:

- **Health checks:** <10 seconds
- **Library indexing:** 1-5 minutes (depends on library size)
- **RAG query:** 10-60 seconds (depends on LLM response time)
- **Full integration suite:** 5-15 minutes

## Continuous Integration (CI)

### Recommended CI Strategy

**1. Fast CI (every commit):**
```yaml
# Run unit tests only
- uv run pytest -m "not integration" --cov=backend
```

**2. Nightly/Weekly CI:**
```yaml
# Run full integration tests
- uv run pytest -m integration
```

**3. Pre-release:**
```yaml
# Run everything
- uv run pytest -m ""
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

  integration-tests:
    runs-on: ubuntu-latest
    # Only run on main branch or manual trigger
    if: github.ref == 'refs/heads/main' || github.event_name == 'workflow_dispatch'
    steps:
      - uses: actions/checkout@v3
      - name: Install uv
        run: curl -LsSf https://astral.sh/uv/install.sh | sh
      - name: Set up Zotero
        run: |
          # Install Zotero and set up test library
          # (Implementation depends on your CI environment)
      - name: Run integration tests
        env:
          MODEL_PRESET: remote-kisski
          KISSKI_API_KEY: ${{ secrets.KISSKI_API_KEY }}
        run: uv run pytest -m integration -v
```

## Writing New Tests

### Unit Test Template

```python
import unittest
from unittest.mock import Mock, AsyncMock

class TestMyFeature(unittest.IsolatedAsyncioTestCase):
    async def test_my_function(self):
        """Test description."""
        # Setup: Create mocks
        mock_dependency = Mock()
        mock_dependency.method = AsyncMock(return_value="expected")

        # Execute: Call your function
        result = await my_function(mock_dependency)

        # Verify: Assert expectations
        self.assertEqual(result, "expected")
        mock_dependency.method.assert_called_once()
```

### Integration Test Template

```python
import pytest

@pytest.mark.integration
@pytest.mark.asyncio
async def test_my_integration(zotero_client, integration_config):
    """Test with real dependencies."""
    # This test will be skipped unless -m integration is used

    # Setup: Use real services
    service = MyService(config=integration_config)

    # Execute: Call with real data
    result = await service.process_real_data()

    # Verify: Check real outcomes
    assert result.success
    assert len(result.items) > 0
```

## Troubleshooting

### Integration Tests Are Skipped

**Problem:** Tests show as "SKIPPED" when running with `-m integration`

**Solutions:**
1. Check Zotero is running: `curl http://localhost:23119/api/users`
2. Verify environment variables: `echo $MODEL_PRESET`
3. Confirm test library is synced in Zotero
4. Check API key is valid: `echo $KISSKI_API_KEY`

### Tests Fail with "Cannot connect to Zotero"

**Problem:** `aiohttp.client_exceptions.ClientConnectorError`

**Solutions:**
1. Start Zotero desktop application
2. Check Zotero local API is enabled (should be default)
3. Verify no firewall blocking localhost:23119
4. Try: `curl http://localhost:23119/api/users` (should return JSON)

### Tests Fail with "API key not found"

**Problem:** `pytest.skip: API key not found in environment: KISSKI_API_KEY`

**Solutions:**
1. Set environment variable (see Setup Environment above)
2. Check variable is set: `echo $KISSKI_API_KEY`
3. Restart terminal/IDE after setting variables
4. Use `.env` file (see .env.dist template)

### Integration Tests Are Too Slow

**Problem:** Tests take >15 minutes

**Solutions:**
1. Run specific tests instead of full suite
2. Use smaller test library
3. Use faster embedding model (all-MiniLM-L6-v2)
4. Consider local LLM instead of API calls
5. Check network latency to KISSKI API

### Memory Issues During Tests

**Problem:** OOM errors or system slowdown

**Solutions:**
1. Close other applications
2. Use smaller embedding model
3. Reduce max_chunk_size in tests
4. Run fewer tests concurrently
5. Check MODEL_PRESET matches your hardware

## Best Practices

### For Unit Tests
- ✅ Mock all external dependencies
- ✅ Keep tests fast (<1 second)
- ✅ Test edge cases and error paths
- ✅ Use descriptive test names
- ✅ One assertion per test (when possible)

### For Integration Tests
- ✅ Use test data that won't change
- ✅ Clean up resources in teardown
- ✅ Make tests idempotent (can run multiple times)
- ✅ Include timeouts for network calls
- ✅ Log progress for long-running tests
- ✅ Skip gracefully if dependencies unavailable

### General
- ✅ Run unit tests before committing
- ✅ Run integration tests before releasing
- ✅ Document expected test duration
- ✅ Keep test code clean (it's documentation!)
- ✅ Update tests when requirements change

## Test Maintenance

### Updating Test Data

If test library content changes:

1. Update constants in `test_real_integration.py`:
   ```python
   EXPECTED_MIN_ITEMS = 20  # Update to actual count
   EXPECTED_MIN_CHUNKS = 200  # Update based on expected chunks
   ```

2. Run tests to validate:
   ```bash
   uv run pytest -m integration -v
   ```

3. Update this documentation if behavior changes

### Adding New Integration Tests

1. Add test to `backend/tests/test_real_integration.py`
2. Mark with `@pytest.mark.integration`
3. Use fixtures: `zotero_client`, `integration_config`, `llm_service`
4. Document prerequisites and expected duration
5. Update this guide if new setup is required

## Further Reading

- [pytest documentation](https://docs.pytest.org/)
- [pytest-asyncio](https://pytest-asyncio.readthedocs.io/)
- [Testing best practices](https://docs.python-guide.org/writing/tests/)
- [Integration testing patterns](https://martinfowler.com/articles/practical-test-pyramid.html)
