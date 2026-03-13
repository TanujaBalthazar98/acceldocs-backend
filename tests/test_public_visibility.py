"""Public docs visibility enforcement tests."""

from datetime import datetime, timedelta, timezone

import jwt

from app.api import public as public_api
from app.config import settings
from app.models import ExternalAccessGrant, OrgRole, Organization, Page, Section, User


def _auth_header_for_user(user_id: int, email: str) -> dict[str, str]:
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


def test_public_route_hides_internal_page_for_anonymous(client, db, monkeypatch):
    monkeypatch.setattr(public_api, "_get_db", lambda: db)

    org = Organization(name="Anon Org", slug="anon-org")
    db.add(org)
    db.flush()

    section = Section(
        organization_id=org.id,
        name="Internal",
        slug="internal",
        visibility="internal",
        is_published=True,
    )
    db.add(section)
    db.flush()

    page = Page(
        organization_id=org.id,
        section_id=section.id,
        google_doc_id="anon-internal-doc",
        title="Internal Runbook",
        slug="internal-runbook",
        published_html="<h1>Internal Runbook</h1>",
        is_published=True,
        status="published",
    )
    db.add(page)
    db.commit()

    page_resp = client.get("/docs/anon-org/internal-runbook")
    assert page_resp.status_code == 404

    landing_resp = client.get("/docs/anon-org")
    assert landing_resp.status_code == 200
    assert "Internal Runbook" not in landing_resp.text


def test_public_route_hides_internal_page_for_org_member(client, db, monkeypatch):
    monkeypatch.setattr(public_api, "_get_db", lambda: db)

    org = Organization(name="Member Org", slug="member-org")
    user = User(google_id="member-user-1", email="member@example.com", name="Member User")
    db.add_all([org, user])
    db.flush()
    db.add(OrgRole(organization_id=org.id, user_id=user.id, role="editor"))
    db.flush()

    section = Section(
        organization_id=org.id,
        name="Internal",
        slug="internal",
        visibility="internal",
        is_published=True,
    )
    db.add(section)
    db.flush()

    page = Page(
        organization_id=org.id,
        section_id=section.id,
        google_doc_id="member-internal-doc",
        title="Internal Handbook",
        slug="internal-handbook",
        published_html="<h1>Internal Handbook</h1>",
        is_published=True,
        status="published",
    )
    db.add(page)
    db.commit()

    resp = client.get(
        "/docs/member-org/internal-handbook",
        headers=_auth_header_for_user(user.id, user.email),
    )
    assert resp.status_code == 404

    forced_internal_resp = client.get(
        "/docs/member-org/internal-handbook?audience=internal",
        headers=_auth_header_for_user(user.id, user.email),
    )
    assert forced_internal_resp.status_code == 404


def test_public_route_hides_internal_page_even_with_docs_cookie(client, db, monkeypatch):
    monkeypatch.setattr(public_api, "_get_db", lambda: db)

    org = Organization(name="Cookie Org", slug="cookie-org")
    user = User(google_id="cookie-user-1", email="cookie-member@example.com", name="Cookie Member")
    db.add_all([org, user])
    db.flush()
    db.add(OrgRole(organization_id=org.id, user_id=user.id, role="editor"))
    db.flush()

    section = Section(
        organization_id=org.id,
        name="Internal",
        slug="internal",
        visibility="internal",
        is_published=True,
    )
    db.add(section)
    db.flush()

    page = Page(
        organization_id=org.id,
        section_id=section.id,
        google_doc_id="cookie-internal-doc",
        title="Cookie Internal",
        slug="cookie-internal",
        published_html="<h1>Cookie Internal</h1>",
        is_published=True,
        status="published",
    )
    db.add(page)
    db.commit()

    token = _auth_header_for_user(user.id, user.email)["Authorization"].replace("Bearer ", "", 1)
    client.cookies.set("acceldocs_docs_session", token)

    resp = client.get("/docs/cookie-org/cookie-internal")
    assert resp.status_code == 404


def test_public_route_allows_external_visibility_for_granted_user(client, db, monkeypatch):
    monkeypatch.setattr(public_api, "_get_db", lambda: db)

    org = Organization(name="External Org", slug="external-org")
    user = User(google_id="external-user-1", email="partner@example.com", name="Partner")
    db.add_all([org, user])
    db.flush()

    db.add(
        ExternalAccessGrant(
            organization_id=org.id,
            email="partner@example.com",
            is_active=True,
        )
    )
    db.flush()

    section = Section(
        organization_id=org.id,
        name="Shared",
        slug="shared",
        visibility="external",
        is_published=True,
    )
    db.add(section)
    db.flush()

    page = Page(
        organization_id=org.id,
        section_id=section.id,
        google_doc_id="external-doc-1",
        title="Partner Guide",
        slug="partner-guide",
        published_html="<h1>Partner Guide</h1>",
        is_published=True,
        status="published",
    )
    db.add(page)
    db.commit()

    resp = client.get(
        "/docs/external-org/partner-guide?audience=external",
        headers=_auth_header_for_user(user.id, user.email),
    )
    assert resp.status_code == 200
    assert "Partner Guide" in resp.text


