"""Regression tests for Drive ACL sync on organization membership changes."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import jwt
from googleapiclient.errors import HttpError

from app.api import org as org_api
from app.api import drive as drive_api
from app.config import settings
from app.models import GoogleToken, Invitation, OrgRole, Organization, Page, Section, User
from app.services import drive as drive_service
from app.services import drive_acl


def _auth_header(user_id: int, email: str) -> dict[str, str]:
    token = jwt.encode(
        {
            "sub": str(user_id),
            "email": email,
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            "iat": datetime.now(timezone.utc),
        },
        settings.secret_key,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


def test_accept_invitation_triggers_drive_acl_sync(client, db, monkeypatch):
    owner = User(google_id="owner-drive-sync", email="owner@example.com", name="Owner")
    invitee = User(google_id="invitee-drive-sync", email="invitee@example.com", name="Invitee")
    org = Organization(name="Sync Org", slug="sync-org", drive_folder_id="drive-root-1", owner_id=1)
    db.add_all([owner, invitee, org])
    db.flush()
    org.owner_id = owner.id
    db.add(OrgRole(organization_id=org.id, user_id=owner.id, role="owner"))
    invitation = Invitation(
        organization_id=org.id,
        invited_by_id=owner.id,
        email=invitee.email,
        role="editor",
        token="invite-token-sync-1",
        expires_at=datetime.now(timezone.utc) + timedelta(days=2),
    )
    db.add(invitation)
    db.commit()

    calls: list[dict] = []

    async def _fake_sync_member_drive_permission(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "status": "created", "drive_role": "writer"}

    monkeypatch.setattr(org_api, "sync_member_drive_permission", _fake_sync_member_drive_permission)

    response = client.post(
        "/api/org/invitations/accept",
        json={"token": invitation.token},
        headers=_auth_header(invitee.id, invitee.email),
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "accepted"
    assert payload["drive_sync"]["ok"] is True
    assert payload["drive_sync"]["status"] == "created"
    assert len(calls) == 1
    assert calls[0]["member_email"] == invitee.email
    assert calls[0]["org_role"] == "editor"


def test_update_member_role_triggers_drive_acl_sync(client, db, monkeypatch):
    owner = User(google_id="owner-role-sync", email="owner2@example.com", name="Owner2")
    member = User(google_id="member-role-sync", email="member2@example.com", name="Member2")
    org = Organization(name="Role Sync Org", slug="role-sync-org", drive_folder_id="drive-root-2", owner_id=1)
    db.add_all([owner, member, org])
    db.flush()
    org.owner_id = owner.id
    db.add_all(
        [
            OrgRole(organization_id=org.id, user_id=owner.id, role="owner"),
            OrgRole(organization_id=org.id, user_id=member.id, role="viewer"),
        ]
    )
    db.commit()

    target_role = (
        db.query(OrgRole)
        .filter(OrgRole.organization_id == org.id, OrgRole.user_id == member.id)
        .first()
    )
    assert target_role is not None

    async def _fake_sync_member_drive_permission(**kwargs):
        return {"ok": True, "status": "updated", "drive_role": "writer"}

    monkeypatch.setattr(org_api, "sync_member_drive_permission", _fake_sync_member_drive_permission)

    response = client.patch(
        f"/api/org/members/{target_role.id}/role",
        json={"role": "editor"},
        headers=_auth_header(owner.id, owner.email),
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["role"] == "editor"
    assert payload["drive_sync"]["ok"] is True
    assert payload["drive_sync"]["status"] == "updated"


def test_remove_member_triggers_drive_acl_revoke(client, db, monkeypatch):
    owner = User(google_id="owner-remove-sync", email="owner3@example.com", name="Owner3")
    member = User(google_id="member-remove-sync", email="member3@example.com", name="Member3")
    org = Organization(name="Remove Sync Org", slug="remove-sync-org", drive_folder_id="drive-root-3", owner_id=1)
    db.add_all([owner, member, org])
    db.flush()
    org.owner_id = owner.id
    db.add_all(
        [
            OrgRole(organization_id=org.id, user_id=owner.id, role="owner"),
            OrgRole(organization_id=org.id, user_id=member.id, role="editor"),
        ]
    )
    db.commit()

    member_role = (
        db.query(OrgRole)
        .filter(OrgRole.organization_id == org.id, OrgRole.user_id == member.id)
        .first()
    )
    assert member_role is not None

    revoke_calls: list[dict] = []

    async def _fake_revoke_member_drive_permission(**kwargs):
        revoke_calls.append(kwargs)
        return {"ok": True, "status": "removed"}

    monkeypatch.setattr(org_api, "revoke_member_drive_permission", _fake_revoke_member_drive_permission)

    response = client.delete(
        f"/api/org/members/{member_role.id}",
        headers=_auth_header(owner.id, owner.email),
    )
    assert response.status_code == 204
    assert len(revoke_calls) == 1
    assert revoke_calls[0]["member_email"] == member.email


def test_drive_acl_sync_retries_with_owner_token_when_invitee_cannot_read_root(db, monkeypatch):
    owner = User(google_id="owner-acl-fallback", email="owner-fallback@example.com", name="Owner")
    invitee = User(google_id="invitee-acl-fallback", email="invitee-fallback@example.com", name="Invitee")
    org = Organization(
        name="ACL Retry Org",
        slug="acl-retry-org",
        drive_folder_id="drive-root-retry",
        owner_id=1,
    )
    db.add_all([owner, invitee, org])
    db.flush()
    org.owner_id = owner.id
    db.add_all(
        [
            OrgRole(organization_id=org.id, user_id=owner.id, role="owner"),
            OrgRole(organization_id=org.id, user_id=invitee.id, role="editor"),
            GoogleToken(
                user_id=invitee.id,
                organization_id=org.id,
                encrypted_refresh_token="invitee-token",
                scope="https://www.googleapis.com/auth/drive",
                token_created_at=datetime.now(timezone.utc),
                last_refreshed_at=datetime.now(timezone.utc),
            ),
            GoogleToken(
                user_id=owner.id,
                organization_id=org.id,
                encrypted_refresh_token="owner-token",
                scope="https://www.googleapis.com/auth/drive",
                token_created_at=datetime.now(timezone.utc),
                last_refreshed_at=datetime.now(timezone.utc),
            ),
        ]
    )
    db.commit()

    create_calls: list[int] = []

    class _FakeDriveService:
        def __init__(self, actor_user_id: int):
            self.actor_user_id = actor_user_id
            self._action = None

        def permissions(self):
            return self

        def create(self, **kwargs):
            self._action = "create"
            return self

        def execute(self):
            if self._action == "create":
                create_calls.append(self.actor_user_id)
                return {"id": "perm-1", "role": "writer"}
            return {}

    async def _fake_build_drive_service_for_token(db, token):
        return _FakeDriveService(token.user_id)

    def _fake_find_user_permission(service, folder_id, email):
        if service.actor_user_id == invitee.id:
            raise HttpError(
                resp=SimpleNamespace(status=403, reason="Forbidden"),
                content=b'{"error":{"message":"insufficientFilePermissions"}}',
            )
        return None, None

    monkeypatch.setattr(drive_acl, "_build_drive_service_for_token", _fake_build_drive_service_for_token)
    monkeypatch.setattr(drive_acl, "_find_user_permission", _fake_find_user_permission)

    result = asyncio.run(
        drive_acl.sync_member_drive_permission(
            db=db,
            org=org,
            member_email=invitee.email,
            org_role="editor",
            preferred_user_ids=[invitee.id, owner.id],
        )
    )

    assert result["ok"] is True
    assert result["status"] == "created"
    assert result["actor_user_id"] == owner.id
    assert create_calls == [owner.id]


def test_drive_acl_sync_uses_member_token_from_other_workspace_when_org_token_missing(db, monkeypatch):
    owner = User(google_id="owner-cross-org-token", email="owner-cross@example.com", name="Owner")
    member = User(google_id="member-cross-org-token", email="member-cross@example.com", name="Member")
    org_primary = Organization(
        name="Primary Org",
        slug="primary-org",
        drive_folder_id="drive-root-primary",
        owner_id=1,
    )
    org_secondary = Organization(
        name="Secondary Org",
        slug="secondary-org",
        drive_folder_id="drive-root-secondary",
        owner_id=1,
    )
    db.add_all([owner, member, org_primary, org_secondary])
    db.flush()
    org_primary.owner_id = owner.id
    org_secondary.owner_id = owner.id
    db.add_all(
        [
            OrgRole(organization_id=org_primary.id, user_id=owner.id, role="owner"),
            OrgRole(organization_id=org_primary.id, user_id=member.id, role="editor"),
            OrgRole(organization_id=org_secondary.id, user_id=member.id, role="editor"),
            # Member has a valid token, but only stored under another workspace.
            GoogleToken(
                user_id=member.id,
                organization_id=org_secondary.id,
                encrypted_refresh_token="member-secondary-token",
                scope="https://www.googleapis.com/auth/drive",
                token_created_at=datetime.now(timezone.utc),
                last_refreshed_at=datetime.now(timezone.utc),
            ),
        ]
    )
    db.commit()

    create_calls: list[int] = []

    class _FakeDriveService:
        def __init__(self, actor_user_id: int):
            self.actor_user_id = actor_user_id
            self._action = None

        def permissions(self):
            return self

        def create(self, **kwargs):
            self._action = "create"
            return self

        def execute(self):
            if self._action == "create":
                create_calls.append(self.actor_user_id)
                return {"id": "perm-x", "role": "writer"}
            return {}

    async def _fake_build_drive_service_for_token(db, token):
        return _FakeDriveService(token.user_id)

    def _fake_find_user_permission(service, folder_id, email):
        return None, None

    monkeypatch.setattr(drive_acl, "_build_drive_service_for_token", _fake_build_drive_service_for_token)
    monkeypatch.setattr(drive_acl, "_find_user_permission", _fake_find_user_permission)

    result = asyncio.run(
        drive_acl.sync_member_drive_permission(
            db=db,
            org=org_primary,
            member_email=member.email,
            org_role="editor",
            preferred_user_ids=[member.id, owner.id],
        )
    )

    assert result["ok"] is True
    assert result["actor_user_id"] == member.id
    assert create_calls == [member.id]


def test_ensure_doc_access_prefers_page_org_when_selected_workspace_is_stale(db, monkeypatch):
    user = User(google_id="ensure-access-user", email="member@example.com", name="Member")
    owner = User(google_id="ensure-access-owner", email="owner@example.com", name="Owner")
    org_a = Organization(name="Org A", slug="org-a", drive_folder_id="drive-root-a", owner_id=1)
    org_b = Organization(name="Org B", slug="org-b", drive_folder_id="drive-root-b", owner_id=1)
    db.add_all([user, owner, org_a, org_b])
    db.flush()
    org_a.owner_id = owner.id
    org_b.owner_id = owner.id
    db.add_all(
        [
            OrgRole(organization_id=org_a.id, user_id=user.id, role="editor"),
            OrgRole(organization_id=org_b.id, user_id=user.id, role="editor"),
            OrgRole(organization_id=org_b.id, user_id=owner.id, role="owner"),
        ]
    )
    section = Section(
        organization_id=org_b.id,
        name="Product",
        slug="product",
        visibility="public",
    )
    db.add(section)
    db.flush()
    page = Page(
        organization_id=org_b.id,
        section_id=section.id,
        google_doc_id="doc-from-org-b",
        title="Doc B",
        slug="doc-b",
        owner_id=owner.id,
    )
    db.add(page)
    db.commit()

    seen: dict[str, int] = {}

    async def _fake_sync_member_drive_permission(**kwargs):
        seen["root_org_id"] = kwargs["org"].id
        seen["root_preferred_ids"] = kwargs.get("preferred_user_ids", [])
        return {"ok": True, "status": "created", "drive_role": "writer"}

    async def _fake_sync_member_drive_file_permission(**kwargs):
        seen["file_org_id"] = kwargs["org"].id
        seen["file_preferred_ids"] = kwargs.get("preferred_user_ids", [])
        return {"ok": True, "status": "created", "drive_role": "writer"}

    class _FakeGoogleDriveService:
        def __init__(self, db_session, current_user):
            self.db = db_session
            self.user = current_user

        async def get_credentials(self, _token):
            return None

    monkeypatch.setattr(drive_service, "GoogleDriveService", _FakeGoogleDriveService)
    monkeypatch.setattr(drive_service, "sync_member_drive_permission", _fake_sync_member_drive_permission)
    monkeypatch.setattr(drive_service, "sync_member_drive_file_permission", _fake_sync_member_drive_file_permission)

    payload = asyncio.run(
        drive_service.google_drive_handler(
            body={
                "action": "ensure_doc_access",
                "docId": "doc-from-org-b",
                # Simulate stale UI workspace selection pointing at org A.
                "_x_org_id": org_a.id,
            },
            db=db,
            user=user,
        )
    )
    assert payload["ok"] is True
    assert payload["orgId"] == org_b.id
    assert payload["pageId"] == page.id
    assert seen["root_org_id"] == org_b.id
    assert seen["file_org_id"] == org_b.id
    assert owner.id in seen["root_preferred_ids"]
    assert owner.id in seen["file_preferred_ids"]
    assert user.id in seen["root_preferred_ids"]


def test_get_drive_credentials_falls_back_to_owner_token_for_shared_member(db, monkeypatch):
    owner = User(google_id="owner-drive-creds", email="owner-creds@example.com", name="Owner")
    member = User(google_id="member-drive-creds", email="member-creds@example.com", name="Member")
    org = Organization(
        name="Shared Sync Org",
        slug="shared-sync-org",
        drive_folder_id="drive-root-shared",
        owner_id=1,
    )
    db.add_all([owner, member, org])
    db.flush()
    org.owner_id = owner.id
    db.add_all(
        [
            OrgRole(organization_id=org.id, user_id=owner.id, role="owner"),
            OrgRole(organization_id=org.id, user_id=member.id, role="editor"),
            # Token exists only for owner; member has none.
            GoogleToken(
                user_id=owner.id,
                organization_id=org.id,
                encrypted_refresh_token="owner-encrypted-refresh",
                scope="https://www.googleapis.com/auth/drive",
                token_created_at=datetime.now(timezone.utc),
                last_refreshed_at=datetime.now(timezone.utc),
            ),
        ]
    )
    db.commit()

    class _FakeEncryption:
        def decrypt(self, _cipher):
            return "owner-refresh-token"

    class _FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"access_token": "owner-access-token"}

    class _FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, *args, **kwargs):
            return _FakeResponse()

    monkeypatch.setattr(drive_api, "get_encryption_service", lambda: _FakeEncryption())
    monkeypatch.setattr(drive_api.httpx, "AsyncClient", lambda *args, **kwargs: _FakeAsyncClient())

    creds = asyncio.run(drive_api.get_drive_credentials(member, org.id, db))
    assert creds is not None
    assert creds.token == "owner-access-token"


def test_drive_status_connected_for_shared_member_when_owner_has_token(client, db):
    owner = User(google_id="owner-drive-status", email="owner-status@example.com", name="Owner")
    member = User(google_id="member-drive-status", email="member-status@example.com", name="Member")
    org = Organization(
        name="Shared Status Org",
        slug="shared-status-org",
        drive_folder_id="drive-root-status",
        owner_id=1,
    )
    db.add_all([owner, member, org])
    db.flush()
    org.owner_id = owner.id
    db.add_all(
        [
            OrgRole(organization_id=org.id, user_id=owner.id, role="owner"),
            OrgRole(organization_id=org.id, user_id=member.id, role="editor"),
            GoogleToken(
                user_id=owner.id,
                organization_id=org.id,
                encrypted_refresh_token="owner-token",
                scope="https://www.googleapis.com/auth/drive",
                token_created_at=datetime.now(timezone.utc),
                last_refreshed_at=datetime.now(timezone.utc),
            ),
        ]
    )
    db.commit()

    response = client.get(
        "/api/drive/status",
        headers={
            **_auth_header(member.id, member.email),
            "X-Org-Id": str(org.id),
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["connected"] is True
    assert payload["drive_folder_id"] == "drive-root-status"
