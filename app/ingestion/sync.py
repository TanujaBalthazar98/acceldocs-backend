"""Sync service — orchestrates Drive scanning → DB updates → conversion → publishing.

Pipeline:
  1. Scan Drive folder tree (single BFS pass — files tagged with parent_folder_id)
  2. For each Google Doc, check if it's new or modified
  3. Export HTML, extract metadata/frontmatter
  4. Convert to Markdown
  5. Upsert document record in DB
  6. Publish based on status
"""

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from slugify import slugify
from sqlalchemy.orm import Session

from app.config import settings
from app.conversion.html_to_md import convert_html_to_markdown
from app.ingestion.drive import (
    GOOGLE_DOC_MIME,
    DriveFile,
    _find_parent_folder,
    _get_service,
    build_folder_tree,
    classify_folder,
    export_doc_as_html,
)
from app.ingestion.metadata import extract_frontmatter
from app.models import Document, SyncLog
from app.publishing.git_publisher import (
    publish_to_preview,
    publish_to_production,
    unpublish_from_production,
)

logger = logging.getLogger(__name__)

# Pattern to strip leading numeric prefixes like "2 ", "07 ", "12. " from Drive doc names
_NUMERIC_PREFIX_RE = re.compile(r"^\d+[\.\s]+")


def _clean_title(raw_name: str) -> str:
    """Remove leading numeric sort-order prefixes from Google Drive doc names."""
    return _NUMERIC_PREFIX_RE.sub("", raw_name).strip()


def run_full_sync(db: Session) -> dict:
    """Run a full Drive → DB sync.

    Uses build_folder_tree() which collects all files during BFS with
    parent_folder_id set. No need to re-list folders — iterate tree.files.
    """
    root_id = settings.google_drive_root_folder_id
    if not root_id:
        return {"error": "No root folder ID configured", "synced": 0}

    try:
        service = _get_service()
    except FileNotFoundError as e:
        return {"error": str(e), "synced": 0}

    tree = build_folder_tree(service, root_id, root_name="Documentation")

    created = 0
    updated = 0
    skipped = 0
    errors = 0
    published_preview = 0
    published_prod = 0

    # Iterate files already collected during tree scan (no extra API calls)
    google_docs = [f for f in tree.files if f.mime_type == GOOGLE_DOC_MIME]

    for doc_file in google_docs:
        parent = _find_parent_folder(doc_file, tree)
        if not parent:
            logger.warning(
                "No parent folder for doc %s (%s) — skipping", doc_file.name, doc_file.id
            )
            skipped += 1
            continue

        classification = classify_folder(parent)
        project = classification["project"]
        if not project:
            skipped += 1
            continue

        try:
            result, publish_result = _sync_one_doc(
                db=db,
                service=service,
                doc_file=doc_file,
                project=project,
                version=classification["version"],
                visibility=(classification["visibility"] or "public").lower(),
                section=classification["section"],
            )
            if result == "created":
                created += 1
            elif result == "updated":
                updated += 1
            else:
                skipped += 1

            if publish_result == "preview":
                published_preview += 1
            elif publish_result == "production":
                published_prod += 1
        except Exception:
            logger.exception("Error syncing doc %s (%s)", doc_file.name, doc_file.id)
            errors += 1

    db.commit()

    summary = {
        "synced": created + updated,
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "published_preview": published_preview,
        "published_production": published_prod,
    }
    logger.info("Sync complete: %s", summary)
    return summary


def _sync_one_doc(
    db: Session,
    service,
    doc_file: DriveFile,
    project: str,
    version: str | None,
    visibility: str,
    section: str | None,
) -> tuple[str, str | None]:
    """Sync a single Google Doc to the database.

    Returns (result, publish_result)
    - result: created|updated|skipped
    - publish_result: preview|production|None
    """
    existing = db.query(Document).filter(Document.google_doc_id == doc_file.id).first()

    if existing and existing.drive_modified_at == doc_file.modified_time:
        return "skipped", None

    html = export_doc_as_html(service, doc_file.id)
    meta = extract_frontmatter(html)

    status = (meta.get("status", "draft") or "draft").strip().lower()
    if status not in {"draft", "review", "approved", "rejected"}:
        status = "draft"

    clean_name = _clean_title(doc_file.name)
    # Prefer auto-generated slug from the (cleaned) title. Frontmatter slugs from
    # the old CMS are often wrong (e.g. "release-notes-440" for "Version 4.6.0").
    doc_slug = slugify(clean_name)
    description = meta.get("description")
    tags = meta.get("tags")

    if "visibility" in meta:
        visibility = meta["visibility"]
    visibility = visibility.lower()
    if visibility not in {"public", "internal"}:
        visibility = "public"

    markdown = convert_html_to_markdown(
        html,
        strip_front=True,
        download_images=True,
        images_dir=Path(settings.docs_repo_path) / "docs" / "assets",
        image_base_path="assets",
    )

    now = datetime.now(timezone.utc).isoformat()

    if existing:
        existing.title = clean_name
        existing.slug = doc_slug
        existing.project = project
        existing.version = version or ""
        existing.section = section
        existing.visibility = visibility
        existing.status = status
        existing.is_published = status == "approved"
        existing.description = description
        existing.tags = tags
        existing.drive_modified_at = doc_file.modified_time
        existing.last_synced_at = now

        db.add(SyncLog(document_id=existing.id, action="sync"))
        result = "updated"
        doc = existing
    else:
        doc = Document(
            google_doc_id=doc_file.id,
            title=clean_name,
            slug=doc_slug,
            project=project,
            version=version or "",
            section=section,
            visibility=visibility,
            status=status,
            is_published=(status == "approved"),
            description=description,
            tags=tags,
            drive_modified_at=doc_file.modified_time,
            last_synced_at=now,
        )
        db.add(doc)
        db.flush()
        db.add(SyncLog(document_id=doc.id, action="sync"))
        result = "created"

    publish_result: str | None = None
    ver = version or ""
    if status == "review":
        commit_sha = publish_to_preview(project, ver, section, doc_slug, markdown)
        if commit_sha:
            db.add(
                SyncLog(
                    document_id=doc.id,
                    action="publish_preview",
                    branch="docs-preview",
                    commit_sha=commit_sha,
                )
            )
            publish_result = "preview"
    elif status == "approved":
        commit_sha = publish_to_production(project, ver, section, doc_slug, markdown)
        if commit_sha:
            doc.is_published = True
            doc.last_published_at = now
            db.add(
                SyncLog(
                    document_id=doc.id,
                    action="publish",
                    branch="main",
                    commit_sha=commit_sha,
                )
            )
            publish_result = "production"
    elif status in {"draft", "rejected"}:
        commit_sha = unpublish_from_production(project, ver, section, doc_slug)
        if commit_sha:
            doc.is_published = False
            doc.last_published_at = None
            db.add(
                SyncLog(
                    document_id=doc.id,
                    action="unpublish",
                    branch="main",
                    commit_sha=commit_sha,
                )
            )

    return result, publish_result
