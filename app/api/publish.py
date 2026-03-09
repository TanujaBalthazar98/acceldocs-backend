"""Bulk publish API — publish all docs for an org via Zensical (git push)."""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.middleware.auth import get_current_user_optional
from app.models import Document, Organization, OrgRole, Project, User
from app.publishing.git_publisher import push_branch, deploy_to_gh_pages, get_repo
from app.publishing.mkdocs_gen import write_zensical_toml
from app.config import settings as _settings
from app.services.documents import _publish_to_git, _set_branding_from_doc
from app.services.encryption import get_encryption_service

logger = logging.getLogger(__name__)

router = APIRouter()


def _try_fetch_content_from_drive(doc: Document, google_token: str | None) -> str | None:
    """Attempt to fetch HTML content from Google Drive for a document.

    Tries the user's OAuth token first (passed from frontend), then falls
    back to the service-account approach.
    """
    if not doc.google_doc_id:
        return None

    # Try with user's OAuth token
    if google_token:
        try:
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build

            creds = Credentials(token=google_token)
            service = build("docs", "v1", credentials=creds, cache_discovery=False)
            # Export as HTML via Drive API
            drive_service = build("drive", "v3", credentials=creds, cache_discovery=False)
            content = drive_service.files().export(
                fileId=doc.google_doc_id, mimeType="text/html"
            ).execute()
            html = content.decode("utf-8") if isinstance(content, bytes) else content
            if html and html.strip():
                logger.info("Fetched content from Drive (OAuth) for doc %s", doc.id)
                return html
        except Exception as e:
            logger.warning("OAuth fetch failed for doc %s: %s", doc.id, e)

    # Fallback: service account
    try:
        from app.ingestion.drive import _get_service, export_doc_as_html
        service = _get_service()
        html = export_doc_as_html(service, doc.google_doc_id)
        if html and html.strip():
            logger.info("Fetched content from Drive (service account) for doc %s", doc.id)
            return html
    except Exception as e:
        logger.warning("Service account fetch failed for doc %s: %s", doc.id, e)

    return None


