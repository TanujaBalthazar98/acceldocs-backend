"""Generate Zensical site configuration for docs output."""

import logging
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

def generate_nav(docs_dir: Path) -> list[dict[str, str]]:
    """Build a flat nav list for top-level markdown files.

    Zensical derives hierarchy automatically from docs/ folder structure,
    so we only provide optional top-level links.
    """
    if not docs_dir.exists():
        return []

    nav: list[dict[str, str]] = []
    for md_file in sorted(docs_dir.glob("*.md")):
        title = md_file.stem.replace("-", " ").replace("_", " ").title()
        if md_file.name == "index.md":
            title = "Home"
        nav.append({title: md_file.name})
    return nav


def generate_zensical_toml(docs_dir: Path | None = None) -> str:
    """Generate a minimal zensical.toml."""
    if docs_dir is None:
        docs_dir = Path(settings.docs_repo_path) / "docs"

    nav_items = generate_nav(docs_dir)
    nav_lines = ""
    if nav_items:
        rendered = []
        for item in nav_items:
            (k, v), = item.items()
            rendered.append(f'  {{ "{k}" = "{v}" }}')
        nav_lines = "nav = [\n" + ",\n".join(rendered) + "\n]\n"

    return (
        "[project]\n"
        'site_name = "AccelDocs"\n'
        'site_description = "Generated from Google Docs."\n'
        + nav_lines
        + "\n[project.theme]\n"
        'language = "en"\n'
        "features = [\n"
        '  "navigation.sections",\n'
        '  "navigation.path",\n'
        '  "search.highlight",\n'
        '  "content.code.copy",\n'
        "]\n"
    )


def write_zensical_toml(repo_path: Path | None = None) -> Path:
    """Generate and write zensical.toml to the repo root."""
    if repo_path is None:
        repo_path = Path(settings.docs_repo_path)

    docs_dir = repo_path / "docs"
    toml_content = generate_zensical_toml(docs_dir)

    toml_path = repo_path / "zensical.toml"
    toml_path.write_text(toml_content, encoding="utf-8")

    # Remove stale mkdocs config if present to avoid tool confusion.
    mkdocs_yml = repo_path / "mkdocs.yml"
    if mkdocs_yml.exists():
        mkdocs_yml.unlink()

    logger.info("Generated zensical.toml at %s", toml_path)
    return toml_path


# Backward-compat names used by older imports/tests.
def generate_mkdocs_yml(docs_dir: Path | None = None) -> str:
    return generate_zensical_toml(docs_dir)


def write_mkdocs_yml(repo_path: Path | None = None) -> Path:
    return write_zensical_toml(repo_path)
