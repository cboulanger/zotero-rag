"""Unit tests for bin/provision_runpod_endpoints.py."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "bin" / "provision_runpod_endpoints.py"
_SPEC = importlib.util.spec_from_file_location("provision_runpod_endpoints_script", _SCRIPT_PATH)
provision = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(provision)


class ParseArgsTest(unittest.TestCase):
    def test_defaults(self):
        args = provision._parse_args([])
        self.assertIsNone(args.api_key)
        self.assertEqual(args.llm_model, "Qwen/Qwen2.5-7B-Instruct")
        self.assertEqual(args.embedding_gpu, "NVIDIA RTX A5000")
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


class _EmptyBody:
    """Sentinel json_body for FakeResponse: simulates a real httpx response
    with no content (e.g. a 204), whose .json() raises JSONDecodeError
    rather than returning a Python value."""


class FakeResponse:
    def __init__(self, status_code, json_body):
        self.status_code = status_code
        self._json_body = json_body
        self.text = "" if isinstance(json_body, _EmptyBody) else str(json_body)

    def json(self):
        if isinstance(self._json_body, _EmptyBody):
            raise json.JSONDecodeError("Expecting value", "", 0)
        return self._json_body


class FakeClient:
    """Minimal stand-in for httpx.Client. `responses` is a list of (status_code,
    json_body) tuples or Exception instances, consumed in order, one per
    .request() call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def request(self, method, url, headers=None, json=None, timeout=None):
        self.calls.append((method, url, headers, json))
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        status_code, body = item
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

    def test_204_no_content_returns_none_without_parsing_json(self):
        """Observed live: RunPod's DELETE /endpoints/{id} returns 204 with an
        empty body. Calling .json() on it unconditionally crashed the script
        with a JSONDecodeError mid-recreate, right after the old endpoint had
        already been deleted but before the replacement was created."""
        client = FakeClient([(204, _EmptyBody())])
        result = provision._request(client, "rp_key", "DELETE", "/endpoints/abc123")
        self.assertIsNone(result)

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


