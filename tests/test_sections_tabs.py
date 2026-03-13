"""Regression tests for section tab typing in clean-arch sections API."""

from datetime import datetime, timedelta, timezone

import jwt

from app.config import settings
from app.models import Organization, OrgRole, User


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


def test_create_update_list_section_type_tab(client, db):
    user = User(google_id="u-tab-1", email="tab-owner@example.com", name="Tab Owner", role="owner")
    db.add(user)
    db.flush()

    org = Organization(name="Tab Org", slug="tab-org", domain="tab.example.com")
    db.add(org)
    db.flush()

    db.add(OrgRole(organization_id=org.id, user_id=user.id, role="owner"))
    db.commit()

    headers = _auth_header(user.id, user.email)

    create_resp = client.post(
        "/api/sections",
        json={"name": "Documentation", "section_type": "tab"},
        headers=headers,
    )
    assert create_resp.status_code == 201
    created = create_resp.json()
    assert created["name"] == "Documentation"
    assert created["section_type"] == "tab"
    assert created["visibility"] == "public"

    section_id = created["id"]
    update_resp = client.patch(
        f"/api/sections/{section_id}",
        json={"section_type": "section", "visibility": "internal"},
        headers=headers,
    )
    assert update_resp.status_code == 200
    assert update_resp.json()["section_type"] == "section"
    assert update_resp.json()["visibility"] == "internal"

    list_resp = client.get("/api/sections", headers=headers)
    assert list_resp.status_code == 200
    sections = list_resp.json()["sections"]
    assert len(sections) == 1
    assert sections[0]["section_type"] == "section"
    assert sections[0]["visibility"] == "internal"
