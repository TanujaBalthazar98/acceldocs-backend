"""Page business logic — slug generation, publish, sync, and record creation."""

import re
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.lib import markdown_import as _markdown_import
from app.lib.slugify import to_slug as slugify, unique_slug as _make_unique
from app.models import Page, Section

# Backward-compatible binding so deploys do not crash if app/lib version lags.
normalize_synced_html = getattr(_markdown_import, "normalize_synced_html", lambda html: html)
clean_google_docs_html = getattr(_markdown_import, "clean_google_docs_html", lambda html: html)

# Strip leading numeric sort-prefixes from Drive doc names ("01 ", "2. ")
_NUM_PREFIX = re.compile(r"^\d+[\.\s]+")


def unique_slug(
    base_value: str,
    org_id: int,
    db: Session,
    exclude_id: int | None = None,
    *,
    strip_numeric_prefix: bool = True,
) -> str:
    """Generate a unique slug within an org, optionally excluding a page by ID."""
    seed = _NUM_PREFIX.sub("", base_value).strip() if strip_numeric_prefix else base_value.strip()
    base = slugify(seed or base_value) or "page"

    def exists(s: str) -> bool:
        q = db.query(Page).filter(Page.organization_id == org_id, Page.slug == s)
        if exclude_id:
            q = q.filter(Page.id != exclude_id)
        return q.first() is not None

    return _make_unique(base, exists)


def apply_publish(db: Session, page: Page) -> None:
    """Snapshot html_content → published_html, set status=published, populate search_text,
    and auto-publish the parent section.  Does NOT commit."""
    page.published_html = page.html_content
    page.is_published = True
    page.status = "published"

    try:
        import bleach
        page.search_text = bleach.clean(page.html_content or "", tags=[], strip=True).strip()
    except Exception:
        page.search_text = None

    if page.section_id:
        section = db.get(Section, page.section_id)
        if section and not section.is_published:
            section.is_published = True


def apply_sync_result(
    db: Session,
    page: Page,
    html: str,
    modified_at: str | None,
    drive_title: str | None,
    org_id: int,
) -> None:
    """Apply a Drive export result to a page — normalize HTML, update title/slug/status.
    Does NOT commit."""
    page.html_content = normalize_synced_html(clean_google_docs_html(html))
    page.drive_modified_at = modified_at
    page.last_synced_at = datetime.now(timezone.utc).isoformat()

    if drive_title:
        clean = _NUM_PREFIX.sub("", drive_title).strip()
        if clean:
            page.title = clean
            if not page.slug_locked and not page.is_published:
                page.slug = unique_slug(clean, org_id, db, exclude_id=page.id)

    # Flag unpublished changes so the old published_html stays live until re-published.
    if page.is_published and html != page.published_html:
        page.status = "draft"


def create_page_record(
    db: Session,
    *,
    org_id: int,
    section_id: int | None,
    google_doc_id: str,
    title: str,
    html_content: str,
    modified_at: str | None,
    display_order: int,
    owner_id: int,
) -> Page:
    """Generate a unique slug and insert a new Page row.  Does NOT commit."""
    slug = unique_slug(title, org_id, db)
    page = Page(
        organization_id=org_id,
        section_id=section_id,
        google_doc_id=google_doc_id,
        title=title,
        slug=slug,
        slug_locked=False,
        html_content=html_content,
        display_order=display_order,
        drive_modified_at=modified_at,
        last_synced_at=datetime.now(timezone.utc).isoformat(),
        owner_id=owner_id,
    )
    db.add(page)
    return page


def create_duplicate_record(
    db: Session,
    *,
    source: Page,
    org_id: int,
    copied_doc_id: str,
    html: str,
    modified_at: str | None,
    drive_title: str | None,
    copy_title: str,
    owner_id: int,
) -> Page:
    """Shift sibling display orders and insert a duplicate Page row.  Does NOT commit."""
    new_title = _NUM_PREFIX.sub("", (drive_title or copy_title)).strip() or copy_title
    new_slug = unique_slug(new_title, org_id, db)
    insert_order = (source.display_order or 0) + 1

    # Shift pages that come after the source to make room.
    following = (
        db.query(Page)
        .filter(
            Page.organization_id == org_id,
            Page.section_id == source.section_id,
            Page.display_order >= insert_order,
            Page.id != source.id,
        )
        .order_by(Page.display_order.desc(), Page.id.desc())
        .all()
    )
    for p in following:
        p.display_order += 1

    duplicate = Page(
        organization_id=org_id,
        section_id=source.section_id,
        google_doc_id=copied_doc_id,
        title=new_title,
        slug=new_slug,
        slug_locked=False,
        html_content=html,
        published_html=None,
        is_published=False,
        status="draft",
        display_order=insert_order,
        drive_modified_at=modified_at,
        last_synced_at=datetime.now(timezone.utc).isoformat(),
        owner_id=owner_id,
    )
    db.add(duplicate)
    return duplicate
