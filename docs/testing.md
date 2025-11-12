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

**Files:** `backend/tests/test_*.py`
- Unit tests: Mock all external dependencies
- Integration tests: Use `@pytest.mark.integration` marker

**Test Markers:**
- `@pytest.mark.integration` - Requires real Zotero + API keys
- `@pytest.mark.slow` - Long-running tests (optional)

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
