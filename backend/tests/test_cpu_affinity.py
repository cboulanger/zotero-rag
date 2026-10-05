"""Unit tests for backend.utils.cpu_affinity.

Root-caused in the 2026-10 Qdrant-timeout incident: an indexing run and a
concurrent /api/query request can both need CPU time at the same moment, and
an unbounded indexing process can starve query handling. These tests cover
the pure CPU-set computation and the affinity-restriction wrapper that no-ops
safely on platforms (e.g. macOS dev) without sched_getaffinity/setaffinity.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from backend.utils import cpu_affinity


class ComputeRestrictedCpuSetTest(unittest.TestCase):
    def test_reserved_zero_means_no_restriction(self):
        self.assertIsNone(cpu_affinity.compute_restricted_cpu_set({0, 1, 2, 3}, reserved_cpus=0))

    def test_reserves_requested_number_of_cpus(self):
        result = cpu_affinity.compute_restricted_cpu_set({0, 1, 2, 3}, reserved_cpus=1)
        self.assertEqual(result, {0, 1, 2})

    def test_reserves_multiple_cpus(self):
        result = cpu_affinity.compute_restricted_cpu_set({0, 1, 2, 3}, reserved_cpus=2)
        self.assertEqual(result, {0, 1})

    def test_never_restricts_indexing_to_fewer_than_one_cpu(self):
        result = cpu_affinity.compute_restricted_cpu_set({0, 1, 2, 3}, reserved_cpus=10)
        self.assertEqual(result, {0})

    def test_single_cpu_host_is_never_restricted(self):
        self.assertIsNone(cpu_affinity.compute_restricted_cpu_set({0}, reserved_cpus=1))

    def test_uses_lowest_numbered_cpus_regardless_of_set_order(self):
        result = cpu_affinity.compute_restricted_cpu_set({3, 1, 2, 0}, reserved_cpus=1)
        self.assertEqual(result, {0, 1, 2})


class RestrictCurrentProcessCpusTest(unittest.TestCase):
    def test_applies_computed_affinity_on_supported_platform(self):
        with patch.object(cpu_affinity.os, "sched_getaffinity", create=True, return_value={0, 1, 2, 3}) as mock_get, \
             patch.object(cpu_affinity.os, "sched_setaffinity", create=True) as mock_set:
            cpu_affinity.restrict_current_process_cpus(reserved_cpus=1)
        mock_get.assert_called_once_with(0)
        mock_set.assert_called_once_with(0, {0, 1, 2})

    def test_does_not_call_setaffinity_when_reserved_is_zero(self):
        with patch.object(cpu_affinity.os, "sched_getaffinity", create=True, return_value={0, 1, 2, 3}), \
             patch.object(cpu_affinity.os, "sched_setaffinity", create=True) as mock_set:
            cpu_affinity.restrict_current_process_cpus(reserved_cpus=0)
        mock_set.assert_not_called()

    def test_noop_on_platform_without_sched_affinity_apis(self):
        with patch.object(cpu_affinity, "os", new=SimpleNamespace()):
            cpu_affinity.restrict_current_process_cpus(reserved_cpus=1)  # must not raise


if __name__ == "__main__":
    unittest.main()
