"""Health check endpoint."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.database import get_db

router = APIRouter()


@router.get("/health")
async def health():
    return {"status": "ok", "service": "acceldocs-backend"}


@router.post("/admin/clear-drive-folder")
async def clear_drive_folder(db: Session = Depends(get_db)):
    """TEMPORARY: Clear drive_folder_id from all orgs to re-trigger onboarding."""
    from app.models import Organization
    count = db.query(Organization).update({Organization.drive_folder_id: None})
    db.commit()
    return {"status": "ok", "orgs_cleared": count}
