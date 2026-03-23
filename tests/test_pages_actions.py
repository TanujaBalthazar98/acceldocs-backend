"""Tests for page action APIs (edit title + duplicate)."""

from datetime import datetime, timedelta, timezone

import jwt

from app.api import pages as pages_api
from app.config import settings
from app.models import Organization, OrgRole, Page, PageComment, PageFeedback, Section, User


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


class _ExecResult:
    def __init__(self, payload: dict):
        self.payload = payload

    def execute(self):
        return self.payload


class _FakeDriveFiles:
    def __init__(self, calls: dict):
        self.calls = calls

    def update(self, *, fileId, body, fields, supportsAllDrives):
        self.calls["update"] = {
            "fileId": fileId,
            "body": body,
            "fields": fields,
            "supportsAllDrives": supportsAllDrives,
        }
        return _ExecResult({"id": fileId, "name": body.get("name")})

    def copy(self, *, fileId, body, fields, supportsAllDrives):
        self.calls["copy"] = {
            "fileId": fileId,
            "body": body,
            "fields": fields,
            "supportsAllDrives": supportsAllDrives,
        }
        return _ExecResult({"id": "doc-copy-1", "modifiedTime": "2026-03-12T10:00:00Z"})


class _FakeDriveService:
    def __init__(self, calls: dict):
        self.calls = calls

    def files(self):
        return _FakeDriveFiles(self.calls)


def _seed_user_org(db):
    user = User(google_id="u-pages-actions", email="pages@example.com", name="Pages User", role="owner")
    db.add(user)
    db.flush()

    org = Organization(name="Pages Org", slug="pages-org", domain="pages.example.com", drive_folder_id="org-folder-1")
    db.add(org)
    db.flush()

    db.add(OrgRole(organization_id=org.id, user_id=user.id, role="owner"))
    db.commit()
    return user, org


