"""Tests for the rag-quality-eval skill (stdlib unittest, no backend or network needed).

Run:  python -m unittest discover -s skills/rag-quality-eval/tests -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import gold_tools  # noqa: E402
import judge  # noqa: E402
import report  # noqa: E402
import run_eval  # noqa: E402
import scoring  # noqa: E402
from common import load_gold  # noqa: E402

GOLD = load_gold()
Q = {q["id"]: q for q in GOLD["questions"]}


def make_response(question: dict, answer: str, n_sources: int | None = None, with_trace: bool = True) -> dict:
    """A minimal /api/query response whose sources are the question's gold documents."""
    sources = []
    for doc_id in question["documents"]:
        doc = GOLD["documents"][doc_id]
        sources.append({"item_id": doc["item_keys"][0], "library_id": "6297749", "title": doc["title"],
                        "page_number": 3, "text_anchor": "x", "relevance_score": 0.8})
    sources = sources[: n_sources if n_sources is not None else len(sources)]
    context = "\n".join(f"[S{i + 1}: {s['title']}]\n" + " ".join(f["text"] for f in question["facts"])
                        for i, s in enumerate(sources))
    trace = {
        "query_id": "t", "timestamp_start": "x", "question": question["question"], "library_ids": ["6297749"],
        "parameters": {}, "total_duration_ms": 1234,
        "agent_executions": [{"agent_name": "rag", "context_text": context, "sources_count": len(sources),
                              "duration_ms": 10, "retrieval": {"embedding_model": "m", "embedding_dims": 3,
                                                              "search_params": {}, "raw_results_count": 10,
                                                              "score_stats": {"max": 0.9}, "documents_grouped": len(sources),
                                                              "escalated": False,
                                                              "chunks": [{"item_key": s["item_id"], "title": s["title"], "score": 0.8}
                                                                         for s in sources]}}],
        "llm_calls": [{"call_type": "rag_generation", "model": "m", "prompt": "p" * 100, "response": answer,
                       "temperature": 0.7, "max_tokens": 10, "duration_ms": 5, "timestamp": "x"}],
    }
    return {"question": question["question"], "answer": f"<p>{answer}</p>", "answer_format": "html",
            "sources": sources, "library_ids": ["6297749"], "model_name": "m", "agents_used": ["rag"],
            "trace": trace if with_trace else None, "status": "complete"}


class GoldFileTests(unittest.TestCase):
    def test_gold_is_valid(self):
        self.assertEqual(gold_tools.validate(GOLD), [])

    def test_ten_questions_easy_to_hard_with_languages(self):
        self.assertEqual(len(GOLD["questions"]), 10)
        self.assertEqual([q["difficulty"] for q in GOLD["questions"]], list(range(1, 11)))
        self.assertGreaterEqual({q["language"] for q in GOLD["questions"]} - {"en"}, {"de", "fr", "es"})

    def test_hard_questions_define_inference_facts(self):
        for q in GOLD["questions"]:
            if q["tier"] == "hard":
                self.assertTrue(q["inference"])
                self.assertTrue(any(f["level"] == "inference" for f in q["facts"]), q["id"])

    def test_formatter_roundtrip_is_lossless(self):
        self.assertEqual(json.loads(gold_tools.format_gold(GOLD)), GOLD)


