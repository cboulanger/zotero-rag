# Integration Testing Quick Start

Quick reference for running integration tests with real Zotero and live API services.

## Prerequisites Checklist

- [ ] Zotero desktop application running
- [ ] Test group synced: https://www.zotero.org/groups/6297749/test-rag-plugin
- [ ] API key available (KISSKI or other)
- [ ] Environment variables configured

## One-Time Setup

### 1. Join Test Group

Visit https://www.zotero.org/groups/6297749/test-rag-plugin and join the group, then sync in Zotero desktop.

### 2. Configure Environment

Create or edit `.env` file:

```bash
# Copy template
cp .env.dist .env

# Edit and add your API key
MODEL_PRESET=remote-kisski
KISSKI_API_KEY=your_actual_api_key_here
```

Or set environment variables directly:

**Windows PowerShell:**
```powershell
$env:MODEL_PRESET="remote-kisski"
$env:KISSKI_API_KEY="your_key_here"
```

**Windows CMD:**
```cmd
set MODEL_PRESET=remote-kisski
set KISSKI_API_KEY=your_key_here
```

**Linux/macOS:**
```bash
export MODEL_PRESET=remote-kisski
export KISSKI_API_KEY=your_key_here
```

### 3. Verify Zotero is Running

```bash
# Should return JSON with user info
curl http://localhost:23119/api/users
```

## Running Integration Tests

### Quick Health Check (30 seconds)

Verify environment is set up correctly:

```bash
npm run test:integration:quick
```

This runs only the connectivity test to confirm:
- Zotero is accessible
- Test library is synced
- API key is valid

### Full Integration Suite (5-15 minutes)

Run all integration tests:

```bash
npm run test:integration
```

Includes:
- Health checks
- Full library indexing
- RAG query with real LLM
- Error handling

### Run Everything (Unit + Integration)

```bash
npm run test:all
```

Takes 10-20 minutes. Best run before releases.

## Understanding Test Output

### Expected Output

```
============================= test session starts =============================
collecting ... collected 10 items

backend/tests/test_real_integration.py::test_zotero_connectivity PASSED
backend/tests/test_real_integration.py::test_embedding_service PASSED
backend/tests/test_real_integration.py::test_llm_service_connectivity PASSED

Indexing library 6297749...
  Progress: 5/20 items processed
  Progress: 10/20 items processed
  Progress: 15/20 items processed
  Progress: 20/20 items processed

Indexing results: {'status': 'completed', 'items_processed': 20, ...}
✅ Successfully indexed 20 items, created 245 chunks

backend/tests/test_real_integration.py::test_index_real_library PASSED

Querying: 'What are the main topics covered in these documents?'
Answer: Based on the retrieved documents, the main topics include...
Sources: 5 citations
  - Document Title (p. 12, score: 0.823)
  - Another Document (p. 5, score: 0.791)
  ...

✅ RAG query successful with 5 sources

backend/tests/test_real_integration.py::test_rag_query_end_to_end PASSED
============================== 10 passed in 8m 34s ==============================
```

### Test Skipped?

If you see `SKIPPED [1]` with a reason:

```
SKIPPED [1] (Cannot connect to Zotero: ...)
```

**Action:** Check prerequisites above (Zotero running, test group synced, etc.)

### Test Failed?

Check the error message and consult [Testing Guide - Troubleshooting](testing.md#troubleshooting).

## Common Workflows

### Before Committing Code

Run fast unit tests only:
```bash
npm run test:backend
```

### Before Creating PR

Run integration tests to catch real-world issues:
```bash
npm run test:integration
```

### Before Release

Run everything:
```bash
npm run test:all
```

### Debugging a Specific Test

```bash
# Run one test with full output
uv run pytest backend/tests/test_real_integration.py::test_rag_query_end_to_end -vv -s --tb=long
```

### Testing with Different Models

```bash
# Use different preset
MODEL_PRESET=remote-openai OPENAI_API_KEY=sk-... npm run test:integration

# Or edit .env file and run normally
npm run test:integration
```

## Performance Expectations

| Test | Duration | Notes |
|------|----------|-------|
| Health check | 10s | Quick validation |
| Library indexing | 1-5min | Depends on PDF count |
| RAG query | 30s-2min | Depends on LLM latency |
| Full suite | 5-15min | All integration tests |

## Troubleshooting Quick Fixes

| Problem | Quick Fix |
|---------|-----------|
| "Cannot connect to Zotero" | Start Zotero desktop app |
| "API key not found" | Set `KISSKI_API_KEY` in `.env` or environment |
| "Test library not found" | Sync test group in Zotero |
| Tests very slow | Use faster embedding model or check network |
| Memory issues | Close other apps, use smaller model |

## Next Steps

For detailed information, see:
- [Full Testing Guide](testing.md) - Complete documentation
- [Test Coverage Report](../htmlcov/index.html) - After running with coverage
- [Phase 1.5 Progress](../implementation/phase1.5-progress.md) - Implementation status

## Tips

✅ **Do:**
- Run health check first to save time
- Use `.env` file for persistent config
- Check Zotero is running before starting tests
- Close resource-heavy apps during tests

❌ **Don't:**
- Don't run integration tests with limited disk space
- Don't interrupt tests (they clean up resources)
- Don't commit API keys to git
- Don't run slow tests on battery power
