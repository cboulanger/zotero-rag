"""Unit tests for the Kreuzberg extraction adapter's timeout computation."""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from backend.services.extraction.kreuzberg import (
    AttachmentTooLargeError,
    KreuzbergExtractor,
    KreuzbergUnavailableError,
    _compute_timeout,
)


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


class TestKreuzbergConnectRetry(unittest.IsolatedAsyncioTestCase):
    """A connection failure — the sidecar refusing connections outright, or
    dropping one mid-request without sending a response — is retried for up
    to 10 minutes before giving up — see kreuzberg.py's module-level
    _CONNECT_RETRY_BUDGET_SECONDS.

    Regression: a bare ConnectError used to raise immediately on the first
    failed attachment, and every subsequent attachment in the run then failed
    the exact same way with no retry at all — if the sidecar was mid-restart
    (a known, recoverable condition after a crash/OOM/deploy), the run never
    gave it a chance to come back.
    """

    def _make_response(self):
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value=[{"chunks": []}])
        return mock_response

    async def test_connect_error_retries_then_succeeds(self):
        extractor = KreuzbergExtractor()
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        connect_exc = httpx.ConnectError("Connection refused")
        mock_client.post = AsyncMock(side_effect=[connect_exc, connect_exc, self._make_response()])

        with patch("backend.services.extraction.kreuzberg.httpx.AsyncClient", return_value=mock_client), \
             patch("backend.services.extraction.kreuzberg.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            chunks = await extractor.extract_and_chunk(b"content", "application/pdf")

        self.assertEqual(chunks, [])
        self.assertEqual(mock_client.post.await_count, 3)
        self.assertEqual(mock_sleep.await_count, 2)

    async def test_remote_protocol_error_retries_then_succeeds(self):
        """"Server disconnected without sending a response" (e.g. the sidecar
        crashed/restarted mid-request) gets the same retry treatment as a
        bare ConnectError, not an immediate failure."""
        extractor = KreuzbergExtractor()
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        disconnect_exc = httpx.RemoteProtocolError("Server disconnected without sending a response.")
        mock_client.post = AsyncMock(side_effect=[disconnect_exc, self._make_response()])

        with patch("backend.services.extraction.kreuzberg.httpx.AsyncClient", return_value=mock_client), \
             patch("backend.services.extraction.kreuzberg.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            chunks = await extractor.extract_and_chunk(b"content", "application/pdf")

        self.assertEqual(chunks, [])
        self.assertEqual(mock_client.post.await_count, 2)
        self.assertEqual(mock_sleep.await_count, 1)

    async def test_connect_error_raises_unavailable_once_budget_exhausted(self):
        extractor = KreuzbergExtractor()
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))

        with patch("backend.services.extraction.kreuzberg.httpx.AsyncClient", return_value=mock_client), \
             patch("backend.services.extraction.kreuzberg.asyncio.sleep", new_callable=AsyncMock), \
             patch("backend.services.extraction.kreuzberg.time.monotonic", side_effect=[0, 1000]):
            with self.assertRaises(KreuzbergUnavailableError) as ctx:
                await extractor.extract_and_chunk(b"content", "application/pdf")

        self.assertIn("600s", str(ctx.exception))

    async def test_zero_retry_budget_raises_immediately_on_first_connect_error(self):
        """Regression: DocumentProcessor passes connect_retry_budget_seconds=0
        in settings.testing, so a test exercising the real upload path without
        mocking httpx (the sidecar is never actually running under pytest)
        must fail on the very first connection attempt rather than retrying
        for minutes and hanging past the test's own timeout."""
        extractor = KreuzbergExtractor(connect_retry_budget_seconds=0)
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))

        with patch("backend.services.extraction.kreuzberg.httpx.AsyncClient", return_value=mock_client), \
             patch("backend.services.extraction.kreuzberg.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            with self.assertRaises(KreuzbergUnavailableError):
                await extractor.extract_and_chunk(b"content", "application/pdf")

        mock_client.post.assert_awaited_once()
        mock_sleep.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
