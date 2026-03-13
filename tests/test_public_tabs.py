"""Public docs tab navigation tests."""

from app.api import public as public_api
from app.models import Organization, Page, Section


def test_public_page_renders_tab_strip_for_tabbed_product(client, db, monkeypatch):
    monkeypatch.setattr(public_api, "_get_db", lambda: db)

    org = Organization(name="Tab Org", slug="tab-org", domain="tab.example.com", primary_color="#0ea5e9")
    db.add(org)
    db.flush()

    product = Section(
        organization_id=org.id,
        parent_id=None,
        name="Product A",
        slug="product-a",
        section_type="section",
        is_published=True,
        display_order=0,
    )
    db.add(product)
    db.flush()

    tab_docs = Section(
        organization_id=org.id,
        parent_id=product.id,
        name="Documentation",
        slug="documentation",
        section_type="tab",
        is_published=True,
        display_order=0,
    )
    tab_api = Section(
        organization_id=org.id,
        parent_id=product.id,
        name="API Reference",
        slug="api-reference",
        section_type="tab",
        is_published=True,
        display_order=1,
    )
    db.add_all([tab_docs, tab_api])
    db.flush()

    docs_page = Page(
        organization_id=org.id,
        section_id=tab_docs.id,
        google_doc_id="doc-docs",
        title="Getting Started",
        slug="getting-started",
        published_html="<h1>Getting Started</h1>",
        is_published=True,
        status="published",
        display_order=0,
    )
    api_page = Page(
        organization_id=org.id,
        section_id=tab_api.id,
        google_doc_id="doc-api",
        title="API Overview",
        slug="api-overview",
        published_html="<h1>API Overview</h1>",
        is_published=True,
        status="published",
        display_order=0,
    )
    db.add_all([docs_page, api_page])
    db.commit()

    resp = client.get("/docs/tab-org/getting-started")
    assert resp.status_code == 200
    html = resp.text

    assert "Documentation" in html
    assert "API Reference" in html
    assert f'/docs/tab-org/p/{docs_page.id}/getting-started' in html
    assert f'/docs/tab-org/p/{api_page.id}/api-overview' in html
    assert "docs-tab active" in html
    assert "tabsbar-product-link" in html
    assert "Product A" in html


def test_public_page_renders_single_root_tab_strip(client, db, monkeypatch):
    monkeypatch.setattr(public_api, "_get_db", lambda: db)

    org = Organization(name="Solo Tab Org", slug="solo-tab-org", domain="solo.example.com", primary_color="#22c55e")
    db.add(org)
    db.flush()

    resume_tab = Section(
        organization_id=org.id,
        parent_id=None,
        name="Resume",
        slug="resume",
        section_type="tab",
        is_published=True,
        display_order=0,
    )
    db.add(resume_tab)
    db.flush()

    child_section = Section(
        organization_id=org.id,
        parent_id=resume_tab.id,
        name="Test",
        slug="test",
        section_type="section",
        is_published=True,
        display_order=0,
    )
    db.add(child_section)
    db.flush()

    page = Page(
        organization_id=org.id,
        section_id=child_section.id,
        google_doc_id="doc-resume",
        title="Doc",
        slug="doc",
        published_html="<h1>Doc</h1>",
        is_published=True,
        status="published",
        display_order=0,
    )
    db.add(page)
    db.commit()

    resp = client.get("/docs/solo-tab-org/doc")
    assert resp.status_code == 200
    html = resp.text

    assert "tabsbar" in html
    assert "Resume" in html
    assert f'/docs/solo-tab-org/p/{page.id}/doc' in html
    assert "docs-tab active" in html


