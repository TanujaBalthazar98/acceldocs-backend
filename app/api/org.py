"""Org API — current organization info, settings, and member management."""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth.routes import get_current_user
from app.database import get_db
from app.lib.slugify import to_slug as slugify
from app.models import GoogleToken, Invitation, OrgRole, Organization, User

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class OrgUpdate(BaseModel):
    name: str | None = None
    logo_url: str | None = None
    primary_color: str | None = None
    tagline: str | None = None


class InviteCreate(BaseModel):
    email: str
    role: str = "editor"  # owner | admin | editor | viewer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_org(user: User, db: Session) -> tuple[Organization, OrgRole]:
    role = db.query(OrgRole).filter(OrgRole.user_id == user.id).first()
    if not role:
        raise HTTPException(status_code=403, detail="User has no organization")
    org = db.get(Organization, role.organization_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    return org, role


def _org_dict(org: Organization, role: OrgRole, member_count: int, has_drive: bool) -> dict[str, Any]:
    return {
        "id": org.id,
        "name": org.name,
        "slug": org.slug,
        "logo_url": org.logo_url,
        "primary_color": org.primary_color,
        "tagline": org.tagline,
        "domain": org.domain,
        "drive_folder_id": org.drive_folder_id,
        "has_drive_connected": has_drive,
        "user_role": role.role,
        "member_count": member_count,
        "created_at": org.created_at.isoformat() if org.created_at else None,
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("")
def get_org(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Return current org info for the authenticated user."""
    org, role = _get_org(user, db)

    # Backfill slug for orgs created before slug was required
    if not org.slug and org.name:
        org.slug = slugify(org.name)
        db.commit()

    member_count = db.query(OrgRole).filter(OrgRole.organization_id == org.id).count()
    has_drive = db.query(GoogleToken).filter(
        GoogleToken.user_id == user.id,
        GoogleToken.organization_id == org.id,
    ).first() is not None
    return _org_dict(org, role, member_count, has_drive)


@router.patch("")
def update_org(
    body: OrgUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    org, role = _get_org(user, db)
    if role.role not in ("owner", "admin"):
        raise HTTPException(status_code=403, detail="Admin role required to update org")

    if body.name is not None:
        org.name = body.name.strip()
        if not org.slug:
            org.slug = slugify(org.name)
    if body.logo_url is not None:
        org.logo_url = body.logo_url
    if body.primary_color is not None:
        org.primary_color = body.primary_color
    if body.tagline is not None:
        org.tagline = body.tagline

    db.commit()
    db.refresh(org)
    member_count = db.query(OrgRole).filter(OrgRole.organization_id == org.id).count()
    has_drive = db.query(GoogleToken).filter(
        GoogleToken.user_id == user.id,
        GoogleToken.organization_id == org.id,
    ).first() is not None
    return _org_dict(org, role, member_count, has_drive)


@router.get("/members")
def list_members(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    org, _ = _get_org(user, db)
    roles = (
        db.query(OrgRole)
        .filter(OrgRole.organization_id == org.id)
        .join(User, User.id == OrgRole.user_id)
        .all()
    )
    members = []
    for r in roles:
        u = db.get(User, r.user_id)
        members.append({
            "id": r.id,
            "user_id": r.user_id,
            "email": u.email if u else None,
            "name": u.name if u else None,
            "role": r.role,
            "joined_at": r.created_at.isoformat() if r.created_at else None,
        })
    return {"members": members}


@router.patch("/members/{member_id}/role")
def update_member_role(
    member_id: int,
    body: dict,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    org, caller_role = _get_org(user, db)
    if caller_role.role not in ("owner", "admin"):
        raise HTTPException(status_code=403, detail="Admin role required")

    target = db.query(OrgRole).filter(
        OrgRole.id == member_id,
        OrgRole.organization_id == org.id,
    ).first()
    if not target:
        raise HTTPException(status_code=404, detail="Member not found")

    new_role = body.get("role")
    if new_role not in ("owner", "admin", "editor", "viewer"):
        raise HTTPException(status_code=400, detail="Invalid role")

    target.role = new_role
    db.commit()
    return {"ok": True, "member_id": member_id, "role": new_role}


@router.delete("/members/{member_id}", status_code=204)
def remove_member(
    member_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    org, caller_role = _get_org(user, db)
    if caller_role.role not in ("owner", "admin"):
        raise HTTPException(status_code=403, detail="Admin role required")

    target = db.query(OrgRole).filter(
        OrgRole.id == member_id,
        OrgRole.organization_id == org.id,
    ).first()
    if not target:
        raise HTTPException(status_code=404, detail="Member not found")

    db.delete(target)
    db.commit()
