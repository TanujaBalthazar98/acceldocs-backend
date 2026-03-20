"""Member and invitation management functions."""

import logging
import secrets
from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session
from app.models import User, Invitation, OrgRole, ProjectMember, JoinRequest, Organization

logger = logging.getLogger(__name__)


def _int(val) -> int | None:
    """Safely cast a value to int for PostgreSQL type safety."""
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def _selected_org_id(body: dict) -> int | None:
    """Read selected organization id from body payload or forwarded org header."""
    return (
        _int(body.get("_x_org_id"))
        or _int(body.get("organizationId"))
        or _int(body.get("organization_id"))
        or _int(body.get("orgId"))
        or _int(body.get("org_id"))
    )


def _resolve_org_role(
    db: Session,
    user: User,
    body: dict,
    *,
    required_roles: list[str] | None = None,
) -> OrgRole | None:
    """Resolve caller org membership, honoring selected workspace when provided."""
    query = db.query(OrgRole).filter(OrgRole.user_id == user.id)
    selected_org_id = _selected_org_id(body)
    if selected_org_id:
        query = query.filter(OrgRole.organization_id == selected_org_id)
    org_role = query.first()
    if not org_role:
        return None
    if required_roles and org_role.role not in required_roles:
        return None
    return org_role


async def create_invitation(body: dict, db: Session, user: User | None) -> dict:
    """Create organization invitation."""
    if not user:
        return {"ok": False, "error": "Authentication required"}

    try:
        org_role = _resolve_org_role(db, user, body, required_roles=["owner", "admin"])
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
        project_id = _int(body.get("projectId"))
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
        invitation_id = _int(body.get("invitationId"))
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
        member_id = _int(body.get("memberId"))
        new_role = body.get("role")

        if not member_id or not new_role:
            return {"ok": False, "error": "Member ID and role required"}

        # Check if requester is owner/admin
        requester_role = _resolve_org_role(db, user, body)
        if not requester_role:
            return {"ok": False, "error": "Insufficient permissions"}

        # If the org has no owner at all, allow any member to claim ownership
        # (fixes orphaned orgs where the first user was auto-joined as viewer)
        org_has_owner = db.query(OrgRole).filter(
            OrgRole.organization_id == requester_role.organization_id,
            OrgRole.role == "owner",
        ).first()
        if not org_has_owner and member_id == user.id and new_role == "owner":
            # Self-promotion to owner when org is ownerless — allowed
            pass
        elif requester_role.role not in ["owner", "admin"]:
            return {"ok": False, "error": "Insufficient permissions"}

        # Update member role
        member_role = db.query(OrgRole).filter(
            OrgRole.user_id == member_id,
            OrgRole.organization_id == requester_role.organization_id,
        ).first()
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
        project_id = _int(body.get("projectId"))
        member_id = _int(body.get("memberId"))
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
        project_id = _int(body.get("projectId"))
        member_id = _int(body.get("memberId"))

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
        org_role = _resolve_org_role(db, user, body, required_roles=["owner", "admin"])
        if not org_role:
            return {"ok": False, "error": "Insufficient permissions"}

        requests = db.query(JoinRequest).filter(
            JoinRequest.organization_id == org_role.organization_id,
            JoinRequest.status == "pending"
        ).order_by(JoinRequest.created_at.desc()).all()

        request_list = []
        for req in requests:
            request_user = db.query(User).filter(User.id == req.user_id).first()
            request_list.append({
                "id": str(req.id),
                "user_id": req.user_id,
                "user_email": request_user.email if request_user else None,
                "user_name": request_user.name if request_user else None,
                "message": req.message,
                "status": req.status,
                "requested_at": req.created_at.isoformat() if req.created_at else None,
                "created_at": req.created_at.isoformat() if req.created_at else None,
            })

        return {"ok": True, "requests": request_list}

    except Exception as e:
        return {"ok": False, "error": str(e)}


async def create_join_request(body: dict, db: Session, user: User | None) -> dict:
    """Submit a request to join an organization."""
    if not user:
        return {"ok": False, "error": "Authentication required"}

    try:
        org_id = _int(body.get("organizationId"))
        if not org_id:
            return {"ok": False, "error": "Organization ID required"}

        organization = db.query(Organization).filter(Organization.id == org_id).first()
        if not organization:
            return {"ok": False, "error": "Organization not found"}

        # Domain-claimed workspaces only allow same-domain requests
        org_domain = (organization.domain or "").strip().lower()
        if org_domain:
            user_email = (user.email or "").strip().lower()
            email_domain = user_email.split("@", 1)[1] if "@" in user_email else ""
            if email_domain != org_domain:
                return {"ok": False, "error": f"Only @{org_domain} addresses can request access"}

        # Don't allow if already a member
        existing_role = db.query(OrgRole).filter(
            OrgRole.user_id == user.id,
            OrgRole.organization_id == org_id,
        ).first()
        if existing_role:
            return {"ok": False, "error": "Already a member of this organization"}

        # Don't allow duplicate pending requests
        existing_req = db.query(JoinRequest).filter(
            JoinRequest.user_id == user.id,
            JoinRequest.organization_id == org_id,
            JoinRequest.status == "pending",
        ).first()
        if existing_req:
            return {"ok": False, "error": "You already have a pending request"}

        req = JoinRequest(
            organization_id=org_id,
            user_id=user.id,
            message=body.get("message", ""),
            status="pending",
        )
        db.add(req)
        db.commit()

        return {
            "ok": True,
            "request": {
                "id": str(req.id),
                "status": req.status,
                "organization_id": req.organization_id,
                "created_at": req.created_at.isoformat() if req.created_at else None,
            },
        }

    except Exception as e:
        db.rollback()
        return {"ok": False, "error": str(e)}


