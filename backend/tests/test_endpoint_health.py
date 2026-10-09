"""Unit tests for backend.utils.endpoint_health."""

import unittest

import httpx

from backend.utils.endpoint_health import HEALTH_CHECKS, check_runpod_health

BASE = "https://api.runpod.ai/v2/abc123/openai/v1"


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class FakeClient:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class CheckRunpodHealthTest(unittest.TestCase):
    def test_ready_when_workers_ready(self):
        client = FakeClient(FakeResponse(200, {"workers": {"ready": 1, "running": 0}}))
        result = check_runpod_health(BASE, "key", client=client)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(client.calls[0][0], "https://api.runpod.ai/v2/abc123/health")
        self.assertEqual(client.calls[0][1]["headers"]["Authorization"], "Bearer key")

    def test_ready_when_workers_running(self):
        client = FakeClient(FakeResponse(200, {"workers": {"ready": 0, "running": 2}}))
        self.assertEqual(check_runpod_health(BASE, "k", client=client)["status"], "ready")

    def test_cold_when_no_workers(self):
        client = FakeClient(FakeResponse(200, {"workers": {"ready": 0, "running": 0, "idle": 0}}))
        self.assertEqual(check_runpod_health(BASE, "k", client=client)["status"], "cold")

    def test_cold_when_workers_object_missing(self):
        client = FakeClient(FakeResponse(200, {}))
        self.assertEqual(check_runpod_health(BASE, "k", client=client)["status"], "cold")

    def test_cold_when_initializing(self):
        """A worker actively spinning up is still 'cold' from the caller's
        point of view (not ready yet), but this is the normal, expected
        transient state — distinct from throttled (see below)."""
        client = FakeClient(FakeResponse(200, {"workers": {"ready": 0, "running": 0, "initializing": 1}}))
        result = check_runpod_health(BASE, "k", client=client)
        self.assertEqual(result["status"], "cold")
        self.assertIn("initializing", result["detail"])

    def test_throttled_when_runpod_cannot_allocate_a_worker(self):
        """Observed live: a query hung for minutes with zero visible
        progress because the endpoint's GPU type had no available RunPod
        capacity — workers.throttled>0 with jobs stuck in jobs.inQueue,
        never reaching initializing/ready. 'cold' (scaled to zero, wakes
        normally on the next request) is actively misleading here: nothing
        will progress until capacity frees up or the GPU type changes, so
        this needs its own distinct status."""
        client = FakeClient(FakeResponse(
            200, {"jobs": {"inQueue": 2, "inProgress": 0}, "workers": {"ready": 0, "running": 0, "throttled": 1}}
        ))
        result = check_runpod_health(BASE, "k", client=client)
        self.assertEqual(result["status"], "throttled")
        self.assertIn("2", result["detail"])

    def test_not_throttled_when_jobs_queued_but_worker_initializing_normally(self):
        """A queued job alongside an *initializing* worker (not throttled) is
        the ordinary cold-start path, not a stuck/no-capacity state."""
        client = FakeClient(FakeResponse(
            200, {"jobs": {"inQueue": 1}, "workers": {"ready": 0, "running": 0, "initializing": 1, "throttled": 0}}
        ))
        result = check_runpod_health(BASE, "k", client=client)
        self.assertEqual(result["status"], "cold")

    def test_malformed_base_url_is_unreachable(self):
        client = FakeClient(FakeResponse(200, {}))
        result = check_runpod_health("https://example.com/v1", "k", client=client)
        self.assertEqual(result["status"], "unreachable")
        self.assertEqual(client.calls, [])

    def test_http_error_is_unreachable_with_status_code(self):
        client = FakeClient(FakeResponse(401, {}))
        result = check_runpod_health(BASE, "k", client=client)
        self.assertEqual(result["status"], "unreachable")
        self.assertIn("401", result["detail"])

    def test_timeout_is_unreachable(self):
        client = FakeClient(httpx.ReadTimeout("timed out"))
        result = check_runpod_health(BASE, "k", client=client)
        self.assertEqual(result["status"], "unreachable")
        self.assertIn("timed out", result["detail"])

    def test_registry_contains_runpod(self):
        self.assertIs(HEALTH_CHECKS["runpod"], check_runpod_health)


if __name__ == "__main__":
    unittest.main()
