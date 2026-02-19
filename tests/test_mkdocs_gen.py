from pathlib import Path

from app.publishing.mkdocs_gen import generate_nav, generate_zensical_toml


def test_generate_nav(tmp_path: Path):
    docs = tmp_path / "docs"
    (docs / "release-notes" / "v1-0").mkdir(parents=True)
    (docs / "index.md").write_text("# Home\n")
    (docs / "release-notes" / "v1-0" / "intro.md").write_text("# Intro\n")

    nav = generate_nav(docs)
    assert isinstance(nav, list)
    assert any("Home" in entry for entry in nav)


def test_generate_zensical_toml_contains_project_block(tmp_path: Path):
    docs = tmp_path / "docs"
    (docs / "project-a").mkdir(parents=True)
    (docs / "project-a" / "page.md").write_text("# Page\n")

    toml = generate_zensical_toml(docs)
    assert "[project]" in toml
    assert 'site_name = "AccelDocs"' in toml
