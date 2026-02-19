"""Sync API routes — trigger Drive scanning and content sync."""

from fastapi import APIRouter

router = APIRouter()


@router.post("/trigger")
async def trigger_sync():
    """Trigger a full Drive scan and content sync.

    Phase 5 will implement the actual Drive scanning logic.
    For now, returns a placeholder response.
    """
    return {"status": "ok", "message": "Sync not yet implemented (Phase 5)"}
