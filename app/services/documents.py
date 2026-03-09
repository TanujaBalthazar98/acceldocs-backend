"""Document management and cache functions."""

import logging
from datetime import datetime, timezone

from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from app.models import User, Document, DocumentCache, Organization, Project, ProjectVersion, Topic

logger = logging.getLogger(__name__)


def _int(val) -> int | None:
    """Safely cast a value to int for PostgreSQL type safety."""
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def _resolve_publish_path(doc: Document) -> tuple[str, str, str | None, str]:
    """Resolve a document's publish path from FK relationships, falling back to string fields.

    Walks the FK chain:  doc → project_rel → slug,  doc → project_version → slug,
    doc → topic → (walk parent chain for nested section path).

    Returns:
        (project_slug, version_slug, section_path_or_none, doc_slug)
    """
    # --- project slug ---
    project_slug = "default"
    if doc.project_rel:
        # If the project is a sub-project, build path: parent-slug/child-slug
        parts: list[str] = []
        p: Project | None = doc.project_rel
        while p is not None:
            parts.append(p.slug or p.name.lower().replace(" ", "-"))
            p = p.parent
        parts.reverse()
        project_slug = "/".join(parts)
    elif doc.project:
        # Legacy string field
        project_slug = doc.project

    # --- version slug ---
    version_slug = ""
    if doc.project_version:
        version_slug = doc.project_version.slug or doc.project_version.name
    elif doc.version:
        version_slug = doc.version

    # --- section (topic hierarchy) ---
    section: str | None = None
    if doc.topic:
        topic_parts: list[str] = []
        t: Topic | None = doc.topic
        while t is not None:
            topic_parts.append(t.slug or t.name.lower().replace(" ", "-"))
            t = t.parent
        topic_parts.reverse()
        section = "/".join(topic_parts)
    elif doc.section:
        section = doc.section

    # --- doc slug ---
    doc_slug = doc.slug or f"doc-{doc.id}"

    return project_slug, version_slug, section, doc_slug


def _serialize_document(d: Document, include_content: bool = False) -> dict:
    """Serialize a Document model to dict with owner info."""
    owner_data = None
    if d.owner:
        owner_data = {
            "id": d.owner.id,
            "full_name": d.owner.name,
            "email": d.owner.email,
        }

    result = {
        "id": d.id,
        "google_doc_id": d.google_doc_id,
        "title": d.title,
        "slug": d.slug,
        "project": d.project,
        "version": d.version,
        "section": d.section,
        "visibility": d.visibility,
        "status": d.status,
        "description": d.description,
        "tags": d.tags,
        "project_id": d.project_id,
        "project_version_id": d.project_version_id,
        "topic_id": d.topic_id,
        "owner_id": d.owner_id,
        "owner": owner_data,
        "is_published": d.is_published,
        "content_id": d.content_id,
        "published_content_id": d.published_content_id,
        "video_url": d.video_url,
        "video_title": d.video_title,
        "display_order": d.display_order,
        "google_modified_at": d.google_modified_at,
        "drive_modified_at": d.drive_modified_at,
        "last_synced_at": d.last_synced_at,
        "last_published_at": d.last_published_at,
        "created_at": d.created_at.isoformat() if d.created_at else None,
        "updated_at": d.updated_at.isoformat() if d.updated_at else None,
    }

    if include_content:
        result["content_html"] = d.content_html
        result["published_content_html"] = d.published_content_html

    return result


def _fetch_html_from_drive(doc: Document) -> str | None:
    """Fetch the latest HTML content for a document from Google Drive."""
    try:
        from app.ingestion.drive import _get_service, export_doc_as_html
        service = _get_service()
        return export_doc_as_html(service, doc.google_doc_id)
    except Exception:
        logger.warning("Could not fetch content from Drive for doc %s (%s)", doc.id, doc.title)
        return None


def _set_branding_from_doc(doc: Document, db) -> None:
    """Load org branding into the git publisher so zensical.toml reflects it."""
    try:
        from app.publishing import git_publisher
        org = None
        if doc.project_id:
            proj = db.get(Project, doc.project_id) if db else None
            if proj and proj.organization_id:
                org = db.get(Organization, proj.organization_id)
        if org:
            git_publisher._current_branding = {
                "site_name": org.name or "Documentation",
                "site_description": org.tagline or "",
                "primary_color": org.primary_color or None,
                "logo_url": org.logo_url or None,
                "font_heading": org.font_heading or None,
                "font_body": org.font_body or None,
                "custom_css": org.custom_css or None,
            }
    except Exception:
        pass  # non-fatal — publish with defaults


