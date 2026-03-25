"""Drive API — scan a Google Drive folder and import it as sections + pages.

POST /api/drive/scan   — recursively walk a folder, create Section + Page records
POST /api/drive/import/local — upload local files/folders into a target section
POST /api/drive/sync   — re-sync all pages for the current org from Drive
GET  /api/drive/status — check Drive connection for current user
"""

import logging
import json
import io
from pathlib import PurePosixPath
import re
from datetime import datetime, timezone
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, File, Form, Header, UploadFile
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth.routes import get_current_user
from app.config import settings
from app.database import get_db
from app.lib import markdown_import as _markdown_import
from app.lib.slugify import to_slug as slugify
from app.models import GoogleToken, OrgRole, Organization, Page, Section, User
from app.services.encryption import get_encryption_service
from app.services.drive import google_drive_handler

logger = logging.getLogger(__name__)
router = APIRouter()

# Backward-compatible binding so deploys do not crash if app/lib version lags.
normalize_imported_markdown = _markdown_import.normalize_imported_markdown
normalize_synced_html = getattr(_markdown_import, "normalize_synced_html", lambda html: html)
clean_google_docs_html = getattr(_markdown_import, "clean_google_docs_html", lambda html: html)

DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"
GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
GOOGLE_TOKEN_REFRESH_URL = "https://oauth2.googleapis.com/token"
MAX_FOLDER_DEPTH = 5
MAX_LOCAL_FILE_BYTES = 20 * 1024 * 1024
ALLOWED_LOCAL_EXTENSIONS: dict[str, str] = {
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".html": "text/html",
    ".htm": "text/html",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".pdf": "application/pdf",
    ".rtf": "application/rtf",
}

_NUM_PREFIX = re.compile(r"^\d+[\.\s]+")


# ---------------------------------------------------------------------------
# Shared Drive credential helper
# ---------------------------------------------------------------------------

DRIVE_WRITE_SCOPE = "https://www.googleapis.com/auth/drive"
DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"


def _token_has_write_scope(google_token: GoogleToken) -> bool:
    """Check if a stored token has full Drive write scope (not just readonly)."""
    scope = (google_token.scope or "").strip()
    if not scope:
        return False
    scopes = scope.split()
    # Full drive scope grants write; drive.readonly alone does not
    return DRIVE_WRITE_SCOPE in scopes


async def get_drive_credentials(user: User, org_id: int, db: Session, *, require_write: bool = True) -> Credentials:
    """Return fresh Google Credentials for org operations.

    Resolution order:
    1. Current member's token for this org.
    2. Current member's latest token from any org (multi-workspace login).
    3. Owner/admin token for this org.
    4. Owner/admin latest token from any org.

    When require_write=True (default), tokens with only drive.readonly scope
    are rejected — the user must re-authenticate to grant full Drive access.
    """

    def _latest_token_for_user(user_id: int, preferred_org_id: int | None = None) -> GoogleToken | None:
        query = db.query(GoogleToken).filter(GoogleToken.user_id == user_id)
        if preferred_org_id is not None:
            token = (
                query.filter(GoogleToken.organization_id == preferred_org_id)
                .order_by(GoogleToken.updated_at.desc(), GoogleToken.id.desc())
                .first()
            )
            if token:
                return token
        return query.order_by(GoogleToken.updated_at.desc(), GoogleToken.id.desc()).first()

    google_token = _latest_token_for_user(user.id, org_id)
    if not google_token:
        privileged_roles = (
            db.query(OrgRole)
            .filter(
                OrgRole.organization_id == org_id,
                OrgRole.role.in_(("owner", "admin")),
            )
            .order_by(OrgRole.created_at.asc(), OrgRole.id.asc())
            .all()
        )
        for role in privileged_roles:
            google_token = _latest_token_for_user(role.user_id, org_id)
            if google_token:
                break

    if not google_token:
        raise HTTPException(
            status_code=401,
            detail="No Google Drive credentials found for this workspace. Ask owner/admin to reconnect Drive.",
        )

    # Validate scope — reject readonly tokens when write access is required
    if require_write and not _token_has_write_scope(google_token):
        logger.warning(
            "Token for user %d org %d has insufficient scope: %s",
            google_token.user_id, org_id, google_token.scope,
        )
        raise HTTPException(
            status_code=403,
            detail="Your Google Drive connection has read-only permissions. "
                   "Please reconnect Drive (Settings > Integrations) to grant full access.",
        )

    enc = get_encryption_service()
    try:
        refresh_token = enc.decrypt(google_token.encrypted_refresh_token)
    except Exception:
        raise HTTPException(status_code=401, detail="Failed to read stored Drive credentials")

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(GOOGLE_TOKEN_REFRESH_URL, data={
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        })

    if resp.status_code != 200:
        raise HTTPException(
            status_code=401,
            detail="Google token refresh failed. Please reconnect your Drive account.",
        )

    access_token = resp.json().get("access_token")
    if not access_token:
        raise HTTPException(
            status_code=401,
            detail="Google token refresh did not return an access token. Please reconnect Drive.",
        )
    google_token.last_refreshed_at = datetime.now(timezone.utc)
    db.commit()
    return Credentials(token=access_token)


