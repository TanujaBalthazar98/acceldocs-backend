"""Sections API — CRUD for the section tree (replaces Project + Topic hierarchy)."""

import logging
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.drive import _create_drive_folder, _trash_drive_item, _move_drive_item, get_drive_credentials
from app.auth.routes import get_current_user
from app.database import get_db
from app.lib.slugify import to_slug as slugify, unique_slug as _make_unique
from app.models import Organization, OrgRole, Page, Section, User

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class SectionCreate(BaseModel):
    name: str
    parent_id: int | None = None
    section_type: Literal["section", "tab", "version"] = "section"
    visibility: Literal["public", "internal", "external"] = "public"
    drive_folder_id: str | None = None
    display_order: int = 0
    clone_from_section_id: int | None = None


class SectionUpdate(BaseModel):
    name: str | None = None
    parent_id: int | None = None
    section_type: Literal["section", "tab", "version"] | None = None
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

def _resolve_org_role(user: User, db: Session, requested_org_id: int | None = None) -> OrgRole:
    query = db.query(OrgRole).filter(OrgRole.user_id == user.id)
    if requested_org_id is not None:
        query = query.filter(OrgRole.organization_id == requested_org_id)
    role = query.first()
    if not role:
        raise HTTPException(status_code=403, detail="User has no organization")
    return role


def _get_org_id(user: User, db: Session, requested_org_id: int | None = None) -> int:
    return _resolve_org_role(user, db, requested_org_id).organization_id


def _require_editor(user: User, db: Session, requested_org_id: int | None = None) -> int:
    """Return org_id; raise 403 if user is not at least editor."""
    role = _resolve_org_role(user, db, requested_org_id)
    if not role or role.role not in ("owner", "admin", "editor"):
        raise HTTPException(status_code=403, detail="Editor role required")
    return role.organization_id


def _require_admin(user: User, db: Session, requested_org_id: int | None = None) -> int:
    """Return org_id; raise 403 if user is not at least admin."""
    role = _resolve_org_role(user, db, requested_org_id)
    if not role or role.role not in ("owner", "admin"):
        raise HTTPException(status_code=403, detail="Admin role required")
    return role.organization_id


def _unique_slug(name: str, org_id: int, parent_id: int | None, db: Session, exclude_id: int | None = None) -> str:
    base = slugify(name)

    def exists(s: str) -> bool:
        q = db.query(Section).filter(
            Section.organization_id == org_id,
            Section.parent_id == parent_id,
            Section.slug == s,
        )
        if exclude_id:
            q = q.filter(Section.id != exclude_id)
        return q.first() is not None

    return _make_unique(base, exists)


def _unique_page_slug(seed: str, org_id: int, db: Session) -> str:
    base = slugify(seed) or "page"
    return _make_unique(
        base,
        lambda s: db.query(Page).filter(Page.organization_id == org_id, Page.slug == s).first() is not None,
    )


def _ordered_sections_for_parent(
    db: Session,
    *,
    org_id: int,
    parent_id: int | None,
    exclude_section_id: int | None = None,
) -> list[Section]:
    query = db.query(Section).filter(Section.organization_id == org_id)
    if parent_id is None:
        query = query.filter(Section.parent_id.is_(None))
    else:
        query = query.filter(Section.parent_id == parent_id)
    if exclude_section_id is not None:
        query = query.filter(Section.id != exclude_section_id)
    return query.order_by(Section.display_order, Section.id).all()


def _clamp_insert_index(index: int, size: int) -> int:
    return max(0, min(index, size))


def _copy_drive_doc(service, source_file_id: str, copy_title: str, parent_id: str | None) -> tuple[str, str | None]:
    body: dict[str, Any] = {"name": copy_title}
    if parent_id:
        body["parents"] = [parent_id]
    result = (
        service.files()
        .copy(
            fileId=source_file_id,
            body=body,
            fields="id,modifiedTime,name",
            supportsAllDrives=True,
        )
        .execute()
    )
    file_id = result.get("id")
    if not file_id:
        raise HTTPException(status_code=502, detail="Drive copy failed: missing file ID")
    return file_id, result.get("modifiedTime")


