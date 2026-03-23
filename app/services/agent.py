"""AI documentation agent — Jira ticket to Docspeare page.

Connects to Jira to fetch ticket context, gathers existing published docs,
calls Claude to generate a documentation draft, creates a Google Doc + Page.
"""

import logging
import re
from html.parser import HTMLParser

import httpx
from googleapiclient.discovery import build
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    JiraCredential,
    Organization,
    OrgRole,
    Page,
    Section,
    User,
)
from app.services.encryption import get_encryption_service

logger = logging.getLogger(__name__)

GOOGLE_DOC_MIME = "application/vnd.google-apps.document"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_user(user: User | None) -> User:
    if not user:
        raise ValueError("Authentication required")
    return user


def _resolve_org_id(body: dict, user: User, db: Session) -> int:
    raw = body.get("_x_org_id") or body.get("organization_id")
    if raw:
        return int(raw)
    role = db.query(OrgRole).filter(OrgRole.user_id == user.id).first()
    if role:
        return role.organization_id
    raise ValueError("No organization found for user")


def _require_editor(user: User, org_id: int, db: Session) -> None:
    role = db.query(OrgRole).filter(
        OrgRole.user_id == user.id, OrgRole.organization_id == org_id
    ).first()
    if not role or role.role not in ("owner", "admin", "editor"):
        raise ValueError("Editor role or higher required")


class _HTMLTextExtractor(HTMLParser):
    """Strip HTML tags, keep text content."""

    def __init__(self):
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str):
        self._parts.append(data)

    def get_text(self) -> str:
        return " ".join(self._parts)


def _html_to_text(html: str) -> str:
    extractor = _HTMLTextExtractor()
    extractor.feed(html or "")
    return extractor.get_text().strip()


def _adf_to_text(node: dict | None) -> str:
    """Convert Jira Atlassian Document Format to plain text."""
    if not node:
        return ""
    t = node.get("type", "")
    if t == "text":
        return node.get("text", "")
    children = node.get("content") or []
    text = " ".join(_adf_to_text(c) for c in children)
    if t in ("paragraph", "heading", "bulletList", "orderedList", "listItem", "blockquote"):
        return text.strip() + "\n"
    return text


def _unique_slug(title: str, org_id: int, db: Session) -> str:
    from slugify import slugify

    base = slugify(title, max_length=200) or "untitled"
    slug = base
    n = 1
    while db.query(Page).filter(Page.organization_id == org_id, Page.slug == slug).first():
        slug = f"{base}-{n}"
        n += 1
    return slug


# ---------------------------------------------------------------------------
# Jira functions
# ---------------------------------------------------------------------------

async def jira_connect(body: dict, db: Session, user: User | None) -> dict:
    """Validate and store Jira credentials."""
    user = _require_user(user)
    org_id = _resolve_org_id(body, user, db)
    _require_editor(user, org_id, db)

    domain = (body.get("domain") or "").strip()
    email = (body.get("email") or "").strip()
    api_token = (body.get("api_token") or "").strip()

    if not domain or not email or not api_token:
        return {"ok": False, "error": "domain, email, and api_token are required"}

    # Strip protocol if pasted as URL
    domain = re.sub(r"^https?://", "", domain).rstrip("/")

    # Validate credentials against Jira
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"https://{domain}/rest/api/3/myself",
            auth=(email, api_token),
        )
    if resp.status_code != 200:
        return {"ok": False, "error": f"Jira authentication failed (HTTP {resp.status_code})"}

    # Encrypt and store
    enc = get_encryption_service()
    encrypted = enc.encrypt(api_token)

    existing = db.query(JiraCredential).filter(
        JiraCredential.user_id == user.id,
        JiraCredential.organization_id == org_id,
    ).first()

    if existing:
        existing.jira_domain = domain
        existing.jira_email = email
        existing.encrypted_api_token = encrypted
    else:
        db.add(JiraCredential(
            user_id=user.id,
            organization_id=org_id,
            jira_domain=domain,
            jira_email=email,
            encrypted_api_token=encrypted,
        ))

    db.commit()
    return {"ok": True, "domain": domain, "email": email}


async def jira_disconnect(body: dict, db: Session, user: User | None) -> dict:
    """Remove stored Jira credentials."""
    user = _require_user(user)
    org_id = _resolve_org_id(body, user, db)

    deleted = db.query(JiraCredential).filter(
        JiraCredential.user_id == user.id,
        JiraCredential.organization_id == org_id,
    ).delete()
    db.commit()
    return {"ok": True, "deleted": deleted > 0}


async def jira_status(body: dict, db: Session, user: User | None) -> dict:
    """Check if Jira is connected for the current user."""
    user = _require_user(user)
    org_id = _resolve_org_id(body, user, db)

    cred = db.query(JiraCredential).filter(
        JiraCredential.user_id == user.id,
        JiraCredential.organization_id == org_id,
    ).first()

    if cred:
        return {"ok": True, "connected": True, "domain": cred.jira_domain, "email": cred.jira_email}
    return {"ok": True, "connected": False}


