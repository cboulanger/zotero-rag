"""Unit tests for the Kreuzberg extraction adapter's timeout computation."""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.services.extraction.kreuzberg import AttachmentTooLargeError, KreuzbergExtractor, _compute_timeout


class TestComputeTimeout(unittest.TestCase):
    def test_scales_with_content_size(self):
        small = _compute_timeout(10_000, "application/pdf")
        large = _compute_timeout(10_000_000, "application/pdf")
        self.assertGreater(large, small)

    def test_floor_applies_to_tiny_files(self):
        self.assertEqual(_compute_timeout(1, "application/pdf"), 60)

    def test_default_cap_is_1800_seconds(self):
        # A huge PDF should saturate at the default cap, not grow unbounded.
        self.assertEqual(_compute_timeout(10_000_000_000, "application/pdf"), 1800)

    def test_custom_cap_overrides_default(self):
        self.assertEqual(
            _compute_timeout(10_000_000_000, "application/pdf", cap=600), 600
        )

    def test_multiplier_scales_both_computed_value_and_cap(self):
        # A file whose computed (unscaled) timeout would already hit the
        # default cap must still get more time when multiplier > 1 — the
        # cap itself has to scale, not just the raw size/rate division,
        # otherwise doubling the timeout for an already-capped huge file
        # would be a no-op.
        capped = _compute_timeout(10_000_000_000, "application/pdf", cap=1800, multiplier=1.0)
        doubled = _compute_timeout(10_000_000_000, "application/pdf", cap=1800, multiplier=2.0)
        self.assertEqual(capped, 1800)
        self.assertEqual(doubled, 3600)

    def test_multiplier_scales_a_below_cap_value_too(self):
        base = _compute_timeout(300_000, "application/pdf", cap=1800, multiplier=1.0)
        doubled = _compute_timeout(300_000, "application/pdf", cap=1800, multiplier=2.0)
        self.assertEqual(doubled, base * 2)


class TestKreuzbergExtractorTimeoutWiring(unittest.IsolatedAsyncioTestCase):
    async def test_extract_and_chunk_uses_configured_cap_and_multiplier(self):
        extractor = KreuzbergExtractor(timeout_cap=600)

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value=[{"chunks": []}])

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "backend.services.extraction.kreuzberg.httpx.AsyncClient",
            return_value=mock_client,
        ) as mock_client_cls:
            await extractor.extract_and_chunk(
                b"x" * 10_000_000_000, "application/pdf", timeout_multiplier=2.0
            )

        # cap=600, multiplier=2.0 → expect the doubled cap, not the default 1800-based one.
        mock_client_cls.assert_called_once_with(timeout=1200)


class TestMaxContentBytes(unittest.IsolatedAsyncioTestCase):
    async def test_refuses_content_over_the_cap_without_contacting_kreuzberg(self):
        extractor = KreuzbergExtractor(max_content_bytes=1000)

        with patch("backend.services.extraction.kreuzberg.httpx.AsyncClient") as mock_client_cls:
            with self.assertRaises(AttachmentTooLargeError):
                await extractor.extract_and_chunk(b"x" * 1001, "application/pdf")
        mock_client_cls.assert_not_called()

    async def test_allows_content_at_exactly_the_cap(self):
        extractor = KreuzbergExtractor(max_content_bytes=1000)
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value=[{"chunks": []}])
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("backend.services.extraction.kreuzberg.httpx.AsyncClient", return_value=mock_client):
            await extractor.extract_and_chunk(b"x" * 1000, "application/pdf")  # must not raise

    async def test_no_cap_configured_never_refuses(self):
        # Default (max_content_bytes=None) — the pre-existing, uncapped behaviour
        # other tests in this file rely on — must be unaffected.
        extractor = KreuzbergExtractor()
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value=[{"chunks": []}])
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("backend.services.extraction.kreuzberg.httpx.AsyncClient", return_value=mock_client):
            await extractor.extract_and_chunk(b"x" * 1_000_000, "application/pdf")  # must not raise


if __name__ == "__main__":
    unittest.main()
