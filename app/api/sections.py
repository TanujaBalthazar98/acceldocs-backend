"""Sections API — CRUD for the section tree (replaces Project + Topic hierarchy)."""

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.drive import _create_drive_folder, _trash_drive_item, _move_drive_item, get_drive_credentials
from app.auth.routes import get_current_user
from app.database import get_db
from app.lib.slugify import to_slug as slugify
from app.models import Organization, OrgRole, Section, User

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class SectionCreate(BaseModel):
    name: str
    parent_id: int | None = None
    section_type: Literal["section", "tab"] = "section"
    visibility: Literal["public", "internal", "external"] = "public"
    drive_folder_id: str | None = None
    display_order: int = 0


class SectionUpdate(BaseModel):
    name: str | None = None
    parent_id: int | None = None
    section_type: Literal["section", "tab"] | None = None
    visibility: Literal["public", "internal", "external"] | None = None
    drive_folder_id: str | None = None
    display_order: int | None = None
    is_published: bool | None = None


def _section_dict(s: Section) -> dict[str, Any]:
    return {
        "id": s.id,
        "organization_id": s.organization_id,
        "parent_id": s.parent_id,
        "name": s.name,
        "slug": s.slug,
        "section_type": s.section_type or "section",
        "visibility": s.visibility or "public",
        "drive_folder_id": s.drive_folder_id,
        "display_order": s.display_order,
        "is_published": s.is_published,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_org_id(user: User, db: Session) -> int:
    role = db.query(OrgRole).filter(OrgRole.user_id == user.id).first()
    if not role:
        raise HTTPException(status_code=403, detail="User has no organization")
    return role.organization_id


def _require_editor(user: User, db: Session) -> int:
    """Return org_id; raise 403 if user is not at least editor."""
    role = db.query(OrgRole).filter(OrgRole.user_id == user.id).first()
    if not role or role.role not in ("owner", "admin", "editor"):
        raise HTTPException(status_code=403, detail="Editor role required")
    return role.organization_id


def _unique_slug(name: str, org_id: int, parent_id: int | None, db: Session, exclude_id: int | None = None) -> str:
    base = slugify(name)
    slug = base
    n = 1
    while True:
        q = db.query(Section).filter(
            Section.organization_id == org_id,
            Section.parent_id == parent_id,
            Section.slug == slug,
        )
        if exclude_id:
            q = q.filter(Section.id != exclude_id)
        if not q.first():
            return slug
        slug = f"{base}-{n}"
        n += 1


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("")
def list_sections(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Return all sections for the current org as a flat list."""
    org_id = _get_org_id(user, db)
    sections = (
        db.query(Section)
        .filter(Section.organization_id == org_id)
        .order_by(Section.parent_id.nulls_first(), Section.display_order, Section.name)
        .all()
    )
    return {"sections": [_section_dict(s) for s in sections]}


@router.post("", status_code=201)
async def create_section(
    body: SectionCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    org_id = _require_editor(user, db)
    slug = _unique_slug(body.name, org_id, body.parent_id, db)
    section = Section(
        organization_id=org_id,
        parent_id=body.parent_id,
        name=body.name.strip(),
        slug=slug,
        section_type=body.section_type,
        visibility=body.visibility,
        drive_folder_id=body.drive_folder_id,
        display_order=body.display_order,
    )
    db.add(section)
    db.commit()
    db.refresh(section)

    # Create a matching Drive folder if Drive is connected
    if not section.drive_folder_id:
        try:
            creds = await get_drive_credentials(user, org_id, db)
            from googleapiclient.discovery import build as _build
            service = _build("drive", "v3", credentials=creds, cache_discovery=False)

            # Determine parent folder: parent section's folder → org root folder
            parent_drive_id: str | None = None
            if body.parent_id:
                parent_sec = db.get(Section, body.parent_id)
                parent_drive_id = parent_sec.drive_folder_id if parent_sec else None
            if not parent_drive_id:
                org = db.get(Organization, org_id)
                parent_drive_id = org.drive_folder_id if org else None

            folder_id = _create_drive_folder(service, section.name, parent_drive_id)
            section.drive_folder_id = folder_id
            db.commit()
            logger.info("Created Drive folder %s for section %d", folder_id, section.id)
        except Exception as exc:
            logger.warning("Could not create Drive folder for section %d: %s", section.id, exc)

    logger.info("Created section %d '%s' for org %d", section.id, section.name, org_id)
    return _section_dict(section)


@router.patch("/{section_id}")
async def update_section(
    section_id: int,
    body: SectionUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    org_id = _require_editor(user, db)
    section = db.query(Section).filter(
        Section.id == section_id,
        Section.organization_id == org_id,
    ).first()
    if not section:
        raise HTTPException(status_code=404, detail="Section not found")

    parent_id_changed = (
        "parent_id" in body.model_fields_set and body.parent_id != section.parent_id
    )
    new_parent_id_value = body.parent_id if parent_id_changed else None

    if body.name is not None:
        section.name = body.name.strip()
        section.slug = _unique_slug(body.name, org_id, section.parent_id, db, exclude_id=section_id)
    if "parent_id" in body.model_fields_set:
        section.parent_id = body.parent_id
    if body.section_type is not None:
        section.section_type = body.section_type
    if body.visibility is not None:
        section.visibility = body.visibility
    if body.drive_folder_id is not None:
        section.drive_folder_id = body.drive_folder_id
    if body.display_order is not None:
        section.display_order = body.display_order
    if body.is_published is not None:
        section.is_published = body.is_published

    db.commit()
    db.refresh(section)

    # Mirror parent change in Drive
    if parent_id_changed and section.drive_folder_id:
        try:
            creds = await get_drive_credentials(user, org_id, db)
            from googleapiclient.discovery import build as _build
            svc = _build("drive", "v3", credentials=creds, cache_discovery=False)
            new_drive_parent: str | None = None
            if new_parent_id_value:
                parent_sec = db.get(Section, new_parent_id_value)
                new_drive_parent = parent_sec.drive_folder_id if parent_sec else None
            if not new_drive_parent:
                org = db.get(Organization, org_id)
                new_drive_parent = org.drive_folder_id if org else None
            if new_drive_parent:
                _move_drive_item(svc, section.drive_folder_id, new_drive_parent)
                logger.info("Moved Drive folder %s to parent %s", section.drive_folder_id, new_drive_parent)
        except Exception as exc:
            logger.warning("Could not move Drive folder for section %d: %s", section_id, exc)

    return _section_dict(section)


@router.delete("/{section_id}", status_code=204)
async def delete_section(
    section_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    org_id = _require_editor(user, db)
    section = db.query(Section).filter(
        Section.id == section_id,
        Section.organization_id == org_id,
    ).first()
    if not section:
        raise HTTPException(status_code=404, detail="Section not found")

    drive_folder_id = section.drive_folder_id

    # Orphan pages rather than cascade-delete them
    from app.models import Page as _Page
    db.query(_Page).filter(_Page.section_id == section_id).update({"section_id": None})
    db.delete(section)
    db.commit()

    # Trash Drive folder
    if drive_folder_id:
        try:
            creds = await get_drive_credentials(user, org_id, db)
            from googleapiclient.discovery import build as _build
            svc = _build("drive", "v3", credentials=creds, cache_discovery=False)
            _trash_drive_item(svc, drive_folder_id)
            logger.info("Trashed Drive folder %s for section %d", drive_folder_id, section_id)
        except Exception as exc:
            logger.warning("Could not trash Drive folder %s: %s", drive_folder_id, exc)