def _resolve_clone_source_id(
    *,
    org_id: int,
    product_id: int,
    new_version_id: int,
    explicit_source_id: int | None,
    db: Session,
) -> int | None:
    latest_sibling_version = (
        db.query(Section)
        .filter(
            Section.organization_id == org_id,
            Section.parent_id == product_id,
            Section.id != new_version_id,
            Section.section_type == "version",
        )
        .order_by(Section.display_order.desc(), Section.id.desc())
        .first()
    )

    if explicit_source_id:
        source = db.query(Section).filter(
            Section.id == explicit_source_id,
            Section.organization_id == org_id,
        ).first()
        if not source:
            raise HTTPException(status_code=400, detail="clone_from_section_id is invalid")
        # Guardrail: never clone a new version from product root when a newer version exists.
        if source.id == product_id and latest_sibling_version:
            return latest_sibling_version.id
        return source.id

    # Preferred path: clone from latest sibling version when available.
    if latest_sibling_version:
        return latest_sibling_version.id

    # First-version creation fallback: duplicate current product tree.
    has_non_version_children = db.query(Section).filter(
        Section.organization_id == org_id,
        Section.parent_id == product_id,
        Section.id != new_version_id,
        Section.section_type != "version",
    ).first()
    has_product_pages = db.query(Page).filter(
        Page.organization_id == org_id,
        Page.section_id == product_id,
    ).first()
    if has_non_version_children or has_product_pages:
        return product_id
    return None


def _validate_version_parent(
    *,
    org_id: int,
    parent_id: int | None,
    db: Session,
) -> None:
    if parent_id is None:
        raise HTTPException(status_code=400, detail="Version must be created under a product")
    parent = db.query(Section).filter(
        Section.id == parent_id,
        Section.organization_id == org_id,
    ).first()
    if not parent:
        raise HTTPException(status_code=404, detail="Parent section not found")
    if parent.parent_id is not None:
        raise HTTPException(status_code=400, detail="Version parent must be a top-level product")


