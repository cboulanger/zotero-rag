"""Unit tests for scripts/provision_runpod_endpoints.py."""

import importlib.util
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
