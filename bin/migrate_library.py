"""
Migrate one Zotero library's indexed RAG data (document_chunks,
deduplication records, and index metadata) from one zotero-rag backend
instance to another.

Usage:
    uv run python bin/migrate_library.py <slug> <source-url> <dest-url>
        --source-key <source-admin-zotero-api-key>
        --dest-key <dest-admin-zotero-api-key>
        [--batch-size 200] [--dry-run]

<slug> is a Zotero.org library slug, e.g. users/39226 or groups/6297749.
--source-key/--dest-key must belong to an account that is an owner/admin of
the respective instance's AUTHORIZED_GROUP_ID (omit for a loopback-mode
instance, which needs no admin key).

The destination's existing data for this library is fully overwritten. A
failed run is recovered by simply re-running the script — the destination
is re-cleared on every run; there is no resume-from-cursor logic.

See docs/superpowers/specs/2026-10-05-library-rag-migration-design.md.
"""

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import httpx  # noqa: E402

from backend.api.public_query import slug_to_backend_id  # noqa: E402
from backend.utils.migration_state import (  # noqa: E402
    MIGRATION_COLLECTIONS,
    delete_state,
    load_state,
    new_state,
    save_state,
    state_path,
)

_MAX_ATTEMPTS = 4


class MigrationError(RuntimeError):
    """Raised for any unrecoverable failure during migration; main() catches it and exits 1."""


def _headers(api_key: Optional[str]) -> dict:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-Zotero-API-Key"] = api_key
    return headers


def _request(
    client, method: str, base_url: str, path: str, api_key: Optional[str],
    params: Optional[dict] = None, json_body: Optional[dict] = None,
) -> dict:
    url = base_url.rstrip("/") + path
    query = {k: v for k, v in (params or {}).items() if v is not None}
    attempt = 0
    while True:
        attempt += 1
        try:
            response = client.request(method, url, headers=_headers(api_key), params=query, json=json_body, timeout=120.0)
        except (httpx.ConnectError, httpx.ReadTimeout) as exc:
            if attempt >= _MAX_ATTEMPTS:
                raise MigrationError(f"{method} {url} failed after {_MAX_ATTEMPTS} attempts: {exc}") from exc
            time.sleep(2 ** (attempt - 1))
            continue
        if response.status_code >= 500:
            if attempt >= _MAX_ATTEMPTS:
                raise MigrationError(
                    f"{method} {url} -> HTTP {response.status_code} after {_MAX_ATTEMPTS} attempts: {response.text}"
                )
            time.sleep(2 ** (attempt - 1))
            continue
        break
    if response.status_code != 200:
        raise MigrationError(f"{method} {url} -> HTTP {response.status_code}: {response.text}")
    return response.json()


def _get(client, base_url: str, path: str, api_key: Optional[str], **params) -> dict:
    return _request(client, "GET", base_url, path, api_key, params=params)


def _post(client, base_url: str, path: str, api_key: Optional[str], json_body: dict, **params) -> dict:
    return _request(client, "POST", base_url, path, api_key, params=params, json_body=json_body)


