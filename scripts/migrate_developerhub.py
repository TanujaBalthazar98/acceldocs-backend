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
# Acceldata multi-product structure
# docs.acceldata.io has a product switcher dropdown with three products:
#   ADOC, Pulse, ODP
# Each product has its own set of top-level tabs and version switcher.
# --all-products iterates every product; --product picks one by slug.
# --all-versions migrates every known version; default = latest only.
# ---------------------------------------------------------------------------

from dataclasses import dataclass, field as dc_field
from typing import Optional


@dataclass
class ProductTab:
    """One top-level tab within a product (e.g. "User Guide", "API Reference")."""
    name: str
    url_path: str  # path segment, e.g. "/documentation", "/api"
    sitemap_prefix: str = ""  # for sitemap fallback filtering


@dataclass
class ProductVersion:
    """A specific version of a product (e.g. Pulse 4.1.x, ODP 3.3.6.3-1)."""
    label: str  # display name shown in the version switcher
    url_path: str = ""  # path segment if version changes the URL (empty = default)
    is_latest: bool = False


@dataclass
class ProductConfig:
    """Full configuration for one product in the Acceldata docs site."""
    name: str
    slug: str  # used in CLI --product filter
    base_url: str  # e.g. "https://docs.acceldata.io"
    tabs: list[ProductTab] = dc_field(default_factory=list)
    versions: list[ProductVersion] = dc_field(default_factory=list)
    # CSS selector / strategy hints for Playwright product switching
    dropdown_label: str = ""  # text shown in the product dropdown


# --- Known products (fall back when Playwright can't auto-discover) ---

ACCELDATA_PRODUCTS: list[ProductConfig] = [
    ProductConfig(
        name="ADOC",
        slug="adoc",
        base_url="https://docs.acceldata.io",
        dropdown_label="ADOC",
        tabs=[
            ProductTab(
                name="Documentation",
                url_path="/documentation",
                sitemap_prefix="/documentation/",
            ),
            ProductTab(
                name="API Reference",
                url_path="/api",
                sitemap_prefix="/api/",
            ),
            ProductTab(
                name="Release Notes",
                url_path="/release",
                sitemap_prefix="/release/",
            ),
        ],
        versions=[
            ProductVersion(label="latest", is_latest=True),
        ],
    ),
    ProductConfig(
        name="Pulse",
        slug="pulse",
        base_url="https://docs.acceldata.io",
        dropdown_label="Pulse",
        tabs=[
            ProductTab(
                name="User Guide",
                url_path="/pulse/user-guide",
                sitemap_prefix="/pulse/user-guide/",
            ),
            ProductTab(
                name="Installation Guide",
                url_path="/pulse/installation-guide",
                sitemap_prefix="/pulse/installation-guide/",
            ),
            ProductTab(
                name="Release Notes",
                url_path="/pulse/release-notes",
                sitemap_prefix="/pulse/release-notes/",
            ),
            ProductTab(
                name="Interface Reference Guide",
                url_path="/pulse/interface-reference-guide",
                sitemap_prefix="/pulse/interface-reference-guide/",
            ),
            ProductTab(
                name="Compatibility Matrix",
                url_path="/pulse/compatibility-matrix",
                sitemap_prefix="/pulse/compatibility-matrix/",
            ),
            ProductTab(
                name="FAQs",
                url_path="/pulse/faqs",
                sitemap_prefix="/pulse/faqs/",
            ),
        ],
        versions=[
            ProductVersion(label="Pulse 4.1.x", is_latest=True),
            # Versions are selected via the DeveloperHub dropdown (JS), not URL path.
            # Add older versions here if Playwright-based version switching is added.
        ],
    ),
    ProductConfig(
        name="ODP",
        slug="odp",
        base_url="https://docs.acceldata.io",
        dropdown_label="ODP",
        tabs=[
            ProductTab(
                name="Documentation",
                url_path="/odp/documentation",
                sitemap_prefix="/odp/documentation/",
            ),
            ProductTab(
                name="Support Matrix",
                url_path="/odp/support-matrix",
                sitemap_prefix="/odp/support-matrix/",
            ),
        ],
        versions=[
            ProductVersion(label="ODP 3.3.6.3-1", is_latest=True),
            # Versions are selected via the DeveloperHub dropdown (JS), not URL path.
        ],
    ),
]

# Legacy alias — kept for backward compatibility with --all-tabs
ACCELDATA_TABS: list[dict[str, str]] = [
    {"name": t.name, "url": ACCELDATA_PRODUCTS[0].base_url + t.url_path, "sitemap_prefix": t.sitemap_prefix}
    for t in ACCELDATA_PRODUCTS[0].tabs  # ADOC tabs only (legacy)
]


def get_product_by_slug(slug: str) -> ProductConfig | None:
    """Look up a product config by its slug (case-insensitive)."""
    slug_lower = slug.lower()
    for p in ACCELDATA_PRODUCTS:
        if p.slug == slug_lower or p.name.lower() == slug_lower:
            return p
    return None


def get_products_to_migrate(
    args_product: str | None,
    args_all_products: bool,
) -> list[ProductConfig]:
    """Determine which products to migrate based on CLI flags."""
    if args_all_products:
        return list(ACCELDATA_PRODUCTS)
    if args_product:
        prod = get_product_by_slug(args_product)
        if not prod:
            available = ", ".join(p.slug for p in ACCELDATA_PRODUCTS)
            log.error("Unknown product '%s'. Available: %s", args_product, available)
            sys.exit(1)
        return [prod]
    # Default: ADOC only (backward compatible)
    return [ACCELDATA_PRODUCTS[0]]


def get_versions_to_migrate(
    product: ProductConfig,
    all_versions: bool = False,
) -> list[ProductVersion]:
    """Return the versions to migrate for a product."""
    if not product.versions:
        return [ProductVersion(label="latest", is_latest=True)]
    if all_versions:
        return list(product.versions)
    # Default: latest only
    latest = [v for v in product.versions if v.is_latest]
    return latest if latest else [product.versions[0]]


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
    lambda soup: soup.find("div", class_="angular-tree-component"),
    lambda soup: soup.find("div", class_="sidebar"),
    lambda soup: soup.find(lambda t: t.name == "div" and "sidebar" in " ".join(t.get("class", []))),
]