def _resolve_org_role(user: User, db: Session, requested_org_id: int | None = None) -> OrgRole:
    query = db.query(OrgRole).filter(OrgRole.user_id == user.id)
    if requested_org_id is not None:
        query = query.filter(OrgRole.organization_id == requested_org_id)
    role = query.first()
    if not role:
        raise HTTPException(status_code=403, detail="User has no organization")
    return role


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class ScanRequest(BaseModel):
    folder_id: str
    parent_section_id: int | None = None
    target_type: Literal["product", "version", "tab", "section"] | None = None


class EnsureDocAccessRequest(BaseModel):
    doc_id: str


# ---------------------------------------------------------------------------
# Internal scan logic
# ---------------------------------------------------------------------------

def _clean_name(raw: str) -> str:
    return _NUM_PREFIX.sub("", raw).strip() or raw


def _unique_section_slug(name: str, org_id: int, parent_id: int | None, db: Session) -> str:
    base = slugify(_clean_name(name))
    slug, n = base, 1
    while True:
        q = db.query(Section).filter(
            Section.organization_id == org_id,
            Section.parent_id == parent_id,
            Section.slug == slug,
        )
        if not q.first():
            return slug
        slug, n = f"{base}-{n}", n + 1


def _unique_page_slug(title: str, org_id: int, db: Session, exclude_id: int | None = None) -> str:
    base = slugify(_clean_name(title))
    slug, n = base, 1
    while True:
        q = db.query(Page).filter(Page.organization_id == org_id, Page.slug == slug)
        if exclude_id:
            q = q.filter(Page.id != exclude_id)
        if not q.first():
            return slug
        slug, n = f"{base}-{n}", n + 1


def _infer_target_type(section: Section) -> Literal["product", "version", "tab", "section"]:
    if section.parent_id is None:
        return "product"
    if (section.section_type or "section") == "version":
        return "version"
    if (section.section_type or "section") == "tab":
        return "tab"
    return "section"


def _resolve_target_section(
    *,
    org_id: int,
    db: Session,
    section_id: int | None,
    expected_type: Literal["product", "version", "tab", "section"] | None,
) -> Section | None:
    if section_id is None:
        return None

    section = db.query(Section).filter(
        Section.id == section_id,
        Section.organization_id == org_id,
    ).first()
    if not section:
        raise HTTPException(status_code=404, detail="Target section not found")

    actual_type = _infer_target_type(section)
    if expected_type and expected_type != actual_type:
        raise HTTPException(
            status_code=400,
            detail=f"Target type mismatch: expected '{expected_type}', got '{actual_type}'",
        )
    return section


def _ensure_section_drive_folder(
    *,
    service,
    org: Organization | None,
    section: Section,
    db: Session,
) -> str:
    if section.drive_folder_id:
        return section.drive_folder_id

    parent_drive_id: str | None = None
    if section.parent_id:
        parent_section = db.get(Section, section.parent_id)
        parent_drive_id = parent_section.drive_folder_id if parent_section else None
    if not parent_drive_id and org:
        parent_drive_id = org.drive_folder_id
    if not parent_drive_id:
        raise HTTPException(
            status_code=400,
            detail="Target section has no Drive folder and workspace root is not configured",
        )

    folder_id = _create_drive_folder(service, section.name, parent_drive_id)
    section.drive_folder_id = folder_id
    db.commit()
    return folder_id


