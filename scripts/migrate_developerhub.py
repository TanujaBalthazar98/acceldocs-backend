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
  1. Discovers the full navigation hierarchy from the sidebar (or sitemap fallback)
  2. Applies the Acceldata category map to produce a proper section hierarchy
  3. Fetches each page and converts HTML → Markdown (pandoc or html2text)
  4. Pre-processes DeveloperHub callout divs into MkDocs admonition syntax
  5. Handles DeveloperHub tab components (safe through pandoc)
  6. Creates sections in AccelDocs via POST /api/sections
  7. Imports pages via POST /api/pages/import (Markdown, no Drive required)
     Optionally creates Google Drive docs with --create-drive-docs
  8. Rewrites internal links using placeholder tokens
  9. Saves/resumes state from migration_state.json
"""

from __future__ import annotations

import argparse
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
# Acceldata category map
# Each entry: category name → list of URL slug prefixes that belong to it.
# Pages whose slug starts with any of these prefixes are grouped into that
# section during sitemap-based discovery.
# ---------------------------------------------------------------------------

ACCELDATA_CATEGORY_MAP: dict[str, list[str]] = {
    "Getting Started": [
        "introduction",
        "quick-start-guide",
    ],
    "Core Concepts": [
        "adoc-architecture",
        "core-concepts",
        "personas",
        "use-cases",
    ],
    "Installation": [
        "dataplane-installation",
        "data-plane-health-monitor",
        "global-storage",
    ],
    "Security": [
        "security-and-network-compliance",
        "accessibility-compliance",
        "authentication",
        "authorization",
        "single-sign-on",
        "api-keys",
        "secret-management",
        "cross---account-access-setup",
        "authenticating-using-external-oauth",
    ],
    "Users and Access": [
        "users-and-groups",
        "roles-and-permissions",
        "domains",
        "service-users",
        "access-visualizer",
    ],
    "Data Discovery": [
        "discover-assets",
        "profile-assets",
        "enrich-assets",
        "asset-details",
        "crawl-data-sources",
        "lineage",
        "discover",
        "observe-your-data-assets",
    ],
    "Data Quality": [
        "how-to-create-your-first-data-quality-policy",
        "apply-policies-and-monitor-reliability",
        "policies",
        "rules-and-rulesets",
        "data-quality-policy",
        "reconciliation-policy",
        "data-freshness",
        "data-anomaly-policy",
        "data-drift-policy",
        "schema-drift-policy",
        "data-policy-template",
        "manage-policies",
        "import-and-export-policies",
        "policy-groups",
        "data-quality-management",
        "data-reliability-settings",
        "reliability-jobs",
        "score-aggregation-methodology",
    ],
    "Alerts and Notifications": [
        "alerts",
        "managing-alerts",
        "notifications-and-notification-groups",
        "notification-integrations",
        "notification-templates",
        "working-with-notification-templates",
        "insights-actions",
        "reliability-reports",
    ],
    "Pipelines": [
        "pipelines",
        "control-pipeline",
        "working-with-pipeline",
        "understanding-the-pipeline-run-details",
        "use-case---set-alerts-for-pipeline-failure",
        "use-data-lineage-to-find-error",
        "airflow",
        "observing-airflow-dags",
        "deployment-on-on-premises-apache-airflow",
        "deployment-on-amazon-mwaa",
        "deployment-on-google-cloud-composer",
        "dbt-cloud",
        "azure-data-factory",
    ],
    "Integrations": [
        "mysql", "postgresql", "mariadb", "oracle", "azure-mssql", "db2",
        "sap-hana", "teradata", "amazon-aurora-mysql", "singlestore",
        "redshift", "bigquery", "azure-synapse-analytics",
        "snowflake", "s3", "azure-data-lake", "google-cloud-storage",
        "databricks", "kafka", "hive", "spark", "trino", "presto",
        "looker", "tableau", "power-bi", "powerbi", "elasticsearch", "opensearch",
        "mongodb", "cassandra", "clickhouse", "amazon-athena", "apache-hdfs",
        "fivetran", "snaplogic", "autosys", "google-cloud-pub-sub",
        "alation", "atlan", "collibra", "external-integrations",
        "aws-iam-roles",
    ],
    "Governance": [
        "governance",
        "data-governance",
        "business-glossary",
        "terms-and-definitions",
        "tags-and-labels",
        "ownership",
        "stewardship",
        "classification",
    ],
    "ADM (AI Data Management)": [
        "adm",
        "adm-reasoning",
        "glossary-adm",
        "understanding-agent",
        "understanding-mcp-server",
        "understanding-workflows",
        "writing-effective-prompts",
        "conversation",
        "end-to-end-flow",
        "query-mode-selection",
        "udt-cli-usage",
        "udt-helper-guide",
        "user-defined-templates",
        "persistence-configuration",
        "resource-recommendations-and-auto-sizing",
        "multi-user-collaboration",
        "good-bad-record-support-using-pushdown-engine",
        "context-switching-in-heterogeneous-pipelines",
        "using-executepolicy-operator",
        "using-operators-and-decorators",
        "pushdown-data-engine",
        "aws-iam-roles-for-databricks-pushdown-integration",
    ],
    "Troubleshooting & FAQs": [
        "faqs",
        "best-practices",
        "compatibility-guidelines",
        "troubleshooting-data-source-connection",
        "knowledge-base",
        "supported-file-types",
        "supported-data-sources",
        "quick-start",
    ],
    "CLI & SDKs": [
        "cli",
        "python",
        "sdk",
        "rest-api",
        "graphql",
    ],
    "Advanced": [
        "advanced",
        "api-docs",
        "api-reference",
        "asset-similarity",
        "business-notebooks",
        "export-and-import-manager",
        "manage",
        "search-acceldata-documentation",
    ],
}


def _categorize_url(slug: str) -> str | None:
    """Return the category name for a URL slug, or None if uncategorized."""
    slug_lower = slug.lower()
    for category, prefixes in ACCELDATA_CATEGORY_MAP.items():
        for prefix in prefixes:
            if slug_lower == prefix or slug_lower.startswith(prefix + "-") or slug_lower.startswith(prefix + "/"):
                return category
    return None


def _apply_category_hierarchy(flat_tree: list[dict]) -> list[dict]:
    """
    Reorganize a flat sitemap tree into a proper section hierarchy using the
    ACCELDATA_CATEGORY_MAP. Uncategorized pages are placed under "Other".

    Returns a new tree with section nodes containing page children.
    """
    # Group pages by category
    sections: dict[str, list[dict]] = {}
    for node in flat_tree:
        url = node.get("url") or ""
        slug = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
        category = _categorize_url(slug) or "Other"
        if category not in sections:
            sections[category] = []
        sections[category].append({**node, "depth": 1})

    # Build tree: ordered by category map first, then "Other" last
    ordered_cats = list(ACCELDATA_CATEGORY_MAP.keys()) + ["Other"]
    tree: list[dict] = []
    for cat in ordered_cats:
        pages = sections.get(cat)
        if not pages:
            continue
        tree.append({
            "title": cat,
            "url": None,
            "depth": 0,
            "children": pages,
        })

    log.info(
        "Category hierarchy built: %d sections, %d total pages",
        len(tree),
        sum(len(n["children"]) for n in tree),
    )
    return tree


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
        items = element.find_all(["li", "a"], recursive=False)

    for li in items:
        anchor = li.find("a", recursive=False) or li.find("a")
        href = anchor.get("href", "") if anchor else ""
        title = ""
        if anchor:
            title = anchor.get_text(separator=" ", strip=True)
        else:
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

        child_ul = li.find("ul", recursive=False)
        if child_ul:
            node["children"] = _walk_nav_tree(child_ul, base_url, depth + 1)
        else:
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
        url_str = f" → {node['url']}" if node["url"] else " (section)"
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
        if source_path_prefix and not u_path.startswith(source_path_prefix):
            continue
        urls.append(u)

    log.info("Sitemap: found %d matching URLs under %s", len(urls), source_path_prefix)
    return list(dict.fromkeys(urls))


def _sitemap_urls_to_tree(urls: list[str], apply_category_map: bool = True) -> list[dict]:
    """
    Convert a flat list of URLs into a tree.

    If apply_category_map is True (default), groups pages into sections using
    ACCELDATA_CATEGORY_MAP to produce a proper section hierarchy.

    Otherwise falls back to grouping by URL path segments.
    """
    if not urls:
        return []

    flat_nodes: list[dict] = []
    for url in urls:
        path = urlparse(url).path
        slug = path.rstrip("/").rsplit("/", 1)[-1]
        # Build a human title from the slug
        title = slug.replace("-", " ").replace("_", " ").title()
        # Fix common acronyms
        for acronym in ("S3", "Api", "Db2", "Mssql", "Aws", "Gcs", "Adl", "Sdk", "Sso", "Rbac", "Saml"):
            title = title.replace(acronym, acronym.upper())
        flat_nodes.append({"title": title, "url": url, "depth": 0, "children": []})

    if apply_category_map:
        return _apply_category_hierarchy(flat_nodes)

    # Generic path-based grouping (2-level)
    paths = [urlparse(u).path.strip("/").split("/") for u in urls]
    min_parts = min(len(p) for p in paths)
    common_depth = 0
    for i in range(min_parts):
        if len({p[i] for p in paths}) == 1:
            common_depth = i + 1
        else:
            break

    sections: dict[str, list[dict]] = {}
    top_pages: list[dict] = []
    for url, parts in zip(urls, paths):
        relative = parts[common_depth:]
        if not relative:
            continue
        title = relative[-1].replace("-", " ").replace("_", " ").title()
        if len(relative) == 1:
            top_pages.append({"title": title, "url": url, "depth": 0, "children": []})
        else:
            section_slug = relative[0]
            if section_slug not in sections:
                sections[section_slug] = []
            sections[section_slug].append({"title": title, "url": url, "depth": 1, "children": []})

    tree: list[dict] = list(top_pages)
    for section_slug, pages in sections.items():
        section_title = section_slug.replace("-", " ").replace("_", " ").title()
        tree.append({"title": section_title, "url": None, "depth": 0, "children": pages})
    return tree


def discover_structure(source_url: str) -> tuple[list[dict], list[str]]:
    """
    Load source URL, find sidebar, walk navigation tree.
    Falls back to sitemap.xml for JavaScript-rendered (SPA) DeveloperHub sites.
    When using the sitemap fallback, applies the Acceldata category map for
    a proper section hierarchy (instead of a flat 157-page list).
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

    fallback_links: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("/") or href.startswith(parsed.scheme + "://"):
            full = urljoin(source_url, href)
            if urlparse(full).netloc == parsed.netloc:
                fallback_links.append(full)
    fallback_links = list(dict.fromkeys(fallback_links))

    # Fallback: sitemap.xml — used when the site is a JavaScript SPA
    if not tree and not fallback_links:
        log.info("Page appears to be a JS-rendered SPA — trying sitemap.xml discovery")
        sitemap_urls = _fetch_sitemap_urls(base_url, source_path + "/")
        if not sitemap_urls:
            sitemap_urls = _fetch_sitemap_urls(base_url, source_path)
        if sitemap_urls:
            log.info("Using sitemap.xml: %d URLs found — applying category hierarchy", len(sitemap_urls))
            tree = _sitemap_urls_to_tree(sitemap_urls, apply_category_map=True)
            fallback_links = sitemap_urls
        else:
            log.warning("Sitemap yielded no matching URLs — discovery incomplete")
    elif not tree and fallback_links:
        log.warning("No structured sidebar found — using flat link list")
        for url in fallback_links:
            path_parts = urlparse(url).path.strip("/").split("/")
            title = path_parts[-1].replace("-", " ").replace("_", " ").title() if path_parts else "Page"
            tree.append({"title": title, "url": url, "depth": 0, "children": []})
    elif tree:
        # Sidebar-based tree found — check if it's suspiciously flat
        all_urls = _collect_all_urls(tree)
        has_children = any(n.get("children") for n in tree)
        if not has_children and len(all_urls) > 20:
            log.info(
                "Sidebar tree is flat (%d pages, no hierarchy) — applying category map",
                len(all_urls),
            )
            tree = _apply_category_hierarchy(tree)

    log.info(
        "Discovery: %d top-level tree nodes, %d fallback links",
        len(tree),
        len(fallback_links),
    )
    return tree, fallback_links


