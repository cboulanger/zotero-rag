"""Reconcile Zotero "indexed" tags with the backend's indexed-attachment truth.

Ground truth is ``VectorStore.get_indexed_attachment_keys`` (chunks with an
``attachment_key``), the same data the real-time event hooks in ``VectorStore``
report on. This module only *plans*: the stored auto-index keys are read-only
(write-scoped keys are rejected at submission), so the backend cannot write tags
to Zotero itself. It emits tag operations for the plugin, which owns the local
library and applies them with ``item.addTag`` / ``removeTag``.

Each library's attachments are reconciled page by page, reading indexed state
fresh for every page. Every ``ops`` record carries ``as_of_seq`` — the event-log
head read *before* that page's indexed state — so a consumer that has already
applied a newer real-time event for an attachment can discard the stale op.
"""

import json
import logging
import sys
from pathlib import Path
from typing import Any, Callable, Optional

from backend.api.public_query import slug_to_backend_id
from backend.services.failed_attachments import FailedAttachmentStore
from backend.services.index_event_log import FAILED_TAG_NAME, INDEXED_TAG_NAME, IndexEventLog

logger = logging.getLogger(__name__)

OP_ADD = "add"
OP_REMOVE = "remove"


class JsonLinesWriter:
    """Writes one JSON object per line to a file (flushed per line) and/or stdout.

    Same line-oriented, flush-per-record contract a poller can tail: the file is
    append-only, so a reader holding a byte offset never re-reads or misses lines.
    """

    def __init__(self, path: Optional[Path] = None, stdout: bool = False) -> None:
        self._file = None
        self._stdout = stdout
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._file = open(path, "a", encoding="utf-8")

    def __call__(self, record: dict) -> None:
        line = json.dumps(record, ensure_ascii=False)
        if self._file is not None:
            self._file.write(line + "\n")
            self._file.flush()
        if self._stdout:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None


def has_tag(item_data: dict, tag: str) -> bool:
    return any(t.get("tag") == tag for t in item_data.get("tags", []))


def plan_tag_ops(
    attachments: list[dict], indexed_keys: set[str], tag: str = INDEXED_TAG_NAME, kind: Optional[str] = None
) -> list[dict]:
    """Return the minimal tag operations that make Zotero agree with the backend.

    ``attachments`` are Zotero web-API item dicts. Attachments already in the
    desired state produce nothing, so a second run over unchanged data plans zero
    operations (idempotency).
    """
    ops: list[dict] = []
    for item in attachments:
        data = item.get("data", {})
        key = data.get("key")
        if not key:
            continue
        tagged = has_tag(data, tag)
        indexed = key in indexed_keys
        if indexed and not tagged:
            op = OP_ADD
        elif tagged and not indexed:
            op = OP_REMOVE
        else:
            continue
        planned = {"op": op, "attachment_key": key, "item_key": data.get("parentItem") or key}
        if kind:
            planned["kind"] = kind
        ops.append(planned)
    return ops