def _normalize_relative_path(path: str) -> str:
    candidate = (path or "").replace("\\", "/").strip().strip("/")
    if not candidate:
        raise HTTPException(status_code=400, detail="Folder import includes an empty file path")
    parts = [segment.strip() for segment in candidate.split("/") if segment.strip() and segment != "."]
    if not parts or any(segment == ".." for segment in parts):
        raise HTTPException(status_code=400, detail=f"Invalid folder path '{path}'")
    normalized = "/".join(parts)
    if normalized.startswith("../") or "/../" in normalized:
        raise HTTPException(status_code=400, detail=f"Invalid folder path '{path}'")
    return normalized


def _drive_import_mime_for_filename(filename: str) -> str:
    ext = PurePosixPath(filename or "").suffix.lower()
    mime = ALLOWED_LOCAL_EXTENSIONS.get(ext)
    if not mime:
        allowed = ", ".join(sorted(ALLOWED_LOCAL_EXTENSIONS.keys()))
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{ext or 'unknown'}'. Allowed: {allowed}",
        )
    return mime


def _markdown_to_html(md_bytes: bytes) -> bytes:
    """Convert markdown content to HTML for better Google Drive import fidelity.

    Google Drive's native markdown-to-Doc conversion is poor (treats markdown
    syntax as literal text). Converting to HTML first preserves headings, lists,
    bold, links, code blocks, tables, etc.
    """
    import markdown as _md

    text = md_bytes.decode("utf-8", errors="replace")
    text = normalize_imported_markdown(text)
    html = _md.markdown(
        text,
        extensions=[
            "tables",
            "fenced_code",
            "codehilite",
            "toc",
            "nl2br",
            "sane_lists",
            "admonition",
            "attr_list",
        ],
    )
    # Wrap in minimal HTML structure so Drive recognizes it as a proper document
    full_html = (
        '<!DOCTYPE html><html><head><meta charset="utf-8"></head>'
        f"<body>{html}</body></html>"
    )
    return full_html.encode("utf-8")


async def _upload_local_as_google_doc(
    *,
    service,
    upload: UploadFile,
    parent_drive_folder_id: str,
    destination_name: str | None = None,
) -> tuple[str, str | None]:
    """Upload a local file to Google Drive as a Google Doc.

    Returns:
        (doc_id, generated_html) — generated_html is the HTML we produced from
        markdown files so the caller can store it directly, bypassing the lossy
        Google Docs round-trip.  None for non-markdown uploads.
    """
    filename = (destination_name or upload.filename or "Untitled").strip() or "Untitled"
    mime = _drive_import_mime_for_filename(filename)
    content = await upload.read()
    if not content:
        raise HTTPException(status_code=400, detail=f"File '{filename}' is empty")
    if len(content) > MAX_LOCAL_FILE_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"File '{filename}' exceeds {MAX_LOCAL_FILE_BYTES // (1024 * 1024)}MB limit",
        )

    # Convert markdown to HTML before uploading — Google Drive handles HTML
    # conversion to Docs much better than raw markdown.
    # Keep the generated HTML so we can store it directly on the Page record.
    generated_html: str | None = None
    if mime == "text/markdown":
        content = _markdown_to_html(content)
        generated_html = content.decode("utf-8")
        mime = "text/html"

    title = _clean_name(PurePosixPath(filename).stem) or "Untitled"
    media = MediaIoBaseUpload(io.BytesIO(content), mimetype=mime, resumable=False)
    result = service.files().create(
        body={
            "name": title,
            "mimeType": GOOGLE_DOC_MIME,
            "parents": [parent_drive_folder_id],
        },
        media_body=media,
        fields="id,name,mimeType",
        supportsAllDrives=True,
    ).execute()
    return result["id"], generated_html

def _trash_drive_item(service, file_id: str) -> None:
    """Move a Drive file/folder to trash."""
    service.files().update(
        fileId=file_id,
        body={"trashed": True},
        supportsAllDrives=True,
    ).execute()


