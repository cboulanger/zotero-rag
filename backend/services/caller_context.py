"""Who the current request is for: the caller's Zotero user id, or None.

The auth middleware binds it with :func:`use_caller` for every authenticated
request (loopback requests run as ``None``, the one trusted local user), so
services deep in the call chain can reach per-user resources, such as an
endpoint that runs in the caller's own provider session, without plumbing the
id through every signature. Worker threads started with ``asyncio.to_thread``
inherit it. Outside a request (cron, CLI) it is ``None``.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional

_caller: ContextVar[Optional[int]] = ContextVar("zotero_rag_caller", default=None)


def current_user_id() -> Optional[int]:
    """The calling user's Zotero id, or None (loopback, cron, CLI)."""
    return _caller.get()


@contextmanager
def use_caller(user_id: Optional[int]) -> Iterator[None]:
    """Run the enclosed code on behalf of ``user_id``."""
    token = _caller.set(user_id)
    try:
        yield
    finally:
        _caller.reset(token)