class EnsureTemplateTest(unittest.TestCase):
    def test_creates_when_missing(self):
        client = FakeClient([
            (200, []),  # GET /templates -> not found
            (200, {"id": "t_new", "name": "zotero-rag-embedding", "imageName": "img:tag", "env": {"A": "1"}}),  # POST
        ])
        result = provision._ensure_template(
            client, "rp_key", name="zotero-rag-embedding", image="img:tag",
            env={"A": "1"}, container_disk_gb=20, recreate=False,
        )
        self.assertEqual(result["id"], "t_new")
        method, url, _, body = client.calls[1]
        self.assertEqual(method, "POST")
        self.assertEqual(url, "https://rest.runpod.io/v1/templates")
        self.assertEqual(body["name"], "zotero-rag-embedding")
        self.assertEqual(body["imageName"], "img:tag")
        self.assertEqual(body["env"], {"A": "1"})
        self.assertTrue(body["isServerless"])

    def test_reuses_matching_existing_template(self):
        existing = {"id": "t_existing", "name": "zotero-rag-embedding", "imageName": "img:tag", "env": {"A": "1"}}
        client = FakeClient([(200, [existing])])  # GET /templates -> found
        result = provision._ensure_template(
            client, "rp_key", name="zotero-rag-embedding", image="img:tag",
            env={"A": "1"}, container_disk_gb=20, recreate=False,
        )
        self.assertEqual(result["id"], "t_existing")
        self.assertEqual(len(client.calls), 1)  # only the GET, no create

    def test_warns_but_keeps_existing_on_mismatch_without_recreate(self):
        existing = {"id": "t_existing", "name": "zotero-rag-embedding", "imageName": "img:OLD", "env": {"A": "1"}}
        client = FakeClient([(200, [existing])])
        with self.assertLogs(provision.logger, level="WARNING") as ctx:
            result = provision._ensure_template(
                client, "rp_key", name="zotero-rag-embedding", image="img:NEW",
                env={"A": "1"}, container_disk_gb=20, recreate=False,
            )
        self.assertEqual(result["id"], "t_existing")
        self.assertTrue(any("differs" in msg for msg in ctx.output))

    def test_recreates_on_mismatch_with_recreate_flag(self):
        existing = {"id": "t_old", "name": "zotero-rag-embedding", "imageName": "img:OLD", "env": {"A": "1"}}
        client = FakeClient([
            (200, [existing]),  # GET templates -> found, mismatched
            (200, []),  # GET endpoints -> no referencing endpoint
            (200, {}),  # DELETE /templates/t_old
            (200, {"id": "t_new", "name": "zotero-rag-embedding", "imageName": "img:NEW", "env": {"A": "1"}}),  # POST
        ])
        result = provision._ensure_template(
            client, "rp_key", name="zotero-rag-embedding", image="img:NEW",
            env={"A": "1"}, container_disk_gb=20, recreate=True,
        )
        self.assertEqual(result["id"], "t_new")
        delete_call = client.calls[2]
        self.assertEqual(delete_call[0], "DELETE")
        self.assertEqual(delete_call[1], "https://rest.runpod.io/v1/templates/t_old")

    def test_recreate_deletes_referencing_endpoint_before_the_template(self):
        """RunPod refuses to delete a template that's still associated with an
        endpoint ("Template is associated with AI API <id>") — observed live
        when recreating a template whose endpoint hadn't been removed first.
        Since this project always names a resource's template and endpoint
        identically, _ensure_template can look up and delete that endpoint
        itself before deleting the template."""
        existing_template = {"id": "t_old", "name": "zotero-rag-llm", "imageName": "img:OLD", "env": {"A": "1"}}
        existing_endpoint = {"id": "e_old", "name": "zotero-rag-llm", "templateId": "t_old"}
        client = FakeClient([
            (200, [existing_template]),  # GET templates -> found, mismatched
            (200, [existing_endpoint]),  # GET endpoints -> found, references t_old
            (200, {}),  # DELETE /endpoints/e_old
            (200, {}),  # DELETE /templates/t_old
            (200, {"id": "t_new", "name": "zotero-rag-llm", "imageName": "img:NEW", "env": {"A": "1"}}),  # POST
        ])
        result = provision._ensure_template(
            client, "rp_key", name="zotero-rag-llm", image="img:NEW",
            env={"A": "1"}, container_disk_gb=40, recreate=True,
        )
        self.assertEqual(result["id"], "t_new")
        methods_and_urls = [(c[0], c[1]) for c in client.calls]
        self.assertEqual(methods_and_urls, [
            ("GET", "https://rest.runpod.io/v1/templates"),
            ("GET", "https://rest.runpod.io/v1/endpoints"),
            ("DELETE", "https://rest.runpod.io/v1/endpoints/e_old"),
            ("DELETE", "https://rest.runpod.io/v1/templates/t_old"),
            ("POST", "https://rest.runpod.io/v1/templates"),
        ])