class ScoringTests(unittest.TestCase):
    def test_backend_citation_regex_matches_fallback(self):
        try:
            from backend.services import rag_engine
        except Exception:
            self.skipTest("backend not importable")
        self.assertEqual(rag_engine._SN_CITATION_PATTERN.pattern, scoring.CITATION_RE.pattern)
        self.assertEqual(rag_engine.CONTEXT_INSUFFICIENT_MARKER, scoring.CONTEXT_INSUFFICIENT_MARKER)

    def test_citation_format_violations_are_detected(self):
        text = ("EndNote found 47% [S1:p.3]. Zotero found 52% [S1:305-306]. It was faster [1]. "
                "See Source 2 and S3 for details. Fine claim here is cited properly [S1:3,S2].")
        res = scoring.score_citations(text, n_sources=2)
        self.assertEqual(set(res["malformed_citations"]), {"page_prefix", "page_range", "numeric_bracket",
                                                           "spelled_source", "bare_sn"})
        self.assertEqual(res["dangling_citations"], [])  # S3 is bare, not a bracketed citation

    def test_dangling_citation_and_coverage(self):
        text = "Zotero retrieved fifty two percent of the available records [S4]. This second sentence has no citation at all."
        res = scoring.score_citations(text, n_sources=2)
        self.assertEqual(res["dangling_citations"], [4])
        self.assertLess(res["citation_compliance"], 0.5)
        self.assertEqual(scoring.score_citations("A long uncited claim about many reference managers.", 1)["citation_compliance"], 0.0)

    def test_citation_after_full_stop_counts(self):
        text = "Zotero retrieved 52% of the available full texts in the study. [S1]"
        self.assertEqual(scoring.score_citations(text, 1)["citation_coverage"], 1.0)

    def test_language_detection(self):
        self.assertEqual(scoring.detect_language("The study found that the tools were not equal in their results and also differed."), "en")
        self.assertEqual(scoring.detect_language("Die Studie zeigt, dass die Werkzeuge nicht gleich sind und auch von der Nutzung abhängen."), "de")
        self.assertEqual(scoring.detect_language("Les résultats sont dans une étude qui montre que les outils ne sont pas les mêmes pour cette analyse."), "fr")
        self.assertEqual(scoring.detect_language("El estudio muestra que los resultados son los mismos para los usuarios de esta herramienta y sus datos."), "es")
        self.assertEqual(scoring.detect_language("ok"), "unknown")

    def test_number_grounding_handles_locale_formats(self):
        res = scoring.score_grounded_numbers("Es waren 1.613 Artikel, davon 79,5 % und 4,8 %.", "1,613 papers; 79.5% used; 4.8% reported")
        self.assertEqual(res["ungrounded_numbers"], [])
        res = scoring.score_grounded_numbers("It was 99% accurate with 1,200 papers.", "1,613 papers")
        self.assertEqual(res["ungrounded_numbers"], ["1200", "99"])

    def test_fact_patterns_levels_and_judge_only(self):
        q = Q["Q09"]
        res = scoring.score_facts("Zotero provides no API [S1].", "", q["facts"])
        by_id = {d["id"]: d for d in res["facts"]}
        self.assertTrue(by_id["zotero_no_api"]["in_answer"])
        self.assertTrue(by_id["inference_access_not_popularity"]["judge_only"])
        self.assertNotIn("inference", res["recall_by_level"])  # judge-only facts are not counted automatically

    def test_good_answer_scores_high_and_bad_answer_fails(self):
        q = Q["Q01"]
        good = make_response(q, "EndNote retrieved 47% of the available full texts while Zotero retrieved 52% [S1]. "
                                "Zotero was also faster than EndNote by 2 minutes 15 seconds on average per dataset [S1:4].")
        res = scoring.score_run(good, q, GOLD)
        self.assertEqual(res["fact_recall"], 1.0)
        self.assertEqual(res["verdict"], "pass", res)
        bad = make_response(q, "I will look through the sources. Reference managers are useful tools for researchers everywhere.")
        res = scoring.score_run(bad, q, GOLD)
        self.assertEqual(res["verdict"], "fail")
        self.assertIn("process_narration", res["hygiene_flags"])

    def test_retrieval_vs_generation_diagnosis(self):
        q = Q["Q01"]
        resp = make_response(q, "Reference managers retrieve some full texts [S1].")
        resp["trace"]["agent_executions"][0]["context_text"] = "EndNote retrieved 47% of available full texts"
        res = scoring.score_run(resp, q, GOLD)
        self.assertIn("endnote_47", res["missed_generation"])      # in the context, not in the answer
        self.assertIn("zotero_52", res["missed_retrieval"])        # never reached the LLM

    def test_source_matching_by_key_and_by_title(self):
        q = Q["Q02"]  # Lorenzetti has two library items with the same title
        resp = make_response(q, "x [S1]")
        resp["sources"][0]["item_id"] = "GQMHNT7V"
        self.assertEqual(scoring.score_sources(resp, q, GOLD)["expected_doc_recall"], 1.0)
        resp["sources"][0]["item_id"] = "UNKNOWN"
        self.assertEqual(scoring.score_sources(resp, q, GOLD)["expected_doc_recall"], 1.0)  # title fallback
        resp["sources"][0]["title"] = "Something else"
        self.assertEqual(scoring.score_sources(resp, q, GOLD)["gold_docs_missing"], ["lorenzetti2013"])


