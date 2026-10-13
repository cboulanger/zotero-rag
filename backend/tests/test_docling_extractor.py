"""Unit tests for the Docling extraction adapter (docling-serve and KISSKI/SAIA flavours)."""

import json
import unittest
from typing import Callable
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from backend.config.presets import ExtractionConfig
from backend.services.extraction import (
    DoclingExtractor,
    KreuzbergExtractor,
    create_configured_extractor,
    effective_extractor_backend,
)
from backend.services.extraction.docling import (
    _RequestSpacer,
    blocks_to_chunks,
    docling_document_blocks,
    markdown_blocks,
)
from backend.services.extraction.kreuzberg import (
    AttachmentTooLargeError,
    KreuzbergParsingError,
    KreuzbergTimeoutError,
    KreuzbergUnavailableError,
)


def _doc() -> dict:
    """A small DoclingDocument: title, header furniture, a list, a table with caption, a picture caption."""
    return {
        "schema_name": "DoclingDocument",
        "body": {"children": [
            {"$ref": "#/texts/0"}, {"$ref": "#/texts/1"}, {"$ref": "#/texts/2"},
            {"$ref": "#/groups/0"}, {"$ref": "#/tables/0"}, {"$ref": "#/pictures/0"}, {"$ref": "#/texts/6"},
        ]},
        "groups": [{"label": "list", "children": [{"$ref": "#/texts/3"}, {"$ref": "#/texts/4"}]}],
        "texts": [
            {"label": "page_header", "text": "Journal 5/2024", "prov": [{"page_no": 1}]},
            {"label": "section_header", "text": "Introduction", "prov": [{"page_no": 1}]},
            {"label": "text", "text": "Zotero  manages   references.", "prov": [{"page_no": 1}]},
            {"label": "list_item", "text": "- first", "prov": [{"page_no": 2}]},
            {"label": "list_item", "text": "- second", "prov": [{"page_no": 2}]},
            {"label": "caption", "text": "Figure 1: a logo", "prov": [{"page_no": 3}]},
            {"label": "page_footer", "text": "151", "prov": [{"page_no": 3}]},
            {"label": "caption", "text": "Table 1: tools", "prov": [{"page_no": 2}]},
        ],
        "tables": [{
            "label": "table", "prov": [{"page_no": 2}], "children": [{"$ref": "#/texts/7"}],
            "data": {"grid": [
                [{"text": "Tool"}, {"text": "Licence"}],
                [{"text": "Zotero"}, {"text": "AGPL"}],
            ]},
        }],
        "pictures": [{"label": "picture", "prov": [{"page_no": 3}], "children": [{"$ref": "#/texts/5"}]}],
    }


class TestDoclingDocumentParsing(unittest.TestCase):
    def test_walks_body_in_reading_order_and_drops_furniture(self):
        blocks = docling_document_blocks(_doc())
        texts = [b.text for b in blocks]
        self.assertEqual(texts, [
            "Introduction", "Zotero manages references.", "- first", "- second",
            "Tool | Licence\nZotero | AGPL", "Table 1: tools", "Figure 1: a logo",
        ])
        self.assertEqual([b.page_number for b in blocks], [1, 1, 2, 2, 2, 2, 3])
        self.assertTrue(blocks[0].is_heading)

    def test_heading_starts_a_new_chunk(self):
        blocks = docling_document_blocks(_doc())
        chunks = blocks_to_chunks(blocks, max_chunk_size=60, chunk_overlap=10)
        self.assertTrue(chunks[0].text.startswith("Introduction\n\nZotero manages"))
        self.assertEqual([c.chunk_index for c in chunks], list(range(len(chunks))))
        self.assertTrue(all(len(c.text) <= 60 for c in chunks))

    def test_long_block_is_split_with_its_page(self):
        long_text = " ".join(["word"] * 200)
        chunks = blocks_to_chunks(markdown_blocks(long_text), max_chunk_size=100, chunk_overlap=20)
        self.assertGreater(len(chunks), 5)
        self.assertTrue(all(len(c.text) <= 100 for c in chunks))
        self.assertIsNone(chunks[0].page_number)

    def test_markdown_headings_and_images(self):
        blocks = markdown_blocks("# Title\n\n![img](a.png)\n\nBody text.\n\n<!-- image -->")
        self.assertEqual([(b.text, b.is_heading) for b in blocks], [("Title", True), ("Body text.", False)])