# ---------------------------------------------------------------------------
# Step 2 — DeveloperHub callout handling
# Pre-process callout divs BEFORE HTML→Markdown conversion so they become
# MkDocs admonition syntax rather than being lost in plain text.
# ---------------------------------------------------------------------------

_CALLOUT_TYPE_MAP = {
    "info":    "note",
    "note":    "note",
    "success": "tip",
    "tip":     "tip",
    "warning": "warning",
    "warn":    "warning",
    "danger":  "danger",
    "error":   "danger",
    "caution": "warning",
}

# DeveloperHub uses various class/attribute patterns for callouts.
# We detect them via CSS class names or data-type attributes.
_CALLOUT_CLASS_RE = re.compile(
    r"\b(callout|admonition|alert|notice|tip|note|info|warning|danger|success|caution)\b",
    re.I,
)

# Placeholder prefix — must survive pandoc (placed in a <pre> that we strip out)
_CALLOUT_PLACEHOLDER_PREFIX = "%%ADMONITION_BLOCK_"


def _detect_callout_type(elem: Tag) -> str | None:
    """Return the admonition type string for a callout element, or None."""
    classes = " ".join(elem.get("class", [])).lower()
    data_type = (elem.get("data-type") or elem.get("data-callout-type") or "").lower()
    role = (elem.get("role") or "").lower()

    # data attribute takes precedence
    for raw in [data_type, role]:
        if raw in _CALLOUT_TYPE_MAP:
            return _CALLOUT_TYPE_MAP[raw]

    # Check class names
    for cls in elem.get("class", []):
        cls_lower = cls.lower()
        if cls_lower in _CALLOUT_TYPE_MAP:
            return _CALLOUT_TYPE_MAP[cls_lower]

    if _CALLOUT_CLASS_RE.search(classes):
        # Try to get the specific type from the class list
        for raw, mapped in _CALLOUT_TYPE_MAP.items():
            if raw in classes:
                return mapped
        return "note"  # default

    return None