class EnsureEndpointTest(unittest.TestCase):
    def test_creates_when_missing(self):
        client = FakeClient([
            (200, []),  # GET /endpoints -> not found
            (200, {"id": "e_new", "name": "zotero-rag-embedding"}),  # POST
        ])
        result = provision._ensure_endpoint(
            client, "rp_key", name="zotero-rag-embedding", template_id="t1",
            gpu_type_ids=["NVIDIA RTX A4000"], workers_max=1, idle_timeout=60,
            data_center_ids=None, recreate=False,
        )
        self.assertEqual(result["id"], "e_new")
        method, url, _, body = client.calls[1]
        self.assertEqual(method, "POST")
        self.assertEqual(body["templateId"], "t1")
        self.assertEqual(body["gpuTypeIds"], ["NVIDIA RTX A4000"])
        self.assertEqual(body["workersMin"], 0)
        self.assertEqual(body["workersMax"], 1)
        self.assertEqual(body["idleTimeout"], 60)
        self.assertNotIn("dataCenterIds", body)

    def test_includes_data_centers_when_given(self):
        client = FakeClient([(200, []), (200, {"id": "e_new", "name": "zotero-rag-llm"})])
        provision._ensure_endpoint(
            client, "rp_key", name="zotero-rag-llm", template_id="t1",
            gpu_type_ids=["NVIDIA RTX A5000"], workers_max=1, idle_timeout=60,
            data_center_ids=["EU-RO-1", "EU-SE-1"], recreate=False,
        )
        _, _, _, body = client.calls[1]
        self.assertEqual(body["dataCenterIds"], ["EU-RO-1", "EU-SE-1"])

    def test_reuses_existing_endpoint(self):
        existing = {"id": "e_existing", "name": "zotero-rag-embedding", "templateId": "t1"}
        client = FakeClient([(200, [existing])])
        result = provision._ensure_endpoint(
            client, "rp_key", name="zotero-rag-embedding", template_id="t1",
            gpu_type_ids=["NVIDIA RTX A4000"], workers_max=1, idle_timeout=60,
            data_center_ids=None, recreate=False,
        )
        self.assertEqual(result["id"], "e_existing")
        self.assertEqual(len(client.calls), 1)

    def test_warns_on_gpu_mismatch_without_recreate(self):
        existing = {"id": "e_existing", "name": "zotero-rag-embedding", "templateId": "t1",
                    "gpuTypeIds": ["NVIDIA RTX PRO 6000 Blackwell Server Edition"]}
        client = FakeClient([(200, [existing])])
        with self.assertLogs(provision.logger, level="WARNING") as ctx:
            result = provision._ensure_endpoint(
                client, "rp_key", name="zotero-rag-embedding", template_id="t1",
                gpu_type_ids=["NVIDIA RTX A4000"], workers_max=1, idle_timeout=60,
                data_center_ids=None, recreate=False,
            )
        self.assertEqual(result["id"], "e_existing")
        self.assertIn("--recreate", "\n".join(ctx.output))

    def test_recreates_endpoint_on_gpu_mismatch_with_recreate(self):
        existing = {"id": "e_old", "name": "zotero-rag-embedding", "templateId": "t1",
                    "gpuTypeIds": ["NVIDIA RTX PRO 6000 Blackwell Server Edition"]}
        client = FakeClient([
            (200, [existing]),  # GET /endpoints
            (200, None),  # DELETE
            (200, {"id": "e_new", "name": "zotero-rag-embedding"}),  # POST
        ])
        result = provision._ensure_endpoint(
            client, "rp_key", name="zotero-rag-embedding", template_id="t1",
            gpu_type_ids=["NVIDIA RTX A4000"], workers_max=1, idle_timeout=60,
            data_center_ids=None, recreate=True,
        )
        self.assertEqual(result["id"], "e_new")
        self.assertEqual(client.calls[1][0], "DELETE")
        self.assertEqual(client.calls[2][3]["gpuTypeIds"], ["NVIDIA RTX A4000"])

    def test_recreate_replaces_endpoint_whose_gpus_are_not_reported(self):
        existing = {"id": "e_old", "name": "zotero-rag-embedding", "templateId": "t1"}
        client = FakeClient([
            (200, [existing]),
            (200, None),  # DELETE
            (200, {"id": "e_new", "name": "zotero-rag-embedding"}),  # POST
        ])
        result = provision._ensure_endpoint(
            client, "rp_key", name="zotero-rag-embedding", template_id="t1",
            gpu_type_ids=["NVIDIA RTX A4000"], workers_max=1, idle_timeout=60,
            data_center_ids=None, recreate=True,
        )
        self.assertEqual(result["id"], "e_new")
        self.assertEqual(client.calls[1][0], "DELETE")

    def test_warns_but_keeps_existing_on_template_mismatch_without_recreate(self):
        existing = {"id": "e_existing", "name": "zotero-rag-embedding", "templateId": "t_old"}
        client = FakeClient([(200, [existing])])
        with self.assertLogs(provision.logger, level="WARNING") as ctx:
            result = provision._ensure_endpoint(
                client, "rp_key", name="zotero-rag-embedding", template_id="t_new",
                gpu_type_ids=["NVIDIA RTX A4000"], workers_max=1, idle_timeout=60,
                data_center_ids=None, recreate=False,
            )
        self.assertEqual(result["id"], "e_existing")
        self.assertEqual(len(client.calls), 1)
        self.assertTrue(any("differs" in msg for msg in ctx.output))

    def test_recreates_on_template_mismatch_with_recreate_flag(self):
        existing = {"id": "e_old", "name": "zotero-rag-embedding", "templateId": "t_old"}
        client = FakeClient([
            (200, [existing]),  # GET -> found, mismatched
            (200, {}),  # DELETE /endpoints/e_old
            (200, {"id": "e_new", "name": "zotero-rag-embedding", "templateId": "t_new"}),  # POST
        ])
        result = provision._ensure_endpoint(
            client, "rp_key", name="zotero-rag-embedding", template_id="t_new",
            gpu_type_ids=["NVIDIA RTX A4000"], workers_max=1, idle_timeout=60,
            data_center_ids=None, recreate=True,
        )
        self.assertEqual(result["id"], "e_new")
        delete_call = client.calls[1]
        self.assertEqual(delete_call[0], "DELETE")
        self.assertEqual(delete_call[1], "https://rest.runpod.io/v1/endpoints/e_old")


