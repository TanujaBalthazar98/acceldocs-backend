"""Org API — current organization info, settings, and member management."""

import logging
import re
import secrets
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Header, Request
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth.routes import get_current_user
from app.config import settings
from app.database import get_db
from app.middleware.security import limiter
from app.lib.slugify import to_slug as slugify
from app.models import AuditLog, GoogleToken, Invitation, OrgRole, Organization, Page, User
from app.services.drive_acl import (
    revoke_member_drive_permission,
    sync_member_drive_file_permission,
    sync_member_drive_permission,
)
from app.services.email import send_invitation_email

logger = logging.getLogger(__name__)
router = APIRouter()
PLACEHOLDER_INVITE_DOMAIN = "pending.acceldocs"


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class OrgUpdate(BaseModel):
    name: str | None = None
    logo_url: str | None = None
    primary_color: str | None = None
    secondary_color: str | None = None
    accent_color: str | None = None
    font_heading: str | None = None
    font_body: str | None = None
    custom_css: str | None = None
    tagline: str | None = None
    hierarchy_mode: str | None = None
    custom_docs_domain: str | None = None
    hero_title: str | None = None
    hero_description: str | None = None
    show_search_on_landing: bool | None = None
    show_featured_projects: bool | None = None
    analytics_property_id: str | None = None
    copyright: str | None = None
    custom_links: str | None = None
    sidebar_position: str | None = None
    show_toc: bool | None = None
    code_theme: str | None = None
    max_content_width: str | None = None
    header_html: str | None = None
    footer_html: str | None = None
    landing_blocks: str | None = None


class InviteCreate(BaseModel):
    email: str
    role: str = "editor"  # owner | admin | editor | reviewer | viewer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_org(user: User, db: Session, org_id: int | None = None) -> tuple[Organization, OrgRole]:
    """Resolve the user's current organization.

    If *org_id* is given, look up that specific org (the user must be a member).
    Otherwise fall back to the user's first org.
    """
    query = db.query(OrgRole).filter(OrgRole.user_id == user.id)
    if org_id:
        query = query.filter(OrgRole.organization_id == org_id)
    role = query.first()
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
        "tagline": org.tagline,
        "domain": org.domain,
        "custom_docs_domain": org.custom_docs_domain,
        # Branding
        "primary_color": org.primary_color,
        "secondary_color": org.secondary_color,
        "accent_color": org.accent_color,
        "font_heading": org.font_heading,
        "font_body": org.font_body,
        "custom_css": org.custom_css,
        # Landing
        "hero_title": org.hero_title,
        "hero_description": org.hero_description,
        "show_search_on_landing": org.show_search_on_landing if org.show_search_on_landing is not None else True,
        "show_featured_projects": org.show_featured_projects if org.show_featured_projects is not None else True,
        # Docs display
        "hierarchy_mode": org.hierarchy_mode or "product",
        "sidebar_position": org.sidebar_position or "left",
        "show_toc": True if org.show_toc is None else bool(org.show_toc),
        "code_theme": org.code_theme or "github-dark",
        "max_content_width": org.max_content_width or "4xl",
        "header_html": org.header_html,
        "footer_html": org.footer_html,
        "landing_blocks": org.landing_blocks,
        # Analytics & metadata
        "analytics_property_id": getattr(org, "analytics_property_id", None),
        "copyright": getattr(org, "copyright", None),
        "custom_links": org.custom_links,
        # MCP/OpenAPI
        "mcp_enabled": org.mcp_enabled if org.mcp_enabled is not None else False,
        "openapi_spec_json": org.openapi_spec_json,
        "openapi_spec_url": org.openapi_spec_url,
        # AI agent (BYOK)
        "ai_provider": getattr(org, "ai_provider", None),
        "ai_has_key": bool(getattr(org, "ai_api_key_encrypted", None)),
        "ai_model": getattr(org, "ai_model", None),
        "ai_base_url": getattr(org, "ai_base_url", None),
        # Infrastructure
        "drive_folder_id": org.drive_folder_id,
        "has_drive_connected": has_drive,
        "user_role": role.role,
        "member_count": member_count,
        "created_at": org.created_at.isoformat() if org.created_at else None,
    }


