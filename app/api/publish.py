"""Bulk publish API — publish all docs for an org via Zensical (git push)."""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.middleware.auth import get_current_user_optional
from app.models import Document, Organization, OrgRole, Project, User
from app.publishing.git_publisher import push_branch
from app.services.documents import _publish_to_git
from app.services.encryption import get_encryption_service

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
            commit_sha = _publish_to_git(doc, db=db)
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

    # Push to the GitHub remote so Zensical picks up the changes
    push_ok = False
    push_error: str | None = None
    if published > 0 and org and org.github_repo_full_name and org.github_token_encrypted:
        try:
            token = get_encryption_service().decrypt(org.github_token_encrypted)
            # Re-stamp the remote URL with the current token before pushing
            from app.api.github_publish import _set_docs_repo_remote
            _set_docs_repo_remote(org.github_repo_full_name, token)
            push_ok = push_branch("main")
            if not push_ok:
                push_error = "Docs committed locally but push to GitHub failed."
        except Exception as exc:
            logger.warning("Push to GitHub failed: %s", exc)
            push_error = "Docs committed but push to GitHub failed. Check your GitHub connection."
    elif published > 0 and not (org and org.github_repo_full_name):
        push_error = "No GitHub repository configured. Connect GitHub in Settings to publish remotely."

    # Prefer the stored Pages URL; fall back to deriving from username
    pages_url = (org.github_pages_url if org else None) or None

    result: dict = {
        "ok": True,
        "published": published,
        "skipped": skipped,
        "errors": errors,
        "pushed": push_ok,
        "pagesUrl": pages_url,
    }
    if push_error:
        result["pushWarning"] = push_error
    return result