class _Recorder:
    """httpx.MockTransport handler that replays queued responses and records requests."""

    def __init__(self, *responses: Callable[[httpx.Request], httpx.Response] | httpx.Response):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        nxt = self.responses.pop(0)
        return nxt(request) if callable(nxt) else nxt


class _HttpTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _RequestSpacer._next_slot.clear()
        self.sleep = AsyncMock()
        patcher = patch("backend.services.extraction.docling.asyncio.sleep", self.sleep)
        patcher.start()
        self.addCleanup(patcher.stop)

    def mock_http(self, recorder: _Recorder):
        real_client = httpx.AsyncClient

        def factory(*args, **kwargs):
            return real_client(*args, transport=httpx.MockTransport(recorder), **kwargs)

        patcher = patch("backend.services.extraction.docling.httpx.AsyncClient", side_effect=factory)
        patcher.start()
        self.addCleanup(patcher.stop)


class TestSaiaFlavour(_HttpTestCase):
    def extractor(self, **kw) -> DoclingExtractor:
        return DoclingExtractor(url="https://saia.test/", api_flavor="saia", api_key="sk-test",
                                max_chunk_size=200, connect_retry_budget_seconds=0, **kw)

    async def test_request_shape_and_page_numbers(self):
        rec = _Recorder(httpx.Response(200, json={"filename": "document", "images": [], "json_data": _doc()}))
        self.mock_http(rec)
        chunks = await self.extractor().extract_and_chunk(b"%PDF-1.7", "application/pdf")
        req = rec.requests[0]
        self.assertEqual(req.url.path, "/v1/documents/convert")
        self.assertEqual(req.url.params["response_type"], "json")
        self.assertEqual(req.headers["authorization"], "Bearer sk-test")
        self.assertIn(b'name="document"; filename="document.pdf"', req.content)
        self.assertEqual(chunks[0].page_number, 1)
        self.assertIn("Zotero | AGPL", "\n".join(c.text for c in chunks))

    async def test_markdown_only_response_has_no_pages(self):
        rec = _Recorder(httpx.Response(200, json={"markdown": "# A\n\nB text"}))
        self.mock_http(rec)
        chunks = await self.extractor().extract_and_chunk(b"<html/>", "text/html")
        self.assertEqual(chunks[0].text, "A\n\nB text")
        self.assertIsNone(chunks[0].page_number)

    async def test_per_second_429_is_waited_out(self):
        rec = _Recorder(
            httpx.Response(429, headers={"Ratelimit-Reset": "1", "X-Ratelimit-Remaining-Hour": "400"}),
            httpx.Response(200, json={"json_data": _doc()}),
        )
        self.mock_http(rec)
        chunks = await self.extractor().extract_and_chunk(b"%PDF", "application/pdf")
        self.assertTrue(chunks)
        self.assertEqual(len(rec.requests), 2)
        self.sleep.assert_any_await(1.0)

    async def test_spent_hourly_quota_is_fatal(self):
        rec = _Recorder(httpx.Response(429, headers={"Ratelimit-Reset": "1800", "X-Ratelimit-Remaining-Hour": "0"}))
        self.mock_http(rec)
        with self.assertRaises(KreuzbergUnavailableError) as ctx:
            await self.extractor().extract_and_chunk(b"%PDF", "application/pdf")
        self.assertIn("hourly quota", str(ctx.exception))

    async def test_gateway_error_is_retried(self):
        rec = _Recorder(httpx.Response(502, text="upstream request failed"),
                        httpx.Response(200, json={"json_data": _doc()}))
        self.mock_http(rec)
        self.assertTrue(await self.extractor().extract_and_chunk(b"%PDF", "application/pdf"))
        self.assertEqual(len(rec.requests), 2)

    async def test_rejected_key_is_fatal(self):
        self.mock_http(_Recorder(httpx.Response(401, json={"message": "Unauthorized"})))
        with self.assertRaises(KreuzbergUnavailableError):
            await self.extractor().extract_and_chunk(b"%PDF", "application/pdf")

    async def test_unconvertible_file_is_a_parse_error(self):
        self.mock_http(_Recorder(httpx.Response(422, json={"detail": "bad"})))
        with self.assertRaises(KreuzbergParsingError):
            await self.extractor().extract_and_chunk(b"%PDF", "application/pdf")

    async def test_missing_key_fails_without_a_request(self):
        rec = _Recorder()
        self.mock_http(rec)
        extractor = DoclingExtractor(url="https://saia.test", api_flavor="saia", api_key=None,
                                     api_key_name="KISSKI_API_KEY")
        with self.assertRaises(KreuzbergUnavailableError) as ctx:
            await extractor.extract_and_chunk(b"%PDF", "application/pdf")
        self.assertIn("KISSKI_API_KEY", str(ctx.exception))
        self.assertEqual(rec.requests, [])

    async def test_unsupported_and_plain_text_need_no_request(self):
        rec = _Recorder()
        self.mock_http(rec)
        with self.assertRaises(KreuzbergParsingError):
            await self.extractor().extract_and_chunk(b"PK..", "application/epub+zip")
        chunks = await self.extractor().extract_and_chunk(b"Plain notes.\n\nSecond.", "text/plain; charset=utf-8")
        self.assertEqual(chunks[0].text, "Plain notes.\n\nSecond.")
        self.assertEqual(rec.requests, [])

    async def test_too_large_is_refused(self):
        with self.assertRaises(AttachmentTooLargeError):
            await self.extractor(max_content_bytes=10).extract_and_chunk(b"x" * 11, "application/pdf")


