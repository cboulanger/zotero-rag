"""Unit tests for backend.services.health_check.

Covers the two checks (disk space, Qdrant collection health) and the
ntfy.sh alert-dedup state machine: alert once when a problem starts, once
when it recovers, and stay silent on every tick in between (observed gap in
the original incident: the "red" optimizer_status sat unnoticed until a user
reported a 504).
"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from backend.services import health_check


class CheckQdrantCollectionsTest(unittest.TestCase):
    def _make_vector_store(self, collection_infos: dict):
        vs = MagicMock()
        vs.CHUNKS_COLLECTION = "document_chunks"
        vs.DEDUP_COLLECTION = "deduplication"
        vs.METADATA_COLLECTION = "library_metadata"
        vs.client.get_collection.side_effect = lambda name: collection_infos[name]
        return vs

    def test_all_green_and_ok_returns_no_problems(self):
        info = SimpleNamespace(status="green", optimizer_status="ok")
        vs = self._make_vector_store({
            "document_chunks": info, "deduplication": info, "library_metadata": info,
        })
        self.assertEqual(health_check.check_qdrant_collections(vs), [])

    def test_red_status_is_reported(self):
        healthy = SimpleNamespace(status="green", optimizer_status="ok")
        unhealthy = SimpleNamespace(status="red", optimizer_status="ok")
        vs = self._make_vector_store({
            "document_chunks": unhealthy, "deduplication": healthy, "library_metadata": healthy,
        })
        problems = health_check.check_qdrant_collections(vs)
        self.assertEqual(len(problems), 1)
        self.assertIn("document_chunks", problems[0])
        self.assertIn("red", problems[0])

    def test_optimizer_error_is_reported_with_detail(self):
        unhealthy = SimpleNamespace(
            status="red",
            optimizer_status=SimpleNamespace(error="Not enough space available for optimization"),
        )
        healthy = SimpleNamespace(status="green", optimizer_status="ok")
        vs = self._make_vector_store({
            "document_chunks": unhealthy, "deduplication": healthy, "library_metadata": healthy,
        })
        problems = health_check.check_qdrant_collections(vs)
        detail = "\n".join(problems)
        self.assertIn("Not enough space available for optimization", detail)

    def test_query_failure_is_reported_not_raised(self):
        vs = MagicMock()
        vs.CHUNKS_COLLECTION = "document_chunks"
        vs.DEDUP_COLLECTION = "deduplication"
        vs.METADATA_COLLECTION = "library_metadata"
        vs.client.get_collection.side_effect = ConnectionError("qdrant unreachable")
        problems = health_check.check_qdrant_collections(vs)
        self.assertTrue(any("qdrant unreachable" in p for p in problems))


class SendNtfyAlertTest(unittest.TestCase):
    def test_posts_title_and_message_to_topic_url(self):
        with patch.object(health_check.requests, "post") as mock_post:
            health_check.send_ntfy_alert(
                "https://ntfy.sh/my-topic", title="Alert", message="Disk low", priority="high"
            )
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], "https://ntfy.sh/my-topic")
        self.assertEqual(kwargs["data"], b"Disk low")
        self.assertEqual(kwargs["headers"]["Title"], "Alert")
        self.assertEqual(kwargs["headers"]["Priority"], "high")


class RunHealthCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = Path(self.tmp.name) / "health_check_state.json"

        self.settings = SimpleNamespace(
            data_path=Path(self.tmp.name),
            health_check_min_free_disk_percent=15.0,
            ntfy_topic_url="https://ntfy.sh/my-topic",
        )
        self.healthy_vs = MagicMock()
        self.healthy_vs.CHUNKS_COLLECTION = "document_chunks"
        self.healthy_vs.DEDUP_COLLECTION = "deduplication"
        self.healthy_vs.METADATA_COLLECTION = "library_metadata"
        healthy_info = SimpleNamespace(status="green", optimizer_status="ok")
        self.healthy_vs.client.get_collection.return_value = healthy_info

        self.unhealthy_vs = MagicMock()
        self.unhealthy_vs.CHUNKS_COLLECTION = "document_chunks"
        self.unhealthy_vs.DEDUP_COLLECTION = "deduplication"
        self.unhealthy_vs.METADATA_COLLECTION = "library_metadata"
        unhealthy_info = SimpleNamespace(status="red", optimizer_status="ok")
        self.unhealthy_vs.client.get_collection.return_value = unhealthy_info

    def _fake_disk_usage(self, free_gb):
        return SimpleNamespace(total=100 * 1024**3, used=0, free=free_gb * 1024**3)

    def test_healthy_run_sends_no_alert(self):
        with patch.object(health_check.requests, "post") as mock_post, \
             patch.object(health_check.disk_space.shutil, "disk_usage", return_value=self._fake_disk_usage(30)):
            problems = health_check.run_health_check(self.settings, self.healthy_vs, state_path=self.state_path)
        self.assertEqual(problems, [])
        mock_post.assert_not_called()

    def test_new_problem_sends_one_alert(self):
        with patch.object(health_check.requests, "post") as mock_post, \
             patch.object(health_check.disk_space.shutil, "disk_usage", return_value=self._fake_disk_usage(30)):
            problems = health_check.run_health_check(self.settings, self.unhealthy_vs, state_path=self.state_path)
        self.assertTrue(problems)
        mock_post.assert_called_once()

    def test_ongoing_problem_does_not_resend_alert(self):
        with patch.object(health_check.requests, "post"), \
             patch.object(health_check.disk_space.shutil, "disk_usage", return_value=self._fake_disk_usage(30)):
            health_check.run_health_check(self.settings, self.unhealthy_vs, state_path=self.state_path)
        with patch.object(health_check.requests, "post") as mock_post_2, \
             patch.object(health_check.disk_space.shutil, "disk_usage", return_value=self._fake_disk_usage(30)):
            health_check.run_health_check(self.settings, self.unhealthy_vs, state_path=self.state_path)
        mock_post_2.assert_not_called()

    def test_recovery_sends_recovery_alert(self):
        with patch.object(health_check.requests, "post"), \
             patch.object(health_check.disk_space.shutil, "disk_usage", return_value=self._fake_disk_usage(30)):
            health_check.run_health_check(self.settings, self.unhealthy_vs, state_path=self.state_path)
        with patch.object(health_check.requests, "post") as mock_post_2, \
             patch.object(health_check.disk_space.shutil, "disk_usage", return_value=self._fake_disk_usage(30)):
            problems = health_check.run_health_check(self.settings, self.healthy_vs, state_path=self.state_path)
        self.assertEqual(problems, [])
        mock_post_2.assert_called_once()
        self.assertIn("recovered", mock_post_2.call_args.kwargs["headers"]["Title"].lower())

    def test_no_topic_url_never_posts_even_when_unhealthy(self):
        self.settings.ntfy_topic_url = None
        with patch.object(health_check.requests, "post") as mock_post, \
             patch.object(health_check.disk_space.shutil, "disk_usage", return_value=self._fake_disk_usage(30)):
            health_check.run_health_check(self.settings, self.unhealthy_vs, state_path=self.state_path)
        mock_post.assert_not_called()

    def test_low_disk_space_is_reported_as_a_problem(self):
        with patch.object(health_check.requests, "post"), \
             patch.object(health_check.disk_space.shutil, "disk_usage", return_value=self._fake_disk_usage(2)):
            problems = health_check.run_health_check(self.settings, self.healthy_vs, state_path=self.state_path)
        self.assertTrue(any("disk" in p.lower() for p in problems))


if __name__ == "__main__":
    unittest.main()