class WarmUpTest(unittest.TestCase):
    def test_embedding_warmup_posts_to_openai_embeddings_path(self):
        client = FakeClient([(200, {"data": [{"embedding": [0.1, 0.2]}]})])
        provision._warm_up_embedding(client, "rp_key", "https://api.runpod.ai/v2/e1/openai/v1", "e1")
        method, url, headers, body = client.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, "https://api.runpod.ai/v2/e1/openai/v1/embeddings")
        self.assertEqual(headers["Authorization"], "Bearer rp_key")
        self.assertEqual(body["input"], "ping")
        self.assertEqual(body["model"], provision.EMBEDDING_MODEL)

    def test_llm_warmup_posts_to_chat_completions_path(self):
        client = FakeClient([(200, {"choices": [{"message": {"content": "hi"}}]})])
        provision._warm_up_llm(client, "rp_key", "https://api.runpod.ai/v2/l1/openai/v1", "Qwen/Qwen2.5-7B-Instruct", "l1")
        method, url, headers, body = client.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, "https://api.runpod.ai/v2/l1/openai/v1/chat/completions")
        self.assertEqual(body["model"], "Qwen/Qwen2.5-7B-Instruct")
        self.assertEqual(body["max_tokens"], 1)

    def test_retries_on_failure_then_succeeds(self):
        client = FakeClient([
            (503, {"error": "cold starting"}),
            (200, {}),  # purge-queue between retries
            (200, {"data": [{"embedding": [0.1]}]}),
        ])
        with patch.object(provision.time, "sleep"):
            provision._warm_up_embedding(client, "rp_key", "https://api.runpod.ai/v2/e1/openai/v1", "e1")
        self.assertEqual(len(client.calls), 3)
        purge_call = client.calls[1]
        self.assertEqual(purge_call[0], "POST")
        self.assertEqual(purge_call[1], "https://api.runpod.ai/v2/e1/purge-queue")

    def test_gives_up_after_max_seconds_and_warns(self):
        # Every call fails; the retry loop must stop instead of looping forever.
        # Each retry cycle is (warm-up attempt, purge-queue call), so alternate.
        responses = [(503, {"error": "cold starting"}), (200, {})] * 25
        client = FakeClient(responses)
        fake_times = iter([0, 10, 50, 100, 200])  # exceeds WARMUP_MAX_SECONDS=180 on the 4th deadline check
        with patch.object(provision.time, "sleep"), \
             patch.object(provision.time, "monotonic", side_effect=lambda: next(fake_times)), \
             self.assertLogs(provision.logger, level="WARNING") as ctx:
            provision._warm_up_embedding(client, "rp_key", "https://api.runpod.ai/v2/e1/openai/v1", "e1")
        self.assertTrue(any("did not warm up" in msg for msg in ctx.output))

    def test_retries_on_connection_error_then_succeeds(self):
        client = FakeClient([
            httpx.ReadTimeout("timed out"),
            (200, {}),  # purge-queue between retries
            (200, {"data": [{"embedding": [0.1]}]}),
        ])
        with patch.object(provision.time, "sleep"):
            provision._warm_up_embedding(client, "rp_key", "https://api.runpod.ai/v2/e1/openai/v1", "e1")
        self.assertEqual(len(client.calls), 3)

    def test_short_circuits_on_non_retryable_4xx(self):
        client = FakeClient([(400, {"error": "bad model name"})])
        with patch.object(provision.time, "sleep") as mock_sleep:
            provision._warm_up_embedding(client, "rp_key", "https://api.runpod.ai/v2/e1/openai/v1", "e1")
        self.assertEqual(len(client.calls), 1)  # no retry attempted, no purge needed
        mock_sleep.assert_not_called()

    def test_retries_429_as_transient_not_as_non_retryable(self):
        client = FakeClient([
            (429, {"error": "rate limited"}),
            (200, {}),  # purge-queue between retries
            (200, {"data": [{"embedding": [0.1]}]}),
        ])
        with patch.object(provision.time, "sleep"):
            provision._warm_up_embedding(client, "rp_key", "https://api.runpod.ai/v2/e1/openai/v1", "e1")
        self.assertEqual(len(client.calls), 3)

    def test_purge_queue_failure_does_not_crash_the_retry_loop(self):
        client = FakeClient([
            (503, {"error": "cold starting"}),
            httpx.ConnectError("purge endpoint unreachable"),  # purge-queue call fails
            (200, {"data": [{"embedding": [0.1]}]}),
        ])
        with patch.object(provision.time, "sleep"):
            provision._warm_up_embedding(client, "rp_key", "https://api.runpod.ai/v2/e1/openai/v1", "e1")
        self.assertEqual(len(client.calls), 3)


