"""Unit tests for bin/library_snapshot.py using an in-memory fake of the migration API."""

import gzip
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from bin import library_snapshot
from bin.migrate_library import MigrationError

LIB = "6297749"  # slug_to_backend_id("groups/6297749")


class FakeResponse:
    status_code = 200

    def __init__(self, body):
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


class FakeInstance:
    """Stateful stand-in for an httpx.Client talking to /api/migration/*."""

    def __init__(self, model="e5", dim=3, chunks=(), dedup=(), metadata=None):
        self.info = {"embedding_model_name": model, "embedding_dim": dim}
        self.data = {"chunks": list(chunks), "dedup": list(dedup)}
        self.metadata = metadata or {"total_chunks": len(chunks), "total_items_indexed": 1}
        self.calls = []
        self.library_changes_during_export = False

    def request(self, method, url, headers=None, params=None, json=None, timeout=None):
        path = url.split("/api", 1)[1]
        self.calls.append((method, path))
        if path == "/migration/embedding-info":
            return FakeResponse(self.info)
        if path == "/migration/export/metadata":
            return FakeResponse(self.metadata)
        if path == "/migration/export/count":
            return FakeResponse({"count": len(self.data[params["collection"]])})
        if path == "/migration/export":
            pts, off, lim = self.data[params["collection"]], int(params.get("offset") or 0), params["limit"]
            page = pts[off:off + lim]
            if self.library_changes_during_export and params["collection"] == "chunks":
                page = page[:-1] if off + lim >= len(pts) else page
            nxt = str(off + lim) if off + lim < len(pts) else None
            return FakeResponse({"points": page, "next_offset": nxt})
        if path == "/migration/import/begin":
            deleted = {"chunks_deleted": len(self.data["chunks"]), "dedup_deleted": len(self.data["dedup"]),
                       "metadata_deleted": True, "library_id": json["library_id"]}
            self.data = {"chunks": [], "dedup": []}
            return FakeResponse(deleted)
        if path == "/migration/import":
            self.data[params["collection"]].extend(json["points"])
            return FakeResponse({})
        if path == "/migration/import/metadata":
            self.metadata = json["payload"]
            return FakeResponse({})
        raise AssertionError(path)


def _points(n, prefix="c"):
    return [{"id": f"{prefix}{i}", "vector": [0.1, 0.2, 0.3], "payload": {"text": f"t{i}"}} for i in range(n)]


class SnapshotRoundTripTest(unittest.TestCase):
    def test_export_then_import_restores_everything(self):
        src = FakeInstance(chunks=_points(5), dedup=_points(2, "d"))
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "gold.jsonl.gz"
            header = library_snapshot.export_snapshot(src, "groups/6297749", "http://src", out, batch_size=2)
            self.assertEqual(header["counts"], {"chunks": 5, "dedup": 2})
            self.assertEqual(library_snapshot.read_header(out)["library_id"], LIB)
            self.assertFalse(out.with_name(out.name + ".partial").exists())

            dest = FakeInstance(chunks=_points(1, "old"))
            result = library_snapshot.import_snapshot(dest, out, "http://dest", batch_size=2)
        self.assertEqual(result["imported"], {"chunks": 5, "dedup": 2})
        self.assertEqual(dest.data, src.data)  # destination's old chunks were replaced
        self.assertEqual(dest.metadata, src.metadata)

    def test_import_refuses_embedding_mismatch_before_writing(self):
        src = FakeInstance(chunks=_points(2))
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "s.jsonl.gz"
            library_snapshot.export_snapshot(src, "groups/6297749", "http://src", out)
            dest = FakeInstance(model="other", chunks=_points(1, "keep"))
            with self.assertRaises(MigrationError):
                library_snapshot.import_snapshot(dest, out, "http://dest")
        self.assertNotIn(("POST", "/migration/import/begin"), dest.calls)
        self.assertEqual(len(dest.data["chunks"]), 1)

    def test_export_of_empty_library_fails(self):
        with TemporaryDirectory() as tmp, self.assertRaises(MigrationError):
            library_snapshot.export_snapshot(FakeInstance(), "groups/6297749", "http://src", Path(tmp) / "x.gz")

    def test_export_detects_library_changing_mid_export_and_leaves_no_file(self):
        src = FakeInstance(chunks=_points(4))
        src.library_changes_during_export = True
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "x.jsonl.gz"
            with self.assertRaises(MigrationError):
                library_snapshot.export_snapshot(src, "groups/6297749", "http://src", out, batch_size=2)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_truncated_snapshot_is_rejected_on_import(self):
        src = FakeInstance(chunks=_points(3))
        with TemporaryDirectory() as tmp:
            good = Path(tmp) / "g.jsonl.gz"
            library_snapshot.export_snapshot(src, "groups/6297749", "http://src", good)
            lines = gzip.open(good, "rt").read().splitlines()
            bad = Path(tmp) / "b.jsonl.gz"
            with gzip.open(bad, "wt") as fh:
                fh.write("\n".join(lines[:-1]) + "\n")
            dest = FakeInstance(chunks=_points(1, "keep"))
            with self.assertRaises(MigrationError):
                library_snapshot.import_snapshot(dest, bad, "http://dest")
        self.assertEqual(dest.calls, [])  # rejected before touching the destination

    def test_rejects_foreign_file(self):
        with TemporaryDirectory() as tmp:
            p = Path(tmp) / "f.gz"
            with gzip.open(p, "wt") as fh:
                fh.write(json.dumps({"format": "nope"}) + "\n")
            with self.assertRaises(MigrationError):
                library_snapshot.read_header(p)


if __name__ == "__main__":
    unittest.main()