async def jira_get_ticket(body: dict, db: Session, user: User | None) -> dict:
    """Fetch a Jira ticket by key."""
    user = _require_user(user)
    org_id = _resolve_org_id(body, user, db)

    ticket_key = (body.get("ticket_key") or "").strip().upper()
    if not ticket_key:
        return {"ok": False, "error": "ticket_key is required"}

    cred = db.query(JiraCredential).filter(
        JiraCredential.user_id == user.id,
        JiraCredential.organization_id == org_id,
    ).first()
    if not cred:
        return {"ok": False, "error": "Jira not connected. Set up Jira credentials first."}

    enc = get_encryption_service()
    token = enc.decrypt(cred.encrypted_api_token)

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"https://{cred.jira_domain}/rest/api/3/issue/{ticket_key}",
            auth=(cred.jira_email, token),
            params={"fields": "summary,description,issuetype,status,labels"},
        )

    if resp.status_code == 404:
        return {"ok": False, "error": f"Ticket {ticket_key} not found"}
    if resp.status_code != 200:
        return {"ok": False, "error": f"Jira API error (HTTP {resp.status_code})"}

    data = resp.json()
    fields = data.get("fields", {})

    description_adf = fields.get("description")
    description_text = _adf_to_text(description_adf).strip() if description_adf else ""

    return {
        "ok": True,
        "key": data.get("key", ticket_key),
        "summary": fields.get("summary", ""),
        "description_text": description_text,
        "status": (fields.get("status") or {}).get("name", ""),
        "issue_type": (fields.get("issuetype") or {}).get("name", ""),
        "labels": fields.get("labels", []),
    }


# ---------------------------------------------------------------------------
# AI generation
# ---------------------------------------------------------------------------

