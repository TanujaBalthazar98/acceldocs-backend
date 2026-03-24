from pathlib import Path

from app.conversion.html_to_md import convert_html_to_markdown


def test_html_to_markdown_basic():
    html = "<h1>Title</h1><p>Hello <strong>world</strong></p>"
    md = convert_html_to_markdown(html)
    assert "# Title" in md
    assert "Hello" in md


def test_html_to_markdown_strips_frontmatter():
    html = """
    <p>---</p>
    <p>slug: sample</p>
    <p>---</p>
    <h1>Doc</h1>
    """
    md = convert_html_to_markdown(html)
    assert "slug: sample" not in md
    assert "# Doc" in md


def test_image_rewrite_without_download(tmp_path: Path):
    html = '<p><img src="https://lh3.googleusercontent.com/abc123" /></p>'
    md = convert_html_to_markdown(html, download_images=False)
    assert "googleusercontent" in md


def test_html_to_markdown_pandoc_falls_back_to_markdownify(monkeypatch):
    html = "<h1>Title</h1><p>Hello world</p>"

    # Simulate missing Pandoc in environment.
    monkeypatch.setattr("app.conversion.html_to_md.shutil.which", lambda _: None)
    md = convert_html_to_markdown(html, engine="pandoc")

    assert "# Title" in md
    assert "Hello world" in md


def test_html_to_markdown_uses_pandoc_when_available(monkeypatch):
    html = "<h1>Title</h1><p>Hello world</p>"

    class _Result:
        stdout = "# Title\n\nHello world\n"

    monkeypatch.setattr("app.conversion.html_to_md.shutil.which", lambda _: "/usr/bin/pandoc")
    monkeypatch.setattr(
        "app.conversion.html_to_md.subprocess.run",
        lambda *args, **kwargs: _Result(),
    )

    md = convert_html_to_markdown(html, engine="pandoc")
    assert "# Title" in md
    assert "Hello world" in md
