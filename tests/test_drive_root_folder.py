"""Tests for workspace Drive root folder updates."""

from datetime import datetime, timedelta, timezone

import jwt

from app.api import drive as drive_api
from app.config import settings
from app.models import OrgRole, Organization, User


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


def _fake_drive_service(*, folder_name: str = "Workspace Root", mime_type: str = "application/vnd.google-apps.folder"):
    class _Request:
        def execute(self):
            return {"id": "folder-id", "name": folder_name, "mimeType": mime_type}

    class _Files:
        def get(self, **kwargs):  # noqa: ARG002
            return _Request()

    class _Service:
        def files(self):
            return _Files()

    return _Service()


def test_owner_can_update_drive_root_folder(client, db, monkeypatch):
    owner = User(google_id="owner-root-update", email="owner-root@example.com", name="Owner")
    org = Organization(
        name="Root Update Org",
        slug="root-update-org",
        drive_folder_id="old-root-folder",
        owner_id=1,
    )
    db.add_all([owner, org])
    db.flush()
    org.owner_id = owner.id
    db.add(OrgRole(organization_id=org.id, user_id=owner.id, role="owner"))
    db.commit()

    async def _fake_get_drive_credentials(*args, **kwargs):  # noqa: ARG001
        return object()

    async def _fake_sync_org_drive_permissions(*, db, org, preferred_user_ids=()):  # noqa: ARG001
        return {"ok": True, "synced": 1, "failed": 0}

    monkeypatch.setattr(drive_api, "get_drive_credentials", _fake_get_drive_credentials)
    monkeypatch.setattr(drive_api, "build", lambda *args, **kwargs: _fake_drive_service(folder_name="New Root Folder"))  # noqa: ARG005
    monkeypatch.setattr(drive_api, "sync_org_drive_permissions", _fake_sync_org_drive_permissions)

    response = client.patch(
        "/api/drive/root-folder",
        json={"folder_id": "new-root-folder"},
        headers=_auth_header(owner.id, owner.email),
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["drive_folder_id"] == "new-root-folder"
    assert payload["previous_drive_folder_id"] == "old-root-folder"
    assert payload["folder_name"] == "New Root Folder"
    assert payload["acl_sync"]["ok"] is True

    db.refresh(org)
    assert org.drive_folder_id == "new-root-folder"


def test_admin_cannot_update_drive_root_folder(client, db):
    owner = User(google_id="owner-root-admin", email="owner-admin@example.com", name="Owner")
    admin = User(google_id="admin-root-admin", email="admin@example.com", name="Admin")
    org = Organization(
        name="Root Admin Org",
        slug="root-admin-org",
        drive_folder_id="root-folder",
        owner_id=1,
    )
    db.add_all([owner, admin, org])
    db.flush()
    org.owner_id = owner.id
    db.add_all(
        [
            OrgRole(organization_id=org.id, user_id=owner.id, role="owner"),
            OrgRole(organization_id=org.id, user_id=admin.id, role="admin"),
        ]
    )
    db.commit()

    response = client.patch(
        "/api/drive/root-folder",
        json={"folder_id": "new-root-folder"},
        headers=_auth_header(admin.id, admin.email),
    )
    assert response.status_code == 403
    assert "Only workspace owner can change Drive root folder" in response.json().get("detail", "")


def test_root_folder_update_rejects_non_folder_target(client, db, monkeypatch):
    owner = User(google_id="owner-root-invalid", email="owner-invalid@example.com", name="Owner")
    org = Organization(
        name="Root Invalid Org",
        slug="root-invalid-org",
        drive_folder_id="root-folder",
        owner_id=1,
    )
    db.add_all([owner, org])
    db.flush()
    org.owner_id = owner.id
    db.add(OrgRole(organization_id=org.id, user_id=owner.id, role="owner"))
    db.commit()

    async def _fake_get_drive_credentials(*args, **kwargs):  # noqa: ARG001
        return object()

    monkeypatch.setattr(drive_api, "get_drive_credentials", _fake_get_drive_credentials)
    monkeypatch.setattr(
        drive_api,
        "build",
        lambda *args, **kwargs: _fake_drive_service(mime_type="application/vnd.google-apps.document"),  # noqa: ARG005
    )

    response = client.patch(
        "/api/drive/root-folder",
        json={"folder_id": "not-a-folder"},
        headers=_auth_header(owner.id, owner.email),
    )
    assert response.status_code == 400
    assert response.json().get("detail") == "Provided ID is not a Drive folder"

    db.refresh(org)
    assert org.drive_folder_id == "root-folder"
