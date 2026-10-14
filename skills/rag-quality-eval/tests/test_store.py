"""Tests for the Hugging Face store helpers and the environment script (no network, no backend)."""

from __future__ import annotations

import gzip
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import common  # noqa: E402
import gold_env  # noqa: E402
import hf_store  # noqa: E402

MANIFEST = {"attachments": [{"attachment_key": "B", "sha256": "2"}, {"attachment_key": "A", "sha256": "1"}]}
REPORT = {
    "meta": {"gold_version": 2, "git_commit": "abc1234"},
    "groups": {"remote-kisski / gemma": {"runs": 10, "composite": 0.71234, "recall_en": 0.8, "recall_non_en": None,
                                         "judge_overall": 3.5, "lost_in_retrieval": 4, "lost_in_generation": 1,
                                         "verdicts": {"pass": 5}, "latency_p50_ms": 1}},
}


class StoreHelperTests(unittest.TestCase):
    def test_paths_are_directory_safe(self):
        snap, prov = hf_store.snapshot_paths("intfloat/multilingual-e5-large")
        self.assertEqual(snap, "snapshots/intfloat--multilingual-e5-large/latest.jsonl.gz")
        self.assertTrue(prov.endswith("latest.json"))

    def test_non_production_extractor_gets_its_own_folder(self):
        self.assertEqual(hf_store.snapshot_paths("m", "kreuzberg")[0], "snapshots/m/latest.jsonl.gz")
        self.assertEqual(hf_store.snapshot_paths("m", "docling"),
                         ("snapshots/m/docling/latest.jsonl.gz", "snapshots/m/docling/latest.json"))

    def test_named_snapshot_does_not_touch_latest(self):
        self.assertEqual(hf_store.snapshot_paths("m", "docling", "20261013-02f00c6"),
                         ("snapshots/m/docling/20261013-02f00c6.jsonl.gz", "snapshots/m/docling/20261013-02f00c6.json"))

    def test_legacy_backend_restores_the_kreuzberg_snapshot(self):
        self.assertEqual(hf_store.restore_extractor("legacy"), "kreuzberg")
        self.assertEqual(hf_store.restore_extractor(None), "kreuzberg")
        self.assertEqual(hf_store.restore_extractor("docling"), "docling")

    def test_fingerprint_is_order_independent_and_content_sensitive(self):
        reordered = {"attachments": list(reversed(MANIFEST["attachments"]))}
        self.assertEqual(hf_store.sources_fingerprint(MANIFEST), hf_store.sources_fingerprint(reordered))
        changed = {"attachments": [{"attachment_key": "A", "sha256": "9"}, MANIFEST["attachments"][0]]}
        self.assertNotEqual(hf_store.sources_fingerprint(MANIFEST), hf_store.sources_fingerprint(changed))

    def test_snapshot_header_reads_first_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.jsonl.gz"
            with gzip.open(path, "wt") as fh:
                fh.write(json.dumps({"slug": "groups/1", "embedding_model_name": "m"}) + "\n{}\n")
            self.assertEqual(hf_store.snapshot_header(path)["embedding_model_name"], "m")

    def test_report_entry_and_idempotent_index_merge(self):
        entry = hf_store.report_index_entry("20261012T000000Z", REPORT)
        self.assertEqual(entry["gold_version"], 2)
        self.assertEqual(entry["groups"]["remote-kisski / gemma"]["composite"], 0.71234)
        self.assertNotIn("latency_p50_ms", entry["groups"]["remote-kisski / gemma"])
        index = hf_store.merge_index([], entry)
        index = hf_store.merge_index(index, {**entry, "git_commit": "new"})
        self.assertEqual(len(index), 1)
        later = hf_store.merge_index(index, {**entry, "run": "20261013T000000Z"})
        self.assertEqual([e["run"] for e in later], ["20261012T000000Z", "20261013T000000Z"])

    def test_gold_markdown_lists_every_question(self):
        gold = common.load_gold()
        md = hf_store.render_gold_markdown(gold)
        for q in gold["questions"]:
            self.assertIn(f"\n### {q['id']}\n", md)
            self.assertIn(f"[{q['id']}](#{q['id'].lower()})", md)
            self.assertIn(q["question"], md)

    def test_gold_markdown_groups_by_domain_and_sorts_by_difficulty(self):
        md = hf_store.render_gold_markdown(common.load_gold())
        heads = [md.index(f"## {t}") for t in hf_store.DOMAIN_TITLES.values()]
        self.assertEqual(heads, sorted(heads))
        facts = md[md.index("## Facts"):md.index("## Routing")]
        order = [int(l.split("difficulty:")[1][0]) for l in facts.splitlines() if l.startswith("Categories:")]
        self.assertEqual(order, sorted(order))

    def test_report_commit_archives_and_replaces_latest_atomically(self):
        existing = ["reports/old/report.md", "reports/latest/report.md", "reports/latest/raw/a.json", "sources/x.pdf"]
        adds, deletes = hf_store.plan_report_commit("new", ["report.md", "report.json"], existing)
        self.assertIn(("reports/new/report.json", "report.json"), adds)
        self.assertIn(("reports/latest/report.json", "report.json"), adds)
        self.assertEqual(deletes, ["reports/latest/raw/a.json"])  # stale file of the previous run only
        self.assertNotIn("reports/old/report.md", deletes)          # the archive is never touched

    def test_no_latest_archives_only_and_keeps_baseline(self):
        existing = ["reports/latest/report.md", "reports/latest/raw/a.json"]
        adds, deletes = hf_store.plan_report_commit("exp", ["report.md"], existing, update_latest=False)
        self.assertEqual(adds, [("reports/exp/report.md", "report.md")])
        self.assertEqual(deletes, [])
        base = hf_store.report_index_entry("20261012T000000Z", REPORT)
        exp = hf_store.report_index_entry("20261013T000000Z", REPORT, latest=False)
        self.assertTrue(exp["not_latest"])
        self.assertNotIn("not_latest", base)
        self.assertEqual(hf_store.latest_run([base, exp]), "20261012T000000Z")
        card = hf_store.render_card("o/r", None, [], [base, exp])
        self.assertIn("Latest run: `20261012T000000Z`", card)

    def test_card_links_the_latest_report(self):
        card = hf_store.render_card("o/r", None, [], [hf_store.report_index_entry("run1", REPORT)])
        self.assertIn("[report](reports/latest/report.md)", card)

    def test_card_has_license_attribution_and_tables(self):
        prov = {"embedding_model": "m", "embedding_dim": 3, "counts": {"chunks": 24}, "extractor": "kreuzberg",
                "preset": "p", "git_commit": "abc", "built_at": "2026-10-12T00:00:00"}
        card = hf_store.render_card("o/r", MANIFEST, [prov], [hf_store.report_index_entry("run1", REPORT)])
        self.assertTrue(card.startswith("---\nlicense: cc-by-4.0"))
        self.assertIn("CC BY 4.0", card)
        self.assertIn("| m (3-dim) | 24 | kreuzberg |", card)
        self.assertIn("| `run1` | v2 | `abc1234` | remote-kisski / gemma | 10 | 0.71 | 0.80 | n/a | 3.50 |", card)

    def test_card_lists_evaluations(self):
        card = hf_store.render_card("o/r", None, [], [], [{"name": "study-1", "title": "Study one"}])
        self.assertIn("| Study one | [`evaluations/study-1/`](evaluations/study-1/README.md) |", card)
        self.assertNotIn("## Evaluations", hf_store.render_card("o/r", None, [], []))

    def test_card_without_content_still_renders(self):
        self.assertIn("# zotero-rag gold evaluation corpus", hf_store.render_card("o/r", None, [], []))


