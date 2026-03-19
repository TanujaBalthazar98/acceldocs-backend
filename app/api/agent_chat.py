"""Conversational documentation agent — streaming chat with tool use.

Supports LLM providers:
  - gemini (default) — Google Gemini Flash, generous free tier
  - groq — Groq Cloud, generous free tier (Llama 3.3 70B)
  - anthropic — Claude Sonnet, higher quality, paid
  - openai_compat — any OpenAI-compatible endpoint (Ollama, vLLM, etc.)
"""

import json
import logging
from datetime import datetime, timezone
from typing import AsyncGenerator

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

try:
    from sse_starlette.sse import EventSourceResponse
except ImportError:
    EventSourceResponse = None  # type: ignore[assignment]

from app.auth.routes import get_current_user
from app.config import settings
from app.database import get_db
from app.models import Organization, OrgRole, Page, Section, User
from app.services.agent import _html_to_text, _unique_slug, jira_get_ticket

logger = logging.getLogger(__name__)
router = APIRouter()

GOOGLE_DOC_MIME = "application/vnd.google-apps.document"


# ---------------------------------------------------------------------------
# Request schema
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str
    history: list[dict] = []


# ---------------------------------------------------------------------------
# Org resolution (same pattern as sections.py)
# ---------------------------------------------------------------------------

def _resolve_org_id(user: User, db: Session, x_org_id: int | None) -> int:
    query = db.query(OrgRole).filter(OrgRole.user_id == user.id)
    if x_org_id is not None:
        query = query.filter(OrgRole.organization_id == x_org_id)
    role = query.first()
    if not role:
        raise HTTPException(status_code=403, detail="User has no organization")
    return role.organization_id


# ---------------------------------------------------------------------------
# Rate limiting (simple per-org daily counter via DB)
# ---------------------------------------------------------------------------

_rate_limit_cache: dict[str, tuple[int, str]] = {}  # org_id -> (count, date_str)


def _check_rate_limit(org_id: int) -> None:
    """Raise 429 if the org has exceeded daily agent message limit."""
    limit = settings.agent_rate_limit_per_org
    if limit <= 0:
        return  # Unlimited

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    key = str(org_id)

    count, date_str = _rate_limit_cache.get(key, (0, today))
    if date_str != today:
        count = 0
        date_str = today

    if count >= limit:
        raise HTTPException(
            status_code=429,
            detail=f"Daily agent message limit reached ({limit}/day). Resets at midnight UTC.",
        )

    _rate_limit_cache[key] = (count + 1, date_str)


# ---------------------------------------------------------------------------
# Tool definitions (provider-agnostic — converted per provider)
# ---------------------------------------------------------------------------

