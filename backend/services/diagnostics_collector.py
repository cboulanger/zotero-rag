"""Per-request diagnostics capture for document uploads.

A :class:`DiagnosticsCollector` is created by ``_execute_upload()`` when the
client asks for diagnostics.  It records processing stages and, via a
context-scoped logging handler, every ``backend.*`` log record emitted while the
request runs (including from ``asyncio.to_thread`` workers, since contextvars
propagate there).  Concurrent requests never see each other's records.

Instrumentation elsewhere uses :func:`stage` / :func:`current`, which are
no-ops when no collector is active, so normal indexing is unaffected.

Log capture needs DEBUG records, which are normally not created.  While at
least one collector is active the capture loggers are lowered to DEBUG
(reference-counted).  Pre-existing root handlers get a filter that drops
records below the originally effective level for those loggers, so console/file
log volume does not change.
"""

import logging
import re
import threading
import time
import traceback
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

from backend.models.diagnostics import (
    DiagnosticLogRecord,
    DiagnosticsPayload,
    DiagnosticStage,
)

logger = logging.getLogger(__name__)

DIAG_MAX_LOG_RECORDS = 500
DIAG_MAX_BYTES = 256 * 1024
DIAG_MAX_BODY_EXCERPT = 2048

# Loggers whose DEBUG output is captured for a diagnostics request.
CAPTURE_LOGGERS = (
    "backend.api.document_upload",
    "backend.services.document_processor",
    "backend.services.extraction",
    "backend.services.embeddings",
    "backend.db",
)

_SECRET_PATTERNS = [
    (re.compile(r"(?i)(authorization\s*[:=]\s*)\S+(\s+\S+)?"), r"\1[REDACTED]"),
    (re.compile(r"(?i)((?:api[_-]?key|secret|token|password)\s*[:=]\s*)[^\s,;'\"]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(x-[a-z0-9-]*-?key\s*[:=]\s*)\S+"), r"\1[REDACTED]"),
]

_active: ContextVar[Optional["DiagnosticsCollector"]] = ContextVar(
    "diagnostics_collector", default=None
)


def scrub_secrets(text: str) -> str:
    """Mask secret-looking ``key: value`` patterns (defense in depth)."""
    for pattern, repl in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def body_excerpt(text: str, limit: int = DIAG_MAX_BODY_EXCERPT) -> str:
    """Truncate an HTTP response body for inclusion in a diagnostics stage."""
    text = scrub_secrets(text or "")
    if len(text) <= limit:
        return text
    return text[:limit] + f"... [truncated {len(text) - limit} chars]"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class _StageHandle:
    """Mutable handle yielded by :meth:`DiagnosticsCollector.stage`."""

    def __init__(self) -> None:
        self.details: dict[str, Any] = {}
        self.outcome: str = "ok"

    def set(self, **kwargs: Any) -> None:
        self.details.update(kwargs)

    def skip(self, **kwargs: Any) -> None:
        self.outcome = "skipped"
        self.details.update(kwargs)


