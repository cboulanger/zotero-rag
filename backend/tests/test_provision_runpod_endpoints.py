"""Unit tests for scripts/provision_runpod_endpoints.py."""

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

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
            (200, [existing]),  # GET -> found, mismatched
            (200, {}),  # DELETE /templates/t_old
            (200, {"id": "t_new", "name": "zotero-rag-embedding", "imageName": "img:NEW", "env": {"A": "1"}}),  # POST
        ])
        result = provision._ensure_template(
            client, "rp_key", name="zotero-rag-embedding", image="img:NEW",
            env={"A": "1"}, container_disk_gb=20, recreate=True,
        )
        self.assertEqual(result["id"], "t_new")
        delete_call = client.calls[1]
        self.assertEqual(delete_call[0], "DELETE")
        self.assertEqual(delete_call[1], "https://rest.runpod.io/v1/templates/t_old")


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
        provision._warm_up_embedding(client, "rp_key", "https://api.runpod.ai/v2/e1/openai/v1")
        method, url, headers, body = client.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, "https://api.runpod.ai/v2/e1/openai/v1/embeddings")
        self.assertEqual(headers["Authorization"], "Bearer rp_key")
        self.assertEqual(body["input"], "ping")
        self.assertEqual(body["model"], provision.EMBEDDING_MODEL)

    def test_llm_warmup_posts_to_chat_completions_path(self):
        client = FakeClient([(200, {"choices": [{"message": {"content": "hi"}}]})])
        provision._warm_up_llm(client, "rp_key", "https://api.runpod.ai/v2/l1/openai/v1", "Qwen/Qwen2.5-7B-Instruct")
        method, url, headers, body = client.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, "https://api.runpod.ai/v2/l1/openai/v1/chat/completions")
        self.assertEqual(body["model"], "Qwen/Qwen2.5-7B-Instruct")
        self.assertEqual(body["max_tokens"], 1)

    def test_retries_on_failure_then_succeeds(self):
        client = FakeClient([(503, {"error": "cold starting"}), (200, {"data": [{"embedding": [0.1]}]})])
        with patch.object(provision.time, "sleep"):
            provision._warm_up_embedding(client, "rp_key", "https://api.runpod.ai/v2/e1/openai/v1")
        self.assertEqual(len(client.calls), 2)

    def test_gives_up_after_max_seconds_and_warns(self):
        # Every call fails; the retry loop must stop instead of looping forever.
        responses = [(503, {"error": "cold starting"})] * 50
        client = FakeClient(responses)
        fake_times = iter([0, 10, 50, 100, 200])  # exceeds WARMUP_MAX_SECONDS=180 on the 4th deadline check
        with patch.object(provision.time, "sleep"), \
             patch.object(provision.time, "monotonic", side_effect=lambda: next(fake_times)), \
             self.assertLogs(provision.logger, level="WARNING") as ctx:
            provision._warm_up_embedding(client, "rp_key", "https://api.runpod.ai/v2/e1/openai/v1")
        self.assertTrue(any("did not warm up" in msg for msg in ctx.output))

    def test_retries_on_connection_error_then_succeeds(self):
        client = FakeClient([httpx.ReadTimeout("timed out"), (200, {"data": [{"embedding": [0.1]}]})])
        with patch.object(provision.time, "sleep"):
            provision._warm_up_embedding(client, "rp_key", "https://api.runpod.ai/v2/e1/openai/v1")
        self.assertEqual(len(client.calls), 2)

    def test_short_circuits_on_non_retryable_4xx(self):
        client = FakeClient([(400, {"error": "bad model name"})])
        with patch.object(provision.time, "sleep") as mock_sleep:
            provision._warm_up_embedding(client, "rp_key", "https://api.runpod.ai/v2/e1/openai/v1")
        self.assertEqual(len(client.calls), 1)  # no retry attempted
        mock_sleep.assert_not_called()

    def test_retries_429_as_transient_not_as_non_retryable(self):
        client = FakeClient([(429, {"error": "rate limited"}), (200, {"data": [{"embedding": [0.1]}]})])
        with patch.object(provision.time, "sleep"):
            provision._warm_up_embedding(client, "rp_key", "https://api.runpod.ai/v2/e1/openai/v1")
        self.assertEqual(len(client.calls), 2)


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


if __name__ == "__main__":
    unittest.main()
