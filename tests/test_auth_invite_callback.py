"""Tests for invite-aware auth callback helper."""

from datetime import datetime, timedelta, timezone

from app.auth.routes import _resolve_pending_invitation_for_callback
from app.models import Invitation, Organization, User


def _seed_org_and_invitation(db, *, email: str = "member@acceldata.io", accepted: bool = False, expired: bool = False):
    owner = User(google_id="owner-invite-flow", email="owner@acceldata.io", name="Owner")
    requester = User(google_id="member-invite-flow", email=email, name="Member")
    org = Organization(name="Invite Org", slug="invite-org", owner_id=1, drive_folder_id="drive-invite")
    db.add_all([owner, requester, org])
    db.flush()
    org.owner_id = owner.id

    invitation = Invitation(
        organization_id=org.id,
        invited_by_id=owner.id,
        email=email,
        role="viewer",
        token="invite-token-123",
        accepted_at=datetime.now(timezone.utc) if accepted else None,
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1) if expired else datetime.now(timezone.utc) + timedelta(days=1),
    )
    db.add(invitation)
    db.commit()
    return invitation


def test_resolve_pending_invitation_for_callback_accepts_valid_token(db):
    invitation = _seed_org_and_invitation(db)
    resolved, error = _resolve_pending_invitation_for_callback(
        db,
        user_email="member@acceldata.io",
        invite_token="invite-token-123",
    )
    assert error is None
    assert resolved is not None
    assert resolved.id == invitation.id


def test_resolve_pending_invitation_for_callback_rejects_email_mismatch(db):
    _seed_org_and_invitation(db, email="member@acceldata.io")
    resolved, error = _resolve_pending_invitation_for_callback(
        db,
        user_email="other@acceldata.io",
        invite_token="invite-token-123",
    )
    assert resolved is None
    assert error == "Invitation email does not match the signed-in account."


def test_resolve_pending_invitation_for_callback_rejects_expired_token(db):
    _seed_org_and_invitation(db, expired=True)
    resolved, error = _resolve_pending_invitation_for_callback(
        db,
        user_email="member@acceldata.io",
        invite_token="invite-token-123",
    )
    assert resolved is None
    assert error == "Invitation has expired."