def test_edit_title_renames_google_doc(client, db, monkeypatch):
    user, org = _seed_user_org(db)
    section = Section(
        organization_id=org.id,
        parent_id=None,
        name="Docs",
        slug="docs",
        drive_folder_id="section-folder-1",
        section_type="section",
        is_published=True,
        display_order=0,
    )
    db.add(section)
    db.flush()

    page = Page(
        organization_id=org.id,
        section_id=section.id,
        google_doc_id="doc-source-1",
        title="Old Title",
        slug="old-title",
        html_content="<h1>Old</h1>",
        status="draft",
        display_order=0,
        owner_id=user.id,
    )
    db.add(page)
    db.commit()

    calls = {}

    async def _fake_creds(_user, _db):
        return object()

    monkeypatch.setattr(pages_api, "_get_drive_credentials", _fake_creds)
    monkeypatch.setattr(pages_api, "build", lambda *args, **kwargs: _FakeDriveService(calls))

    resp = client.patch(
        f"/api/pages/{page.id}",
        json={"title": "Updated Title"},
        headers=_auth_header(user.id, user.email),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["title"] == "Updated Title"
    assert calls["update"]["fileId"] == "doc-source-1"
    assert calls["update"]["body"]["name"] == "Updated Title"


def test_duplicate_creates_drive_copy_and_inserts_below(client, db, monkeypatch):
    user, org = _seed_user_org(db)
    section = Section(
        organization_id=org.id,
        parent_id=None,
        name="Docs",
        slug="docs",
        drive_folder_id="section-folder-1",
        section_type="section",
        is_published=True,
        display_order=0,
    )
    db.add(section)
    db.flush()

    source = Page(
        organization_id=org.id,
        section_id=section.id,
        google_doc_id="doc-source-1",
        title="Source Page",
        slug="source-page",
        html_content="<h1>Source</h1>",
        status="published",
        is_published=True,
        display_order=0,
        owner_id=user.id,
    )
    sibling = Page(
        organization_id=org.id,
        section_id=section.id,
        google_doc_id="doc-sibling-1",
        title="Sibling Page",
        slug="sibling-page",
        html_content="<h1>Sibling</h1>",
        status="draft",
        display_order=1,
        owner_id=user.id,
    )
    db.add_all([source, sibling])
    db.commit()
    db.refresh(source)
    db.refresh(sibling)

    calls = {}

    async def _fake_creds(_user, _db):
        return object()

    async def _fake_export_html(_doc_id, _creds):
        return "<h1>Copy</h1>", "2026-03-12T10:00:00Z", "Source Page Copy"

    monkeypatch.setattr(pages_api, "_get_drive_credentials", _fake_creds)
    monkeypatch.setattr(pages_api, "_export_html", _fake_export_html)
    monkeypatch.setattr(pages_api, "build", lambda *args, **kwargs: _FakeDriveService(calls))

    resp = client.post(
        f"/api/pages/{source.id}/duplicate",
        headers=_auth_header(user.id, user.email),
    )
    assert resp.status_code == 201
    created = resp.json()

    assert created["title"] == "Source Page Copy"
    assert created["google_doc_id"] == "doc-copy-1"
    assert created["section_id"] == section.id
    assert created["display_order"] == 1

    # Endpoint runs in a separate session, so invalidate local identity-map state first.
    db.expire_all()
    all_pages = (
        db.query(Page)
        .filter(Page.organization_id == org.id, Page.section_id == section.id)
        .order_by(Page.display_order, Page.id)
        .all()
    )
    assert all_pages[0].id == source.id
    assert all_pages[0].display_order == 0
    assert all_pages[1].id == created["id"]
    assert all_pages[1].display_order == 1
    assert all_pages[2].id == sibling.id
    assert all_pages[2].display_order == 2

    assert calls["copy"]["fileId"] == "doc-source-1"
    assert calls["copy"]["body"]["parents"] == ["section-folder-1"]


def test_edit_slug_auto_resolves_duplicates(client, db):
    user, org = _seed_user_org(db)
    section = Section(
        organization_id=org.id,
        parent_id=None,
        name="Docs",
        slug="docs",
        drive_folder_id="section-folder-1",
        section_type="section",
        is_published=True,
        display_order=0,
    )
    db.add(section)
    db.flush()

    first = Page(
        organization_id=org.id,
        section_id=section.id,
        google_doc_id="doc-slug-1",
        title="First",
        slug="overview",
        html_content="<h1>First</h1>",
        status="draft",
        display_order=0,
        owner_id=user.id,
    )
    second = Page(
        organization_id=org.id,
        section_id=section.id,
        google_doc_id="doc-slug-2",
        title="Second",
        slug="details",
        html_content="<h1>Second</h1>",
        status="draft",
        display_order=1,
        owner_id=user.id,
    )
    db.add_all([first, second])
    db.commit()

    resp = client.patch(
        f"/api/pages/{second.id}",
        json={"slug": "overview"},
        headers=_auth_header(user.id, user.email),
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["slug"] == "overview-1"
    assert payload["slug_locked"] is True


def test_publish_requires_section_assignment(client, db):
    user, org = _seed_user_org(db)

    page = Page(
        organization_id=org.id,
        section_id=None,
        google_doc_id="doc-orphan-1",
        title="Orphan Page",
        slug="orphan-page",
        html_content="<h1>Orphan</h1>",
        status="draft",
        display_order=0,
        owner_id=user.id,
    )
    db.add(page)
    db.commit()

    resp = client.post(
        f"/api/pages/{page.id}/publish",
        headers=_auth_header(user.id, user.email),
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Assign this page to a section before publishing."


def test_sync_does_not_change_locked_slug(client, db, monkeypatch):
    user, org = _seed_user_org(db)
    section = Section(
        organization_id=org.id,
        parent_id=None,
        name="Docs",
        slug="docs",
        drive_folder_id="section-folder-1",
        section_type="section",
        is_published=True,
        display_order=0,
    )
    db.add(section)
    db.flush()

    page = Page(
        organization_id=org.id,
        section_id=section.id,
        google_doc_id="doc-sync-locked",
        title="Old Title",
        slug="custom-url",
        slug_locked=True,
        html_content="<h1>Old</h1>",
        status="draft",
        display_order=0,
        owner_id=user.id,
    )
    db.add(page)
    db.commit()

    async def _fake_creds(_user, _db):
        return object()

    async def _fake_export_html(_doc_id, _creds):
        return "<h1>New</h1>", "2026-03-13T10:00:00Z", "New Drive Title"

    monkeypatch.setattr(pages_api, "_get_drive_credentials", _fake_creds)
    monkeypatch.setattr(pages_api, "_export_html", _fake_export_html)

    resp = client.post(
        f"/api/pages/{page.id}/sync",
        headers=_auth_header(user.id, user.email),
    )
    assert resp.status_code == 200
    payload = resp.json()["page"]
    assert payload["title"] == "New Drive Title"
    assert payload["slug"] == "custom-url"


def test_update_page_visibility_override(client, db):
    user, org = _seed_user_org(db)
    section = Section(
        organization_id=org.id,
        parent_id=None,
        name="Docs",
        slug="docs",
        drive_folder_id="section-folder-1",
        section_type="section",
        is_published=True,
        display_order=0,
    )
    db.add(section)
    db.flush()

    page = Page(
        organization_id=org.id,
        section_id=section.id,
        google_doc_id="doc-vis-1",
        title="Visibility Page",
        slug="visibility-page",
        html_content="<h1>Visibility</h1>",
        status="draft",
        display_order=0,
        owner_id=user.id,
    )
    db.add(page)
    db.commit()

    resp = client.patch(
        f"/api/pages/{page.id}",
        json={"visibility_override": "internal"},
        headers=_auth_header(user.id, user.email),
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["visibility_override"] == "internal"


def test_engagement_overview_returns_feedback_and_comments(client, db):
    user, org = _seed_user_org(db)
    section = Section(
        organization_id=org.id,
        parent_id=None,
        name="Docs",
        slug="docs",
        section_type="section",
        is_published=True,
        display_order=0,
    )
    db.add(section)
    db.flush()

    page = Page(
        organization_id=org.id,
        section_id=section.id,
        google_doc_id="doc-engage-1",
        title="Engagement Page",
        slug="engagement-page",
        html_content="<h1>Engage</h1>",
        status="published",
        is_published=True,
        display_order=0,
        owner_id=user.id,
    )
    db.add(page)
    db.flush()

    db.add_all(
        [
            PageFeedback(
                organization_id=org.id,
                page_id=page.id,
                user_id=user.id,
                user_email=user.email,
                vote="up",
                message="Great page",
                source="internal",
            ),
            PageFeedback(
                organization_id=org.id,
                page_id=page.id,
                user_id=user.id,
                user_email=user.email,
                vote="down",
                message="Needs examples",
                source="internal",
            ),
            PageComment(
                organization_id=org.id,
                page_id=page.id,
                user_id=user.id,
                user_email=user.email,
                display_name=user.name,
                body="Please add one more troubleshooting section.",
                source="internal",
            ),
        ]
    )
    db.commit()

    headers = _auth_header(user.id, user.email)
    overview = client.get("/api/pages/engagement/overview", headers=headers)
    assert overview.status_code == 200
    payload = overview.json()

    assert payload["summary"]["total_feedback"] == 2
    assert payload["summary"]["helpful"] == 1
    assert payload["summary"]["not_helpful"] == 1
    assert payload["summary"]["total_comments"] == 1
    assert len(payload["pages"]) == 1
    assert payload["pages"][0]["page_id"] == page.id
    assert payload["pages"][0]["total_feedback"] == 2
    assert payload["pages"][0]["total_comments"] == 1
    assert payload["recent_comments"][0]["page_id"] == page.id

    detail = client.get(f"/api/pages/{page.id}/engagement", headers=headers)
    assert detail.status_code == 200
    detail_payload = detail.json()
    assert detail_payload["feedback"]["up"] == 1
    assert detail_payload["feedback"]["down"] == 1
    assert len(detail_payload["comments"]) == 1
