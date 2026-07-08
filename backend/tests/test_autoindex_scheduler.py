"""Unit tests for backend.services.autoindex_scheduler."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet
from pydantic import ValidationError

from backend.config.settings import Settings
from backend.services.autoindex_scheduler import (
    read_scheduler_state,
    trigger_index_run,
    write_scheduler_state,
)


class SettingsValidatorTest(unittest.TestCase):
    def test_autoindex_interval_minutes_defaults_none(self):
        self.assertIsNone(Settings().autoindex_interval_minutes)

    def test_autoindex_interval_minutes_accepts_positive_int(self):
        s = Settings(autoindex_interval_minutes=60)
        self.assertEqual(s.autoindex_interval_minutes, 60)

    def test_autoindex_interval_minutes_rejects_zero(self):
        with self.assertRaises(ValidationError):
            Settings(autoindex_interval_minutes=0)

    def test_autoindex_interval_minutes_rejects_negative(self):
        with self.assertRaises(ValidationError):
            Settings(autoindex_interval_minutes=-5)


class TriggerIndexRunTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.settings = Settings(data_path=self.tmp, autoindex_secret=None)

    async def test_returns_disabled_when_secret_unset(self):
        result = await trigger_index_run(self.settings)
        self.assertEqual(result, "disabled")

    async def test_returns_already_running(self):
        self.settings.autoindex_secret = Fernet.generate_key().decode()
        with patch("backend.services.autoindex_scheduler.read_live_status", return_value={"running": True}):
            result = await trigger_index_run(self.settings)
        self.assertEqual(result, "already_running")

    async def test_spawns_subprocess_when_not_running(self):
        self.settings.autoindex_secret = Fernet.generate_key().decode()
        with patch("backend.services.autoindex_scheduler.read_live_status", return_value={}), \
             patch("backend.services.autoindex_scheduler.asyncio.create_subprocess_exec", new=AsyncMock()) as mock_spawn:
            result = await trigger_index_run(self.settings)
        self.assertEqual(result, "started")
        mock_spawn.assert_awaited_once()

    async def test_unscoped_run_omits_fingerprint_flag(self):
        self.settings.autoindex_secret = Fernet.generate_key().decode()
        with patch("backend.services.autoindex_scheduler.read_live_status", return_value={}), \
             patch("backend.services.autoindex_scheduler.asyncio.create_subprocess_exec", new=AsyncMock()) as mock_spawn:
            await trigger_index_run(self.settings)
        self.assertNotIn("--fingerprint", mock_spawn.await_args.args)

    async def test_scoped_run_includes_fingerprint_flag(self):
        self.settings.autoindex_secret = Fernet.generate_key().decode()
        with patch("backend.services.autoindex_scheduler.read_live_status", return_value={}), \
             patch("backend.services.autoindex_scheduler.asyncio.create_subprocess_exec", new=AsyncMock()) as mock_spawn:
            await trigger_index_run(self.settings, fingerprint="fp-abc")
        args = mock_spawn.await_args.args
        self.assertIn("--fingerprint", args)
        self.assertIn("fp-abc", args)


class SchedulerStateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_missing_file_reads_empty_dict(self):
        self.assertEqual(read_scheduler_state(self.tmp), {})

    def test_round_trip(self):
        write_scheduler_state(self.tmp, {"paused": True})
        self.assertEqual(read_scheduler_state(self.tmp), {"paused": True})


if __name__ == "__main__":
    unittest.main()
