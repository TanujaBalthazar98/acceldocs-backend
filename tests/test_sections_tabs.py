"""Regression tests for section tab typing in clean-arch sections API."""

from datetime import datetime, timedelta, timezone

import jwt

from app.api import sections as sections_api
from app.config import settings
from app.models import Organization, OrgRole, Page, Section, User


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


def test_create_version_clones_product_content(client, db, monkeypatch):
    user = User(google_id="u-version-1", email="version-owner@example.com", name="Version Owner", role="owner")
    db.add(user)
    db.flush()

    org = Organization(name="Version Org", slug="version-org", domain="version.example.com")
    db.add(org)
    db.flush()
    db.add(OrgRole(organization_id=org.id, user_id=user.id, role="owner"))
    db.flush()

    product = Section(
        organization_id=org.id,
        parent_id=None,
        name="Resume",
        slug="resume",
        section_type="section",
        drive_folder_id="drive-product",
        is_published=True,
        display_order=0,
    )
    guides = Section(
        organization_id=org.id,
        parent_id=None,  # set below after product flush
        name="Guides",
        slug="guides",
        section_type="section",
        drive_folder_id="drive-guides",
        is_published=True,
        display_order=0,
    )
    db.add(product)
    db.flush()
    guides.parent_id = product.id
    db.add(guides)
    db.flush()

    page_product = Page(
        organization_id=org.id,
        section_id=product.id,
        google_doc_id="doc-product",
        title="Overview",
        slug="overview",
        html_content="<h1>Overview</h1>",
        is_published=True,
        status="published",
        display_order=0,
        owner_id=user.id,
    )
    page_guides = Page(
        organization_id=org.id,
        section_id=guides.id,
        google_doc_id="doc-guides",
        title="Quickstart",
        slug="quickstart",
        html_content="<h1>Quickstart</h1>",
        is_published=True,
        status="published",
        display_order=0,
        owner_id=user.id,
    )
    db.add_all([page_product, page_guides])
    db.commit()

    calls: dict[str, list[dict[str, str | None]]] = {"copies": [], "folders": []}

    class _CopyRequest:
        def __init__(self, *, file_id: str, body: dict):
            self.file_id = file_id
            self.body = body

        def execute(self):
            calls["copies"].append({"file_id": self.file_id, "parent": (self.body.get("parents") or [None])[0]})
            idx = len(calls["copies"])
            return {
                "id": f"doc-copy-{idx}",
                "modifiedTime": "2026-03-13T10:00:00Z",
                "name": self.body.get("name"),
            }

    class _Files:
        def copy(self, *, fileId, body, fields, supportsAllDrives):  # noqa: N803
            return _CopyRequest(file_id=fileId, body=body)

    class _DriveService:
        def files(self):
            return _Files()

    async def _fake_creds(_user, _org_id, _db):
        return object()

    def _fake_folder(_service, name, parent_id):
        calls["folders"].append({"name": name, "parent": parent_id})
        return f"folder-{len(calls['folders'])}"

    monkeypatch.setattr(sections_api, "get_drive_credentials", _fake_creds)
    monkeypatch.setattr(sections_api, "_create_drive_folder", _fake_folder)
    monkeypatch.setattr(sections_api, "_build", lambda *args, **kwargs: _DriveService(), raising=False)

    # sections.py imports googleapiclient.build inside function, so patch module-level symbol too.
    import googleapiclient.discovery as discovery  # type: ignore
    monkeypatch.setattr(discovery, "build", lambda *args, **kwargs: _DriveService())

    headers = _auth_header(user.id, user.email)
    create_resp = client.post(
        "/api/sections",
        json={
            "name": "Resume V2",
            "parent_id": product.id,
            "section_type": "version",
            "clone_from_section_id": product.id,
        },
        headers=headers,
    )
    assert create_resp.status_code == 201, create_resp.text
    created_version = create_resp.json()
    assert created_version["section_type"] == "version"

    sections = client.get("/api/sections", headers=headers).json()["sections"]
    pages = client.get("/api/pages", headers=headers).json()["pages"]

    cloned_guides = next(
        (
            section
            for section in sections
            if section["parent_id"] == created_version["id"] and section["name"] == "Guides"
        ),
        None,
    )
    assert cloned_guides is not None
    assert cloned_guides["section_type"] == "section"

    cloned_overview = next(
        (
            page
            for page in pages
            if page["section_id"] == created_version["id"] and page["title"] == "Overview"
        ),
        None,
    )
    assert cloned_overview is not None
    assert cloned_overview["is_published"] is False
    assert cloned_overview["status"] == "draft"

    cloned_quickstart = next(
        (
            page
            for page in pages
            if page["section_id"] == cloned_guides["id"] and page["title"] == "Quickstart"
        ),
        None,
    )
    assert cloned_quickstart is not None
    assert cloned_quickstart["google_doc_id"] != page_guides.google_doc_id
    assert cloned_quickstart["is_published"] is False
    assert cloned_quickstart["status"] == "draft"

    # Both source pages should be copied as new Drive docs.
    copied_source_ids = {entry["file_id"] for entry in calls["copies"]}
    assert copied_source_ids == {"doc-product", "doc-guides"}