def _normalize_dt_for_compare(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    # SQLite often returns naive datetimes even when timezone=True.
    # Normalize to UTC-aware so comparisons never raise TypeError.
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _is_expired(dt: datetime | None, now: datetime | None = None) -> bool:
    normalized = _normalize_dt_for_compare(dt)
    if normalized is None:
        return False
    now_utc = now.astimezone(timezone.utc) if now and now.tzinfo else (now.replace(tzinfo=timezone.utc) if now else datetime.now(timezone.utc))
    return normalized < now_utc


def _email_domain(email: str) -> str | None:
    value = (email or "").strip().lower()
    if "@" not in value:
        return None
    return value.split("@", 1)[1].strip() or None


def _run_async(coro):
    """Execute a coroutine from sync route handlers."""
    return asyncio.run(coro)


def _sync_member_page_acl_backfill(
    *,
    db: Session,
    org: Organization,
    member_email: str | None,
    org_role: str,
    preferred_user_ids: list[int | None],
    max_docs: int = 200,
) -> dict:
    """Best-effort ACL sync for existing Google Docs in an org."""
    if not org or not org.id:
        return {"ok": False, "reason": "organization_missing"}
    if not member_email:
        return {"ok": False, "reason": "member_email_missing"}

    doc_rows = (
        db.query(Page.google_doc_id)
        .filter(Page.organization_id == org.id)
        .distinct()
        .limit(max_docs + 1)
        .all()
    )
    doc_ids = [doc_id for (doc_id,) in doc_rows if doc_id]
    truncated = len(doc_ids) > max_docs
    doc_ids = doc_ids[:max_docs]
    if not doc_ids:
        return {"ok": True, "status": "skipped", "reason": "no_pages", "total": 0}

    synced = 0
    failed = 0
    failures: list[dict[str, Any]] = []
    for doc_id in doc_ids:
        result = _run_async(
            sync_member_drive_file_permission(
                db=db,
                org=org,
                member_email=member_email,
                org_role=org_role,
                drive_file_id=doc_id,
                preferred_user_ids=preferred_user_ids,
            )
        )
        if result.get("ok"):
            synced += 1
        else:
            failed += 1
            if len(failures) < 10:
                failures.append({"doc_id": doc_id, **result})

    return {
        "ok": failed == 0,
        "status": "completed",
        "total": len(doc_ids),
        "synced": synced,
        "failed": failed,
        "truncated": truncated,
        "failures": failures,
    }




# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/list")
def list_user_orgs(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Return all organizations the authenticated user belongs to."""
    roles = db.query(OrgRole).filter(OrgRole.user_id == user.id).all()
    orgs = []
    for r in roles:
        org = db.get(Organization, r.organization_id)
        if org:
            orgs.append({
                "id": org.id,
                "name": org.name,
                "slug": org.slug,
                "logo_url": org.logo_url,
                "domain": org.domain,
                "user_role": r.role,
            })
    return {"ok": True, "organizations": orgs}


@router.get("")
def get_org(
    org_id: int | None = None,
    x_org_id: int | None = Header(default=None, alias="X-Org-Id"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Return org info. Pass ?org_id=N to select a specific org."""
    selected_org_id = org_id if org_id is not None else x_org_id
    org, role = _get_org(user, db, org_id=selected_org_id)

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
    x_org_id: int | None = Header(default=None, alias="X-Org-Id"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    org, role = _get_org(user, db, org_id=x_org_id)
    if role.role not in ("owner", "admin"):
        raise HTTPException(status_code=403, detail="Admin role required to update org")

    if body.name is not None:
        next_name = body.name.strip()
        if not next_name:
            raise HTTPException(status_code=400, detail="Workspace name cannot be empty")
        org.name = next_name
        if not org.slug:
            org.slug = slugify(org.name)
    if body.logo_url is not None:
        org.logo_url = body.logo_url
    if body.primary_color is not None:
        org.primary_color = body.primary_color
    if body.secondary_color is not None:
        org.secondary_color = body.secondary_color
    if body.accent_color is not None:
        org.accent_color = body.accent_color
    if body.font_heading is not None:
        org.font_heading = body.font_heading
    if body.font_body is not None:
        org.font_body = body.font_body
    if body.custom_css is not None:
        org.custom_css = body.custom_css
    if body.tagline is not None:
        org.tagline = body.tagline
    if body.hero_title is not None:
        org.hero_title = body.hero_title
    if body.hero_description is not None:
        org.hero_description = body.hero_description
    if body.show_search_on_landing is not None:
        org.show_search_on_landing = bool(body.show_search_on_landing)
    if body.show_featured_projects is not None:
        org.show_featured_projects = bool(body.show_featured_projects)
    if body.analytics_property_id is not None:
        org.analytics_property_id = body.analytics_property_id.strip() or None
    if body.copyright is not None:
        org.copyright = body.copyright
    if body.custom_links is not None:
        org.custom_links = body.custom_links
    if body.hierarchy_mode is not None:
        org.hierarchy_mode = "flat" if body.hierarchy_mode == "flat" else "product"
    if body.sidebar_position is not None:
        value = body.sidebar_position.strip().lower()
        if value not in ("left", "right"):
            raise HTTPException(status_code=400, detail="sidebar_position must be 'left' or 'right'")
        org.sidebar_position = value
    if body.show_toc is not None:
        org.show_toc = bool(body.show_toc)
    if body.code_theme is not None:
        org.code_theme = body.code_theme.strip() if body.code_theme else None
    if body.max_content_width is not None:
        value = body.max_content_width.strip().lower() if body.max_content_width else ""
        if value and value not in ("4xl", "5xl", "6xl", "full"):
            raise HTTPException(
                status_code=400,
                detail="max_content_width must be one of: 4xl, 5xl, 6xl, full",
            )
        org.max_content_width = value or None
    if body.header_html is not None:
        org.header_html = body.header_html
    if body.footer_html is not None:
        org.footer_html = body.footer_html
    if body.landing_blocks is not None:
        org.landing_blocks = body.landing_blocks
    if body.custom_docs_domain is not None:
        value = body.custom_docs_domain.strip().lower()
        if value == "":
            org.custom_docs_domain = None
        else:
            domain_regex = re.compile(r"^([a-z0-9]+(-[a-z0-9]+)*\.)+[a-z]{2,}$")
            if not domain_regex.match(value):
                raise HTTPException(
                    status_code=400,
                    detail="Invalid domain format. Example: docs.example.com",
                )

            existing = (
                db.query(Organization)
                .filter(func.lower(Organization.custom_docs_domain) == value, Organization.id != org.id)
                .first()
            )
            if existing:
                raise HTTPException(status_code=409, detail="Custom docs domain is already in use")
            org.custom_docs_domain = value

    db.commit()
    db.refresh(org)
    member_count = db.query(OrgRole).filter(OrgRole.organization_id == org.id).count()
    has_drive = db.query(GoogleToken).filter(
        GoogleToken.user_id == user.id,
        GoogleToken.organization_id == org.id,
    ).first() is not None
    return _org_dict(org, role, member_count, has_drive)


# ---------------------------------------------------------------------------
# AI Settings (BYOK — Bring Your Own Key)
# ---------------------------------------------------------------------------

class AISettingsUpdate(BaseModel):
    ai_provider: str | None = None  # gemini | anthropic | groq | openai_compat
    ai_api_key: str | None = None  # plaintext — will be encrypted before storage
    ai_model: str | None = None
    ai_base_url: str | None = None  # for openai_compat


@router.get("/ai-settings")
def get_ai_settings(
    x_org_id: int | None = Header(default=None, alias="X-Org-Id"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    org, role = _get_org(user, db, org_id=x_org_id)
    return {
        "ai_provider": getattr(org, "ai_provider", None),
        "ai_has_key": bool(getattr(org, "ai_api_key_encrypted", None)),
        "ai_model": getattr(org, "ai_model", None),
        "ai_base_url": getattr(org, "ai_base_url", None),
    }


@router.patch("/ai-settings")
def update_ai_settings(
    body: AISettingsUpdate,
    x_org_id: int | None = Header(default=None, alias="X-Org-Id"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    org, role = _get_org(user, db, org_id=x_org_id)
    if role.role not in ("owner", "admin"):
        raise HTTPException(status_code=403, detail="Owner or admin role required to manage AI settings")

    valid_providers = {"gemini", "anthropic", "groq", "openai_compat"}

    if body.ai_provider is not None:
        if body.ai_provider and body.ai_provider not in valid_providers:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid provider. Must be one of: {', '.join(sorted(valid_providers))}",
            )
        org.ai_provider = body.ai_provider or None

    if body.ai_api_key is not None:
        if body.ai_api_key.strip():
            from app.services.encryption import get_encryption_service
            enc = get_encryption_service()
            org.ai_api_key_encrypted = enc.encrypt(body.ai_api_key.strip())
        else:
            org.ai_api_key_encrypted = None

    if body.ai_model is not None:
        org.ai_model = body.ai_model.strip() or None

    if body.ai_base_url is not None:
        org.ai_base_url = body.ai_base_url.strip() or None

    db.commit()
    return {
        "ok": True,
        "ai_provider": org.ai_provider,
        "ai_has_key": bool(org.ai_api_key_encrypted),
        "ai_model": org.ai_model,
        "ai_base_url": org.ai_base_url,
    }


@router.delete("/ai-settings")
def delete_ai_settings(
    x_org_id: int | None = Header(default=None, alias="X-Org-Id"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    org, role = _get_org(user, db, org_id=x_org_id)
    if role.role not in ("owner", "admin"):
        raise HTTPException(status_code=403, detail="Owner or admin role required")
    org.ai_provider = None
    org.ai_api_key_encrypted = None
    org.ai_model = None
    org.ai_base_url = None
    db.commit()
    return {"ok": True}


@router.get("/members")
def list_members(
    x_org_id: int | None = Header(default=None, alias="X-Org-Id"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    org, _ = _get_org(user, db, org_id=x_org_id)
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
    x_org_id: int | None = Header(default=None, alias="X-Org-Id"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    org, caller_role = _get_org(user, db, org_id=x_org_id)
    if caller_role.role not in ("owner", "admin"):
        raise HTTPException(status_code=403, detail="Admin role required")

    target = db.query(OrgRole).filter(
        OrgRole.id == member_id,
        OrgRole.organization_id == org.id,
    ).first()
    if not target:
        raise HTTPException(status_code=404, detail="Member not found")

    new_role = body.get("role")
    if new_role not in ("owner", "admin", "editor", "reviewer", "viewer"):
        raise HTTPException(status_code=400, detail="Invalid role")

    # Only the owner can promote someone to owner
    if new_role == "owner" and caller_role.role != "owner":
        raise HTTPException(status_code=403, detail="Only the workspace owner can assign the owner role")

    # Prevent demoting an owner unless the caller is also an owner
    if target.role == "owner" and caller_role.role != "owner":
        raise HTTPException(status_code=403, detail="Only a workspace owner can change another owner's role")

    target_user = db.get(User, target.user_id)
    old_role = target.role
    target.role = new_role

    # When transferring ownership, demote the current owner to admin
    # and update org.owner_id to the new owner.
    previous_owner_role = None
    if new_role == "owner" and target.user_id != user.id:
        # Demote the caller (current owner) to admin
        caller_role.role = "admin"
        previous_owner_role = caller_role
        # Update the org-level owner reference
        org.owner_id = target.user_id

    # Audit trail
    import json as _json
    db.add(AuditLog(
        user_id=user.id,
        action="member_role_changed",
        entity_type="OrgRole",
        entity_id=target.id,
        audit_metadata=_json.dumps({
            "target_user_id": target.user_id,
            "target_email": target_user.email if target_user else None,
            "old_role": old_role,
            "new_role": new_role,
            "organization_id": org.id,
            "ownership_transferred": new_role == "owner" and target.user_id != user.id,
        }),
    ))
    db.commit()

    drive_sync = None
    docs_sync = None
    try:
        # Sync Drive permissions for the new owner
        drive_sync = _run_async(
            sync_member_drive_permission(
                db=db,
                org=org,
                member_email=target_user.email if target_user else None,
                org_role=new_role,
                preferred_user_ids=[org.owner_id, user.id, target.user_id],
            )
        )
        docs_sync = _sync_member_page_acl_backfill(
            db=db,
            org=org,
            member_email=target_user.email if target_user else None,
            org_role=new_role,
            preferred_user_ids=[org.owner_id, user.id, target.user_id],
        )
        # If ownership was transferred, also sync Drive for the demoted previous owner
        if previous_owner_role:
            _run_async(
                sync_member_drive_permission(
                    db=db,
                    org=org,
                    member_email=user.email,
                    org_role="admin",
                    preferred_user_ids=[org.owner_id, user.id],
                )
            )
    except Exception as exc:
        logger.warning("Failed to sync Drive permission for member role update %s: %s", target.user_id, exc)
    return {
        "ok": True,
        "member_id": member_id,
        "role": new_role,
        "previous_owner_demoted": previous_owner_role is not None,
        "drive_sync": drive_sync,
        "docs_sync": docs_sync,
    }


@router.delete("/members/{member_id}", status_code=204)
def remove_member(
    member_id: int,
    x_org_id: int | None = Header(default=None, alias="X-Org-Id"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    org, caller_role = _get_org(user, db, org_id=x_org_id)
    if caller_role.role not in ("owner", "admin"):
        raise HTTPException(status_code=403, detail="Admin role required")

    target = db.query(OrgRole).filter(
        OrgRole.id == member_id,
        OrgRole.organization_id == org.id,
    ).first()
    if not target:
        raise HTTPException(status_code=404, detail="Member not found")

    target_user = db.get(User, target.user_id)

    # Audit trail
    import json as _json
    db.add(AuditLog(
        user_id=user.id,
        action="member_removed",
        entity_type="OrgRole",
        entity_id=target.id,
        audit_metadata=_json.dumps({
            "target_user_id": target.user_id,
            "target_email": target_user.email if target_user else None,
            "role_at_removal": target.role,
            "organization_id": org.id,
        }),
    ))
    db.delete(target)
    db.commit()

    try:
        _run_async(
            revoke_member_drive_permission(
                db=db,
                org=org,
                member_email=target_user.email if target_user else None,
                preferred_user_ids=[org.owner_id, user.id],
            )
        )
    except Exception as exc:
        logger.warning("Failed to revoke Drive access for removed member %s: %s", target.user_id, exc)


# ---------------------------------------------------------------------------
# Invitations
# ---------------------------------------------------------------------------

@router.post("/invitations", status_code=201)
@limiter.limit("20/minute")
def create_invitation(
    request: Request,
    body: InviteCreate,
    x_org_id: int | None = Header(default=None, alias="X-Org-Id"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Create an org invitation and return a shareable token link."""
    org, caller_role = _get_org(user, db, org_id=x_org_id)
    if caller_role.role not in ("owner", "admin"):
        raise HTTPException(status_code=403, detail="Admin role required to invite members")

    email = body.email.strip().lower()
    # Validate email format
    if not re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', email):
        raise HTTPException(status_code=400, detail="Invalid email address format")
    role = body.role if body.role in ("owner", "admin", "editor", "reviewer", "viewer") else "viewer"
    # Only owner can invite as owner
    if role == "owner" and caller_role.role != "owner":
        raise HTTPException(status_code=403, detail="Only the workspace owner can invite someone as owner")
    email_domain = _email_domain(email)
    is_placeholder = email_domain == PLACEHOLDER_INVITE_DOMAIN

    # For orgs with a claimed domain, invitations must stay inside that domain.
    # Link-only invites (placeholder email) are disallowed in this mode.
    org_domain = (org.domain or "").strip().lower()
    if org_domain:
        if is_placeholder:
            raise HTTPException(
                status_code=400,
                detail=f"Workspace invites are restricted to @{org_domain} addresses.",
            )
        if not email_domain or email_domain != org_domain:
            raise HTTPException(
                status_code=400,
                detail=f"Only @{org_domain} email addresses can be invited to this workspace.",
            )

    # Check if user is already a member
    existing_user = db.query(User).filter(User.email == email).first()
    if existing_user:
        already_member = (
            db.query(OrgRole)
            .filter(OrgRole.organization_id == org.id, OrgRole.user_id == existing_user.id)
            .first()
        )
        if already_member:
            return {"ok": True, "status": "member", "message": f"{email} is already a member"}

    # Check for existing pending invitation
    existing_invite = (
        db.query(Invitation)
        .filter(
            Invitation.organization_id == org.id,
            Invitation.email == email,
            Invitation.accepted_at.is_(None),
            Invitation.expires_at > datetime.now(timezone.utc),
        )
        .first()
    )
    if existing_invite:
        return {
            "ok": True,
            "status": "pending",
            "token": existing_invite.token,
            "message": f"Pending invitation already exists for {email}",
        }

    invitation = Invitation(
        organization_id=org.id,
        invited_by_id=user.id,
        email=email,
        role=role,
        token=secrets.token_urlsafe(32),
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
    )
    db.add(invitation)
    db.commit()

    # Build invite link and send email (best-effort — won't fail the request)
    invite_link = f"{settings.frontend_url}/auth/callback?invite={invitation.token}"
    email_sent = False
    if not is_placeholder:
        email_sent = send_invitation_email(
            to_email=email,
            inviter_name=user.name or user.email,
            org_name=org.name or "Workspace",
            role=role,
            invite_link=invite_link,
        )

    return {
        "ok": True,
        "status": "created",
        "token": invitation.token,
        "email_sent": email_sent,
        "invitation": {
            "id": invitation.id,
            "email": email,
            "role": role,
            "expires_at": invitation.expires_at.isoformat(),
        },
    }


@router.get("/invitations")
def list_invitations(
    x_org_id: int | None = Header(default=None, alias="X-Org-Id"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """List all org invitations (pending and accepted)."""
    org, caller_role = _get_org(user, db, org_id=x_org_id)
    if caller_role.role not in ("owner", "admin"):
        raise HTTPException(status_code=403, detail="Admin role required")

    invitations = (
        db.query(Invitation)
        .filter(Invitation.organization_id == org.id)
        .order_by(Invitation.created_at.desc())
        .all()
    )
    now = datetime.now(timezone.utc)
    results = []
    for inv in invitations:
        status = "accepted" if inv.accepted_at else ("expired" if _is_expired(inv.expires_at, now) else "pending")
        inviter = db.get(User, inv.invited_by_id)
        results.append({
            "id": inv.id,
            "email": inv.email,
            "role": inv.role,
            "status": status,
            "token": inv.token if status == "pending" else None,
            "invited_by": inviter.email if inviter else None,
            "created_at": inv.created_at.isoformat() if inv.created_at else None,
            "expires_at": inv.expires_at.isoformat(),
            "accepted_at": inv.accepted_at.isoformat() if inv.accepted_at else None,
        })
    return {"invitations": results}


@router.delete("/invitations/{invitation_id}", status_code=204)
def revoke_invitation(
    invitation_id: int,
    x_org_id: int | None = Header(default=None, alias="X-Org-Id"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    """Revoke (delete) a pending invitation."""
    org, caller_role = _get_org(user, db, org_id=x_org_id)
    if caller_role.role not in ("owner", "admin"):
        raise HTTPException(status_code=403, detail="Admin role required")

    invitation = (
        db.query(Invitation)
        .filter(Invitation.id == invitation_id, Invitation.organization_id == org.id)
        .first()
    )
    if not invitation:
        raise HTTPException(status_code=404, detail="Invitation not found")
    db.delete(invitation)
    db.commit()


@router.post("/invitations/accept")
@limiter.limit("10/minute")
def accept_invitation(
    request: Request,
    body: dict,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Accept an invitation by token. Adds the authenticated user to the org."""
    token = (body.get("token") or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="Invitation token required")

    invitation = db.query(Invitation).filter(Invitation.token == token).first()
    if not invitation:
        raise HTTPException(status_code=404, detail="Invitation not found")

    invite_email = (invitation.email or "").strip().lower()
    user_email = (user.email or "").strip().lower()
    if invite_email and user_email and invite_email != user_email:
        raise HTTPException(status_code=403, detail="Invitation email does not match signed-in account")

    org_id = invitation.organization_id
    if not org_id:
        raise HTTPException(status_code=400, detail="Invalid invitation (no organization)")

    # Check if already a member first so accept is idempotent for domain auto-join paths.
    existing = (
        db.query(OrgRole)
        .filter(OrgRole.organization_id == org_id, OrgRole.user_id == user.id)
        .first()
    )
    if existing:
        if invitation.accepted_at is None:
            invitation.accepted_at = datetime.now(timezone.utc)
            db.commit()
        org = db.get(Organization, org_id)
        drive_sync = None
        docs_sync = None
        try:
            drive_sync = _run_async(
                sync_member_drive_permission(
                    db=db,
                    org=org,
                    member_email=user.email,
                    org_role=existing.role,
                    preferred_user_ids=[org.owner_id if org else None, invitation.invited_by_id, user.id],
                )
            )
            if org:
                docs_sync = _sync_member_page_acl_backfill(
                    db=db,
                    org=org,
                    member_email=user.email,
                    org_role=existing.role,
                    preferred_user_ids=[org.owner_id, invitation.invited_by_id, user.id],
                )
        except Exception as exc:
            logger.warning("Failed to sync Drive permission for existing member %s: %s", user.id, exc)
        return {
            "ok": True,
            "status": "already_member",
            "role": existing.role,
            "drive_sync": drive_sync,
            "docs_sync": docs_sync,
        }

    if invitation.accepted_at is not None:
        raise HTTPException(status_code=409, detail="Invitation already accepted")

    if _is_expired(invitation.expires_at):
        raise HTTPException(status_code=410, detail="Invitation has expired")

    # Add to org
    db.add(OrgRole(
        organization_id=org_id,
        user_id=user.id,
        role=invitation.role,
    ))
    invitation.accepted_at = datetime.now(timezone.utc)
    db.commit()

    org = db.get(Organization, org_id)
    drive_sync = None
    docs_sync = None
    try:
        drive_sync = _run_async(
            sync_member_drive_permission(
                db=db,
                org=org,
                member_email=user.email,
                org_role=invitation.role,
                preferred_user_ids=[org.owner_id if org else None, invitation.invited_by_id, user.id],
            )
        )
        if org:
            docs_sync = _sync_member_page_acl_backfill(
                db=db,
                org=org,
                member_email=user.email,
                org_role=invitation.role,
                preferred_user_ids=[org.owner_id, invitation.invited_by_id, user.id],
            )
    except Exception as exc:
        logger.warning("Failed to sync Drive permission for accepted invitation %s: %s", invitation.id, exc)

    return {
        "ok": True,
        "status": "accepted",
        "role": invitation.role,
        "organization": {"id": org_id, "name": org.name if org else None, "slug": org.slug if org else None},
        "drive_sync": drive_sync,
        "docs_sync": docs_sync,
    }
