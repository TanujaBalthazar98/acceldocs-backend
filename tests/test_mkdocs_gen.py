from pathlib import Path

from app.publishing.mkdocs_gen import generate_nav, generate_mkdocs_yml


def test_generate_nav(tmp_path: Path):
    docs = tmp_path / "docs"
    (docs / "release-notes").mkdir(parents=True)
    (docs / "index.md").write_text("# Home\n")
    (docs / "release-notes" / "intro.md").write_text("# Intro\n")

    nav = generate_nav(docs)
    assert isinstance(nav, list)
    assert any("Home" in entry for entry in nav)


def test_generate_mkdocs_yml_contains_site_name(tmp_path: Path):
    docs = tmp_path / "docs"
    (docs / "project-a").mkdir(parents=True)
    (docs / "project-a" / "page.md").write_text("# Page\n")

    yml = generate_mkdocs_yml(docs)
    assert "site_name: AccelDocs" in yml
    assert "material" in yml
