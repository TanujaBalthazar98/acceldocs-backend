"""Google OAuth authentication routes with JWT session tokens."""

import logging
from datetime import datetime, timedelta, timezone

import httpx
import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import User

logger = logging.getLogger(__name__)

router = APIRouter()

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_HOURS = 24


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: dict


def _create_jwt(user_id: int, email: str) -> str:
    payload = {
        "sub": str(user_id),
        "email": email,
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRY_HOURS),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=JWT_ALGORITHM)


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    """Dependency: extract and validate JWT from Authorization header."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    token = auth_header[7:]
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

    user_id = int(payload["sub"])
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user


@router.get("/login")
async def login():
    """Return the Google OAuth consent URL."""
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
    """Handle OAuth callback — exchange code for tokens, upsert user, return JWT."""
    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code")

    if not settings.google_client_id or not settings.google_client_secret:
        raise HTTPException(status_code=501, detail="Google OAuth not configured")

    # Exchange authorization code for tokens
    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": "http://localhost:8000/auth/callback",
                "grant_type": "authorization_code",
            },
        )

    if token_resp.status_code != 200:
        logger.error("Token exchange failed: %s", token_resp.text)
        raise HTTPException(status_code=400, detail="Failed to exchange authorization code")

    tokens = token_resp.json()
    access_token = tokens.get("access_token")
    if not access_token:
        raise HTTPException(status_code=400, detail="No access token in response")

    # Fetch user info from Google
    async with httpx.AsyncClient() as client:
        userinfo_resp = await client.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )

    if userinfo_resp.status_code != 200:
        raise HTTPException(status_code=400, detail="Failed to fetch user info")

    userinfo = userinfo_resp.json()
    google_id = userinfo.get("sub")
    email = userinfo.get("email")
    name = userinfo.get("name")

    if not google_id or not email:
        raise HTTPException(status_code=400, detail="Incomplete user info from Google")

    # Upsert user in DB
    user = db.query(User).filter(User.google_id == google_id).first()
    if user:
        user.email = email
        user.name = name
    else:
        user = User(google_id=google_id, email=email, name=name, role="viewer")
        db.add(user)
        db.flush()

    db.commit()

    # Create JWT session token
    token = _create_jwt(user.id, user.email)

    return TokenResponse(
        access_token=token,
        user={"id": user.id, "email": user.email, "name": user.name, "role": user.role},
    )


@router.get("/me")
async def me(user: User = Depends(get_current_user)):
    """Return the current authenticated user."""
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "role": user.role,
    }
