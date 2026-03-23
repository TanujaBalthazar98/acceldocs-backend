"""Conversational documentation agent — streaming chat with tool use.

Supports LLM providers:
  - gemini (default) — Google Gemini Flash, generous free tier
  - groq — Groq Cloud, generous free tier (Llama 3.3 70B)
  - anthropic — Claude Sonnet, higher quality, paid
  - openai_compat — any OpenAI-compatible endpoint (Ollama, vLLM, etc.)
"""

import dataclasses
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
# LLM configuration — per-org BYOK with env-var fallback
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class LLMConfig:
    provider: str
    api_key: str
    model: str
    base_url: str = ""


def _resolve_llm_config(org_id: int, db: Session) -> LLMConfig:
    """Resolve LLM config: org BYOK settings first, then env-var fallback."""
    org = db.query(Organization).filter(Organization.id == org_id).first()

    # Check org-level BYOK settings
    if org and org.ai_provider and org.ai_api_key_encrypted:
        try:
            from app.services.encryption import get_encryption_service
            enc = get_encryption_service()
            api_key = enc.decrypt(org.ai_api_key_encrypted)
            return LLMConfig(
                provider=org.ai_provider,
                api_key=api_key,
                model=org.ai_model or "",
                base_url=org.ai_base_url or "",
            )
        except Exception:
            logger.warning("Failed to decrypt org %s AI key, falling back to env vars", org_id)

    # Fall back to global env vars
    provider = (settings.agent_provider or "gemini").lower()
    if provider == "gemini":
        return LLMConfig(provider="gemini", api_key=settings.gemini_api_key or "", model=settings.gemini_model or "gemini-2.0-flash")
    if provider == "groq":
        return LLMConfig(provider="groq", api_key=settings.groq_api_key or "", model=settings.groq_model or "meta-llama/llama-4-scout-17b-16e-instruct")
    if provider == "anthropic":
        return LLMConfig(provider="anthropic", api_key=settings.anthropic_api_key or "", model=settings.anthropic_model or "claude-sonnet-4-5-20250514")
    if provider == "openai_compat":
        return LLMConfig(
            provider="openai_compat",
            api_key=settings.openai_compat_api_key or "",
            model=settings.openai_compat_model or "",
            base_url=settings.openai_compat_base_url or "",
        )
    return LLMConfig(provider=provider, api_key="", model="")