class ProxyKeyTests(unittest.TestCase):
    def test_flag_enables_placeholder_keys(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(common, "PROXY_KEYS_FLAG", Path(tmp) / "flag"):
            with mock.patch.dict("os.environ", {}, clear=False):
                import os
                os.environ.pop("RAG_EVAL_ASSUME_PROXY_KEYS", None)
                self.assertFalse(common.assume_proxy_keys())
                (Path(tmp) / "flag").write_text("x")
                self.assertTrue(common.assume_proxy_keys())
            with mock.patch.dict("os.environ", {"RAG_EVAL_ASSUME_PROXY_KEYS": "1"}):
                (Path(tmp) / "flag").unlink()
                self.assertTrue(common.assume_proxy_keys())


class GoldEnvTests(unittest.TestCase):
    def test_library_id_comes_from_the_gold_file(self):
        self.assertEqual(gold_env.library_id(), "6297749")

    def test_chunk_count_is_zero_when_backend_unreachable(self):
        with mock.patch.object(gold_env, "http_json", return_value=(0, None)):
            self.assertEqual(gold_env.chunk_count("http://x", "1"), 0)

    def test_down_without_a_started_backend_is_a_noop(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(gold_env, "PID_FILE", Path(tmp) / "pid"):
            self.assertFalse(gold_env.stop_backend())


if __name__ == "__main__":
    unittest.main()
