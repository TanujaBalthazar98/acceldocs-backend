"""Google OAuth authentication routes."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import User

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/login")
async def login():
    """Redirect to Google OAuth consent screen.

    Full implementation in Phase 8 with authlib.
    """
    if not settings.google_client_id:
        raise HTTPException(status_code=501, detail="Google OAuth not configured")

    return {
        "url": (
            "https://accounts.google.com/o/oauth2/v2/auth"
            f"?client_id={settings.google_client_id}"
            "&response_type=code"
            "&scope=openid%20email%20profile"
            "&redirect_uri=http://localhost:8000/auth/callback"
            "&access_type=offline"
        )
    }


@router.get("/callback")
async def callback(code: str | None = None, db: Session = Depends(get_db)):
    """Handle OAuth callback — exchange code for tokens, upsert user.

    Full implementation in Phase 8 with authlib token exchange.
    """
    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code")

    # Phase 8: exchange code for tokens via authlib
    # Phase 8: fetch user info from Google
    # Phase 8: upsert user in DB
    # Phase 8: return JWT session token

    return {"status": "ok", "message": "OAuth callback not yet implemented (Phase 8)"}


@router.get("/me")
async def me():
    """Return current authenticated user.

    Phase 8 will add JWT middleware to extract user from token.
    """
    return {"status": "ok", "message": "Auth middleware not yet implemented (Phase 8)"}
