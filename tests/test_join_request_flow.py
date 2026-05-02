"""Join-request workflow tests for internal workspace access."""

import asyncio

from app.models import JoinRequest, OrgRole, Organization, User
from app.services import drive_acl, members


def _seed_org_with_owner(db):
    owner = User(google_id="owner-join-flow", email="owner@acceldata.io", name="Owner")
    admin = User(google_id="admin-join-flow", email="admin@acceldata.io", name="Admin")
    requester = User(google_id="requester-join-flow", email="requester@acceldata.io", name="Requester")
    org = Organization(name="Acceldata Workspace", slug="acceldata-workspace", owner_id=1, drive_folder_id="drive-root-join")

    db.add_all([owner, admin, requester, org])
    db.flush()

    org.owner_id = owner.id
    db.add(OrgRole(organization_id=org.id, user_id=owner.id, role="owner"))
    db.add(OrgRole(organization_id=org.id, user_id=admin.id, role="admin"))
    db.commit()

    return owner, admin, requester, org


def test_join_request_is_visible_to_owner(db):
    owner, _, requester, org = _seed_org_with_owner(db)

    created = asyncio.run(
        members.create_join_request(
            {"organizationId": org.id, "message": "Please grant access"},
            db,
            requester,
        )
    )
    assert created["ok"] is True

    listed = asyncio.run(
        members.list_join_requests({"organizationId": org.id}, db, owner)
    )
    assert listed["ok"] is True
    assert len(listed["requests"]) == 1
    assert listed["requests"][0]["user_email"] == requester.email


def test_approve_join_request_sets_viewer_and_syncs_drive_acl(db, monkeypatch):
    owner, _, requester, org = _seed_org_with_owner(db)

    created = asyncio.run(
        members.create_join_request({"organizationId": org.id}, db, requester)
    )
    assert created["ok"] is True
    request_id = created["request"]["id"]

    calls: list[dict] = []

    async def _fake_sync_member_drive_permission(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "status": "created", "drive_role": "reader"}

    monkeypatch.setattr(drive_acl, "sync_member_drive_permission", _fake_sync_member_drive_permission)

    approved = asyncio.run(
        members.approve_join_request(
            {"requestId": request_id, "organizationId": org.id},
            db,
            owner,
        )
    )

    assert approved["ok"] is True
    assert approved["role"] == "viewer"
    assert approved["drive_sync"]["ok"] is True

    membership = (
        db.query(OrgRole)
        .filter(OrgRole.organization_id == org.id, OrgRole.user_id == requester.id)
        .first()
    )
    assert membership is not None
    assert membership.role == "viewer"

    req = db.query(JoinRequest).filter(JoinRequest.id == int(request_id)).first()
    assert req is not None
    assert req.status == "approved"

    assert len(calls) == 1
    assert calls[0]["member_email"] == requester.email
    assert calls[0]["org_role"] == "viewer"


def test_approve_join_request_blocks_when_drive_sync_fails(db, monkeypatch):
    owner, _, requester, org = _seed_org_with_owner(db)

    created = asyncio.run(
        members.create_join_request({"organizationId": org.id}, db, requester)
    )
    assert created["ok"] is True
    request_id = created["request"]["id"]

    async def _fake_sync_member_drive_permission(**kwargs):
        return {"ok": False, "reason": "drive_acl_sync_failed"}

    monkeypatch.setattr(drive_acl, "sync_member_drive_permission", _fake_sync_member_drive_permission)

    approved = asyncio.run(
        members.approve_join_request(
            {"requestId": request_id, "organizationId": org.id},
            db,
            owner,
        )
    )

    assert approved["ok"] is False
    assert "Drive permissions could not be synced" in approved["error"]

    req = db.query(JoinRequest).filter(JoinRequest.id == int(request_id)).first()
    assert req is not None
    assert req.status == "pending"

    membership = (
        db.query(OrgRole)
        .filter(OrgRole.organization_id == org.id, OrgRole.user_id == requester.id)
        .first()
    )
    assert membership is None


def test_admin_can_approve_join_request(db, monkeypatch):
    _, admin, requester, org = _seed_org_with_owner(db)

    created = asyncio.run(
        members.create_join_request({"organizationId": org.id}, db, requester)
    )
    assert created["ok"] is True
    request_id = created["request"]["id"]

    async def _fake_sync_member_drive_permission(**kwargs):
        return {"ok": True, "status": "created", "drive_role": "reader"}

    monkeypatch.setattr(drive_acl, "sync_member_drive_permission", _fake_sync_member_drive_permission)

    approved = asyncio.run(
        members.approve_join_request(
            {"requestId": request_id, "organizationId": org.id},
            db,
            admin,
        )
    )

    assert approved["ok"] is True
    membership = (
        db.query(OrgRole)
        .filter(OrgRole.organization_id == org.id, OrgRole.user_id == requester.id)
        .first()
    )
    assert membership is not None
    assert membership.role == "viewer"