async def approve_join_request(body: dict, db: Session, user: User | None) -> dict:
    """Approve a pending join request — adds the user to the organization."""
    if not user:
        return {"ok": False, "error": "Authentication required"}

    try:
        request_id = _int(body.get("requestId") or body.get("id"))
        if not request_id:
            return {"ok": False, "error": "Request ID required"}

        # Requester must be owner/admin
        requester_role = _resolve_org_role(db, user, body, required_roles=["owner", "admin"])
        if not requester_role or requester_role.role not in ["owner", "admin"]:
            return {"ok": False, "error": "Insufficient permissions"}

        req = db.query(JoinRequest).filter(JoinRequest.id == request_id).first()
        if not req:
            return {"ok": False, "error": "Request not found"}
        if req.organization_id != requester_role.organization_id:
            return {"ok": False, "error": "Request does not belong to your organization"}
        if req.status != "pending":
            return {"ok": False, "error": f"Request already {req.status}"}

        # Add user to org as viewer
        role = str(body.get("role") or "viewer").strip().lower()
        if role not in {"viewer", "editor", "reviewer", "admin"}:
            role = "viewer"
        existing_membership = db.query(OrgRole).filter(
            OrgRole.organization_id == req.organization_id,
            OrgRole.user_id == req.user_id,
        ).first()
        if existing_membership:
            req.status = "approved"
            db.commit()
            # Still sync Drive access for existing members (may be missing)
            try:
                org = db.get(Organization, req.organization_id)
                member = db.get(User, req.user_id)
                if org and org.drive_folder_id and member:
                    from app.services.drive_acl import sync_member_drive_permission
                    await sync_member_drive_permission(
                        db=db,
                        org=org,
                        member_email=member.email,
                        org_role=existing_membership.role,
                        preferred_user_ids=[org.owner_id, user.id, req.user_id],
                    )
            except Exception as exc:
                logger.warning("Drive ACL sync for existing member %s failed: %s", req.user_id, exc)
            return {"ok": True, "already_member": True}
        org_role = OrgRole(
            organization_id=req.organization_id,
            user_id=req.user_id,
            role=role,
        )
        db.add(org_role)
        req.status = "approved"
        db.commit()

        # Grant Drive folder access to the newly approved member
        drive_sync = None
        try:
            org = db.get(Organization, req.organization_id)
            member = db.get(User, req.user_id)
            if org and org.drive_folder_id and member:
                from app.services.drive_acl import sync_member_drive_permission
                drive_sync = await sync_member_drive_permission(
                    db=db,
                    org=org,
                    member_email=member.email,
                    org_role=role,
                    preferred_user_ids=[org.owner_id, user.id, req.user_id],
                )
                if not drive_sync.get("ok"):
                    logger.warning(
                        "Drive ACL sync after join approval not fully successful for user %s in org %s: %s",
                        req.user_id, req.organization_id, drive_sync,
                    )
        except Exception as exc:
            logger.warning("Drive ACL sync after join approval failed for user %s: %s", req.user_id, exc)

        return {"ok": True, "drive_sync": drive_sync}

    except Exception as e:
        db.rollback()
        return {"ok": False, "error": str(e)}


async def reject_join_request(body: dict, db: Session, user: User | None) -> dict:
    """Reject a pending join request."""
    if not user:
        return {"ok": False, "error": "Authentication required"}

    try:
        request_id = _int(body.get("requestId") or body.get("id"))
        if not request_id:
            return {"ok": False, "error": "Request ID required"}

        requester_role = _resolve_org_role(db, user, body, required_roles=["owner", "admin"])
        if not requester_role or requester_role.role not in ["owner", "admin"]:
            return {"ok": False, "error": "Insufficient permissions"}

        req = db.query(JoinRequest).filter(JoinRequest.id == request_id).first()
        if not req:
            return {"ok": False, "error": "Request not found"}
        if req.organization_id != requester_role.organization_id:
            return {"ok": False, "error": "Request does not belong to your organization"}
        if req.status != "pending":
            return {"ok": False, "error": f"Request already {req.status}"}

        req.status = "rejected"
        db.commit()

        return {"ok": True}

    except Exception as e:
        db.rollback()
        return {"ok": False, "error": str(e)}


async def get_project_share(body: dict, db: Session, user: User | None) -> dict:
    """Get project sharing information."""
    if not user:
        return {"ok": False, "error": "Authentication required"}

    try:
        project_id = _int(body.get("projectId"))
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
