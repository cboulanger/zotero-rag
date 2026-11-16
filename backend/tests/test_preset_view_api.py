"""GET /api/config/preset-view: the "Configure preset" dialog's view model."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from backend.config.presets import ensure_default_presets
from backend.config.settings import get_settings, reset_settings
from backend.providers.types import Health
from backend.services import provisioning
from backend.services.preset_view import Probe
from backend.services.side_probe import SideProbe
from backend.services.zotero_identity import ZoteroIdentity

UNPROVISIONED = Health(status="unreachable", detail="not provisioned")


class PresetViewApiTest(unittest.TestCase):
    def setUp(self):
        from backend.main import app
        reset_settings()
        provisioning.reset_job_state()
        self.addCleanup(provisioning.reset_job_state)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(reset_settings)
        self.settings = get_settings()
        self.settings.data_path = Path(self.tmp.name)
        self.settings.qdrant_url = None
        ensure_default_presets(self.settings.data_path)
        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def _get(self, preset, headers=None, admin=False, loopback=True, health=UNPROVISIONED, probe=Probe(ok=True), user_id=5):
        self.settings.model_preset = preset
        if not loopback:
            self.settings.api_host = "rag.example.com"
            self.settings.authorized_group_id = 999
        ident = ZoteroIdentity(user_id=user_id, username="u", targets=[f"users/{user_id}"])
        with patch("backend.main.resolve_zotero_identity", new=AsyncMock(return_value=ident)), \
             patch("backend.zotero.group_roles.is_group_admin", new=AsyncMock(return_value=admin)), \
             patch("backend.providers.runpod.RunPodProvider.health", return_value=health), \
             patch("backend.api.config._probe", new=AsyncMock(return_value=probe)):
            r = self.client.get("/api/config/preset-view", headers={"X-Zotero-API-Key": "Z", **(headers or {})})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    @staticmethod
    def _side(body, name):
        return next(s for s in body["sides"] if s["side"] == name)

    def test_sides_follow_the_pipeline_order(self):
        body = self._get("cpu-only")
        self.assertEqual([s["side"] for s in body["sides"]], ["extraction", "embedding", "retrieval", "rerank", "llm"])
        self.assertEqual(body["preset"]["name"], "cpu-only")
        no_rerank = self._get("remote-kisski", headers={"X-Kisski-Api-Key": "k"})
        self.assertNotIn("rerank", [s["side"] for s in no_rerank["sides"]])

    def test_local_models_are_ready_and_need_no_account(self):
        body = self._get("cpu-only")
        self.assertEqual(self._side(body, "embedding")["state"], "ready")
        self.assertEqual(self._side(body, "llm")["state"], "ready")
        self.assertEqual(body["accounts"], [])

    def test_unprovisioned_endpoints_are_down_with_a_start_action_and_one_account_card(self):
        body = self._get("runpod", headers={"X-Runpod-Api-Key": "rpa_secretvalue"})
        for name in ("embedding", "llm"):
            side = self._side(body, name)
            self.assertEqual((side["state"], side["account"]), ("down", "runpod"))
            start = next(a for a in side["actions"] if a["workflow"] == "start")
            self.assertEqual(start["request"], {"method": "POST", "path": "/api/config/provision", "body": {"sides": [name]}})
            self.assertEqual(start["account"], "runpod")
        self.assertEqual(len(body["accounts"]), 1)
        card = body["accounts"][0]
        self.assertEqual((card["id"], card["serves"], card["state"]), ("runpod", ["embedding", "rerank", "llm"], "ready"))
        storages = {f["id"]: (f["storage"], f["is_set"]) for f in card["fields"]}
        self.assertEqual(storages["RUNPOD_API_KEY"], ("client_secret", True))
        self.assertEqual(storages["session:RUNPOD_API_KEY"][0], "session")

    def test_a_missing_personal_key_marks_the_card(self):
        body = self._get("runpod")
        card = body["accounts"][0]
        self.assertEqual(card["state"], "needs_input")
        self.assertTrue(card["messages"])

    def test_a_ready_endpoint_offers_pause(self):
        body = self._get("runpod", headers={"X-Runpod-Api-Key": "rpa_x"}, health=Health(status="ready"))
        side = self._side(body, "llm")
        self.assertEqual(side["state"], "ready")
        self.assertEqual([a["id"] for a in side["actions"]], ["pause"])
        self.assertEqual(side["actions"][0]["request"]["path"], "/api/config/suspend")

    def test_a_running_job_shows_starting_and_its_log(self):
        provisioning.mark_running(["llm"], provisioning.user_slot(5))
        provisioning._add_progress(provisioning.user_slot(5), "llm", "creating endpoint")
        body = self._get("runpod", headers={"X-Runpod-Api-Key": "rpa_x"})
        side = self._side(body, "llm")
        self.assertEqual(side["state"], "starting")
        self.assertEqual(side["log"], ["creating endpoint"])
        self.assertTrue(body["busy"])
        self.assertFalse(next(a for a in side["actions"] if a["id"] == "start")["enabled"])

    SHARED_PRESET = {
        "version": 2,
        "description": "Admin-shared gateway (test)",
        "embedding": {"model_type": "remote", "model_name": "e5", "batch_size": 8,
                      "provider": {"id": "generic", "scope": "shared"},
                      "model_kwargs": {"shared_base_url_env": "TEST_EMB_URL", "shared_api_key_env": "TEST_EMB_API_KEY"}},
        "llm": {"model_type": "remote", "model_names": ["m"], "max_context_length": 8192,
                "provider": {"id": "generic", "scope": "shared"},
                "model_kwargs": {"shared_base_url_env": "TEST_LLM_URL", "shared_api_key_env": "TEST_LLM_API_KEY"}},
        "rag": {"top_k": 10, "score_threshold": 0.35, "max_chunk_size": 800},
        "memory_budget_gb": 0.5, "platform": "any",
    }

    def _write_shared_preset(self):
        (self.settings.data_path / "presets" / "shared-test.json").write_text(json.dumps(self.SHARED_PRESET))

    def test_shared_credentials_are_read_only_for_non_admins(self):
        self._write_shared_preset()
        body = self._get("shared-test", loopback=False, user_id=6)  # admin role is cached per user
        card = body["accounts"][0]
        self.assertEqual(card["serves"], ["embedding", "llm"])
        self.assertTrue(all(f["storage"] == "shared" and not f["editable"] for f in card["fields"]))
        self.assertEqual(card["state"], "needs_input")
        self.assertEqual(body["preset"]["default_options"], [])

    def test_admins_can_edit_shared_credentials_and_get_the_default_chooser(self):
        self._write_shared_preset()
        body = self._get("shared-test", admin=True, loopback=False, user_id=7)
        self.assertTrue(all(f["editable"] for f in body["accounts"][0]["fields"]))
        self.assertTrue(body["preset"]["is_admin"])
        self.assertIn(body["preset"]["default_preset"], body["preset"]["default_options"])

    def _get_mpcdf(self, session, health=UNPROVISIONED):
        with patch("backend.providers.mpcdf.MpcdfProvider.session_status", return_value=session), \
             patch("backend.providers.mpcdf.MpcdfProvider.health", return_value=health):
            return self._get("remote-mpcdf", user_id=8)

    def test_session_provider_card_offers_the_login_form_until_logged_in(self):
        body = self._get_mpcdf({"state": "login_required"})
        card = body["accounts"][0]
        self.assertEqual(card["id"], "mpcdf")
        self.assertEqual(card["serves"], ["embedding", "rerank", "llm"])
        self.assertEqual(card["state"], "needs_input")
        self.assertFalse(card["auto_start"])
        logins = [f["key"] for f in card["fields"] if f["storage"] == "login"]
        self.assertEqual(logins, ["username", "password", "otp", "hf_token"])
        login = next(a for a in card["actions"] if a["sends_login"])
        self.assertEqual(login["request"]["path"], "/api/config/session/login")
        self.assertIn("side", login["request"]["body"])
        # Starting needs the login first.
        start = next(a for a in self._side(body, "llm")["actions"] if a["id"] == "start")
        self.assertFalse(start["enabled"])
        self.assertIn("Log in", start["reason"])

    def test_logged_in_session_card_shows_logout_and_enables_start(self):
        body = self._get_mpcdf({"state": "ok", "username": "cb", "expires_at": "2026-11-16T08:00:00Z", "hf_token_set": True})
        card = body["accounts"][0]
        self.assertEqual(card["state"], "ready")
        self.assertFalse([f for f in card["fields"] if f["storage"] == "login"])
        self.assertEqual([a["id"] for a in card["actions"]], ["logout"])
        self.assertIn("cb", card["messages"][0]["text"])
        start = next(a for a in self._side(body, "embedding")["actions"] if a["id"] == "start")
        self.assertTrue(start["enabled"])
        self.assertEqual(start["workflow"], "start")

    def test_session_status_failure_falls_back_to_the_login_form(self):
        with patch("backend.providers.mpcdf.MpcdfProvider.session_status", side_effect=RuntimeError("down")), \
             patch("backend.providers.mpcdf.MpcdfProvider.health", return_value=UNPROVISIONED):
            body = self._get("remote-mpcdf", user_id=9)
        self.assertEqual(body["accounts"][0]["state"], "needs_input")

    def test_remote_reranker_is_an_optional_tab_left_out_of_start_all(self):
        body = self._get("runpod", headers={"X-Runpod-Api-Key": "rpa_x"})
        self.assertEqual([s["side"] for s in body["sides"]], ["extraction", "embedding", "retrieval", "rerank", "llm"])
        rerank = self._side(body, "rerank")
        self.assertTrue(rerank["optional"])
        self.assertEqual(rerank["state"], "down")
        toggle = rerank["fields"][0]
        self.assertEqual((toggle["type"], toggle["storage"], toggle["key"], toggle["default"]),
                         ("toggle", "client_pref", "enableReranking", True))
        start = next(a for a in rerank["actions"] if a["id"] == "start")
        self.assertIsNone(start["workflow"])
        self.assertEqual(start["request"]["body"], {"sides": ["rerank"]})
        self.assertIn("rerank", body["accounts"][0]["serves"])

    def test_remote_reranker_pause_joins_pause_all(self):
        body = self._get("runpod", headers={"X-Runpod-Api-Key": "rpa_x"}, health=Health(status="ready"))
        pause = next(a for a in self._side(body, "rerank")["actions"] if a["id"] == "pause")
        self.assertEqual(pause["workflow"], "pause")

    def test_local_reranker_runs_on_the_server(self):
        body = self._get("cpu-only")
        rerank = self._side(body, "rerank")
        self.assertEqual((rerank["state"], rerank["optional"]), ("ready", True))
        facts = {f["label"]: f["value"] for f in rerank["facts"]}
        self.assertIn("mMiniLM", facts["Model"])
        self.assertIn("30", facts["Rescored"])

    def test_docling_extraction_joins_the_account_of_its_key(self):
        body = self._get("kisski-docling")
        card = body["accounts"][0]
        self.assertEqual(card["serves"], ["extraction", "embedding", "llm"])
        self.assertEqual(self._side(body, "extraction")["state"], "down")
        # No status check and no key: the side cannot work.
        self.assertEqual(self._side(body, "llm")["state"], "down")
        with patch("backend.services.side_probe.probe_side", new=AsyncMock(return_value=SideProbe())):
            with_key = self._get("kisski-docling", headers={"X-Kisski-Api-Key": "k"})
        self.assertEqual(self._side(with_key, "llm")["state"], "unknown")
        self.assertEqual(with_key["accounts"][0]["state"], "ready")

    def test_unreachable_extraction_service_is_down(self):
        self.settings.extractor_backend = "kreuzberg"
        body = self._get("cpu-only", probe=Probe(ok=False, detail="not reachable (ConnectError)"))
        side = self._side(body, "extraction")
        self.assertEqual(side["state"], "down")
        self.assertIn("not reachable", side["summary"])

    def test_retrieval_tuning_fields_are_client_preferences_with_server_defaults(self):
        from backend.services import rag_engine

        body = self._get("cpu-only")
        fields = {f["id"]: f for f in self._side(body, "retrieval")["fields"]}
        self.assertEqual(fields["diversityFloor"]["storage"], "client_pref")
        self.assertEqual(fields["diversityFloor"]["default"], rag_engine._DIVERSITY_FLOOR)
        self.assertEqual(fields["maxChunksPerDocument"]["default"], rag_engine._MAX_CHUNKS_PER_DOCUMENT)
        self.assertEqual(fields["top_k"]["type"], "readonly")

    def test_retrieval_tuning_matches_the_plugin_list(self):
        """The plugin sends these preferences with each query (DIVERSITY_TUNING_FIELDS)."""
        from backend.services.preset_view import RETRIEVAL_TUNING

        source = (Path(__file__).resolve().parents[2] / "plugin" / "src" / "zotero-rag.js").read_text()
        for pref, *_ in RETRIEVAL_TUNING:
            self.assertIn(f"prefKey: '{pref}'", source)

    def test_no_secret_value_is_echoed(self):
        body = self._get("runpod", headers={"X-Runpod-Api-Key": "rpa_secretvalue"})
        self.assertNotIn("rpa_secretvalue", json.dumps(body))

    # -- test requests, quota and demand -------------------------------------------

    def _kisski(self, probe, availability=None, headers=None):
        mock = AsyncMock(return_value=probe)
        with patch("backend.services.side_probe.probe_side", new=mock), \
             patch("backend.providers.kisski.KisskiProvider.model_availability", return_value=availability):
            body = self._get("remote-kisski", headers={"X-Kisski-Api-Key": "k"} if headers is None else headers)
        return body, mock

    def test_a_side_without_status_check_is_ready_when_a_test_request_answers(self):
        body, mock = self._kisski(SideProbe(ok=True))
        side = self._side(body, "embedding")
        self.assertEqual(side["state"], "ready")
        self.assertIn("test request", side["summary"])
        self.assertTrue(all(call.kwargs["send"] for call in mock.await_args_list))

    def test_a_failed_test_request_marks_the_side_down_with_the_reason(self):
        body, _ = self._kisski(SideProbe(ok=False, detail="HTTP 401: invalid key"))
        side = self._side(body, "llm")
        self.assertEqual(side["state"], "down")
        self.assertIn("HTTP 401", side["summary"])

    def test_the_test_request_uses_a_model_the_key_can_use(self):
        # The preset's first model is not in the key's live list; the test and the tab use the live default.
        from backend.providers.types import ModelInfo

        live = [ModelInfo(id="gemma-4-31b-it", demand=0, availability="available")]
        with patch("backend.api.config._live_llm_models", return_value=live):
            body, mock = self._kisski(SideProbe(ok=True, model="gemma-4-31b-it"))
        llm_calls = [c for c in mock.await_args_list if c.args[1] == "llm"]
        self.assertEqual(llm_calls[0].kwargs["model"], "gemma-4-31b-it")
        embedding_calls = [c for c in mock.await_args_list if c.args[1] == "embedding"]
        self.assertIsNone(embedding_calls[0].kwargs["model"])
        self.assertIn({"label": "Model", "value": "gemma-4-31b-it"}, self._side(body, "llm")["facts"])

    def test_no_test_request_while_the_key_is_missing(self):
        body, mock = self._kisski(SideProbe(), headers={})
        self.assertFalse(any(call.kwargs["send"] for call in mock.await_args_list))
        self.assertEqual(self._side(body, "llm")["state"], "down")

    def test_kisski_quota_combines_hour_and_day_and_shows_on_the_card(self):
        meters = [
            {"id": "requests/hour", "side": "llm", "unit": "requests", "period": "hour", "limit": 100, "remaining": 90},
            {"id": "requests/day", "side": "llm", "unit": "requests", "period": "day", "limit": 1000, "remaining": 200},
            {"id": "requests/month", "side": "llm", "unit": "requests", "period": "month", "limit": 10, "remaining": 0},
        ]
        body, _ = self._kisski(SideProbe(ok=True, meters=meters))
        quota = self._side(body, "llm")["quota"]
        self.assertEqual((quota["used_pct"], quota["level"]), (80, "warn"))
        self.assertIn("daily", quota["text"])
        self.assertIn("hourly and daily quota combined", quota["text"])
        self.assertEqual(body["accounts"][0]["quota"]["used_pct"], 80)

    def test_exhausted_quota_marks_the_side_down(self):
        meters = [{"id": "requests/hour", "side": "llm", "unit": "requests", "period": "hour", "limit": 100, "remaining": 0}]
        body, _ = self._kisski(SideProbe(ok=True, meters=meters))
        self.assertEqual(self._side(body, "llm")["state"], "down")
        self.assertEqual(body["accounts"][0]["quota"]["level"], "error")

    def _quota_view(self):
        probe = SideProbe(ok=False, detail="quota exhausted until 23:00 UTC", rate_limited=True)
        with patch("backend.services.side_probe.probe_side", new=AsyncMock(return_value=probe)), \
             patch("backend.providers.mpcdf.MpcdfProvider.session_status", return_value={"state": "ok"}):
            return self._get("kisski-rerank-mpcdf", headers={"X-Kisski-Api-Key": "k"})

    def test_an_exhausted_quota_is_reported_with_a_standin_action(self):
        side = self._side(self._quota_view(), "embedding")
        self.assertEqual(side["state"], "down")
        texts = [m["text"] for m in side["messages"]]
        self.assertIn("KISSKI's embedding quota is used up until 23:00 UTC.", texts)
        self.assertNotIn("Check the credentials in the account card below.", texts)
        action = next(a for a in side["actions"] if a["id"] == "standin")
        self.assertEqual((action["label"], action["enabled"]), ("Use a stand-in until 23:00", True))
        self.assertIsNone(action["request"])

    def test_kisski_card_only_saves_and_others_keep_auto_start(self):
        body = self._quota_view()
        cards = {c["id"]: c for c in body["accounts"]}
        self.assertFalse(cards["kisski"]["auto_start"])
        self.assertFalse(cards["mpcdf"]["auto_start"])
        runpod = self._get("runpod", headers={"X-Runpod-Api-Key": "rpa_x"})
        self.assertTrue(runpod["accounts"][0]["auto_start"])

    def test_a_very_busy_model_is_reported(self):
        body, _ = self._kisski(SideProbe(ok=True), availability="very busy")
        side = self._side(body, "llm")
        self.assertIn({"label": "Demand", "value": "very busy"}, side["facts"])
        self.assertTrue(any("very busy" in m["text"] for m in side["messages"]))


if __name__ == "__main__":
    unittest.main()