class JudgmentTests(unittest.TestCase):
    def _judged(self, verdicts: dict, **extra):
        q = Q["Q09"]
        resp = make_response(q, "Zotero offers no API and no registration, so its reader counts cannot be collected [S1,S2]. "
                                "Mendeley exposes disciplines and status through its API [S2].")
        auto = scoring.score_run(resp, q, GOLD)
        facts = {f["id"]: {"verdict": verdicts.get(f["id"], "missing"), "attribution_ok": True, "evidence": ""}
                 for f in q["facts"]}
        judgment = {"facts": facts, "unsupported_claims": extra.get("unsupported", []), "attribution_errors": [],
                    "citation_support": 0.8, "inference_quality": extra.get("inference", 0.5), "overall": 3.5, "notes": ""}
        return q, resp, auto, judgment

    def test_incomplete_judgment_is_rejected(self):
        q = Q["Q09"]
        self.assertTrue(scoring.validate_judgment({"facts": {}}, q))

    def test_inferred_and_semantic_hits_count_and_regex_errors_are_reported(self):
        q, resp, auto, judgment = self._judged({
            "zotero_no_usercounts": "semantic", "zotero_no_api": "verbatim", "mendeley_api_metadata": "verbatim",
            "correlation": "missing", "inference_access_not_popularity": "inferred"})
        self.assertEqual(scoring.validate_judgment(judgment, q), [])
        res = scoring.apply_judgment(auto, judgment, q, resp)
        self.assertTrue(res["judged"])
        self.assertAlmostEqual(res["fact_recall"], 4 / 5)
        self.assertEqual(res["recall_by_level"]["inference"], 1.0)
        self.assertIn("inference_access_not_popularity", [d["id"] for d in res["facts_judged"]])

    def test_attribution_error_removes_credit_and_false_positive_is_flagged(self):
        q, resp, auto, judgment = self._judged({"zotero_no_api": "verbatim", "mendeley_api_metadata": "verbatim"})
        judgment["facts"]["zotero_no_api"]["attribution_ok"] = False
        res = scoring.apply_judgment(auto, judgment, q, resp)
        self.assertIn("zotero_no_api", res["attribution_errors"])
        self.assertIn("zotero_no_api", res["regex_false_positives"])  # regex matched, judge says no credit

    def test_unsupported_claims_reduce_groundedness_and_composite(self):
        q, resp, auto, judgment = self._judged({f["id"]: "verbatim" for f in q_facts()}, unsupported=[])
        clean = scoring.apply_judgment(dict(auto), judgment, q, resp)
        q, resp, auto, judgment = self._judged({f["id"]: "verbatim" for f in q_facts()}, unsupported=["a", "b"])
        dirty = scoring.apply_judgment(dict(auto), judgment, q, resp)
        self.assertLess(dirty["groundedness"], clean["groundedness"])
        self.assertLess(dirty["composite"], clean["composite"])


def q_facts():
    return Q["Q09"]["facts"]