def test_create_version_requires_top_level_product_parent(client, db):
    user = User(google_id="u-version-parent", email="version-parent@example.com", name="Version Parent", role="owner")
    db.add(user)
    db.flush()

    org = Organization(name="Version Parent Org", slug="version-parent-org", domain="version-parent.example.com")
    db.add(org)
    db.flush()
    db.add(OrgRole(organization_id=org.id, user_id=user.id, role="owner"))
    db.flush()

    product = Section(
        organization_id=org.id,
        parent_id=None,
        name="Resume",
        slug="resume",
        section_type="section",
        is_published=True,
    )
    child = Section(
        organization_id=org.id,
        parent_id=None,
        name="Guides",
        slug="guides",
        section_type="section",
        is_published=True,
    )
    db.add(product)
    db.flush()
    child.parent_id = product.id
    db.add(child)
    db.commit()

    headers = _auth_header(user.id, user.email)
    resp = client.post(
        "/api/sections",
        json={
            "name": "v2.0",
            "parent_id": child.id,
            "section_type": "version",
        },
        headers=headers,
    )
    assert resp.status_code == 400
    assert "top-level product" in resp.json()["detail"]


def test_update_section_to_version_requires_top_level_parent(client, db):
    user = User(google_id="u-version-update", email="version-update@example.com", name="Version Update", role="owner")
    db.add(user)
    db.flush()

    org = Organization(name="Version Update Org", slug="version-update-org", domain="version-update.example.com")
    db.add(org)
    db.flush()
    db.add(OrgRole(organization_id=org.id, user_id=user.id, role="owner"))
    db.flush()

    product = Section(
        organization_id=org.id,
        parent_id=None,
        name="Resume",
        slug="resume",
        section_type="section",
        is_published=True,
    )
    child = Section(
        organization_id=org.id,
        parent_id=None,
        name="Guides",
        slug="guides",
        section_type="section",
        is_published=True,
    )
    db.add(product)
    db.flush()
    child.parent_id = product.id
    db.add(child)
    db.flush()

    nested = Section(
        organization_id=org.id,
        parent_id=child.id,
        name="Nested",
        slug="nested",
        section_type="section",
        is_published=True,
    )
    db.add(nested)
    db.commit()

    headers = _auth_header(user.id, user.email)
    resp = client.patch(
        f"/api/sections/{nested.id}",
        json={"section_type": "version"},
        headers=headers,
    )
    assert resp.status_code == 400
    assert "top-level product" in resp.json()["detail"]


