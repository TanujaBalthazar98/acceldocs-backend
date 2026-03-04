"""Health check endpoint."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.database import get_db

router = APIRouter()


@router.get("/health")
async def health():
    return {"status": "ok", "service": "acceldocs-backend"}


@router.post("/admin/reset-database")
async def reset_database(db: Session = Depends(get_db)):
    """TEMPORARY: Reset all data for testing. Remove after use."""
    from app.models import User, Organization, OrgRole, GoogleToken
    for model in [GoogleToken, OrgRole, Organization, User]:
        try:
            db.query(model).delete()
        except Exception:
            db.rollback()
    db.commit()
    return {"status": "ok", "message": "Database reset complete"}