async def agent_generate_doc(body: dict, db: Session, user: User | None) -> dict:
    """Generate a documentation draft from a Jira ticket using Claude."""
    user = _require_user(user)
    org_id = _resolve_org_id(body, user, db)
    _require_editor(user, org_id, db)

    if not settings.anthropic_api_key:
        return {"ok": False, "error": "Anthropic API key not configured"}

    ticket_key = (body.get("ticket_key") or "").strip().upper()
    section_id = body.get("section_id")
    title_override = (body.get("title_override") or "").strip()

    if not ticket_key:
        return {"ok": False, "error": "ticket_key is required"}

    # 1. Fetch ticket from Jira
    ticket_result = await jira_get_ticket(body, db, user)
    if not ticket_result.get("ok"):
        return ticket_result

    # 2. Gather relevant published pages as context (BM25-ranked)
    org = db.get(Organization, org_id)
    org_name = org.name if org else "the organization"

    search_query = f"{ticket_result['summary']} {ticket_result.get('description_text', '')[:200]}"
    try:
        from app.services.search import search_pages_bm25
        bm25_results = search_pages_bm25(org_id, search_query, db, limit=10)
        context_docs = ""
        for r in bm25_results:
            page = db.get(Page, r["id"])
            if page:
                text = _html_to_text(page.published_html or "")
                if text:
                    context_docs += f"\n--- {page.title} (relevance: {r['score']}) ---\n{text[:2000]}\n"
    except Exception:
        # Fallback to recent pages if BM25 fails
        fallback_query = db.query(Page).filter(
            Page.organization_id == org_id,
            Page.is_published == True,  # noqa: E712
            Page.published_html.isnot(None),
        )
        if section_id:
            fallback_query = fallback_query.filter(Page.section_id == int(section_id))
        context_pages = fallback_query.order_by(Page.updated_at.desc()).limit(10).all()
        context_docs = ""
        for p in context_pages:
            text = _html_to_text(p.published_html or "")
            if text:
                context_docs += f"\n--- {p.title} ---\n{text[:2000]}\n"

    # 3. Call Claude
    import anthropic

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    system_prompt = (
        f"You are a technical documentation writer for {org_name}. "
        "Write clear, concise product documentation in Markdown. "
        "Follow the style and structure of the existing documentation provided below. "
        "Start directly with the content — no preamble or meta-commentary."
    )
    if context_docs:
        system_prompt += f"\n\nExisting documentation for style reference:\n{context_docs}"

    user_prompt = (
        f"Write a documentation page for this feature.\n\n"
        f"Ticket: [{ticket_result['key']}] {ticket_result['summary']}\n"
        f"Type: {ticket_result['issue_type']}\n"
        f"Status: {ticket_result['status']}\n\n"
        f"Description:\n{ticket_result['description_text']}\n\n"
        "Generate a complete documentation page with:\n"
        "1. A clear title (H1)\n"
        "2. A brief overview paragraph\n"
        "3. Key concepts or prerequisites (if applicable)\n"
        "4. Step-by-step instructions or feature details\n"
        "5. Any relevant notes or limitations"
    )

    try:
        response = client.messages.create(
            model="claude-sonnet-4-5-20250514",
            max_tokens=4096,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        generated_content = response.content[0].text
    except Exception as exc:
        logger.error("Claude API call failed: %s", exc)
        return {"ok": False, "error": f"AI generation failed: {exc}"}

    # 4. Create Google Doc and write content
    title = title_override or ticket_result["summary"] or f"Documentation for {ticket_key}"

    try:
        from app.api.drive import get_drive_credentials, _create_drive_doc

        creds = await get_drive_credentials(user, org_id, db)
        drive_service = build("drive", "v3", credentials=creds, cache_discovery=False)

        # Determine parent folder
        parent_drive_id = None
        if section_id:
            sec = db.get(Section, int(section_id))
            parent_drive_id = sec.drive_folder_id if sec else None
        if not parent_drive_id and org:
            parent_drive_id = org.drive_folder_id

        doc_id = _create_drive_doc(drive_service, title, parent_drive_id)

        # Write content into the Google Doc
        docs_service = build("docs", "v1", credentials=creds, cache_discovery=False)
        docs_service.documents().batchUpdate(
            documentId=doc_id,
            body={"requests": [{"insertText": {"location": {"index": 1}, "text": generated_content}}]},
        ).execute()

    except Exception as exc:
        logger.error("Drive doc creation failed: %s", exc)
        return {"ok": False, "error": f"Failed to create Google Doc: {exc}"}

    # 5. Create Page record
    slug = _unique_slug(title, org_id, db)
    section_id_int = int(section_id) if section_id else None

    page = Page(
        organization_id=org_id,
        section_id=section_id_int,
        google_doc_id=doc_id,
        title=title,
        slug=slug,
        slug_locked=False,
        status="draft",
        is_published=False,
        display_order=0,
        owner_id=user.id,
    )
    db.add(page)
    db.commit()
    db.refresh(page)

    # 6. Sync HTML from the newly created doc
    try:
        raw = drive_service.files().export(fileId=doc_id, mimeType="text/html").execute()
        html = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        page.html_content = html
        meta = drive_service.files().get(
            fileId=doc_id, fields="modifiedTime", supportsAllDrives=True
        ).execute()
        page.drive_modified_at = meta.get("modifiedTime")
        from datetime import datetime, timezone
        page.last_synced_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(page)
    except Exception as exc:
        logger.warning("Post-creation sync failed for page %d: %s", page.id, exc)

    return {
        "ok": True,
        "page_id": page.id,
        "title": page.title,
        "slug": page.slug,
        "google_doc_id": doc_id,
        "preview_html": page.html_content or "",
    }


# ---------------------------------------------------------------------------
# Template page creation
# ---------------------------------------------------------------------------

async def create_template_page(body: dict, db: Session, user: User | None) -> dict:
    """Create a new page pre-filled with a template's markdown content.

    Body fields:
      - title (str, required): Page title
      - content (str, required): Markdown content from the template
      - section_id (int, optional): Section to place the page in
    """
    user = _require_user(user)
    org_id = _resolve_org_id(body, user, db)
    _require_editor(user, org_id, db)

    title = (body.get("title") or "").strip()
    content = (body.get("content") or "").strip()
    section_id = body.get("section_id")

    if not title:
        return {"ok": False, "error": "title is required"}
    if not content:
        return {"ok": False, "error": "content is required"}

    # Resolve Drive folder
    from app.models import Organization, Section
    org = db.get(Organization, org_id)

    parent_drive_id = None
    if section_id:
        sec = db.get(Section, int(section_id))
        if sec and sec.organization_id == org_id:
            parent_drive_id = sec.drive_folder_id
    if not parent_drive_id and org:
        parent_drive_id = org.drive_folder_id

    # Create Google Doc with template content
    try:
        from app.api.drive import get_drive_credentials, _create_drive_doc
        from googleapiclient.discovery import build

        creds = await get_drive_credentials(user, org_id, db)
        drive_service = build("drive", "v3", credentials=creds, cache_discovery=False)
        doc_id = _create_drive_doc(drive_service, title, parent_drive_id)

        docs_service = build("docs", "v1", credentials=creds, cache_discovery=False)
        docs_service.documents().batchUpdate(
            documentId=doc_id,
            body={"requests": [{"insertText": {"location": {"index": 1}, "text": content}}]},
        ).execute()
    except Exception as exc:
        logger.error("Drive doc creation failed: %s", exc)
        return {"ok": False, "error": f"Failed to create Google Doc: {exc}"}

    # Create Page record
    slug = _unique_slug(title, org_id, db)
    page = Page(
        organization_id=org_id,
        section_id=int(section_id) if section_id else None,
        google_doc_id=doc_id,
        title=title,
        slug=slug,
        slug_locked=False,
        status="draft",
        is_published=False,
        display_order=0,
        owner_id=user.id,
    )
    db.add(page)
    db.commit()
    db.refresh(page)

    # Sync HTML from the new doc
    try:
        raw = drive_service.files().export(fileId=doc_id, mimeType="text/html").execute()
        html = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        page.html_content = html
        from datetime import datetime, timezone
        page.last_synced_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(page)
    except Exception as exc:
        logger.warning("Post-creation sync failed for template page %d: %s", page.id, exc)

    return {
        "ok": True,
        "page_id": page.id,
        "title": page.title,
        "slug": page.slug,
        "google_doc_id": doc_id,
    }