def test_update_section_reindexes_siblings_when_reordered_between_sections(client, db):
    user = User(google_id="u-sect-reorder", email="sect-reorder@example.com", name="Section Reorder", role="owner")
    db.add(user)
    db.flush()

    org = Organization(name="Section Reorder Org", slug="section-reorder-org", domain="section-reorder.example.com")
    db.add(org)
    db.flush()
    db.add(OrgRole(organization_id=org.id, user_id=user.id, role="owner"))
    db.flush()

    first = Section(
        organization_id=org.id,
        parent_id=None,
        name="First",
        slug="first",
        section_type="section",
        display_order=0,
    )
    second = Section(
        organization_id=org.id,
        parent_id=None,
        name="Second",
        slug="second",
        section_type="section",
        display_order=1,
    )
    third = Section(
        organization_id=org.id,
        parent_id=None,
        name="Third",
        slug="third",
        section_type="section",
        display_order=2,
    )
    db.add_all([first, second, third])
    db.commit()

    headers = _auth_header(user.id, user.email)
    resp = client.patch(
        f"/api/sections/{third.id}",
        json={"display_order": 1},
        headers=headers,
    )
    assert resp.status_code == 200

    db.expire_all()
    ordered = (
        db.query(Section)
        .filter(Section.organization_id == org.id, Section.parent_id.is_(None))
        .order_by(Section.display_order, Section.id)
        .all()
    )
    assert [section.id for section in ordered] == [first.id, third.id, second.id]
    assert [section.display_order for section in ordered] == [0, 1, 2]


