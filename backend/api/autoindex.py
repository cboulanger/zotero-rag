"""Auto-index key endpoints.

POST   /api/autoindex/keys  — submit a read-only key (validated, stored encrypted)
DELETE /api/autoindex/keys  — remove a key
GET    /api/autoindex/keys  — list key metadata (admin; no plaintext)

All endpoints are protected by the global X-API-Key middleware. When
AUTOINDEX_SECRET is unset the feature is disabled and endpoints return 503.
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.config.settings import get_settings
from backend.services.autoindex_key_store import AutoIndexKeyStore
from backend.zotero.key_validator import validate_key

router = APIRouter()
logger = logging.getLogger(__name__)


class KeyRequest(BaseModel):
    api_key: str


def _store() -> AutoIndexKeyStore:
    settings = get_settings()
    store = AutoIndexKeyStore(settings.autoindex_keys_path, settings.autoindex_secret)
    if not store.enabled:
        raise HTTPException(
            status_code=503,
            detail="Auto-indexing is not configured on this server (AUTOINDEX_SECRET unset).",
        )
    return store


@router.post("/autoindex/keys", summary="Submit a read-only auto-index key")
async def add_key(request: KeyRequest) -> dict:
    store = _store()
    validation = await validate_key(request.api_key)
    if not validation.read_only:
        raise HTTPException(status_code=400, detail=validation.reason or "Key is not read-only.")
    store.add(request.api_key, validation)
    return {
        "user_id": validation.user_id,
        "username": validation.username,
        "targets": validation.targets,
    }


@router.delete("/autoindex/keys", summary="Remove an auto-index key")
async def delete_key(request: KeyRequest) -> dict:
    store = _store()
    removed = store.remove_by_key(request.api_key)
    return {"removed": removed}


@router.get("/autoindex/keys", summary="List auto-index key metadata (admin)")
async def list_keys() -> dict:
    store = _store()
    return {"keys": store.list_metadata()}
