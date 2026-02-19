from app.ingestion.metadata import extract_frontmatter


def test_extract_yaml_frontmatter():
    content = """---
slug: release-notes
status: review
visibility: public
---
# Title
Body
"""
    meta = extract_frontmatter(content)
    assert meta["slug"] == "release-notes"
    assert meta["status"] == "review"
    assert meta["visibility"] == "public"


def test_extract_html_frontmatter():
    html = """
    <p>---</p>
    <p>slug: getting-started</p>
    <p>status: approved</p>
    <p>---</p>
    <h1>Hello</h1>
    """
    meta = extract_frontmatter(html)
    assert meta["slug"] == "getting-started"
    assert meta["status"] == "approved"
