"""Tests for backend.utils.log_rotation."""

import logging
import os
import tempfile
import time
import unittest
from pathlib import Path

from backend.utils.log_rotation import RotatingLogHandler


class TestRotatingLogHandler(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.log = self.dir / "server.log"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_rotates_existing_file_on_startup(self) -> None:
        self.log.write_text("old\n")
        h = RotatingLogHandler(self.log)
        h.close()
        rotated = [p for p in self.dir.iterdir() if p.name != "server.log"]
        self.assertEqual(len(rotated), 1)
        self.assertEqual(rotated[0].read_text(), "old\n")
        self.assertEqual(self.log.read_text(), "")

    def test_no_startup_rotation_when_disabled(self) -> None:
        self.log.write_text("old\n")
        h = RotatingLogHandler(self.log, rotate_on_startup=False)
        h.close()
        self.assertEqual(list(self.dir.iterdir()), [self.log])

    def test_prunes_files_older_than_retention(self) -> None:
        old = self.dir / "server.log.2020-01-01_000000"
        new = self.dir / "server.log.2999-01-01_000000"
        unrelated = self.dir / "other.log.2020-01-01_000000"
        for p in (old, new, unrelated):
            p.write_text("x")
        ts = time.time() - 8 * 86400
        os.utime(old, (ts, ts))
        os.utime(unrelated, (ts, ts))
        self.log.write_text("cur\n")
        RotatingLogHandler(self.log, retention_days=7).close()
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())
        self.assertTrue(unrelated.exists())

    def test_writes_records(self) -> None:
        h = RotatingLogHandler(self.log)
        logger = logging.getLogger("rot-test")
        logger.addHandler(h)
        logger.warning("hello")
        h.close()
        logger.removeHandler(h)
        self.assertIn("hello", self.log.read_text())


if __name__ == "__main__":
    unittest.main()
