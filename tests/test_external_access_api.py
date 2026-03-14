"""External access API tests."""

from datetime import datetime, timedelta, timezone

import jwt

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


def test_external_access_admin_can_grant_list_revoke(client, db):
    owner = User(google_id="owner-ext-api", email="owner@example.com", name="Owner")
    org = Organization(name="External API Org", slug="external-api-org", domain="company.com")
    db.add_all([owner, org])
    db.flush()
    db.add(OrgRole(organization_id=org.id, user_id=owner.id, role="owner"))
    db.commit()

    headers = _auth_header(owner.id, owner.email)

    grant_resp = client.post(
        "/api/external-access",
        json={"email": "partner@example.com"},
        headers=headers,
    )
    assert grant_resp.status_code == 200
    grant_payload = grant_resp.json()
    assert grant_payload["ok"] is True
    assert grant_payload["status"] == "created"
    grant_id = grant_payload["grant"]["id"]

    list_resp = client.get("/api/external-access", headers=headers)
    assert list_resp.status_code == 200
    grants = list_resp.json()["grants"]
    assert len(grants) == 1
    assert grants[0]["email"] == "partner@example.com"
    assert grants[0]["is_active"] is True

    revoke_resp = client.delete(f"/api/external-access/{grant_id}", headers=headers)
    assert revoke_resp.status_code == 200
    assert revoke_resp.json()["status"] == "revoked"

    list_inactive = client.get("/api/external-access?include_inactive=true", headers=headers)
    assert list_inactive.status_code == 200
    all_grants = list_inactive.json()["grants"]
    assert len(all_grants) == 1
    assert all_grants[0]["is_active"] is False


def test_external_access_rejects_internal_domain_email(client, db):
    owner = User(google_id="owner-ext-domain", email="owner2@example.com", name="Owner2")
    org = Organization(name="Domain Org", slug="domain-org", domain="acceldata.io")
    db.add_all([owner, org])
    db.flush()
    db.add(OrgRole(organization_id=org.id, user_id=owner.id, role="owner"))
    db.commit()

    headers = _auth_header(owner.id, owner.email)
    resp = client.post(
        "/api/external-access",
        json={"email": "teammate@acceldata.io"},
        headers=headers,
    )
    assert resp.status_code == 400
    assert "organization membership" in resp.json()["detail"].lower()


def test_external_access_requires_admin_permissions(client, db):
    owner = User(google_id="owner-ext-perm", email="owner3@example.com", name="Owner3")
    editor = User(google_id="editor-ext-perm", email="editor3@example.com", name="Editor3")
    org = Organization(name="Perm Org", slug="perm-org")
    db.add_all([owner, editor, org])
    db.flush()
    db.add_all(
        [
            OrgRole(organization_id=org.id, user_id=owner.id, role="owner"),
            OrgRole(organization_id=org.id, user_id=editor.id, role="editor"),
        ]
    )
    db.commit()

    headers = _auth_header(editor.id, editor.email)
    resp = client.post(
        "/api/external-access",
        json={"email": "partner2@example.com"},
        headers=headers,
    )
    assert resp.status_code == 403
    assert "insufficient permissions" in resp.json()["detail"].lower()
