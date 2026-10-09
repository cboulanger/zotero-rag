"""Unit tests for scripts/provision_runpod_endpoints.py."""

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import MagicMock

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "provision_runpod_endpoints.py"
_SPEC = importlib.util.spec_from_file_location("provision_runpod_endpoints_script", _SCRIPT_PATH)
provision = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(provision)


class ParseArgsTest(unittest.TestCase):
    def test_defaults(self):
        args = provision._parse_args([])
        self.assertIsNone(args.api_key)
        self.assertEqual(args.llm_model, "Qwen/Qwen2.5-7B-Instruct")
        self.assertEqual(args.embedding_gpu, "NVIDIA RTX A4000")
        self.assertEqual(args.llm_gpu, "NVIDIA RTX A5000")
        self.assertEqual(args.workers_max, 1)
        self.assertEqual(args.idle_timeout, 60)
        self.assertIsNone(args.data_centers)
        self.assertFalse(args.recreate)
        self.assertFalse(args.teardown)
        self.assertFalse(args.yes)
        self.assertFalse(args.skip_warmup)

    def test_overrides(self):
        args = provision._parse_args([
            "--api-key", "rp_test123",
            "--llm-model", "Qwen/Qwen2.5-14B-Instruct",
            "--embedding-gpu", "NVIDIA RTX 4000 Ada",
            "--llm-gpu", "NVIDIA RTX 4090",
            "--workers-max", "2",
            "--idle-timeout", "120",
            "--data-centers", "EU-RO-1,EU-SE-1",
            "--recreate",
            "--skip-warmup",
        ])
        self.assertEqual(args.api_key, "rp_test123")
        self.assertEqual(args.llm_model, "Qwen/Qwen2.5-14B-Instruct")
        self.assertEqual(args.embedding_gpu, "NVIDIA RTX 4000 Ada")
        self.assertEqual(args.llm_gpu, "NVIDIA RTX 4090")
        self.assertEqual(args.workers_max, 2)
        self.assertEqual(args.idle_timeout, 120)
        self.assertEqual(args.data_centers, "EU-RO-1,EU-SE-1")
        self.assertTrue(args.recreate)
        self.assertTrue(args.skip_warmup)

    def test_teardown_flag(self):
        args = provision._parse_args(["--teardown", "--yes"])
        self.assertTrue(args.teardown)
        self.assertTrue(args.yes)


class ResolveApiKeyTest(unittest.TestCase):
    def test_cli_flag_takes_priority_over_env(self):
        key = provision._resolve_api_key("rp_from_cli", env={"RUNPOD_API_KEY": "rp_from_env"})
        self.assertEqual(key, "rp_from_cli")

    def test_falls_back_to_env(self):
        key = provision._resolve_api_key(None, env={"RUNPOD_API_KEY": "rp_from_env"})
        self.assertEqual(key, "rp_from_env")

    def test_raises_when_neither_set(self):
        with self.assertRaises(provision.ProvisionError):
            provision._resolve_api_key(None, env={})


class FakeResponse:
    def __init__(self, status_code, json_body):
        self.status_code = status_code
        self._json_body = json_body
        self.text = str(json_body)

    def json(self):
        return self._json_body


class FakeClient:
    """Minimal stand-in for httpx.Client. `responses` is a list of (status_code,
    json_body) tuples, consumed in order, one per .request() call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def request(self, method, url, headers=None, json=None, timeout=None):
        self.calls.append((method, url, headers, json))
        status_code, body = self._responses.pop(0)
        return FakeResponse(status_code, body)


class RequestTest(unittest.TestCase):
    def test_get_returns_parsed_json_on_200(self):
        client = FakeClient([(200, {"id": "abc123", "name": "zotero-rag-embedding"})])
        result = provision._request(client, "rp_key", "GET", "/templates")
        self.assertEqual(result, {"id": "abc123", "name": "zotero-rag-embedding"})
        method, url, headers, body = client.calls[0]
        self.assertEqual(method, "GET")
        self.assertEqual(url, "https://rest.runpod.io/v1/templates")
        self.assertEqual(headers["Authorization"], "Bearer rp_key")

    def test_raises_provision_error_on_non_2xx(self):
        client = FakeClient([(401, {"error": "invalid API key"})])
        with self.assertRaises(provision.ProvisionError) as ctx:
            provision._request(client, "rp_bad_key", "GET", "/templates")
        self.assertIn("401", str(ctx.exception))

    def test_post_sends_json_body(self):
        client = FakeClient([(200, {"id": "new123"})])
        result = provision._request(
            client, "rp_key", "POST", "/templates", json_body={"name": "zotero-rag-embedding"}
        )
        self.assertEqual(result, {"id": "new123"})
        _, _, _, body = client.calls[0]
        self.assertEqual(body, {"name": "zotero-rag-embedding"})


class FindByNameTest(unittest.TestCase):
    def test_finds_matching_item(self):
        client = FakeClient([
            (200, [{"id": "t1", "name": "other-template"}, {"id": "t2", "name": "zotero-rag-embedding"}]),
        ])
        result = provision._find_by_name(client, "rp_key", "templates", "zotero-rag-embedding")
        self.assertEqual(result["id"], "t2")

    def test_returns_none_when_not_found(self):
        client = FakeClient([(200, [{"id": "t1", "name": "other-template"}])])
        result = provision._find_by_name(client, "rp_key", "templates", "zotero-rag-embedding")
        self.assertIsNone(result)

    def test_returns_none_on_empty_list(self):
        client = FakeClient([(200, [])])
        result = provision._find_by_name(client, "rp_key", "endpoints", "zotero-rag-llm")
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
