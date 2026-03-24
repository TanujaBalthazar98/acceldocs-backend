import markdown

from app.lib.markdown_import import normalize_imported_markdown, normalize_synced_html


def test_normalize_imported_markdown_strips_leaked_frontmatter_block():
    raw = """type: page
title: Version 26.1.0
listed: true
slug: version-26-1-0
description:
index_title: Version 26.1.0
hidden:
keywords:
tags:
---published

# Version 26.1.0

Body content.
"""
    normalized = normalize_imported_markdown(raw)
    assert "type: page" not in normalized
    assert "slug: version-26-1-0" not in normalized
    assert "---published" not in normalized
    assert normalized.startswith("# Version 26.1.0")


def test_normalize_imported_markdown_converts_docusaurus_callout():
    raw = """# Title

:::info What's New
This section consists of new features.
:::
"""
    normalized = normalize_imported_markdown(raw)
    assert '!!! info "What\'s New"' in normalized

    html = markdown.markdown(normalized, extensions=["admonition"])
    assert "admonition" in html
    assert "This section consists of new features." in html


def test_normalize_imported_markdown_converts_github_alert():
    raw = """# Title

> [!WARNING]
> Use this carefully.
"""
    normalized = normalize_imported_markdown(raw)
    assert "!!! warning" in normalized


def test_normalize_imported_markdown_strips_bare_published_key():
    raw = """title: Example
slug: example
published

# Example
"""
    normalized = normalize_imported_markdown(raw)
    assert "published" not in normalized
    assert normalized.startswith("# Example")


def test_normalize_synced_html_rehydrates_leaked_frontmatter():
    raw_html = """
    <p>type: page</p>
    <p>title: Version 26.1.0</p>
    <p>listed: true</p>
    <p>slug: version-26-1-0</p>
    <p>description:</p>
    <p>index_title: Version 26.1.0</p>
    <p>hidden:</p>
    <p>keywords:</p>
    <p>tags:</p>
    <p>---published</p>
    <h1>Version 26.1.0</h1>
    <p>Body content.</p>
    """
    normalized_html = normalize_synced_html(raw_html)
    assert "type: page" not in normalized_html
    assert "slug: version-26-1-0" not in normalized_html
    assert "Version 26.1.0" in normalized_html
    assert "Body content." in normalized_html


def test_normalize_synced_html_keeps_clean_html_unchanged():
    raw_html = "<h1>Clean Title</h1><p>No leaked metadata here.</p>"
    normalized_html = normalize_synced_html(raw_html)
    assert normalized_html == raw_html


def test_normalize_synced_html_rehydrates_single_published_marker():
    raw_html = """
    <p>published</p>
    <p>• First item</p>
    <p>• Second item</p>
    """
    normalized_html = normalize_synced_html(raw_html)
    assert "published" not in normalized_html.lower()
    # List items should be interpreted as list elements, not raw bullet glyph text.
    assert "<li>" in normalized_html
    assert "First item" in normalized_html
    assert "Second item" in normalized_html