@router.post("/publish/mkdocs")
async def publish_mkdocs(
    body: dict,
    request: Request = None,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user_optional),
):
    """Publish all documents for an org to the docs git repo.

    Called by the frontend when the user clicks "Publish".
    Iterates every document in the org's projects, fetches content from
    Drive if needed, converts to Markdown, and commits to the docs git repo.
    Then builds with Zensical and pushes to gh-pages.

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

    # Extract Google access token from header (for fetching content on-the-fly)
    google_token = None
    if request:
        google_token = request.headers.get("x-google-token")
    if not google_token:
        google_token = body.get("_google_access_token") or body.get("googleAccessToken")

    # Get all org projects
    projects = db.query(Project).filter(Project.organization_id == org_id).all()
    project_ids = [p.id for p in projects]
    if not project_ids:
        return {"ok": True, "published": 0, "skipped": 0, "errors": 0, "pagesUrl": None}

    # Build slug→id map for backfilling orphan docs
    slug_to_project: dict[str, Project] = {}
    for p in projects:
        if p.slug:
            slug_to_project[p.slug.lower()] = p
        if p.name:
            slug_to_project[p.name.lower()] = p
            slug_to_project[p.name.lower().replace(" ", "-")] = p

    # ----- Collect ALL publishable docs -----
    # Step 1: docs with project_id set
    docs_with_pid = (
        db.query(Document)
        .options(
            joinedload(Document.project_rel).joinedload(Project.parent),
            joinedload(Document.project_version),
            joinedload(Document.topic),
        )
        .filter(Document.project_id.in_(project_ids))
        .all()
    )

    # Step 2: orphan docs (project_id NULL) — backfill them
    org_user_ids = {
        r.user_id
        for r in db.query(OrgRole).filter(OrgRole.organization_id == org_id).all()
    }
    orphans = (
        db.query(Document)
        .options(
            joinedload(Document.project_rel),
            joinedload(Document.project_version),
            joinedload(Document.topic),
        )
        .filter(
            Document.project_id.is_(None),
            or_(
                Document.owner_id.in_(org_user_ids),
                Document.owner_id.is_(None),
            ),
        )
        .all()
    )

    # Backfill orphans via legacy project string
    backfilled = 0
    for d in orphans:
        if d.project and d.project.lower() in slug_to_project:
            proj = slug_to_project[d.project.lower()]
            d.project_id = proj.id
            backfilled += 1
    if backfilled > 0:
        try:
            db.commit()
            logger.info("Publish: backfilled project_id for %d orphan documents", backfilled)
        except Exception:
            db.rollback()

    # Combine: all docs that belong to this org
    seen_ids: set[int] = set()
    all_docs: list[Document] = []
    for d in docs_with_pid:
        if d.id not in seen_ids:
            seen_ids.add(d.id)
            all_docs.append(d)
    for d in orphans:
        if d.id not in seen_ids and d.project_id and d.project_id in set(project_ids):
            seen_ids.add(d.id)
            all_docs.append(d)

    logger.info("Publish: found %d docs total (%d with FK, %d orphans backfilled)",
                len(all_docs), len(docs_with_pid), backfilled)

    # ----- Publish each doc -----
    published = 0
    skipped = 0
    errors = 0
    content_fetched = 0
    doc_details: list[dict] = []  # diagnostic info per doc

    # Set branding from org for zensical config
    if org and all_docs:
        _set_branding_from_doc(all_docs[0], db)

    for doc in all_docs:
        detail = {"id": doc.id, "title": doc.title or "(untitled)", "status": "unknown"}
        try:
            has_html = bool(doc.content_html)
            has_pub_html = bool(doc.published_content_html)
            has_gdoc_id = bool(doc.google_doc_id)
            detail["has_html"] = has_html
            detail["has_pub_html"] = has_pub_html
            detail["has_gdoc_id"] = has_gdoc_id
            detail["project_id"] = doc.project_id
            detail["project_legacy"] = doc.project

            # If doc has no content, try to fetch from Google Drive
            if not doc.content_html and not doc.published_content_html:
                html = _try_fetch_content_from_drive(doc, google_token)
                if html:
                    doc.content_html = html
                    content_fetched += 1
                    detail["fetched"] = True
                else:
                    logger.warning("No content for doc %s (%s) — skipping", doc.id, doc.title)
                    skipped += 1
                    detail["status"] = "skipped_no_content"
                    doc_details.append(detail)
                    continue

            commit_sha = _publish_to_git(doc, db=db)
            if commit_sha:
                doc.is_published = True
                doc.last_published_at = datetime.now(timezone.utc).isoformat()
                if doc.content_html and not doc.published_content_html:
                    doc.published_content_html = doc.content_html
                published += 1
                detail["status"] = "published"
                detail["commit"] = commit_sha[:8]
            else:
                skipped += 1
                detail["status"] = "skipped_no_changes"
        except Exception as e:
            logger.error("Failed to publish doc %s: %s", doc.id, e)
            errors += 1
            detail["status"] = f"error: {e}"
        doc_details.append(detail)

    logger.info("Publish details: %s", doc_details)

    if published > 0 or content_fetched > 0:
        try:
            db.commit()
        except Exception:
            db.rollback()

    # Always refresh zensical.toml with current branding/nav before deploying,
    # even when no doc content changed (fixes stale nav restriction).
    if org and org.github_repo_full_name:
        try:
            from pathlib import Path as _Path
            from app.publishing import git_publisher as _gp
            _branding = dict(_gp._current_branding) if _gp._current_branding else {}
            if not _branding.get("site_name"):
                _branding["site_name"] = org.name or "Documentation"
            _repo_path = _Path(_settings.docs_repo_path)
            _cfg = write_zensical_toml(_repo_path, **_branding)
            _repo = get_repo()
            _repo.index.add([str(_cfg.relative_to(_repo_path))])
            if _repo.is_dirty():
                _repo.index.commit("Update zensical.toml configuration")
                published += 1
        except Exception as _toml_err:
            logger.warning("Could not refresh zensical.toml before deploy: %s", _toml_err)

    # Build with zensical locally and push the pre-built HTML to gh-pages
    push_ok = False
    push_error: str | None = None
    if org and org.github_repo_full_name and org.github_token_encrypted:
        try:
            import requests as _req
            from pathlib import Path as _Path
            from app.api.github_publish import _set_docs_repo_remote

            token = get_encryption_service().decrypt(org.github_token_encrypted)
            full_name = org.github_repo_full_name
            remote_url_with_token = f"https://oauth2:{token}@github.com/{full_name}.git"

            # Stamp the main-branch remote with the current token so push works
            _set_docs_repo_remote(full_name, token)
            push_branch("main")  # push markdown source (best-effort backup)

            # Build with zensical and push pre-built HTML to gh-pages
            repo_path = _Path(_settings.docs_repo_path)
            deploy_result = deploy_to_gh_pages(repo_path, remote_url_with_token)
            push_ok = deploy_result is True

            if push_ok:
                # Point GitHub Pages at gh-pages / (raw HTML, no Jekyll build needed)
                _req.put(
                    f"https://api.github.com/repos/{full_name}/pages",
                    headers={
                        "Authorization": f"token {token}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28",
                    },
                    json={"source": {"branch": "gh-pages", "path": "/"}},
                    timeout=10,
                )
                logger.info("GitHub Pages now serving from gh-pages branch for %s", full_name)
            else:
                push_error = deploy_result if isinstance(deploy_result, str) else "Docs built locally but push to GitHub Pages failed."
        except Exception as exc:
            logger.warning("Deploy to gh-pages failed: %s", exc)
            push_error = f"Deployment failed: {exc}"
    elif published > 0 and not (org and org.github_repo_full_name):
        push_error = "No GitHub repository configured. Connect GitHub in Settings to publish remotely."

    # Prefer the stored Pages URL; fall back to deriving from username
    pages_url = (org.github_pages_url if org else None) or None

    result: dict = {
        "ok": True,
        "published": published,
        "skipped": skipped,
        "errors": errors,
        "contentFetched": content_fetched,
        "pushed": push_ok,
        "pagesUrl": pages_url,
        "totalDocsFound": len(all_docs),
        "hasGoogleToken": bool(google_token),
        "docDetails": doc_details[:20],  # first 20 for debugging
    }
    if push_error:
        result["pushWarning"] = push_error

    # Include docs-site git state for debugging
    try:
        from pathlib import Path as _P
        import subprocess as _sp
        _rp = _P(_settings.docs_repo_path)
        _log = _sp.run(["git", "log", "--oneline", "-5"], cwd=str(_rp), capture_output=True, text=True, timeout=10)
        _files = _sp.run(["find", "docs", "-name", "*.md"], cwd=str(_rp), capture_output=True, text=True, timeout=10)
        result["_debug"] = {
            "docsRepoPath": str(_rp.resolve()),
            "gitLog": _log.stdout.strip().splitlines(),
            "mdFiles": _files.stdout.strip().splitlines(),
        }
    except Exception as _dbg_e:
        result["_debug"] = {"error": str(_dbg_e)}

    return result
