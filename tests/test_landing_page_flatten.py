from pathlib import Path

from app.publishing.mkdocs_gen import write_zensical_toml


def test_landing_page_flattens_single_child_wrapper(tmp_path: Path):
    docs_dir = tmp_path / "docs" / "new-project" / "release-notes"
    docs_dir.mkdir(parents=True)
    (docs_dir / "version-4-9-0.md").write_text("# Version 4.9.0\n", encoding="utf-8")

    write_zensical_toml(repo_path=tmp_path, site_name="Test Docs")

    landing = (tmp_path / "docs" / "index.md").read_text(encoding="utf-8")
    assert "## [Release Notes]" in landing
    assert "## [New Project]" not in landing