def _get_org_for_doc(doc: Document, db) -> Organization | None:
    """Return the Organization for a document's project, or None."""
    if not db or not doc.project_id:
        return None
    proj = db.get(Project, doc.project_id)
    if proj and proj.organization_id:
        return db.get(Organization, proj.organization_id)
    return None


def _auto_deploy_if_public(doc: Document, db) -> None:
    """If the document's project is public and the org has GitHub configured,
    build with zensical and push the pre-built site to gh-pages automatically."""
    try:
        proj = db.get(Project, doc.project_id) if doc.project_id else None
        if not proj or proj.visibility != "public":
            return

        org = db.get(Organization, proj.organization_id) if proj.organization_id else None
        if not org or not org.github_repo_full_name or not org.github_token_encrypted:
            return

        from app.services.encryption import get_encryption_service
        from app.publishing.git_publisher import deploy_to_gh_pages
        from app.config import settings
        from pathlib import Path

        token = get_encryption_service().decrypt(org.github_token_encrypted)
        remote_url = f"https://oauth2:{token}@github.com/{org.github_repo_full_name}.git"
        repo_path = Path(settings.docs_repo_path)

        ok = deploy_to_gh_pages(repo_path, remote_url)
        if ok:
            logger.info("Auto-deployed gh-pages for org %s after publishing doc %s", org.id, doc.id)
        else:
            logger.warning("Auto-deploy gh-pages failed for doc %s", doc.id)
    except Exception:
        logger.exception("Auto-deploy gh-pages raised for doc %s", doc.id)


def _publish_to_git(doc: Document, db=None) -> str | None:
    """Convert document HTML to Markdown and commit to Git production branch.

    If the project is public and GitHub is configured, also builds the Zensical
    site and pushes the pre-built HTML to the gh-pages branch automatically.
    """
    try:
        from app.conversion.html_to_md import convert_html_to_markdown
        from app.publishing.git_publisher import publish_to_production

        _set_branding_from_doc(doc, db)

        html = doc.published_content_html or doc.content_html
        if not html:
            html = _fetch_html_from_drive(doc)
        if not html:
            logger.warning("No HTML content to publish for doc %s (%s)", doc.id, doc.title)
            return None

        markdown = convert_html_to_markdown(html)
        if not markdown.strip():
            logger.warning("Empty markdown after conversion for doc %s", doc.id)
            return None

        project_slug, version_slug, section, doc_slug = _resolve_publish_path(doc)

        commit_sha = publish_to_production(project_slug, version_slug, section, doc_slug, markdown)
        if commit_sha:
            logger.info("Published doc %s (%s) to Git: %s", doc.id, doc.title, commit_sha[:8])
            # Auto-deploy to GitHub Pages for public projects
            if db:
                _auto_deploy_if_public(doc, db)
        return commit_sha
    except Exception:
        logger.exception("Failed to publish doc %s to Git", doc.id)
        return None


def _unpublish_from_git(doc: Document, db=None) -> str | None:
    """Remove document from Git production branch and rebuild public site if needed."""
    try:
        from app.publishing.git_publisher import unpublish_from_production

        project_slug, version_slug, section, doc_slug = _resolve_publish_path(doc)

        commit_sha = unpublish_from_production(project_slug, version_slug, section, doc_slug)
        if commit_sha:
            logger.info("Unpublished doc %s (%s) from Git: %s", doc.id, doc.title, commit_sha[:8])
            if db:
                _auto_deploy_if_public(doc, db)
        return commit_sha
    except Exception:
        logger.exception("Failed to unpublish doc %s from Git", doc.id)
        return None