class UpdateEnvFileTest(unittest.TestCase):
    def test_creates_file_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            provision._update_env_file(env_path, {"RUNPOD_API_KEY": "rp_key"})
            content = env_path.read_text()
        self.assertIn("RUNPOD_API_KEY=rp_key\n", content)

    def test_appends_new_keys_to_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text("EXISTING_VAR=value\n")
            provision._update_env_file(env_path, {"RUNPOD_API_KEY": "rp_key"})
            content = env_path.read_text()
        self.assertIn("EXISTING_VAR=value\n", content)
        self.assertIn("RUNPOD_API_KEY=rp_key\n", content)

    def test_replaces_existing_key_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text("RUNPOD_API_KEY=old_key\nOTHER_VAR=keep_me\n")
            provision._update_env_file(env_path, {"RUNPOD_API_KEY": "new_key"})
            lines = env_path.read_text().splitlines()
        self.assertIn("RUNPOD_API_KEY=new_key", lines)
        self.assertIn("OTHER_VAR=keep_me", lines)
        self.assertNotIn("RUNPOD_API_KEY=old_key", lines)
        self.assertEqual(len(lines), 2)  # no duplicate line added

    def test_handles_file_without_trailing_newline(self):
        """Regression guard: a blind `>>` append onto a file with no trailing
        newline would concatenate onto the last line instead of starting a
        new one — see this project's documented worktree .env hazard."""
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text("EXISTING_VAR=value")  # no trailing newline
            provision._update_env_file(env_path, {"RUNPOD_API_KEY": "rp_key"})
            lines = env_path.read_text().splitlines()
        self.assertIn("EXISTING_VAR=value", lines)
        self.assertIn("RUNPOD_API_KEY=rp_key", lines)
        self.assertEqual(len(lines), 2)


class TeardownTest(unittest.TestCase):
    def test_deletes_existing_endpoint_and_template(self):
        client = FakeClient([
            (200, [{"id": "e1", "name": "zotero-rag-embedding"}]),  # GET endpoints
            (200, {}),  # DELETE endpoint
            (200, [{"id": "t1", "name": "zotero-rag-embedding"}]),  # GET templates
            (200, {}),  # DELETE template
        ])
        provision._teardown_resource(client, "rp_key", name="zotero-rag-embedding")
        methods = [c[0] for c in client.calls]
        self.assertEqual(methods, ["GET", "DELETE", "GET", "DELETE"])
        self.assertEqual(client.calls[1][1], "https://rest.runpod.io/v1/endpoints/e1")
        self.assertEqual(client.calls[3][1], "https://rest.runpod.io/v1/templates/t1")

    def test_noop_when_nothing_exists(self):
        client = FakeClient([(200, []), (200, [])])  # GET endpoints, GET templates — both empty
        provision._teardown_resource(client, "rp_key", name="zotero-rag-embedding")
        methods = [c[0] for c in client.calls]
        self.assertEqual(methods, ["GET", "GET"])  # no DELETE calls made


