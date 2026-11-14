"""Per-user sessions and endpoints on the MPCDF LLM Inference Service.

The service's terms do not allow one account to serve other people, so every
Zotero user logs in with their own MPCDF account (username, password and a
one-time code) and their endpoints run as their own Slurm jobs. This module
keeps one ``mpcdf_llm.manager.ProvisioningManager`` per user:

- data directory ``<data_path>/system/mpcdf/<user-hash>/`` (the library's preset
  and endpoint state; no secrets);
- the session token sealed with ``secret_store`` (``session.sealed.json``), never
  the password or the one-time code; the user's Hugging Face token, which every
  vLLM job needs, sealed next to it;
- the library's supervisor renews endpoints before their Slurm job ends and
  cancels them after an idle period; :meth:`MpcdfSessions.touch` reports use.

The managers live on the server's event loop. Provider methods run in worker
threads (core calls them through ``asyncio.to_thread``), so they reach the
managers through :meth:`MpcdfSessions.call`.

``mpcdf-llm`` is an optional dependency (``uv pip install
"mpcdf-llm @ git+https://gitlab.mpcdf.mpg.de/cboul/llm-mpcdf.git"``); without it
:func:`available` is False and the MPCDF provider reports why.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

SESSION_FILE = "session.sealed.json"
#: Login attempts per user within LOGIN_WINDOW_SECONDS before further attempts are refused.
LOGIN_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 600.0


class SessionsUnavailable(RuntimeError):
    """MPCDF sessions cannot be used here (library missing, or the server is not running)."""


class LoginRateLimited(RuntimeError):
    """Too many login attempts for one user in a short time."""


def available() -> bool:
    """Whether the ``mpcdf-llm`` library is installed."""
    try:
        import mpcdf_llm.manager  # noqa: F401
    except ImportError:
        return False
    return True


def user_key(user_id: Optional[int]) -> str:
    """Directory name for a Zotero user (``local`` for a loopback server's single user)."""
    if user_id is None:
        return "local"
    return hashlib.sha256(f"zotero-user:{user_id}".encode()).hexdigest()[:16]


class SealedSessionStore:
    """``mpcdf_llm`` session store that keeps the token encrypted with ``secret_store``.

    Implements the library's ``SessionStore`` protocol (``load``/``save``/``clear``)
    plus the user's Hugging Face token, which renewals need without the user present.
    """

    def __init__(self, directory: Path):
        self.path = Path(directory) / SESSION_FILE
        self._lock = threading.Lock()

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def load(self):
        from mpcdf_llm.models import Session
        from backend.services.secret_store import unseal

        data = self._read()
        token = unseal(data.get("token")) if data.get("token") else None
        if not token or not data.get("username") or not data.get("obtained_at"):
            return None
        return Session(token=token, username=data["username"], obtained_at=datetime.fromisoformat(data["obtained_at"]))

    def save(self, s) -> None:
        from backend.services.secret_store import require_secrets_enabled, seal

        require_secrets_enabled()
        with self._lock:
            data = self._read()
            data.update(
                token=seal(s.token.get_secret_value()),
                username=s.username,
                obtained_at=s.obtained_at.isoformat(),
            )
            self._write(data)

    def clear(self) -> None:
        with self._lock:
            data = self._read()
            for key in ("token", "username", "obtained_at"):
                data.pop(key, None)
            self._write(data)

    def hf_token(self) -> Optional[str]:
        from backend.services.secret_store import unseal

        stored = self._read().get("hf_token")
        return unseal(stored) if stored else None

    def set_hf_token(self, value: str) -> None:
        from backend.services.secret_store import require_secrets_enabled, seal

        require_secrets_enabled()
        with self._lock:
            data = self._read()
            data["hf_token"] = seal(value)
            self._write(data)

    def has_session(self) -> bool:
        return bool(self._read().get("token"))


@dataclass
class EndpointCredentials:
    """Where one of the user's endpoints answers, and its key (changes at every renewal)."""

    base_url: str
    api_key: str
    model: str


def _default_factory(directory: Path, store: SealedSessionStore):
    from mpcdf_llm.manager import ProvisioningManager

    return ProvisioningManager(directory, sessions=store, hf_token=store.hf_token)


class MpcdfSessions:
    """One provisioning manager per Zotero user, on the server's event loop."""

    def __init__(
        self,
        root: Path,
        manager_factory: Optional[Callable[[Path, SealedSessionStore], Any]] = None,
    ):
        self.root = Path(root)
        self._factory = manager_factory or _default_factory
        self._managers: dict[str, Any] = {}
        self._stores: dict[str, SealedSessionStore] = {}
        self._create_lock: Optional[asyncio.Lock] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._attempts: dict[str, list[float]] = {}

    # -- lifecycle ------------------------------------------------------------

    async def open(self) -> None:
        """Bind to the running loop and open the managers of users with a stored session."""
        self._loop = asyncio.get_running_loop()
        self._create_lock = asyncio.Lock()
        logging.getLogger("httpx").setLevel(logging.WARNING)  # endpoint URLs carry their slug
        if not self.root.is_dir():
            return
        for directory in sorted(p for p in self.root.iterdir() if p.is_dir()):
            if SealedSessionStore(directory).has_session():
                try:
                    await self._manager_for_key(directory.name)
                except Exception as exc:  # one user's broken state must not stop the server
                    logger.warning("Could not open MPCDF manager %s: %s", directory.name, exc)

    async def close(self) -> None:
        for key, manager in list(self._managers.items()):
            try:
                await manager.close()
            except Exception as exc:
                logger.warning("Closing MPCDF manager %s failed: %s", key, exc)
        self._managers.clear()
        self._loop = None

    def store(self, user_id: Optional[int]) -> SealedSessionStore:
        key = user_key(user_id)
        if key not in self._stores:
            self._stores[key] = SealedSessionStore(self.root / key)
        return self._stores[key]

    async def _manager_for_key(self, key: str):
        assert self._create_lock is not None, "MpcdfSessions.open() was not called"
        async with self._create_lock:
            manager = self._managers.get(key)
            if manager is None:
                directory = self.root / key
                directory.mkdir(parents=True, exist_ok=True)
                store = self._stores.setdefault(key, SealedSessionStore(directory))
                manager = self._factory(directory, store)
                await manager.open()
                manager.start_supervisor()
                self._managers[key] = manager
            return manager

    async def manager(self, user_id: Optional[int]):
        return await self._manager_for_key(user_key(user_id))

    # -- calls from worker threads ---------------------------------------------

    def call(self, fn: Callable[[], Awaitable[Any]], timeout: float = 30.0) -> Any:
        """Run ``fn()`` on the server's loop from a worker thread and wait for it."""
        loop = self._loop
        if loop is None or loop.is_closed():
            raise SessionsUnavailable("MPCDF sessions are not running on this server")
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            raise RuntimeError("MpcdfSessions.call() must not be used on the event loop thread")
        return asyncio.run_coroutine_threadsafe(fn(), loop).result(timeout)

    # -- session -----------------------------------------------------------------

    def _check_rate(self, user_id: Optional[int]) -> None:
        key = user_key(user_id)
        now = time.monotonic()
        recent = [t for t in self._attempts.get(key, []) if now - t < LOGIN_WINDOW_SECONDS]
        if len(recent) >= LOGIN_ATTEMPTS:
            raise LoginRateLimited("Too many login attempts; wait a few minutes and try again.")
        recent.append(now)
        self._attempts[key] = recent

    async def login(self, user_id: Optional[int], username: str, password: str, otp: str,
                    hf_token: Optional[str] = None) -> dict:
        """Log the user in; raises ``mpcdf_llm.errors.LoginFailed`` on rejected credentials."""
        self._check_rate(user_id)
        if hf_token:
            self.store(user_id).set_hf_token(hf_token)
        manager = await self.manager(user_id)
        info = await manager.login(username, password, otp)
        self._attempts.pop(user_key(user_id), None)
        return self._info_dict(info, user_id)

    async def logout(self, user_id: Optional[int]) -> None:
        manager = await self.manager(user_id)
        await manager.logout()

    async def status(self, user_id: Optional[int]) -> dict:
        """``{"state": "ok"|"login_required", "username", "expires_at", "hf_token_set"}``."""
        if not self.store(user_id).has_session() and user_key(user_id) not in self._managers:
            return {"state": "login_required", "username": None, "expires_at": None,
                    "hf_token_set": bool(self.store(user_id).hf_token())}
        manager = await self.manager(user_id)
        return self._info_dict(await manager.session_status(), user_id)

    def _info_dict(self, info, user_id: Optional[int]) -> dict:
        return {
            "state": info.state,
            "username": info.username,
            "expires_at": info.expires_at.isoformat() if info.expires_at else None,
            "hf_token_set": bool(self.store(user_id).hf_token()),
        }

    # -- endpoints ------------------------------------------------------------------

    async def credentials(self, user_id: Optional[int], preset: str) -> Optional[EndpointCredentials]:
        """The user's ready endpoint for ``preset``, or None (not running, or not logged in)."""
        from mpcdf_llm.errors import LoginRequired

        manager = await self.manager(user_id)
        try:
            for item in await manager.available():
                if item.preset == preset:
                    c = item.credentials
                    return EndpointCredentials(base_url=c.base_url, api_key=c.api_key.get_secret_value(), model=c.model)
        except LoginRequired:
            return None
        return None

    async def endpoint_state(self, user_id: Optional[int], preset: str) -> tuple[str, str]:
        """``(status, detail)`` in ``backend.providers.types.Health`` terms."""
        from mpcdf_llm.errors import LoginRequired, MpcdfError

        if not self.store(user_id).has_session():
            return "unreachable", "log in to the MPCDF service first"
        manager = await self.manager(user_id)
        try:
            if any(a.preset == preset for a in await manager.available()):
                return "ready", ""
        except LoginRequired:
            return "unreachable", "the MPCDF session ended; log in again"
        except MpcdfError as exc:
            return "unreachable", f"MPCDF service error: {type(exc).__name__}"
        starting = any(m.preset == preset and m.ready_at is None for m in manager.state.managed().values())
        if starting:
            return "cold", "the endpoint is starting"
        return "unreachable", "not running"

    async def touch(self, user_id: Optional[int], preset: str) -> None:
        manager = await self.manager(user_id)
        await manager.touch(preset=preset)

    async def put_preset(self, user_id: Optional[int], preset: Any) -> None:
        manager = await self.manager(user_id)
        await manager.put_preset(preset)

    async def start_endpoint(self, user_id: Optional[int], preset: str, progress: Callable[[str], None],
                    until: Optional[datetime] = None, deadline: Optional[float] = None) -> EndpointCredentials:
        """Start (or reuse) the user's endpoint for ``preset`` and wait until it serves.

        Raises ``RuntimeError`` with a user-facing message when the start fails.
        """
        manager = await self.manager(user_id)
        last_phase = None
        async for ev in manager.start(preset, until=until):
            if ev.phase != last_phase:
                progress(ev.phase + (f": {ev.message}" if ev.message else ""))
                last_phase = ev.phase
            if ev.phase == "ready" and ev.result is not None:
                c = ev.result
                return EndpointCredentials(base_url=c.base_url, api_key=c.api_key.get_secret_value(), model=c.model)
            if ev.phase == "login_required":
                raise RuntimeError("The MPCDF session ended; log in again.")
            if ev.phase in ("failed", "canceled", "idle"):
                raise RuntimeError(f"Starting the endpoint ended with '{ev.phase}'" + (f": {ev.message}" if ev.message else ""))
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError("the endpoint did not become ready in time; it keeps starting in the background")
        raise RuntimeError("the start job ended without a result")

    async def stop_endpoint(self, user_id: Optional[int], preset: str) -> None:
        from mpcdf_llm.errors import NotFound

        manager = await self.manager(user_id)
        try:
            await manager.stop(preset=preset)
        except NotFound:
            pass  # nothing running: already paused


_sessions: Optional[MpcdfSessions] = None


def get_sessions() -> MpcdfSessions:
    """The process-wide registry (``<data_path>/system/mpcdf``)."""
    global _sessions
    if _sessions is None:
        from backend.config.settings import get_settings

        _sessions = MpcdfSessions(Path(get_settings().data_path) / "system" / "mpcdf")
    return _sessions


def set_sessions(sessions: Optional[MpcdfSessions]) -> None:
    """Replace the registry (tests)."""
    global _sessions
    _sessions = sessions