class IndexedTagSync:
    """Plans tag operations for a set of libraries, streaming records to ``emit``."""

    def __init__(
        self,
        vector_store: Any,
        event_log: IndexEventLog,
        emit: Callable[[dict], None],
        web_api_factory: Callable[[str], Any],
        tag: str = INDEXED_TAG_NAME,
        writer_factory: Optional[Callable[[str], Any]] = None,
        dry_run: bool = False,
        failed_store: Optional[FailedAttachmentStore] = None,
        failed_tag: str = FAILED_TAG_NAME,
    ) -> None:
        self.vector_store = vector_store
        self.event_log = event_log
        self.emit = emit
        self.web_api_factory = web_api_factory
        self.tag = tag
        # With a write-scoped key the planned operations are applied to zotero.org
        # directly; without one (the server/plugin path) they are only emitted.
        self.writer_factory = writer_factory
        self.dry_run = dry_run
        # Optional second tag: attachments the backend refuses to process. Its ops
        # carry kind="failed" and are only emitted (the plugin applies them).
        self.failed_store = failed_store
        self.failed_tag = failed_tag

    def _build_updates(self, page: list[dict], ops: list[dict]) -> list[dict]:
        """Full tag lists (Zotero replaces, not merges) with the tag added/removed."""
        by_key = {i["data"]["key"]: i["data"] for i in page}
        updates = []
        for op in ops:
            data = by_key[op["attachment_key"]]
            tags = [t for t in data.get("tags", []) if t.get("tag") != self.tag]
            if op["op"] == OP_ADD:
                tags.append({"tag": self.tag, "type": 1})
            updates.append({"key": data["key"], "version": data["version"], "tags": tags})
        return updates

    async def run_library(self, slug: str) -> dict:
        """Reconcile one library; returns its counters. Raises on listing errors."""
        import asyncio

        backend_id = slug_to_backend_id(slug)
        library_type = "user" if slug.startswith("users/") else "group"
        self.emit({"type": "library_start", "library": slug})
        counts = {"attachments_checked": 0, "to_add": 0, "to_remove": 0, "already_correct": 0}
        if self.writer_factory is not None and not self.dry_run:
            counts.update(written=0, write_failed=0)

        async with self.web_api_factory(slug) as web_api:
            async for page in web_api.iter_attachment_pages(backend_id, library_type):
                # Head sampled BEFORE reading indexed state: any event with a
                # greater seq happened after (or concurrently with) this read.
                as_of_seq = await asyncio.to_thread(self.event_log.last_seq)
                keys = [i["data"]["key"] for i in page if i.get("data", {}).get("key")]
                indexed = await asyncio.to_thread(
                    self.vector_store.get_indexed_attachment_keys, backend_id, keys
                )
                ops = plan_tag_ops(page, indexed, self.tag)
                adds = sum(1 for o in ops if o["op"] == OP_ADD)
                counts["attachments_checked"] += len(page)
                counts["to_add"] += adds
                counts["to_remove"] += len(ops) - adds
                counts["already_correct"] += len(page) - len(ops)
                if ops:
                    self.emit({"type": "ops", "library": slug, "as_of_seq": as_of_seq, "ops": ops})
                    if self.writer_factory is not None and not self.dry_run:
                        async with self.writer_factory(slug) as writer:
                            outcome = await writer.update_item_tags(
                                backend_id, self._build_updates(page, ops), library_type
                            )
                        counts["written"] += len(outcome["written"])
                        counts["write_failed"] += len(outcome["failed"])
                        self.emit({"type": "applied", "library": slug, **outcome})
                if self.failed_store is not None:
                    failed_keys = await asyncio.to_thread(self.failed_store.failed_keys, backend_id, keys)
                    failed_ops = plan_tag_ops(page, failed_keys, self.failed_tag, kind="failed")
                    if failed_ops:
                        self.emit({"type": "ops", "library": slug, "as_of_seq": as_of_seq, "ops": failed_ops})
                self.emit({"type": "progress", "library": slug, **counts})

        self.emit({"type": "library_done", "library": slug, **counts})
        return counts

    async def run(self, slugs: list[str]) -> dict:
        """Reconcile each library; a failing library is reported, not fatal."""
        totals = {"attachments_checked": 0, "to_add": 0, "to_remove": 0, "already_correct": 0, "libraries_failed": 0}
        if self.writer_factory is not None and not self.dry_run:
            totals.update(written=0, write_failed=0)
        for slug in slugs:
            try:
                counts = await self.run_library(slug)
            except Exception as exc:  # noqa: BLE001 - one bad library must not stop the rest
                logger.warning("Tag sync failed for %s: %s", slug, exc, exc_info=True)
                self.emit({"type": "library_error", "library": slug, "error": str(exc)})
                totals["libraries_failed"] += 1
                continue
            for k, v in counts.items():
                totals[k] = totals.get(k, 0) + v
        return totals