class ConfirmTeardownTest(unittest.TestCase):
    def test_skips_prompt_when_yes_flag_set(self):
        # Should not touch stdin/input() at all when yes=True.
        with patch("builtins.input", side_effect=AssertionError("should not prompt")):
            result = provision._confirm_teardown(
                resource_names=["zotero-rag-embedding", "zotero-rag-llm"], yes=True, interactive=True,
            )
        self.assertTrue(result)

    def test_refuses_when_noninteractive_without_yes(self):
        result = provision._confirm_teardown(
            resource_names=["zotero-rag-embedding", "zotero-rag-llm"], yes=False, interactive=False,
        )
        self.assertFalse(result)

    def test_prompts_and_honors_yes_answer(self):
        with patch("builtins.input", return_value="y") as mock_input:
            result = provision._confirm_teardown(
                resource_names=["zotero-rag-embedding", "zotero-rag-llm"], yes=False, interactive=True,
            )
        self.assertTrue(result)
        prompt_text = mock_input.call_args[0][0]
        self.assertIn("zotero-rag-embedding", prompt_text)
        self.assertIn("zotero-rag-llm", prompt_text)

    def test_prompts_and_honors_no_answer(self):
        with patch("builtins.input", return_value="n"):
            result = provision._confirm_teardown(
                resource_names=["zotero-rag-embedding", "zotero-rag-llm"], yes=False, interactive=True,
            )
        self.assertFalse(result)