_SELECTOR_NAMES = [
    "nav[aria-label]",
    ".sidebar-nav",
    "[class*='sidebar'] nav",
    "[class*='navigation']",
    "nav (fallback)",
    "div.angular-tree-component",
    "div.sidebar",
    "[class*='sidebar'] div",
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


def _walk_angular_tree(element: Tag, base_url: str, depth: int = 0) -> list[dict]:
    """Walk an Angular tree component (DeveloperHub div.tree-node structure).

    The Angular tree renders ALL tree-node divs flat in the DOM, using
    tree-node-level-X classes to indicate nesting depth. There are no nested
    <ul>/<li> structures. Nodes are identified as:

      - Section: has "category-container" in class — acts as a section heading,
        may or may not have an <a> link (some sections are also pages).
      - Page: has a direct <a href> link.

    Children are siblings in the flat DOM, identified by level class.
    Collapsed sections show no children in the DOM.

    Structure:
      <div class="angular-tree-component">
        <tree-node-collection>
          <div>  <- plain container
            <div class="... tree-node-level-1 category-container">Section Name</div>
            <div class="... tree-node-level-2 tree-node-leaf"><a href="/path">Page</a></div>
          </div>
        </tree-node-collection>
      </div>
    """
    tree_node_collection = element.find("tree-node-collection")
    container = tree_node_collection.find("div") if tree_node_collection else element

    all_tree_divs = container.find_all(
        "div",
        class_=lambda x: (
            x and "tree-node-level-" in (" ".join(x) if isinstance(x, list) else (x or ""))
        ),
    )

    if not all_tree_divs:
        return []

    parsed_nodes: list[dict] = []
    for div in all_tree_divs:
        classes = div.get("class", [])
        classes_str = " ".join(classes) if isinstance(classes, list) else classes or ""

        level_match = re.search(r"tree-node-level-(\d+)", classes_str)
        node_level = int(level_match.group(1)) if level_match else 1

        is_category = "category-container" in classes_str
        anchor = div.find("a", href=True)
        href = anchor.get("href", "") if anchor else ""

        url: str | None = None
        if href and not href.startswith("#") and not href.startswith("javascript"):
            url = urljoin(base_url, href)

        title: str = "Untitled"
        node_func = div.find("span", class_="node-function")
        if node_func:
            node_text = node_func.find("div", class_="node-text")
            if node_text:
                category_span = node_text.find(
                    "span",
                    class_=lambda x: x and "category" in (" ".join(x) if isinstance(x, list) else x),
                )
                if category_span:
                    title = category_span.get_text(separator=" ", strip=True)
                else:
                    page_span = node_text.find("span", class_=lambda x: x and "node" in (" ".join(x) if isinstance(x, list) else x))
                    if page_span:
                        title = page_span.get_text(separator=" ", strip=True)

        if title == "Untitled" and anchor:
            title = anchor.get_text(separator=" ", strip=True)

        if title == "Untitled":
            title_raw = div.get_text(separator=" ", strip=True)
            title = title_raw.split("\n")[0].strip() or "Untitled"

        parsed_nodes.append({
            "title": title,
            "url": url,
            "depth": node_level - 1,
            "children": [],
            "_level": node_level,
            "_is_category": is_category,
        })

    # Use the generic depth-based tree builder to handle arbitrary nesting
    # (levels 1–N). The parsed_nodes already have correct 'depth' set to
    # node_level - 1, so level-1 → depth 0, level-2 → depth 1, etc.
    tree = _depth_list_to_tree(parsed_nodes)

    # Recursively clean up private keys from all nodes
    def _cleanup(nodes: list[dict]) -> None:
        for n in nodes:
            n.pop("_level", None)
            n.pop("_is_category", None)
            _cleanup(n.get("children", []))

    _cleanup(tree)

    return tree


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

    Tries multiple sitemap locations:
    1. Product-specific sitemap (e.g. /pulse/sitemap.xml) — derived from source_path_prefix
    2. Root sitemap at /sitemap.xml
    """
    parsed = urlparse(base_url)

    # Build list of sitemap URLs to try
    sitemap_candidates: list[str] = []

    # Try product-specific sitemap first (e.g. /pulse/sitemap.xml for /pulse/user-guide/)
    prefix_parts = source_path_prefix.strip("/").split("/")
    if prefix_parts and prefix_parts[0]:
        product_sitemap = urlunparse((
            parsed.scheme, parsed.netloc,
            f"/{prefix_parts[0]}/sitemap.xml", "", "", "",
        ))
        sitemap_candidates.append(product_sitemap)

    # Always try root sitemap
    root_sitemap = urlunparse((parsed.scheme, parsed.netloc, "/sitemap.xml", "", "", ""))
    if root_sitemap not in sitemap_candidates:
        sitemap_candidates.append(root_sitemap)

    for sitemap_url in sitemap_candidates:
        log.info("Checking sitemap: %s", sitemap_url)
        html = fetch_html(sitemap_url)
        if not html:
            continue

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

        if urls:
            log.info("Sitemap: found %d matching URLs under %s", len(urls), source_path_prefix)
            return list(dict.fromkeys(urls))

    log.info("Sitemap: found 0 matching URLs under %s", source_path_prefix)
    return []


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


# ---------------------------------------------------------------------------
# Playwright-based discovery — for JavaScript SPAs
# ---------------------------------------------------------------------------

_PLAYWRIGHT_AVAILABLE: bool | None = None


def _pw_discover_versions(page_obj: Any) -> list[dict]:
    """Use Playwright to discover available versions from the version picker dropdown.

    Uses force-click since the dropdown toggle may appear hidden to Playwright.
    Returns list of dicts: [{"label": "Pulse 4.1.x", "is_latest": True}, ...]
    The first item in the dropdown is assumed to be the latest.
    """
    locator = page_obj.locator(".version-picker-container").first
    try:
        if not locator.count():
            log.info("No version picker found on page")
            return []
    except Exception:
        return []

    try:
        locator.click(force=True)
        page_obj.wait_for_timeout(1500)
    except Exception as exc:
        log.warning("Could not click version picker: %s", exc)
        return []

    items = page_obj.locator(".version-picker-container .dropdown-item").all()
    versions: list[dict] = []
    for idx, item in enumerate(items):
        txt = item.inner_text().strip()
        if txt:
            versions.append({"label": txt, "is_latest": idx == 0})

    # Close the dropdown
    try:
        page_obj.locator("body").click(position={"x": 10, "y": 10}, force=True)
        page_obj.wait_for_timeout(300)
    except Exception:
        pass

    log.info("Discovered %d versions: %s", len(versions), ", ".join(v["label"] for v in versions))
    return versions


def _pw_switch_version(page_obj: Any, version_label: str) -> bool:
    """Click the version picker and select the given version. Returns True on success.

    Uses normalized text matching: strips whitespace and compares case-insensitively,
    falling back to substring containment if exact match fails.
    """
    locator = page_obj.locator(".version-picker-container").first
    try:
        locator.click(force=True)
        page_obj.wait_for_timeout(1500)
    except Exception:
        return False

    items = page_obj.locator(".version-picker-container .dropdown-item").all()
    target = version_label.strip().lower()

    # Pass 1: exact case-insensitive match
    for item in items:
        txt = item.inner_text().strip()
        if txt.lower() == target:
            try:
                item.click(force=True)
                page_obj.wait_for_timeout(4000)
                log.info("Switched to version: %s (now at %s)", txt, page_obj.url)
                return True
            except Exception as exc:
                log.warning("Failed to click version '%s': %s", txt, exc)
                return False

    # Pass 2: substring containment (handles "Pulse 4.1.x" matching "4.1.x")
    for item in items:
        txt = item.inner_text().strip()
        if target in txt.lower() or txt.lower() in target:
            try:
                item.click(force=True)
                page_obj.wait_for_timeout(4000)
                log.info("Switched to version: %s (fuzzy match for '%s', now at %s)", txt, version_label, page_obj.url)
                return True
            except Exception as exc:
                log.warning("Failed to click version '%s': %s", txt, exc)
                return False

    available = [item.inner_text().strip() for item in items]
    log.warning("Version '%s' not found in dropdown. Available: %s", version_label, available)
    return False


def _pw_discover_versions(page_obj: Any) -> list[dict]:
    """Discover all available versions from the version picker dropdown.

    Opens the dropdown, reads version names, returns list. Does NOT close the dropdown.
    Returns list of dicts: [{"name": "Pulse 4.1.x", "href": ""}, ...]
    """
    try:
        page_obj.click("app-version-picker .top-picker", timeout=5000)
        page_obj.wait_for_timeout(1500)
    except Exception:
        return []

    versions: list[dict] = []
    try:
        html = page_obj.content()
        soup = BeautifulSoup(html, "html.parser")
        picker = soup.find("app-version-picker")
        if picker:
            menu = picker.find("ul", class_="dropdown-menu")
            if menu:
                for li in menu.find_all("li", role="menuitem"):
                    text = li.get_text(separator=" ", strip=True)
                    if text:
                        versions.append({"name": text})
    except Exception:
        pass

    if versions:
        log.info("Discovered %d versions: %s", len(versions), ", ".join(v["name"] for v in versions))
    return versions


def _pw_close_dropdown(page_obj: Any) -> None:
    """Close any open dropdown by pressing Escape."""
    try:
        page_obj.keyboard.press("Escape")
        page_obj.wait_for_timeout(300)
    except Exception:
        pass


def _pw_discover_tabs(page_obj: Any) -> list[dict]:
    """Discover tabs from the current page.

    DeveloperHub renders tabs as visible <a> links in a top bar (y ~40-100px),
    NOT inside a dropdown. The hidden `.section-picker-container` is not used
    when tabs are shown as top-bar links.

    Returns list of dicts: [{"name": "User Guide", "url_path": "/pulse/user-guide"}, ...]
    """
    tabs_data = page_obj.evaluate("""() => {
        const results = [];
        const allLinks = document.querySelectorAll('a');
        for (const a of allLinks) {
            const rect = a.getBoundingClientRect();
            // Tabs are in the top bar area (y between 30 and 100) and are visible
            if (rect.top > 30 && rect.top < 100 && rect.width > 20 && rect.height > 0) {
                const text = a.textContent.trim();
                const href = a.getAttribute('href') || '';
                // Filter: must have a path href (not # or empty) and non-empty text
                if (text && href.startsWith('/')) {
                    results.push({name: text, url_path: href});
                }
            }
        }
        return results;
    }""")

    if tabs_data:
        log.info(
            "Discovered %d tabs: %s",
            len(tabs_data),
            ", ".join(t["name"] for t in tabs_data),
        )
    return tabs_data


def _check_playwright() -> bool:
    global _PLAYWRIGHT_AVAILABLE
    if _PLAYWRIGHT_AVAILABLE is None:
        try:
            import playwright  # noqa: F401
            _PLAYWRIGHT_AVAILABLE = True
            log.info("playwright is available — can use for JS-rendered sidebar discovery")
        except ImportError:
            _PLAYWRIGHT_AVAILABLE = False
            log.info("playwright not installed — will use static HTML / sitemap fallback")
    return _PLAYWRIGHT_AVAILABLE


def _pw_extract_nav_tree(page_obj: Any, base_url: str, max_depth: int = 0) -> list[dict]:
    """
    Extract the full navigation tree from a Playwright page object.

    DeveloperHub uses an Angular tree with an accordion pattern — only one
    section can be expanded at a time. Child nodes are loaded into the DOM when
    their parent is expanded.

    Strategy:
    1. Wait for page to settle.
    2. Iterate through all category sections: click to expand, capture level-2
       children from the DOM (accordion collapses previous sections but their
       children remain in the DOM).
    3. For each level-2 child that has sub-pages (tree-node-collapsed), navigate
       to that page and extract level-3 sub-pages from the sidebar.
    4. Return a 3-level tree: sections > pages/subsections > sub-pages.
    """
    try:
        page_obj.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        page_obj.wait_for_load_state("domcontentloaded", timeout=15000)
        page_obj.wait_for_timeout(1500)

    html = page_obj.content()
    soup = BeautifulSoup(html, "html.parser")
    nav = _find_nav(soup)

    if nav:
        nav_classes = " ".join(nav.get("class", [])) if isinstance(nav.get("class"), list) else nav.get("class", "") or ""

        if "angular-tree-component" in nav_classes:
            tree = _pw_extract_angular_deep(page_obj, nav, base_url, max_depth=max_depth)
            if tree:
                all_urls = _collect_all_urls(tree)
                has_depth = any(n.get("children") for n in tree)
                log.info(
                    "Playwright Angular deep: %d sections, %d total URLs, deep=%s",
                    len(tree),
                    len(all_urls),
                    has_depth,
                )
                return tree

        tree = _walk_nav_tree(nav, base_url)
        all_urls = _collect_all_urls(tree)
        has_depth = any(n.get("children") for n in tree)
        log.info(
            "Playwright sidebar: %d top-level nodes, %d total URLs, deep=%s",
            len(tree),
            len(all_urls),
            has_depth,
        )
        return tree

    nav_area = (
        soup.find("nav", attrs={"aria-label": True})
        or soup.find(class_="sidebar-nav")
        or soup.find("nav")
    )
    if not nav_area:
        return []

    anchors = nav_area.find_all("a", href=True)
    flat_nodes: list[dict] = []
    for anchor in anchors:
        href = anchor.get("href", "")
        if not href or href.startswith("#") or href.startswith("javascript"):
            continue
        full_url = urljoin(base_url, href)
        title = anchor.get_text(separator=" ", strip=True) or "Untitled"
        depth = 0
        parent = anchor.parent
        while parent and parent != nav_area:
            tag = parent.name or ""
            if tag in ("ul", "ol", "li"):
                depth += 1
            parent = parent.parent
        depth = min(depth // 2, 6)
        flat_nodes.append({"title": title, "url": full_url, "depth": depth, "children": []})

    if not flat_nodes:
        return []

    return _depth_list_to_tree(flat_nodes)


def _pw_extract_angular_deep(page_obj: Any, nav: Tag, base_url: str, max_depth: int = 0) -> list[dict]:
    """
    Extract a deep (3+ level) navigation tree from a DeveloperHub Angular tree.

    DeveloperHub sidebar is an Angular tree with accordion behavior: only one
    top-level section is expanded at a time. Collapsed sections' children are
    not visible in the DOM, but they ARE present (just hidden with CSS).

    Algorithm:
    1. Get all section elements and names.
    2. Iterate through each section: click to expand it, wait for the accordion
       to settle, extract level-2 children for that section by finding nodes between
       this section and the next in the DOM. Previous sections collapse but their
       nodes remain in the DOM (hidden), so we capture them during iteration.
    3. For each level-2 child marked as collapsed (has sub-pages), navigate
       to that child's URL and extract level-3 sub-pages from its sidebar.
    """
    try:
        page_obj.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        page_obj.wait_for_timeout(2000)

    section_selectors = [
        ".angular-tree-component .category-container",
        ".sidebar .category-container",
    ]

    section_elements: list[Any] = []
    for sel in section_selectors:
        try:
            section_elements = page_obj.query_selector_all(sel)
            if section_elements:
                break
        except Exception:
            pass

    if not section_elements:
        log.warning("No category sections found in Angular tree")
        return []

    section_names: list[str] = []
    for sec_el in section_elements:
        try:
            html = sec_el.inner_html()
            s_soup = BeautifulSoup(html, "html.parser")
            span_cat = s_soup.find(
                "span",
                class_=lambda x: x and "category" in (" ".join(x) if isinstance(x, list) else (x or "")),
            )
            name = span_cat.get_text(strip=True) if span_cat else sec_el.inner_text().strip().split("\n")[0]
            section_names.append(name)
        except Exception:
            section_names.append("")

    log.info("Angular accordion: found %d sections, extracting L2 children", len(section_names))

    tree: list[dict] = []

    section_elements = []
    for sel in section_selectors:
        try:
            section_elements = page_obj.query_selector_all(sel)
            if section_elements:
                break
        except Exception:
            pass

    def get_l2_for_section(sec_el: Any) -> list[dict]:
        try:
            soup = BeautifulSoup(page_obj.content(), "html.parser")
        except Exception:
            return []
        tnc = soup.find("tree-node-collection")
        if not tnc:
            return []
        container = tnc.find("div")
        if not container:
            return []
        all_tree = container.find_all(
            "div",
            class_=lambda x: (
                x and "tree-node-level-" in (" ".join(x) if isinstance(x, list) else (x or ""))
            ),
        )
        section_positions: dict[str, int] = {}
        for idx, node in enumerate(all_tree):
            classes = node.get("class", [])
            classes_str = " ".join(classes) if isinstance(classes, list) else classes or ""
            node_func = node.find("span", class_="node-function")
            if node_func:
                node_text = node_func.find("div", class_="node-text")
                if node_text:
                    span_cat = node_text.find(
                        "span",
                        class_=lambda x: x and "category" in (" ".join(x) if isinstance(x, list) else (x or "")),
                    )
                    if span_cat:
                        section_positions[span_cat.get_text(strip=True)] = idx
        sorted_positions = sorted(section_positions.items(), key=lambda x: x[1])
        return all_tree, sorted_positions

    prev_expanded_idx = 0

    for sec_idx, sec_name in enumerate(section_names):
        if not sec_name:
            continue

        if sec_idx != prev_expanded_idx:
            try:
                sec_el = section_elements[sec_idx]
                sec_el.click(timeout=2000)
                page_obj.wait_for_timeout(1000)
                prev_expanded_idx = sec_idx
            except Exception as exc:
                log.warning("Could not expand section %d '%s': %s", sec_idx, sec_name, exc)
                continue

        try:
            soup = BeautifulSoup(page_obj.content(), "html.parser")
        except Exception:
            continue
        tnc = soup.find("tree-node-collection")
        if not tnc:
            continue
        container = tnc.find("div")
        if not container:
            continue
        all_tree = container.find_all(
            "div",
            class_=lambda x: (
                x and "tree-node-level-" in (" ".join(x) if isinstance(x, list) else (x or ""))
            ),
        )
        section_positions: dict[str, int] = {}
        for idx, node in enumerate(all_tree):
            classes = node.get("class", [])
            classes_str = " ".join(classes) if isinstance(classes, list) else classes or ""
            node_func = node.find("span", class_="node-function")
            if node_func:
                node_text = node_func.find("div", class_="node-text")
                if node_text:
                    span_cat = node_text.find(
                        "span",
                        class_=lambda x: x and "category" in (" ".join(x) if isinstance(x, list) else (x or "")),
                    )
                    if span_cat:
                        section_positions[span_cat.get_text(strip=True)] = idx
        sorted_positions = sorted(section_positions.items(), key=lambda x: x[1])

        for s_idx, (s_name, s_pos) in enumerate(sorted_positions):
            if s_name != sec_name:
                continue
            next_pos = sorted_positions[s_idx + 1][1] if s_idx + 1 < len(sorted_positions) else len(all_tree)

            section_node: dict[str, Any] = {
                "title": sec_name,
                "url": None,
                "depth": 0,
                "_section_type": "section",
                "children": [],
            }
            l2_count = 0

            for node in all_tree[s_pos + 1 : next_pos]:
                classes = node.get("class", [])
                classes_str = " ".join(classes) if isinstance(classes, list) else classes or ""
                if "tree-node-level-2" not in classes_str:
                    continue

                anchor = node.find("a", href=True)
                if not anchor:
                    continue

                href = anchor.get("href", "")
                if href.startswith("#") or href.startswith("javascript"):
                    continue

                l2_url = urljoin(base_url, href)
                l2_title = anchor.get_text(separator=" ", strip=True)

                has_l3 = "tree-node-collapsed" in classes_str
                l2_node: dict[str, Any] = {
                    "title": l2_title,
                    "url": l2_url,
                    "depth": 1,
                    "children": [],
                }

                if has_l3:
                    if max_depth <= 2:
                        pass
                    else:
                        log.info("    L3: extracting from %s", l2_url)
                        l3_nodes = _pw_extract_l3_from_page(page_obj, l2_url)
                        log.info("    L3: got %d nodes for %s", len(l3_nodes), l2_title[:30])
                        if l3_nodes:
                            l2_node["children"] = l3_nodes

                section_node["children"].append(l2_node)
                l2_count += 1

            if l2_count > 0:
                log.info("  Angular accordion section '%s': %d L2 children", sec_name, l2_count)
            tree.append(section_node)
            break

    return tree


def _pw_extract_l3_from_page(page_obj: Any, page_url: str) -> list[dict]:
    """
    Extract level-3 sub-pages from the sidebar when viewing a page that has them.

    When navigating to a page like /pulse/user-guide/analyze-cluster-health-in-detail,
    the sidebar shows all section headers + the current page's section with its
    L2 and L3 children expanded. We find the current L2 page node and extract
    all L3 siblings that follow it until the next section header.
    """
    try:
        page_obj.goto(page_url, wait_until="networkidle", timeout=30000)
        page_obj.wait_for_timeout(3000)
    except Exception:
        try:
            page_obj.goto(page_url, wait_until="domcontentloaded", timeout=30000)
            page_obj.wait_for_timeout(3000)
        except Exception:
            pass

    try:
        soup = BeautifulSoup(page_obj.content(), "html.parser")
        tnc = soup.find("tree-node-collection")
        if not tnc:
            return []

        container = tnc.find("div")
        if not container:
            return []

        all_tree = container.find_all(
            "div",
            class_=lambda x: (
                x and "tree-node-level-" in (" ".join(x) if isinstance(x, list) else (x or ""))
            ),
        )

        parsed_url = urlparse(page_url)
        page_path = parsed_url.path.rstrip("/")

        current_idx: int | None = None
        for idx, node in enumerate(all_tree):
            anchor = node.find("a", href=True)
            if not anchor:
                continue
            href_path = urlparse(anchor.get("href", "")).path.rstrip("/")
            if href_path == page_path or page_path.endswith(href_path):
                if current_idx is None or idx < current_idx:
                    current_idx = idx

        if current_idx is None:
            return []

        l3_nodes: list[dict] = []
        for node in all_tree[current_idx + 1 :]:
            classes = node.get("class", [])
            classes_str = " ".join(classes) if isinstance(classes, list) else classes or ""

            if "tree-node-level-1" in classes_str and "category-container" in classes_str:
                break

            if "tree-node-level-3" in classes_str:
                anchor = node.find("a", href=True)
                if anchor:
                    href = anchor.get("href", "")
                    if not href.startswith("#") and not href.startswith("javascript"):
                        l3_nodes.append({
                            "title": anchor.get_text(separator=" ", strip=True),
                            "url": urljoin("https://docs.acceldata.io", href),
                            "depth": 2,
                            "children": [],
                        })

        return l3_nodes
    except Exception:
        return []


def _depth_list_to_tree(flat: list[dict]) -> list[dict]:
    """
    Convert a flat list of dicts with 'depth' field into a nested tree.
    Each node's children are all immediately following nodes at depth+1
    before any node at <= current depth.
    """
    root: list[dict] = []
    stack: list[tuple[int, list[dict]]] = [(-1, root)]  # (depth, children_list)

    for node in flat:
        node = {**node, "children": []}
        d = node["depth"]
        # Pop stack until we find a parent at depth < d
        while len(stack) > 1 and stack[-1][0] >= d:
            stack.pop()
        stack[-1][1].append(node)
        stack.append((d, node["children"]))

    return root


def _fetch_html_playwright(url: str, pw_browser: Any) -> str | None:
    """Fetch a page using a running Playwright browser instance.

    Waits for the Angular-rendered content container to appear before
    returning the full page HTML. This ensures the actual documentation
    content is present, not just the SPA shell.
    """
    try:
        page = pw_browser.new_page()
        page.goto(url, timeout=45000)
        # Wait for DeveloperHub Angular content to render
        try:
            page.wait_for_selector(
                ".content-container, .editor-top-level, .master-content",
                timeout=15000,
            )
        except Exception:
            # Fallback: just wait a bit for any slow rendering
            page.wait_for_timeout(5000)
        html = page.content()
        page.close()
        return html
    except Exception as exc:
        log.warning("Playwright fetch failed for %s: %s", url, exc)
        return None


def discover_structure(
    source_url: str,
    use_playwright: bool = False,
    apply_category_map: bool = True,
    max_depth: int = 0,
) -> tuple[list[dict], list[str]]:
    """
    Discover the navigation hierarchy of the documentation site.

    Discovery order:
    1. If use_playwright=True (and playwright is installed): launch a headless
       Chromium browser, render the JS sidebar, click open all collapsed sections,
       and walk the full multi-level hierarchy for ALL versions and tabs.
       This extracts everything: all versions, all tabs, all sections/subsections.
    2. Static HTML: fetch with requests and try to find a nav element.
    3. Sitemap.xml fallback with ACCELDATA_CATEGORY_MAP grouping.

    Returns (tree, fallback_link_list).
    """
    parsed = urlparse(source_url)
    base_url = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
    source_path = parsed.path.rstrip("/")

    tree: list[dict] = []
    fallback_links: list[str] = []

    # -----------------------------------------------------------------------
    # Strategy 1: Playwright — renders JS, clicks open all collapsed sections,
    # gives the true multi-level hierarchy
    # -----------------------------------------------------------------------
    if use_playwright and _check_playwright():
        log.info("Using Playwright for JS-rendered sidebar discovery…")
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                context = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    )
                )
                page_obj = context.new_page()
                log.info("Playwright: navigating to %s", source_url)
                page_obj.goto(source_url, wait_until="domcontentloaded", timeout=45000)
                page_obj.wait_for_timeout(2000)
                versions = _pw_discover_versions(page_obj)
                _pw_close_dropdown(page_obj)
                if versions:
                    log.info("Discovered %d versions, extracting all tabs/sections for each", len(versions))
                    all_urls: list[str] = []
                    for ver_idx, ver in enumerate(versions):
                        ver_name = ver.get("name") or f"Version {ver_idx + 1}"
                        try:
                            page_obj.click("app-version-picker .top-picker", timeout=5000)
                            page_obj.wait_for_timeout(1000)
                            page_obj.evaluate(
                                f"""() => {{
                                    const items = document.querySelectorAll("app-version-picker li");
                                    for (const li of items) {{
                                        if (li.innerText?.trim() === "{ver["name"]}") {{
                                            li.click();
                                            break;
                                        }}
                                    }}
                                }}"""
                            )
                            page_obj.wait_for_timeout(2000)
                            ver_url = page_obj.url
                            log.info("  Version %d/%d: %s -> %s", ver_idx + 1, len(versions), ver_name, ver_url)

                            page_obj.wait_for_timeout(1000)
                            tabs = _pw_discover_tabs(page_obj)
                            if not tabs:
                                tabs = [{"name": ver_name, "url_path": urlparse(ver_url).path.rstrip("/")}]

                            version_node = {
                                "title": ver_name,
                                "url": ver_url,
                                "depth": 0,
                                "_section_type": "version",
                                "children": [],
                            }
                            version_page_count = 0

                            for tab in tabs:
                                tab_url = urljoin(base_url, tab["url_path"])
                                try:
                                    page_obj.goto(tab_url, wait_until="domcontentloaded", timeout=15000)
                                    page_obj.wait_for_timeout(1000)
                                    tab_tree = _pw_extract_nav_tree(page_obj, tab_url, max_depth=max_depth)
                                    if tab_tree:
                                        tab_urls = _collect_all_urls(tab_tree)
                                        all_urls.extend(tab_urls)
                                        version_page_count += len(tab_urls)
                                        tab_node = {
                                            "title": tab["name"],
                                            "url": tab_url,
                                            "depth": 0,
                                            "_section_type": "tab",
                                            "children": tab_tree,
                                        }
                                        version_node["children"].append(tab_node)
                                        log.info("    %s: %d pages", tab["name"], len(tab_urls))
                                except Exception as exc:
                                    log.warning("    Failed tab %s: %s", tab["name"], exc)

                            if version_node["children"]:
                                tree.append(version_node)
                                log.info("  Version %s: %d pages across %d tabs", ver_name, version_page_count, len(version_node["children"]))
                        except Exception as exc:
                            log.warning("  Failed version %s: %s", ver_name, exc)
                    if not tree:
                        tabs = _pw_discover_tabs(page_obj)
                        if tabs:
                            log.info("Discovered %d tabs, extracting sections for each", len(tabs))
                            for tab in tabs:
                                tab_url = urljoin(base_url, tab["url_path"])
                                log.info("  Tab: %s -> %s", tab["name"], tab_url)
                                try:
                                    page_obj.goto(tab_url, wait_until="domcontentloaded", timeout=15000)
                                    page_obj.wait_for_timeout(1000)
                                    tab_tree = _pw_extract_nav_tree(page_obj, tab_url, max_depth=max_depth)
                                    if tab_tree:
                                        tree.append({
                                            "title": tab["name"],
                                            "url": tab_url,
                                            "depth": 0,
                                            "_section_type": "tab",
                                            "children": tab_tree,
                                        })
                                except Exception as exc:
                                    log.warning("  Failed to extract tab %s: %s", tab["name"], exc)
                        elif not tree:
                            tree = _pw_extract_nav_tree(page_obj, source_url, max_depth=max_depth)
                browser.close()
        except Exception as exc:
            log.warning("Playwright discovery failed: %s — falling back to static", exc)
            tree = []

        if tree:
            all_urls = _collect_all_urls(tree)
            log.info(
                "Playwright discovery complete: %d sections, %d pages total",
                len(tree),
                len(all_urls),
            )

            if len(all_urls) < 200 and apply_category_map and max_depth > 2 and not versions:
                log.info(
                    "Playwright sidebar has only %d URLs (collapsed sections) — "
                    "supplementing with sitemap, preserving Playwright section hierarchy",
                    len(all_urls),
                )
                sitemap_urls = _fetch_sitemap_urls(base_url, source_path + "/")
                if not sitemap_urls:
                    sitemap_urls = _fetch_sitemap_urls(base_url, source_path)
                if sitemap_urls:
                    existing_urls: set[str] = set()
                    for node in tree:
                        if node.get("url"):
                            existing_urls.add(node["url"])
                        for child in _flatten_pages(node.get("children", [])):
                            if child.get("url"):
                                existing_urls.add(child["url"])

                    for node in tree:
                        if not node.get("children"):
                            node["children"] = []

                    uncategorized: list[dict] = []
                    for url in sitemap_urls:
                        if url in existing_urls:
                            continue
                        slug = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
                        title = slug.replace("-", " ").replace("_", " ").title()
                        category = _categorize_url(slug)
                        page_node = {"title": title, "url": url, "depth": 1, "children": []}
                        if category:
                            matched = False
                            for section in tree:
                                if section["title"] == category:
                                    section["children"].append(page_node)
                                    matched = True
                                    break
                            if not matched:
                                uncategorized.append(page_node)
                        else:
                            uncategorized.append(page_node)

                    if uncategorized:
                        tree.append({
                            "title": "Other",
                            "url": None,
                            "depth": 0,
                            "children": uncategorized,
                        })

                    merged_urls = _collect_all_urls(tree)
                    log.info(
                        "Merged tree: %d sections, %d total pages",
                        len(tree),
                        len(merged_urls),
                    )
                    fallback_links = sitemap_urls
                else:
                    fallback_links = all_urls
            else:
                fallback_links = all_urls

    # -----------------------------------------------------------------------
    # Strategy 2: Static HTML (works for server-rendered sites)
    # -----------------------------------------------------------------------
    if not tree:
        log.info("Fetching source page (static): %s", source_url)
        html = fetch_html(source_url)
        if not html:
            log.error("Could not load source URL: %s", source_url)
            sys.exit(1)

        soup = BeautifulSoup(html, "html.parser")
        nav = _find_nav(soup)
        if nav:
            tree = _walk_nav_tree(nav, source_url)

        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith("/") or href.startswith(parsed.scheme + "://"):
                full = urljoin(source_url, href)
                if urlparse(full).netloc == parsed.netloc:
                    fallback_links.append(full)
        fallback_links = list(dict.fromkeys(fallback_links))

    # -----------------------------------------------------------------------
    # Strategy 3: Sitemap.xml fallback + category map
    # -----------------------------------------------------------------------
    if not tree:
        log.info("No sidebar found — trying sitemap.xml")
        sitemap_urls = _fetch_sitemap_urls(base_url, source_path + "/")
        if not sitemap_urls:
            sitemap_urls = _fetch_sitemap_urls(base_url, source_path)
        if sitemap_urls:
            log.info(
                "Sitemap: %d URLs — applying %s",
                len(sitemap_urls),
                "category hierarchy" if apply_category_map else "path grouping",
            )
            tree = _sitemap_urls_to_tree(sitemap_urls, apply_category_map=apply_category_map)
            fallback_links = sitemap_urls
        elif fallback_links:
            log.warning("No sitemap — building flat tree from page links")
            for url in fallback_links:
                path_parts = urlparse(url).path.strip("/").split("/")
                title = path_parts[-1].replace("-", " ").replace("_", " ").title() if path_parts else "Page"
                tree.append({"title": title, "url": url, "depth": 0, "children": []})
        else:
            log.error("Could not discover any pages. Check the source URL.")
            sys.exit(1)

    # If we got a static/sitemap tree that is suspiciously flat, apply category map
    if not use_playwright and apply_category_map:
        all_urls = _collect_all_urls(tree)
        has_depth = any(n.get("children") for n in tree)
        if not has_depth and len(all_urls) > 20:
            log.info("Tree is flat (%d pages) — applying Acceldata category map", len(all_urls))
            tree = _apply_category_hierarchy(tree)

    log.info(
        "Discovery complete: %d top-level sections/nodes, %d total pages",
        len(tree),
        len(_collect_all_urls(tree)),
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

    # Check pluginobject JSON attribute (DeveloperHub <app-callout> Angular component)
    plugin_obj = elem.get("pluginobject")
    if plugin_obj:
        try:
            import json
            obj = json.loads(plugin_obj)
            data = obj.get("data", {})
            plugin_type = (data.get("type") or "").lower()
            if plugin_type in _CALLOUT_TYPE_MAP:
                return _CALLOUT_TYPE_MAP[plugin_type]
        except Exception:
            pass

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
    # DeveloperHub Angular-rendered content containers (most specific first)
    lambda soup: soup.find(class_="content-container"),
    lambda soup: soup.find(class_="editor-top-level"),
    lambda soup: soup.find(class_="master-content"),
    # Generic doc platform selectors
    lambda soup: soup.find("main"),
    lambda soup: soup.find("article"),
    lambda soup: soup.find(class_="content-body"),
    lambda soup: soup.find(attrs={"role": "main"}),
    lambda soup: soup.find(class_="page-content"),
    lambda soup: soup.find(class_="docs-content"),
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
            "--to=gfm+pipe_tables+task_lists",
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


def _convert_callouts_to_admonition_html(soup_elem: Tag) -> None:
    """Convert DeveloperHub callout elements in-place to AccelDocs admonition HTML.

    DeveloperHub uses:
        <div class="callout warning">
          <div class="callout-text">Do not restart the pods...</div>
        </div>
    Or Angular component:
        <app-callout pluginobject='{"data":{"text":"...","type":"info"}}'>
    Or:
        <span class="cbadge info">Important</span>

    Converts to:
        <div class="admonition warning">
          <p class="admonition-title">Warning</p>
          <div class="admonition-body">Do not restart the pods...</div>
        </div>

    This matches the CSS in AccelDocs' docs.html template.
    """
    import json as _json

    # Include app-callout Angular component alongside standard HTML elements
    for tag_name in ["aside", "div", "section", "blockquote", "app-callout"]:
        for elem in list(soup_elem.find_all(tag_name)):
            atype = _detect_callout_type(elem)
            if not atype:
                continue

            classes = " ".join(elem.get("class", []) or []).lower()
            if "callout-text" in classes:
                continue

            # Handle <app-callout> Angular component: extract from pluginobject JSON
            plugin_obj = elem.get("pluginobject")
            if plugin_obj:
                try:
                    obj = _json.loads(plugin_obj)
                    data = obj.get("data", {})
                    body_html = data.get("text", "").strip()
                    title_text = (data.get("title") or atype.capitalize()).strip()
                except Exception:
                    body_html = ""
                    title_text = atype.capitalize()
            else:
                # Extract title from child elements
                title_elem = elem.find(
                    lambda t: t and t.name in ("p", "span", "strong", "h1", "h2", "h3", "h4", "h5")
                    and any(
                        cls in " ".join(t.get("class", []) or []).lower()
                        for cls in ("title", "header", "heading", "callout-title", "admonition-title", "cbadge")
                    )
                )
                if title_elem:
                    title_text = title_elem.get_text(strip=True)
                    title_elem.decompose()
                else:
                    title_text = atype.capitalize()

                # Extract body from callout-text wrapper
                callout_text_div = elem.find(class_="callout-text")
                if callout_text_div:
                    body_html = callout_text_div.decode_contents().strip()
                else:
                    body_html = elem.decode_contents().strip()

            if not body_html:
                continue

            # Build admonition: body may contain block elements (p, ul, etc.)
            # Use div wrapper instead of <p> to avoid invalid nesting
            admonition_html = (
                f'<div class="admonition {atype}">'
                f'<p class="admonition-title">{title_text}</p>'
                f'<div class="admonition-body">{body_html}</div>'
                f'</div>'
            )
            new_elem = BeautifulSoup(admonition_html, "html.parser")
            elem.replace_with(new_elem)


def _convert_tabs_to_html(soup_elem: Tag) -> None:
    """Convert DeveloperHub tab components in-place to simple HTML tab structure.

    Output format (styled by AccelDocs CSS):
        <div class="tabs-container">
          <div class="tab-content" data-tab="Tab Name">
            <h4>Tab Name</h4>
            ...content...
          </div>
        </div>
    """
    # Handle <tab-group> web component
    for tab_group in list(soup_elem.find_all("tab-group")):
        tabs = tab_group.find_all(["tab", "tab-panel"])
        if not tabs:
            continue
        parts = ['<div class="tabs-container">']
        for tab in tabs:
            label = tab.get("label") or tab.get("title") or "Tab"
            content = tab.decode_contents().strip()
            parts.append(f'<div class="tab-content" data-tab="{label}"><h4>{label}</h4>{content}</div>')
        parts.append('</div>')
        new_elem = BeautifulSoup("\n".join(parts), "html.parser")
        tab_group.replace_with(new_elem)

    # Handle div.tabs-wrapper / div.tab-group
    for wrapper in soup_elem.find_all("div", class_=re.compile(r"tabs?[-_]?(wrapper|group|container)", re.I)):
        panels = (
            wrapper.find_all(["div", "section"], attrs={"data-tab": True})
            or wrapper.find_all(["div", "section"], role="tabpanel")
            or wrapper.find_all(
                ["div", "section"],
                class_=re.compile(r"tab[-_]?(panel|content|pane)", re.I),
            )
        )
        if not panels:
            continue
        tab_labels = [
            btn.get_text(strip=True)
            for btn in wrapper.find_all(
                ["button", "a", "li"],
                class_=re.compile(r"tab[-_]?(item|label|button|link)?", re.I),
            )
        ]
        parts = ['<div class="tabs-container">']
        for idx, panel in enumerate(panels):
            label = (
                panel.get("data-tab")
                or panel.get("aria-label")
                or (tab_labels[idx] if idx < len(tab_labels) else f"Tab {idx + 1}")
            )
            content = panel.decode_contents().strip()
            parts.append(f'<div class="tab-content" data-tab="{label}"><h4>{label}</h4>{content}</div>')
        parts.append('</div>')
        new_elem = BeautifulSoup("\n".join(parts), "html.parser")
        wrapper.replace_with(new_elem)


def _clean_developerhub_html(soup_elem: Tag) -> None:
    """Remove DeveloperHub-specific scripts, styles, and wrapper elements."""
    # Remove script and style tags
    for tag in soup_elem.find_all(["script", "style", "noscript"]):
        tag.decompose()

    # Remove known DeveloperHub UI chrome divs
    for div in soup_elem.find_all("div"):
        try:
            classes = " ".join(div.get("class", []) or []).lower()
        except (AttributeError, TypeError):
            continue
        if any(x in classes for x in [
            "sidebar", "nav-", "breadcrumb", "footer", "header-",
            "feedback", "was-this-helpful", "edit-page", "table-of-contents",
            "on-this-page", "page-nav", "pagination",
        ]):
            div.decompose()

    def _strip_attrs(tag: Tag) -> None:
        if not hasattr(tag, "attrs") or not tag.attrs:
            return
        attrs_to_remove = [
            attr for attr in tag.attrs
            if (attr.startswith("data-") and attr not in ("data-tab",))
            or attr.startswith("ng-")
            or attr.startswith("_ngcontent")
            or attr.startswith("_nghost")
        ]
        for attr in attrs_to_remove:
            del tag[attr]

    # Strip attrs from root element and all descendants
    _strip_attrs(soup_elem)
    for tag in soup_elem.find_all(True):
        _strip_attrs(tag)


def _admonitions_to_blockquotes(html: str) -> str:
    """
    Convert AccelDocs-style admonition HTML to Google Docs-compatible blockquotes.

    AccelDocs admonitions use:
        <div class="admonition warning">
          <p class="admonition-title">Warning</p>
          <div class="admonition-body"><p>Body text...</p></div>
        </div>

    Google Docs only renders semantic HTML. We convert to:
        <blockquote>
          <p><strong>Warning:</strong></p>
          <p>Body text...</p>
        </blockquote>

    Google Docs renders <blockquote> as indented text with a left border,
    which is the closest equivalent to a callout box.
    """
    soup = BeautifulSoup(html, "html.parser")
    for adv in list(soup.find_all("div", class_="admonition")):
        title_elem = adv.find("p", class_="admonition-title")
        body_elem = adv.find("div", class_="admonition-body")

        # Extract type from class for blockquote styling
        adv_classes = adv.get("class", [])
        atype = next(
            (c for c in adv_classes if c != "admonition"),
            "note",
        )

        title_text = title_elem.get_text(strip=True) if title_elem else atype.capitalize()
        body_html = body_elem.decode_contents().strip() if body_elem else ""

        if not body_html:
            adv.decompose()
            continue

        # Build blockquote with bold title
        blockquote = soup.new_tag("blockquote")
        title_p = soup.new_tag("p")
        title_strong = soup.new_tag("strong")
        title_strong.string = f"{title_text}:"
        title_p.append(title_strong)
        blockquote.append(title_p)

        # Parse body HTML and append children (wrap bare text in <p> tags)
        body_soup = BeautifulSoup(body_html, "html.parser")
        body_children = list(body_soup.children)
        if not body_children or all(isinstance(c, str) and c.strip() for c in body_children):
            # No block elements — wrap the whole thing in a <p>
            p = soup.new_tag("p")
            p.string = body_html
            blockquote.append(p)
        else:
            for child in body_children:
                if isinstance(child, str) and child.strip():
                    p = soup.new_tag("p")
                    p.string = child.strip()
                    blockquote.append(p)
                else:
                    blockquote.append(child)

        adv.replace_with(blockquote)

    return str(soup)


def _convert_codemirror_to_code_blocks(soup_elem: Tag) -> None:
    """
    Convert CodeMirror-rendered code lines into proper <pre><code> blocks.

    DeveloperHub renders code in CodeMirror editors, producing:
        <div class="CodeMirror-code">
          <pre class="CodeMirror-line">line 1</pre>
          <pre class="CodeMirror-line">line 2</pre>
          ...
        </div>

    These nested <pre class="CodeMirror-line"> elements don't render as code blocks
    in Google Docs. We find all CodeMirror-code wrappers, extract the code lines,
    and replace them with a single <pre><code> block.
    """
    # Find all CodeMirror-code wrapper divs
    cm_wrappers = soup_elem.find_all(
        "div",
        class_="CodeMirror-code",
    )

    for wrapper in cm_wrappers:
        # Collect all CodeMirror-line text
        lines: list[str] = []
        for pre in wrapper.find_all("pre", class_="CodeMirror-line"):
            text = pre.get_text()
            lines.append(text)

        if not lines:
            continue

        # Create proper <pre><code> block
        code_text = "\n".join(lines)
        # Use wrapper's underlying soup to create new tags
        new_soup = BeautifulSoup("", "html.parser")
        code_elem = new_soup.new_tag("code")
        code_elem.string = code_text
        pre_elem = new_soup.new_tag("pre")
        pre_elem.append(code_elem)

        # Replace the entire CodeMirror-code wrapper with the code block
        wrapper.replace_with(pre_elem)


def _convert_custom_html_components(soup_elem: Tag) -> None:
    """
    Extract HTML content from DeveloperHub <app-custom-html> Angular components.

    DeveloperHub uses <app-custom-html> components to embed arbitrary HTML content
    (e.g., compatibility matrices, custom styled sections). The HTML is stored in
    a pluginobject JSON attribute as URL-encoded data.

    Example:
        <app-custom-html pluginobject='{"data":{"contents":"<!DOCTYPE html>..."}}'>

    We extract the HTML, parse it, strip the wrapper tags (doctype, html, head, body)
    and insert the body content directly.
    """
    import json as _json
    from urllib.parse import unquote as _unquote

    custom_html_components = list(soup_elem.find_all("app-custom-html"))
    if not custom_html_components:
        return

    # Get the parent soup to create new tags
    parent_soup = BeautifulSoup("", "html.parser")

    for comp in custom_html_components:
        plugin_obj = comp.get("pluginobject", "")
        if not plugin_obj:
            comp.decompose()
            continue

        try:
            data = _json.loads(plugin_obj)
            contents = data.get("data", {}).get("contents", "")
            if not contents:
                comp.decompose()
                continue

            # Decode URL-encoded HTML
            decoded_html = _unquote(contents)

            # Find body content boundaries
            body_start = decoded_html.lower().find("<body")
            body_end = decoded_html.lower().rfind("</body>")

            if body_start < 0 or body_end < 0 or body_end <= body_start:
                comp.decompose()
                continue

            # Extract content between body tags
            body_start_tag_end = decoded_html.find(">", body_start) + 1
            body_content = decoded_html[body_start_tag_end:body_end]

            # Parse body content - body_content doesn't include <body> tags
            body_soup = BeautifulSoup(body_content, "html.parser")

            # Extract all children as a fragment
            children_html = "".join(str(child) for child in body_soup.children if hasattr(child, 'name') and child.name)
            if not children_html:
                comp.decompose()
                continue

            # Parse the children HTML and append to a wrapper
            wrapper = parent_soup.new_tag("div")
            wrapper["class"] = "custom-html-content"

            if children_html:
                children_soup = BeautifulSoup(children_html, "html.parser")
                for child in children_soup.children:
                    if hasattr(child, 'name') and child.name:
                        wrapper.append(child)

            # Replace the component with the wrapper
            comp.replace_with(wrapper)

        except Exception:
            comp.decompose()


def fetch_and_convert_page(url: str, pw_browser: Any = None) -> dict | None:
    """
    Fetch a page, extract main content, handle callouts + tabs, convert to Markdown.

    If pw_browser is supplied (a Playwright Browser instance), uses it to render
    the page with JavaScript so that dynamically-loaded content is included.
    Otherwise falls back to a plain HTTP GET.

    Returns dict with keys: url, title, markdown, raw_html
    """
    log.debug("Fetching page: %s", url)
    if pw_browser is not None:
        html = _fetch_html_playwright(url, pw_browser)
    else:
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

    # 1. Extract main content area first
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

    # 2. Convert DeveloperHub callouts directly to AccelDocs admonition HTML
    #    (no MkDocs !!! syntax — stays as clean HTML)
    _convert_callouts_to_admonition_html(content_elem)

    # 3. Convert DeveloperHub tabs to AccelDocs tab HTML
    _convert_tabs_to_html(content_elem)

    # 4. Extract HTML content from <app-custom-html> components
    #    (e.g., compatibility matrices, custom styled sections)
    _convert_custom_html_components(content_elem)

    # 5. Convert CodeMirror-rendered code lines to proper <pre><code> blocks
    _convert_codemirror_to_code_blocks(content_elem)

    # 5. Clean up the HTML: strip DeveloperHub-specific wrappers, classes, scripts
    _clean_developerhub_html(content_elem)

    content_html = str(content_elem)

    # 5. Also produce Markdown as fallback (for pages where raw_html fails)
    # Use placeholders for callouts/tabs since they don't survive pandoc well
    md_soup = BeautifulSoup(content_html, "html.parser")
    md_soup, callout_placeholders = _convert_callouts_to_placeholders(md_soup)
    md_soup, tab_placeholders = _convert_tabs_to_placeholders(md_soup)
    md_html = str(md_soup)

    if _check_pandoc():
        markdown = _html_to_markdown_pandoc(md_html)
    else:
        markdown = _html_to_markdown_fallback(md_html)

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


def rewrite_html_internal_links(html: str, source_domain: str, slug_map: dict[str, str]) -> str:
    """
    Rewrite internal links in HTML content.

    Converts <a href="https://docs.acceldata.io/pulse/..."> links to
    [[MIGRATED:path]] placeholders (URL path) that can be resolved to
    /pages/{id} URLs after import using old_url_to_page_id mapping.

    External links and anchor-only links (#section) are left unchanged.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        parsed = urlparse(href)
        # Skip external links
        if parsed.netloc and parsed.netloc != source_domain:
            continue
        # Skip anchor-only links (e.g., #supported-databases)
        if not parsed.path or parsed.path == "/":
            continue
        path = parsed.path.rstrip("/")
        slug = slug_map.get(path)
        if slug:
            # Store the path as identifier for later resolution
            # Format: [[MIGRATED:/pulse/user-guide/overview]]
            anchor["href"] = f"[[MIGRATED:{path}]]"
            # Preserve fragment if present
            if parsed.fragment:
                anchor["href"] += f"#{parsed.fragment}"
    return str(soup)


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
        section_type: str = "section",
    ) -> dict:
        body: dict[str, Any] = {
            "name": name,
            "section_type": section_type,
            "visibility": "public",
            "display_order": display_order,
        }
        if parent_id is not None:
            body["parent_id"] = parent_id
        return self._post_json("/api/sections", body)

    def import_page(
        self,
        title: str,
        section_id: int,
        display_order: int = 0,
        create_drive_doc: bool = False,
        markdown_content: str = "",
        html_content: str = "",
        drive_html_content: str = "",
    ) -> dict:
        """
        POST /api/pages/import — create a page from HTML or Markdown.

        If html_content is provided, it is stored directly (no conversion).
        Otherwise markdown_content is converted to HTML on the backend.
        If create_drive_doc is True, also creates a Google Doc in Drive.
        drive_html_content provides Google Docs-compatible HTML (admonitions as blockquotes).
        """
        body: dict[str, Any] = {
            "title": title,
            "section_id": section_id,
            "display_order": display_order,
        }
        if html_content:
            body["html_content"] = html_content
        else:
            body["markdown_content"] = markdown_content
        if drive_html_content:
            body["drive_html_content"] = drive_html_content
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
        # Use _section_type from the tree node (tab/version/section)
        section_type = node.get("_section_type", "section")

        has_children = bool(children)

        if has_children:
            section_id = section_map.get(node_path)
            if not section_id:
                log.info("Creating %s: %s (parent=%d)", section_type, title, parent_section_id)
                try:
                    result = client.create_section(
                        name=title,
                        parent_id=parent_section_id,
                        display_order=order,
                        section_type=section_type,
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
        # Prefer raw_html (cleaned, with callouts converted to admonition divs)
        # over markdown to avoid lossy round-trip
        raw_html = data.get("raw_html") or ""
        markdown = data.get("markdown") or ""
        # Google Docs-compatible HTML: admonitions as blockquotes
        drive_html = _admonitions_to_blockquotes(raw_html) if raw_html else ""

        log.info("Importing page '%s' (%s) into section=%d", title, url, section_id)
        try:
            if raw_html:
                result = client.import_page(
                    title=title,
                    html_content=raw_html,
                    section_id=section_id,
                    display_order=order,
                    create_drive_doc=create_drive_docs,
                    drive_html_content=drive_html,
                )
            else:
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


def _resolve_and_patch_links(
    client: AccelDocsClient,
    old_url_to_page_id: dict[str, int],
    page_data: dict[str, dict],
    log: Any,
) -> None:
    """
    Second pass: resolve [[MIGRATED:slug]] placeholders to /pages/{id} URLs.

    After import_hierarchy(), we have the old_url → new_page_id mapping.
    We resolve all placeholders in page_data and patch each page via PATCH API.
    """
    # Build slug → new_page_id mapping from old_url_to_page_id
    slug_to_page_id: dict[str, int] = {}
    for old_url, page_id in old_url_to_page_id.items():
        if not page_id or page_id <= 0:
            continue
        slug = _slugify(old_url.rstrip("/").rsplit("/", 1)[-1])
        slug_to_page_id[slug] = page_id

    pages_to_patch: list[tuple[int, str, str]] = []

    for old_url, data in page_data.items():
        page_id = old_url_to_page_id.get(old_url)
        if not page_id or page_id <= 0:
            continue

        raw_html = data.get("raw_html") or ""
        markdown = data.get("markdown") or ""

        # Resolve HTML links
        resolved_html = raw_html
        if raw_html and "[[MIGRATED:" in raw_html:
            resolved_html = resolve_migrated_links_html(raw_html, old_url_to_page_id)

        # Resolve Markdown links
        resolved_md = markdown
        if markdown and "[[MIGRATED:" in markdown:
            resolved_md = resolve_migrated_links(markdown, old_url_to_page_id)

        pages_to_patch.append((page_id, resolved_html, resolved_md))

    if not pages_to_patch:
        return

    log.info("Patching %d pages with resolved internal links...", len(pages_to_patch))
    success_count = 0
    for page_id, resolved_html, resolved_md in pages_to_patch:
        try:
            body: dict[str, Any] = {}
            if resolved_html:
                body["html_content"] = resolved_html
            elif resolved_md:
                body["html_content"] = f"<p>{resolved_md}</p>"

            if body:
                client.patch_page(page_id, body)
                success_count += 1
        except Exception as exc:
            log.warning("Failed to patch page %d: %s", page_id, exc)

    log.info("Patched %d/%d pages with resolved internal links", success_count, len(pages_to_patch))


def resolve_migrated_links_html(html: str, old_url_to_page_id: dict[str, int]) -> str:
    """
    Resolve [[MIGRATED:path]] placeholders in HTML to /pages/{id} URLs.

    Also handles fragment anchors: [[MIGRATED:/path]]#section → /pages/{id}#section

    Placeholders store the URL path (e.g., /pulse/user-guide/overview), which is
    used to look up the page_id from old_url_to_page_id.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")

    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if not href.startswith("[[MIGRATED:"):
            continue

        # Extract path and optional fragment
        placeholder = href
        fragment = ""
        if "#" in href:
            placeholder, fragment = href.split("#", 1)
            fragment = f"#{fragment}"

        path = placeholder.replace("[[MIGRATED:", "").replace("]]", "")

        # Look up the page_id using the full path as key
        page_id = None
        for old_url, pid in old_url_to_page_id.items():
            old_path = urlparse(old_url).path.rstrip("/")
            if old_path == path:
                page_id = pid
                break

        if page_id:
            anchor["href"] = f"/pages/{page_id}{fragment}"
        else:
            log.warning("Could not resolve migrated path: %s", path)

    return str(soup)


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
    p.add_argument(
        "--playwright",
        action="store_true",
        help=(
            "Use a headless Chromium browser (via Playwright) to render the JavaScript sidebar "
            "and capture the full multi-level section hierarchy. This is the RECOMMENDED option "
            "for docs.acceldata.io because it is a JS SPA. Also uses Playwright for page content "
            "fetching so dynamically-rendered content is included. "
            "Requires: pip install playwright && playwright install chromium"
        ),
    )
    p.add_argument(
        "--all-tabs",
        action="store_true",
        help=(
            "Migrate ALL top-level tabs from the Acceldata docs site: Documentation, "
            "API Reference, and Release Notes. Each tab becomes a top-level section in "
            "AccelDocs with its own nested hierarchy. Without this flag, only the single "
            "tab specified by --source is migrated."
        ),
    )
    # --- Multi-product flags ---
    p.add_argument(
        "--all-products",
        action="store_true",
        help=(
            "Migrate ALL known products (ADOC, Pulse, ODP). Each product becomes a "
            "root section with its own tabs and version hierarchy underneath."
        ),
    )
    p.add_argument(
        "--product",
        type=str,
        default=None,
        help=(
            "Migrate a single product by slug (e.g. adoc, pulse, odp). "
            "Default: adoc. Use --all-products to migrate everything."
        ),
    )
    p.add_argument(
        "--all-versions",
        action="store_true",
        help="Migrate all known versions for each product (default: latest only).",
    )
    return p.parse_args()


def _discover_product_tree(
    product: ProductConfig,
    versions: list[ProductVersion],
    use_playwright: bool,
    use_category_map: bool,
    all_versions: bool = False,
) -> list[dict]:
    """Discover the full tree for one product (all tabs x versions).

    Returns a list of tree nodes. The product itself becomes a root node,
    with tab nodes underneath, each containing the discovered page hierarchy.

    When use_playwright=True and all_versions=True, auto-discovers versions
    from the version picker dropdown and iterates each one with version switching.
    """
    product_children: list[dict] = []

    # Playwright path: use a headless browser for sidebar discovery.
    # When all_versions=True, also auto-discover and switch between versions.
    # When all_versions=False, use Playwright for sidebar only (latest version).
    if use_playwright and _check_playwright():
        if all_versions:
            # Full version discovery + switching via dropdown
            return _discover_product_tree_playwright(product, use_category_map)
        else:
            # Playwright for sidebar only, latest version, no version switching
            return _discover_product_tree_playwright_latest(product, use_category_map)

    # Non-Playwright (sitemap) path: iterate configured versions x tabs.
    # NOTE: DeveloperHub versions are JS-only (no URL change), so sitemap
    # always returns the LATEST version's pages. When multiple versions are
    # configured we can only meaningfully migrate the latest via sitemap.
    if len(versions) > 1:
        log.warning(
            "Non-Playwright mode: DeveloperHub version switching requires JavaScript. "
            "Only the latest version's pages will be discovered via sitemap. "
            "Use --playwright --all-versions to migrate older versions."
        )
        versions = [v for v in versions if v.is_latest] or [versions[0]]

    for version in versions:
        version_children: list[dict] = []

        for tab in product.tabs:
            tab_url = product.base_url + tab.url_path

            log.info(
                "--- Discovering: %s / %s / %s (%s) ---",
                product.name, version.label, tab.name, tab_url,
            )

            tab_tree, _ = discover_structure(
                tab_url,
                use_playwright=False,  # explicitly static in this branch
                apply_category_map=use_category_map,
            )

            if tab_tree:
                total_tab_pages = len(_collect_all_urls(tab_tree))
                log.info(
                    "  %s > %s > %s: %d sections, %d pages",
                    product.name, version.label, tab.name,
                    len(tab_tree), total_tab_pages,
                )
                version_children.append({
                    "title": tab.name,
                    "url": None,
                    "depth": 1,
                    "children": tab_tree,
                    "_section_type": "tab",
                })
            else:
                log.warning("  No pages found for %s > %s > %s", product.name, version.label, tab.name)

        # If only one version (latest), skip the version wrapper node
        if len(versions) == 1:
            product_children.extend(version_children)
        else:
            if version_children:
                product_children.append({
                    "title": version.label,
                    "url": None,
                    "depth": 1,
                    "children": version_children,
                    "_section_type": "version",
                })

    return product_children


def _discover_product_tree_playwright_latest(
    product: ProductConfig,
    use_category_map: bool,
) -> list[dict]:
    """Playwright-based product discovery for the LATEST version only.

    Uses Playwright for rendering the JS sidebar (deep nesting) but does NOT
    iterate versions. This is used when --playwright is set without --all-versions.
    """
    from playwright.sync_api import sync_playwright

    product_children: list[dict] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        )
        page_obj = ctx.new_page()

        # Discover tabs (use configured tabs as starting point)
        tabs_to_process = [
            {"name": t.name, "url_path": t.url_path}
            for t in product.tabs
        ]

        # Navigate to first tab and try to discover tabs dynamically
        first_tab_url = product.base_url + product.tabs[0].url_path if product.tabs else product.base_url
        log.info("Playwright (latest): navigating to %s", first_tab_url)
        page_obj.goto(first_tab_url, wait_until="networkidle", timeout=30000)
        page_obj.wait_for_timeout(2000)

        discovered_tabs = _pw_discover_tabs(page_obj)
        if discovered_tabs:
            tabs_to_process = discovered_tabs

        for tab_info in tabs_to_process:
            tab_name = tab_info["name"]
            tab_path = tab_info.get("url_path", "")
            tab_url = product.base_url + tab_path if tab_path.startswith("/") else tab_path

            log.info("  Tab: %s (%s)", tab_name, tab_url)

            if tab_url != page_obj.url:
                try:
                    page_obj.goto(tab_url, wait_until="networkidle", timeout=30000)
                    page_obj.wait_for_timeout(2000)
                except Exception as exc:
                    log.warning("  Could not navigate to tab %s: %s", tab_name, exc)
                    continue

            tab_tree = _pw_extract_nav_tree(page_obj, tab_url)

            if tab_tree:
                total_pages = len(_collect_all_urls(tab_tree))
                log.info("  %s > %s: %d sections, %d pages", product.name, tab_name, len(tab_tree), total_pages)
                product_children.append({
                    "title": tab_name,
                    "url": None,
                    "depth": 1,
                    "children": tab_tree,
                    "_section_type": "tab",
                })
            else:
                # Sitemap fallback for this tab
                log.info("  Playwright sidebar empty for %s — trying sitemap", tab_name)
                parsed_tab = urlparse(tab_url)
                base = urlunparse((parsed_tab.scheme, parsed_tab.netloc, "", "", "", ""))
                sitemap_urls = _fetch_sitemap_urls(base, parsed_tab.path.rstrip("/") + "/")
                if sitemap_urls:
                    tree_from_sitemap = _sitemap_urls_to_tree(sitemap_urls, apply_category_map=use_category_map)
                    total_pages = len(_collect_all_urls(tree_from_sitemap))
                    log.info("  %s > %s (sitemap): %d pages", product.name, tab_name, total_pages)
                    product_children.append({
                        "title": tab_name,
                        "url": None,
                        "depth": 1,
                        "children": tree_from_sitemap,
                        "_section_type": "tab",
                    })

        browser.close()

    return product_children


def _discover_product_tree_playwright(
    product: ProductConfig,
    use_category_map: bool,
) -> list[dict]:
    """Playwright-based product discovery with live version/tab switching.

    Launches a single browser, navigates to the product's first tab, discovers
    available versions from the version picker dropdown, then for each version:
    1. Clicks the version in the dropdown
    2. Discovers tabs from the section picker (tabs may differ per version)
    3. For each tab, extracts the sidebar navigation tree

    Returns the product children list (version > tab > section hierarchy).
    """
    from playwright.sync_api import sync_playwright

    product_children: list[dict] = []

    first_tab_url = product.base_url + product.tabs[0].url_path if product.tabs else product.base_url

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        )
        page_obj = ctx.new_page()

        log.info("Playwright: navigating to %s for version discovery", first_tab_url)
        page_obj.goto(first_tab_url, wait_until="networkidle", timeout=30000)
        page_obj.wait_for_timeout(2000)

        # Discover versions from the dropdown
        discovered_versions = _pw_discover_versions(page_obj)
        if not discovered_versions:
            # No version picker — treat as single-version product
            discovered_versions = [{"label": "latest", "is_latest": True}]

        for ver_idx, ver in enumerate(discovered_versions):
            ver_label = ver["label"]
            is_latest = ver.get("is_latest", False)
            log.info("=== Version %d/%d: %s ===", ver_idx + 1, len(discovered_versions), ver_label)

            # Switch to this version (skip for the first/latest — already there)
            if ver_idx > 0:
                # Navigate back to the first tab's base URL first
                page_obj.goto(first_tab_url, wait_until="networkidle", timeout=30000)
                page_obj.wait_for_timeout(2000)

                if not _pw_switch_version(page_obj, ver_label):
                    log.warning("Could not switch to version %s — skipping", ver_label)
                    continue

            # After version switch, discover what tabs are available
            # (tabs may differ between versions)
            current_url = page_obj.url
            log.info("Version %s active at: %s", ver_label, current_url)

            # Discover tabs from the section picker for this version
            discovered_tabs = _pw_discover_tabs(page_obj)
            if not discovered_tabs:
                # Use configured tabs as fallback
                discovered_tabs = [
                    {"name": t.name, "url_path": t.url_path}
                    for t in product.tabs
                ]

            version_children: list[dict] = []

            for tab_info in discovered_tabs:
                tab_name = tab_info["name"]
                tab_path = tab_info.get("url_path", "")

                # Navigate to the tab
                if tab_path.startswith("http"):
                    tab_url = tab_path
                elif tab_path.startswith("/"):
                    tab_url = product.base_url + tab_path
                else:
                    tab_url = current_url  # already on this tab

                log.info("  Tab: %s (%s)", tab_name, tab_url)

                if tab_url != page_obj.url:
                    try:
                        page_obj.goto(tab_url, wait_until="networkidle", timeout=30000)
                        page_obj.wait_for_timeout(2000)
                    except Exception as exc:
                        log.warning("  Could not navigate to tab %s: %s", tab_name, exc)
                        continue

                # Extract sidebar tree from current page
                tab_tree = _pw_extract_nav_tree(page_obj, tab_url)

                if tab_tree:
                    total_tab_pages = len(_collect_all_urls(tab_tree))
                    log.info(
                        "  %s > %s > %s: %d sections, %d pages",
                        product.name, ver_label, tab_name,
                        len(tab_tree), total_tab_pages,
                    )
                    version_children.append({
                        "title": tab_name,
                        "url": None,
                        "depth": 1,
                        "children": tab_tree,
                        "_section_type": "tab",
                    })
                else:
                    # Fallback to sitemap for this tab
                    log.info("  Playwright sidebar empty for %s — trying sitemap", tab_name)
                    parsed_tab = urlparse(tab_url)
                    base = urlunparse((parsed_tab.scheme, parsed_tab.netloc, "", "", "", ""))
                    sitemap_urls = _fetch_sitemap_urls(base, parsed_tab.path.rstrip("/") + "/")
                    if sitemap_urls:
                        tree_from_sitemap = _sitemap_urls_to_tree(
                            sitemap_urls, apply_category_map=use_category_map,
                        )
                        total_pages = len(_collect_all_urls(tree_from_sitemap))
                        log.info(
                            "  %s > %s > %s (sitemap): %d sections, %d pages",
                            product.name, ver_label, tab_name,
                            len(tree_from_sitemap), total_pages,
                        )
                        version_children.append({
                            "title": tab_name,
                            "url": None,
                            "depth": 1,
                            "children": tree_from_sitemap,
                            "_section_type": "tab",
                        })

            if version_children:
                if len(discovered_versions) == 1:
                    product_children.extend(version_children)
                else:
                    product_children.append({
                        "title": ver_label,
                        "url": None,
                        "depth": 1,
                        "children": version_children,
                        "_section_type": "version",
                    })

        browser.close()

    return product_children


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

    use_playwright = getattr(args, "playwright", False)
    use_category_map = not args.no_category_map
    use_all_tabs = getattr(args, "all_tabs", False)
    use_all_products = getattr(args, "all_products", False)
    use_all_versions = getattr(args, "all_versions", False)
    selected_product = getattr(args, "product", None)

    # Determine which products to process
    products = get_products_to_migrate(selected_product, use_all_products)

    log.info("=== DeveloperHub → AccelDocs Migration ===")
    log.info("Products: %s", ", ".join(p.name for p in products))
    log.info("All versions: %s", use_all_versions)
    log.info("Backend: %s", args.backend)
    log.info("Dry run: %s", args.dry_run)
    log.info("Playwright: %s", use_playwright)
    log.info("Create Drive docs: %s", getattr(args, "create_drive_docs", False))

    # Load or start fresh state
    state: dict = {}
    if args.resume:
        state = load_state()
        if state:
            log.info("Loaded existing state from %s", STATE_FILE)

    # -----------------------------------------------------------------------
    # Step 1: Discover structure — multi-product aware
    # -----------------------------------------------------------------------
    # New multi-product path: --all-products or --product <slug>
    if use_all_products or selected_product:
        tree = []
        for product in products:
            versions = get_versions_to_migrate(product, use_all_versions)
            log.info(
                "Product '%s': %d tabs, %d versions to migrate",
                product.name, len(product.tabs), len(versions),
            )

            product_children = _discover_product_tree(
                product, versions, use_playwright, use_category_map,
                all_versions=use_all_versions,
            )

            if product_children:
                # Wrap under a product root node
                tree.append({
                    "title": product.name,
                    "url": None,
                    "depth": 0,
                    "children": product_children,
                    "_section_type": "section",
                })

        state["tree"] = tree
        state["source"] = args.source
        state["products"] = [p.slug for p in products]
        state["discovered_at"] = datetime.now(timezone.utc).isoformat()
        save_state(state)

    # Legacy single-product path: --all-tabs or plain --source
    elif state.get("tree") and not use_playwright and not use_all_tabs:
        tree = state["tree"]
        fallback_links: list[str] = state.get("fallback_links", [])
        log.info("Using cached tree from state (%d top-level nodes)", len(tree))
        all_urls_cached = _collect_all_urls(tree)
        has_children_cached = any(n.get("children") for n in tree)
        if not has_children_cached and len(all_urls_cached) > 10 and use_category_map:
            log.info("Cached tree is flat — applying category hierarchy now")
            tree = _apply_category_hierarchy(tree)
            state["tree"] = tree
            save_state(state)
    else:
        tree = []
        fallback_links = []

        if use_all_tabs:
            sources = [
                {"name": tab["name"], "url": tab["url"]}
                for tab in ACCELDATA_TABS
            ]
            log.info(
                "All-tabs mode: will process %d tabs: %s",
                len(sources),
                ", ".join(s["name"] for s in sources),
            )
        else:
            sources = [{"name": None, "url": args.source}]

        for source in sources:
            tab_name = source["name"]
            tab_url = source["url"]
            log.info("--- Discovering: %s (%s) ---", tab_name or "source", tab_url)

            tab_tree, tab_links = discover_structure(
                tab_url,
                use_playwright=use_playwright,
                apply_category_map=use_category_map,
            )

            if use_all_tabs and tab_name and tab_tree:
                total_tab_pages = len(_collect_all_urls(tab_tree))
                log.info(
                    "Tab '%s': %d sections, %d pages",
                    tab_name,
                    len(tab_tree),
                    total_tab_pages,
                )
                tree.append({
                    "title": tab_name,
                    "url": None,
                    "depth": 0,
                    "children": tab_tree,
                })
            else:
                tree.extend(tab_tree)
            fallback_links.extend(tab_links)

        state["tree"] = tree
        state["fallback_links"] = list(dict.fromkeys(fallback_links))
        state["source"] = args.source
        state["all_tabs"] = use_all_tabs
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
        print(f"DRY RUN — Products: {', '.join(p.name for p in products)}")
        print(f"{'=' * 60}\n")
        print(f"Discovered {total_pages} page URLs across {len(products)} product(s)")
        print("\nHierarchy:")
        print("-" * 40)
        _print_tree(tree)
        print(f"\n{'=' * 60}")
        print(f"Total unique page URLs: {total_pages}")

        # Per-product summary
        if len(products) > 1 or use_all_products or selected_product:
            print("\nProducts:")
            for node in tree:
                if not node.get("url") and node.get("children"):
                    prod_pages = len(_collect_all_urls([node]))
                    tab_count = len([c for c in node["children"] if not c.get("url")])
                    print(f"  {node['title']}: {prod_pages} pages, {tab_count} tabs")
                    for child in node.get("children", []):
                        if not child.get("url") and child.get("children"):
                            child_pages = len(_collect_all_urls([child]))
                            print(f"    {child['title']}: {child_pages} pages")
        elif use_all_tabs:
            print("\nTabs discovered:")
            for node in tree:
                if not node.get("url") and node.get("children"):
                    tab_pages = len(_collect_all_urls([node]))
                    print(f"  {node['title']}: {tab_pages} pages, {len(node['children'])} sub-sections")

        print(f"\nRecommended: use --playwright for the full multi-level hierarchy:")
        print(f"  pip install playwright && playwright install chromium")
        import_cmd = (
            f"\n  python scripts/migrate_developerhub.py \\\n"
            f"    --source {args.source} \\\n"
            f"    --backend {args.backend} \\\n"
            f"    --token <YOUR_TOKEN> \\\n"
            f"    --org-id <YOUR_ORG_ID> \\\n"
            f"    --product-id <YOUR_PRODUCT_ID> \\\n"
            f"    --playwright"
        )
        if not (use_all_products or selected_product):
            import_cmd += " \\\n    --all-products  # migrates ADOC + Pulse + ODP"
        print(import_cmd)
        print(f"\nTo also create Google Drive docs add:  --create-drive-docs")
        return

    # -----------------------------------------------------------------------
    # Step 2+3: Fetch and convert pages
    # -----------------------------------------------------------------------
    page_data: dict[str, dict] = dict(state.get("page_data", {}))
    already_fetched = set(page_data.keys())

    urls_to_fetch = [u for u in all_page_urls if u not in already_fetched]
    log.info("Pages to fetch: %d (already cached: %d)", len(urls_to_fetch), len(already_fetched))

    if use_playwright and _check_playwright() and urls_to_fetch:
        log.info("Using Playwright browser for page content fetching…")
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                context = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    )
                )
                for idx, url in enumerate(urls_to_fetch, 1):
                    log.info("[%d/%d] Fetching (Playwright): %s", idx, len(urls_to_fetch), url)
                    result = fetch_and_convert_page(url, pw_browser=context)
                    if result:
                        page_data[url] = result
                        state["page_data"] = page_data
                        if idx % 10 == 0:
                            save_state(state)
                    time.sleep(_REQUEST_DELAY)
                browser.close()
        except Exception as exc:
            log.warning("Playwright page fetch failed: %s — falling back to static HTTP", exc)
            for idx, url in enumerate(urls_to_fetch, 1):
                if url in page_data:
                    continue
                log.info("[%d/%d] Fetching (static): %s", idx, len(urls_to_fetch), url)
                result = fetch_and_convert_page(url)
                if result:
                    page_data[url] = result
                    state["page_data"] = page_data
                    if idx % 10 == 0:
                        save_state(state)
    else:
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
        if data.get("raw_html"):
            data["raw_html"] = rewrite_html_internal_links(
                data["raw_html"], source_domain, slug_map
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
    # Step 5b: Resolve internal links
    # Replace [[MIGRATED:slug]] placeholders with actual /pages/{id} URLs.
    # We couldn't do this during import because page IDs weren't known yet.
    # ------------------------------------------------------------------------
    _resolve_and_patch_links(client, old_url_to_page_id, page_data, log)

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------
    imported = sum(1 for pid in old_url_to_page_id.values() if pid and pid > 0)
    skipped = total_pages - imported

    print("\n" + "=" * 60)
    print("Migration Complete")
    print("=" * 60)
    print(f"  Products migrated:      {', '.join(p.name for p in products)}")
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