# ---------------------------------------------------------------------------
# Request schema
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str
    history: list[dict] = []
    conversation_id: int | None = None


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
                "section_id": {"type": ["integer", "string"], "description": "The numeric section ID to list pages for. Must be a number. Use list_sections first."},
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
                "page_id": {"type": ["integer", "string"], "description": "The numeric page ID to read. Must be a number."},
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
        "name": "search_knowledge_base",
        "description": "Semantic search across all published documentation using relevance ranking (BM25). Returns the most relevant pages with content snippets. Better than keyword search for finding related content to use as context when writing new docs.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural language search query."},
                "limit": {"type": ["integer", "string"], "description": "Max results to return (default 5)."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_templates",
        "description": "List available documentation templates (API Reference, Getting Started, FAQ, Changelog, How-To Guide, Troubleshooting). Use this to help users create structured documentation.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "create_from_template",
        "description": "Create a new documentation page from a template. The template structure is filled with content based on the user's description and existing knowledge base context.",
        "parameters": {
            "type": "object",
            "properties": {
                "template_slug": {"type": "string", "description": "Template to use (e.g. 'api-reference', 'getting-started', 'faq', 'changelog', 'how-to-guide', 'troubleshooting')."},
                "title": {"type": "string", "description": "The page title."},
                "description": {"type": "string", "description": "What the page should cover. Be specific — the AI uses this to fill in the template."},
                "section_id": {"type": ["integer", "string"], "description": "The numeric section ID to place the page in. Must be a number. Use list_sections first."},
            },
            "required": ["template_slug", "title", "description"],
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
                "section_id": {"type": ["integer", "string"], "description": "The numeric section ID to place the page in. Must be a number (e.g. 5), not a name or slug. Use list_sections first to find available section IDs."},
            },
            "required": ["title", "content"],
        },
    },
    {
        "name": "create_section",
        "description": "Create a new documentation section to organize pages. Sections can be nested under other sections via parent_id.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The section name."},
                "parent_id": {"type": ["integer", "string", "null"], "description": "Optional numeric parent section ID for nesting. Omit or null for a top-level section."},
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
                "page_id": {"type": ["integer", "string"], "description": "The numeric page ID to update. Must be a number."},
                "title": {"type": ["string", "null"], "description": "New title for the page."},
                "section_id": {"type": ["integer", "string", "null"], "description": "New numeric section ID to move the page to."},
                "display_order": {"type": ["integer", "string", "null"], "description": "New display order (0-based)."},
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
                "page_id": {"type": ["integer", "string"], "description": "The numeric page ID to publish."},
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
                "page_id": {"type": ["integer", "string"], "description": "The numeric page ID to unpublish."},
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
                "page_id": {"type": ["integer", "string"], "description": "The numeric page ID to sync."},
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
                "page_id": {"type": ["integer", "string"], "description": "The numeric page ID to duplicate."},
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
                "page_id": {"type": ["integer", "string"], "description": "The numeric page ID to delete."},
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
    {
        "name": "web_search",
        "description": "Search the internet for up-to-date information. Use this when the user asks about external topics, current releases, news, or anything not found in the internal knowledge base. Returns web search results with titles, URLs, and snippets.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query to look up on the web."},
                "max_results": {"type": ["integer", "string"], "description": "Maximum number of results to return (default 5, max 10)."},
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
            gemini_props = {}
            for k, v in params["properties"].items():
                raw_type = v.get("type", "string")
                # Gemini doesn't support union types — pick the first non-null type
                if isinstance(raw_type, list):
                    primary = next((t for t in raw_type if t not in ("null",)), "string")
                else:
                    primary = raw_type
                gemini_props[k] = types.Schema(
                    type=primary.upper(),
                    description=v.get("description", ""),
                )
            schema = types.Schema(
                type="OBJECT",
                properties=gemini_props,
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
    sections = db.query(Section).filter(
        Section.organization_id == org_id,
    ).order_by(Section.display_order).all()

    # Build a lookup for hierarchy display
    by_id = {s.id: s for s in sections}
    result = []
    for s in sections:
        page_count = db.query(func.count(Page.id)).filter(
            Page.section_id == s.id,
        ).scalar() or 0

        # Build a human-readable path like "Product > Getting Started > Tutorials"
        path_parts = []
        current = s
        while current:
            path_parts.insert(0, current.name)
            current = by_id.get(current.parent_id) if current.parent_id else None

        section_type = getattr(s, "section_type", "section")
        # Only leaf sections (those with no children) should have pages created in them
        has_children = any(c.parent_id == s.id for c in sections)

        result.append({
            "id": s.id,
            "name": s.name,
            "full_path": " > ".join(path_parts),
            "parent_id": s.parent_id,
            "section_type": section_type,
            "is_container": has_children,  # True = has sub-sections, prefer placing pages in children
            "page_count": page_count,
        })
    return {
        "sections": result,
        "hint": "Use the 'id' field when creating pages. Prefer placing pages in leaf sections (is_container=false). The full_path shows where each section sits in the hierarchy.",
    }


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


async def _tool_search_knowledge_base(
    input_data: dict, user: User, org_id: int, db: Session,
) -> dict:
    """BM25-ranked semantic search across published pages."""
    from app.services.search import search_pages_bm25

    query = (input_data.get("query") or "").strip()
    if not query:
        return {"error": "query is required"}
    limit = input_data.get("limit", 5)
    results = search_pages_bm25(org_id, query, db, limit=limit)
    # Enrich with longer content for agent context
    enriched = []
    for r in results:
        page = db.get(Page, r["id"])
        text = _html_to_text(page.published_html or page.html_content or "") if page else ""
        enriched.append({**r, "content": text[:3000]})
    return {"results": enriched, "count": len(enriched)}


async def _tool_list_templates(
    input_data: dict, user: User, org_id: int, db: Session,
) -> dict:
    from app.services.templates import list_template_summaries
    return {"templates": list_template_summaries()}


async def _tool_create_from_template(
    input_data: dict, user: User, org_id: int, db: Session,
) -> dict:
    """Create a page from a template, filled with AI-generated content."""
    from app.services.templates import get_template_by_slug
    from app.services.search import search_pages_bm25

    template_slug = (input_data.get("template_slug") or "").strip()
    title = (input_data.get("title") or "").strip()
    description = (input_data.get("description") or "").strip()
    section_id = input_data.get("section_id")

    if not template_slug or not title:
        return {"error": "template_slug and title are required"}

    # Resolve section — same logic as create_draft
    if not section_id:
        all_sections = db.query(Section).filter(
            Section.organization_id == org_id,
        ).order_by(Section.display_order).all()
        if not all_sections:
            return {"error": "No sections exist. Create a section first using create_section."}
        parent_ids = {s.parent_id for s in all_sections if s.parent_id}
        leaves = [s for s in all_sections if s.id not in parent_ids]
        if len(leaves) == 1:
            section_id = leaves[0].id
        elif len(all_sections) == 1:
            section_id = all_sections[0].id
        else:
            section_names = [f"  - id={s.id}: {s.name}" for s in leaves[:10]]
            return {
                "error": "section_id is required when multiple sections exist. "
                "Call list_sections first to find the right section. "
                "Available sections:\n" + "\n".join(section_names),
            }

    template = get_template_by_slug(template_slug)
    if not template:
        return {"error": f"Template '{template_slug}' not found. Use list_templates to see available options."}

    # Gather relevant context from knowledge base
    context_docs = ""
    if description:
        try:
            results = search_pages_bm25(org_id, description, db, limit=5)
            for r in results:
                page = db.get(Page, r["id"])
                if page:
                    text = _html_to_text(page.published_html or page.html_content or "")
                    if text:
                        context_docs += f"\n--- {page.title} ---\n{text[:1500]}\n"
        except Exception:
            pass  # proceed without context

    # Build the template content with title substituted
    template_content = template["content"].replace("{title}", title)

    # Use LLM to fill the template
    org = db.get(Organization, org_id)
    org_name = org.name if org else "the organization"

    fill_prompt = (
        f"You are a technical documentation writer for {org_name}. "
        f"Fill in the following documentation template with real, detailed content based on the description provided. "
        f"Keep the markdown structure and headings from the template. Replace placeholder text with actual content. "
        f"Start directly with the content — no preamble.\n\n"
        f"Template structure:\n{template_content}\n\n"
        f"Description of what to write: {description}\n"
    )
    if context_docs:
        fill_prompt += f"\nExisting documentation for style reference:\n{context_docs}"

    # Single-turn LLM call using per-org config
    llm_config = _resolve_llm_config(org_id, db)
    generated_content = await _llm_single_turn(fill_prompt, llm_config)

    # Create the draft using existing tool
    return await _tool_create_draft(
        {"title": title, "content": generated_content, "section_id": section_id},
        user, org_id, db,
    )


async def _llm_single_turn(prompt: str, llm_config: LLMConfig | None = None) -> str:
    """Single-turn LLM call using the given or default LLM config."""
    if llm_config is None:
        # Legacy fallback — use env vars directly (for calls without org context)
        from app.config import settings as _settings
        provider = (_settings.agent_provider or "gemini").lower()
        api_key = ""
        model = ""
        base_url = ""
        if provider == "gemini":
            api_key, model = _settings.gemini_api_key or "", _settings.gemini_model or "gemini-2.0-flash"
        elif provider == "groq":
            api_key, model = _settings.groq_api_key or "", _settings.groq_model or "meta-llama/llama-4-scout-17b-16e-instruct"
        elif provider == "anthropic":
            api_key, model = _settings.anthropic_api_key or "", _settings.anthropic_model or "claude-sonnet-4-5-20250514"
        elif provider == "openai_compat":
            api_key, model = _settings.openai_compat_api_key or "", _settings.openai_compat_model or ""
            base_url = _settings.openai_compat_base_url or ""
        llm_config = LLMConfig(provider=provider, api_key=api_key, model=model, base_url=base_url)

    if not llm_config.api_key:
        return "(No LLM provider configured — set up AI settings in workspace settings)"

    provider = llm_config.provider.lower()

    if provider == "anthropic":
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=llm_config.api_key)
        msg = await client.messages.create(
            model=llm_config.model or "claude-sonnet-4-5-20250514",
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text if msg.content else ""

    if provider in ("groq", "openai_compat"):
        import httpx
        base_url = llm_config.base_url or ("https://api.groq.com/openai" if provider == "groq" else "")
        if not base_url:
            return "(No base URL configured for OpenAI-compatible provider)"
        async with httpx.AsyncClient() as http:
            resp = await http.post(
                f"{base_url.rstrip('/')}/v1/chat/completions",
                headers={"Authorization": f"Bearer {llm_config.api_key}"},
                json={
                    "model": llm_config.model or "meta-llama/llama-4-scout-17b-16e-instruct",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 4096,
                },
                timeout=60.0,
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]

    # Default: Gemini
    from google import genai
    client = genai.Client(api_key=llm_config.api_key)
    resp = client.models.generate_content(
        model=llm_config.model or "gemini-2.0-flash",
        contents=prompt,
    )
    return resp.text or ""


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

    if not title or not content:
        return {"error": "title and content are required"}

    # Resolve section
    if not section_id:
        # Only auto-pick if there's exactly one leaf section
        all_sections = db.query(Section).filter(
            Section.organization_id == org_id,
        ).order_by(Section.display_order).all()
        if not all_sections:
            return {"error": "No sections exist. Create a section first using create_section."}
        # Find leaf sections (no children)
        parent_ids = {s.parent_id for s in all_sections if s.parent_id}
        leaves = [s for s in all_sections if s.id not in parent_ids]
        if len(leaves) == 1:
            section_id = leaves[0].id
        elif len(all_sections) == 1:
            section_id = all_sections[0].id
        else:
            section_names = [f"  - id={s.id}: {s.name}" for s in leaves[:10]]
            return {
                "error": "section_id is required when multiple sections exist. "
                "Call list_sections first to find the right section. "
                "Available sections:\n" + "\n".join(section_names),
            }

    try:
        section_id_int = int(section_id)
    except (ValueError, TypeError):
        return {"error": f"Invalid section_id '{section_id}'. Must be a numeric ID. Use list_sections to find section IDs."}

    section = db.query(Section).filter(
        Section.id == section_id_int,
        Section.organization_id == org_id,
    ).first()
    if not section:
        return {"error": f"Section {section_id} not found. Use list_sections to see available sections."}

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

        # Grant the requesting user writer access to the new doc
        if user.email:
            try:
                drive_service.permissions().create(
                    fileId=doc_id,
                    body={"type": "user", "role": "writer", "emailAddress": user.email},
                    sendNotificationEmail=False,
                    supportsAllDrives=True,
                ).execute()
            except Exception as perm_exc:
                logger.warning("Could not share doc with %s: %s", user.email, perm_exc)

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
        "section_id": section.id,
        "section_name": section.name,
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


async def _tool_web_search(
    input_data: dict, user: User, org_id: int, db: Session,
) -> dict:
    query = (input_data.get("query") or "").strip()
    if not query:
        return {"error": "query is required"}
    max_results = min(int(input_data.get("max_results") or 5), 10)
    try:
        from ddgs import DDGS
        with DDGS() as ddgs:
            raw = list(ddgs.text(query, max_results=max_results))
        results = [
            {"title": r.get("title", ""), "url": r.get("href", ""), "snippet": r.get("body", "")}
            for r in raw
        ]
        return {"query": query, "results": results, "count": len(results)}
    except Exception as exc:
        logger.warning("Web search failed: %s", exc)
        return {"error": f"Web search failed: {exc}"}


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

    from slugify import slugify as _slugify

    slug = _slugify(name, max_length=200)
    # Ensure slug uniqueness within the org
    existing = db.query(Section).filter(
        Section.organization_id == org_id,
        Section.slug == slug,
    ).first()
    if existing:
        slug = f"{slug}-{db.query(func.count(Section.id)).filter(Section.organization_id == org_id).scalar()}"

    section = Section(
        organization_id=org_id,
        name=name,
        slug=slug,
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
    "search_knowledge_base": _tool_search_knowledge_base,
    "list_templates": _tool_list_templates,
    "create_from_template": _tool_create_from_template,
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
    "web_search": _tool_web_search,
}

TOOL_FRIENDLY_NAMES = {
    "list_sections": "Browsing sections",
    "list_pages": "Listing pages",
    "read_page": "Reading page",
    "search_docs": "Searching documentation",
    "search_knowledge_base": "Searching knowledge base",
    "list_templates": "Listing templates",
    "create_from_template": "Creating from template",
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
    "web_search": "Searching the web",
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
        f"- EXPLORE: Browse sections and pages, read page content, search across all docs, search knowledge base for semantic matches, list team members\n"
        f"- CREATE: Write new draft pages (backed by Google Docs), create new sections, generate from templates\n"
        f"- MANAGE: Move pages between sections, rename pages, reorder pages, duplicate pages as templates\n"
        f"- PUBLISH: Publish drafts to the public docs site, unpublish pages, sync content from Google Drive\n"
        f"- DELETE: Remove outdated pages (with user confirmation)\n"
        f"- INTEGRATE: Fetch Jira tickets for context when writing docs\n"
        f"- WEB SEARCH: Search the internet for up-to-date information, release notes, external references, and anything not in the internal docs\n\n"
        f"CRITICAL RULES:\n"
        f"- BEFORE creating any page or draft, you MUST call list_sections first to see all available sections and their IDs.\n"
        f"- ALWAYS pass the correct numeric section_id when calling create_draft or create_from_template. NEVER omit it.\n"
        f"- Section IDs are numbers (e.g. 5, 12), NOT names or slugs. Get them from list_sections.\n"
        f"- Place pages in the most specific/relevant leaf section. If the user says 'in Getting Started', find the section named 'Getting Started' and use its ID.\n"
        f"- If the user specifies where to place content, use that exact section. If unclear, ask which section they want.\n"
        f"- After creating a draft, tell the user the title AND which section it was placed in.\n\n"
        f"Guidelines:\n"
        f"- ALWAYS use your tools to answer questions. Never say you don't have access — you DO have tools to read pages, search content, list sections, etc.\n"
        f"- When asked to summarize or describe docs, use list_sections and list_pages to discover content, then use read_page on each page to read the actual text. Summarize from the real content.\n"
        f"- Use search_knowledge_base to find relevant pages by topic. Use read_page to read full page content by ID.\n"
        f"- Before writing new content, use search_knowledge_base to find relevant existing pages for style and content reference.\n"
        f"- When generating documentation, cite and link to related existing pages where relevant.\n"
        f"- When the user wants to create structured documentation, suggest relevant templates using list_templates.\n"
        f"- Be proactive. When asked to write docs, first explore existing docs to match style and structure.\n"
        f"- Write clear, concise technical documentation with proper headings (# ## ###).\n"
        f"- When asked about external topics, current releases, news, or anything not in the internal docs, use web_search to find up-to-date information.\n"
        f"- When the user mentions a Jira ticket (e.g. PROJ-123), fetch it first for context.\n"
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
    *,
    llm_config: LLMConfig | None = None,
) -> AsyncGenerator[dict, None]:
    """Run agent loop using Google Gemini."""
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise HTTPException(status_code=503, detail="google-genai not installed") from exc

    cfg = llm_config or _resolve_llm_config(org_id, db)
    client = genai.Client(api_key=cfg.api_key)
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
            model=cfg.model or "gemini-2.0-flash",
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

            # Coerce string IDs to integers (Gemini sometimes sends "16" instead of 16)
            for key in ("section_id", "page_id", "parent_id", "display_order", "limit", "member_id", "max_results"):
                val = tool_input.get(key)
                if val is None:
                    tool_input.pop(key, None)
                elif isinstance(val, str):
                    try:
                        tool_input[key] = int(val)
                    except (ValueError, TypeError):
                        tool_input.pop(key, None)

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

                # Coerce string IDs to integers (some models send "16" instead of 16)
                # and strip null values for optional params
                for key in ("section_id", "page_id", "parent_id", "display_order", "limit", "member_id", "max_results"):
                    val = tool_input.get(key)
                    if val is None:
                        tool_input.pop(key, None)
                    elif isinstance(val, str):
                        try:
                            tool_input[key] = int(val)
                        except (ValueError, TypeError):
                            tool_input.pop(key, None)
                # Also fix the arguments in the tool call so Groq doesn't reject them on the next turn
                tc["function"]["arguments"] = json.dumps(tool_input)

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
    *,
    llm_config: LLMConfig | None = None,
) -> AsyncGenerator[dict, None]:
    """Run agent loop using Groq (free tier, OpenAI-compatible)."""
    cfg = llm_config or _resolve_llm_config(org_id, db)
    async for event in _run_openai_compat_loop(
        message, history, user, org_id, db,
        base_url="https://api.groq.com/openai",
        model=cfg.model or "meta-llama/llama-4-scout-17b-16e-instruct",
        api_key=cfg.api_key,
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
    *,
    llm_config: LLMConfig | None = None,
) -> AsyncGenerator[dict, None]:
    """Run agent loop using Anthropic Claude."""
    try:
        import anthropic  # type: ignore
    except ImportError as exc:
        raise HTTPException(status_code=503, detail="anthropic not installed") from exc

    cfg = llm_config or _resolve_llm_config(org_id, db)
    client = anthropic.AsyncAnthropic(api_key=cfg.api_key)
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
            model=cfg.model or "claude-sonnet-4-5-20250514",
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
            tool_input = dict(block.input) if block.input else {}

            # Coerce string IDs to integers
            for key in ("section_id", "page_id", "parent_id", "display_order", "limit", "member_id", "max_results"):
                val = tool_input.get(key)
                if val is None:
                    tool_input.pop(key, None)
                elif isinstance(val, str):
                    try:
                        tool_input[key] = int(val)
                    except (ValueError, TypeError):
                        tool_input.pop(key, None)

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

    org_id = _resolve_org_id(user, db, x_org_id)

    # Resolve LLM config (per-org BYOK first, then env-var fallback)
    llm_cfg = _resolve_llm_config(org_id, db)

    if not llm_cfg.api_key:
        raise HTTPException(
            status_code=503,
            detail="AI agent not configured — ask your workspace admin to set up AI settings",
        )

    provider = llm_cfg.provider.lower()
    valid_providers = ("gemini", "groq", "anthropic", "openai_compat")
    if provider not in valid_providers:
        raise HTTPException(status_code=503, detail=f"Unknown agent provider: {provider}. Valid: {', '.join(valid_providers)}")

    # Rate limit check
    _check_rate_limit(org_id)

    # Select the right agent loop, passing resolved config
    if provider == "openai_compat":
        async def run_loop(msg, hist, usr, oid, d):
            async for ev in _run_openai_compat_loop(
                msg, hist, usr, oid, d,
                base_url=llm_cfg.base_url or settings.openai_compat_base_url,
                model=llm_cfg.model or settings.openai_compat_model,
                api_key=llm_cfg.api_key,
                provider_name="OpenAI-compat",
            ):
                yield ev
    else:
        _loop_fn = {
            "gemini": _run_gemini_loop,
            "groq": _run_groq_loop,
            "anthropic": _run_anthropic_loop,
        }[provider]

        async def run_loop(msg, hist, usr, oid, d):
            async for ev in _loop_fn(msg, hist, usr, oid, d, llm_config=llm_cfg):
                yield ev

    async def event_generator():
        assistant_text = ""
        all_events: list[dict] = []
        try:
            async for event in run_loop(
                body.message, body.history, user, org_id, db,
            ):
                all_events.append(event)
                if event.get("type") == "text_delta":
                    assistant_text += event.get("text", "")
                yield {"event": "message", "data": json.dumps(event)}
        except Exception as exc:
            logger.error("Agent chat error: %s", exc, exc_info=True)
            yield {
                "event": "message",
                "data": json.dumps({"type": "error", "message": str(exc)}),
            }

        # Auto-save conversation
        try:
            from app.models import AgentConversation
            conv_id = body.conversation_id

            # Build updated history for LLM context
            updated_history = list(body.history) + [
                {"role": "user", "content": body.message},
            ]
            if assistant_text:
                updated_history.append({"role": "assistant", "content": assistant_text})
            # Keep last 40 for LLM context
            updated_history = updated_history[-40:]

            # Build UI items from events
            ui_items: list[dict] = [{"role": "user", "content": body.message}]
            for ev in all_events:
                etype = ev.get("type")
                if etype == "text_delta":
                    # Accumulate into last assistant message
                    if ui_items and ui_items[-1].get("role") == "assistant":
                        ui_items[-1]["content"] += ev.get("text", "")
                    else:
                        ui_items.append({"role": "assistant", "content": ev.get("text", "")})
                elif etype == "tool_start":
                    ui_items.append({
                        "type": "tool",
                        "toolName": ev.get("tool_name", ""),
                        "friendlyName": ev.get("tool_name", ""),
                    })
                elif etype == "tool_result":
                    # Update last matching tool item
                    for item in reversed(ui_items):
                        if item.get("type") == "tool" and item.get("toolName") == ev.get("tool_name"):
                            item["success"] = ev.get("success")
                            break
                elif etype == "draft_created":
                    ui_items.append({
                        "type": "draft",
                        "pageId": ev.get("page_id"),
                        "title": ev.get("title", ""),
                        "googleDocId": ev.get("google_doc_id", ""),
                    })

            if conv_id:
                conv = db.query(AgentConversation).filter(
                    AgentConversation.id == conv_id,
                    AgentConversation.user_id == user.id,
                ).first()
                if conv:
                    # Append new items to existing messages
                    existing = json.loads(conv.messages or "[]")
                    existing.extend(ui_items)
                    conv.messages = json.dumps(existing)
                    conv.history = json.dumps(updated_history)
                    db.commit()
            else:
                # Create new conversation
                title = body.message[:60].strip()
                if len(body.message) > 60:
                    title += "..."
                conv = AgentConversation(
                    organization_id=org_id,
                    user_id=user.id,
                    title=title,
                    messages=json.dumps(ui_items),
                    history=json.dumps(updated_history),
                )
                db.add(conv)
                db.commit()
                db.refresh(conv)
                conv_id = conv.id

            yield {
                "event": "message",
                "data": json.dumps({"type": "conversation_saved", "conversation_id": conv_id}),
            }
        except Exception as exc:
            logger.warning("Failed to save conversation: %s", exc)

    return EventSourceResponse(event_generator())