class MockBackend(BaseHTTPRequestHandler):
    """Just enough of the backend API for run_eval/judge/report."""
    state = {"preset": "remote-kisski", "switched": []}

    def log_message(self, *a):  # silence
        pass

    def _send(self, code: int, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/api/config":
            st = self.state
            self._send(200, {
                "preset_name": st["preset"], "preset_description": "", "api_version": "1", "embedding_model": "e",
                "embedding_model_type": "remote", "llm_model": "m-a", "llm_models": ["m-a", "m-b"],
                "vector_db_path": "", "model_cache_dir": "", "available_presets": ["remote-kisski", "runpod", "cpu-only"],
                "compatible_presets": ["remote-kisski", "runpod"],
                "switchable_presets": [{"name": "remote-kisski", "active": st["preset"] == "remote-kisski", "credentials": "ok"},
                                       {"name": "runpod", "active": st["preset"] == "runpod", "credentials": "ok"}],
                "default_top_k": 10, "default_min_score": 0.3, "max_chunk_size": 800})
        elif self.path == "/api/config/health":
            self._send(200, {"embedding": None, "llm": None})
        elif self.path == "/api/libraries":
            self._send(200, [{"library_id": "6297749"}])
        else:
            self._send(404, {})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if self.path == "/api/config":
            self.state["preset"] = body["preset_name"]
            self.state["switched"].append(body["preset_name"])
            self._send(200, {"preset_name": body["preset_name"]})
        elif self.path == "/api/query":
            question = next((q for q in GOLD["questions"] if q["question"] == body["question"]), Q["Q01"])
            answer = " ".join(f"{f['text']} [S1]." for f in question["facts"] if f.get("answer_patterns"))
            self._send(200, make_response(question, answer))
        else:
            self._send(404, {})


class EndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        MockBackend.state = {"preset": "remote-kisski", "switched": []}
        cls.server = HTTPServer(("127.0.0.1", 0), MockBackend)
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_full_pipeline_run_judge_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "run"
            argv = ["run_eval.py", "--presets", "all", "--models", "all", "--questions", "Q01,Q03,Q09", "--url", self.url,
                    "--output-dir", str(out), "--no-local", "--delay", "0", "--no-warmup"]
            with mock.patch.object(sys, "argv", argv):
                self.assertEqual(run_eval.main(), 0)
            # 2 presets x 2 models x 3 questions
            self.assertEqual(len(list((out / "raw").glob("*.json"))), 12)
            # active preset restored after switching to runpod
            self.assertEqual(MockBackend.state["preset"], "remote-kisski")
            self.assertIn("runpod", MockBackend.state["switched"])
            self.assertTrue((out / "report.md").exists())
            self.assertIn("Automatic floor only", (out / "report.md").read_text())

            judge_args = type("A", (), {"run_dir": str(out), "force": False})()
            self.assertEqual(judge.cmd_prepare(judge_args), 0)
            self.assertEqual(judge.cmd_validate(judge_args), 1)  # skeletons are empty

            for path in (out / "judgments").glob("*.json"):
                j = json.loads(path.read_text())
                for fid, entry in j["facts"].items():
                    entry.update(verdict="semantic", attribution_ok=True)
                j.update(citation_support=1.0, overall=4)
                if "inference_quality" in j:
                    j["inference_quality"] = 1.0
                path.write_text(json.dumps(j))
            self.assertEqual(judge.cmd_validate(judge_args), 0)

            groups = report.build_report(out)
            self.assertEqual(len(groups), 4)
            md = (out / "report.md").read_text()
            self.assertNotIn("Automatic floor only", md)
            self.assertIn("Recall by reasoning level", md)
            g = next(iter(groups.values()))
            self.assertEqual(g["judged_runs"], 3)
            self.assertEqual(g["recall_by_level"]["inference"], 1.0)

    def test_dry_run_sends_no_queries(self):
        argv = ["run_eval.py", "--presets", "all", "--url", self.url, "--no-local", "--dry-run"]
        before = len(MockBackend.state["switched"])
        with mock.patch.object(sys, "argv", argv):
            self.assertEqual(run_eval.main(), 0)
        self.assertEqual(len(MockBackend.state["switched"]), before)


if __name__ == "__main__":
    unittest.main()