TOOL_DEFS = [
    # --- Read / Explore ---
    {
        "name": "list_sections",
        "description": "List all documentation sections in the workspace. Returns section names, IDs, hierarchy, and page counts. Use this to understand the documentation structure.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "list_pages",
        "description": "List all pages within a specific section. Returns page titles, IDs, status (draft/review/published), and whether they are published.",
        "parameters": {
            "type": "object",
            "properties": {
                "section_id": {"type": "integer", "description": "The section ID to list pages for."},
            },
            "required": ["section_id"],
        },
    },
    {
        "name": "read_page",
        "description": "Read the full content of a specific documentation page. Returns the page title, text content, status, and section. Use this to understand existing documentation style and content.",
        "parameters": {
            "type": "object",
            "properties": {
                "page_id": {"type": "integer", "description": "The page ID to read."},
            },
            "required": ["page_id"],
        },
    },
    {
        "name": "search_docs",
        "description": "Search across all documentation pages by keyword. Returns matching page titles and content snippets. Use this to find relevant existing documentation.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query to match against page titles and content."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_members",
        "description": "List all team members in the workspace with their roles (owner, admin, editor, reviewer, viewer). Use this to understand who has access and what roles exist.",
        "parameters": {"type": "object", "properties": {}},
    },
    # --- Create / Write ---
    {
        "name": "create_draft",
        "description": "Create a new documentation draft page. This creates a Google Doc and registers it as a draft page. The content should be well-structured markdown with headings (#, ##, ###).",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "The page title."},
                "content": {"type": "string", "description": "The documentation content in markdown format."},
                "section_id": {"type": "integer", "description": "The section ID to place the page in."},
            },
            "required": ["title", "content", "section_id"],
        },
    },
    {
        "name": "create_section",
        "description": "Create a new documentation section to organize pages. Sections can be nested under other sections via parent_id.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The section name."},
                "parent_id": {"type": "integer", "description": "Optional parent section ID for nesting. Omit for a top-level section."},
            },
            "required": ["name"],
        },
    },
    # --- Update / Manage ---
    {
        "name": "update_page",
        "description": "Update a page's properties: move it to a different section, rename it, or change its display order.",
        "parameters": {
            "type": "object",
            "properties": {
                "page_id": {"type": "integer", "description": "The page ID to update."},
                "title": {"type": "string", "description": "New title for the page."},
                "section_id": {"type": "integer", "description": "New section ID to move the page to."},
                "display_order": {"type": "integer", "description": "New display order (0-based)."},
            },
            "required": ["page_id"],
        },
    },
    {
        "name": "publish_page",
        "description": "Publish a draft page, making it visible on the public docs site. The page must have content synced from Google Drive first.",
        "parameters": {
            "type": "object",
            "properties": {
                "page_id": {"type": "integer", "description": "The page ID to publish."},
            },
            "required": ["page_id"],
        },
    },
    {
        "name": "unpublish_page",
        "description": "Unpublish a published page, removing it from the public docs site. The draft content is preserved.",
        "parameters": {
            "type": "object",
            "properties": {
                "page_id": {"type": "integer", "description": "The page ID to unpublish."},
            },
            "required": ["page_id"],
        },
    },
    {
        "name": "sync_page",
        "description": "Sync a page's content from its Google Doc. Use this to refresh the page after edits have been made in Google Docs.",
        "parameters": {
            "type": "object",
            "properties": {
                "page_id": {"type": "integer", "description": "The page ID to sync."},
            },
            "required": ["page_id"],
        },
    },
    {
        "name": "duplicate_page",
        "description": "Create a copy of an existing page (including its Google Doc). Useful for using an existing page as a template for a new one.",
        "parameters": {
            "type": "object",
            "properties": {
                "page_id": {"type": "integer", "description": "The page ID to duplicate."},
            },
            "required": ["page_id"],
        },
    },
    {
        "name": "delete_page",
        "description": "Delete a documentation page. This moves the Google Doc to trash and removes the page from the system. Use with caution — ask the user for confirmation first.",
        "parameters": {
            "type": "object",
            "properties": {
                "page_id": {"type": "integer", "description": "The page ID to delete."},
            },
            "required": ["page_id"],
        },
    },
    # --- External integrations ---
    {
        "name": "fetch_jira_ticket",
        "description": "Fetch a Jira ticket by its key (e.g. PROJ-123). Returns the ticket summary, description, status, and type. Requires Jira to be connected.",
        "parameters": {
            "type": "object",
            "properties": {
                "ticket_key": {"type": "string", "description": "The Jira ticket key (e.g. PROJ-123)."},
            },
            "required": ["ticket_key"],
        },
    },
    {
        "name": "search_confluence",
        "description": "Search Confluence for relevant pages. Use this to find external documentation context.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query for Confluence."},
            },
            "required": ["query"],
        },
    },
]


def _tools_for_anthropic() -> list[dict]:
    """Convert tool defs to Anthropic format (input_schema instead of parameters)."""
    return [
        {
            "name": t["name"],
            "description": t["description"],
            "input_schema": t["parameters"],
        }
        for t in TOOL_DEFS
    ]


def _tools_for_gemini():
    """Convert tool defs to Gemini function declarations."""
    from google import genai
    from google.genai import types

    declarations = []
    for t in TOOL_DEFS:
        params = t["parameters"]
        # Gemini needs properties to be non-empty for function declarations
        schema = None
        if params.get("properties"):
            schema = types.Schema(
                type="OBJECT",
                properties={
                    k: types.Schema(
                        type=v.get("type", "string").upper(),
                        description=v.get("description", ""),
                    )
                    for k, v in params["properties"].items()
                },
                required=params.get("required", []),
            )
        else:
            schema = types.Schema(type="OBJECT", properties={})

        declarations.append(types.FunctionDeclaration(
            name=t["name"],
            description=t["description"],
            parameters=schema,
        ))

    return types.Tool(function_declarations=declarations)


# ---------------------------------------------------------------------------
# Tool implementations (unchanged — provider-agnostic)
# ---------------------------------------------------------------------------

async def _tool_list_sections(
    _input: dict, user: User, org_id: int, db: Session,
) -> dict:
    sections = db.query(Section).filter(Section.organization_id == org_id).all()
    result = []
    for s in sections:
        page_count = db.query(func.count(Page.id)).filter(
            Page.section_id == s.id,
        ).scalar() or 0
        result.append({
            "id": s.id,
            "name": s.name,
            "parent_id": s.parent_id,
            "section_type": getattr(s, "section_type", "section"),
            "page_count": page_count,
        })
    return {"sections": result}


async def _tool_list_pages(
    input_data: dict, user: User, org_id: int, db: Session,
) -> dict:
    section_id = input_data.get("section_id")
    if not section_id:
        return {"error": "section_id is required"}
    pages = db.query(Page).filter(
        Page.organization_id == org_id,
        Page.section_id == int(section_id),
    ).order_by(Page.display_order, Page.title).all()
    return {
        "pages": [
            {
                "id": p.id,
                "title": p.title,
                "slug": p.slug,
                "status": p.status,
                "is_published": p.is_published,
            }
            for p in pages
        ]
    }


