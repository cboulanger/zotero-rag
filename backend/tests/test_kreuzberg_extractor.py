"""Unit tests for backend.services.extraction.kreuzberg's per-request language config."""

import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.services.extraction.kreuzberg import KreuzbergExtractor


class TestPerRequestLanguage(unittest.IsolatedAsyncioTestCase):
    async def test_default_has_no_language_key(self):
        extractor = KreuzbergExtractor()
        mock_response = MagicMock()
        mock_response.json.return_value = [{"chunks": []}]
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            await extractor.extract_and_chunk(b"fake pdf bytes", "application/pdf")
            sent_config = json.loads(mock_client.post.call_args.kwargs["data"]["config"])
        self.assertNotIn("ocr", sent_config)

    async def test_language_override_sets_ocr_language(self):
        extractor = KreuzbergExtractor()
        mock_response = MagicMock()
        mock_response.json.return_value = [{"chunks": []}]
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            await extractor.extract_and_chunk(b"fake pdf bytes", "application/pdf", ocr_language="deu")
            sent_config = json.loads(mock_client.post.call_args.kwargs["data"]["config"])
        self.assertEqual(sent_config["ocr"]["language"], "deu")


if __name__ == "__main__":
    unittest.main()