def _move_drive_item(service, file_id: str, new_parent_id: str) -> None:
    """Move a Drive file/folder to a new parent folder."""
    meta = service.files().get(
        fileId=file_id,
        fields="parents",
        supportsAllDrives=True,
    ).execute()
    old_parents = ",".join(meta.get("parents", []))
    service.files().update(
        fileId=file_id,
        addParents=new_parent_id,
        removeParents=old_parents,
        fields="id,parents",
        supportsAllDrives=True,
    ).execute()


def _create_drive_folder(service, name: str, parent_id: str | None) -> str:
    """Create a Drive folder and return its ID."""
    metadata: dict = {"name": name, "mimeType": DRIVE_FOLDER_MIME}
    if parent_id:
        metadata["parents"] = [parent_id]
    f = service.files().create(body=metadata, fields="id", supportsAllDrives=True).execute()
    return f["id"]


def _create_drive_doc(service, title: str, parent_id: str | None) -> str:
    """Create a blank Google Doc and return its ID."""
    metadata: dict = {"name": title, "mimeType": GOOGLE_DOC_MIME}
    if parent_id:
        metadata["parents"] = [parent_id]
    f = service.files().create(body=metadata, fields="id", supportsAllDrives=True).execute()
    return f["id"]


def _list_folder_items(service, folder_id: str) -> list[dict]:
    results = service.files().list(
        q=f"'{folder_id}' in parents and trashed=false",
        fields="files(id,name,mimeType,modifiedTime)",
        pageSize=200,
        orderBy="folder,name",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    return results.get("files", [])


def _scan_folder(
    service,
    folder_id: str,
    parent_section_id: int | None,
    org_id: int,
    user_id: int,
    db: Session,
    depth: int = 0,
) -> dict[str, int]:
    """Recursively scan a Drive folder, creating Sections and Pages.

    This function is intentionally non-destructive for existing rows so moves
    between folders during a single scan do not lose DB identity/history.
    """
    counts: dict[str, int] = {"sections": 0, "pages": 0, "updated": 0, "removed": 0}
    if depth > MAX_FOLDER_DEPTH:
        return counts

    items = _list_folder_items(service, folder_id)
    page_order = 0

    for i, item in enumerate(items):
        mime = item["mimeType"]
        name = item["name"]
        item_id = item["id"]
        modified = item.get("modifiedTime")

        if mime == DRIVE_FOLDER_MIME:
            existing_section = db.query(Section).filter(
                Section.organization_id == org_id,
                Section.drive_folder_id == item_id,
            ).first()

            if existing_section:
                existing_section.display_order = i
                existing_section.is_published = True
                section = existing_section
            else:
                slug = _unique_section_slug(name, org_id, parent_section_id, db)
                section = Section(
                    organization_id=org_id,
                    parent_id=parent_section_id,
                    name=_clean_name(name),
                    slug=slug,
                    section_type="section",
                    drive_folder_id=item_id,
                    display_order=i,
                    is_published=True,
                )
                db.add(section)
                db.flush()
                counts["sections"] += 1

            sub = _scan_folder(service, item_id, section.id, org_id, user_id, db, depth + 1)
            counts["sections"] += sub["sections"]
            counts["pages"] += sub["pages"]
            counts["updated"] += sub["updated"]
            counts["removed"] += sub["removed"]

        elif mime == GOOGLE_DOC_MIME:
            title = _clean_name(name)
            existing_page = db.query(Page).filter(
                Page.organization_id == org_id,
                Page.google_doc_id == item_id,
            ).first()

            if existing_page:
                if existing_page.drive_modified_at != modified:
                    existing_page.drive_modified_at = modified
                    counts["updated"] += 1
                existing_page.display_order = page_order
                if parent_section_id is not None:
                    existing_page.section_id = parent_section_id
            else:
                slug = _unique_page_slug(title, org_id, db)
                page = Page(
                    organization_id=org_id,
                    section_id=parent_section_id,
                    google_doc_id=item_id,
                    title=title,
                    slug=slug,
                    drive_modified_at=modified,
                    display_order=page_order,
                    owner_id=user_id,
                )
                db.add(page)
                counts["pages"] += 1

            page_order += 1

    db.commit()
    return counts


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/status")
def drive_status(
    x_org_id: int | None = Header(default=None, alias="X-Org-Id"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    org_role = _resolve_org_role(user, db, x_org_id)

    org = db.get(Organization, org_role.organization_id)
    # Workspace-level connectivity (any member token exists for this org)
    workspace_token = (
        db.query(GoogleToken)
        .join(OrgRole, OrgRole.user_id == GoogleToken.user_id)
        .filter(OrgRole.organization_id == org_role.organization_id)
        .order_by(GoogleToken.updated_at.desc(), GoogleToken.id.desc())
        .first()
    )
    # Current member capability for this workspace
    member_token = (
        db.query(GoogleToken)
        .filter(
            GoogleToken.user_id == user.id,
            GoogleToken.organization_id == org_role.organization_id,
        )
        .order_by(GoogleToken.updated_at.desc(), GoogleToken.id.desc())
        .first()
    )
    has_write = _token_has_write_scope(member_token) if member_token else False
    return {
        "connected": workspace_token is not None,
        "has_write_access": has_write,
        "drive_folder_id": org.drive_folder_id if org else None,
        "last_refreshed_at": (
            member_token.last_refreshed_at.isoformat() if member_token and member_token.last_refreshed_at else None
        ),
    }


@router.post("/ensure-doc-access")
async def ensure_doc_access(
    body: EnsureDocAccessRequest,
    x_org_id: int | None = Header(default=None, alias="X-Org-Id"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Ensure current member can open a Google Doc source file.

    Uses the shared ACL sync handler and returns the Google Docs edit URL.
    """
    if not body.doc_id.strip():
        raise HTTPException(status_code=400, detail="Document ID required")

    result = await google_drive_handler(
        body={
            "action": "ensure_doc_access",
            "docId": body.doc_id,
            "_x_org_id": x_org_id,
        },
        db=db,
        user=user,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=403, detail=result.get("error") or "Unable to grant Google Docs access")
    return result


@router.post("/scan")
async def scan_folder(
    body: ScanRequest,
    x_org_id: int | None = Header(default=None, alias="X-Org-Id"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Scan a Google Drive folder and create/update Sections and Pages.

    HTML content is NOT fetched here — call /sync after scan to pull content.
    """
    org_role = _resolve_org_role(user, db, x_org_id)
    if not org_role or org_role.role not in ("owner", "admin", "editor"):
        raise HTTPException(status_code=403, detail="Editor role required")

    org_id = org_role.organization_id
    org = db.get(Organization, org_id)
    target_section = _resolve_target_section(
        org_id=org_id,
        db=db,
        section_id=body.parent_section_id,
        expected_type=body.target_type,
    )

    # Root folder is workspace-level configuration and must stay owner/admin only.
    if body.parent_section_id is None and (not org or not org.drive_folder_id):
        if org_role.role not in ("owner", "admin"):
            raise HTTPException(
                status_code=403,
                detail="Only workspace owner/admin can configure Drive root folder",
            )

    if target_section and org and org.drive_folder_id and body.folder_id == org.drive_folder_id:
        raise HTTPException(
            status_code=400,
            detail="Cannot import the connected root into a nested destination.",
        )

    if body.parent_section_id is None and org and org.drive_folder_id and body.folder_id != org.drive_folder_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "Import-from-drive only supports the connected root folder. "
                "Use the additional import flow for other folders."
            ),
        )

    creds = await get_drive_credentials(user, org_id, db)
    service = build("drive", "v3", credentials=creds, cache_discovery=False)

    try:
        folder_meta = service.files().get(
            fileId=body.folder_id,
            fields="id,name,mimeType,parents",
            supportsAllDrives=True,
        ).execute()
    except Exception as e:
        logger.error("Could not access Drive folder: %s", e)
        raise HTTPException(status_code=400, detail="Could not access the specified folder. Please check the folder ID and your permissions.")

    if folder_meta.get("mimeType") != DRIVE_FOLDER_MIME:
        raise HTTPException(status_code=400, detail="Provided ID is not a Drive folder")

    # Store root folder on org (first-time setup)
    if org and not org.drive_folder_id and body.parent_section_id is None:
        org.drive_folder_id = body.folder_id
        db.commit()

    if target_section is not None:
        target_drive_parent = _ensure_section_drive_folder(service=service, org=org, section=target_section, db=db)
        current_parents = folder_meta.get("parents") or []
        if body.folder_id != target_drive_parent and target_drive_parent not in current_parents:
            _move_drive_item(service, body.folder_id, target_drive_parent)
            logger.info(
                "Moved imported Drive folder %s under section %d (%s)",
                body.folder_id,
                target_section.id,
                target_drive_parent,
            )

    scan_parent_section_id = body.parent_section_id
    if target_section is not None:
        if body.folder_id == target_section.drive_folder_id:
            scan_parent_section_id = target_section.id
        else:
            folder_name = _clean_name(folder_meta.get("name", "Imported section"))
            imported_root_section = db.query(Section).filter(
                Section.organization_id == org_id,
                Section.drive_folder_id == body.folder_id,
            ).first()
            if imported_root_section:
                imported_root_section.parent_id = target_section.id
                imported_root_section.name = folder_name
                imported_root_section.display_order = 0
                imported_root_section.is_published = True
            else:
                imported_root_section = Section(
                    organization_id=org_id,
                    parent_id=target_section.id,
                    name=folder_name,
                    slug=_unique_section_slug(folder_name, org_id, target_section.id, db),
                    section_type="section",
                    drive_folder_id=body.folder_id,
                    display_order=0,
                    is_published=True,
                )
                db.add(imported_root_section)
                db.flush()
            db.commit()
            scan_parent_section_id = imported_root_section.id

    # Root onboarding scans treat the selected folder as an invisible workspace container.
    # Targeted scans first map the selected folder as a child section under the target.
    counts = _scan_folder(
        service=service,
        folder_id=body.folder_id,
        parent_section_id=scan_parent_section_id,
        org_id=org_id,
        user_id=user.id,
        db=db,
    )

    logger.info(
        "Scan complete org=%d: +%d sections, +%d pages, ~%d updated",
        org_id, counts["sections"], counts["pages"], counts["updated"],
    )
    return {
        "ok": True,
        "folder_name": folder_meta.get("name"),
        "sections_created": counts["sections"],
        "pages_created": counts["pages"],
        "pages_updated": counts["updated"],
    }


@router.post("/import/local")
async def import_local(
    target_section_id: int = Form(...),
    target_type: Literal["product", "version", "tab", "section"] | None = Form(default=None),
    mode: Literal["files", "folder"] = Form(default="files"),
    relative_paths_json: str | None = Form(default=None),
    files: list[UploadFile] = File(...),
    x_org_id: int | None = Header(default=None, alias="X-Org-Id"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Import local files/folders into a target section and sync DB from Drive."""
    org_role = _resolve_org_role(user, db, x_org_id)
    if not org_role or org_role.role not in ("owner", "admin", "editor"):
        raise HTTPException(status_code=403, detail="Editor role required")
    org_id = org_role.organization_id
    org = db.get(Organization, org_id)
    if not org or not org.drive_folder_id:
        raise HTTPException(status_code=400, detail="Connect Drive and configure a root folder first")

    target_section = _resolve_target_section(
        org_id=org_id,
        db=db,
        section_id=target_section_id,
        expected_type=target_type,
    )
    if target_section is None:
        raise HTTPException(status_code=400, detail="Target section is required")

    effective_target_type = _infer_target_type(target_section)
    if mode == "files" and effective_target_type != "section":
        raise HTTPException(
            status_code=400,
            detail="File import is supported only for section targets. Choose a section destination.",
        )

    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")

    try:
        creds = await get_drive_credentials(user, org_id, db)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Drive credentials failed during import")
        raise HTTPException(status_code=500, detail=f"Drive credentials error: {exc}")

    service = build("drive", "v3", credentials=creds, cache_discovery=False)

    try:
        target_drive_folder_id = _ensure_section_drive_folder(service=service, org=org, section=target_section, db=db)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to ensure section Drive folder")
        raise HTTPException(status_code=500, detail=f"Drive folder setup error: {exc}")

    relative_paths: list[str] = []
    if relative_paths_json:
        try:
            payload = json.loads(relative_paths_json)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="Invalid relative_paths_json payload") from exc
        if not isinstance(payload, list) or not all(isinstance(item, str) for item in payload):
            raise HTTPException(status_code=400, detail="relative_paths_json must be a JSON string array")
        relative_paths = payload
        if len(relative_paths) != len(files):
            raise HTTPException(status_code=400, detail="relative_paths_json size must match uploaded files")

    folder_cache: dict[str, str] = {"": target_drive_folder_id}
    uploaded = 0
    # Map Drive doc ID → HTML we generated locally (markdown files only).
    # Used to backfill html_content on Page records after scan, bypassing
    # the lossy Google Docs HTML round-trip.
    import_html_map: dict[str, str] = {}

    for idx, upload in enumerate(files):
        raw_relative = (
            relative_paths[idx]
            if idx < len(relative_paths)
            else (upload.filename or "")
        )
        normalized_relative = _normalize_relative_path(raw_relative)
        relative_parts = normalized_relative.split("/")
        file_name = relative_parts[-1]
        folder_parts = relative_parts[:-1] if mode == "folder" else []

        parent_path = ""
        parent_drive = target_drive_folder_id
        for folder_name in folder_parts:
            parent_path = f"{parent_path}/{folder_name}" if parent_path else folder_name
            cached = folder_cache.get(parent_path)
            if cached:
                parent_drive = cached
                continue
            created_folder = _create_drive_folder(service, _clean_name(folder_name), parent_drive)
            folder_cache[parent_path] = created_folder
            parent_drive = created_folder

        try:
            doc_id, generated_html = await _upload_local_as_google_doc(
                service=service,
                upload=upload,
                parent_drive_folder_id=parent_drive,
                destination_name=file_name,
            )
            if generated_html:
                import_html_map[doc_id] = generated_html
            uploaded += 1
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("Failed to upload file '%s' to Drive", file_name)
            raise HTTPException(
                status_code=500,
                detail=f"Failed to upload '{file_name}' to Google Drive: {exc}",
            )

    counts = _scan_folder(
        service=service,
        folder_id=target_drive_folder_id,
        parent_section_id=target_section.id,
        org_id=org_id,
        user_id=user.id,
        db=db,
    )

    # Backfill html_content on Pages created from markdown imports.
    # This preserves the original markdown structure instead of relying on
    # Google Docs' lossy HTML re-export during sync.
    if import_html_map:
        for doc_id, html in import_html_map.items():
            page = db.query(Page).filter(
                Page.organization_id == org_id,
                Page.google_doc_id == doc_id,
            ).first()
            if page and not page.html_content:
                page.html_content = html
                page.last_synced_at = datetime.now(timezone.utc).isoformat()
        db.commit()

    logger.info(
        "Local import complete org=%d target=%d mode=%s: %d files uploaded",
        org_id,
        target_section.id,
        mode,
        uploaded,
    )
    return {
        "ok": True,
        "target_section_id": target_section.id,
        "target_type": effective_target_type,
        "mode": mode,
        "uploaded_files": uploaded,
        "sections_created": counts["sections"],
        "pages_created": counts["pages"],
        "pages_updated": counts["updated"],
    }


def _is_file_not_found(exc: Exception) -> bool:
    """Check if a Google API error indicates the file was deleted/trashed."""
    from googleapiclient.errors import HttpError
    if isinstance(exc, HttpError):
        return exc.resp.status in (404, 410)
    msg = str(exc).lower()
    return "not found" in msg or "404" in msg or "file not found" in msg


@router.post("/sync")
async def sync_all_pages(
    x_org_id: int | None = Header(default=None, alias="X-Org-Id"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Fetch latest HTML from Google Drive for all pages in the org.

    - Skips pages whose drive_modified_at matches the stored value (already up to date).
    - Removes pages whose Drive docs are explicitly marked trashed.
    - Removes sections whose Drive folders are explicitly marked trashed and empty.
    """
    org_role = _resolve_org_role(user, db, x_org_id)
    if not org_role or org_role.role not in ("owner", "admin", "editor"):
        raise HTTPException(status_code=403, detail="Editor role required")

    org_id = org_role.organization_id
    creds = await get_drive_credentials(user, org_id, db, require_write=False)
    service = build("drive", "v3", credentials=creds, cache_discovery=False)

    pages = db.query(Page).filter(Page.organization_id == org_id).all()
    synced, skipped, errors = 0, 0, 0
    removed_pages: list[int] = []

    for page in pages:
        if not page.google_doc_id:
            skipped += 1
            continue

        try:
            meta = service.files().get(
                fileId=page.google_doc_id,
                fields="modifiedTime,trashed",
                supportsAllDrives=True,
            ).execute()
        except Exception as e:
            if _is_file_not_found(e):
                logger.warning(
                    "Page %d (%s) not visible in Drive metadata lookup; keeping row to avoid destructive false positives",
                    page.id, page.google_doc_id,
                )
                errors += 1
                continue
            logger.warning("Metadata fetch failed page %d (%s): %s", page.id, page.google_doc_id, e)
            errors += 1
            continue

        # File exists but is trashed — treat as deleted
        if meta.get("trashed"):
            logger.info(
                "Page %d (%s) is trashed in Drive — removing from DB",
                page.id, page.google_doc_id,
            )
            removed_pages.append(page.id)
            db.delete(page)
            continue

        drive_mod = meta.get("modifiedTime")
        if drive_mod == page.drive_modified_at and page.html_content:
            # Self-heal cached HTML after parser/normalizer improvements.
            # Without this, unchanged Drive files stay stuck with legacy
            # leaked frontmatter/callout formatting forever.
            cleaned_cached = clean_google_docs_html(page.html_content)
            rehydrated_cached = normalize_synced_html(cleaned_cached)
            if rehydrated_cached != page.html_content:
                if page.is_published and rehydrated_cached != page.published_html:
                    page.status = "draft"
                page.html_content = rehydrated_cached
                page.last_synced_at = datetime.now(timezone.utc).isoformat()
                synced += 1
            else:
                skipped += 1
            continue

        try:
            raw = service.files().export(
                fileId=page.google_doc_id, mimeType="text/html"
            ).execute()
            html = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        except Exception as e:
            logger.warning("Export failed page %d (%s): %s", page.id, page.google_doc_id, e)
            errors += 1
            continue

        # Strip Google Docs inline styles / wrapper tags, then apply
        # frontmatter/callout normalization.
        cleaned_html = clean_google_docs_html(html)
        normalized_html = normalize_synced_html(cleaned_html)

        if page.is_published and normalized_html != page.published_html:
            page.status = "draft"
        page.html_content = normalized_html
        page.drive_modified_at = drive_mod
        page.last_synced_at = datetime.now(timezone.utc).isoformat()
        synced += 1

    # Clean up orphaned sections whose Drive folders no longer exist
    removed_sections: list[int] = []
    sections = db.query(Section).filter(
        Section.organization_id == org_id,
        Section.drive_folder_id.isnot(None),
    ).all()

    for section in sections:
        try:
            folder_meta = service.files().get(
                fileId=section.drive_folder_id,
                fields="trashed",
                supportsAllDrives=True,
            ).execute()
            if not folder_meta.get("trashed"):
                continue
        except Exception as e:
            # Treat not-found as non-destructive here: could be permission/visibility
            # differences for the acting member token.
            continue

        # Folder is gone or trashed — check if section still has any pages
        remaining_pages = db.query(Page).filter(
            Page.section_id == section.id,
        ).count()
        remaining_children = db.query(Section).filter(
            Section.parent_id == section.id,
        ).count()

        if remaining_pages == 0 and remaining_children == 0:
            logger.info(
                "Section %d (%s) Drive folder gone and empty — removing",
                section.id, section.drive_folder_id,
            )
            removed_sections.append(section.id)
            db.delete(section)

    db.commit()
    logger.info(
        "Sync complete org=%d: %d synced, %d skipped, %d errors, %d pages removed, %d sections removed",
        org_id, synced, skipped, errors, len(removed_pages), len(removed_sections),
    )
    return {
        "ok": True,
        "synced": synced,
        "skipped": skipped,
        "errors": errors,
        "removed_pages": len(removed_pages),
        "removed_sections": len(removed_sections),
    }