async def list_documents(body: dict, db: Session, user: User | None) -> dict:
    """Get all documents for projects.

    Returns documents that belong to the given project IDs (by FK or legacy
    string match).  Only returns documents within the user's own organization
    to prevent cross-account data leaks.
    """
    if not user:
        return {"ok": False, "error": "Authentication required"}

    try:
        raw_ids = body.get("projectIds", [])
        project_ids = [_int(pid) for pid in raw_ids if _int(pid) is not None]
        logger.info("[list_documents] user=%s raw_ids=%s project_ids=%s",
                     user.id, raw_ids[:5], project_ids[:5])
        if not project_ids:
            logger.warning("[list_documents] No valid project IDs after casting")
            return {"ok": True, "documents": []}

        # ----- Step 1: resolve org-scoped project info -----
        from app.models import OrgRole
        org_roles = db.query(OrgRole).filter(OrgRole.user_id == user.id).all()
        user_org_ids = {r.organization_id for r in org_roles}
        logger.info("[list_documents] user_org_ids=%s", sorted(user_org_ids))
        if not user_org_ids:
            logger.warning("[list_documents] user has no org memberships")
            return {"ok": True, "documents": []}

        # Only allow project IDs that belong to one of the user's organizations
        projects = db.query(Project).filter(Project.id.in_(project_ids)).all()
        logger.info("[list_documents] projects from DB: %s",
                     [(p.id, p.name, p.organization_id) for p in projects])
        projects = [p for p in projects if p.organization_id in user_org_ids]
        scoped_project_ids = [p.id for p in projects]
        logger.info("[list_documents] scoped_project_ids=%s", scoped_project_ids)

        if not scoped_project_ids:
            logger.warning("[list_documents] No scoped projects — returning empty")
            return {"ok": True, "documents": []}

        # ----- Step 2: query documents by FK -----
        documents = db.query(Document).options(
            joinedload(Document.owner)
        ).filter(
            Document.project_id.in_(scoped_project_ids)
        ).order_by(Document.display_order, Document.id).all()
        logger.info("[list_documents] FK query returned %d docs", len(documents))

        # ----- Step 3: Also get ALL documents in the org (by project FK) -----
        # Some docs may have project_id pointing to projects not in the
        # requested list (e.g. stale sub-projects). Count total for debug.
        all_org_docs = db.query(Document).filter(
            Document.project_id.in_(scoped_project_ids)
        ).count()

        # Count total documents in the entire DB for diagnostics
        total_docs_in_db = db.query(Document).count()
        null_pid_docs = db.query(Document).filter(Document.project_id.is_(None)).count()
        logger.info("[list_documents] total_docs_in_db=%d null_project_id=%d org_docs=%d",
                     total_docs_in_db, null_pid_docs, all_org_docs)

        # ----- Step 4: find orphan docs (project_id is NULL) that can be
        #       backfilled into scoped projects -----
        slug_to_id: dict[str, int] = {}
        for p in projects:
            if p.slug:
                slug_to_id[p.slug.lower()] = p.id
            if p.name:
                slug_to_id[p.name.lower()] = p.id
                slug_to_id[p.name.lower().replace(" ", "-")] = p.id
        logger.info("[list_documents] slug_to_id keys=%s", list(slug_to_id.keys()))

        # Scope orphan query to users in the same org(s) to prevent cross-account leaks
        from app.models import OrgRole as _OrgRole
        org_user_ids = {
            r.user_id
            for r in db.query(_OrgRole).filter(_OrgRole.organization_id.in_(user_org_ids)).all()
        }

        # Match orphans owned by any org member OR unowned (synced docs have
        # owner_id=NULL).  SQL IN does not match NULL, so we need an explicit OR.
        orphans = db.query(Document).options(
            joinedload(Document.owner)
        ).filter(
            Document.project_id.is_(None),
            or_(
                Document.owner_id.in_(org_user_ids),
                Document.owner_id.is_(None),
            ),
        ).all()
        logger.info("[list_documents] orphans found: %d — legacy strings: %s",
                     len(orphans),
                     [(d.id, d.title, d.project, d.owner_id) for d in orphans[:10]])

        already_in_docs = {d.id for d in documents}
        backfilled = 0
        for d in orphans:
            if d.id in already_in_docs:
                continue
            if d.project and d.project.lower() in slug_to_id:
                # Backfill via legacy project string → set FK and include
                d.project_id = slug_to_id[d.project.lower()]
                backfilled += 1
                documents.append(d)
            elif d.owner_id == user.id:
                # Always surface orphans owned by the current user even without a project
                documents.append(d)
        if backfilled > 0:
            try:
                db.commit()
                logger.info("Backfilled project_id for %d orphan documents", backfilled)
            except Exception:
                db.rollback()

        # Deduplicate (in case an orphan was already picked up by FK query)
        seen_ids: set[int] = set()
        unique_docs = []
        for d in documents:
            if d.id not in seen_ids:
                seen_ids.add(d.id)
                unique_docs.append(d)

        doc_list = [_serialize_document(d) for d in unique_docs]
        logger.info("[list_documents] returning %d documents", len(doc_list))
        return {"ok": True, "documents": doc_list}

    except Exception as e:
        logger.exception("list_documents failed")
        return {"ok": False, "error": str(e)}