def run_migration(
    client,
    slug: str,
    source_url: str,
    dest_url: str,
    source_key: Optional[str],
    dest_key: Optional[str],
    batch_size: int = 200,
    dry_run: bool = False,
    mode: str = "clean",
    data_path: Optional[Path] = None,
) -> dict:
    """Run the full migration (or, if dry_run, just the compatibility/size check).

    `mode` is "clean" (ignore/clear any prior state and start fresh, the
    default) or "resume" (continue a previously interrupted run for this
    exact slug/source/dest combination; raises MigrationError if no
    matching state file exists). The clean-vs-resume *prompt* shown when
    the caller hasn't decided yet lives in main(), not here — this
    function always receives an already-resolved mode so it stays
    non-interactive and testable.

    `data_path` is the base directory under which the state checkpoint
    file is kept (at `<data_path>/system/migration_state/`). If omitted,
    it's resolved from `get_settings().data_path`.

    Returns a summary dict used by main() for its console output and by
    tests for assertions.
    """
    if mode not in ("clean", "resume"):
        raise MigrationError(f"Invalid mode: {mode!r} (must be 'clean' or 'resume')")

    library_id = slug_to_backend_id(slug)

    source_info = _get(client, source_url, "/api/migration/embedding-info", source_key)
    dest_info = _get(client, dest_url, "/api/migration/embedding-info", dest_key)
    source_model = (source_info["embedding_model_name"], source_info["embedding_dim"])
    dest_model = (dest_info["embedding_model_name"], dest_info["embedding_dim"])
    if source_model != dest_model:
        raise MigrationError(
            f"Embedding mismatch: source uses {source_model[0]} ({source_model[1]}-dim), "
            f"destination uses {dest_model[0]} ({dest_model[1]}-dim). "
            "Re-index on the destination instead of migrating vectors between incompatible models."
        )

    metadata = _get(client, source_url, "/api/migration/export/metadata", source_key, library_id=library_id)

    if dry_run:
        return {"library_id": library_id, "dry_run": True, "metadata": metadata}

    if data_path is None:
        from backend.config.settings import get_settings
        data_path = get_settings().data_path
    path = state_path(slug, source_url, dest_url, data_path)

    if mode == "resume":
        state = load_state(path)
        if state is None:
            raise MigrationError(
                f"No incomplete migration found for {slug} ({source_url} -> {dest_url}); "
                "use --mode=clean or omit --mode to start fresh."
            )
        if (state["embedding_model_name"], state["embedding_dim"]) != source_model:
            raise MigrationError(
                "Embedding config changed since the interrupted run: recorded "
                f"{state['embedding_model_name']} ({state['embedding_dim']}-dim), "
                f"now {source_model[0]} ({source_model[1]}-dim). Use --mode=clean to start fresh."
            )
    else:
        delete_state(path)
        state = new_state(
            slug, source_url, dest_url, library_id,
            source_model[0], source_model[1],
            started_at=datetime.now(timezone.utc).isoformat(),
        )
        save_state(path, state)

    if not state["begin_done"]:
        begin_result = _post(client, dest_url, "/api/migration/import/begin", dest_key, {"library_id": library_id})
        state["begin_done"] = True
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        save_state(path, state)
    else:
        begin_result = None

    transferred = {}
    for collection in MIGRATION_COLLECTIONS:
        coll_state = state["collections"][collection]
        total = _get(
            client, source_url, "/api/migration/export/count", source_key,
            library_id=library_id, collection=collection,
        )["count"]
        count = coll_state["transferred"]
        offset = coll_state["cursor"]
        if coll_state["done"]:
            print(f"[OK] {collection}: {count}/{total} transferred (already complete)", file=sys.stderr)
            transferred[collection] = count
            continue
        while True:
            page = _get(
                client, source_url, "/api/migration/export", source_key,
                library_id=library_id, collection=collection, offset=offset, limit=batch_size,
            )
            points = page["points"]
            if points:
                _post(
                    client, dest_url, "/api/migration/import", dest_key,
                    {"library_id": library_id, "points": points}, collection=collection,
                )
                count += len(points)
            offset = page["next_offset"]
            coll_state["cursor"] = offset
            coll_state["transferred"] = count
            coll_state["done"] = offset is None
            state["updated_at"] = datetime.now(timezone.utc).isoformat()
            save_state(path, state)
            print(f"[..] {collection}: {count}/{total} transferred", end="\r", file=sys.stderr)
            if offset is None:
                break
        print(f"[OK] {collection}: {count}/{total} transferred", file=sys.stderr)
        transferred[collection] = count

    if not state["metadata_done"]:
        _post(client, dest_url, "/api/migration/import/metadata", dest_key, {"library_id": library_id, "payload": metadata})
        state["metadata_done"] = True
        save_state(path, state)

    delete_state(path)

    return {
        "library_id": library_id,
        "dry_run": False,
        "begin_result": begin_result,
        "transferred": transferred,
        "metadata": metadata,
    }


def _parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("slug", help="Zotero library slug, e.g. users/39226 or groups/6297749")
    parser.add_argument("source_url", help="Source instance base URL, e.g. https://rag.example.com")
    parser.add_argument("dest_url", help="Destination instance base URL, e.g. http://localhost:8119")
    parser.add_argument("--source-key", default=None, help="Admin Zotero API key for the source instance")
    parser.add_argument("--dest-key", default=None, help="Admin Zotero API key for the destination instance")
    parser.add_argument("--batch-size", type=int, default=200, help="Points per export/import page (default: 200)")
    parser.add_argument("--dry-run", action="store_true", help="Check compatibility and report counts without writing anything")
    return parser.parse_args(argv)


def main() -> None:
    args = _parse_args()
    start = time.monotonic()
    try:
        with httpx.Client() as client:
            result = run_migration(
                client, args.slug, args.source_url, args.dest_url,
                args.source_key, args.dest_key, args.batch_size, args.dry_run,
            )
    except (MigrationError, ValueError) as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        sys.exit(1)

    if result["dry_run"]:
        meta = result["metadata"]
        print(
            f"[OK] --dry-run: {args.slug} ({result['library_id']}) has {meta['total_chunks']} chunks, "
            f"{meta['total_items_indexed']} items on {args.source_url}. No data written."
        )
        return

    print(
        f"[OK] Cleared destination: {result['begin_result']['chunks_deleted']} chunks, "
        f"{result['begin_result']['dedup_deleted']} dedup records."
    )
    # Per-collection "N/total transferred" progress/completion lines are already
    # printed to stderr by run_migration as each collection finishes; avoid
    # reprinting the same counts here and just give the overall wrap-up.
    total_points = sum(result["transferred"].values())
    print(f"[OK] {total_points} points and metadata transferred. Done in {time.monotonic() - start:.1f}s")


if __name__ == "__main__":
    main()
