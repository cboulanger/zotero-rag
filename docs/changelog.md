## Recent Updates

**Version 1.1 - January 2025:**

- Added incremental indexing with version-based change detection
- Implemented operation cancellation support
- Added library metadata tracking (last indexed, item counts, version numbers)
- Enhanced API with new endpoints for index status and cancellation
- Improved plugin UI with mode selection and status display

**Version 1.3 - April 2025:**

- Replaced monolithic PDF extraction pipeline with pluggable `DocumentExtractor` adapter pattern
- Introduced Kreuzberg as the default extraction backend (Rust-based, native async, 91+ formats)
- Extended indexable MIME types: PDF, HTML, DOCX, EPUB
- Added `extractor_backend` setting (`kreuzberg` or `legacy`)
- Legacy pypdf + spaCy pipeline retained as `LegacyExtractor` fallback

**Version 1.4 - April 2026:**

- **Remote server support**: backend can now run on a separate machine
- New document upload API: `POST /api/index/document` (multipart bytes + metadata) and `POST /api/libraries/{id}/check-indexed` (batch version check)
- Extracted `DocumentProcessor._process_attachment_bytes()` as shared processing core for both local and remote paths
- Plugin-side `RemoteIndexer` (`plugin/src/remote_indexer.js`): reads attachment bytes via `IOUtils.read()`, uploads via multipart form data
- Automatic local/remote mode detection based on `backendURL` (configurable via plugin preferences)
- Optional API key authentication (`API_KEY` env var, `X-API-Key` header; `?api_key=` param for SSE)
- Configurable CORS origins (`ALLOWED_ORIGINS` env var)
- `REQUIRE_ZOTERO=false` setting to skip Zotero local API check in remote deployments
- Plugin preferences: API key field shown/hidden based on whether URL is local or remote
- Updated hardware presets: `apple-silicon-32gb`, `high-memory`, `cpu-only`, `remote-openai`, `apple-silicon-kisski`, `remote-kisski`, `windows-test`

**Version 1.5 - April 2026:**

- **Push-based indexing**: plugin uploads attachment bytes directly; backend no longer needs Zotero access
- Document upload API: `POST /api/index/document` (multipart bytes + metadata) and `POST /api/libraries/{id}/check-indexed` (batch version check)
- `DocumentProcessor._process_attachment_bytes()` as the core processing entry point
- Configurable CORS origins (`ALLOWED_ORIGINS` env var)
- Pull-based indexing endpoints removed (return 410 Gone)

---

**Document Version:** 1.4
**Last Updated:** April 2026
