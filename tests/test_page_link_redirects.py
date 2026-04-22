"""Page link stability tests (canonical rewrites + redirects)."""

from app.api import public as public_api
from app.models import Organization, Page, PageRedirect, Section


def test_public_page_rewrites_google_doc_and_legacy_links(client, db, monkeypatch):
    monkeypatch.setattr(public_api, "_get_db", lambda: db)

    org = Organization(name="Link Org", slug="link-org", domain="link.example.com")
    db.add(org)
    db.flush()

    section = Section(
        organization_id=org.id,
        name="Guides",
        slug="guides",
        section_type="section",
        visibility="public",
        is_published=True,
    )
    db.add(section)
    db.flush()

    target = Page(
        organization_id=org.id,
        section_id=section.id,
        google_doc_id="doc-target",
        title="Target Page",
        slug="target-page",
        published_html="<h1>Target Page</h1>",
        is_published=True,
        status="published",
    )
    source = Page(
        organization_id=org.id,
        section_id=section.id,
        google_doc_id="doc-source",
        title="Source Page",
        slug="source-page",
        published_html=(
            '<h1>Source</h1>'
            '<p><a href="https://docs.google.com/document/d/doc-target/edit">Google Doc target</a></p>'
            '<p><a href="/docs/link-org/target-page">Legacy slug target</a></p>'
        ),
        is_published=True,
        status="published",
    )
    db.add_all([target, source])
    db.commit()

    resp = client.get("/docs/link-org/source-page")
    assert resp.status_code == 200
    html = resp.text
    canonical = f"/docs/link-org/{section.slug}/documentation/{target.slug}"
    assert canonical in html


def test_public_page_rewrites_cross_visibility_links_to_correct_docs_root(client, db, monkeypatch):
    monkeypatch.setattr(public_api, "_get_db", lambda: db)

    org = Organization(name="Cross Visibility Org", slug="cross-vis-org", domain="cross.example.com")
    db.add(org)
    db.flush()

    public_section = Section(
        organization_id=org.id,
        name="Public",
        slug="public",
        section_type="section",
        visibility="public",
        is_published=True,
    )
    internal_section = Section(
        organization_id=org.id,
        name="Internal",
        slug="internal",
        section_type="section",
        visibility="internal",
        is_published=True,
    )
    external_section = Section(
        organization_id=org.id,
        name="External",
        slug="external",
        section_type="section",
        visibility="external",
        is_published=True,
    )
    db.add_all([public_section, internal_section, external_section])
    db.flush()

    internal_target = Page(
        organization_id=org.id,
        section_id=internal_section.id,
        google_doc_id="doc-internal-target",
        title="Internal Target",
        slug="internal-target",
        published_html="<h1>Internal Target</h1>",
        is_published=True,
        status="published",
    )
    external_target = Page(
        organization_id=org.id,
        section_id=external_section.id,
        google_doc_id="doc-external-target",
        title="External Target",
        slug="external-target",
        published_html="<h1>External Target</h1>",
        is_published=True,
        status="published",
    )
    source = Page(
        organization_id=org.id,
        section_id=public_section.id,
        google_doc_id="doc-cross-source",
        title="Public Source",
        slug="public-source",
        published_html=(
            '<h1>Source</h1>'
            '<p><a href="https://docs.google.com/document/d/doc-internal-target/edit">Internal link</a></p>'
            '<p><a href="https://docs.google.com/document/d/doc-external-target/edit">External link</a></p>'
        ),
        is_published=True,
        status="published",
    )
    db.add_all([internal_target, external_target, source])
    db.commit()

    resp = client.get("/docs/cross-vis-org/public-source")
    assert resp.status_code == 200
    html = resp.text
    assert f"/internal-docs/cross-vis-org/{internal_section.slug}/documentation/{internal_target.slug}" in html
    assert f"/external-docs/cross-vis-org/{external_section.slug}/documentation/{external_target.slug}" in html


def test_internal_link_target_shows_access_gate_instead_of_raw_error(client, db, monkeypatch):
    monkeypatch.setattr(public_api, "_get_db", lambda: db)

    org = Organization(name="Gate Org", slug="gate-org", domain="gate.example.com")
    db.add(org)
    db.flush()

    public_section = Section(
        organization_id=org.id,
        name="Public",
        slug="public",
        section_type="section",
        visibility="public",
        is_published=True,
    )
    internal_section = Section(
        organization_id=org.id,
        name="Internal",
        slug="internal",
        section_type="section",
        visibility="internal",
        is_published=True,
    )
    db.add_all([public_section, internal_section])
    db.flush()

    internal_target = Page(
        organization_id=org.id,
        section_id=internal_section.id,
        google_doc_id="doc-internal-gate",
        title="Internal Target",
        slug="internal-target",
        published_html="<h1>Internal Target</h1>",
        is_published=True,
        status="published",
    )
    source = Page(
        organization_id=org.id,
        section_id=public_section.id,
        google_doc_id="doc-source-gate",
        title="Source",
        slug="source",
        published_html='<a href="https://docs.google.com/document/d/doc-internal-gate/edit">Internal link</a>',
        is_published=True,
        status="published",
    )
    db.add_all([internal_target, source])
    db.commit()

    source_resp = client.get("/docs/gate-org/source")
    assert source_resp.status_code == 200
    expected_internal_link = f"/internal-docs/gate-org/{internal_section.slug}/documentation/{internal_target.slug}"
    assert expected_internal_link in source_resp.text

    target_resp = client.get(expected_internal_link)
    assert target_resp.status_code == 403
    assert "Internal docs only" in target_resp.text
    assert "Sign in" in target_resp.text


def test_legacy_slug_route_redirects_via_page_redirect(client, db, monkeypatch):
    monkeypatch.setattr(public_api, "_get_db", lambda: db)

    org = Organization(name="Redirect Org", slug="redirect-org", domain="redirect.example.com")
    db.add(org)
    db.flush()

    section = Section(
        organization_id=org.id,
        name="Guides",
        slug="guides",
        section_type="section",
        visibility="public",
        is_published=True,
    )
    db.add(section)
    db.flush()

    page = Page(
        organization_id=org.id,
        section_id=section.id,
        google_doc_id="doc-redirect",
        title="Current",
        slug="current-slug",
        published_html="<h1>Current</h1>",
        is_published=True,
        status="published",
    )
    db.add(page)
    db.flush()
    db.add(
        PageRedirect(
            organization_id=org.id,
            source_page_id=page.id,
            source_slug="old-slug",
            target_page_id=page.id,
            status_code=307,
            is_active=True,
        )
    )
    db.commit()

    resp = client.get("/docs/redirect-org/old-slug", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == f"/docs/redirect-org/{section.slug}/documentation/{page.slug}"


def test_canonical_page_route_redirects_deleted_page_to_landing(client, db, monkeypatch):
    monkeypatch.setattr(public_api, "_get_db", lambda: db)

    org = Organization(name="Deleted Org", slug="deleted-org", domain="deleted.example.com")
    db.add(org)
    db.flush()
    db.add(
        PageRedirect(
            organization_id=org.id,
            source_page_id=9991,
            source_slug="old-deleted-page",
            target_page_id=None,
            target_url=None,
            status_code=307,
            is_active=True,
        )
    )
    db.commit()

    resp = client.get("/docs/deleted-org/p/9991/old-deleted-page", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/docs/deleted-org"