def _pdf(pages: int) -> bytes:
    import io

    import pikepdf

    pdf = pikepdf.Pdf.new()
    for _ in range(pages):
        pdf.add_blank_page()
    buf = io.BytesIO()
    pdf.save(buf)
    return buf.getvalue()


def _uploaded_pages(request: httpx.Request) -> int:
    import io

    import pikepdf

    body = request.content
    start, end = body.index(b"%PDF"), body.rindex(b"%%EOF") + 5
    with pikepdf.open(io.BytesIO(body[start:end])) as pdf:
        return len(pdf.pages)


def _per_page_doc(pages: int) -> dict:
    """One text item per page, page numbers relative to the uploaded range (as Docling reports them)."""
    return {
        "body": {"children": [{"$ref": f"#/texts/{i}"} for i in range(pages)]},
        "texts": [{"label": "text", "text": f"text of range page {i + 1}", "prov": [{"page_no": i + 1}]}
                  for i in range(pages)],
    }


class TestPdfPageRanges(_HttpTestCase):
    def extractor(self, **kw) -> DoclingExtractor:
        return DoclingExtractor(url="https://saia.test", api_flavor="saia", api_key="k",
                                max_chunk_size=30, connect_retry_budget_seconds=0, **kw)

    async def test_ranges_keep_document_page_numbers(self):
        rec = _Recorder(*[lambda r: httpx.Response(200, json={"json_data": _per_page_doc(_uploaded_pages(r))})] * 3)
        self.mock_http(rec)
        chunks = await self.extractor(max_pages_per_request=2).extract_and_chunk(_pdf(5), "application/pdf")
        self.assertEqual([_uploaded_pages(r) for r in rec.requests], [2, 2, 1])
        self.assertEqual([c.page_number for c in chunks], [1, 2, 3, 4, 5])

    async def test_slow_range_is_halved_instead_of_retried(self):
        def handler(request):
            pages = _uploaded_pages(request)
            if pages > 2:
                return httpx.Response(502, text="upstream request failed")
            return httpx.Response(200, json={"json_data": _per_page_doc(pages)})

        rec = _Recorder(*[handler] * 10)
        self.mock_http(rec)
        chunks = await self.extractor().extract_and_chunk(_pdf(5), "application/pdf")
        self.assertEqual([_uploaded_pages(r) for r in rec.requests], [5, 3, 2, 1, 2])
        self.assertEqual([c.page_number for c in chunks], [1, 2, 3, 4, 5])
        self.sleep.assert_not_awaited()  # no gateway backoff for splittable ranges

    async def test_after_a_split_the_rest_uses_the_smaller_size(self):
        def handler(request):
            pages = _uploaded_pages(request)
            if pages > 2:
                return httpx.Response(502, text="upstream request failed")
            return httpx.Response(200, json={"json_data": _per_page_doc(pages)})

        rec = _Recorder(*[handler] * 10)
        self.mock_http(rec)
        chunks = await self.extractor(max_pages_per_request=4).extract_and_chunk(_pdf(8), "application/pdf")
        self.assertEqual([_uploaded_pages(r) for r in rec.requests], [4, 2, 2, 2, 2])
        self.assertEqual([c.page_number for c in chunks], list(range(1, 9)))

    async def test_a_page_that_keeps_failing_is_skipped(self):
        rec = _Recorder(
            httpx.Response(200, json={"json_data": _per_page_doc(1)}),
            *[httpx.Response(502, text="upstream request failed")] * 4,  # page 2: first try + 3 retries
        )
        self.mock_http(rec)
        chunks = await self.extractor(max_pages_per_request=1).extract_and_chunk(_pdf(2), "application/pdf")
        self.assertEqual([c.page_number for c in chunks], [1])

    async def test_all_pages_failing_is_a_timeout(self):
        self.mock_http(_Recorder(*[httpx.Response(504)] * 4))
        with self.assertRaises(KreuzbergTimeoutError):
            await self.extractor().extract_and_chunk(_pdf(1), "application/pdf")