class DiagnosticsCollector:
    """Accumulates stages, log records and the final error for one request."""

    def __init__(self, server: Optional[dict[str, Any]] = None) -> None:
        self.request_id = uuid.uuid4().hex[:12]
        self._server = server or {}
        self._stages: list[DiagnosticStage] = []
        self._logs: list[DiagnosticLogRecord] = []
        self._logs_dropped = 0
        self._error: Optional[dict[str, str]] = None
        self._final_status = ""
        self._lock = threading.Lock()

    @contextmanager
    def stage(self, name: str) -> Iterator[_StageHandle]:
        """Record timing + outcome of a stage; an escaping exception marks it ``error``."""
        handle = _StageHandle()
        started = _now_iso()
        t0 = time.monotonic()
        try:
            yield handle
        except BaseException as exc:
            handle.outcome = "error"
            handle.details.setdefault("error", f"{type(exc).__name__}: {scrub_secrets(str(exc))}")
            raise
        finally:
            with self._lock:
                self._stages.append(DiagnosticStage(
                    name=name,
                    started_at=started,
                    duration_ms=int((time.monotonic() - t0) * 1000),
                    outcome=handle.outcome,
                    details=handle.details,
                ))

    def add_log(self, record: logging.LogRecord) -> None:
        """Append a log record, dropping the oldest past the cap."""
        exc_text = None
        if record.exc_info:
            exc_text = scrub_secrets("".join(traceback.format_exception(*record.exc_info)))
        entry = DiagnosticLogRecord(
            ts=datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            level=record.levelname,
            logger=record.name,
            message=scrub_secrets(record.getMessage()),
            exc_text=exc_text,
        )
        with self._lock:
            self._logs.append(entry)
            if len(self._logs) > DIAG_MAX_LOG_RECORDS:
                self._logs.pop(0)
                self._logs_dropped += 1

    def set_error(self, exc: BaseException) -> None:
        self._error = {
            "type": type(exc).__name__,
            "message": scrub_secrets(str(exc)),
            "traceback": scrub_secrets("".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            )),
        }

    def set_final_status(self, status: str) -> None:
        self._final_status = status

    def finalize(self) -> DiagnosticsPayload:
        """Build the payload, enforcing the serialized-size cap (oldest logs dropped first)."""
        with self._lock:
            logs = list(self._logs)
            dropped = self._logs_dropped
            stages = list(self._stages)
        payload = DiagnosticsPayload(
            request_id=self.request_id,
            server=self._server,
            stages=stages,
            log_records=logs,
            final_status=self._final_status,
            error=self._error,
        )
        while logs and len(payload.model_dump_json()) > DIAG_MAX_BYTES:
            drop = max(1, len(logs) // 10)
            logs = logs[drop:]
            dropped += drop
            payload.log_records = logs
        if dropped:
            payload.truncated = {"log_records_dropped": dropped}
        return payload


class _CaptureHandler(logging.Handler):
    """Routes records to the collector active in the *current context*."""

    def emit(self, record: logging.LogRecord) -> None:
        collector = _active.get()
        if collector is None:
            return
        try:
            collector.add_log(record)
        except Exception:  # never let capture break the request
            pass


class _DropBelowFilter(logging.Filter):
    """On pre-existing handlers: drop records below the original effective level."""

    def __init__(self, thresholds: dict[str, int]) -> None:
        super().__init__()
        self._thresholds = thresholds

    def filter(self, record: logging.LogRecord) -> bool:
        for name, level in self._thresholds.items():
            if record.name == name or record.name.startswith(name + "."):
                return record.levelno >= level
        return True


_lock = threading.Lock()
_refcount = 0
_capture_handler: Optional[_CaptureHandler] = None
_saved_levels: dict[str, int] = {}
_guarded: list[tuple[logging.Handler, logging.Filter]] = []


def _acquire_logging() -> None:
    global _refcount, _capture_handler
    with _lock:
        _refcount += 1
        if _refcount > 1:
            return
        _capture_handler = _CaptureHandler(level=logging.DEBUG)
        thresholds: dict[str, int] = {}
        for name in CAPTURE_LOGGERS:
            lg = logging.getLogger(name)
            thresholds[name] = lg.getEffectiveLevel()
            _saved_levels[name] = lg.level
        flt = _DropBelowFilter(thresholds)
        for handler in logging.getLogger().handlers:
            handler.addFilter(flt)
            _guarded.append((handler, flt))
        for name in CAPTURE_LOGGERS:
            lg = logging.getLogger(name)
            lg.addHandler(_capture_handler)
            lg.setLevel(logging.DEBUG)


def _release_logging() -> None:
    global _refcount, _capture_handler
    with _lock:
        _refcount -= 1
        if _refcount > 0:
            return
        for name in CAPTURE_LOGGERS:
            lg = logging.getLogger(name)
            if _capture_handler is not None:
                lg.removeHandler(_capture_handler)
            lg.setLevel(_saved_levels.get(name, logging.NOTSET))
        for handler, flt in _guarded:
            handler.removeFilter(flt)
        _guarded.clear()
        _saved_levels.clear()
        _capture_handler = None
        _refcount = 0


@contextmanager
def activate(collector: DiagnosticsCollector) -> Iterator[DiagnosticsCollector]:
    """Make ``collector`` the active one for the current context and capture logs."""
    _acquire_logging()
    token = _active.set(collector)
    try:
        logger.info(f"Diagnostics capture started (request_id={collector.request_id})")
        yield collector
    finally:
        _active.reset(token)
        _release_logging()


def current() -> Optional[DiagnosticsCollector]:
    """Return the collector active in this context, if any."""
    return _active.get()


@contextmanager
def stage(name: str) -> Iterator[_StageHandle]:
    """Record a stage on the active collector; a cheap no-op without one."""
    collector = _active.get()
    if collector is None:
        yield _StageHandle()
        return
    with collector.stage(name) as handle:
        yield handle


def build_server_info(embedding_service: Any = None) -> dict[str, Any]:
    """Allowlisted, non-secret server facts (never dumps settings wholesale)."""
    import sys
    from urllib.parse import urlparse

    from backend.config.settings import get_settings

    s = get_settings()
    info: dict[str, Any] = {
        "backend_version": s.version,
        "extractor_backend": s.extractor_backend,
        "ocr_enabled": s.ocr_enabled,
        "kreuzberg_timeout_cap_seconds": s.kreuzberg_timeout_seconds,
        "kreuzberg_host": urlparse(s.kreuzberg_url).hostname,
        "pdf_split_threshold_bytes": s.pdf_split_threshold,
        "vector_store_mode": "server" if s.qdrant_url else "embedded",
        "python_version": sys.version.split()[0],
    }
    if embedding_service is not None:
        for attr in ("model_name", "model", "dimensions"):
            val = getattr(embedding_service, attr, None)
            if isinstance(val, (str, int)):
                info[f"embedding_{attr}"] = val
    return info