class RunTest(unittest.TestCase):
    def test_teardown_path_calls_teardown_for_both_resources(self):
        client = FakeClient([
            (200, []), (200, []),  # embedding: GET endpoints, GET templates (nothing to delete)
            (200, []), (200, []),  # llm: GET endpoints, GET templates (nothing to delete)
        ])
        args = provision._parse_args(["--teardown", "--yes"])
        exit_code = provision._run(args, api_key="rp_key", client=client)
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(client.calls), 4)

    def test_teardown_path_aborts_without_confirmation(self):
        client = FakeClient([])
        args = provision._parse_args(["--teardown"])
        with patch("builtins.input", side_effect=AssertionError("should not prompt in this test")), \
             patch.object(provision.sys.stdin, "isatty", return_value=False):
            exit_code = provision._run(args, api_key="rp_key", client=client)
        self.assertEqual(exit_code, 1)
        self.assertEqual(len(client.calls), 0)  # nothing was even looked up

    def test_provision_path_creates_both_and_writes_env(self):
        client = FakeClient([
            (200, []),  # embedding: GET templates -> none
            (200, {"id": "t_emb", "name": provision.EMBEDDING_TEMPLATE_NAME,
                   "imageName": provision.EMBEDDING_IMAGE, "env": provision.EMBEDDING_ENV}),  # POST template
            (200, []),  # embedding: GET endpoints -> none
            (200, {"id": "e_emb", "name": provision.EMBEDDING_ENDPOINT_NAME}),  # POST endpoint
            (200, {"data": [{"embedding": [0.1]}]}),  # embedding warm-up
            (200, []),  # llm: GET templates -> none
            (200, {"id": "t_llm", "name": provision.LLM_TEMPLATE_NAME,
                   "imageName": provision.LLM_IMAGE, "env": {"MODEL_NAME": "Qwen/Qwen2.5-7B-Instruct"}}),  # POST template
            (200, []),  # llm: GET endpoints -> none
            (200, {"id": "e_llm", "name": provision.LLM_ENDPOINT_NAME}),  # POST endpoint
            (200, {"choices": [{"message": {"content": "hi"}}]}),  # llm warm-up
        ])
        args = provision._parse_args([])
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            with patch.object(provision, "ENV_PATH", env_path):
                exit_code = provision._run(args, api_key="rp_key", client=client)
            env_content = env_path.read_text()
        self.assertEqual(exit_code, 0)
        self.assertIn("RUNPOD_API_KEY=rp_key", env_content)
        self.assertIn("RUNPOD_EMBEDDING_BASE_URL=https://api.runpod.ai/v2/e_emb/openai/v1", env_content)
        self.assertIn("RUNPOD_LLM_BASE_URL=https://api.runpod.ai/v2/e_llm/openai/v1", env_content)

    def _success_responses(self):
        return [
            (200, []),
            (200, {"id": "t_emb", "name": provision.EMBEDDING_TEMPLATE_NAME,
                   "imageName": provision.EMBEDDING_IMAGE, "env": provision.EMBEDDING_ENV}),
            (200, []),
            (200, {"id": "e_emb", "name": provision.EMBEDDING_ENDPOINT_NAME}),
            (200, {"data": [{"embedding": [0.1]}]}),
            (200, []),
            (200, {"id": "t_llm", "name": provision.LLM_TEMPLATE_NAME,
                   "imageName": provision.LLM_IMAGE, "env": {"MODEL_NAME": "Qwen/Qwen2.5-7B-Instruct"}}),
            (200, []),
            (200, {"id": "e_llm", "name": provision.LLM_ENDPOINT_NAME}),
            (200, {"choices": [{"message": {"content": "hi"}}]}),
        ]

    def _run_capturing(self, argv):
        import contextlib
        import io
        client = FakeClient(self._success_responses())
        args = provision._parse_args(argv)
        buf = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(provision, "ENV_PATH", Path(tmp) / ".env"), \
                contextlib.redirect_stdout(buf):
            exit_code = provision._run(args, api_key="rp_key", client=client)
        return exit_code, buf.getvalue()

    def test_json_flag_prints_provision_result_line(self):
        import json
        exit_code, out = self._run_capturing(["--json"])
        self.assertEqual(exit_code, 0)
        lines = [l for l in out.splitlines() if l.startswith("PROVISION_RESULT:")]
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0][len("PROVISION_RESULT:"):]), {
            "RUNPOD_EMBEDDING_BASE_URL": "https://api.runpod.ai/v2/e_emb/openai/v1",
            "RUNPOD_LLM_BASE_URL": "https://api.runpod.ai/v2/e_llm/openai/v1",
        })
        self.assertIn("Embedding endpoint ready", out)  # human-readable output unchanged

    def test_no_json_flag_prints_no_result_line(self):
        exit_code, out = self._run_capturing([])
        self.assertEqual(exit_code, 0)
        self.assertNotIn("PROVISION_RESULT:", out)

    def test_provision_path_returns_1_on_http_error_instead_of_raising(self):
        client = FakeClient([
            (200, []),  # embedding: GET templates -> none
            (500, {"error": "internal error"}),  # POST template fails
        ])
        args = provision._parse_args([])
        exit_code = provision._run(args, api_key="rp_key", client=client)
        self.assertEqual(exit_code, 1)

    def test_provision_path_returns_1_on_connection_error_instead_of_raising(self):
        client = FakeClient([httpx.ConnectError("network unreachable")])
        args = provision._parse_args([])
        exit_code = provision._run(args, api_key="rp_key", client=client)
        self.assertEqual(exit_code, 1)


if __name__ == "__main__":
    unittest.main()


class PushToBackendTest(unittest.TestCase):
    VALUES = {"RUNPOD_EMBEDDING_BASE_URL": "https://api.runpod.ai/v2/e/openai/v1"}

    def test_posts_values_with_admin_key_header(self):
        client = FakeClient([(200, {"is_set": {}})])
        ok = provision._push_to_backend(client, "http://localhost:8119/", "zk", self.VALUES)
        self.assertTrue(ok)
        method, url, headers, body = client.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, "http://localhost:8119/api/config/remote-fields")
        self.assertEqual(headers["X-Zotero-API-Key"], "zk")
        self.assertEqual(body, {"values": self.VALUES})

    def test_omits_key_header_without_admin_key(self):
        client = FakeClient([(200, {})])
        self.assertTrue(provision._push_to_backend(client, "http://b", None, self.VALUES))
        self.assertNotIn("X-Zotero-API-Key", client.calls[0][2])

    def test_returns_false_on_rejection_and_hints_at_admin_key(self):
        client = FakeClient([(401, {"detail": "unauthorized"})])
        with self.assertLogs(provision.logger, level="WARNING") as logs:
            ok = provision._push_to_backend(client, "http://b", None, self.VALUES)
        self.assertFalse(ok)
        self.assertIn(provision.BACKEND_ADMIN_KEY_ENV, logs.output[0])

    def test_returns_false_on_connection_error(self):
        client = FakeClient([httpx.ConnectError("refused")])
        with self.assertLogs(provision.logger, level="WARNING"):
            self.assertFalse(provision._push_to_backend(client, "http://b", "zk", self.VALUES))