async def _tool_read_page(
    input_data: dict, user: User, org_id: int, db: Session,
) -> dict:
    page_id = input_data.get("page_id")
    if not page_id:
        return {"error": "page_id is required"}
    page = db.query(Page).filter(
        Page.id == int(page_id),
        Page.organization_id == org_id,
    ).first()
    if not page:
        return {"error": f"Page {page_id} not found"}
    content = _html_to_text(page.published_html or page.html_content or "")
    return {
        "id": page.id,
        "title": page.title,
        "slug": page.slug,
        "status": page.status,
        "section_id": page.section_id,
        "content": content[:8000],
    }


async def _tool_search_docs(
    input_data: dict, user: User, org_id: int, db: Session,
) -> dict:
    query = (input_data.get("query") or "").strip()
    if not query:
        return {"error": "query is required"}
    like_pat = f"%{query}%"
    pages = db.query(Page).filter(
        Page.organization_id == org_id,
        or_(
            Page.title.ilike(like_pat),
            Page.html_content.ilike(like_pat),
            Page.published_html.ilike(like_pat),
        ),
    ).limit(10).all()
    results = []
    for p in pages:
        snippet = _html_to_text(p.published_html or p.html_content or "")[:300]
        results.append({
            "id": p.id,
            "title": p.title,
            "slug": p.slug,
            "status": p.status,
            "snippet": snippet,
        })
    return {"results": results, "count": len(results)}


def _markdown_to_docs_requests(markdown: str) -> list[dict]:
    """Convert markdown text to Google Docs API batchUpdate requests."""
    import re

    lines = markdown.split("\n")
    full_text = "\n".join(lines) + "\n"
    if not full_text.strip():
        return []

    requests: list[dict] = []
    requests.append({
        "insertText": {
            "location": {"index": 1},
            "text": full_text,
        }
    })

    offset = 1
    for line in lines:
        line_len = len(line)

        heading_match = re.match(r"^(#{1,3})\s+(.*)", line)
        if heading_match:
            level = len(heading_match.group(1))
            named_style = {
                1: "HEADING_1",
                2: "HEADING_2",
                3: "HEADING_3",
            }.get(level, "NORMAL_TEXT")
            requests.append({
                "updateParagraphStyle": {
                    "range": {"startIndex": offset, "endIndex": offset + line_len},
                    "paragraphStyle": {"namedStyleType": named_style},
                    "fields": "namedStyleType",
                }
            })
            prefix_len = level + 1
            requests.append({
                "deleteContentRange": {
                    "range": {
                        "startIndex": offset,
                        "endIndex": offset + prefix_len,
                    }
                }
            })

        offset += line_len + 1

    if len(requests) > 1:
        requests = [requests[0]] + list(reversed(requests[1:]))

    return requests


