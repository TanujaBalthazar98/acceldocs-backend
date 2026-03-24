import markdown

from app.lib.markdown_import import normalize_imported_markdown


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
