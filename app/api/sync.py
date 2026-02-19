"""Sync API routes — trigger Drive scanning and content sync."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.ingestion.sync import run_full_sync

router = APIRouter()


@router.post("/trigger")
async def trigger_sync(db: Session = Depends(get_db)):
    """Trigger a full Drive scan and content sync."""
    summary = run_full_sync(db)
    if "error" in summary:
        return {"status": "error", **summary}
    return {"status": "ok", **summary}