def test_public_page_renders_single_root_section_without_tabs(client, db, monkeypatch):
    monkeypatch.setattr(public_api, "_get_db", lambda: db)

    org = Organization(name="Legacy Org", slug="legacy-org", domain="legacy.example.com", primary_color="#f97316")
    db.add(org)
    db.flush()

    root_section = Section(
        organization_id=org.id,
        parent_id=None,
        name="Resume",
        slug="resume",
        section_type="section",
        is_published=True,
        display_order=0,
    )
    db.add(root_section)
    db.flush()

    child_section = Section(
        organization_id=org.id,
        parent_id=root_section.id,
        name="Test",
        slug="test",
        section_type="section",
        is_published=True,
        display_order=0,
    )
    db.add(child_section)
    db.flush()

    page = Page(
        organization_id=org.id,
        section_id=child_section.id,
        google_doc_id="doc-legacy",
        title="Doc",
        slug="doc",
        published_html="<h1>Doc</h1>",
        is_published=True,
        status="published",
        display_order=0,
    )
    db.add(page)
    db.commit()

    resp = client.get("/docs/legacy-org/doc")
    assert resp.status_code == 200
    html = resp.text

    assert 'aria-label="Tabs"' not in html
    assert "tabsbar-product-link" in html
    assert "Resume" in html
    assert "Test" in html
    assert f'/docs/legacy-org/p/{page.id}/doc' in html
    assert "docs-tab active" not in html


def test_public_page_flattens_same_name_wrapper_without_tabs(client, db, monkeypatch):
    monkeypatch.setattr(public_api, "_get_db", lambda: db)

    org = Organization(name="Wrapper Org", slug="wrapper-org", domain="wrapper.example.com", primary_color="#06b6d4")
    db.add(org)
    db.flush()

    root = Section(
        organization_id=org.id,
        parent_id=None,
        name="Resume",
        slug="resume",
        section_type="section",
        is_published=True,
        display_order=0,
    )
    db.add(root)
    db.flush()

    wrapper = Section(
        organization_id=org.id,
        parent_id=root.id,
        name="Resume",
        slug="resume-folder",
        section_type="section",
        is_published=True,
        display_order=0,
    )
    child = Section(
        organization_id=org.id,
        parent_id=wrapper.id,
        name="Test",
        slug="test",
        section_type="section",
        is_published=True,
        display_order=0,
    )
    db.add_all([wrapper, child])
    db.flush()

    page = Page(
        organization_id=org.id,
        section_id=child.id,
        google_doc_id="doc-wrapper",
        title="Doc",
        slug="doc",
        published_html="<h1>Doc</h1>",
        is_published=True,
        status="published",
        display_order=0,
    )
    db.add(page)
    db.commit()

    resp = client.get("/docs/wrapper-org/doc")
    assert resp.status_code == 200
    html = resp.text

    # No tabs in this structure; wrapper should not duplicate "Resume" in sidebar.
    assert "docs-tab active" not in html
    assert f"toggleSection('sec-{wrapper.id}')" not in html


def test_public_landing_renders_hub_layout_with_product_groups(client, db, monkeypatch):
    monkeypatch.setattr(public_api, "_get_db", lambda: db)

    org = Organization(name="Hub Org", slug="hub-org", domain="hub.example.com", primary_color="#0ea5e9")
    db.add(org)
    db.flush()

    product = Section(
        organization_id=org.id,
        parent_id=None,
        name="Resume",
        slug="resume",
        section_type="section",
        is_published=True,
        display_order=0,
    )
    topic = Section(
        organization_id=org.id,
        parent_id=product.id,
        name="Test",
        slug="test",
        section_type="section",
        is_published=True,
        display_order=0,
    )
    db.add_all([product, topic])
    db.flush()

    page = Page(
        organization_id=org.id,
        section_id=topic.id,
        google_doc_id="doc-hub",
        title="Doc",
        slug="doc",
        published_html="<h1>Doc</h1>",
        is_published=True,
        status="published",
        display_order=0,
    )
    db.add(page)
    db.commit()

    resp = client.get("/docs/hub-org")
    assert resp.status_code == 200
    html = resp.text

    assert "Developer Hub" in html
    assert "product-rail" in html
    assert "Get Started" in html
    assert "Open Section" in html
    assert f"/docs/hub-org/p/{page.id}/doc" in html


def test_public_search_endpoint_returns_results(client, db, monkeypatch):
    monkeypatch.setattr(public_api, "_get_db", lambda: db)

    org = Organization(name="Search Org", slug="search-org", domain="search.example.com", primary_color="#0ea5e9")
    db.add(org)
    db.flush()

    section = Section(
        organization_id=org.id,
        parent_id=None,
        name="Guides",
        slug="guides",
        section_type="section",
        is_published=True,
        display_order=0,
    )
    db.add(section)
    db.flush()

    page = Page(
        organization_id=org.id,
        section_id=section.id,
        google_doc_id="doc-search",
        title="Platform Overview",
        slug="platform-overview",
        published_html="<h1>Platform Overview</h1><p>Searchable body text.</p>",
        is_published=True,
        status="published",
        display_order=0,
    )
    db.add(page)
    db.commit()

    resp = client.get("/docs/search-org/search?q=platform")
    assert resp.status_code == 200
    data = resp.json()
    assert "results" in data
    assert len(data["results"]) >= 1
    assert data["results"][0]["slug"] == "platform-overview"
    assert data["results"][0]["page_id"] == page.id


