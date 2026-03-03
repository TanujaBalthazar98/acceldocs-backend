"""Health check endpoint."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import Base, engine, get_db

router = APIRouter()


@router.get("/health")
async def health():
    return {"status": "ok", "service": "acceldocs-backend"}


@router.post("/admin/reset-database")
async def reset_database(db: Session = Depends(get_db)):
    """Drop all tables and recreate them. TEMPORARY — remove after initial setup."""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    return {"ok": True, "message": "Database reset complete. All data deleted."}
