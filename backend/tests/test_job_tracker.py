"""Unit tests for backend.services.job_tracker."""

import time
import unittest
from unittest.mock import patch

from backend.services.job_tracker import JobTracker, make_progress_callback


class TestJobTracker(unittest.TestCase):
    def setUp(self):
        self.tracker = JobTracker()

    def test_create_returns_processing_job(self):
        job_id = self.tracker.create()
        job = self.tracker.get(job_id)
        self.assertEqual(job.status, "processing")
        self.assertEqual(job.progress, 0.0)

    def test_update_progress_and_message(self):
        job_id = self.tracker.create()
        self.tracker.update(job_id, progress=0.5, message="halfway")
        job = self.tracker.get(job_id)
        self.assertEqual(job.progress, 0.5)
        self.assertEqual(job.message, "halfway")
        self.assertEqual(job.status, "processing")

    def test_update_with_result_marks_done(self):
        job_id = self.tracker.create()
        self.tracker.update(job_id, result={"chapters": []})
        job = self.tracker.get(job_id)
        self.assertEqual(job.status, "done")
        self.assertEqual(job.result, {"chapters": []})

    def test_update_with_error_marks_error(self):
        job_id = self.tracker.create()
        self.tracker.update(job_id, error="boom")
        job = self.tracker.get(job_id)
        self.assertEqual(job.status, "error")
        self.assertEqual(job.error, "boom")

    def test_unknown_job_id_returns_none(self):
        self.assertIsNone(self.tracker.get("does-not-exist"))

    def test_update_unknown_job_id_is_noop(self):
        self.tracker.update("does-not-exist", progress=1.0)  # must not raise

    def test_stale_jobs_are_pruned_on_get(self):
        job_id = self.tracker.create()
        with patch("time.monotonic", return_value=time.monotonic() + 3601):
            self.assertIsNone(self.tracker.get(job_id))


class TestMakeProgressCallback(unittest.TestCase):
    def test_callback_updates_tracker(self):
        tracker = JobTracker()
        job_id = tracker.create()
        callback = make_progress_callback(tracker, job_id)
        callback(0.3, "step 3 of 10")
        job = tracker.get(job_id)
        self.assertEqual(job.progress, 0.3)
        self.assertEqual(job.message, "step 3 of 10")


if __name__ == "__main__":
    unittest.main()