class TestServeFlavour(_HttpTestCase):
    async def test_sync_request_shape(self):
        rec = _Recorder(httpx.Response(200, json={"document": {"json_content": _doc(), "md_content": "ignored"},
                                                  "status": "success"}))
        self.mock_http(rec)
        extractor = DoclingExtractor(url="http://docling:5001", api_key="k", ocr_enabled=False,
                                     extra_options={"table_mode": "accurate", "do_table_structure": True})
        chunks = await extractor.extract_and_chunk(b"%PDF", "application/pdf")
        req = rec.requests[0]
        self.assertEqual(req.url.path, "/v1/convert/file")
        self.assertEqual(req.headers["x-api-key"], "k")
        body = req.content.decode("latin-1")
        for field, value in (("to_formats", "json"), ("do_ocr", "false"), ("table_mode", "accurate"),
                             ("do_table_structure", "true")):
            self.assertRegex(body, rf'name="{field}"\r\n\r\n{value}\r\n')
        self.assertIn('name="files"; filename="document.pdf"', body)
        self.assertEqual(chunks[0].page_number, 1)

    async def test_async_mode_submits_polls_and_fetches(self):
        rec = _Recorder(
            httpx.Response(200, json={"task_id": "t1", "task_status": "pending"}),
            httpx.Response(200, json={"task_id": "t1", "task_status": "started"}),
            httpx.Response(200, json={"task_id": "t1", "task_status": "success"}),
            httpx.Response(200, json={"document": {"json_content": _doc()}}),
        )
        self.mock_http(rec)
        extractor = DoclingExtractor(url="http://docling:5001", async_mode=True)
        chunks = await extractor.extract_and_chunk(b"%PDF", "application/pdf")
        self.assertEqual([r.url.path for r in rec.requests], [
            "/v1/convert/file/async", "/v1/status/poll/t1", "/v1/status/poll/t1", "/v1/result/t1",
        ])
        self.assertNotIn("x-api-key", rec.requests[0].headers)
        self.assertTrue(chunks)


