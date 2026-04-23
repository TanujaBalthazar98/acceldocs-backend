"""MCP endpoints for published docs."""

from app.api import public as public_api
from app.models import Organization, Page, Section


def _seed_org(db, *, mcp_enabled: bool = True):
    org = Organization(
        name="MCP Org",
        slug="mcp-org",
        domain="mcp.example.com",
        mcp_enabled=mcp_enabled,
    )
    db.add(org)
    db.flush()

    product = Section(
        organization_id=org.id,
        parent_id=None,
        name="ADOC",
        slug="adoc",
        section_type="section",
        visibility="public",
        is_published=True,
        display_order=0,
    )
    tab = Section(
        organization_id=org.id,
        parent_id=None,
        name="Documentation",
        slug="documentation",
        section_type="tab",
        visibility="public",
        is_published=True,
        display_order=1,
    )
    internal = Section(
        organization_id=org.id,
        parent_id=None,
        name="Internal",
        slug="internal",
        section_type="section",
        visibility="internal",
        is_published=True,
        display_order=2,
    )
    db.add_all([product, tab, internal])
    db.flush()

    page_public = Page(
        organization_id=org.id,
        section_id=product.id,
        google_doc_id="doc-public",
        title="Architecture",
        slug="architecture-2",
        published_html="<h1>Architecture</h1><p>Data observability platform docs.</p>",
        is_published=True,
        status="published",
        display_order=0,
    )
    page_internal = Page(
        organization_id=org.id,
        section_id=internal.id,
        google_doc_id="doc-internal",
        title="Internal Runbook",
        slug="internal-runbook",
        published_html="<h1>Internal Runbook</h1><p>Private troubleshooting content.</p>",
        is_published=True,
        status="published",
        display_order=1,
    )
    db.add_all([page_public, page_internal])
    db.commit()
    return org, page_public, page_internal


def test_org_mcp_info_advertises_rpc_and_tools(client, db, monkeypatch):
    monkeypatch.setattr(public_api, "_get_db", lambda: db)
    org, *_ = _seed_org(db, mcp_enabled=True)

    resp = client.get(f"/docs/{org.slug}/mcp/info")
    assert resp.status_code == 200
    payload = resp.json()

    assert payload["enabled"] is True
    assert payload["transport"]["rpc_url"].endswith(f"/docs/{org.slug}/mcp/rpc")
    tool_names = {tool["name"] for tool in payload["tools"]}
    assert "search_published_docs" in tool_names
    assert "get_published_doc" in tool_names


def test_org_mcp_rpc_search_and_get_only_public_pages(client, db, monkeypatch):
    monkeypatch.setattr(public_api, "_get_db", lambda: db)
    org, page_public, page_internal = _seed_org(db, mcp_enabled=True)

    init_resp = client.post(
        f"/docs/{org.slug}/mcp/rpc",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    assert init_resp.status_code == 200
    assert init_resp.json()["result"]["protocolVersion"] == "2025-03-26"

    search_resp = client.post(
        f"/docs/{org.slug}/mcp/rpc",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "search_published_docs",
                "arguments": {"query": "architecture", "limit": 10},
            },
        },
    )
    assert search_resp.status_code == 200
    search_payload = search_resp.json()["result"]["structuredContent"]
    assert search_payload["count"] == 1
    assert search_payload["results"][0]["id"] == page_public.id
    assert search_payload["results"][0]["slug"] == "architecture-2"
    assert all(r["id"] != page_internal.id for r in search_payload["results"])

    get_resp = client.post(
        f"/docs/{org.slug}/mcp/rpc",
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "get_published_doc",
                "arguments": {"page_id": page_public.id},
            },
        },
    )
    assert get_resp.status_code == 200
    get_payload = get_resp.json()["result"]["structuredContent"]["page"]
    assert get_payload["id"] == page_public.id
    assert get_payload["url_path"].endswith("/architecture-2")


def test_org_mcp_rpc_respects_workspace_toggle(client, db, monkeypatch):
    monkeypatch.setattr(public_api, "_get_db", lambda: db)
    org, *_ = _seed_org(db, mcp_enabled=False)

    resp = client.post(
        f"/docs/{org.slug}/mcp/rpc",
        json={"jsonrpc": "2.0", "id": 99, "method": "initialize", "params": {}},
    )
    assert resp.status_code == 200
    error = resp.json()["error"]
    assert error["code"] == -32004
    assert "disabled" in error["message"].lower()