async def _tool_create_draft(
    input_data: dict, user: User, org_id: int, db: Session,
) -> dict:
    title = (input_data.get("title") or "").strip()
    content = (input_data.get("content") or "").strip()
    section_id = input_data.get("section_id")

    if not title or not content or not section_id:
        return {"error": "title, content, and section_id are all required"}

    section = db.query(Section).filter(
        Section.id == int(section_id),
        Section.organization_id == org_id,
    ).first()
    if not section:
        return {"error": f"Section {section_id} not found"}

    try:
        from app.api.drive import get_drive_credentials, _create_drive_doc
        from googleapiclient.discovery import build

        creds = await get_drive_credentials(user, org_id, db)
        drive_service = build("drive", "v3", credentials=creds, cache_discovery=False)

        parent_drive_id = section.drive_folder_id
        if not parent_drive_id:
            org = db.get(Organization, org_id)
            parent_drive_id = org.drive_folder_id if org else None

        doc_id = _create_drive_doc(drive_service, title, parent_drive_id)

        docs_service = build("docs", "v1", credentials=creds, cache_discovery=False)
        requests = _markdown_to_docs_requests(content)
        if requests:
            docs_service.documents().batchUpdate(
                documentId=doc_id,
                body={"requests": requests},
            ).execute()
    except Exception as exc:
        logger.error("Drive doc creation failed: %s", exc)
        return {"error": f"Failed to create Google Doc: {exc}"}

    slug = _unique_slug(title, org_id, db)
    page = Page(
        organization_id=org_id,
        section_id=int(section_id),
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

    try:
        raw = drive_service.files().export(fileId=doc_id, mimeType="text/html").execute()
        html = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        page.html_content = html
        meta = drive_service.files().get(
            fileId=doc_id, fields="modifiedTime", supportsAllDrives=True,
        ).execute()
        page.drive_modified_at = meta.get("modifiedTime")
        page.last_synced_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(page)
    except Exception as exc:
        logger.warning("Post-creation sync failed for page %d: %s", page.id, exc)

    return {
        "page_id": page.id,
        "title": page.title,
        "slug": page.slug,
        "google_doc_id": doc_id,
    }


async def _tool_fetch_jira_ticket(
    input_data: dict, user: User, org_id: int, db: Session,
) -> dict:
    ticket_key = (input_data.get("ticket_key") or "").strip()
    if not ticket_key:
        return {"error": "ticket_key is required"}
    body = {"ticket_key": ticket_key, "_x_org_id": str(org_id)}
    result = await jira_get_ticket(body=body, db=db, user=user)
    return result


async def _tool_search_confluence(
    input_data: dict, user: User, org_id: int, db: Session,
) -> dict:
    return {"error": "Confluence integration is not configured yet. This feature is coming soon."}


async def _tool_list_members(
    _input: dict, user: User, org_id: int, db: Session,
) -> dict:
    roles = db.query(OrgRole).filter(OrgRole.organization_id == org_id).all()
    members = []
    for r in roles:
        u = db.get(User, r.user_id)
        if u:
            members.append({
                "id": u.id,
                "name": u.name or u.email,
                "email": u.email,
                "role": r.role,
            })
    return {"members": members, "count": len(members)}


async def _tool_create_section(
    input_data: dict, user: User, org_id: int, db: Session,
) -> dict:
    name = (input_data.get("name") or "").strip()
    if not name:
        return {"error": "name is required"}

    parent_id = input_data.get("parent_id")
    if parent_id is not None:
        parent = db.query(Section).filter(
            Section.id == int(parent_id),
            Section.organization_id == org_id,
        ).first()
        if not parent:
            return {"error": f"Parent section {parent_id} not found"}

    section = Section(
        organization_id=org_id,
        name=name,
        parent_id=int(parent_id) if parent_id else None,
        section_type="section",
        display_order=0,
    )
    db.add(section)
    db.commit()
    db.refresh(section)

    return {"id": section.id, "name": section.name, "parent_id": section.parent_id}


async def _tool_update_page(
    input_data: dict, user: User, org_id: int, db: Session,
) -> dict:
    page_id = input_data.get("page_id")
    if not page_id:
        return {"error": "page_id is required"}

    page = db.query(Page).filter(
        Page.id == int(page_id),
        Page.organization_id == org_id,
    ).first()
    if not page:
        return {"error": f"Page {page_id} not found"}

    if "title" in input_data and input_data["title"]:
        page.title = input_data["title"].strip()
    if "section_id" in input_data and input_data["section_id"]:
        target = db.query(Section).filter(
            Section.id == int(input_data["section_id"]),
            Section.organization_id == org_id,
        ).first()
        if not target:
            return {"error": f"Section {input_data['section_id']} not found"}
        page.section_id = target.id
    if "display_order" in input_data and input_data["display_order"] is not None:
        page.display_order = int(input_data["display_order"])

    db.commit()
    db.refresh(page)

    return {
        "id": page.id,
        "title": page.title,
        "section_id": page.section_id,
        "display_order": page.display_order,
    }


async def _tool_publish_page(
    input_data: dict, user: User, org_id: int, db: Session,
) -> dict:
    page_id = input_data.get("page_id")
    if not page_id:
        return {"error": "page_id is required"}

    page = db.query(Page).filter(
        Page.id == int(page_id),
        Page.organization_id == org_id,
    ).first()
    if not page:
        return {"error": f"Page {page_id} not found"}

    if not page.html_content:
        return {"error": "Page has no content to publish. Sync from Google Drive first."}

    page.published_html = page.html_content
    page.is_published = True
    page.status = "published"
    page.published_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(page)

    return {"id": page.id, "title": page.title, "status": page.status, "is_published": True}


async def _tool_unpublish_page(
    input_data: dict, user: User, org_id: int, db: Session,
) -> dict:
    page_id = input_data.get("page_id")
    if not page_id:
        return {"error": "page_id is required"}

    page = db.query(Page).filter(
        Page.id == int(page_id),
        Page.organization_id == org_id,
    ).first()
    if not page:
        return {"error": f"Page {page_id} not found"}

    page.is_published = False
    page.status = "draft"
    db.commit()
    db.refresh(page)

    return {"id": page.id, "title": page.title, "status": page.status, "is_published": False}


async def _tool_sync_page(
    input_data: dict, user: User, org_id: int, db: Session,
) -> dict:
    page_id = input_data.get("page_id")
    if not page_id:
        return {"error": "page_id is required"}

    page = db.query(Page).filter(
        Page.id == int(page_id),
        Page.organization_id == org_id,
    ).first()
    if not page:
        return {"error": f"Page {page_id} not found"}
    if not page.google_doc_id:
        return {"error": "Page has no linked Google Doc"}

    try:
        from app.api.drive import get_drive_credentials
        from googleapiclient.discovery import build

        creds = await get_drive_credentials(user, org_id, db)
        drive_service = build("drive", "v3", credentials=creds, cache_discovery=False)

        raw = drive_service.files().export(
            fileId=page.google_doc_id, mimeType="text/html",
        ).execute()
        html = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        page.html_content = html

        meta = drive_service.files().get(
            fileId=page.google_doc_id, fields="modifiedTime", supportsAllDrives=True,
        ).execute()
        page.drive_modified_at = meta.get("modifiedTime")
        page.last_synced_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(page)
    except Exception as exc:
        return {"error": f"Sync failed: {exc}"}

    return {"id": page.id, "title": page.title, "synced": True}


async def _tool_duplicate_page(
    input_data: dict, user: User, org_id: int, db: Session,
) -> dict:
    page_id = input_data.get("page_id")
    if not page_id:
        return {"error": "page_id is required"}

    page = db.query(Page).filter(
        Page.id == int(page_id),
        Page.organization_id == org_id,
    ).first()
    if not page:
        return {"error": f"Page {page_id} not found"}

    try:
        from app.api.drive import get_drive_credentials, _create_drive_doc
        from googleapiclient.discovery import build

        creds = await get_drive_credentials(user, org_id, db)
        drive_service = build("drive", "v3", credentials=creds, cache_discovery=False)

        new_title = f"{page.title} (copy)"

        if page.google_doc_id:
            copy_meta = drive_service.files().copy(
                fileId=page.google_doc_id,
                body={"name": new_title},
                supportsAllDrives=True,
            ).execute()
            new_doc_id = copy_meta["id"]
        else:
            org = db.get(Organization, org_id)
            parent_id = (
                page.section.drive_folder_id
                if page.section and page.section.drive_folder_id
                else (org.drive_folder_id if org else None)
            )
            new_doc_id = _create_drive_doc(drive_service, new_title, parent_id)
    except Exception as exc:
        return {"error": f"Failed to duplicate Google Doc: {exc}"}

    slug = _unique_slug(new_title, org_id, db)
    new_page = Page(
        organization_id=org_id,
        section_id=page.section_id,
        google_doc_id=new_doc_id,
        title=new_title,
        slug=slug,
        slug_locked=False,
        status="draft",
        is_published=False,
        display_order=page.display_order + 1,
        owner_id=user.id,
        html_content=page.html_content,
    )
    db.add(new_page)
    db.commit()
    db.refresh(new_page)

    return {
        "page_id": new_page.id,
        "title": new_page.title,
        "slug": new_page.slug,
        "google_doc_id": new_doc_id,
        "original_page_id": page.id,
    }


async def _tool_delete_page(
    input_data: dict, user: User, org_id: int, db: Session,
) -> dict:
    page_id = input_data.get("page_id")
    if not page_id:
        return {"error": "page_id is required"}

    page = db.query(Page).filter(
        Page.id == int(page_id),
        Page.organization_id == org_id,
    ).first()
    if not page:
        return {"error": f"Page {page_id} not found"}

    title = page.title

    # Trash the Google Doc if linked
    if page.google_doc_id:
        try:
            from app.api.drive import get_drive_credentials
            from googleapiclient.discovery import build

            creds = await get_drive_credentials(user, org_id, db)
            drive_service = build("drive", "v3", credentials=creds, cache_discovery=False)
            drive_service.files().update(
                fileId=page.google_doc_id,
                body={"trashed": True},
                supportsAllDrives=True,
            ).execute()
        except Exception as exc:
            logger.warning("Failed to trash Google Doc %s: %s", page.google_doc_id, exc)

    db.delete(page)
    db.commit()

    return {"deleted": True, "page_id": int(page_id), "title": title}


TOOL_HANDLERS = {
    "list_sections": _tool_list_sections,
    "list_pages": _tool_list_pages,
    "read_page": _tool_read_page,
    "search_docs": _tool_search_docs,
    "list_members": _tool_list_members,
    "create_draft": _tool_create_draft,
    "create_section": _tool_create_section,
    "update_page": _tool_update_page,
    "publish_page": _tool_publish_page,
    "unpublish_page": _tool_unpublish_page,
    "sync_page": _tool_sync_page,
    "duplicate_page": _tool_duplicate_page,
    "delete_page": _tool_delete_page,
    "fetch_jira_ticket": _tool_fetch_jira_ticket,
    "search_confluence": _tool_search_confluence,
}

TOOL_FRIENDLY_NAMES = {
    "list_sections": "Browsing sections",
    "list_pages": "Listing pages",
    "read_page": "Reading page",
    "search_docs": "Searching documentation",
    "list_members": "Listing team members",
    "create_draft": "Creating draft",
    "create_section": "Creating section",
    "update_page": "Updating page",
    "publish_page": "Publishing page",
    "unpublish_page": "Unpublishing page",
    "sync_page": "Syncing from Drive",
    "duplicate_page": "Duplicating page",
    "delete_page": "Deleting page",
    "fetch_jira_ticket": "Fetching Jira ticket",
    "search_confluence": "Searching Confluence",
}


# ---------------------------------------------------------------------------
# System prompt (shared by both providers)
# ---------------------------------------------------------------------------

def _build_system_prompt(org_id: int, db: Session) -> str:
    org = db.get(Organization, org_id)
    org_name = org.name if org else "the organization"

    section_count = db.query(func.count(Section.id)).filter(
        Section.organization_id == org_id,
    ).scalar() or 0
    page_count = db.query(func.count(Page.id)).filter(
        Page.organization_id == org_id,
    ).scalar() or 0
    published_count = db.query(func.count(Page.id)).filter(
        Page.organization_id == org_id,
        Page.is_published == True,  # noqa: E712
    ).scalar() or 0

    draft_count = db.query(func.count(Page.id)).filter(
        Page.organization_id == org_id,
        Page.status == "draft",
    ).scalar() or 0

    return (
        f"You are the documentation agent for {org_name} on AccelDocs.\n"
        f"You are a powerful autonomous agent that can explore, create, organize, publish, and manage documentation.\n\n"
        f"Workspace stats: {section_count} sections, {page_count} pages "
        f"({published_count} published, {draft_count} drafts).\n\n"
        f"Your capabilities:\n"
        f"- EXPLORE: Browse sections and pages, read page content, search across all docs, list team members\n"
        f"- CREATE: Write new draft pages (backed by Google Docs), create new sections to organize content\n"
        f"- MANAGE: Move pages between sections, rename pages, reorder pages, duplicate pages as templates\n"
        f"- PUBLISH: Publish drafts to the public docs site, unpublish pages, sync content from Google Drive\n"
        f"- DELETE: Remove outdated pages (with user confirmation)\n"
        f"- INTEGRATE: Fetch Jira tickets for context when writing docs\n\n"
        f"Guidelines:\n"
        f"- Be proactive. When asked to write docs, first explore existing docs to match style and structure.\n"
        f"- When creating content, place it in the right section. If no section fits, create one.\n"
        f"- Write clear, concise technical documentation with proper headings (# ## ###).\n"
        f"- When the user mentions a Jira ticket (e.g. PROJ-123), fetch it first for context.\n"
        f"- After creating a draft, tell the user the title and offer to publish it.\n"
        f"- Before deleting anything, always confirm with the user first.\n"
        f"- When asked to reorganize or audit docs, systematically browse all sections and pages.\n"
        f"- You can chain multiple actions: e.g. create a section, then create pages in it, then publish them.\n"
        f"- Be conversational, helpful, and take initiative. You're a documentation expert."
    )


# ---------------------------------------------------------------------------
# Tool execution helper (shared by both providers)
# ---------------------------------------------------------------------------

async def _execute_tool(
    tool_name: str, tool_input: dict, user: User, org_id: int, db: Session,
) -> tuple[dict, bool]:
    """Execute a tool and return (result_dict, success)."""
    handler = TOOL_HANDLERS.get(tool_name)
    if handler:
        try:
            result = await handler(tool_input, user, org_id, db)
        except Exception as exc:
            result = {"error": str(exc)}
    else:
        result = {"error": f"Unknown tool: {tool_name}"}

    success = "error" not in result
    return result, success


# ---------------------------------------------------------------------------
# Gemini agent loop
# ---------------------------------------------------------------------------

async def _run_gemini_loop(
    message: str,
    history: list[dict],
    user: User,
    org_id: int,
    db: Session,
) -> AsyncGenerator[dict, None]:
    """Run agent loop using Google Gemini."""
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise HTTPException(status_code=503, detail="google-genai not installed") from exc

    client = genai.Client(api_key=settings.gemini_api_key)
    system_prompt = _build_system_prompt(org_id, db)
    tools = _tools_for_gemini()

    # Build Gemini message history
    contents = []
    for h in history[-20:]:
        role = "model" if h["role"] == "assistant" else "user"
        contents.append(types.Content(role=role, parts=[types.Part.from_text(text=h["content"])]))
    contents.append(types.Content(role="user", parts=[types.Part.from_text(text=message)]))

    max_iterations = 10

    for _ in range(max_iterations):
        # Call Gemini (non-streaming for tool use reliability, stream text chunks)
        response = client.models.generate_content(
            model=settings.gemini_model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                tools=[tools],
                temperature=0.7,
                max_output_tokens=4096,
            ),
        )

        if not response.candidates or not response.candidates[0].content:
            yield {"type": "error", "message": "No response from AI model"}
            return

        candidate = response.candidates[0]
        parts = candidate.content.parts or []

        # Collect text parts and function calls
        text_parts = []
        function_calls = []
        for part in parts:
            if part.text:
                text_parts.append(part.text)
            if part.function_call:
                function_calls.append(part.function_call)

        # Stream text
        if text_parts:
            full_text = "".join(text_parts)
            # Yield in chunks for streaming feel
            chunk_size = 20
            for i in range(0, len(full_text), chunk_size):
                yield {"type": "text_delta", "text": full_text[i:i + chunk_size]}

        # If no function calls, we're done
        if not function_calls:
            yield {"type": "done"}
            return

        # Add assistant response to history
        contents.append(candidate.content)

        # Process function calls
        function_responses = []
        for fc in function_calls:
            tool_name = fc.name
            tool_input = dict(fc.args) if fc.args else {}

            yield {
                "type": "tool_start",
                "tool_name": tool_name,
                "tool_input": tool_input,
            }

            result, success = await _execute_tool(tool_name, tool_input, user, org_id, db)

            yield {
                "type": "tool_result",
                "tool_name": tool_name,
                "success": success,
                "data": result,
            }

            if tool_name == "create_draft" and success and "page_id" in result:
                yield {
                    "type": "draft_created",
                    "page_id": result["page_id"],
                    "title": result["title"],
                    "google_doc_id": result["google_doc_id"],
                }

            # Truncate for history
            result_str = json.dumps(result)
            if len(result_str) > 2000:
                result_str = result_str[:2000] + "...(truncated)"

            function_responses.append(
                types.Part.from_function_response(
                    name=tool_name,
                    response={"result": result_str},
                )
            )

        # Add tool results back to conversation
        contents.append(types.Content(
            role="user",
            parts=function_responses,
        ))

    yield {"type": "text_delta", "text": "\n\nI've reached the maximum number of steps for this turn. Please continue the conversation if you need more."}
    yield {"type": "done"}


