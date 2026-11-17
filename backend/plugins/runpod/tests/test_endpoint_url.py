"""RunPod finds a user's endpoint URL from their key, by the endpoint's name."""

import unittest

from backend.plugins.runpod.testing import provider as runpod_provider
from backend.providers.testing import FakeHTTP, FakeResponse


class TestRunPodEndpointUrl(unittest.TestCase):
    def test_finds_the_endpoint_by_name(self):
        http = FakeHTTP()
        http.add("GET", r"/v2/serverless$", FakeResponse(200, {"endpoints": [
            {"id": "other", "name": "something-else"}, {"id": "ep9", "name": "zotero-rag-llm"}],
            "pagination": {"nextCursor": None, "hasNextPage": False}}))
        self.assertEqual(runpod_provider("llm", http=http).endpoint_url("k"), "https://api.runpod.ai/v2/ep9/openai/v1")

    def test_none_when_absent_or_on_any_error(self):
        http = FakeHTTP()
        http.add("GET", r"/v2/serverless$", FakeResponse(200, {"endpoints": [], "pagination": {"nextCursor": None, "hasNextPage": False}}))
        self.assertIsNone(runpod_provider("llm", http=http).endpoint_url("k"))
        http = FakeHTTP()
        http.add("GET", r"/v2/serverless$", FakeResponse(403, {"error": "no"}))
        self.assertIsNone(runpod_provider("llm", http=http).endpoint_url("k"))


if __name__ == "__main__":
    unittest.main()
