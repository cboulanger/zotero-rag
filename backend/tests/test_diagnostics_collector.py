"""Unit tests for per-request upload diagnostics capture."""

import asyncio
import logging
import unittest

from backend.services import diagnostics_collector as dc
from backend.services.diagnostics_collector import DiagnosticsCollector, activate

_LOG = logging.getLogger("backend.services.document_processor")


class TestScrubbing(unittest.TestCase):
    def test_masks_secrets(self):
        out = dc.scrub_secrets("Authorization: Bearer abc123 api_key=SEKRET X-Kisski-Api-Key: zzz")
        self.assertNotIn("abc123", out)
        self.assertNotIn("SEKRET", out)
        self.assertNotIn("zzz", out)

    def test_body_excerpt_truncates(self):
        out = dc.body_excerpt("x" * 5000, limit=100)
        self.assertLess(len(out), 200)
        self.assertIn("truncated", out)


class TestStage(unittest.TestCase):
    def test_stage_noop_without_collector(self):
        with dc.stage("x") as st:
            st.set(a=1)  # must not raise
        self.assertIsNone(dc.current())

    def test_stage_records_and_marks_error(self):
        c = DiagnosticsCollector()
        with c.stage("ok") as st:
            st.set(n=1)
        with self.assertRaises(ValueError):
            with c.stage("bad"):
                raise ValueError("boom")
        p = c.finalize()
        self.assertEqual([s.name for s in p.stages], ["ok", "bad"])
        self.assertEqual(p.stages[0].outcome, "ok")
        self.assertEqual(p.stages[1].outcome, "error")
        self.assertIn("boom", p.stages[1].details["error"])

    def test_set_error_captures_traceback(self):
        c = DiagnosticsCollector()
        try:
            raise RuntimeError("kaboom")
        except RuntimeError as e:
            c.set_error(e)
        p = c.finalize()
        self.assertEqual(p.error["type"], "RuntimeError")
        self.assertIn("kaboom", p.error["traceback"])


class TestLogCapture(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_requests_are_isolated(self):
        async def run(tag: str):
            c = DiagnosticsCollector()
            with activate(c):
                _LOG.debug(f"start {tag}")
                await asyncio.sleep(0.01)
                await asyncio.to_thread(_LOG.debug, f"thread {tag}")
                await asyncio.sleep(0.01)
            return c.finalize()

        a, b = await asyncio.gather(run("A"), run("B"))
        msgs_a = [r.message for r in a.log_records]
        msgs_b = [r.message for r in b.log_records]
        self.assertIn("thread A", msgs_a)
        self.assertIn("thread B", msgs_b)
        self.assertFalse(any("B" in m.split()[-1] for m in msgs_a if m.startswith(("start", "thread"))))
        self.assertFalse(any("A" in m.split()[-1] for m in msgs_b if m.startswith(("start", "thread"))))

    async def test_levels_and_handlers_restored(self):
        lg = logging.getLogger("backend.services.extraction")
        before_level = lg.level
        before_handlers = list(lg.handlers)
        root_filters = [list(h.filters) for h in logging.getLogger().handlers]
        with activate(DiagnosticsCollector()):
            self.assertEqual(lg.level, logging.DEBUG)
        self.assertEqual(lg.level, before_level)
        self.assertEqual(lg.handlers, before_handlers)
        self.assertEqual([list(h.filters) for h in logging.getLogger().handlers], root_filters)

    async def test_nested_activation_refcounted(self):
        lg = logging.getLogger("backend.services.extraction")
        before = lg.level
        with activate(DiagnosticsCollector()):
            with activate(DiagnosticsCollector()):
                pass
            self.assertEqual(lg.level, logging.DEBUG)
        self.assertEqual(lg.level, before)

    async def test_existing_handlers_do_not_get_debug_records(self):
        class _Collect(logging.Handler):
            def __init__(self):
                super().__init__(level=logging.NOTSET)
                self.records = []

            def emit(self, record):
                self.records.append(record)

        root = logging.getLogger()
        h = _Collect()
        root.addHandler(h)
        old_root_level = root.level
        root.setLevel(logging.INFO)
        try:
            with activate(DiagnosticsCollector()):
                _LOG.debug("secret-debug-line")
                _LOG.warning("kept-warning")
            names = [r.getMessage() for r in h.records]
            self.assertNotIn("secret-debug-line", names)
            self.assertIn("kept-warning", names)
        finally:
            root.removeHandler(h)
            root.setLevel(old_root_level)

    async def test_log_cap_drops_oldest(self):
        c = DiagnosticsCollector()
        with activate(c):
            for i in range(dc.DIAG_MAX_LOG_RECORDS + 20):
                _LOG.debug(f"line {i}")
        p = c.finalize()
        self.assertLessEqual(len(p.log_records), dc.DIAG_MAX_LOG_RECORDS)
        self.assertGreaterEqual(p.truncated.get("log_records_dropped", 0), 20)
        self.assertEqual(p.log_records[-1].message, f"line {dc.DIAG_MAX_LOG_RECORDS + 19}")

    async def test_no_capture_outside_request(self):
        c = DiagnosticsCollector()
        with activate(c):
            pass
        _LOG.debug("after")
        self.assertFalse(any(r.message == "after" for r in c.finalize().log_records))


class TestServerInfo(unittest.TestCase):
    def test_allowlist_only(self):
        info = dc.build_server_info()
        for key in info:
            self.assertNotIn("key", key.lower().replace("keyword", ""))
            self.assertNotIn("secret", key.lower())
        self.assertIn("backend_version", info)


if __name__ == "__main__":
    unittest.main()