def test_update_section_reindexes_source_and_target_when_parent_changes(client, db):
    user = User(google_id="u-sect-parent", email="sect-parent@example.com", name="Section Parent Move", role="owner")
    db.add(user)
    db.flush()

    org = Organization(name="Section Parent Org", slug="section-parent-org", domain="section-parent.example.com")
    db.add(org)
    db.flush()
    db.add(OrgRole(organization_id=org.id, user_id=user.id, role="owner"))
    db.flush()

    source_parent = Section(
        organization_id=org.id,
        parent_id=None,
        name="Source Parent",
        slug="source-parent",
        section_type="section",
        display_order=0,
    )
    target_parent = Section(
        organization_id=org.id,
        parent_id=None,
        name="Target Parent",
        slug="target-parent",
        section_type="section",
        display_order=1,
    )
    db.add_all([source_parent, target_parent])
    db.flush()

    source_first = Section(
        organization_id=org.id,
        parent_id=source_parent.id,
        name="Source First",
        slug="source-first",
        section_type="section",
        display_order=0,
    )
    moving = Section(
        organization_id=org.id,
        parent_id=source_parent.id,
        name="Move Me",
        slug="move-me",
        section_type="section",
        display_order=1,
    )
    target_first = Section(
        organization_id=org.id,
        parent_id=target_parent.id,
        name="Target First",
        slug="target-first",
        section_type="section",
        display_order=0,
    )
    target_second = Section(
        organization_id=org.id,
        parent_id=target_parent.id,
        name="Target Second",
        slug="target-second",
        section_type="section",
        display_order=1,
    )
    db.add_all([source_first, moving, target_first, target_second])
    db.commit()

    headers = _auth_header(user.id, user.email)
    resp = client.patch(
        f"/api/sections/{moving.id}",
        json={"parent_id": target_parent.id, "display_order": 1},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["parent_id"] == target_parent.id

    db.expire_all()
    source_children = (
        db.query(Section)
        .filter(Section.organization_id == org.id, Section.parent_id == source_parent.id)
        .order_by(Section.display_order, Section.id)
        .all()
    )
    target_children = (
        db.query(Section)
        .filter(Section.organization_id == org.id, Section.parent_id == target_parent.id)
        .order_by(Section.display_order, Section.id)
        .all()
    )
    assert [section.id for section in source_children] == [source_first.id]
    assert [section.display_order for section in source_children] == [0]
    assert [section.id for section in target_children] == [target_first.id, moving.id, target_second.id]
    assert [section.display_order for section in target_children] == [0, 1, 2]


def test_clone_from_version_skips_nested_version_children(client, db, monkeypatch):
    user = User(google_id="u-version-clone", email="version-clone@example.com", name="Version Clone", role="owner")
    db.add(user)
    db.flush()

    org = Organization(name="Version Clone Org", slug="version-clone-org", domain="version-clone.example.com")
    db.add(org)
    db.flush()
    db.add(OrgRole(organization_id=org.id, user_id=user.id, role="owner"))
    db.flush()

    product = Section(
        organization_id=org.id,
        parent_id=None,
        name="Resume",
        slug="resume",
        section_type="section",
        drive_folder_id="drive-product",
        is_published=True,
    )
    db.add(product)
    db.flush()

    version_v1 = Section(
        organization_id=org.id,
        parent_id=product.id,
        name="Resume v1.0",
        slug="resume-v1-0",
        section_type="version",
        drive_folder_id="drive-version-v1",
        is_published=True,
    )
    db.add(version_v1)
    db.flush()

    guides = Section(
        organization_id=org.id,
        parent_id=version_v1.id,
        name="Guides",
        slug="guides",
        section_type="section",
        drive_folder_id="drive-guides",
        is_published=True,
    )
    nested_version_like = Section(
        organization_id=org.id,
        parent_id=version_v1.id,
        name="Resume v0.9",
        slug="resume-v0-9",
        section_type="version",
        drive_folder_id="drive-old-version",
        is_published=True,
    )
    db.add_all([guides, nested_version_like])
    db.flush()

    page_guides = Page(
        organization_id=org.id,
        section_id=guides.id,
        google_doc_id="doc-guides-v1",
        title="Guide",
        slug="guide",
        html_content="<h1>Guide</h1>",
        is_published=True,
        status="published",
        display_order=0,
        owner_id=user.id,
    )
    db.add(page_guides)
    db.commit()

    class _CopyRequest:
        def __init__(self, *, body: dict):
            self.body = body

        def execute(self):
            return {
                "id": f"doc-copy-{self.body.get('name', 'page').lower().replace(' ', '-')}",
                "modifiedTime": "2026-03-13T10:00:00Z",
                "name": self.body.get("name"),
            }

    class _Files:
        def copy(self, *, fileId, body, fields, supportsAllDrives):  # noqa: N803, ARG002
            return _CopyRequest(body=body)

    class _DriveService:
        def files(self):
            return _Files()

    async def _fake_creds(_user, _org_id, _db):
        return object()

    def _fake_folder(_service, name, parent_id):  # noqa: ARG001
        normalized = name.lower().replace(" ", "-")
        return f"folder-{normalized}"

    monkeypatch.setattr(sections_api, "get_drive_credentials", _fake_creds)
    monkeypatch.setattr(sections_api, "_create_drive_folder", _fake_folder)
    monkeypatch.setattr(sections_api, "_build", lambda *args, **kwargs: _DriveService(), raising=False)
    import googleapiclient.discovery as discovery  # type: ignore
    monkeypatch.setattr(discovery, "build", lambda *args, **kwargs: _DriveService())

    headers = _auth_header(user.id, user.email)
    create_resp = client.post(
        "/api/sections",
        json={
            "name": "Resume v2.0",
            "parent_id": product.id,
            "section_type": "version",
            "clone_from_section_id": version_v1.id,
        },
        headers=headers,
    )
    assert create_resp.status_code == 201, create_resp.text
    created_version = create_resp.json()

    sections = client.get("/api/sections", headers=headers).json()["sections"]
    cloned_children = [s for s in sections if s["parent_id"] == created_version["id"]]

    assert any(child["name"] == "Guides" for child in cloned_children)
    assert not any(child["name"] == "Resume v0.9" for child in cloned_children)