# ---------------------------------------------------------------------------
# OpenAI-compatible agent loop (Groq, Ollama, vLLM, Together, etc.)
# ---------------------------------------------------------------------------

def _tools_for_openai() -> list[dict]:
    """Convert tool defs to OpenAI function-calling format."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["parameters"],
            },
        }
        for t in TOOL_DEFS
    ]


async def _run_openai_compat_loop(
    message: str,
    history: list[dict],
    user: User,
    org_id: int,
    db: Session,
    *,
    base_url: str,
    model: str,
    api_key: str = "",
    provider_name: str = "AI",
) -> AsyncGenerator[dict, None]:
    """Run agent loop using any OpenAI-compatible API (Groq, Ollama, vLLM, etc.)."""
    import httpx

    base_url = base_url.rstrip("/")
    system_prompt = _build_system_prompt(org_id, db)
    tools = _tools_for_openai()

    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    # Build OpenAI-format messages
    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    for h in history[-20:]:
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": message})

    max_iterations = 10

    async with httpx.AsyncClient(timeout=120.0) as client:
        for _ in range(max_iterations):
            try:
                resp = await client.post(
                    f"{base_url}/v1/chat/completions",
                    headers=headers,
                    json={
                        "model": model,
                        "messages": messages,
                        "tools": tools,
                        "temperature": 0.7,
                        "max_tokens": 4096,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
            except httpx.ConnectError:
                yield {"type": "error", "message": f"Cannot connect to {provider_name} at {base_url}. Is it running?"}
                return
            except httpx.HTTPStatusError as exc:
                error_body = exc.response.text[:500] if exc.response else str(exc)
                yield {"type": "error", "message": f"{provider_name} error ({exc.response.status_code}): {error_body}"}
                return
            except Exception as exc:
                yield {"type": "error", "message": f"{provider_name} error: {exc}"}
                return

            choice = data.get("choices", [{}])[0]
            msg = choice.get("message", {})

            # Stream text content
            text_content = msg.get("content") or ""
            if text_content:
                chunk_size = 20
                for i in range(0, len(text_content), chunk_size):
                    yield {"type": "text_delta", "text": text_content[i:i + chunk_size]}

            # Check for tool calls
            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                yield {"type": "done"}
                return

            # Add assistant message with tool calls to history
            messages.append(msg)

            # Process tool calls
            for tc in tool_calls:
                func = tc.get("function", {})
                tool_name = func.get("name", "")
                try:
                    tool_input = json.loads(func.get("arguments", "{}"))
                except json.JSONDecodeError:
                    tool_input = {}

                yield {
                    "type": "tool_start",
                    "tool_name": tool_name,
                    "tool_input": tool_input,
                }

                result, success = await _execute_tool(tool_name, tool_input, user, org_id, db)

                yield {
                    "type": "tool_result",
                    "tool_name": tool_name,
                    "success": success,
                    "data": result,
                }

                if tool_name == "create_draft" and success and "page_id" in result:
                    yield {
                        "type": "draft_created",
                        "page_id": result["page_id"],
                        "title": result["title"],
                        "google_doc_id": result["google_doc_id"],
                    }

                result_str = json.dumps(result)
                if len(result_str) > 2000:
                    result_str = result_str[:2000] + "...(truncated)"

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": result_str,
                })

    yield {"type": "text_delta", "text": "\n\nI've reached the maximum number of steps for this turn. Please continue the conversation if you need more."}
    yield {"type": "done"}


async def _run_groq_loop(
    message: str,
    history: list[dict],
    user: User,
    org_id: int,
    db: Session,
) -> AsyncGenerator[dict, None]:
    """Run agent loop using Groq (free tier, OpenAI-compatible)."""
    async for event in _run_openai_compat_loop(
        message, history, user, org_id, db,
        base_url="https://api.groq.com/openai",
        model=settings.groq_model,
        api_key=settings.groq_api_key,
        provider_name="Groq",
    ):
        yield event


# ---------------------------------------------------------------------------
# Anthropic agent loop
# ---------------------------------------------------------------------------

async def _run_anthropic_loop(
    message: str,
    history: list[dict],
    user: User,
    org_id: int,
    db: Session,
) -> AsyncGenerator[dict, None]:
    """Run agent loop using Anthropic Claude."""
    try:
        import anthropic  # type: ignore
    except ImportError as exc:
        raise HTTPException(status_code=503, detail="anthropic not installed") from exc

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    system = _build_system_prompt(org_id, db)
    tools = _tools_for_anthropic()

    messages = []
    for h in history[-20:]:
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": message})

    max_iterations = 10

    for _ in range(max_iterations):
        collected_text = ""

        async with client.messages.stream(
            model=settings.anthropic_model,
            max_tokens=4096,
            system=system,
            tools=tools,
            messages=messages,
        ) as stream:
            async for event in stream:
                if event.type == "content_block_delta":
                    if hasattr(event.delta, "text"):
                        collected_text += event.delta.text
                        yield {"type": "text_delta", "text": event.delta.text}

            response = await stream.get_final_message()

        if response.stop_reason != "tool_use":
            yield {"type": "done"}
            return

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []

        for block in response.content:
            if block.type != "tool_use":
                continue

            tool_name = block.name
            tool_input = block.input

            yield {
                "type": "tool_start",
                "tool_name": tool_name,
                "tool_input": tool_input,
            }

            result, success = await _execute_tool(tool_name, tool_input, user, org_id, db)

            yield {
                "type": "tool_result",
                "tool_name": tool_name,
                "success": success,
                "data": result,
            }

            if tool_name == "create_draft" and success and "page_id" in result:
                yield {
                    "type": "draft_created",
                    "page_id": result["page_id"],
                    "title": result["title"],
                    "google_doc_id": result["google_doc_id"],
                }

            result_str = json.dumps(result)
            if len(result_str) > 2000:
                result_str = result_str[:2000] + "...(truncated)"

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result_str,
            })

        messages.append({"role": "user", "content": tool_results})

    yield {"type": "text_delta", "text": "\n\nI've reached the maximum number of steps for this turn. Please continue the conversation if you need more."}
    yield {"type": "done"}


# ---------------------------------------------------------------------------
# SSE endpoint
# ---------------------------------------------------------------------------

@router.post("/api/agent/chat")
async def agent_chat(
    body: ChatRequest,
    x_org_id: int | None = Header(default=None, alias="X-Org-Id"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Streaming conversational agent endpoint via SSE."""
    if EventSourceResponse is None:
        raise HTTPException(status_code=503, detail="AI agent streaming dependency not installed")

    provider = settings.agent_provider.lower()

    # Validate provider configuration
    valid_providers = ("gemini", "groq", "anthropic", "openai_compat")
    if provider not in valid_providers:
        raise HTTPException(status_code=503, detail=f"Unknown agent provider: {provider}. Valid: {', '.join(valid_providers)}")
    if provider == "gemini" and not settings.gemini_api_key:
        raise HTTPException(status_code=503, detail="AI agent not configured — set GEMINI_API_KEY")
    if provider == "groq" and not settings.groq_api_key:
        raise HTTPException(status_code=503, detail="AI agent not configured — set GROQ_API_KEY (free at console.groq.com)")
    if provider == "anthropic" and not settings.anthropic_api_key:
        raise HTTPException(status_code=503, detail="AI agent not configured — set ANTHROPIC_API_KEY")

    org_id = _resolve_org_id(user, db, x_org_id)

    # Rate limit check
    _check_rate_limit(org_id)

    # Select the right agent loop
    if provider == "openai_compat":
        async def run_loop(msg, hist, usr, oid, d):
            async for ev in _run_openai_compat_loop(
                msg, hist, usr, oid, d,
                base_url=settings.openai_compat_base_url,
                model=settings.openai_compat_model,
                api_key=settings.openai_compat_api_key,
                provider_name="OpenAI-compat",
            ):
                yield ev
    else:
        run_loop = {
            "gemini": _run_gemini_loop,
            "groq": _run_groq_loop,
            "anthropic": _run_anthropic_loop,
        }[provider]

    async def event_generator():
        try:
            async for event in run_loop(
                body.message, body.history, user, org_id, db,
            ):
                yield {"event": "message", "data": json.dumps(event)}
        except Exception as exc:
            logger.error("Agent chat error: %s", exc, exc_info=True)
            yield {
                "event": "message",
                "data": json.dumps({"type": "error", "message": str(exc)}),
            }

    return EventSourceResponse(event_generator())
