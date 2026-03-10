"""Generate Zensical configuration for the docs site.

Replaces the old MkDocs generator. Produces zensical.toml with per-org
branding (colors, fonts, logo) pulled from the Organization record.
"""

import logging
import re as _re
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
    """Convert 'version-4-10-0' → 'Version 4.10.0'.

    Also strips leading numeric ordering prefixes that documentation tools
    add for sorting (e.g. '2-version-4-10-0' → 'Version 4.10.0').
    """
    # Strip leading number prefix used for ordering (e.g. "2-version" → "version")
    text = _re.sub(r"^\d+[-_]\s*", "", slug)
    text = text.replace("-", " ").replace("_", " ")
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


_SKIP_DIRS = {"assets", "static", "images", "img", "css", "js", "fonts", "stylesheets"}


def generate_nav(docs_dir: Path) -> list[dict[str, Any]]:
    """Build nav tree from the docs/ directory structure.

    Applies smart flattening: if a folder has exactly one child folder
    and no direct pages, the child is promoted up to avoid unnecessary
    nesting (e.g. "New Project > V1.0 > Release Notes" becomes just
    "Release Notes" if each level has a single child).

    When the org has only one project, the project's contents are promoted
    directly to the top level (no wrapper section for the project name).
    """
    if not docs_dir.exists():
        return []

    project_dirs = sorted(
        p for p in docs_dir.iterdir()
        if p.is_dir() and not p.name.startswith(".")
        and p.name.lower() not in _SKIP_DIRS
    )

    nav: list[dict[str, Any]] = []

    if len(project_dirs) == 1:
        # Single project: promote its contents directly to the top level.
        # No need for a "Home" entry or a wrapping project section.
        single_dir = project_dirs[0]
        project_nav = _build_folder_nav(single_dir, docs_dir)
        if project_nav:
            # Flatten any single-child wrappers (e.g. version folders)
            _, flattened_nav = _flatten_single_child(
                _folder_title(single_dir.name), project_nav
            )
            # Promote the inner items directly into the top-level nav
            for item in flattened_nav:
                nav.append(item)
        return nav

    # Multiple projects: use Home + project sections
    if (docs_dir / "index.md").exists():
        nav.append({"Home": "index.md"})

    for project_dir in project_dirs:
        project_nav = _build_folder_nav(project_dir, docs_dir)
        if project_nav:
            label = _folder_title(project_dir.name)
            flattened_label, flattened_nav = _flatten_single_child(label, project_nav)
            nav.append({flattened_label: flattened_nav})

    return nav