async def create_document(body: dict, db: Session, user: User | None) -> dict:
    """Create new document/page."""
    if not user:
        return {"ok": False, "error": "Authentication required"}

    try:
        google_doc_id = body.get("googleDocId") or body.get("google_doc_id", "")
        title = body.get("title", "New Document")
        project = body.get("project", "")
        version = body.get("version", "v1.0")

        if not google_doc_id:
            return {"ok": False, "error": "Google Doc ID required"}

        project_id = _int(body.get("projectId") or body.get("project_id"))
        project_version_id = _int(body.get("projectVersionId") or body.get("project_version_id"))

        # If no version specified, resolve the project's default version
        if project_id and not project_version_id:
            default_ver = db.query(ProjectVersion).filter(
                ProjectVersion.project_id == project_id,
                ProjectVersion.is_default == True,
            ).first()
            if default_ver:
                project_version_id = default_ver.id

        topic_id = _int(body.get("topicId") or body.get("topic_id"))

        # Resolve legacy string fields from FK relationships so the publish
        # pipeline can build correct file paths even if the caller only
        # provides the relational IDs (e.g. the onboarding import).
        resolved_project = project
        resolved_version = version
        resolved_section = body.get("section")

        if project_id and not resolved_project:
            proj = db.get(Project, project_id)
            if proj:
                # Walk up the parent chain for sub-projects
                parts: list[str] = []
                p: Project | None = proj
                while p is not None:
                    parts.append(p.slug or p.name.lower().replace(" ", "-"))
                    p = p.parent
                parts.reverse()
                resolved_project = "/".join(parts)

        if project_version_id and not resolved_version:
            pv = db.get(ProjectVersion, project_version_id)
            if pv:
                resolved_version = pv.slug or pv.name

        if topic_id and not resolved_section:
            topic = db.get(Topic, topic_id)
            if topic:
                topic_parts: list[str] = []
                t: Topic | None = topic
                while t is not None:
                    topic_parts.append(t.slug or t.name.lower().replace(" ", "-"))
                    t = t.parent
                topic_parts.reverse()
                resolved_section = "/".join(topic_parts)

        document = Document(
            google_doc_id=google_doc_id,
            title=title,
            slug=body.get("slug", title.lower().replace(" ", "-")),
            project=resolved_project,
            version=resolved_version,
            section=resolved_section,
            visibility=body.get("visibility", "public"),
            status=body.get("status", "draft"),
            description=body.get("description"),
            tags=body.get("tags"),
            project_id=project_id,
            project_version_id=project_version_id,
            topic_id=topic_id,
            owner_id=user.id,
            display_order=body.get("displayOrder", body.get("display_order", 0)),
        )
        db.add(document)
        db.commit()

        return {
            "ok": True,
            "documentId": document.id,
            "document": {
                "id": document.id,
                "google_doc_id": document.google_doc_id,
                "title": document.title,
                "slug": document.slug,
            }
        }

    except Exception as e:
        db.rollback()
        return {"ok": False, "error": str(e)}


async def get_document(body: dict, db: Session, user: User | None) -> dict:
    """Fetch single document."""
    if not user:
        return {"ok": False, "error": "Authentication required"}

    try:
        doc_id = _int(body.get("id") or body.get("documentId"))
        if not doc_id:
            return {"ok": False, "error": "Document ID required"}

        document = db.query(Document).options(
            joinedload(Document.owner)
        ).filter(Document.id == doc_id).first()
        if not document:
            return {"ok": False, "error": "Document not found"}

        return {
            "ok": True,
            "document": _serialize_document(document, include_content=True),
        }

    except Exception as e:
        return {"ok": False, "error": str(e)}