def _convert_callouts_to_placeholders(soup: BeautifulSoup) -> tuple[BeautifulSoup, dict[str, str]]:
    """
    Find DeveloperHub callout/admonition elements in the soup, convert them
    to MkDocs admonition Markdown, and replace each element with a unique
    placeholder string that will survive pandoc.

    Returns (modified_soup, {placeholder: admonition_markdown}).
    """
    placeholders: dict[str, str] = {}
    counter = 0

    # Look for block-level elements that are likely callouts
    for tag_name in ["aside", "div", "section", "blockquote"]:
        for elem in list(soup.find_all(tag_name)):
            atype = _detect_callout_type(elem)
            if not atype:
                continue

            # Extract title: look for a dedicated title child element
            title_elem = elem.find(
                lambda t: t.name in ("p", "span", "strong", "h1", "h2", "h3", "h4", "h5")
                and any(
                    cls in " ".join(t.get("class", [])).lower()
                    for cls in ("title", "header", "heading", "callout-title", "admonition-title")
                )
            )
            if title_elem:
                title_text = title_elem.get_text(strip=True)
                title_elem.decompose()
            else:
                title_text = atype.capitalize()

            body_text = elem.get_text(separator="\n", strip=True)
            if not body_text:
                continue

            # Build MkDocs admonition syntax
            indented = "\n".join(f"    {line}" for line in body_text.splitlines() if line.strip())
            admonition_md = f'!!! {atype} "{title_text}"\n{indented}\n'

            key = f"{_CALLOUT_PLACEHOLDER_PREFIX}{counter}%%"
            counter += 1
            placeholders[key] = admonition_md

            # Replace element with a plain text marker that pandoc will keep
            placeholder_tag = soup.new_tag("p")
            placeholder_tag.string = key
            elem.replace_with(placeholder_tag)

    return soup, placeholders


