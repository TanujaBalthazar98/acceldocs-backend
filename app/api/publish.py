"""Bulk publish API — publish all docs for an org to the MkDocs git repo."""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.middleware.auth import get_current_user_optional
from app.models import Document, Organization, OrgRole, Project, User
from app.services.documents import _publish_to_git, _resolve_publish_path

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/publish/mkdocs")
async def publish_mkdocs(
    body: dict,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user_optional),
):
    """Publish all is_published documents for an org to the MkDocs git repo.

    Called by GeneralSettings when the user clicks "Publish to MkDocs".
    Iterates every published document, fetches content from Drive if needed,
    converts to Markdown, and commits to the docs git repo.

    Returns a summary: { ok, published, skipped, errors, pagesUrl }
    """
    if not user:
        return {"ok": False, "error": "Authentication required"}

    try:
        org_id = int(body.get("organizationId") or body.get("organization_id") or 0)
    except (ValueError, TypeError):
        org_id = 0
    if not org_id:
        return {"ok": False, "error": "organizationId required"}

    # Verify user belongs to this org
    org_role = (
        db.query(OrgRole)
        .filter(OrgRole.user_id == user.id, OrgRole.organization_id == org_id)
        .first()
    )
    if not org_role:
        return {"ok": False, "error": "Not a member of this organization"}
    if org_role.role not in ("owner", "admin"):
        return {"ok": False, "error": "Only owners and admins can publish"}

    # Get the org for site metadata
    org = db.get(Organization, org_id)

    # Fetch all published documents for this org's projects
    project_ids = [
        row.id
        for row in db.query(Project.id).filter(Project.organization_id == org_id).all()
    ]
    if not project_ids:
        return {"ok": True, "published": 0, "skipped": 0, "errors": 0, "pagesUrl": None}

    docs = (
        db.query(Document)
        .options(
            joinedload(Document.project_rel).joinedload(Project.parent),
            joinedload(Document.project_version),
            joinedload(Document.topic),
        )
        .filter(
            Document.project_id.in_(project_ids),
            Document.is_published == True,
        )
        .all()
    )

    published = 0
    skipped = 0
    errors = 0

    for doc in docs:
        try:
            commit_sha = _publish_to_git(doc)
            if commit_sha:
                doc.last_published_at = datetime.now(timezone.utc).isoformat()
                published += 1
            else:
                skipped += 1
        except Exception as e:
            logger.error("Failed to publish doc %s: %s", doc.id, e)
            errors += 1

    if published > 0:
        db.commit()

    # Build the expected GitHub Pages URL from the org domain or slug
    pages_url = None
    if org:
        domain = getattr(org, "domain", None) or getattr(org, "slug", None)
        if domain:
            pages_url = f"https://{domain}.github.io/docs"

    return {
        "ok": True,
        "published": published,
        "skipped": skipped,
        "errors": errors,
        "pagesUrl": pages_url,
    }