class TestConfiguredExtractor(unittest.TestCase):
    def settings(self, **kw) -> MagicMock:
        defaults = dict(
            extractor_backend="kreuzberg", ocr_enabled=True, kreuzberg_url="http://kb:8100",
            kreuzberg_timeout_seconds=1800, kreuzberg_max_content_bytes=None, testing=True,
            docling_url="http://localhost:5001", docling_api_flavor="serve", docling_async=False,
            docling_min_request_interval=0.0, data_path="/nonexistent",
        )
        defaults.update(kw)
        return MagicMock(**defaults)

    def preset(self, extraction=None, key_env="KISSKI_API_KEY") -> MagicMock:
        preset = MagicMock()
        preset.extraction = extraction
        preset.embedding.model_kwargs = {"api_key_env": key_env}
        return preset

    def test_preset_without_extraction_uses_the_server_setting(self):
        settings = self.settings()
        extractor = create_configured_extractor(settings, self.preset())
        self.assertIsInstance(extractor, KreuzbergExtractor)
        self.assertEqual(effective_extractor_backend(settings, self.preset()), "kreuzberg")

    def test_preset_extraction_wins_and_reuses_the_embedding_key(self):
        extraction = ExtractionConfig(backend="docling", options={
            "api_flavor": "saia", "url": "https://chat-ai.academiccloud.de",
            "api_key_env": "KISSKI_API_KEY", "min_request_interval_seconds": 1.1,
        })
        embedding = MagicMock(api_key="sk-user")
        extractor = create_configured_extractor(self.settings(), self.preset(extraction), embedding,
                                                max_chunk_size=800)
        self.assertIsInstance(extractor, DoclingExtractor)
        self.assertEqual(extractor._flavor, "saia")
        self.assertEqual(extractor._api_key, "sk-user")
        self.assertEqual(extractor._min_request_interval, 1.1)
        self.assertEqual(extractor._max_chunk_size, 800)
        self.assertEqual(extractor._connect_retry_budget_seconds, 0)

    def test_other_key_names_come_from_the_shared_store(self):
        extraction = ExtractionConfig(backend="docling", options={"api_key_env": "DOCLING_API_KEY"})
        with patch("backend.services.admin_settings_store.resolve_shared_value", return_value="shared") as rs:
            extractor = create_configured_extractor(self.settings(), self.preset(extraction), MagicMock(api_key="x"))
        rs.assert_called_once_with("DOCLING_API_KEY", "/nonexistent")
        self.assertEqual(extractor._api_key, "shared")

    def test_server_level_docling_settings(self):
        settings = self.settings(extractor_backend="docling", docling_url="http://ds:5001", docling_async=True)
        extractor = create_configured_extractor(settings, self.preset())
        self.assertIsInstance(extractor, DoclingExtractor)
        self.assertEqual(extractor._url, "http://ds:5001")
        self.assertTrue(extractor._async_mode)
        self.assertIsNone(extractor._api_key)


class TestBundledPreset(unittest.TestCase):
    def test_kisski_docling_preset_loads(self):
        import tempfile
        from pathlib import Path

        from backend.config.presets import get_preset

        with tempfile.TemporaryDirectory() as tmp:
            preset = get_preset("kisski-docling", Path(tmp))
        self.assertEqual(preset.extraction.backend, "docling")
        self.assertEqual(preset.extraction.options["api_flavor"], "saia")
        self.assertEqual(preset.embedding.model_name, "multilingual-e5-large-instruct")
        json.dumps(preset.model_dump())  # serialisable for the config API


if __name__ == "__main__":
    unittest.main()
