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
    """Build a tab-based nav tree from the docs/ directory structure.

    Hierarchy:
        - Each **project** folder becomes a **tab** in the top bar.
        - Each **topic** subfolder becomes a **section** in the left sidebar.
        - **Pages** (.md files) appear flat under their topic section.

    Single-child wrapper folders (e.g. a lone version folder) are
    automatically flattened so they don't create redundant nesting.

    A root ``index.md`` is always listed first as the landing / home tab.
    """
    if not docs_dir.exists():
        return []

    project_dirs = sorted(
        p for p in docs_dir.iterdir()
        if p.is_dir() and not p.name.startswith(".")
        and p.name.lower() not in _SKIP_DIRS
    )

    nav: list[dict[str, Any]] = []

    # Landing page — always first tab (Home)
    if (docs_dir / "index.md").exists():
        nav.append({"Home": "index.md"})

    for project_dir in project_dirs:
        project_label = _folder_title(project_dir.name)

        # Build the raw nav for this project
        project_nav = _build_folder_nav(project_dir, docs_dir)
        if not project_nav:
            continue

        # Flatten single-child wrappers (e.g. "New Project > V1 > Release Notes")
        flattened_label, flattened_nav = _flatten_single_child(project_label, project_nav)

        # Build the tab entry: the project is a top-level tab.
        # Inside the tab, topics become sections, pages are flat items.
        tab_items = _build_tab_items(flattened_nav, flattened_label)
        nav.append({flattened_label: tab_items})

    return nav


def _build_tab_items(
    nav_items: list[dict[str, Any]], project_label: str
) -> list[dict[str, Any]]:
    """Structure nav items for a project tab.

    - Renames 'Overview' entries to the project label.
    - Keeps sections (topics) and pages in their natural order.
    """
    items: list[dict[str, Any]] = []
    for entry in nav_items:
        for key, val in entry.items():
            if key.lower() == "overview" and isinstance(val, str):
                # Rename Overview → project label so it reads e.g. "Release Notes"
                items.append({project_label: val})
            else:
                items.append({key: val})
    return items


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
        "navigation.tabs",
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
        '    - navigation.tabs',
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
# Landing page & index generation
# ---------------------------------------------------------------------------

def _ensure_landing_page(docs_dir: Path, marker: str) -> None:
    """Generate a root ``docs/index.md`` landing page with project cards.

    The landing page is shown when the user clicks the org name / logo.
    It lists all projects as clickable cards that navigate to the
    corresponding tab.
    """
    index_md = docs_dir / "index.md"

    # Don't overwrite a user-authored landing page
    if index_md.exists():
        existing = index_md.read_text(encoding="utf-8")
        if marker not in existing and "Auto-generated index page" not in existing:
            return

    project_dirs = sorted(
        p for p in docs_dir.iterdir()
        if p.is_dir() and not p.name.startswith(".")
        and p.name.lower() not in _SKIP_DIRS
    )

    lines = [marker, "# Welcome", ""]
    lines.append("Browse the documentation by selecting a project below.")
    lines.append("")

    if project_dirs:
        lines.append('<div class="grid cards" markdown>')
        lines.append("")
        for pdir in project_dirs:
            label = _folder_title(pdir.name)
            # Link to the project's index page (first page in the tab)
            project_index = pdir / "index.md"
            if project_index.exists():
                rel = str(project_index.relative_to(docs_dir))
            else:
                # Find first .md file in the project
                first_md = next(pdir.rglob("*.md"), None)
                rel = str(first_md.relative_to(docs_dir)) if first_md else f"{pdir.name}/"
            lines.append(f"-   :material-book-open-variant: **[{label}]({rel})**")
            lines.append("")
            # Count pages for a subtitle
            page_count = sum(1 for _ in pdir.rglob("*.md"))
            lines.append(f"    {page_count} page{'s' if page_count != 1 else ''}")
            lines.append("")
        lines.append("</div>")
        lines.append("")

    index_md.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    logger.debug("Generated landing page: %s", index_md)

def _ensure_folder_indexes(docs_dir: Path) -> None:
    """Ensure each content folder has a generated index page.

    Also generates a root landing page (``docs/index.md``) with cards
    linking to each project tab if one doesn't already exist.
    """
    if not docs_dir.exists():
        return

    skip_names = {"assets", "static", "images", "img", "css", "js", "fonts", "stylesheets"}
    marker = "<!-- auto-generated-index -->"

    # Root landing page — always regenerate (it lists project cards)
    _ensure_landing_page(docs_dir, marker)

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

    lines = [marker, f"# {title}", ""]

    child_dirs = sorted(
        d for d in folder.iterdir()
        if d.is_dir() and any(d.rglob("*.md")) and not d.name.startswith(".")
        and d.name.lower() not in _SKIP_DIRS
    )
    child_pages = sorted(
        p for p in folder.glob("*.md")
        if p.name != "index.md" and not p.name.startswith(".")
    )

    if child_dirs:
        for d in child_dirs:
            label = _folder_title(d.name)
            lines.append(f"- [{label}](./{d.name}/)")
        lines.append("")

    if child_pages:
        for p in child_pages:
            label = _slug_to_label(p.stem)
            lines.append(f"- [{label}](./{p.stem}/)")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
