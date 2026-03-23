"""Inline writing assistant — single-turn text operations (rewrite, expand, etc.)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth.routes import get_current_user
from app.database import get_db
from app.models import OrgRole, User

router = APIRouter(tags=["agent-inline"])


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------

class InlineRequest(BaseModel):
    operation: str  # rewrite, expand, summarize, simplify, translate, fix_grammar
    selected_text: str
    context: str = ""  # surrounding page content for coherence
    language: str = ""  # target language for translate


class InlineResponse(BaseModel):
    ok: bool = True
    result: str
    operation: str


# ---------------------------------------------------------------------------
# Operation prompts
# ---------------------------------------------------------------------------

OPERATION_PROMPTS: dict[str, str] = {
    "rewrite": (
        "Rewrite the following text to be clearer and more professional, "
        "maintaining the same meaning and tone:\n\n{text}"
    ),
    "expand": (
        "Expand the following text with more detail, examples, and explanation. "
        "Maintain the same tone:\n\n{text}"
    ),
    "summarize": "Summarize the following text concisely:\n\n{text}",
    "simplify": (
        "Simplify the following text for a broader audience, using plain language "
        "and shorter sentences:\n\n{text}"
    ),
    "translate": "Translate the following text to {language}:\n\n{text}",
    "fix_grammar": (
        "Fix any grammar, spelling, and punctuation errors in the following text. "
        "Only fix errors — do not change the meaning or style:\n\n{text}"
    ),
}


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.post("/api/agent/inline", response_model=InlineResponse)
async def inline_assist(
    req: InlineRequest,
    x_org_id: int | None = Header(default=None, alias="X-Org-Id"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if req.operation not in OPERATION_PROMPTS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown operation: {req.operation}. "
            f"Valid: {', '.join(OPERATION_PROMPTS.keys())}",
        )

    if not req.selected_text.strip():
        raise HTTPException(status_code=400, detail="selected_text is required")

    # Resolve org for per-org LLM config
    query = db.query(OrgRole).filter(OrgRole.user_id == user.id)
    if x_org_id is not None:
        query = query.filter(OrgRole.organization_id == x_org_id)
    role = query.first()
    if not role:
        raise HTTPException(status_code=403, detail="User has no organization")
    org_id = role.organization_id

    prompt_template = OPERATION_PROMPTS[req.operation]
    prompt = prompt_template.format(
        text=req.selected_text,
        language=req.language or "English",
    )

    if req.context:
        prompt = (
            f"Context (surrounding content on the page):\n{req.context[:2000]}\n\n"
            + prompt
        )

    from app.api.agent_chat import _llm_single_turn, _resolve_llm_config

    llm_config = _resolve_llm_config(org_id, db)
    if not llm_config.api_key:
        raise HTTPException(
            status_code=503,
            detail="AI not configured — ask your workspace admin to set up AI settings",
        )

    result = await _llm_single_turn(prompt, llm_config)
    return InlineResponse(result=result, operation=req.operation)
