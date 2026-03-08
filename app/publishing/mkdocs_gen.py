"""Generate Zensical configuration for the docs site.

Replaces the old MkDocs generator. Produces zensical.toml with per-org
branding (colors, fonts, logo) pulled from the Organization record.
"""

import logging
import re
from pathlib import Path
from typing import Any

try:
    import tomli_w  # Python 3.11+ or pip install tomli-w
except ImportError:
    tomli_w = None  # type: ignore[assignment]

import yaml  # fallback: write TOML by hand if tomli_w unavailable

from app.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Nav helpers
# ---------------------------------------------------------------------------

def _slug_to_label(slug: str) -> str:
    """Convert 'version-4-10-0' → 'Version 4.10.0'."""
    text = slug.replace("-", " ").replace("_", " ")
    parts = text.split()
    result: list[str] = []
    i = 0
    while i < len(parts):
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
    if any("." in r for r in result):
        return " ".join(
            w.capitalize() if not any(c.isdigit() for c in w) else w
            for w in result
        )
    return " ".join(result).title()


def generate_nav(docs_dir: Path) -> list[dict[str, Any]]:
    """Build nav tree from the docs/ directory structure."""
    if not docs_dir.exists():
        return []

    nav: list[dict[str, Any]] = []
    if (docs_dir / "index.md").exists():
        nav.append({"Home": "index.md"})

    for project_dir in sorted(
        p for p in docs_dir.iterdir()
        if p.is_dir() and not p.name.startswith(".")
    ):
        project_nav = _build_folder_nav(project_dir, docs_dir)
        if project_nav:
            label = project_dir.name.replace("-", " ").replace("_", " ").title()
            nav.append({label: project_nav})

    return nav


