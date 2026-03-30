#!/usr/bin/env python3
"""Migrate any public DeveloperHub documentation site into AccelDocs.

Usage:
    python scripts/migrate_developerhub.py \\
        --source https://docs.acceldata.io/documentation \\
        --backend http://localhost:8000 \\
        --token <jwt_token> \\
        --org-id <org_id> \\
        --product-id <product_id> \\
        --dry-run

The script:
  1. Discovers the full navigation hierarchy from the sidebar
  2. Fetches each page and converts HTML → Markdown (pandoc or html2text)
  3. Handles DeveloperHub tab components
  4. Creates sections in AccelDocs via POST /api/sections
  5. Imports pages via POST /api/drive/import/local (multipart upload)
  6. Rewrites internal links using placeholder tokens
  7. Saves/resumes state from migration_state.json
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

# ---------------------------------------------------------------------------
# Optional imports — graceful degradation if not installed
# ---------------------------------------------------------------------------
try:
    import requests
except ImportError:
    print("ERROR: 'requests' is not installed. Run: pip install requests", file=sys.stderr)
    sys.exit(1)

try:
    from bs4 import BeautifulSoup, Tag
except ImportError:
    print("ERROR: 'beautifulsoup4' is not installed. Run: pip install beautifulsoup4", file=sys.stderr)
    sys.exit(1)

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

LOG_FILE = Path("migration.log")

def _setup_logging() -> logging.Logger:
    logger = logging.getLogger("migrate_developerhub")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")

    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.INFO)
    sh.setFormatter(fmt)

    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


log = _setup_logging()

# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

STATE_FILE = Path("migration_state.json")


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("Could not load %s: %s — starting fresh", STATE_FILE, exc)
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    log.debug("State saved to %s", STATE_FILE)


# ---------------------------------------------------------------------------
# HTML fetch helpers
# ---------------------------------------------------------------------------

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (compatible; AccelDocs-Migrator/1.0; "
        "+https://github.com/acceldocs/migrator)"
    )
})

_MAX_RETRIES = 3
_RETRY_DELAY = 1.0
_REQUEST_DELAY = 0.5


def fetch_html(url: str) -> str | None:
    """Fetch URL with retries. Returns HTML string or None on failure."""
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            resp = SESSION.get(url, timeout=30)
            resp.raise_for_status()
            time.sleep(_REQUEST_DELAY)
            return resp.text
        except requests.RequestException as exc:
            log.warning("Attempt %d/%d failed for %s: %s", attempt, _MAX_RETRIES, url, exc)
            if attempt < _MAX_RETRIES:
                time.sleep(_RETRY_DELAY * attempt)
    log.error("All retries exhausted for %s", url)
    return None


# ---------------------------------------------------------------------------
# Step 1 — Sidebar discovery
# ---------------------------------------------------------------------------

_SIDEBAR_SELECTORS = [
    lambda soup: soup.find("nav", attrs={"aria-label": True}),
    lambda soup: soup.find(class_="sidebar-nav"),
    lambda soup: soup.find(lambda t: t.name and "sidebar" in " ".join(t.get("class", [])) and t.find("nav")),
    lambda soup: soup.find(lambda t: t.name and "navigation" in " ".join(t.get("class", []))),
    lambda soup: soup.find("nav"),
]

_SELECTOR_NAMES = [
    "nav[aria-label]",
    ".sidebar-nav",
    "[class*='sidebar'] nav",
    "[class*='navigation']",
    "nav (fallback)",
]


def _find_nav(soup: BeautifulSoup) -> Tag | None:
    for idx, selector in enumerate(_SIDEBAR_SELECTORS):
        result = selector(soup)
        if result:
            log.info("Sidebar found using selector: %s", _SELECTOR_NAMES[idx])
            return result
    return None


def _slugify(text: str) -> str:
    """Simple slug: lowercase, hyphens, no specials."""
    s = text.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "page"


def _walk_nav_tree(element: Tag, base_url: str, depth: int = 0) -> list[dict]:
    """Recursively walk <ul>/<li> tree and return a list of node dicts."""
    nodes: list[dict] = []

    items = element.find_all("li", recursive=False)
    if not items:
        # Some navs use a flat structure without explicit <li>
        items = element.find_all(["li", "a"], recursive=False)

    for li in items:
        # Find the direct anchor (if any)
        anchor = li.find("a", recursive=False) or li.find("a")
        href = anchor.get("href", "") if anchor else ""
        title = ""
        if anchor:
            title = anchor.get_text(separator=" ", strip=True)
        else:
            # Heading-only item — use first text node
            title = li.get_text(separator=" ", strip=True).split("\n")[0].strip()

        url: str | None = None
        if href and not href.startswith("#") and not href.startswith("javascript"):
            url = urljoin(base_url, href)

        node: dict[str, Any] = {
            "title": title or "Untitled",
            "url": url,
            "depth": depth,
            "children": [],
        }

        # Recurse into nested <ul>
        child_ul = li.find("ul", recursive=False)
        if child_ul:
            node["children"] = _walk_nav_tree(child_ul, base_url, depth + 1)
        else:
            # Some DeveloperHub navs nest deeper
            child_list = li.find(["ul", "ol"])
            if child_list:
                node["children"] = _walk_nav_tree(child_list, base_url, depth + 1)

        nodes.append(node)

    return nodes


def _collect_all_urls(tree: list[dict]) -> list[str]:
    """Flatten tree into all unique page URLs (depth-first)."""
    urls: list[str] = []
    for node in tree:
        if node["url"]:
            urls.append(node["url"])
        urls.extend(_collect_all_urls(node["children"]))
    return urls


def _print_tree(tree: list[dict], indent: int = 0) -> None:
    for node in tree:
        prefix = "  " * indent
        url_str = f" → {node['url']}" if node["url"] else " (section heading)"
        print(f"{prefix}{'├─' if indent else '•'} {node['title']}{url_str}")
        _print_tree(node["children"], indent + 1)


def _fetch_sitemap_urls(base_url: str, source_path_prefix: str) -> list[str]:
    """
    Try to fetch sitemap.xml and return URLs matching the source path prefix.
    Returns empty list if sitemap not available or contains no matching URLs.
    """
    parsed = urlparse(base_url)
    sitemap_url = urlunparse((parsed.scheme, parsed.netloc, "/sitemap.xml", "", "", ""))
    log.info("Checking sitemap: %s", sitemap_url)
    html = fetch_html(sitemap_url)
    if not html:
        return []

    try:
        soup = BeautifulSoup(html, "xml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    urls: list[str] = []
    for loc in soup.find_all("loc"):
        u = loc.get_text(strip=True)
        if not u:
            continue
        u_path = urlparse(u).path
        # Only include URLs under the source path prefix (e.g. /documentation/)
        if source_path_prefix and not u_path.startswith(source_path_prefix):
            continue
        urls.append(u)

    log.info("Sitemap: found %d matching URLs under %s", len(urls), source_path_prefix)
    return list(dict.fromkeys(urls))


def _sitemap_urls_to_tree(urls: list[str]) -> list[dict]:
    """
    Convert a flat list of URLs into a shallow tree grouped by path segment.
    Each URL becomes a page node. Path segments become section headings.

    e.g. /documentation/connectors/s3  → section "Connectors" → page "S3"
         /documentation/getting-started → page "Getting Started" (top-level)
    """
    # Determine the common path prefix to strip
    if not urls:
        return []

    paths = [urlparse(u).path.strip("/").split("/") for u in urls]
    # Find deepest common prefix
    min_parts = min(len(p) for p in paths)
    common_depth = 0
    for i in range(min_parts):
        if len({p[i] for p in paths}) == 1:
            common_depth = i + 1
        else:
            break

    # Build tree: group by the segment AFTER the common prefix
    # Structure: section_slug → [page, ...]
    sections: dict[str, list[dict]] = {}
    top_pages: list[dict] = []

    for url, parts in zip(urls, paths):
        relative = parts[common_depth:]
        if not relative:
            continue

        # Title from last path segment
        title = relative[-1].replace("-", " ").replace("_", " ").title()

        if len(relative) == 1:
            # Top-level page
            top_pages.append({"title": title, "url": url, "depth": 0, "children": []})
        else:
            # Nested page — use first relative segment as section name
            section_slug = relative[0]
            section_title = section_slug.replace("-", " ").replace("_", " ").title()
            if section_slug not in sections:
                sections[section_slug] = []
            sections[section_slug].append(
                {"title": title, "url": url, "depth": 1, "children": []}
            )

    tree: list[dict] = []
    # Add top-level pages first
    tree.extend(top_pages)
    # Add sections
    for section_slug, pages in sections.items():
        section_title = section_slug.replace("-", " ").replace("_", " ").title()
        tree.append({
            "title": section_title,
            "url": None,
            "depth": 0,
            "children": pages,
        })

    return tree


def discover_structure(source_url: str) -> tuple[list[dict], list[str]]:
    """
    Load source URL, find sidebar, walk navigation tree.
    Falls back to sitemap.xml for JavaScript-rendered (SPA) DeveloperHub sites.
    Returns (tree, fallback_link_list).
    """
    log.info("Fetching source page: %s", source_url)
    html = fetch_html(source_url)
    if not html:
        log.error("Could not load source URL: %s", source_url)
        sys.exit(1)

    soup = BeautifulSoup(html, "html.parser")
    parsed = urlparse(source_url)
    base_url = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
    source_path = parsed.path.rstrip("/")

    nav = _find_nav(soup)
    tree: list[dict] = []
    if nav:
        tree = _walk_nav_tree(nav, source_url)

    # Fallback 1: collect all internal <a href> links from the page
    fallback_links: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("/") or href.startswith(parsed.scheme + "://"):
            full = urljoin(source_url, href)
            if urlparse(full).netloc == parsed.netloc:
                fallback_links.append(full)
    fallback_links = list(dict.fromkeys(fallback_links))

    # Fallback 2: sitemap.xml — used when the site is a JavaScript SPA
    # (no HTML-rendered sidebar, no links discoverable from initial HTML)
    if not tree and not fallback_links:
        log.info("Page appears to be a JS-rendered SPA — trying sitemap.xml discovery")
        sitemap_urls = _fetch_sitemap_urls(base_url, source_path + "/")
        if not sitemap_urls:
            # Try without trailing slash (some sitemaps omit the base path)
            sitemap_urls = _fetch_sitemap_urls(base_url, source_path)
        if sitemap_urls:
            log.info("Using sitemap.xml: %d URLs found", len(sitemap_urls))
            tree = _sitemap_urls_to_tree(sitemap_urls)
            fallback_links = sitemap_urls
        else:
            log.warning("Sitemap yielded no matching URLs — discovery incomplete")
    elif not tree and fallback_links:
        log.warning("No structured sidebar found — using flat link list as pages")
        # Create a flat tree from fallback links
        for url in fallback_links:
            path_parts = urlparse(url).path.strip("/").split("/")
            title = path_parts[-1].replace("-", " ").replace("_", " ").title() if path_parts else "Page"
            tree.append({"title": title, "url": url, "depth": 0, "children": []})

    log.info(
        "Discovery: %d top-level tree nodes, %d fallback links",
        len(tree),
        len(fallback_links),
    )
    return tree, fallback_links


# ---------------------------------------------------------------------------
# Step 2 — DeveloperHub tab handling
# ---------------------------------------------------------------------------

_TAB_SELECTORS = [
    {"class": "tabs-wrapper"},
    {"class": "tab-group"},
    {"role": "tabpanel"},
]


def _convert_tabs_to_markdown(soup: BeautifulSoup) -> BeautifulSoup:
    """Convert DeveloperHub tab components to MkDocs-style tab syntax."""
    # Handle <tab-group> web component
    for tab_group in soup.find_all("tab-group"):
        tabs = tab_group.find_all(["tab", "tab-panel"])
        md_lines: list[str] = []
        if tabs:
            for tab in tabs:
                label = tab.get("label") or tab.get("title") or tab.name
                content = tab.get_text(separator="\n", strip=True)
                md_lines.append(f'=== "{label}"')
                for line in content.splitlines():
                    md_lines.append(f"    {line}")
                md_lines.append("")
        placeholder = soup.new_tag("pre")
        placeholder.string = "\n".join(md_lines)
        tab_group.replace_with(placeholder)

    # Handle div.tabs-wrapper / div.tab-group
    for wrapper in soup.find_all("div", class_=re.compile(r"tabs?[-_]?(wrapper|group|container)", re.I)):
        panels = wrapper.find_all(["div", "section"], attrs={"data-tab": True}) or \
                 wrapper.find_all(["div", "section"], role="tabpanel") or \
                 wrapper.find_all(["div", "section"], class_=re.compile(r"tab[-_]?(panel|content|pane)", re.I))

        # Also collect tab labels from nav/button elements
        tab_labels = [
            btn.get_text(strip=True)
            for btn in wrapper.find_all(["button", "a", "li"], class_=re.compile(r"tab[-_]?(item|label|button|link)?", re.I))
        ]

        md_lines = []
        for idx, panel in enumerate(panels):
            label = (
                panel.get("data-tab")
                or panel.get("aria-label")
                or (tab_labels[idx] if idx < len(tab_labels) else f"Tab {idx + 1}")
            )
            content = panel.get_text(separator="\n", strip=True)
            md_lines.append(f'=== "{label}"')
            for line in content.splitlines():
                md_lines.append(f"    {line}")
            md_lines.append("")

        if md_lines:
            placeholder = soup.new_tag("pre")
            placeholder.string = "\n".join(md_lines)
            wrapper.replace_with(placeholder)

    return soup


# ---------------------------------------------------------------------------
# Step 3 — Page fetch + HTML → Markdown conversion
# ---------------------------------------------------------------------------

_CONTENT_SELECTORS = [
    lambda soup: soup.find("main"),
    lambda soup: soup.find("article"),
    lambda soup: soup.find(class_="content-body"),
    lambda soup: soup.find(attrs={"role": "main"}),
    lambda soup: soup.find(class_="page-content"),
    lambda soup: soup.find(class_="docs-content"),
    lambda soup: soup.find(class_=re.compile(r"content", re.I)),
]


def _resolve_images(soup: BeautifulSoup, page_url: str) -> BeautifulSoup:
    """Rewrite relative image src to absolute URLs."""
    for img in soup.find_all("img", src=True):
        src = img["src"]
        if src and not src.startswith(("http://", "https://", "data:")):
            img["src"] = urljoin(page_url, src)
    return soup


_PANDOC_AVAILABLE: bool | None = None


def _check_pandoc() -> bool:
    global _PANDOC_AVAILABLE
    if _PANDOC_AVAILABLE is None:
        try:
            result = subprocess.run(
                ["pandoc", "--version"],
                capture_output=True,
                timeout=5,
            )
            _PANDOC_AVAILABLE = result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            _PANDOC_AVAILABLE = False
        if _PANDOC_AVAILABLE:
            log.info("pandoc is available — using for HTML→Markdown conversion")
        else:
            log.warning("pandoc not found — falling back to html2text/basic conversion")
    return _PANDOC_AVAILABLE


def _html_to_markdown_pandoc(html: str) -> str:
    result = subprocess.run(
        [
            "pandoc",
            "--from=html",
            "--to=gfm+pipe_tables+task_lists+fenced_code_blocks",
            "--wrap=none",
        ],
        input=html,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        log.warning("pandoc error: %s", result.stderr[:200])
        return result.stdout or ""
    return result.stdout


def _html_to_markdown_fallback(html: str) -> str:
    """Basic HTML → Markdown using beautifulsoup text extraction."""
    try:
        import html2text as _h2t  # type: ignore
        h = _h2t.HTML2Text()
        h.ignore_links = False
        h.ignore_images = False
        h.body_width = 0
        h.protect_links = True
        h.wrap_links = False
        return h.handle(html)
    except ImportError:
        pass

    # Last resort: pure BeautifulSoup text dump
    soup = BeautifulSoup(html, "html.parser")
    lines: list[str] = []
    for elem in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "pre", "code"]):
        tag = elem.name
        text = elem.get_text(separator=" ", strip=True)
        if not text:
            continue
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = int(tag[1])
            lines.append(f"{'#' * level} {text}\n")
        elif tag == "li":
            lines.append(f"- {text}")
        elif tag in ("pre", "code"):
            lines.append(f"```\n{text}\n```\n")
        else:
            lines.append(f"{text}\n")
    return "\n".join(lines)


def fetch_and_convert_page(url: str) -> dict | None:
    """
    Fetch a page, extract main content, handle tabs, convert to Markdown.
    Returns dict with keys: url, title, markdown, raw_html
    """
    log.debug("Fetching page: %s", url)
    html = fetch_html(url)
    if not html:
        log.warning("Could not fetch page: %s", url)
        return None

    soup = BeautifulSoup(html, "html.parser")
    title = ""
    title_tag = soup.find("title")
    if title_tag:
        title = title_tag.get_text(strip=True)
        # DeveloperHub often has "Page Title | Product Name"
        if " | " in title:
            title = title.split(" | ")[0].strip()
        elif " - " in title:
            title = title.split(" - ")[0].strip()

    # Handle tab components before extracting content
    soup = _convert_tabs_to_markdown(soup)

    # Extract main content area
    content_elem = None
    for selector in _CONTENT_SELECTORS:
        elem = selector(soup)
        if elem and elem.get_text(strip=True):
            content_elem = elem
            break

    if not content_elem:
        log.warning("No main content found on %s — using body", url)
        content_elem = soup.find("body") or soup

    # Resolve relative images
    content_elem = _resolve_images(content_elem, url)

    # Try to extract h1 as page title
    h1 = content_elem.find("h1")
    if h1:
        h1_text = h1.get_text(strip=True)
        if h1_text:
            title = h1_text

    content_html = str(content_elem)

    if _check_pandoc():
        markdown = _html_to_markdown_pandoc(content_html)
    else:
        markdown = _html_to_markdown_fallback(content_html)

    return {
        "url": url,
        "title": title or "Untitled",
        "markdown": markdown,
        "raw_html": content_html,
    }


# ---------------------------------------------------------------------------
# Step 4 — Link rewriting
# ---------------------------------------------------------------------------

def _url_to_path(url: str) -> str:
    """Extract path component from URL for slug mapping."""
    return urlparse(url).path.rstrip("/")


def build_slug_map(pages: list[dict]) -> dict[str, str]:
    """Map url_path → page_slug for all discovered pages."""
    slug_map: dict[str, str] = {}
    for page in pages:
        url = page.get("url") or ""
        title = page.get("title") or "page"
        path = _url_to_path(url)
        slug = _slugify(title)
        if path:
            slug_map[path] = slug
    return slug_map


def rewrite_internal_links(markdown: str, source_domain: str, slug_map: dict[str, str]) -> str:
    """Replace internal links with [[MIGRATED:slug]] placeholders."""
    def replace_link(m: re.Match) -> str:
        link_text = m.group(1)
        href = m.group(2)
        parsed = urlparse(href)
        if parsed.netloc and parsed.netloc != source_domain:
            return m.group(0)  # External link — leave alone
        path = parsed.path.rstrip("/")
        slug = slug_map.get(path)
        if slug:
            return f"[{link_text}]([[MIGRATED:{slug}]])"
        return m.group(0)

    return re.sub(r"\[([^\]]*)\]\(([^)]+)\)", replace_link, markdown)


def resolve_migrated_links(markdown: str, old_url_to_page_id: dict[str, int]) -> str:
    """Replace [[MIGRATED:slug]] with resolved page IDs if available."""
    def replace_placeholder(m: re.Match) -> str:
        slug = m.group(1)
        for old_url, page_id in old_url_to_page_id.items():
            if _slugify(old_url.rstrip("/").rsplit("/", 1)[-1]) == slug:
                return f"/pages/{page_id}"
        return m.group(0)  # Could not resolve — leave placeholder

    return re.sub(r"\[\[MIGRATED:([^\]]+)\]\]", replace_placeholder, markdown)


# ---------------------------------------------------------------------------
# AccelDocs API client
# ---------------------------------------------------------------------------

class AccelDocsClient:
    def __init__(self, backend_url: str, token: str, org_id: int) -> None:
        self.backend = backend_url.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {token}",
            "X-Org-Id": str(org_id),
            "Content-Type": "application/json",
        }
        self.org_id = org_id

    def _post_json(self, path: str, body: dict) -> dict:
        url = f"{self.backend}{path}"
        resp = requests.post(url, json=body, headers=self.headers, timeout=30)
        if not resp.ok:
            raise RuntimeError(
                f"POST {path} failed [{resp.status_code}]: {resp.text[:400]}"
            )
        return resp.json()

    def create_section(
        self,
        name: str,
        parent_id: int | None = None,
        display_order: int = 0,
    ) -> dict:
        """POST /api/sections"""
        body: dict[str, Any] = {
            "name": name,
            "section_type": "section",
            "visibility": "public",
            "display_order": display_order,
        }
        if parent_id is not None:
            body["parent_id"] = parent_id
        return self._post_json("/api/sections", body)

    def import_pages(
        self,
        section_id: int,
        pages: list[dict],
    ) -> dict:
        """
        POST /api/drive/import/local — upload markdown files for a section.
        pages: list of {title, markdown} dicts.
        Returns response JSON.
        """
        url = f"{self.backend}/api/drive/import/local"
        # Build multipart form data — one file per page
        files_payload: list[tuple] = []
        relative_paths: list[str] = []

        for page in pages:
            filename = _slugify(page["title"]) + ".md"
            content = page.get("markdown") or ""
            files_payload.append(
                ("files", (filename, io.BytesIO(content.encode("utf-8")), "text/markdown"))
            )
            relative_paths.append(filename)

        # Remove Content-Type so requests sets multipart boundary automatically
        upload_headers = {
            "Authorization": self.headers["Authorization"],
            "X-Org-Id": self.headers["X-Org-Id"],
        }
        data = {
            "target_section_id": str(section_id),
            "mode": "files",
            "relative_paths_json": json.dumps(relative_paths),
        }
        resp = requests.post(
            url,
            data=data,
            files=files_payload,
            headers=upload_headers,
            timeout=120,
        )
        if not resp.ok:
            raise RuntimeError(
                f"POST /api/drive/import/local failed [{resp.status_code}]: {resp.text[:400]}"
            )
        return resp.json()

    def patch_page(self, page_id: int, body: dict) -> dict:
        """PATCH /api/pages/{page_id}"""
        url = f"{self.backend}/api/pages/{page_id}"
        patch_headers = {**self.headers}
        patch_headers.pop("Content-Type", None)
        patch_headers["Content-Type"] = "application/json"
        resp = requests.patch(url, json=body, headers=patch_headers, timeout=30)
        if not resp.ok:
            raise RuntimeError(
                f"PATCH /api/pages/{page_id} failed [{resp.status_code}]: {resp.text[:200]}"
            )
        return resp.json()


# ---------------------------------------------------------------------------
# Step 5 — Import into AccelDocs
# ---------------------------------------------------------------------------

def _flatten_pages(tree: list[dict]) -> list[dict]:
    """Flatten tree to get all page nodes (nodes with a URL)."""
    result: list[dict] = []
    for node in tree:
        if node.get("url"):
            result.append(node)
        result.extend(_flatten_pages(node["children"]))
    return result


def import_hierarchy(
    client: AccelDocsClient,
    tree: list[dict],
    product_id: int,
    page_data: dict[str, dict],  # url → {title, markdown}
    state: dict,
) -> dict[str, int]:
    """
    Import the full hierarchy into AccelDocs.
    Returns {old_url: new_page_id} mapping.
    """
    old_url_to_page_id: dict[str, int] = dict(state.get("page_id_map", {}))
    section_map: dict[str, int] = dict(state.get("section_map", {}))  # title_path → section_id

    def _import_node(node: dict, parent_section_id: int, path_prefix: str, order: int) -> None:
        title = node["title"]
        url = node.get("url")
        children = node.get("children", [])
        node_path = f"{path_prefix}/{_slugify(title)}"

        # Determine if this node is a section (has children or no direct URL but children)
        has_children = bool(children)

        if has_children:
            # Create or reuse section
            section_id = section_map.get(node_path)
            if not section_id:
                log.info("Creating section: %s (parent=%d)", title, parent_section_id)
                try:
                    result = client.create_section(
                        name=title,
                        parent_id=parent_section_id,
                        display_order=order,
                    )
                    section_id = result["id"]
                    section_map[node_path] = section_id
                    state["section_map"] = section_map
                    save_state(state)
                    log.info("Created section '%s' → id=%d", title, section_id)
                except Exception as exc:
                    log.error("Failed to create section '%s': %s", title, exc)
                    return

            # If the section heading also has a URL, import that page too
            if url and url not in old_url_to_page_id:
                _import_page(url, section_id, order)

            # Process children
            for child_order, child in enumerate(children):
                _import_node(child, section_id, node_path, child_order)

        elif url:
            # Leaf page — import under parent section
            if url not in old_url_to_page_id:
                _import_page(url, parent_section_id, order)

    def _import_page(url: str, section_id: int, order: int) -> None:
        if url in old_url_to_page_id:
            log.info("Skipping already-imported page: %s", url)
            return

        data = page_data.get(url)
        if not data:
            log.warning("No content for URL %s — skipping", url)
            return

        title = data["title"]
        markdown = data.get("markdown") or ""

        log.info("Importing page '%s' (%s) into section=%d", title, url, section_id)
        try:
            result = client.import_pages(
                section_id=section_id,
                pages=[{"title": title, "markdown": markdown}],
            )
            # The import/local endpoint returns aggregate counts, not individual page IDs.
            # We record the URL as done; page ID lookup would require an extra GET /api/pages call.
            uploaded = result.get("uploaded_files", 0)
            if uploaded > 0:
                # Store a sentinel so we know this URL was imported
                old_url_to_page_id[url] = -1  # -1 = imported, ID unknown
                state["page_id_map"] = old_url_to_page_id
                save_state(state)
                log.info("Imported page '%s'", title)
            else:
                log.warning(
                    "Import returned 0 uploaded files for '%s': %s",
                    title,
                    result.get("failed_file_errors", []),
                )
        except Exception as exc:
            log.error("Failed to import page '%s' (%s): %s", title, url, exc)

    for order, node in enumerate(tree):
        _import_node(node, product_id, "", order)

    return old_url_to_page_id


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Migrate a DeveloperHub documentation site into AccelDocs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--source", required=True, help="Public DeveloperHub docs URL to crawl")
    p.add_argument("--backend", default="http://localhost:8000", help="AccelDocs backend URL")
    p.add_argument("--token", help="JWT token (or set ACCELDOCS_TOKEN env var)")
    p.add_argument("--org-id", type=int, help="AccelDocs org ID (or set ACCELDOCS_ORG_ID env var)")
    p.add_argument(
        "--product-id",
        type=int,
        help="AccelDocs product/section ID to import under (or set ACCELDOCS_PRODUCT_ID env var)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Crawl only — print discovered hierarchy and page count without touching AccelDocs",
    )
    p.add_argument(
        "--resume",
        action="store_true",
        help="Load migration_state.json and skip already-imported pages/sections",
    )
    p.add_argument(
        "--delay",
        type=float,
        default=0.5,
        help="Seconds to wait between page fetches (default: 0.5)",
    )
    p.add_argument(
        "--max-pages",
        type=int,
        default=0,
        help="Maximum pages to fetch (0 = unlimited, for testing)",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()

    global _REQUEST_DELAY
    _REQUEST_DELAY = args.delay

    # Resolve credentials from args or env
    token = args.token or os.environ.get("ACCELDOCS_TOKEN", "")
    org_id_raw = args.org_id or os.environ.get("ACCELDOCS_ORG_ID", "")
    product_id_raw = args.product_id or os.environ.get("ACCELDOCS_PRODUCT_ID", "")

    if not args.dry_run:
        if not token:
            print("ERROR: --token or ACCELDOCS_TOKEN is required for live import", file=sys.stderr)
            sys.exit(1)
        if not org_id_raw:
            print("ERROR: --org-id or ACCELDOCS_ORG_ID is required for live import", file=sys.stderr)
            sys.exit(1)
        if not product_id_raw:
            print("ERROR: --product-id or ACCELDOCS_PRODUCT_ID is required for live import", file=sys.stderr)
            sys.exit(1)

    org_id = int(org_id_raw) if org_id_raw else 0
    product_id = int(product_id_raw) if product_id_raw else 0

    log.info("=== DeveloperHub → AccelDocs Migration ===")
    log.info("Source: %s", args.source)
    log.info("Backend: %s", args.backend)
    log.info("Dry run: %s", args.dry_run)

    # Load or initialize state
    state: dict = {}
    if args.resume or not args.dry_run:
        state = load_state()
        if state:
            log.info("Loaded existing state from %s", STATE_FILE)

    # -----------------------------------------------------------------------
    # Step 1: Discover structure
    # -----------------------------------------------------------------------
    tree = state.get("tree")
    fallback_links = state.get("fallback_links", [])

    if not tree:
        tree, fallback_links = discover_structure(args.source)
        state["tree"] = tree
        state["fallback_links"] = fallback_links
        state["source"] = args.source
        state["discovered_at"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
    else:
        log.info("Using cached tree from state (%d top-level nodes)", len(tree))

    all_page_urls = _collect_all_urls(tree)
    if not all_page_urls:
        log.warning("Tree contained no page URLs — falling back to all internal links")
        all_page_urls = fallback_links

    # Deduplicate while preserving order
    all_page_urls = list(dict.fromkeys(all_page_urls))

    # Apply --max-pages limit
    if args.max_pages > 0:
        log.info("Limiting to first %d pages (--max-pages)", args.max_pages)
        all_page_urls = all_page_urls[: args.max_pages]

    total_pages = len(all_page_urls)

    # -----------------------------------------------------------------------
    # DRY RUN: print hierarchy and exit
    # -----------------------------------------------------------------------
    if args.dry_run:
        print("\n" + "=" * 60)
        print(f"DRY RUN — Source: {args.source}")
        print("=" * 60)
        print(f"\nDiscovered {total_pages} page URLs")
        print(f"Top-level tree nodes: {len(tree)}\n")
        print("Navigation Hierarchy:")
        print("-" * 40)
        _print_tree(tree)
        print(f"\n{'=' * 60}")
        print(f"Total unique page URLs: {total_pages}")
        print(f"Fallback links found:   {len(fallback_links)}")
        print(f"\nTo run the full import:")
        print(
            f"  python scripts/migrate_developerhub.py \\\n"
            f"    --source {args.source} \\\n"
            f"    --backend {args.backend} \\\n"
            f"    --token <YOUR_TOKEN> \\\n"
            f"    --org-id <YOUR_ORG_ID> \\\n"
            f"    --product-id <YOUR_PRODUCT_ID>"
        )
        return

    # -----------------------------------------------------------------------
    # Step 2+3: Fetch and convert pages
    # -----------------------------------------------------------------------
    parsed_source = urlparse(args.source)
    source_domain = parsed_source.netloc

    page_data: dict[str, dict] = dict(state.get("page_data", {}))
    already_fetched = set(page_data.keys())

    urls_to_fetch = [u for u in all_page_urls if u not in already_fetched]
    log.info("Pages to fetch: %d (already cached: %d)", len(urls_to_fetch), len(already_fetched))

    for idx, url in enumerate(urls_to_fetch, 1):
        log.info("[%d/%d] Fetching: %s", idx, len(urls_to_fetch), url)
        result = fetch_and_convert_page(url)
        if result:
            page_data[url] = result
            state["page_data"] = page_data
            if idx % 10 == 0:
                save_state(state)

    save_state(state)
    log.info("Fetched %d pages total", len(page_data))

    # -----------------------------------------------------------------------
    # Step 4: Link rewriting
    # -----------------------------------------------------------------------
    slug_map = build_slug_map(list(page_data.values()))
    for url, data in page_data.items():
        if data.get("markdown"):
            data["markdown"] = rewrite_internal_links(
                data["markdown"], source_domain, slug_map
            )
    state["page_data"] = page_data
    save_state(state)
    log.info("Internal links rewritten with [[MIGRATED:slug]] placeholders")

    # -----------------------------------------------------------------------
    # Step 5: Import into AccelDocs
    # -----------------------------------------------------------------------
    client = AccelDocsClient(
        backend_url=args.backend,
        token=token,
        org_id=org_id,
    )

    log.info("Starting import into AccelDocs (product_id=%d)", product_id)
    old_url_to_page_id = import_hierarchy(
        client=client,
        tree=tree,
        product_id=product_id,
        page_data=page_data,
        state=state,
    )

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------
    imported = sum(1 for pid in old_url_to_page_id.values() if pid != 0)
    skipped = total_pages - imported

    print("\n" + "=" * 60)
    print("Migration Complete")
    print("=" * 60)
    print(f"  Total pages discovered: {total_pages}")
    print(f"  Pages imported:         {imported}")
    print(f"  Pages skipped/failed:   {skipped}")
    print(f"  State saved to:         {STATE_FILE}")
    print(f"  Full log in:            {LOG_FILE}")
    print("=" * 60)

    log.info(
        "Migration finished: %d imported, %d skipped of %d total",
        imported,
        skipped,
        total_pages,
    )

    state["completed_at"] = datetime.now(timezone.utc).isoformat()
    save_state(state)


if __name__ == "__main__":
    main()
