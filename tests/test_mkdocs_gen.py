from pathlib import Path

from app.publishing.mkdocs_gen import generate_nav


def test_generate_nav(tmp_path: Path):
    docs = tmp_path / "docs"
    (docs / "release-notes").mkdir(parents=True)
    (docs / "index.md").write_text("# Home\n")
    (docs / "release-notes" / "intro.md").write_text("# Intro\n")

    nav = generate_nav(docs)
    assert isinstance(nav, list)
    assert any("Home" in entry for entry in nav)
