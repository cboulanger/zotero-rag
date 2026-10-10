"""bin/provision.py: the generic provisioning CLI."""

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_PATH = Path(__file__).resolve().parents[2] / "bin" / "provision.py"
_SPEC = importlib.util.spec_from_file_location("provision_cli", _PATH)
cli = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cli)

from backend.tests.test_provider_runpod import runpod_preset  # noqa: E402


def run(argv, preset=None, stored_key=None, prompt_value="typed-key"):
    from backend.providers import get_providers
    preset = preset or runpod_preset()
    providers = get_providers(preset)
    for p in providers.values():
        p.provision = MagicMock(return_value={})
        p.suspend = MagicMock()
        p.teardown = MagicMock()
    prompt = MagicMock(return_value=prompt_value)
    with patch.object(cli, "get_preset", return_value=preset), \
         patch.object(cli, "get_providers", return_value=providers), \
         patch.object(cli, "resolve_shared_value", return_value=stored_key), \
         patch.object(cli, "update_remote_config") as upd:
        code = cli.main(argv, prompt=prompt)
    return code, providers, prompt, upd


class ProvisionCliTest(unittest.TestCase):
    def test_provisions_both_sides_with_stored_key_and_no_prompt(self):
        code, providers, prompt, _ = run(["--preset", "runpod"], stored_key="stored")
        self.assertEqual(code, 0)
        prompt.assert_not_called()
        for p in providers.values():
            self.assertEqual(p.provision.call_args.args[0].credential, "stored")

    def test_prompts_when_no_stored_key_and_does_not_store_it(self):
        code, providers, prompt, upd = run(["--preset", "runpod", "--side", "llm"])
        self.assertEqual(code, 0)
        self.assertEqual(providers["llm"].provision.call_args.args[0].credential, "typed-key")
        providers["embedding"].provision.assert_not_called()
        upd.assert_not_called()

    def test_returned_values_are_stored(self):
        from backend.providers import get_providers
        preset = runpod_preset()
        with patch.object(cli, "get_preset", return_value=preset):
            providers = get_providers(preset)
            providers["llm"].provision = MagicMock(return_value={"RUNPOD_LLM_BASE_URL": "https://x"})
            providers["embedding"].provision = MagicMock(return_value={})
            with patch.object(cli, "get_providers", return_value=providers), \
                 patch.object(cli, "resolve_shared_value", return_value="k"), \
                 patch.object(cli, "update_remote_config") as upd:
                self.assertEqual(cli.main(["--preset", "runpod"]), 0)
        upd.assert_called_once()

    def test_pause_and_teardown(self):
        code, providers, _, _ = run(["--preset", "runpod", "--pause"], stored_key="k")
        self.assertEqual(code, 0)
        providers["llm"].suspend.assert_called_once()
        code, providers, _, _ = run(["--preset", "runpod", "--teardown", "--yes"], stored_key="k")
        providers["llm"].teardown.assert_called_once()

    def test_teardown_needs_confirmation(self):
        with patch("builtins.input", return_value="n"):
            code, providers, _, _ = run(["--preset", "runpod", "--teardown"], stored_key="k")
        self.assertEqual(code, 1)
        providers["llm"].teardown.assert_not_called()

    def test_a_failing_side_gives_exit_1_but_other_side_still_runs(self):
        from backend.providers import get_providers
        preset = runpod_preset()
        providers = get_providers(preset)
        providers["embedding"].provision = MagicMock(side_effect=RuntimeError("boom"))
        providers["llm"].provision = MagicMock(return_value={})
        with patch.object(cli, "get_preset", return_value=preset), \
             patch.object(cli, "get_providers", return_value=providers), \
             patch.object(cli, "resolve_shared_value", return_value="k"):
            self.assertEqual(cli.main(["--preset", "runpod"]), 1)
        providers["llm"].provision.assert_called_once()

    def test_unprovisionable_preset_is_rejected(self):
        with patch.object(cli, "get_preset", side_effect=ValueError("nope")):
            self.assertEqual(cli.main(["--preset", "zzz"]), 2)


if __name__ == "__main__":
    unittest.main()