def test_public_page_routing_isolated_across_products(client, db, monkeypatch):
    monkeypatch.setattr(public_api, "_get_db", lambda: db)

    org = Organization(name="Duplicate Org", slug="duplicate-org", domain="dup.example.com", primary_color="#0ea5e9")
    db.add(org)
    db.flush()

    product_a = Section(
        organization_id=org.id,
        parent_id=None,
        name="Resume",
        slug="resume",
        section_type="section",
        is_published=True,
        display_order=0,
    )
    product_b = Section(
        organization_id=org.id,
        parent_id=None,
        name="Release Notes",
        slug="release-notes",
        section_type="section",
        is_published=True,
        display_order=1,
    )
    db.add_all([product_a, product_b])
    db.flush()

    page_a = Page(
        organization_id=org.id,
        section_id=product_a.id,
        google_doc_id="doc-a",
        title="Overview A",
        slug="overview",
        published_html="<h1>Overview A</h1>",
        is_published=True,
        status="published",
        display_order=0,
    )
    page_b = Page(
        organization_id=org.id,
        section_id=product_b.id,
        google_doc_id="doc-b",
        title="Overview B",
        slug="overview-b",
        published_html="<h1>Overview B</h1>",
        is_published=True,
        status="published",
        display_order=0,
    )
    db.add_all([page_a, page_b])
    db.commit()

    a_resp = client.get(f"/docs/duplicate-org/p/{page_a.id}/{page_a.slug}")
    b_resp = client.get(f"/docs/duplicate-org/p/{page_b.id}/{page_b.slug}")

    assert a_resp.status_code == 200
    assert b_resp.status_code == 200
    assert "Overview A" in a_resp.text
    assert "Overview B" in b_resp.text


def test_multiple_products_do_not_render_as_top_tabs(client, db, monkeypatch):
    monkeypatch.setattr(public_api, "_get_db", lambda: db)

    org = Organization(name="Products Org", slug="products-org", domain="products.example.com", primary_color="#0ea5e9")
    db.add(org)
    db.flush()

    resume = Section(
        organization_id=org.id,
        parent_id=None,
        name="Resume",
        slug="resume",
        section_type="section",
        is_published=True,
        display_order=0,
    )
    release_notes = Section(
        organization_id=org.id,
        parent_id=None,
        name="Release Notes",
        slug="release-notes",
        section_type="section",
        is_published=True,
        display_order=1,
    )
    db.add_all([resume, release_notes])
    db.flush()

    resume_page = Page(
        organization_id=org.id,
        section_id=resume.id,
        google_doc_id="doc-resume-only",
        title="Resume Intro",
        slug="resume-intro",
        published_html="<h1>Resume Intro</h1>",
        is_published=True,
        status="published",
        display_order=0,
    )
    release_page = Page(
        organization_id=org.id,
        section_id=release_notes.id,
        google_doc_id="doc-release-only",
        title="Release Intro",
        slug="release-intro",
        published_html="<h1>Release Intro</h1>",
        is_published=True,
        status="published",
        display_order=0,
    )
    db.add_all([resume_page, release_page])
    db.commit()

    resp = client.get(f"/docs/products-org/p/{resume_page.id}/{resume_page.slug}")
    assert resp.status_code == 200
    html = resp.text

    assert 'aria-label="Tabs"' not in html
    assert 'class="docs-tab ' not in html
    assert "docs-tab active" not in html
    assert "tabsbar-product-link" in html
    assert ">Resume<" in html
    assert "Resume Intro" in html
    assert f"/docs/products-org/p/{resume_page.id}/{resume_page.slug}" in html


