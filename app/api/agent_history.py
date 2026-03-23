"""Agent conversation history — CRUD for persisted chat sessions."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth.routes import get_current_user
from app.database import get_db
from app.models import AgentConversation, OrgRole, User

router = APIRouter(tags=["agent-history"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_org_id(user: User, db: Session, x_org_id: int | None) -> int:
    query = db.query(OrgRole).filter(OrgRole.user_id == user.id)
    if x_org_id is not None:
        query = query.filter(OrgRole.organization_id == x_org_id)
    role = query.first()
    if not role:
        raise HTTPException(status_code=403, detail="No organization access")
    return role.organization_id


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class ConversationSummary(BaseModel):
    id: int
    title: str
    updated_at: str


class ConversationFull(BaseModel):
    id: int
    title: str
    messages: str  # JSON string
    history: str   # JSON string
    created_at: str
    updated_at: str


class ConversationUpdate(BaseModel):
    title: str | None = None
    messages: str | None = None  # JSON string
    history: str | None = None   # JSON string


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/api/agent/conversations")
async def list_conversations(
    x_org_id: int | None = Header(default=None, alias="X-Org-Id"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[ConversationSummary]:
    org_id = _resolve_org_id(user, db, x_org_id)
    convos = (
        db.query(AgentConversation)
        .filter(
            AgentConversation.organization_id == org_id,
            AgentConversation.user_id == user.id,
        )
        .order_by(AgentConversation.updated_at.desc())
        .limit(50)
        .all()
    )
    return [
        ConversationSummary(
            id=c.id,
            title=c.title,
            updated_at=str(c.updated_at),
        )
        for c in convos
    ]


@router.get("/api/agent/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: int,
    x_org_id: int | None = Header(default=None, alias="X-Org-Id"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ConversationFull:
    org_id = _resolve_org_id(user, db, x_org_id)
    c = db.query(AgentConversation).filter(
        AgentConversation.id == conversation_id,
        AgentConversation.organization_id == org_id,
        AgentConversation.user_id == user.id,
    ).first()
    if not c:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return ConversationFull(
        id=c.id,
        title=c.title,
        messages=c.messages,
        history=c.history,
        created_at=str(c.created_at),
        updated_at=str(c.updated_at),
    )


@router.patch("/api/agent/conversations/{conversation_id}")
async def update_conversation(
    conversation_id: int,
    body: ConversationUpdate,
    x_org_id: int | None = Header(default=None, alias="X-Org-Id"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    org_id = _resolve_org_id(user, db, x_org_id)
    c = db.query(AgentConversation).filter(
        AgentConversation.id == conversation_id,
        AgentConversation.organization_id == org_id,
        AgentConversation.user_id == user.id,
    ).first()
    if not c:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if body.title is not None:
        c.title = body.title
    if body.messages is not None:
        c.messages = body.messages
    if body.history is not None:
        c.history = body.history
    db.commit()
    return {"ok": True, "id": c.id}


@router.delete("/api/agent/conversations/{conversation_id}")
async def delete_conversation(
    conversation_id: int,
    x_org_id: int | None = Header(default=None, alias="X-Org-Id"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    org_id = _resolve_org_id(user, db, x_org_id)
    c = db.query(AgentConversation).filter(
        AgentConversation.id == conversation_id,
        AgentConversation.organization_id == org_id,
        AgentConversation.user_id == user.id,
    ).first()
    if not c:
        raise HTTPException(status_code=404, detail="Conversation not found")
    db.delete(c)
    db.commit()
    return {"ok": True}