def test_public_search_filters_by_visibility_scope(client, db, monkeypatch):
    monkeypatch.setattr(public_api, "_get_db", lambda: db)

    org = Organization(name="Search Visibility Org", slug="search-vis-org")
    user = User(google_id="search-member-1", email="searcher@example.com", name="Searcher")
    db.add_all([org, user])
    db.flush()
    db.add(OrgRole(organization_id=org.id, user_id=user.id, role="viewer"))
    db.flush()

    public_section = Section(
        organization_id=org.id,
        name="Public",
        slug="public",
        visibility="public",
        is_published=True,
    )
    internal_section = Section(
        organization_id=org.id,
        name="Internal",
        slug="internal",
        visibility="internal",
        is_published=True,
    )
    db.add_all([public_section, internal_section])
    db.flush()

    db.add_all(
        [
            Page(
                organization_id=org.id,
                section_id=public_section.id,
                google_doc_id="search-public-doc",
                title="Guide Public",
                slug="guide-public",
                published_html="<p>Guide Public Content</p>",
                is_published=True,
                status="published",
            ),
            Page(
                organization_id=org.id,
                section_id=internal_section.id,
                google_doc_id="search-internal-doc",
                title="Guide Internal",
                slug="guide-internal",
                published_html="<p>Guide Internal Content</p>",
                is_published=True,
                status="published",
            ),
        ]
    )
    db.commit()
    user_id = user.id
    user_email = user.email

    anon = client.get("/docs/search-vis-org/search?q=guide")
    assert anon.status_code == 200
    anon_slugs = {item["slug"] for item in anon.json()["results"]}
    assert "guide-public" in anon_slugs
    assert "guide-internal" not in anon_slugs

    member = client.get(
        "/docs/search-vis-org/search?q=guide",
        headers=_auth_header_for_user(user_id, user_email),
    )
    assert member.status_code == 200
    member_slugs = {item["slug"] for item in member.json()["results"]}
    assert "guide-public" in member_slugs
    assert "guide-internal" not in member_slugs

    member_all = client.get(
        "/docs/search-vis-org/search?q=guide&audience=all",
        headers=_auth_header_for_user(user_id, user_email),
    )
    assert member_all.status_code == 200
    member_all_slugs = {item["slug"] for item in member_all.json()["results"]}
    assert "guide-public" in member_all_slugs
    assert "guide-internal" not in member_all_slugs


def test_internal_docs_route_requires_org_membership(client, db, monkeypatch):
    monkeypatch.setattr(public_api, "_get_db", lambda: db)

    org = Organization(name="Strict Internal Org", slug="strict-internal-org")
    db.add(org)
    db.flush()

    section = Section(
        organization_id=org.id,
        name="Internal Section",
        slug="internal-section",
        visibility="internal",
        is_published=True,
    )
    db.add(section)
    db.flush()

    page = Page(
        organization_id=org.id,
        section_id=section.id,
        google_doc_id="strict-internal-doc",
        title="Internal Only",
        slug="internal-only",
        published_html="<h1>Internal Only</h1>",
        is_published=True,
        status="published",
    )
    db.add(page)
    db.commit()

    resp = client.get("/internal-docs/strict-internal-org")
    assert resp.status_code == 403


