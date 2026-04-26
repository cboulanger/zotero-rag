"""
Unit tests for get_item_versions_bulk retry and timeout logic.

Uses a mock Qdrant client so no real Qdrant server is needed.
The VectorStore is constructed with a very short base timeout (1s) so
retry escalation (1s → 2s → 4s) can be verified without waiting real seconds.
"""

import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch
from qdrant_client.models import Distance


def _make_store(timeout: int = 1) -> "VectorStore":
    """Return a VectorStore whose Qdrant client is a MagicMock."""
    from backend.db.vector_store import VectorStore

    with patch("backend.db.vector_store.QdrantClient") as MockClient:
        store = VectorStore.__new__(VectorStore)
        store.storage_path = Path("/tmp/test-qdrant")
        store.embedding_dim = 384
        store.embedding_model_name = "test-model"
        store.distance = Distance.COSINE
        store.qdrant_timeout = timeout
        store.client = MagicMock()
        return store


def _scroll_result(item_keys: list, version: int = 1):
    """Build a fake scroll() return value: (points, next_offset=None)."""
    points = []
    for key in item_keys:
        point = MagicMock()
        point.payload = {"item_key": key, "library_id": "lib1", "item_version": version}
        points.append(point)
    return (points, None)


class TestGetItemVersionsBulkRetry(unittest.TestCase):

    def test_success_on_first_attempt(self):
        """scroll() succeeds immediately — no retries, correct versions returned."""
        store = _make_store(timeout=1)
        store.client.scroll.return_value = _scroll_result(["KEY1", "KEY2"], version=5)

        result = store.get_item_versions_bulk("lib1", ["KEY1", "KEY2"])

        self.assertEqual(result, {"KEY1": 5, "KEY2": 5})
        self.assertEqual(store.client.scroll.call_count, 1)
        # Timeout passed to scroll must be the base timeout (1s, attempt 0)
        _, kwargs = store.client.scroll.call_args
        self.assertEqual(kwargs["timeout"], 1)

    def test_success_on_second_attempt_after_timeout(self):
        """First scroll() raises a timeout; second attempt succeeds with doubled timeout."""
        store = _make_store(timeout=1)
        store.client.scroll.side_effect = [
            TimeoutError("timed out"),
            _scroll_result(["KEY1"], version=3),
        ]

        with patch("time.sleep") as mock_sleep:
            result = store.get_item_versions_bulk("lib1", ["KEY1"])

        self.assertEqual(result, {"KEY1": 3})
        self.assertEqual(store.client.scroll.call_count, 2)
        mock_sleep.assert_called_once_with(1)

        # First call: timeout=1, second call: timeout=2
        timeouts = [c[1]["timeout"] for c in store.client.scroll.call_args_list]
        self.assertEqual(timeouts, [1, 2])

    def test_success_on_third_attempt(self):
        """First two scroll() calls fail; third succeeds with 4× timeout."""
        store = _make_store(timeout=1)
        store.client.scroll.side_effect = [
            TimeoutError("timed out"),
            TimeoutError("timed out again"),
            _scroll_result(["KEY1"], version=7),
        ]

        with patch("time.sleep"):
            result = store.get_item_versions_bulk("lib1", ["KEY1"])

        self.assertEqual(result, {"KEY1": 7})
        self.assertEqual(store.client.scroll.call_count, 3)
        timeouts = [c[1]["timeout"] for c in store.client.scroll.call_args_list]
        self.assertEqual(timeouts, [1, 2, 4])

    def test_raises_after_all_attempts_exhausted(self):
        """All three attempts fail — the exception propagates to the caller."""
        store = _make_store(timeout=1)
        store.client.scroll.side_effect = TimeoutError("always times out")

        with patch("time.sleep"):
            with self.assertRaises(TimeoutError):
                store.get_item_versions_bulk("lib1", ["KEY1"])

        self.assertEqual(store.client.scroll.call_count, 3)

    def test_empty_keys_returns_immediately(self):
        """Passing an empty key list must return {} without touching Qdrant."""
        store = _make_store(timeout=1)

        result = store.get_item_versions_bulk("lib1", [])

        self.assertEqual(result, {})
        store.client.scroll.assert_not_called()

    def test_timeout_doubles_on_each_retry(self):
        """Verify timeout progression is base × 2^attempt for each attempt."""
        store = _make_store(timeout=5)
        store.client.scroll.side_effect = TimeoutError("always")

        with patch("time.sleep"):
            with self.assertRaises(TimeoutError):
                store.get_item_versions_bulk("lib1", ["KEY1"])

        timeouts = [c[1]["timeout"] for c in store.client.scroll.call_args_list]
        self.assertEqual(timeouts, [5, 10, 20])

    def test_pagination_followed_until_all_keys_found(self):
        """scroll() is called multiple times when next_offset is non-None."""
        store = _make_store(timeout=1)

        page1_points = []
        p = MagicMock()
        p.payload = {"item_key": "KEY1", "library_id": "lib1", "item_version": 1}
        page1_points.append(p)

        page2_points = []
        p2 = MagicMock()
        p2.payload = {"item_key": "KEY2", "library_id": "lib1", "item_version": 2}
        page2_points.append(p2)

        store.client.scroll.side_effect = [
            (page1_points, "some_offset"),  # page 1 — more results pending
            (page2_points, None),           # page 2 — done
        ]

        result = store.get_item_versions_bulk("lib1", ["KEY1", "KEY2"])

        self.assertEqual(result, {"KEY1": 1, "KEY2": 2})
        self.assertEqual(store.client.scroll.call_count, 2)
        # Second call must use the offset returned by the first
        second_call_kwargs = store.client.scroll.call_args_list[1][1]
        self.assertEqual(second_call_kwargs["offset"], "some_offset")

    def test_warning_logged_on_retry(self):
        """A warning is logged for each failed attempt before the final one."""
        store = _make_store(timeout=1)
        store.client.scroll.side_effect = [
            TimeoutError("fail 1"),
            _scroll_result(["KEY1"]),
        ]

        with patch("time.sleep"):
            with self.assertLogs("backend.db.vector_store", level="WARNING") as cm:
                store.get_item_versions_bulk("lib1", ["KEY1"])

        self.assertTrue(any("retrying" in line for line in cm.output))

    def test_error_logged_on_final_failure(self):
        """An error is logged when all attempts are exhausted."""
        store = _make_store(timeout=1)
        store.client.scroll.side_effect = TimeoutError("always")

        with patch("time.sleep"):
            with self.assertLogs("backend.db.vector_store", level="ERROR") as cm:
                with self.assertRaises(TimeoutError):
                    store.get_item_versions_bulk("lib1", ["KEY1"])

        self.assertTrue(any("failed after 3 attempts" in line for line in cm.output))


if __name__ == "__main__":
    unittest.main()
