"""Unit tests for backend.utils.disk_space.check_disk_space.

Reproduces the production incident where Qdrant's optimizer got stuck because
the disk ran down to ~1.95 GB free: the auto-indexer kept writing while
optimization couldn't find room to merge segments. This check must refuse to
proceed under those conditions, and is shared by the auto-indexer's pre-flight
guard and the production health check.
"""

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.utils import disk_space


class CheckDiskSpaceTest(unittest.TestCase):
    @staticmethod
    def _fake_usage(total_gb: float, free_gb: float) -> SimpleNamespace:
        return SimpleNamespace(total=int(total_gb * 1024**3), used=0, free=int(free_gb * 1024**3))

    def test_returns_none_when_free_space_well_above_threshold(self):
        with patch.object(disk_space.shutil, "disk_usage", return_value=self._fake_usage(100, 30)):
            result = disk_space.check_disk_space(Path("/fake"), min_free_percent=15.0)
        self.assertIsNone(result)

    def test_returns_reason_when_free_space_below_threshold(self):
        with patch.object(disk_space.shutil, "disk_usage", return_value=self._fake_usage(79, 2)):
            result = disk_space.check_disk_space(Path("/fake"), min_free_percent=15.0)
        self.assertIsNotNone(result)
        self.assertIn("/fake", result)

    def test_boundary_exactly_at_threshold_counts_as_enough(self):
        with patch.object(disk_space.shutil, "disk_usage", return_value=self._fake_usage(100, 15)):
            result = disk_space.check_disk_space(Path("/fake"), min_free_percent=15.0)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
