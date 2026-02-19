"""Generate MkDocs configuration for the docs site."""

import logging
import re
from pathlib import Path

import yaml

from app.config import settings

logger = logging.getLogger(__name__)

# Matches sequences like "4-10-0" that should become "4.10.0"
_VERSION_RE = re.compile(r"\b(\d+(?:-\d+)+)\b")


def _slug_to_label(slug: str) -> str:
    """Convert a slug like 'version-4-10-0' to a nice label 'Version 4.10.0'."""
    # First, convert hyphens around version numbers to dots
    text = slug.replace("-", " ").replace("_", " ")
    # Find version-number-like groups (e.g., "4 10 0") and join with dots
    parts = text.split()
    result = []
    i = 0
    while i < len(parts):
        # Check if this starts a version number sequence
        if parts[i].isdigit():
            nums = [parts[i]]
            j = i + 1
            while j < len(parts) and parts[j].isdigit():
                nums.append(parts[j])
                j += 1
            if len(nums) >= 2:
                result.append(".".join(nums))
                i = j
                continue
        result.append(parts[i].capitalize() if not result else parts[i])
        i += 1
    return " ".join(result).title() if not any("." in r for r in result) else " ".join(
        w.capitalize() if not any(c.isdigit() for c in w) else w for w in result
    )


def generate_nav(docs_dir: Path) -> list:
    """Build MkDocs nav tree from the docs/ directory structure."""
    if not docs_dir.exists():
        return []

    nav: list = []
    # Top-level index
    if (docs_dir / "index.md").exists():
        nav.append({"Home": "index.md"})

    # Each project folder
    for project_dir in sorted(p for p in docs_dir.iterdir() if p.is_dir() and not p.name.startswith(".")):
        project_nav = _build_folder_nav(project_dir, docs_dir)
        if project_nav:
            label = project_dir.name.replace("-", " ").replace("_", " ").title()
            nav.append({label: project_nav})

    return nav


def _build_folder_nav(folder: Path, docs_root: Path) -> list:
    """Recursively build nav entries for a folder."""
    entries: list = []

    # Folder index
    index_md = folder / "index.md"
    if index_md.exists():
        rel = str(index_md.relative_to(docs_root))
        entries.append({"Overview": rel})

    # Child pages (non-index .md files)
    for page in sorted(folder.glob("*.md")):
        if page.name == "index.md":
            continue
        rel = str(page.relative_to(docs_root))
        label = _slug_to_label(page.stem)
        entries.append({label: rel})

    # Child folders (recurse)
    for child_dir in sorted(p for p in folder.iterdir() if p.is_dir() and not p.name.startswith(".")):
        child_nav = _build_folder_nav(child_dir, docs_root)
        if child_nav:
            label = child_dir.name.replace("-", " ").replace("_", " ").title()
            entries.append({label: child_nav})

    return entries


def generate_mkdocs_yml(docs_dir: Path | None = None) -> str:
    """Generate mkdocs.yml content."""
    if docs_dir is None:
        docs_dir = Path(settings.docs_repo_path) / "docs"

    nav = generate_nav(docs_dir)

    config = {
        "site_name": "AccelDocs",
        "site_description": "Acceldata Documentation",
        "theme": {
            "name": "material",
            "features": [
                "navigation.tabs",
                "navigation.sections",
                "navigation.path",
                "search.suggest",
                "search.highlight",
                "content.code.copy",
            ],
        },
        "nav": nav,
        "markdown_extensions": [
            "tables",
            "admonition",
            "pymdownx.highlight",
            "pymdownx.superfences",
            "toc",
        ],
    }

    return yaml.dump(config, default_flow_style=False, sort_keys=False, allow_unicode=True)


def write_mkdocs_yml(repo_path: Path | None = None) -> Path:
    """Generate and write mkdocs.yml to the repo root."""
    if repo_path is None:
        repo_path = Path(settings.docs_repo_path)

    docs_dir = repo_path / "docs"
    _ensure_folder_indexes(docs_dir)
    content = generate_mkdocs_yml(docs_dir)

    yml_path = repo_path / "mkdocs.yml"
    yml_path.write_text(content, encoding="utf-8")

    # Remove stale zensical.toml if present
    zensical = repo_path / "zensical.toml"
    if zensical.exists():
        zensical.unlink()

    logger.info("Generated mkdocs.yml at %s", yml_path)
    return yml_path


def _ensure_folder_indexes(docs_dir: Path) -> None:
    """Ensure each content folder has a generated index page."""
    if not docs_dir.exists():
        return

    skip_names = {"assets", "static", "images", "img", "css", "js", "fonts"}
    marker = "<!-- auto-generated-index -->"

    for folder in sorted(p for p in docs_dir.rglob("*") if p.is_dir()):
        if folder == docs_dir:
            continue
        if folder.name.lower() in skip_names or folder.name.startswith("."):
            continue
        if not any(p.suffix.lower() == ".md" for p in folder.rglob("*.md")):
            continue

        index_md = folder / "index.md"
        if index_md.exists():
            existing = index_md.read_text(encoding="utf-8")
            if marker not in existing and "Auto-generated index page" not in existing:
                continue

        index_md.write_text(_build_index_md(folder, docs_dir, marker), encoding="utf-8")


def _build_index_md(folder: Path, docs_dir: Path, marker: str) -> str:
    """Build a useful index page with links to children."""
    title = folder.name.replace("-", " ").replace("_", " ").title()
    rel_parts = folder.relative_to(docs_dir).parts
    depth = len(rel_parts)

    lines = [marker, f"# {title}", ""]
    if depth == 1:
        lines.append("Project home.")
    elif depth == 2:
        lines.append("Version home.")
    else:
        lines.append("Section home.")
    lines.append("")

    child_dirs = sorted(
        d for d in folder.iterdir()
        if d.is_dir() and any(d.rglob("*.md")) and not d.name.startswith(".")
    )
    child_pages = sorted(
        p for p in folder.glob("*.md")
        if p.name != "index.md" and not p.name.startswith(".")
    )

    if child_dirs:
        lines.append("## Folders")
        for d in child_dirs:
            label = d.name.replace("-", " ").replace("_", " ").title()
            lines.append(f"- [{label}](./{d.name}/)")
        lines.append("")

    if child_pages:
        lines.append("## Pages")
        for p in child_pages:
            label = p.stem.replace("-", " ").replace("_", " ").title()
            lines.append(f"- [{label}](./{p.stem}/)")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