def _restore_callout_placeholders(markdown: str, placeholders: dict[str, str]) -> str:
    """Replace placeholder strings in the output Markdown with admonition blocks."""
    for key, value in placeholders.items():
        # pandoc might have wrapped the key in backticks or escaped underscores
        # Try exact match first, then a few common pandoc mutations
        variants = [
            key,
            key.replace("_", r"\_"),  # pandoc underscore escape
            f"`{key}`",               # pandoc inline code
        ]
        for variant in variants:
            if variant in markdown:
                markdown = markdown.replace(variant, "\n" + value + "\n")
                break
    return markdown


# ---------------------------------------------------------------------------
# Step 3 — DeveloperHub tab handling
# ---------------------------------------------------------------------------

# Use unique placeholders so pandoc doesn't wrap them in code fences.
_TAB_PLACEHOLDER_PREFIX = "%%TAB_BLOCK_"


def _convert_tabs_to_placeholders(soup: BeautifulSoup) -> tuple[BeautifulSoup, dict[str, str]]:
    """
    Convert DeveloperHub tab components to MkDocs-style tab syntax.

    Instead of using <pre> tags (which pandoc wraps in code fences), we:
    1. Build the MkDocs === tab Markdown string
    2. Store it in a dict keyed by a unique placeholder
    3. Replace the original element with a <p> containing just the placeholder key
    4. After pandoc runs, restore_tab_placeholders() swaps the key for real Markdown
    """
    placeholders: dict[str, str] = {}
    counter = 0

    # Handle <tab-group> web component
    for tab_group in list(soup.find_all("tab-group")):
        tabs = tab_group.find_all(["tab", "tab-panel"])
        md_lines: list[str] = []
        for tab in tabs:
            label = tab.get("label") or tab.get("title") or tab.name
            content = tab.get_text(separator="\n", strip=True)
            md_lines.append(f'=== "{label}"')
            for line in content.splitlines():
                md_lines.append(f"    {line}")
            md_lines.append("")
        if md_lines:
            key = f"{_TAB_PLACEHOLDER_PREFIX}{counter}%%"
            counter += 1
            placeholders[key] = "\n".join(md_lines)
            ph = soup.new_tag("p")
            ph.string = key
            tab_group.replace_with(ph)

    # Handle div.tabs-wrapper / div.tab-group
    for wrapper in soup.find_all("div", class_=re.compile(r"tabs?[-_]?(wrapper|group|container)", re.I)):
        panels = (
            wrapper.find_all(["div", "section"], attrs={"data-tab": True})
            or wrapper.find_all(["div", "section"], role="tabpanel")
            or wrapper.find_all(
                ["div", "section"],
                class_=re.compile(r"tab[-_]?(panel|content|pane)", re.I),
            )
        )
        tab_labels = [
            btn.get_text(strip=True)
            for btn in wrapper.find_all(
                ["button", "a", "li"],
                class_=re.compile(r"tab[-_]?(item|label|button|link)?", re.I),
            )
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
            key = f"{_TAB_PLACEHOLDER_PREFIX}{counter}%%"
            counter += 1
            placeholders[key] = "\n".join(md_lines)
            ph = soup.new_tag("p")
            ph.string = key
            wrapper.replace_with(ph)

    return soup, placeholders


def _restore_tab_placeholders(markdown: str, placeholders: dict[str, str]) -> str:
    """Replace tab placeholder strings in Markdown with the actual MkDocs tab syntax."""
    for key, value in placeholders.items():
        variants = [key, key.replace("_", r"\_"), f"`{key}`"]
        for variant in variants:
            if variant in markdown:
                markdown = markdown.replace(variant, "\n" + value + "\n")
                break
    return markdown


# ---------------------------------------------------------------------------
# Step 4 — Page fetch + HTML → Markdown conversion
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
    """Basic HTML → Markdown using html2text or BeautifulSoup text extraction."""
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
    Fetch a page, extract main content, handle callouts + tabs, convert to Markdown.
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
        if " | " in title:
            title = title.split(" | ")[0].strip()
        elif " - " in title:
            title = title.split(" - ")[0].strip()

    # 1. Pre-process: callouts → placeholders (BEFORE content extraction so we
    #    capture callouts that might be outside the main content element)
    soup, callout_placeholders = _convert_callouts_to_placeholders(soup)

    # 2. Pre-process: tab groups → placeholders
    soup, tab_placeholders = _convert_tabs_to_placeholders(soup)

    # 3. Extract main content area
    content_elem = None
    for selector in _CONTENT_SELECTORS:
        elem = selector(soup)
        if elem and elem.get_text(strip=True):
            content_elem = elem
            break

    if not content_elem:
        log.warning("No main content found on %s — using body", url)
        content_elem = soup.find("body") or soup

    # Resolve relative images to absolute URLs
    content_elem = _resolve_images(content_elem, url)

    # Override title with in-page h1 if present
    h1 = content_elem.find("h1")
    if h1:
        h1_text = h1.get_text(strip=True)
        if h1_text:
            title = h1_text

    content_html = str(content_elem)

    # 4. HTML → Markdown
    if _check_pandoc():
        markdown = _html_to_markdown_pandoc(content_html)
    else:
        markdown = _html_to_markdown_fallback(content_html)

    # 5. Restore placeholders with their Markdown equivalents
    markdown = _restore_callout_placeholders(markdown, callout_placeholders)
    markdown = _restore_tab_placeholders(markdown, tab_placeholders)

    return {
        "url": url,
        "title": title or "Untitled",
        "markdown": markdown,
        "raw_html": content_html,
    }


# ---------------------------------------------------------------------------
# Step 5 — Link rewriting
# ---------------------------------------------------------------------------

def _url_to_path(url: str) -> str:
    return urlparse(url).path.rstrip("/")


def build_slug_map(pages: list[dict]) -> dict[str, str]:
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
    def replace_link(m: re.Match) -> str:
        link_text = m.group(1)
        href = m.group(2)
        parsed = urlparse(href)
        if parsed.netloc and parsed.netloc != source_domain:
            return m.group(0)
        path = parsed.path.rstrip("/")
        slug = slug_map.get(path)
        if slug:
            return f"[{link_text}]([[MIGRATED:{slug}]])"
        return m.group(0)

    return re.sub(r"\[([^\]]*)\]\(([^)]+)\)", replace_link, markdown)


def resolve_migrated_links(markdown: str, old_url_to_page_id: dict[str, int]) -> str:
    def replace_placeholder(m: re.Match) -> str:
        slug = m.group(1)
        for old_url, page_id in old_url_to_page_id.items():
            if _slugify(old_url.rstrip("/").rsplit("/", 1)[-1]) == slug:
                return f"/pages/{page_id}"
        return m.group(0)

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
        body: dict[str, Any] = {
            "name": name,
            "section_type": "section",
            "visibility": "public",
            "display_order": display_order,
        }
        if parent_id is not None:
            body["parent_id"] = parent_id
        return self._post_json("/api/sections", body)

    def import_page(
        self,
        title: str,
        markdown_content: str,
        section_id: int,
        display_order: int = 0,
        create_drive_doc: bool = False,
    ) -> dict:
        """
        POST /api/pages/import — create a page from Markdown.

        If create_drive_doc is True, passes the flag to the backend so it also
        creates a Google Doc in the section's Drive folder.
        """
        body: dict[str, Any] = {
            "title": title,
            "markdown_content": markdown_content,
            "section_id": section_id,
            "display_order": display_order,
        }
        if create_drive_doc:
            body["create_drive_doc"] = True
        return self._post_json("/api/pages/import", body)

    def patch_page(self, page_id: int, body: dict) -> dict:
        url = f"{self.backend}/api/pages/{page_id}"
        patch_headers = {**self.headers}
        patch_headers["Content-Type"] = "application/json"
        resp = requests.patch(url, json=body, headers=patch_headers, timeout=30)
        if not resp.ok:
            raise RuntimeError(
                f"PATCH /api/pages/{page_id} failed [{resp.status_code}]: {resp.text[:200]}"
            )
        return resp.json()


# ---------------------------------------------------------------------------
# Step 6 — Import into AccelDocs
# ---------------------------------------------------------------------------

def _flatten_pages(tree: list[dict]) -> list[dict]:
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
    page_data: dict[str, dict],
    state: dict,
    create_drive_docs: bool = False,
) -> dict[str, int]:
    """
    Import the full hierarchy into AccelDocs.
    Returns {old_url: new_page_id} mapping.
    """
    old_url_to_page_id: dict[str, int] = dict(state.get("page_id_map", {}))
    section_map: dict[str, int] = dict(state.get("section_map", {}))

    def _import_node(node: dict, parent_section_id: int, path_prefix: str, order: int) -> None:
        title = node["title"]
        url = node.get("url")
        children = node.get("children", [])
        node_path = f"{path_prefix}/{_slugify(title)}"

        has_children = bool(children)

        if has_children:
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

            if url and url not in old_url_to_page_id:
                _import_page(url, section_id, order)

            for child_order, child in enumerate(children):
                _import_node(child, section_id, node_path, child_order)

        elif url:
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
            result = client.import_page(
                title=title,
                markdown_content=markdown,
                section_id=section_id,
                display_order=order,
                create_drive_doc=create_drive_docs,
            )
            page_id = result.get("id", -1)
            old_url_to_page_id[url] = page_id
            state["page_id_map"] = old_url_to_page_id
            save_state(state)
            log.info("Imported page '%s' → id=%d", title, page_id)
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
    p.add_argument(
        "--create-drive-docs",
        action="store_true",
        help=(
            "Also create a Google Doc in each section's Drive folder for every imported page. "
            "Requires Google Drive to be connected in AccelDocs. Slower but gives Drive editability."
        ),
    )
    p.add_argument(
        "--no-category-map",
        action="store_true",
        help="Disable the Acceldata category map and use generic path-based grouping instead",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()

    global _REQUEST_DELAY
    _REQUEST_DELAY = args.delay

    token = args.token or os.environ.get("ACCELDOCS_TOKEN", "")
    org_id_raw = args.org_id or os.environ.get("ACCELDOCS_ORG_ID", "")
    product_id_raw = args.product_id or os.environ.get("ACCELDOCS_PRODUCT_ID", "")

    if not args.dry_run:
        if not token:
            log.error("--token / ACCELDOCS_TOKEN is required for a live import")
            sys.exit(1)
        if not org_id_raw:
            log.error("--org-id / ACCELDOCS_ORG_ID is required")
            sys.exit(1)
        if not product_id_raw:
            log.error("--product-id / ACCELDOCS_PRODUCT_ID is required")
            sys.exit(1)

    org_id = int(org_id_raw) if org_id_raw else 0
    product_id = int(product_id_raw) if product_id_raw else 0

    log.info("=== DeveloperHub → AccelDocs Migration ===")
    log.info("Source: %s", args.source)
    log.info("Backend: %s", args.backend)
    log.info("Dry run: %s", args.dry_run)
    log.info("Create Drive docs: %s", getattr(args, "create_drive_docs", False))

    # Load or start fresh state
    state: dict = {}
    if args.resume:
        state = load_state()
        if state:
            log.info("Loaded existing state from %s", STATE_FILE)

    # -----------------------------------------------------------------------
    # Step 1: Discover structure
    # -----------------------------------------------------------------------
    if state.get("tree"):
        tree: list[dict] = state["tree"]
        fallback_links: list[str] = state.get("fallback_links", [])
        log.info("Using cached tree from state (%d top-level nodes)", len(tree))
        # Re-apply category map if the cached tree is flat (from a previous dry run)
        all_urls_cached = _collect_all_urls(tree)
        has_children_cached = any(n.get("children") for n in tree)
        if not has_children_cached and len(all_urls_cached) > 10 and not args.no_category_map:
            log.info("Cached tree is flat — applying category hierarchy now")
            tree = _apply_category_hierarchy(tree)
            state["tree"] = tree
            save_state(state)
    else:
        tree, fallback_links = discover_structure(args.source)
        # Apply category map override for the sitemap fallback if needed
        if args.no_category_map:
            # Re-discover without category map
            all_flat = _collect_all_urls(tree)
            if all_flat:
                tree = _sitemap_urls_to_tree(all_flat, apply_category_map=False)
        state["tree"] = tree
        state["fallback_links"] = fallback_links
        state["source"] = args.source
        state["discovered_at"] = datetime.now(timezone.utc).isoformat()
        save_state(state)

    all_page_urls = list(dict.fromkeys(_collect_all_urls(tree)))
    total_pages = len(all_page_urls)

    if args.max_pages and args.max_pages > 0:
        all_page_urls = all_page_urls[:args.max_pages]
        log.info("Limiting to first %d pages (--max-pages)", args.max_pages)

    # -----------------------------------------------------------------------
    # Dry run: print and exit
    # -----------------------------------------------------------------------
    if args.dry_run:
        print(f"\n{'=' * 60}")
        print(f"DRY RUN — Source: {args.source}")
        print(f"{'=' * 60}\n")
        print(f"Discovered {total_pages} page URLs")
        top_sections = [n for n in tree if n.get("children")]
        print(f"Sections: {len(top_sections)}")
        print("\nHierarchy:")
        print("-" * 40)
        _print_tree(tree)
        print(f"\n{'=' * 60}")
        print(f"Total unique page URLs: {total_pages}")
        print(f"\nTo run the full import:")
        print(
            f"  python scripts/migrate_developerhub.py \\\n"
            f"    --source {args.source} \\\n"
            f"    --backend {args.backend} \\\n"
            f"    --token <YOUR_TOKEN> \\\n"
            f"    --org-id <YOUR_ORG_ID> \\\n"
            f"    --product-id <YOUR_PRODUCT_ID>"
        )
        print(f"\nTo also create Google Drive docs (editability):")
        print(f"    add --create-drive-docs")
        return

    # -----------------------------------------------------------------------
    # Step 2+3: Fetch and convert pages
    # -----------------------------------------------------------------------
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
    parsed_source = urlparse(args.source)
    source_domain = parsed_source.netloc

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
    client = AccelDocsClient(backend_url=args.backend, token=token, org_id=org_id)

    log.info(
        "Starting import into AccelDocs (product_id=%d, create_drive_docs=%s)",
        product_id,
        getattr(args, "create_drive_docs", False),
    )
    old_url_to_page_id = import_hierarchy(
        client=client,
        tree=tree,
        product_id=product_id,
        page_data=page_data,
        state=state,
        create_drive_docs=getattr(args, "create_drive_docs", False),
    )

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------
    imported = sum(1 for pid in old_url_to_page_id.values() if pid and pid > 0)
    skipped = total_pages - imported

    print("\n" + "=" * 60)
    print("Migration Complete")
    print("=" * 60)
    print(f"  Total pages discovered: {total_pages}")
    print(f"  Pages imported:         {imported}")
    print(f"  Pages skipped/failed:   {skipped}")
    print(f"  Drive docs created:     {getattr(args, 'create_drive_docs', False)}")
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