class RunBackendPushTest(unittest.TestCase):
    _success_responses = RunTest._success_responses

    def _run_with_backend(self, argv, backend_response, env=None):
        import contextlib
        import io
        client = FakeClient(self._success_responses() + [backend_response])
        args = provision._parse_args(argv)
        buf = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(provision, "ENV_PATH", Path(tmp) / ".env"), \
                patch.dict(provision.os.environ, env or {}, clear=False), \
                contextlib.redirect_stdout(buf):
            exit_code = provision._run(args, api_key="rp_key", client=client)
        return exit_code, buf.getvalue(), client

    def test_backend_url_applies_values_and_skips_curl_hint(self):
        exit_code, out, client = self._run_with_backend(
            ["--backend-url", "http://localhost:8119"], (200, {}),
            env={provision.BACKEND_ADMIN_KEY_ENV: "zk"},
        )
        self.assertEqual(exit_code, 0)
        method, url, headers, body = client.calls[-1]
        self.assertEqual(url, "http://localhost:8119/api/config/remote-fields")
        self.assertEqual(headers["X-Zotero-API-Key"], "zk")
        self.assertEqual(body["values"]["RUNPOD_LLM_BASE_URL"], "https://api.runpod.ai/v2/e_llm/openai/v1")
        self.assertIn("Applied the new endpoint URLs", out)
        self.assertNotIn("curl -X POST", out)

    def test_backend_rejection_falls_back_to_curl_hint_but_still_succeeds(self):
        with self.assertLogs(provision.logger, level="WARNING"):
            exit_code, out, _ = self._run_with_backend(
                ["--backend-url", "http://b"], (400, {"detail": "Unknown remote-config key(s)"}),
            )
        self.assertEqual(exit_code, 0)
        self.assertIn("curl -X POST", out)

    def test_backend_url_from_env(self):
        exit_code, out, client = self._run_with_backend(
            [], (200, {}), env={provision.BACKEND_URL_ENV: "http://envhost"},
        )
        self.assertEqual(client.calls[-1][1], "http://envhost/api/config/remote-fields")

    def test_json_mode_never_pushes_to_backend(self):
        client = FakeClient(self._success_responses())  # no extra response queued
        args = provision._parse_args(["--json", "--backend-url", "http://b"])
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(provision, "ENV_PATH", Path(tmp) / ".env"), \
                contextlib.redirect_stdout(io.StringIO()):
            exit_code = provision._run(args, api_key="rp_key", client=client)
        self.assertEqual(exit_code, 0)
        self.assertFalse(any("remote-fields" in c[1] for c in client.calls))


class ProvisioningKeyAndJsonModeTest(unittest.TestCase):
    def test_provisioning_key_takes_priority_over_runpod_api_key(self):
        env = {"PROVISIONING_API_KEY": "rpa_ONCE", "RUNPOD_API_KEY": "rpa_STORED"}
        self.assertEqual(provision._resolve_api_key(None, env=env), "rpa_ONCE")

    def test_json_mode_does_not_write_env_file(self):
        import contextlib
        import io
        client = FakeClient(RunTest._success_responses(None))
        args = provision._parse_args(["--json"])
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            with patch.object(provision, "ENV_PATH", env_path), \
                    contextlib.redirect_stdout(io.StringIO()):
                exit_code = provision._run(args, api_key="rp_key", client=client)
            self.assertEqual(exit_code, 0)
            self.assertFalse(env_path.exists())