def _flatten_single_child(label: str, nav_items: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Recursively flatten single-child wrapper folders.

    If a nav section contains only one entry and that entry is a sub-section
    (not a page), replace the parent with the child. Repeat until there's
    more than one child or the child is a page.

    Example: "New Project" > [{"V1.0": [{"Release Notes": [...]}]}]
    becomes: "Release Notes" > [...]
    """
    # Filter out index/overview entries — they don't count as "real" children
    real_items = [
        item for item in nav_items
        if not any(k.lower() in ("overview", "home") and isinstance(v, str)
                   for k, v in item.items())
    ]

    if len(real_items) == 1:
        entry = real_items[0]
        for child_label, child_val in entry.items():
            if isinstance(child_val, list):
                # This child is a folder — promote it and recurse
                return _flatten_single_child(child_label, child_val)

    return label, nav_items


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
        and p.name.lower() not in _SKIP_DIRS
    ):
        child_nav = _build_folder_nav(child_dir, docs_root)
        if child_nav:
            label = _folder_title(child_dir.name)
            entries.append({label: child_nav})

    return entries


# ---------------------------------------------------------------------------
# Config generation
# ---------------------------------------------------------------------------

def _toml_str(value: str) -> str:
    """Escape a string for TOML."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _nav_to_toml_inline(nav: list[dict[str, Any]]) -> str:
    """Serialize a nav list to TOML inline-table format.

    Converts a nested nav structure like:
        [{"Home": "index.md"}, {"Resume": [{"Overview": "resume/index.md"}]}]
    Into TOML inline:
        [{Home = "index.md"}, {Resume = [{Overview = "resume/index.md"}]}]
    """
    items: list[str] = []
    for entry in nav:
        for key, val in entry.items():
            escaped_key = _toml_str(key)
            if isinstance(val, str):
                items.append(f"{{{escaped_key} = {_toml_str(val)}}}")
            elif isinstance(val, list):
                inner = _nav_to_toml_inline(val)
                items.append(f"{{{escaped_key} = [{inner}]}}")
    return ", ".join(items)


def generate_zensical_toml(
    docs_dir: Path | None = None,
    *,
    site_name: str = "Documentation",
    site_description: str = "",
    primary_color: str | None = None,
    accent_color: str | None = None,
    logo_url: str | None = None,
    font_heading: str | None = None,
    font_body: str | None = None,
    custom_css: str | None = None,
    # Extended branding fields
    site_url: str | None = None,
    repo_url: str | None = None,
    repo_name: str | None = None,
    copyright: str | None = None,
    analytics_property_id: str | None = None,
    social_links: list[dict] | None = None,
    site_author: str | None = None,
    edit_uri: str | None = None,
) -> str:
    """Generate zensical.toml content with org branding."""
    if docs_dir is None:
        docs_dir = Path(settings.docs_repo_path) / "docs"

    # Build TOML by hand (simple, no extra dependency needed)
    lines: list[str] = []
    lines.append("[project]")
    lines.append(f"site_name = {_toml_str(site_name)}")
    if site_description:
        lines.append(f"site_description = {_toml_str(site_description)}")
    if site_author:
        lines.append(f"site_author = {_toml_str(site_author)}")
    if site_url:
        lines.append(f"site_url = {_toml_str(site_url)}")
    if repo_url:
        lines.append(f"repo_url = {_toml_str(repo_url)}")
    if repo_name:
        lines.append(f"repo_name = {_toml_str(repo_name)}")
    if edit_uri:
        lines.append(f"edit_uri = {_toml_str(edit_uri)}")
    if copyright:
        lines.append(f"copyright = {_toml_str(copyright)}")
    lines.append("")

    # Extra CSS for custom branding
    if custom_css:
        lines.append('extra_css = ["stylesheets/extra.css"]')
        lines.append("")

    # Docs directory — explicit so Zensical always knows where to find content
    lines.append('docs_dir = "docs"')
    lines.append("")

    # Nav: generate the FULL nav tree from the docs/ directory.
    # Previous approach of omitting nav relied on Zensical auto-discovery,
    # but that proved unreliable. Including the complete tree ensures every
    # committed document appears in the site navigation.
    nav = generate_nav(docs_dir)
    if nav:
        toml_nav = _nav_to_toml_inline(nav)
        lines.append(f"nav = [{toml_nav}]")
        lines.append("")

    # Theme — use the "modern" variant for Inter font + lucide icons
    lines.append("[project.theme]")
    lines.append('variant = "modern"')
    lines.append('language = "en"')
    lines.append("features = [")
    for feat in [
        "content.code.copy",
        "content.code.annotate",
        "content.tabs.link",
        "navigation.footer",
        "navigation.indexes",
        "navigation.instant",
        "navigation.instant.prefetch",
        "navigation.instant.progress",
        "navigation.path",
        "navigation.sections",
        "navigation.top",
        "navigation.tracking",
        "search.highlight",
        "search.suggest",
    ]:
        lines.append(f'    "{feat}",')
    lines.append("]")
    lines.append("")

    # Palette with org branding
    lines.append("[[project.theme.palette]]")
    lines.append('scheme = "default"')
    if primary_color:
        lines.append(f"primary = {_toml_str(primary_color)}")
    if accent_color:
        lines.append(f"accent = {_toml_str(accent_color)}")
    lines.append('toggle.icon = "lucide/sun"')
    lines.append('toggle.name = "Switch to dark mode"')
    lines.append("")

    lines.append("[[project.theme.palette]]")
    lines.append('scheme = "slate"')
    if primary_color:
        lines.append(f"primary = {_toml_str(primary_color)}")
    if accent_color:
        lines.append(f"accent = {_toml_str(accent_color)}")
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

    # Markdown extensions — full set that Zensical supports by default
    lines.append("[project.markdown_extensions]")
    lines.append("abbr = {}")
    lines.append("admonition = {}")
    lines.append('"attr_list" = {}')
    lines.append('"def_list" = {}')
    lines.append("footnotes = {}")
    lines.append('"md_in_html" = {}')
    lines.append("tables = {}")
    lines.append("toc = {permalink = true}")
    lines.append('"pymdownx.arithmatex" = {generic = true}')
    lines.append('"pymdownx.betterem" = {}')
    lines.append('"pymdownx.caret" = {}')
    lines.append('"pymdownx.details" = {}')
    lines.append('"pymdownx.highlight" = {anchor_linenums = true, line_spans = "__span", pygments_lang_class = true}')
    lines.append('"pymdownx.inlinehilite" = {}')
    lines.append('"pymdownx.keys" = {}')
    lines.append('"pymdownx.magiclink" = {}')
    lines.append('"pymdownx.mark" = {}')
    lines.append('"pymdownx.smartsymbols" = {}')
    lines.append('"pymdownx.snippets" = {}')
    lines.append('"pymdownx.superfences" = {custom_fences = [{name = "mermaid", class = "mermaid"}]}')
    lines.append('"pymdownx.tabbed" = {alternate_style = true, combine_header_slug = true}')
    lines.append('"pymdownx.tasklist" = {custom_checkbox = true}')
    lines.append('"pymdownx.tilde" = {}')
    lines.append("")

    # Analytics
    if analytics_property_id:
        lines.append("[project.extra.analytics]")
        lines.append('provider = "google"')
        lines.append(f"property = {_toml_str(analytics_property_id)}")
        lines.append("")

    # Social links
    if social_links:
        for link in social_links:
            if link.get("link"):
                lines.append("[[project.extra.social]]")
                lines.append(f"link = {_toml_str(link['link'])}")
                if link.get("icon"):
                    lines.append(f"icon = {_toml_str(link['icon'])}")
                if link.get("name"):
                    lines.append(f"name = {_toml_str(link['name'])}")
                lines.append("")

    return "\n".join(lines)


def write_zensical_toml(
    repo_path: Path | None = None,
    *,
    site_name: str = "Documentation",
    site_description: str = "",
    primary_color: str | None = None,
    accent_color: str | None = None,
    logo_url: str | None = None,
    font_heading: str | None = None,
    font_body: str | None = None,
    custom_css: str | None = None,
    site_url: str | None = None,
    repo_url: str | None = None,
    repo_name: str | None = None,
    copyright: str | None = None,
    analytics_property_id: str | None = None,
    social_links: list[dict] | None = None,
    site_author: str | None = None,
    edit_uri: str | None = None,
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
        accent_color=accent_color,
        logo_url=logo_url,
        font_heading=font_heading,
        font_body=font_body,
        custom_css=custom_css,
        site_url=site_url,
        repo_url=repo_url,
        repo_name=repo_name,
        copyright=copyright,
        analytics_property_id=analytics_property_id,
        social_links=social_links,
        site_author=site_author,
        edit_uri=edit_uri,
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


def _folder_title(name: str) -> str:
    """Convert a slug folder name to a human-readable title.

    Handles version slugs like 'v1-0' → 'V1.0', 'v2-1-3' → 'V2.1.3'.
    Strips leading numeric ordering prefixes (e.g. '2-release-notes' → 'Release Notes').
    Falls back to title-casing for everything else.
    """
    # Strip leading number prefix used for ordering
    cleaned = _re.sub(r"^\d+[-_]\s*", "", name)
    m = _re.match(r"^v(\d+(?:-\d+)*)$", cleaned)
    if m:
        return "V" + m.group(1).replace("-", ".")
    return cleaned.replace("-", " ").replace("_", " ").title()


def _build_index_md(folder: Path, docs_dir: Path, marker: str) -> str:
    title = _folder_title(folder.name)
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
            label = _folder_title(d.name)
            lines.append(f"- [{label}](./{d.name}/)")
        lines.append("")

    if child_pages:
        lines.append("## Pages")
        for p in child_pages:
            label = _slug_to_label(p.stem)
            lines.append(f"- [{label}](./{p.stem}/)")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
