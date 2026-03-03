"""Member and invitation management functions."""

import secrets
from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session
from app.models import User, Invitation, OrgRole, ProjectMember, JoinRequest


async def create_invitation(body: dict, db: Session, user: User | None) -> dict:
    """Create organization invitation."""
    if not user:
        return {"ok": False, "error": "Authentication required"}

    try:
        # Find user's organization
        org_role = db.query(OrgRole).filter(OrgRole.user_id == user.id).first()
        if not org_role or org_role.role not in ["owner", "admin"]:
            return {"ok": False, "error": "Insufficient permissions"}

        email = body.get("email")
        role = body.get("role", "viewer")

        if not email:
            return {"ok": False, "error": "Email required"}

        invitation = Invitation(
            organization_id=org_role.organization_id,
            invited_by_id=user.id,
            email=email,
            role=role,
            token=secrets.token_urlsafe(32),
            expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        )
        db.add(invitation)
        db.commit()

        return {"ok": True, "invitation": {"id": invitation.id, "email": email}}

    except Exception as e:
        db.rollback()
        return {"ok": False, "error": str(e)}


async def create_project_invitation(body: dict, db: Session, user: User | None) -> dict:
    """Create project-level invitation."""
    if not user:
        return {"ok": False, "error": "Authentication required"}

    try:
        project_id = body.get("projectId")
        email = body.get("email")
        role = body.get("role", "viewer")

        if not project_id or not email:
            return {"ok": False, "error": "Project ID and email required"}

        invitation = Invitation(
            project_id=project_id,
            invited_by_id=user.id,
            email=email,
            role=role,
            token=secrets.token_urlsafe(32),
            expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        )
        db.add(invitation)
        db.commit()

        return {"ok": True, "invitation": {"id": invitation.id, "email": email}}

    except Exception as e:
        db.rollback()
        return {"ok": False, "error": str(e)}


async def remove_project_invitation(body: dict, db: Session, user: User | None) -> dict:
    """Revoke invitation."""
    if not user:
        return {"ok": False, "error": "Authentication required"}

    try:
        invitation_id = body.get("invitationId")
        if not invitation_id:
            return {"ok": False, "error": "Invitation ID required"}

        invitation = db.query(Invitation).filter(Invitation.id == invitation_id).first()
        if not invitation:
            return {"ok": False, "error": "Invitation not found"}

        db.delete(invitation)
        db.commit()

        return {"ok": True}

    except Exception as e:
        db.rollback()
        return {"ok": False, "error": str(e)}


async def update_member_role(body: dict, db: Session, user: User | None) -> dict:
    """Change organization member role."""
    if not user:
        return {"ok": False, "error": "Authentication required"}

    try:
        member_id = body.get("memberId")
        new_role = body.get("role")

        if not member_id or not new_role:
            return {"ok": False, "error": "Member ID and role required"}

        # Check if requester is owner/admin
        requester_role = db.query(OrgRole).filter(OrgRole.user_id == user.id).first()
        if not requester_role or requester_role.role not in ["owner", "admin"]:
            return {"ok": False, "error": "Insufficient permissions"}

        # Update member role
        member_role = db.query(OrgRole).filter(OrgRole.user_id == member_id).first()
        if not member_role:
            return {"ok": False, "error": "Member not found"}

        member_role.role = new_role
        db.commit()

        return {"ok": True}

    except Exception as e:
        db.rollback()
        return {"ok": False, "error": str(e)}


async def update_project_member_role(body: dict, db: Session, user: User | None) -> dict:
    """Change project member role."""
    if not user:
        return {"ok": False, "error": "Authentication required"}

    try:
        project_id = body.get("projectId")
        member_id = body.get("memberId")
        new_role = body.get("role")

        if not all([project_id, member_id, new_role]):
            return {"ok": False, "error": "Project ID, member ID, and role required"}

        member = db.query(ProjectMember).filter(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == member_id
        ).first()

        if not member:
            return {"ok": False, "error": "Member not found"}

        member.role = new_role
        db.commit()

        return {"ok": True}

    except Exception as e:
        db.rollback()
        return {"ok": False, "error": str(e)}


async def remove_project_member(body: dict, db: Session, user: User | None) -> dict:
    """Remove member from project."""
    if not user:
        return {"ok": False, "error": "Authentication required"}

    try:
        project_id = body.get("projectId")
        member_id = body.get("memberId")

        if not project_id or not member_id:
            return {"ok": False, "error": "Project ID and member ID required"}

        member = db.query(ProjectMember).filter(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == member_id
        ).first()

        if not member:
            return {"ok": False, "error": "Member not found"}

        db.delete(member)
        db.commit()

        return {"ok": True}

    except Exception as e:
        db.rollback()
        return {"ok": False, "error": str(e)}


async def list_join_requests(body: dict, db: Session, user: User | None) -> dict:
    """Get pending join requests."""
    if not user:
        return {"ok": False, "error": "Authentication required"}

    try:
        # Find user's organization
        org_role = db.query(OrgRole).filter(OrgRole.user_id == user.id).first()
        if not org_role:
            return {"ok": True, "requests": []}

        requests = db.query(JoinRequest).filter(
            JoinRequest.organization_id == org_role.organization_id,
            JoinRequest.status == "pending"
        ).all()

        request_list = []
        for req in requests:
            request_user = db.query(User).filter(User.id == req.user_id).first()
            request_list.append({
                "id": req.id,
                "user_id": req.user_id,
                "user_email": request_user.email if request_user else None,
                "user_name": request_user.name if request_user else None,
                "message": req.message,
                "status": req.status,
                "created_at": req.created_at.isoformat() if req.created_at else None,
            })

        return {"ok": True, "requests": request_list}

    except Exception as e:
        return {"ok": False, "error": str(e)}


async def get_project_share(body: dict, db: Session, user: User | None) -> dict:
    """Get project sharing information."""
    if not user:
        return {"ok": False, "error": "Authentication required"}

    try:
        project_id = body.get("projectId")
        if not project_id:
            return {"ok": False, "error": "Project ID required"}

        members = db.query(ProjectMember).filter(ProjectMember.project_id == project_id).all()

        member_list = []
        for m in members:
            member_user = db.query(User).filter(User.id == m.user_id).first()
            if member_user:
                member_list.append({
                    "id": m.id,
                    "user_id": m.user_id,
                    "email": member_user.email,
                    "name": member_user.name,
                    "role": m.role,
                })

        return {"ok": True, "members": member_list}

    except Exception as e:
        return {"ok": False, "error": str(e)}