def _build_folder_nav(folder: Path, docs_root: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []

    index_md = folder / "index.md"
    if index_md.exists():
        rel = str(index_md.relative_to(docs_root))
        entries.append({"Overview": rel})

    for page in sorted(folder.glob("*.md")):
        if page.name == "index.md":
            continue
        rel = str(page.relative_to(docs_root))
        label = _slug_to_label(page.stem)
        entries.append({label: rel})

    for child_dir in sorted(
        p for p in folder.iterdir()
        if p.is_dir() and not p.name.startswith(".")
    ):
        child_nav = _build_folder_nav(child_dir, docs_root)
        if child_nav:
            label = child_dir.name.replace("-", " ").replace("_", " ").title()
            entries.append({label: child_nav})

    return entries


# ---------------------------------------------------------------------------
# Config generation
# ---------------------------------------------------------------------------

def _toml_str(value: str) -> str:
    """Escape a string for TOML."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def generate_zensical_toml(
    docs_dir: Path | None = None,
    *,
    site_name: str = "Documentation",
    site_description: str = "",
    primary_color: str | None = None,
    logo_url: str | None = None,
    font_heading: str | None = None,
    font_body: str | None = None,
    custom_css: str | None = None,
) -> str:
    """Generate zensical.toml content with org branding."""
    if docs_dir is None:
        docs_dir = Path(settings.docs_repo_path) / "docs"

    nav = generate_nav(docs_dir)

    # Build TOML by hand (simple, no extra dependency needed)
    lines: list[str] = []
    lines.append("[project]")
    lines.append(f"site_name = {_toml_str(site_name)}")
    if site_description:
        lines.append(f"site_description = {_toml_str(site_description)}")
    lines.append("")

    # Extra CSS for custom branding
    if custom_css:
        lines.append('extra_css = ["stylesheets/extra.css"]')
        lines.append("")

    # Nav
    if nav:
        # Convert nav to TOML array-of-tables inline format
        nav_parts: list[str] = []
        for item in nav:
            for label, value in item.items():
                if isinstance(value, str):
                    nav_parts.append(f'{{ {_toml_str(label)} = {_toml_str(value)} }}')
                # Nested nav items are too complex for inline TOML — omit them
                # and let Zensical auto-derive from directory structure
        if nav_parts:
            lines.append(f"nav = [{', '.join(nav_parts)}]")
            lines.append("")

    # Theme
    lines.append("[project.theme]")
    lines.append('language = "en"')
    lines.append("features = [")
    for feat in [
        "content.code.copy",
        "content.code.annotate",
        "navigation.footer",
        "navigation.indexes",
        "navigation.instant",
        "navigation.instant.prefetch",
        "navigation.path",
        "navigation.sections",
        "navigation.top",
        "navigation.tracking",
        "search.highlight",
    ]:
        lines.append(f'    "{feat}",')
    lines.append("]")
    lines.append("")

    # Palette with org branding
    lines.append("[[project.theme.palette]]")
    lines.append('scheme = "default"')
    if primary_color:
        lines.append(f"primary = {_toml_str(primary_color)}")
    lines.append('toggle.icon = "lucide/sun"')
    lines.append('toggle.name = "Switch to dark mode"')
    lines.append("")

    lines.append("[[project.theme.palette]]")
    lines.append('scheme = "slate"')
    if primary_color:
        lines.append(f"primary = {_toml_str(primary_color)}")
    lines.append('toggle.icon = "lucide/moon"')
    lines.append('toggle.name = "Switch to light mode"')
    lines.append("")

    # Fonts
    if font_body or font_heading:
        lines.append("[project.theme.font]")
        if font_body:
            lines.append(f"text = {_toml_str(font_body)}")
        if font_heading:
            # Zensical doesn't have a heading font option, but we use text
            pass
        lines.append("")

    # Logo
    if logo_url:
        lines.append("[project.theme.icon]")
        lines.append(f"logo = {_toml_str(logo_url)}")
        lines.append("")

    # Markdown extensions
    lines.append("[project.markdown_extensions]")
    lines.append("tables = {}")
    lines.append("admonition = {}")
    lines.append('"pymdownx.highlight" = {}')
    lines.append('"pymdownx.superfences" = {}')
    lines.append("toc = {}")
    lines.append("")

    return "\n".join(lines)


def write_zensical_toml(
    repo_path: Path | None = None,
    *,
    site_name: str = "Documentation",
    site_description: str = "",
    primary_color: str | None = None,
    logo_url: str | None = None,
    font_heading: str | None = None,
    font_body: str | None = None,
    custom_css: str | None = None,
) -> Path:
    """Generate and write zensical.toml to the repo root."""
    if repo_path is None:
        repo_path = Path(settings.docs_repo_path)

    docs_dir = repo_path / "docs"
    _ensure_folder_indexes(docs_dir)

    content = generate_zensical_toml(
        docs_dir,
        site_name=site_name,
        site_description=site_description,
        primary_color=primary_color,
        logo_url=logo_url,
        font_heading=font_heading,
        font_body=font_body,
        custom_css=custom_css,
    )

    toml_path = repo_path / "zensical.toml"
    toml_path.write_text(content, encoding="utf-8")

    # Write custom CSS if provided
    if custom_css:
        css_dir = docs_dir / "stylesheets"
        css_dir.mkdir(parents=True, exist_ok=True)
        (css_dir / "extra.css").write_text(custom_css, encoding="utf-8")

    # Remove stale mkdocs.yml if present
    mkdocs_yml = repo_path / "mkdocs.yml"
    if mkdocs_yml.exists():
        mkdocs_yml.unlink()

    logger.info("Generated zensical.toml at %s", toml_path)
    return toml_path


# Keep backward-compatible alias so git_publisher.py doesn't break
def write_mkdocs_yml(repo_path: Path | None = None, **branding) -> Path:
    """Backward-compatible wrapper — now generates zensical.toml."""
    return write_zensical_toml(repo_path, **branding)


# ---------------------------------------------------------------------------
# MkDocs Material YAML (used by GitHub Actions CI build)
# ---------------------------------------------------------------------------

def generate_mkdocs_yml_content(
    *,
    site_name: str = "Documentation",
    site_description: str = "",
    primary_color: str | None = None,
    font_body: str | None = None,
    **_,  # absorb extra kwargs (logo_url, custom_css, font_heading …)
) -> str:
    """Generate a mkdocs.yml for MkDocs Material that GitHub Actions can build."""
    lines = [
        f'site_name: "{site_name}"',
        'docs_dir: docs',
        '',
        'theme:',
        '  name: material',
        '  language: en',
        '  features:',
        '    - content.code.copy',
        '    - content.code.annotate',
        '    - navigation.footer',
        '    - navigation.indexes',
        '    - navigation.instant',
        '    - navigation.instant.prefetch',
        '    - navigation.path',
        '    - navigation.sections',
        '    - navigation.top',
        '    - navigation.tracking',
        '    - search.highlight',
        '  palette:',
        '    - scheme: default',
    ]
    if primary_color:
        lines.append(f'      primary: "{primary_color}"')
    lines += [
        '      toggle:',
        '        icon: material/brightness-7',
        '        name: Switch to dark mode',
        '    - scheme: slate',
    ]
    if primary_color:
        lines.append(f'      primary: "{primary_color}"')
    lines += [
        '      toggle:',
        '        icon: material/brightness-4',
        '        name: Switch to light mode',
    ]
    if font_body:
        lines += ['', f'  font:', f'    text: "{font_body}"']
    if site_description:
        lines += ['', f'site_description: "{site_description}"']
    lines += [
        '',
        'markdown_extensions:',
        '  - tables',
        '  - admonition',
        '  - toc',
        '  - pymdownx.highlight:',
        '      anchor_linenums: true',
        '  - pymdownx.superfences',
    ]
    return '\n'.join(lines) + '\n'


# ---------------------------------------------------------------------------
# Index generation (unchanged)
# ---------------------------------------------------------------------------

def _ensure_folder_indexes(docs_dir: Path) -> None:
    """Ensure each content folder has a generated index page."""
    if not docs_dir.exists():
        return

    skip_names = {"assets", "static", "images", "img", "css", "js", "fonts", "stylesheets"}
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
