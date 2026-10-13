"""Unit tests for backend.services.system_health.get_system_health."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from backend.config.settings import Settings
from backend.services.system_health import get_system_health


def _fake_vm(percent=50.0, total=16 * 1024 ** 3, available=8 * 1024 ** 3):
    return SimpleNamespace(percent=percent, total=total, available=available)


def _fake_swap(percent=10.0, total=8 * 1024 ** 3, used=1 * 1024 ** 3):
    return SimpleNamespace(percent=percent, total=total, used=used)


class GetSystemHealthTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.settings = Settings(data_path=self.tmp, kreuzberg_url="http://kreuzberg:8000", qdrant_url="http://qdrant:6333")

    async def test_reports_cpu_memory_swap_disk(self):
        with patch("backend.services.system_health.psutil.virtual_memory", return_value=_fake_vm()), \
             patch("backend.services.system_health.psutil.swap_memory", return_value=_fake_swap()), \
             patch("backend.services.system_health.psutil.cpu_percent", return_value=12.3), \
             patch("backend.services.system_health.httpx.AsyncClient.get", new=AsyncMock(side_effect=httpx.ConnectError("refused"))):
            result = await get_system_health(self.settings)
        self.assertEqual(result["cpu_percent"], 12.3)
        self.assertEqual(result["memory"]["percent"], 50.0)
        self.assertEqual(result["swap"]["percent"], 10.0)
        self.assertIsNotNone(result["disk"])
        self.assertIn("free_percent", result["disk"])

    async def test_sidecar_ok_reports_latency(self):
        ok_response = httpx.Response(200, request=httpx.Request("GET", "http://kreuzberg:8000/health"))
        with patch("backend.services.system_health.psutil.virtual_memory", return_value=_fake_vm()), \
             patch("backend.services.system_health.psutil.swap_memory", return_value=_fake_swap()), \
             patch("backend.services.system_health.psutil.cpu_percent", return_value=0.0), \
             patch("backend.services.system_health.httpx.AsyncClient.get", new=AsyncMock(return_value=ok_response)):
            result = await get_system_health(self.settings)
        self.assertEqual(result["sidecars"]["kreuzberg"]["status"], "ok")
        self.assertIn("latency_ms", result["sidecars"]["kreuzberg"])
        self.assertEqual(result["sidecars"]["qdrant"]["status"], "ok")

    async def test_sidecar_unreachable_reported_without_raising(self):
        with patch("backend.services.system_health.psutil.virtual_memory", return_value=_fake_vm()), \
             patch("backend.services.system_health.psutil.swap_memory", return_value=_fake_swap()), \
             patch("backend.services.system_health.psutil.cpu_percent", return_value=0.0), \
             patch("backend.services.system_health.httpx.AsyncClient.get", new=AsyncMock(side_effect=httpx.ConnectError("refused"))):
            result = await get_system_health(self.settings)
        self.assertEqual(result["sidecars"]["kreuzberg"]["status"], "unreachable")
        self.assertEqual(result["sidecars"]["qdrant"]["status"], "unreachable")

    async def test_sidecar_timeout_reported_without_raising(self):
        with patch("backend.services.system_health.psutil.virtual_memory", return_value=_fake_vm()), \
             patch("backend.services.system_health.psutil.swap_memory", return_value=_fake_swap()), \
             patch("backend.services.system_health.psutil.cpu_percent", return_value=0.0), \
             patch("backend.services.system_health.httpx.AsyncClient.get", new=AsyncMock(side_effect=httpx.TimeoutException("timed out"))):
            result = await get_system_health(self.settings)
        self.assertEqual(result["sidecars"]["kreuzberg"]["status"], "timeout")

    async def test_qdrant_local_mode_when_url_unset(self):
        self.settings.qdrant_url = None
        with patch("backend.services.system_health.psutil.virtual_memory", return_value=_fake_vm()), \
             patch("backend.services.system_health.psutil.swap_memory", return_value=_fake_swap()), \
             patch("backend.services.system_health.psutil.cpu_percent", return_value=0.0), \
             patch("backend.services.system_health.httpx.AsyncClient.get", new=AsyncMock(side_effect=httpx.ConnectError("refused"))):
            result = await get_system_health(self.settings)
        self.assertEqual(result["sidecars"]["qdrant"]["status"], "local-mode")
