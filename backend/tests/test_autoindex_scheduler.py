"""Unit tests for backend.services.autoindex_scheduler."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet
from pydantic import ValidationError

from backend.config.settings import Settings
from backend.services.autoindex_scheduler import (
    _CLAIM_TTL_SECONDS,
    _STARTUP_DELAY_SECONDS,
    _claim_is_fresh,
    _write_claim,
    read_scheduler_state,
    run_scheduler_loop,
    trigger_index_run,
    update_scheduler_state,
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

    async def test_unscoped_run_omits_slug_flag(self):
        self.settings.autoindex_secret = Fernet.generate_key().decode()
        with patch("backend.services.autoindex_scheduler.read_live_status", return_value={}), \
             patch("backend.services.autoindex_scheduler.asyncio.create_subprocess_exec", new=AsyncMock()) as mock_spawn:
            await trigger_index_run(self.settings)
        self.assertNotIn("--slug", mock_spawn.await_args.args)

    async def test_slug_scoped_run_includes_slug_flag(self):
        self.settings.autoindex_secret = Fernet.generate_key().decode()
        with patch("backend.services.autoindex_scheduler.read_live_status", return_value={}), \
             patch("backend.services.autoindex_scheduler.asyncio.create_subprocess_exec", new=AsyncMock()) as mock_spawn:
            await trigger_index_run(self.settings, slug="groups/42")
        args = mock_spawn.await_args.args
        self.assertIn("--slug", args)
        self.assertIn("groups/42", args)

    async def test_concurrent_calls_spawn_only_one_subprocess(self):
        # Regression: two near-simultaneous calls (e.g. a double-clicked
        # "Index" button) must not both spawn a subprocess. read_live_status
        # is pinned to "not running" for the whole test — on purpose, since
        # in reality the spawned subprocess hasn't had time to report its own
        # "running" status yet either; the claim file (not the status file)
        # is what must close this gap.
        self.settings.autoindex_secret = Fernet.generate_key().decode()
        with patch("backend.services.autoindex_scheduler.read_live_status", return_value={}), \
             patch("backend.services.autoindex_scheduler.asyncio.create_subprocess_exec", new=AsyncMock()) as mock_spawn:
            results = await asyncio.gather(
                trigger_index_run(self.settings, slug="users/1"),
                trigger_index_run(self.settings, slug="users/1"),
            )
        self.assertEqual(mock_spawn.await_count, 1)
        self.assertEqual(sorted(results), ["already_running", "started"])

    async def test_claim_is_fresh_immediately_after_write(self):
        _write_claim(self.tmp)
        self.assertTrue(_claim_is_fresh(self.tmp))

    async def test_claim_is_fresh_false_when_no_claim_written(self):
        self.assertFalse(_claim_is_fresh(self.tmp))

    async def test_claim_is_fresh_false_once_ttl_expires(self):
        from datetime import datetime, timedelta, timezone

        from backend.services.autoindex_scheduler import _atomic_write_json, _claim_path

        stale_claim_at = datetime.now(timezone.utc) - timedelta(seconds=_CLAIM_TTL_SECONDS + 1)
        _atomic_write_json(_claim_path(self.tmp), {"claimed_at": stale_claim_at.isoformat()})
        self.assertFalse(_claim_is_fresh(self.tmp))

    async def test_fresh_claim_blocks_a_new_run_even_when_not_yet_reflected_in_status(self):
        # A third call, slightly later than the two above, must also be
        # blocked for as long as the claim is fresh — not just the exact
        # concurrent pair.
        self.settings.autoindex_secret = Fernet.generate_key().decode()
        _write_claim(self.tmp)
        with patch("backend.services.autoindex_scheduler.read_live_status", return_value={}), \
             patch("backend.services.autoindex_scheduler.asyncio.create_subprocess_exec", new=AsyncMock()) as mock_spawn:
            result = await trigger_index_run(self.settings, slug="users/1")
        self.assertEqual(result, "already_running")
        mock_spawn.assert_not_awaited()


class SchedulerStateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_missing_file_reads_empty_dict(self):
        self.assertEqual(read_scheduler_state(self.tmp), {})

    def test_round_trip(self):
        write_scheduler_state(self.tmp, {"paused": True})
        self.assertEqual(read_scheduler_state(self.tmp), {"paused": True})

    def test_update_merges_without_clobbering_existing_fields(self):
        write_scheduler_state(self.tmp, {"paused": True})
        update_scheduler_state(self.tmp, next_tick_at="2026-01-01T00:00:00+00:00")
        result = read_scheduler_state(self.tmp)
        self.assertTrue(result["paused"])
        self.assertEqual(result["next_tick_at"], "2026-01-01T00:00:00+00:00")

    def test_update_on_missing_file_creates_it(self):
        update_scheduler_state(self.tmp, paused=True)
        self.assertEqual(read_scheduler_state(self.tmp), {"paused": True})


class RunSchedulerLoopTest(unittest.IsolatedAsyncioTestCase):
    async def test_tick_then_cancel(self):
        """One tick fires after the startup delay, then the loop can be
        cancelled cleanly via the next sleep call."""
        settings = Settings(data_path=Path(tempfile.mkdtemp()), autoindex_interval_minutes=60)
        calls = []

        async def fake_sleep(seconds):
            calls.append(seconds)
            if len(calls) >= 2:
                raise asyncio.CancelledError()

        with patch("backend.services.autoindex_scheduler.asyncio.sleep", new=AsyncMock(side_effect=fake_sleep)), \
             patch("backend.services.autoindex_scheduler.trigger_index_run", new=AsyncMock(return_value="started")) as mock_trigger:
            with self.assertRaises(asyncio.CancelledError):
                await run_scheduler_loop(settings)

        mock_trigger.assert_awaited_once()
        self.assertEqual(calls, [_STARTUP_DELAY_SECONDS, settings.autoindex_interval_minutes * 60])

    async def test_tick_persists_next_tick_at(self):
        settings = Settings(data_path=Path(tempfile.mkdtemp()), autoindex_interval_minutes=60)
        calls = []

        async def fake_sleep(seconds):
            calls.append(seconds)
            if len(calls) >= 2:
                raise asyncio.CancelledError()

        with patch("backend.services.autoindex_scheduler.asyncio.sleep", new=AsyncMock(side_effect=fake_sleep)), \
             patch("backend.services.autoindex_scheduler.trigger_index_run", new=AsyncMock(return_value="started")):
            with self.assertRaises(asyncio.CancelledError):
                await run_scheduler_loop(settings)

        state = read_scheduler_state(settings.data_path)
        self.assertIn("next_tick_at", state)

    async def test_tick_exception_does_not_stop_loop(self):
        """A tick that raises is logged and swallowed, not propagated —
        proven by reaching the second sleep call."""
        settings = Settings(data_path=Path(tempfile.mkdtemp()), autoindex_interval_minutes=60)
        calls = []

        async def fake_sleep(seconds):
            calls.append(seconds)
            if len(calls) >= 2:
                raise asyncio.CancelledError()

        with patch("backend.services.autoindex_scheduler.asyncio.sleep", new=AsyncMock(side_effect=fake_sleep)), \
             patch("backend.services.autoindex_scheduler.trigger_index_run", new=AsyncMock(side_effect=RuntimeError("boom"))):
            with self.assertRaises(asyncio.CancelledError):
                await run_scheduler_loop(settings)

        self.assertEqual(len(calls), 2)

    async def test_paused_scheduler_skips_trigger(self):
        settings = Settings(data_path=Path(tempfile.mkdtemp()), autoindex_interval_minutes=60)
        write_scheduler_state(settings.data_path, {"paused": True})
        calls = []

        async def fake_sleep(seconds):
            calls.append(seconds)
            if len(calls) >= 2:
                raise asyncio.CancelledError()

        with patch("backend.services.autoindex_scheduler.asyncio.sleep", new=AsyncMock(side_effect=fake_sleep)), \
             patch("backend.services.autoindex_scheduler.trigger_index_run", new=AsyncMock()) as mock_trigger:
            with self.assertRaises(asyncio.CancelledError):
                await run_scheduler_loop(settings)

        mock_trigger.assert_not_awaited()

    async def test_returns_immediately_when_interval_unset(self):
        settings = Settings(data_path=Path(tempfile.mkdtemp()), autoindex_interval_minutes=None)
        with patch("backend.services.autoindex_scheduler.asyncio.sleep", new=AsyncMock()) as mock_sleep, \
             patch("backend.services.autoindex_scheduler.trigger_index_run", new=AsyncMock()) as mock_trigger:
            await run_scheduler_loop(settings)  # must return, not raise or hang
        mock_sleep.assert_not_awaited()
        mock_trigger.assert_not_awaited()

    async def test_read_scheduler_state_exception_does_not_stop_loop(self):
        """A corrupted/unreadable scheduler-state file must not kill the loop,
        same as a trigger_index_run failure."""
        settings = Settings(data_path=Path(tempfile.mkdtemp()), autoindex_interval_minutes=60)
        calls = []

        async def fake_sleep(seconds):
            calls.append(seconds)
            if len(calls) >= 2:
                raise asyncio.CancelledError()

        with patch("backend.services.autoindex_scheduler.asyncio.sleep", new=AsyncMock(side_effect=fake_sleep)), \
             patch("backend.services.autoindex_scheduler.read_scheduler_state", side_effect=UnicodeDecodeError("utf-8", b"", 0, 1, "boom")), \
             patch("backend.services.autoindex_scheduler.trigger_index_run", new=AsyncMock()) as mock_trigger:
            with self.assertRaises(asyncio.CancelledError):
                await run_scheduler_loop(settings)

        self.assertEqual(len(calls), 2)  # reached the second sleep -> loop survived
        mock_trigger.assert_not_awaited()  # never got past the raising read_scheduler_state call


if __name__ == "__main__":
    unittest.main()
