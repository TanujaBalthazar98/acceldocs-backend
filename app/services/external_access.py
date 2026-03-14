"""External-access grant management for invite-only external docs."""

from __future__ import annotations

import re

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import ExternalAccessGrant, OrgRole, Organization, User

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _normalize_email(value: str | None) -> str:
    return (value or "").strip().lower()


def _is_valid_email(value: str) -> bool:
    return bool(_EMAIL_RE.match(value))


def _get_org_and_role(db: Session, user: User | None) -> tuple[Organization | None, OrgRole | None]:
    if not user:
        return None, None
    role = db.query(OrgRole).filter(OrgRole.user_id == user.id).first()
    if not role:
        return None, None
    org = db.get(Organization, role.organization_id)
    return org, role


def _grant_payload(grant: ExternalAccessGrant, db: Session) -> dict:
    creator = db.get(User, grant.created_by_user_id) if grant.created_by_user_id else None
    return {
        "id": grant.id,
        "email": grant.email,
        "is_active": bool(grant.is_active),
        "created_by_user_id": grant.created_by_user_id,
        "created_by_name": creator.name if creator else None,
        "created_by_email": creator.email if creator else None,
        "created_at": grant.created_at.isoformat() if grant.created_at else None,
        "updated_at": grant.updated_at.isoformat() if grant.updated_at else None,
    }


async def list_external_access(body: dict, db: Session, user: User | None) -> dict:
    """List external-access grants for the caller's organization."""
    org, role = _get_org_and_role(db, user)
    if not org or not role:
        return {"ok": False, "error": "User has no organization"}

    include_inactive = bool(body.get("include_inactive", False))
    query = db.query(ExternalAccessGrant).filter(ExternalAccessGrant.organization_id == org.id)
    if not include_inactive:
        query = query.filter(ExternalAccessGrant.is_active == True)  # noqa: E712

    grants = query.order_by(ExternalAccessGrant.email.asc()).all()
    return {"ok": True, "grants": [_grant_payload(g, db) for g in grants]}


async def grant_external_access(body: dict, db: Session, user: User | None) -> dict:
    """Grant external docs access to an email for the caller's organization."""
    org, role = _get_org_and_role(db, user)
    if not org or not role:
        return {"ok": False, "error": "User has no organization"}
    if role.role not in ("owner", "admin"):
        return {"ok": False, "error": "Insufficient permissions"}

    email = _normalize_email(body.get("email"))
    if not email:
        return {"ok": False, "error": "Email required"}
    if not _is_valid_email(email):
        return {"ok": False, "error": "Invalid email"}

    org_domain = _normalize_email(org.domain)
    if org_domain and email.endswith(f"@{org_domain}"):
        return {"ok": False, "error": "Use organization membership for internal users"}

    existing_user = db.query(User).filter(func.lower(User.email) == email).first()
    if existing_user:
        existing_org_role = (
            db.query(OrgRole)
            .filter(
                OrgRole.organization_id == org.id,
                OrgRole.user_id == existing_user.id,
            )
            .first()
        )
        if existing_org_role:
            return {"ok": False, "error": "User is already an organization member"}

    grant = (
        db.query(ExternalAccessGrant)
        .filter(
            ExternalAccessGrant.organization_id == org.id,
            ExternalAccessGrant.email == email,
        )
        .first()
    )

    if not grant:
        grant = ExternalAccessGrant(
            organization_id=org.id,
            email=email,
            is_active=True,
            created_by_user_id=user.id if user else None,
        )
        db.add(grant)
        status = "created"
    elif grant.is_active:
        status = "already_active"
    else:
        grant.is_active = True
        status = "reactivated"

    db.commit()
    db.refresh(grant)
    return {"ok": True, "status": status, "grant": _grant_payload(grant, db)}


async def revoke_external_access(body: dict, db: Session, user: User | None) -> dict:
    """Revoke external docs access for a grant id or email."""
    org, role = _get_org_and_role(db, user)
    if not org or not role:
        return {"ok": False, "error": "User has no organization"}
    if role.role not in ("owner", "admin"):
        return {"ok": False, "error": "Insufficient permissions"}

    grant_id = body.get("grant_id") or body.get("grantId") or body.get("id")
    email = _normalize_email(body.get("email"))

    query = db.query(ExternalAccessGrant).filter(ExternalAccessGrant.organization_id == org.id)
    if grant_id is not None:
        try:
            grant_id_int = int(grant_id)
        except (TypeError, ValueError):
            return {"ok": False, "error": "Invalid grant id"}
        grant = query.filter(ExternalAccessGrant.id == grant_id_int).first()
    elif email:
        grant = query.filter(ExternalAccessGrant.email == email).first()
    else:
        return {"ok": False, "error": "grantId or email required"}

    if not grant:
        return {"ok": False, "error": "External access grant not found"}

    if not grant.is_active:
        return {"ok": True, "status": "already_revoked", "grant": _grant_payload(grant, db)}

    grant.is_active = False
    db.commit()
    db.refresh(grant)
    return {"ok": True, "status": "revoked", "grant": _grant_payload(grant, db)}