async def update_document(body: dict, db: Session, user: User | None) -> dict:
    """Modify document (content, metadata, publish status)."""
    if not user:
        return {"ok": False, "error": "Authentication required"}

    try:
        doc_id = _int(body.get("id") or body.get("documentId"))
        if not doc_id:
            return {"ok": False, "error": "Document ID required"}

        document = db.query(Document).options(
            joinedload(Document.owner)
        ).filter(Document.id == doc_id).first()
        if not document:
            return {"ok": False, "error": "Document not found"}

        # Frontend sends { documentId, data: { ...fields } }
        update_data = body.get("data", body)

        # Track if publish state is changing
        was_published = document.is_published
        will_publish = update_data.get("is_published", was_published)

        # Update fields
        updatable_fields = [
            "title", "slug", "project", "version", "section", "visibility", "status",
            "description", "tags", "project_id", "project_version_id", "topic_id",
            "is_published", "content_html", "published_content_html", "content_id",
            "published_content_id", "video_url", "video_title", "display_order",
            "google_modified_at", "drive_modified_at", "last_synced_at", "last_published_at"
        ]
        for field in updatable_fields:
            if field in update_data:
                setattr(document, field, update_data[field])

        # Trigger Git publishing pipeline when publish state changes
        if will_publish and not was_published:
            _publish_to_git(document, db=db)
            document.last_published_at = datetime.now(timezone.utc)
        elif not will_publish and was_published:
            _unpublish_from_git(document, db=db)

        db.commit()

        return {
            "ok": True,
            "document": _serialize_document(document, include_content=True),
        }

    except Exception as e:
        db.rollback()
        return {"ok": False, "error": str(e)}


async def delete_document(body: dict, db: Session, user: User | None) -> dict:
    """Delete document."""
    if not user:
        return {"ok": False, "error": "Authentication required"}

    try:
        doc_id = _int(body.get("id") or body.get("documentId"))
        if not doc_id:
            return {"ok": False, "error": "Document ID required"}

        document = db.query(Document).filter(Document.id == doc_id).first()
        if not document:
            return {"ok": False, "error": "Document not found"}

        db.delete(document)
        db.commit()

        return {"ok": True}

    except Exception as e:
        db.rollback()
        return {"ok": False, "error": str(e)}


async def document_cache(body: dict, db: Session, user: User | None) -> dict:
    """Get/set cached HTML content."""
    if not user:
        return {"ok": False, "error": "Authentication required"}

    try:
        doc_id = _int(body.get("documentId"))
        action = body.get("action", "get")  # get or set

        if not doc_id:
            return {"ok": False, "error": "Document ID required"}

        if action == "set":
            # Set cache
            cache = db.query(DocumentCache).filter(DocumentCache.document_id == doc_id).first()
            if not cache:
                # Find org_id from user
                from app.models import OrgRole
                org_role = db.query(OrgRole).filter(OrgRole.user_id == user.id).first()
                org_id = org_role.organization_id if org_role else 1

                cache = DocumentCache(
                    document_id=doc_id,
                    organization_id=org_id,
                )
                db.add(cache)

            cache.content_html_encrypted = body.get("content_html")
            cache.content_text_encrypted = body.get("content_text")
            cache.headings_encrypted = body.get("headings")
            cache.published_content_html_encrypted = body.get("published_content_html")
            db.commit()

            return {"ok": True}

        else:
            # Get cache
            cache = db.query(DocumentCache).filter(DocumentCache.document_id == doc_id).first()
            if not cache:
                return {"ok": True, "cache": None}

            return {
                "ok": True,
                "cache": {
                    "content_html": cache.content_html_encrypted,
                    "content_text": cache.content_text_encrypted,
                    "headings": cache.headings_encrypted,
                    "published_content_html": cache.published_content_html_encrypted,
                }
            }

    except Exception as e:
        db.rollback()
        return {"ok": False, "error": str(e)}


async def docs_ai_assistant(body: dict, db: Session, user: User | None) -> dict:
    """AI documentation assistant (placeholder)."""
    # Placeholder - would integrate with AI service
    return {
        "ok": True,
        "message": "AI assistant not yet configured",
        "response": "The AI documentation assistant feature is coming soon."
    }