def _clone_section_tree_into_version(
    *,
    org_id: int,
    source_section_id: int,
    target_section_id: int,
    db: Session,
    drive_service: Any,
    owner_id: int | None,
) -> tuple[int, int]:
    """Deep-clone sections/pages from source subtree into a new version subtree."""
    sections_created = 0
    pages_created = 0

    def clone_node(src_id: int, dst_id: int) -> None:
        nonlocal sections_created, pages_created

        destination = db.get(Section, dst_id)
        if not destination:
            raise HTTPException(status_code=404, detail="Destination section not found during clone")

        source_pages = (
            db.query(Page)
            .filter(Page.organization_id == org_id, Page.section_id == src_id)
            .order_by(Page.display_order, Page.id)
            .all()
        )
        for src_page in source_pages:
            copied_doc_id, copied_modified_at = _copy_drive_doc(
                drive_service,
                src_page.google_doc_id,
                src_page.title,
                destination.drive_folder_id,
            )
            slug_seed = src_page.slug or src_page.title
            cloned_page = Page(
                organization_id=org_id,
                section_id=dst_id,
                google_doc_id=copied_doc_id,
                title=src_page.title,
                slug=_unique_page_slug(slug_seed, org_id, db),
                slug_locked=False,
                visibility_override=src_page.visibility_override,
                html_content=src_page.html_content,
                published_html=None,
                is_published=False,
                status="draft",
                display_order=src_page.display_order,
                drive_modified_at=copied_modified_at or src_page.drive_modified_at,
                last_synced_at=datetime.now(timezone.utc).isoformat(),
                owner_id=owner_id if owner_id is not None else src_page.owner_id,
            )
            db.add(cloned_page)
            pages_created += 1

        source_children = (
            db.query(Section)
            .filter(Section.organization_id == org_id, Section.parent_id == src_id)
            .order_by(Section.display_order, Section.id)
            .all()
        )
        for src_child in source_children:
            src_type = src_child.section_type or "section"
            if src_type == "version":
                continue

            clone_type = src_type if src_type in ("section", "tab") else "section"
            cloned_child = Section(
                organization_id=org_id,
                parent_id=dst_id,
                name=src_child.name,
                slug=_unique_slug(src_child.name, org_id, dst_id, db),
                section_type=clone_type,
                visibility=src_child.visibility or "public",
                drive_folder_id=None,
                display_order=src_child.display_order,
                is_published=False,
            )
            db.add(cloned_child)
            db.flush()

            cloned_child.drive_folder_id = _create_drive_folder(
                drive_service,
                cloned_child.name,
                destination.drive_folder_id,
            )
            sections_created += 1
            clone_node(src_child.id, cloned_child.id)

    source_root = db.query(Section).filter(
        Section.id == source_section_id,
        Section.organization_id == org_id,
    ).first()
    if not source_root:
        raise HTTPException(status_code=400, detail="Source section not found for version clone")

    clone_node(source_root.id, target_section_id)
    return sections_created, pages_created


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("")
def list_sections(
    x_org_id: int | None = Header(default=None, alias="X-Org-Id"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Return all sections for the current org as a flat list."""
    org_id = _get_org_id(user, db, x_org_id)
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
    x_org_id: int | None = Header(default=None, alias="X-Org-Id"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    org_id = _require_editor(user, db, x_org_id)
    org = db.get(Organization, org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    # Product hierarchy requires a configured Drive root before creating
    # a top-level product (section with no parent).
    hierarchy_mode = (org.hierarchy_mode or "product").strip().lower()
    if (
        body.parent_id is None
        and body.section_type == "section"
        and hierarchy_mode != "flat"
        and not (org.drive_folder_id or "").strip()
    ):
        raise HTTPException(
            status_code=400,
            detail="Workspace Drive root folder must be configured before creating a product",
        )

    if body.section_type == "version":
        _validate_version_parent(org_id=org_id, parent_id=body.parent_id, db=db)

    if body.parent_id is not None:
        parent = db.query(Section).filter(
            Section.id == body.parent_id,
            Section.organization_id == org_id,
        ).first()
        if not parent:
            raise HTTPException(status_code=404, detail="Parent section not found")

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
    db.flush()

    drive_service = None
    strict_clone_mode = body.section_type == "version"
    if not section.drive_folder_id:
        try:
            creds = await get_drive_credentials(user, org_id, db)
            from googleapiclient.discovery import build as _build
            drive_service = _build("drive", "v3", credentials=creds, cache_discovery=False)

            # Determine parent folder: parent section's folder → org root folder
            parent_drive_id: str | None = None
            if body.parent_id:
                parent_sec = db.get(Section, body.parent_id)
                parent_drive_id = parent_sec.drive_folder_id if parent_sec else None
            if not parent_drive_id:
                parent_drive_id = org.drive_folder_id
            if strict_clone_mode and not parent_drive_id:
                raise HTTPException(status_code=400, detail="Drive root folder is required to create a version")

            folder_id = _create_drive_folder(drive_service, section.name, parent_drive_id)
            section.drive_folder_id = folder_id
            logger.info("Created Drive folder %s for section %d", folder_id, section.id)
        except HTTPException as exc:
            if strict_clone_mode:
                raise
            logger.warning("Could not create Drive folder for section %d: %s", section.id, exc.detail)
        except Exception as exc:
            if strict_clone_mode:
                db.rollback()
                logger.error("Drive setup required for version cloning: %s", exc)
                raise HTTPException(status_code=400, detail="Drive setup required for version cloning. Please check your Drive configuration.") from exc
            logger.warning("Could not create Drive folder for section %d: %s", section.id, exc)

    cloned_sections = 0
    cloned_pages = 0
    if body.section_type == "version":
        clone_source_id = _resolve_clone_source_id(
            org_id=org_id,
            product_id=body.parent_id,  # validated above via _validate_version_parent
            new_version_id=section.id,
            explicit_source_id=body.clone_from_section_id,
            db=db,
        )
        if clone_source_id is not None:
            if drive_service is None:
                try:
                    creds = await get_drive_credentials(user, org_id, db)
                    from googleapiclient.discovery import build as _build
                    drive_service = _build("drive", "v3", credentials=creds, cache_discovery=False)
                except Exception as exc:
                    db.rollback()
                    raise HTTPException(
                        status_code=400,
                        detail=f"Drive connection is required to clone version content: {exc}",
                    ) from exc
            if not section.drive_folder_id:
                db.rollback()
                raise HTTPException(status_code=400, detail="Version folder missing in Drive; cannot clone")

            cloned_sections, cloned_pages = _clone_section_tree_into_version(
                org_id=org_id,
                source_section_id=clone_source_id,
                target_section_id=section.id,
                db=db,
                drive_service=drive_service,
                owner_id=user.id,
            )
            logger.info(
                "Cloned version %d from source %d (%d sections, %d pages)",
                section.id,
                clone_source_id,
                cloned_sections,
                cloned_pages,
            )

    db.commit()
    db.refresh(section)

    # Create a matching Drive folder if Drive is connected (legacy non-version flow fallback)
    if not section.drive_folder_id and not strict_clone_mode:
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
                parent_drive_id = org.drive_folder_id

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
    x_org_id: int | None = Header(default=None, alias="X-Org-Id"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    org_id = _require_editor(user, db, x_org_id)
    section = db.query(Section).filter(
        Section.id == section_id,
        Section.organization_id == org_id,
    ).first()
    if not section:
        raise HTTPException(status_code=404, detail="Section not found")

    old_parent_id = section.parent_id
    parent_id_explicit = "parent_id" in body.model_fields_set
    target_parent_id = body.parent_id if parent_id_explicit else old_parent_id
    parent_id_changed = target_parent_id != old_parent_id
    display_order_requested = body.display_order is not None
    new_parent_id_value = target_parent_id if parent_id_changed else None

    if target_parent_id == section.id:
        raise HTTPException(status_code=400, detail="Section cannot be its own parent")

    target_parent: Section | None = None
    if target_parent_id is not None:
        target_parent = db.query(Section).filter(
            Section.id == target_parent_id,
            Section.organization_id == org_id,
        ).first()
        if not target_parent:
            raise HTTPException(status_code=404, detail="Parent section not found")

        # Prevent moving a node under its own descendant.
        cursor = target_parent
        while cursor is not None:
            if cursor.id == section.id:
                raise HTTPException(status_code=400, detail="Cannot move section under its own descendant")
            if cursor.parent_id is None:
                break
            cursor = db.query(Section).filter(
                Section.id == cursor.parent_id,
                Section.organization_id == org_id,
            ).first()

    next_section_type = body.section_type if body.section_type is not None else section.section_type
    next_parent_id = target_parent_id
    if next_section_type == "version":
        _validate_version_parent(org_id=org_id, parent_id=next_parent_id, db=db)

    if body.name is not None:
        cleaned_name = body.name.strip()
        if not cleaned_name:
            raise HTTPException(status_code=400, detail="Name cannot be empty")
        section.name = cleaned_name

    if parent_id_explicit:
        section.parent_id = target_parent_id

    if body.name is not None or parent_id_changed:
        section.slug = _unique_slug(section.name, org_id, section.parent_id, db, exclude_id=section_id)

    if body.section_type is not None:
        section.section_type = body.section_type
    if body.visibility is not None:
        section.visibility = body.visibility
    if "drive_folder_id" in body.model_fields_set:
        section.drive_folder_id = body.drive_folder_id
    if body.is_published is not None:
        section.is_published = body.is_published

    if parent_id_changed or display_order_requested:
        source_siblings = _ordered_sections_for_parent(
            db,
            org_id=org_id,
            parent_id=old_parent_id,
            exclude_section_id=section.id,
        )
        if parent_id_changed:
            target_siblings = _ordered_sections_for_parent(
                db,
                org_id=org_id,
                parent_id=section.parent_id,
                exclude_section_id=section.id,
            )
        else:
            target_siblings = source_siblings

        requested_index = body.display_order if display_order_requested else None
        if requested_index is None:
            requested_index = len(target_siblings) if parent_id_changed else section.display_order
        insert_index = _clamp_insert_index(requested_index, len(target_siblings))

        if parent_id_changed:
            for idx, sibling in enumerate(source_siblings):
                sibling.display_order = idx

        reordered_target = list(target_siblings)
        reordered_target.insert(insert_index, section)
        for idx, sibling in enumerate(reordered_target):
            sibling.display_order = idx

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


@router.delete("/{section_id}")
async def delete_section(
    section_id: int,
    x_org_id: int | None = Header(default=None, alias="X-Org-Id"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    org_id = _require_editor(user, db, x_org_id)
    section = db.query(Section).filter(
        Section.id == section_id,
        Section.organization_id == org_id,
    ).first()
    if not section:
        raise HTTPException(status_code=404, detail="Section not found")

    # Collect ALL section IDs in the subtree (self + descendants)
    all_section_ids: list[int] = []
    drive_folder_ids: list[str] = []
    drive_doc_ids: list[str] = []

    def _collect_tree(sid: int) -> None:
        all_section_ids.append(sid)
        sec = db.get(Section, sid)
        if sec and sec.drive_folder_id:
            drive_folder_ids.append(sec.drive_folder_id)
        # Collect pages' Drive doc IDs
        pages = db.query(Page).filter(Page.section_id == sid).all()
        for p in pages:
            if p.google_doc_id:
                drive_doc_ids.append(p.google_doc_id)
        # Recurse into child sections
        children = db.query(Section).filter(
            Section.parent_id == sid,
            Section.organization_id == org_id,
        ).all()
        for child in children:
            _collect_tree(child.id)

    _collect_tree(section_id)

    # Delete all pages under the entire subtree
    pages_deleted = 0
    if all_section_ids:
        pages_deleted = (
            db.query(Page)
            .filter(Page.section_id.in_(all_section_ids))
            .delete(synchronize_session="fetch")
        )

    # Delete all sections in subtree (children first, then self via CASCADE)
    db.delete(section)
    db.commit()

    # Trash Drive items — folder first (which also trashes contents), then
    # individual docs that might live outside the folder structure
    drive_trashed = 0
    drive_errors: list[str] = []
    items_to_trash = drive_folder_ids + drive_doc_ids

    if items_to_trash:
        try:
            creds = await get_drive_credentials(user, org_id, db)
            from googleapiclient.discovery import build as _build
            svc = _build("drive", "v3", credentials=creds, cache_discovery=False)

            trashed_ids: set[str] = set()
            # Trash folders first — this also trashes their contents in Drive
            for folder_id in drive_folder_ids:
                if folder_id in trashed_ids:
                    continue
                try:
                    _trash_drive_item(svc, folder_id)
                    trashed_ids.add(folder_id)
                    drive_trashed += 1
                    logger.info("Trashed Drive folder %s", folder_id)
                except Exception as exc:
                    drive_errors.append(f"folder {folder_id}: {exc}")
                    logger.warning("Could not trash Drive folder %s: %s", folder_id, exc)

            # Trash individual docs that may not be inside the trashed folders
            for doc_id in drive_doc_ids:
                if doc_id in trashed_ids:
                    continue
                try:
                    _trash_drive_item(svc, doc_id)
                    trashed_ids.add(doc_id)
                    drive_trashed += 1
                except Exception as exc:
                    # Don't log individual doc errors as warnings — the folder trash
                    # likely already handled these
                    pass
        except Exception as exc:
            drive_errors.append(f"credentials: {exc}")
            logger.warning("Could not get Drive credentials for cleanup: %s", exc)

    return {
        "ok": True,
        "pages_deleted": pages_deleted,
        "sections_deleted": len(all_section_ids),
        "drive_trashed": drive_trashed,
        "drive_errors": drive_errors if drive_errors else None,
    }
