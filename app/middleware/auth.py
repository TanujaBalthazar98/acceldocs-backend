"""Authentication middleware for protecting routes."""

import logging
from typing import Optional

import jwt
from fastapi import Depends, Header, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db

logger = logging.getLogger(__name__)


class TokenData(BaseModel):
    """JWT token payload data."""
    user_id: int
    email: str
    role: str


class AuthUser(BaseModel):
    """Authenticated user information."""
    id: int
    email: str
    role: str
    name: Optional[str] = None


def decode_token(token: str) -> TokenData:
    """
    Decode and validate JWT token.

    Args:
        token: JWT token string

    Returns:
        TokenData with user information

    Raises:
        HTTPException: If token is invalid or expired
    """
    try:
        payload = jwt.decode(
            token,
            settings.secret_key,
            algorithms=["HS256"]
        )

        # JWT uses "sub" for user ID (standard JWT claim)
        user_id = int(payload.get("sub") or payload.get("user_id", 0))

        return TokenData(
            user_id=user_id,
            email=payload["email"],
            role=payload.get("role", "viewer")
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidTokenError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {str(e)}",
            headers={"WWW-Authenticate": "Bearer"},
        )


def get_current_user(authorization: str = Header(None)) -> Optional[AuthUser]:
    """
    Extract and validate user from Authorization header.

    Args:
        authorization: Authorization header value (Bearer <token>)

    Returns:
        AuthUser if authenticated, None if no token provided

    Raises:
        HTTPException: If token is provided but invalid
    """
    if not authorization:
        return None

    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization header format. Expected: Bearer <token>",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = authorization.replace("Bearer ", "")
    token_data = decode_token(token)

    return AuthUser(
        id=token_data.user_id,
        email=token_data.email,
        role=token_data.role
    )


def require_auth(authorization: str = Header(None)) -> AuthUser:
    """
    Require authentication - raises exception if not authenticated.

    Args:
        authorization: Authorization header value

    Returns:
        AuthUser with user information

    Raises:
        HTTPException: If not authenticated
    """
    user = get_current_user(authorization)

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return user


def require_role(min_role: str):
    """
    Dependency factory for role-based access control.

    Args:
        min_role: Minimum required role (viewer, editor, reviewer, admin)

    Returns:
        Dependency function that validates user has required role
    """
    role_hierarchy = {
        "viewer": 0,
        "editor": 1,
        "reviewer": 2,
        "admin": 3,
    }

    min_level = role_hierarchy.get(min_role, 0)

    def role_checker(authorization: str = Header(None)) -> AuthUser:
        # First require authentication
        user = require_auth(authorization)

        # Then check role level
        user_level = role_hierarchy.get(user.role, 0)

        if user_level < min_level:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Insufficient permissions. Required role: {min_role}"
            )

        return user

    return role_checker


def can_access_document(user: Optional[AuthUser], visibility: str) -> bool:
    """
    Check if user can access a document based on visibility.

    Args:
        user: Authenticated user (None if not logged in)
        visibility: Document visibility (public or internal)

    Returns:
        True if user can access, False otherwise
    """
    # Public documents are accessible to everyone
    if visibility == "public":
        return True

    # Internal documents require authentication
    if visibility == "internal":
        return user is not None

    # Unknown visibility - deny by default
    return False


def get_current_user_optional(
    authorization: str = Header(None),
    db: Session = Depends(get_db)
) -> Optional["User"]:
    """
    Get current user from database (optional - returns None if not authenticated).

    Args:
        authorization: Authorization header value (Bearer <token>)
        db: Database session

    Returns:
        User model instance if authenticated, None otherwise

    Raises:
        HTTPException: If token is provided but invalid
    """
    # Avoid circular import
    from app.models import User

    # Get auth user from token
    auth_user = get_current_user(authorization)
    if not auth_user:
        return None

    # Look up user in database
    user = db.query(User).filter(User.id == auth_user.id).first()
    return user