def test_flat_hierarchy_landing_hides_product_rail(client, db, monkeypatch):
    monkeypatch.setattr(public_api, "_get_db", lambda: db)

    org = Organization(
        name="Flat Org",
        slug="flat-org",
        domain="flat.example.com",
        hierarchy_mode="flat",
        primary_color="#0ea5e9",
    )
    db.add(org)
    db.flush()

    guides = Section(
        organization_id=org.id,
        parent_id=None,
        name="Guides",
        slug="guides",
        section_type="section",
        is_published=True,
        display_order=0,
    )
    api = Section(
        organization_id=org.id,
        parent_id=None,
        name="API",
        slug="api",
        section_type="section",
        is_published=True,
        display_order=1,
    )
    db.add_all([guides, api])
    db.flush()

    guides_page = Page(
        organization_id=org.id,
        section_id=guides.id,
        google_doc_id="doc-flat-guides",
        title="Intro",
        slug="intro",
        published_html="<h1>Intro</h1>",
        is_published=True,
        status="published",
        display_order=0,
    )
    api_page = Page(
        organization_id=org.id,
        section_id=api.id,
        google_doc_id="doc-flat-api",
        title="Reference",
        slug="reference",
        published_html="<h1>Reference</h1>",
        is_published=True,
        status="published",
        display_order=0,
    )
    db.add_all([guides_page, api_page])
    db.commit()

    resp = client.get("/docs/flat-org")
    assert resp.status_code == 200
    html = resp.text

    assert "product-rail" not in html
    assert "?product=" not in html
    assert "Guides" in html
    assert "API" in html


def test_flat_hierarchy_page_hides_product_header(client, db, monkeypatch):
    monkeypatch.setattr(public_api, "_get_db", lambda: db)

    org = Organization(
        name="Flat Page Org",
        slug="flat-page-org",
        domain="flat-page.example.com",
        hierarchy_mode="flat",
        primary_color="#0ea5e9",
    )
    db.add(org)
    db.flush()

    section = Section(
        organization_id=org.id,
        parent_id=None,
        name="Guides",
        slug="guides",
        section_type="section",
        is_published=True,
        display_order=0,
    )
    db.add(section)
    db.flush()

    page = Page(
        organization_id=org.id,
        section_id=section.id,
        google_doc_id="doc-flat-page",
        title="Intro",
        slug="intro",
        published_html="<h1>Intro</h1>",
        is_published=True,
        status="published",
        display_order=0,
    )
    db.add(page)
    db.commit()

    resp = client.get(f"/docs/flat-page-org/p/{page.id}/{page.slug}")
    assert resp.status_code == 200
    html = resp.text

    assert 'class="tabsbar-product-link"' not in html
    assert 'aria-label="Product"' not in html
    assert 'aria-label="Tabs"' not in html


def test_flat_hierarchy_supports_root_tabs_without_product_header(client, db, monkeypatch):
    monkeypatch.setattr(public_api, "_get_db", lambda: db)

    org = Organization(
        name="Flat Tabs Org",
        slug="flat-tabs-org",
        domain="flat-tabs.example.com",
        hierarchy_mode="flat",
        primary_color="#0ea5e9",
    )
    db.add(org)
    db.flush()

    api_tab = Section(
        organization_id=org.id,
        parent_id=None,
        name="API",
        slug="api",
        section_type="tab",
        is_published=True,
        display_order=0,
    )
    guides = Section(
        organization_id=org.id,
        parent_id=None,
        name="Guides",
        slug="guides",
        section_type="section",
        is_published=True,
        display_order=1,
    )
    db.add_all([api_tab, guides])
    db.flush()

    api_page = Page(
        organization_id=org.id,
        section_id=api_tab.id,
        google_doc_id="doc-flat-tab-api",
        title="API Intro",
        slug="api-intro",
        published_html="<h1>API Intro</h1>",
        is_published=True,
        status="published",
        display_order=0,
    )
    guides_page = Page(
        organization_id=org.id,
        section_id=guides.id,
        google_doc_id="doc-flat-tab-guides",
        title="Guide Intro",
        slug="guide-intro",
        published_html="<h1>Guide Intro</h1>",
        is_published=True,
        status="published",
        display_order=0,
    )
    db.add_all([api_page, guides_page])
    db.commit()

    resp = client.get(f"/docs/flat-tabs-org/p/{api_page.id}/{api_page.slug}")
    assert resp.status_code == 200
    html = resp.text

    assert 'aria-label="Tabs"' in html
    assert "docs-tab active" in html
    assert 'class="tabsbar-product-link"' not in html