def test_internal_docs_route_renders_only_internal_content(client, db, monkeypatch):
    monkeypatch.setattr(public_api, "_get_db", lambda: db)

    org = Organization(name="Internal Filter Org", slug="internal-filter-org")
    user = User(google_id="internal-member-1", email="internal-member@example.com", name="Internal Member")
    db.add_all([org, user])
    db.flush()
    db.add(OrgRole(organization_id=org.id, user_id=user.id, role="viewer"))
    db.flush()

    public_section = Section(
        organization_id=org.id,
        name="Public",
        slug="public",
        visibility="public",
        is_published=True,
    )
    internal_section = Section(
        organization_id=org.id,
        name="Internal",
        slug="internal",
        visibility="internal",
        is_published=True,
    )
    db.add_all([public_section, internal_section])
    db.flush()

    public_page = Page(
        organization_id=org.id,
        section_id=public_section.id,
        google_doc_id="internal-filter-public-doc",
        title="Public Landing Page",
        slug="public-landing-page",
        published_html="<h1>Public Landing Page</h1>",
        is_published=True,
        status="published",
    )
    internal_page = Page(
        organization_id=org.id,
        section_id=internal_section.id,
        google_doc_id="internal-filter-internal-doc",
        title="Internal Ops Guide",
        slug="internal-ops-guide",
        published_html="<h1>Internal Ops Guide</h1>",
        is_published=True,
        status="published",
    )
    db.add_all([public_page, internal_page])
    db.commit()

    auth_header = _auth_header_for_user(user.id, user.email)

    landing = client.get("/internal-docs/internal-filter-org", headers=auth_header)
    assert landing.status_code == 200
    assert "Internal Ops Guide" in landing.text
    assert "Public Landing Page" not in landing.text

    internal_page_resp = client.get(
        "/internal-docs/internal-filter-org/internal-ops-guide",
        headers=auth_header,
    )
    assert internal_page_resp.status_code == 200
    assert "Internal Ops Guide" in internal_page_resp.text

    public_page_resp = client.get(
        "/internal-docs/internal-filter-org/public-landing-page",
        headers=auth_header,
    )
    assert public_page_resp.status_code == 404

    internal_search = client.get(
        "/internal-docs/internal-filter-org/search?q=guide",
        headers=auth_header,
    )
    assert internal_search.status_code == 200
    internal_slugs = {item["slug"] for item in internal_search.json()["results"]}
    assert "internal-ops-guide" in internal_slugs
    assert "public-landing-page" not in internal_slugs


def test_internal_docs_route_uses_docs_session_cookie(client, db, monkeypatch):
    monkeypatch.setattr(public_api, "_get_db", lambda: db)

    org = Organization(name="Internal Cookie Org", slug="internal-cookie-org")
    user = User(google_id="internal-cookie-user", email="cookie-internal@example.com", name="Cookie Internal")
    db.add_all([org, user])
    db.flush()
    db.add(OrgRole(organization_id=org.id, user_id=user.id, role="editor"))
    db.flush()

    section = Section(
        organization_id=org.id,
        name="Internal Section",
        slug="internal-section",
        visibility="internal",
        is_published=True,
    )
    db.add(section)
    db.flush()

    page = Page(
        organization_id=org.id,
        section_id=section.id,
        google_doc_id="internal-cookie-doc",
        title="Cookie Internal Doc",
        slug="cookie-internal-doc",
        published_html="<h1>Cookie Internal Doc</h1>",
        is_published=True,
        status="published",
    )
    db.add(page)
    db.commit()

    token = _auth_header_for_user(user.id, user.email)["Authorization"].replace("Bearer ", "", 1)
    client.cookies.set("acceldocs_docs_session", token)

    resp = client.get("/internal-docs/internal-cookie-org/cookie-internal-doc")
    assert resp.status_code == 200
    assert "Cookie Internal Doc" in resp.text


def test_internal_docs_accepts_query_auth_token_and_bootstraps_cookie(client, db, monkeypatch):
    monkeypatch.setattr(public_api, "_get_db", lambda: db)

    org = Organization(name="Internal Query Org", slug="internal-query-org")
    user = User(google_id="internal-query-user", email="query-internal@example.com", name="Query Internal")
    db.add_all([org, user])
    db.flush()
    db.add(OrgRole(organization_id=org.id, user_id=user.id, role="owner"))
    db.flush()

    section = Section(
        organization_id=org.id,
        name="Internal Section",
        slug="internal-section",
        visibility="internal",
        is_published=True,
    )
    db.add(section)
    db.flush()

    page = Page(
        organization_id=org.id,
        section_id=section.id,
        google_doc_id="internal-query-doc",
        title="Query Token Internal Doc",
        slug="query-token-internal-doc",
        published_html="<h1>Query Token Internal Doc</h1>",
        is_published=True,
        status="published",
    )
    db.add(page)
    db.commit()

    raw_token = _auth_header_for_user(user.id, user.email)["Authorization"].replace("Bearer ", "", 1)

    bootstrap = client.get(
        f"/internal-docs/internal-query-org/query-token-internal-doc?auth_token={raw_token}",
        follow_redirects=False,
    )
    assert bootstrap.status_code == 307
    assert "auth_token=" not in (bootstrap.headers.get("location") or "")
    assert "acceldocs_docs_session=" in (bootstrap.headers.get("set-cookie") or "")

    follow = client.get("/internal-docs/internal-query-org/query-token-internal-doc")
    assert follow.status_code == 200
    assert "Query Token Internal Doc" in follow.text
