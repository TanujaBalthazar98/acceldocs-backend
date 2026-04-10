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

def _resolve_log_file() -> Path:
    """Return a writable log file path across local and serverless runtimes."""
    env_path = os.getenv("MIGRATION_LOG_FILE")
    candidates: list[Path] = []
    if env_path:
        candidates.append(Path(env_path))
    candidates.extend(
        [
            Path("/tmp/migration.log"),
            Path.cwd() / "migration.log",
        ]
    )

    for candidate in candidates:
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            with open(candidate, "a", encoding="utf-8"):
                pass
            return candidate
        except OSError:
            continue

    # Last-resort fallback (should still be writable on serverless)
    return Path("/tmp/migration.log")


LOG_FILE = _resolve_log_file()


def _setup_logging() -> logging.Logger:
    logger = logging.getLogger("migrate_developerhub")
    logger.setLevel(logging.DEBUG)
    if logger.handlers:
        return logger

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
# State persistence (per-product)
# ---------------------------------------------------------------------------

STATE_DIR = Path("migration_states")


def _get_state_file(product_slug: str) -> Path:
    """Get the state file path for a specific product."""
    STATE_DIR.mkdir(exist_ok=True)
    return STATE_DIR / f"migration_state_{product_slug}.json"


def load_state(product_slug: str = "") -> dict:
    """Load state for a specific product."""
    if product_slug:
        state_file = _get_state_file(product_slug)
    else:
        state_file = Path("migration_state.json")
    
    if state_file.exists():
        try:
            return json.loads(state_file.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("Could not load %s: %s — starting fresh", state_file, exc)
    return {}


def save_state(state: dict, product_slug: str = "") -> None:
    """Save state for a specific product."""
    if product_slug:
        state_file = _get_state_file(product_slug)
    else:
        state_file = Path("migration_state.json")
    
    state_file.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    log.debug("State saved to %s", state_file)


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
    max_versions: int = 0,
    version_idx: int | None = None,
) -> list[ProductVersion]:
    """Return the versions to migrate for a product.
    
    Args:
        all_versions: If True, process all versions
        max_versions: If > 0, limit to first N versions (for chunked processing)
        version_idx: If set, process specific version by index (0=first, 1=second, etc.)
    """
    if not product.versions:
        return [ProductVersion(label="latest", is_latest=True)]
    
    all_vers = list(product.versions)
    
    if version_idx is not None and 0 <= version_idx < len(all_vers):
        # Single version by index
        return [all_vers[version_idx]]
    elif all_versions and max_versions > 0:
        # Chunked: process N versions at a time
        return all_vers[:max_versions]
    elif all_versions:
        return all_vers
    # Default: latest only
    latest = [v for v in all_vers if v.is_latest]
    return latest if latest else [all_vers[0]]


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
    Reorganize a flat sitemap tree into a proper section hierarchy.

    Uses URL path structure first (multi-level), then falls back to ACCELDATA_CATEGORY_MAP
    for pages without path structure. Uncategorized pages go under "Other".

    Returns a new tree with section nodes containing page children.
    """
    root_nodes: list[dict] = []

    for node in flat_tree:
        url = node.get("url") or ""
        if not url:
            continue

        parsed = urlparse(url)
        full_path = parsed.path.rstrip("/")
        path_parts = [p for p in full_path.split("/") if p]

        section_parts: list[str]
        page_slug = path_parts[-1] if path_parts else ""

        # Use full path structure - each segment becomes a section level
        if len(path_parts) >= 2:
            section_parts = path_parts[:-1]
        elif page_slug:
            # Single level - try category map first
            category = _categorize_url(page_slug)
            if category:
                section_parts = [category]
            else:
                category = _categorize_url(full_path)
                section_parts = [category] if category else ["Other"]
        else:
            section_parts = ["Other"]

        # Build or find section nodes
        current = root_nodes
        depth = 0
        for idx, part in enumerate(section_parts):
            section_title = part if " " in part else _humanize_path_part(part)
            section_slug = _slugify(section_title)

            existing = None
            for n in current:
                if n.get("title") and _slugify(n["title"]) == section_slug and not n.get("url"):
                    existing = n
                    break

            if existing:
                if not isinstance(existing.get("children"), list):
                    existing["children"] = []
                current = existing["children"]
            else:
                new_section = {
                    "title": section_title,
                    "url": None,
                    "depth": idx,
                    "children": [],
                }
                current.append(new_section)
                current = new_section["children"]

            depth = idx + 1

        current.append({**node, "depth": depth})

    # Build ordered tree: category map order first, then "Other", then any remaining roots
    ordered_cats = list(ACCELDATA_CATEGORY_MAP.keys())
    tree: list[dict] = []

    root_by_slug = {_slugify(n.get("title") or ""): n for n in root_nodes}

    for cat in ordered_cats:
        if cat in root_by_slug:
            tree.append(root_by_slug.pop(cat))

    for n in root_nodes:
        title = n.get("title") or ""
        if _slugify(title) not in [slug for slug, node in root_by_slug.items() if node in tree]:
            if n not in tree:
                tree.append(n)

    for slug, n in root_by_slug.items():
        if n not in tree:
            tree.append(n)

    page_count = len(tree)
    for n in tree:
        page_count += _count_descendant_pages(n)
    log.info(
        "Category hierarchy built: %d top-level sections, %d total pages",
        len(tree),
        page_count - len(tree),
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

        # If no title from sidebar link, try URL slug
        if not title and url:
            from urllib.parse import urlparse, unquote
            parsed = urlparse(url)
            slug = parsed.path.rstrip("/").rsplit("/", 1)[-1]
            slug = unquote(slug)
            title = slug.replace("-", " ").replace("_", " ").strip()
            title = " ".join(word.capitalize() for word in title.split() if word) or "Untitled"
        elif not title:
            title = "Untitled"

        node: dict[str, Any] = {
            "title": title,
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


def _build_hierarchy_from_sidebar_crawl(
    base_url: str,
    page_urls: list[str],
    max_pages: int = 0,
) -> list[dict]:
    """
    Build proper hierarchy by crawling each page's sidebar.
    
    This is slow (one browser visit per page) but gives accurate
    sidebar hierarchy that can't be derived from flat sitemap URLs.
    
    For each page, navigates to it and extracts what section
    it's under in the sidebar - this reveals parent-child
    relationships that sitemap doesn't contain.
    """
    from playwright.sync_api import sync_playwright
    
    tree: list[dict] = []
    url_to_node: dict[str, dict] = {}
    
    log.info("Crawling sidebar for %d pages (this may take a while...)", len(page_urls))
    
    if max_pages and len(page_urls) > max_pages:
        log.warning(f"Limiting crawl to {max_pages} pages")
        page_urls = page_urls[:max_pages]
    
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()
        
        processed_sections: set[str] = set()
        
        for idx, url in enumerate(page_urls):
            if idx % 20 == 0:
                log.info(f"Crawl progress: {idx}/{len(page_urls)}")
            
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(1500)
                
                # Extract sidebar tree from this page
                nav_tree = _pw_extract_nav_tree(page, base_url)
                
                # Process this page's sidebar context
                current_path = []
                
                # Find which section(s) this page belongs to by walking the tree
                def find_path_to_url(nodes, path: list):
                    for node in nodes:
                        title = node.get("title", "")
                        url = node.get("url", "")
                        children = node.get("children", [])
                        
                        if url and url in page.url:
                            return path + [title]
                        
                        if children:
                            result = find_path_to_url(children, path + [title])
                            if result:
                                return result
                    return None
                
                if nav_tree:
                    path = find_path_to_url(nav_tree, [])
                    if path:
                        # Add this page under its sidebar section
                        # Build tree from path
                        current = tree
                        for section_title in path:
                            # Find or create section
                            found = None
                            for n in current:
                                if n.get("title") == section_title and not n.get("url"):
                                    found = n
                                    break
                            
                            if not found:
                                found = {
                                    "title": section_title,
                                    "url": None,
                                    "depth": len(current) + 1,
                                    "children": [],
                                }
                                current.append(found)
                            
                            current = found.get("children", [])
                        
                        # Now add the page itself
                        slug = page.url.split("/")[-1] if "/" in page.url else page.url
                        current.append({
                            "title": _humanize_path_part(slug),
                            "url": url,
                            "depth": len(current) + 1,
                            "children": [],
                        })
                        
            except Exception as e:
                log.warning(f"Failed to crawl {url}: {e}")
                continue
        
        browser.close()
    
    log.info("Sidebar crawl complete: %d top-level sections", len(tree))
    return tree


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


def _normalize_url_key(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") or "/"
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def _humanize_path_part(part: str) -> str:
    text = part.replace("-", " ").replace("_", " ").strip()
    if not text:
        return "Untitled"
    return " ".join(word.capitalize() for word in text.split())


def _count_descendant_pages(node: dict) -> int:
    count = 0
    children = node.get("children", [])
    if not isinstance(children, list):
        return 0
    for child in children:
        if child.get("url"):
            count += 1
        count += _count_descendant_pages(child)
    return count


def _collect_tree_url_set(tree: list[dict]) -> set[str]:
    out: set[str] = set()

    def _walk(nodes: list[dict]) -> None:
        for node in nodes:
            url = node.get("url")
            if isinstance(url, str) and url:
                out.add(_normalize_url_key(url))
            children = node.get("children")
            if isinstance(children, list) and children:
                _walk(children)

    _walk(tree)
    return out


def _tokenize_for_match(text: str) -> set[str]:
    tokens: set[str] = set()
    for raw in re.split(r"[^a-z0-9]+", text.lower()):
        if len(raw) < 3:
            continue
        tokens.add(raw)

        # Lightweight stemming so "users" ↔ "user" and
        # "management" ↔ "manage" can match in heuristic placement.
        stem = raw
        if stem.endswith("ies") and len(stem) > 4:
            stem = stem[:-3] + "y"
        elif stem.endswith("s") and len(stem) > 4:
            stem = stem[:-1]
        if stem.endswith("ing") and len(stem) > 5:
            stem = stem[:-3]
        elif stem.endswith("ment") and len(stem) > 6:
            stem = stem[:-4]
        elif stem.endswith("ed") and len(stem) > 4:
            stem = stem[:-2]

        if len(stem) >= 3:
            tokens.add(stem)
    return tokens


def _iter_section_candidates(nodes: list[dict], depth: int = 0) -> list[tuple[dict, int]]:
    out: list[tuple[dict, int]] = []
    for node in nodes:
        children = node.get("children")
        is_section_node = not node.get("url")
        is_landing_with_children = bool(node.get("url")) and isinstance(children, list) and bool(children)
        if is_section_node or is_landing_with_children:
            out.append((node, depth))
        if isinstance(children, list) and children:
            out.extend(_iter_section_candidates(children, depth + 1))
    return out


def _infer_section_for_flat_page(tab_tree: list[dict], page_slug: str) -> str | None:
    """
    Infer best section title for flat URL pages using simple lexical matching
    against existing sidebar section and child page titles.
    """
    page_tokens = _tokenize_for_match(page_slug.replace("-", " ").replace("_", " "))
    if not page_tokens:
        return None

    best_title: str | None = None
    best_score = 0

    for section, section_depth in _iter_section_candidates(tab_tree):
        # Treat node as a section candidate if it is a pure section node OR
        # a "section landing page" (has url + children).
        has_children = bool(section.get("children"))
        if section.get("url") and not has_children:
            continue
        section_title = str(section.get("title") or "").strip()
        if not section_title:
            continue

        # Score based on direct match with section title only (not child pages)
        score = len(page_tokens & _tokenize_for_match(section_title)) * 10

        # Prefer shallower candidates when scores tie.
        score -= section_depth

        if score > best_score:
            best_score = score
            best_title = section_title

    return best_title if best_score >= 4 else None


def _find_or_create_section_node(
    nodes: list[dict],
    title: str,
    depth: int,
) -> dict:
    title_slug = _slugify(title)
    for node in nodes:
        # Reuse both pure section nodes and section-landing pages that already
        # exist in the sidebar (url + children).
        has_children = bool(node.get("children"))
        if node.get("url") and not has_children:
            continue
        if _slugify(str(node.get("title") or "")) == title_slug:
            if not isinstance(node.get("children"), list):
                node["children"] = []
            return node

    created = {
        "title": title,
        "url": None,
        "depth": depth,
        "children": [],
    }
    nodes.append(created)
    return created


def _merge_sitemap_urls_into_tab_tree(
    *,
    tab_tree: list[dict],
    tab_url: str,
    sitemap_urls: list[str],
    skip_words: set[str] | None = None,
) -> int:
    """
    Merge sitemap pages missing from sidebar tree into the same tab hierarchy.

    Uses URL path segments under the tab path to create section/subsection nodes
    deterministically, preserving existing sidebar order while appending only
    missing pages.
    """
    if not sitemap_urls:
        return 0

    tab_path = urlparse(tab_url).path.rstrip("/")
    existing = _collect_tree_url_set(tab_tree)
    added = 0
    if skip_words is None:
        skip_words = set()

    for raw_url in sitemap_urls:
        normalized = _normalize_url_key(raw_url)
        if normalized in existing:
            continue

        parsed = urlparse(raw_url)
        full_path = parsed.path.rstrip("/")
        # Ensure we only merge URLs that belong to this tab path.
        if tab_path and not (full_path == tab_path or full_path.startswith(tab_path + "/")):
            continue

        relative = full_path[len(tab_path):].strip("/") if tab_path else full_path.strip("/")
        parts = [p for p in relative.split("/") if p]
        if not parts:
            continue

        section_parts = parts[:-1] if len(parts) > 1 else []
        page_slug = parts[-1]

        COMMON_PREFIXES = {
            "monitor": "Monitor",
            "visualize": "Visualize", 
            "configure": "Configure",
            "create": "Create",
            "deploy": "Deploy",
            "manage": "Manage",
            "analyze": "Analyze",
            "search": "Search",
            "troubleshoot": "Troubleshoot",
            "enable": "Enable",
            "perform": "Perform",
            "track": "Track",
            "check": "Check",
            "understand": "Understand",
            "set": "Set Up",
            "install": "Install",
            "upgrade": "Upgrade",
            "standalone": "Standalone",
            "multi": "Multi",
            "change": "Change",
            "limit": "Limit",
            "modify": "Modify",
            "update": "Update",
            "understand": "Understand",
            "release": "Release",
            "upgradefrom": "Upgrade From",
            "installand": "Install And",
            "deployingle": "Deploying",
            "single": "Single",
            "multiple": "Multiple",
            "cluster": "Cluster",
            "node": "Node",
            "service": "Service",
        }

        def _extract_all_prefixes(slug: str, max_depth: int = 4, skip_words: set[str] | None = None) -> list[str]:
            """Extract hyphenated parts as potential section names, skipping duplicates and common words.
            
            Each hyphen-separated part becomes a potential section level.
            Limits to max_depth to avoid excessively deep nesting.
            skip_words: words to filter out (like parent tab/version names to avoid duplicates)
            """
            if skip_words is None:
                skip_words = set()
            parts_list = slug.split("-")
            prefixes = []
            seen_lower: set[str] = set()
            for p in parts_list[:-1]:  # Skip the last part (page name itself)
                if len(prefixes) >= max_depth:
                    break
                p_lower = p.lower()
                if p_lower in seen_lower or p_lower in skip_words:
                    continue
                seen_lower.add(p_lower)
                if p_lower in COMMON_PREFIXES:
                    prefixes.append(COMMON_PREFIXES[p_lower])
                elif len(p) >= 3 and any(c.isupper() for c in p):
                    prefixes.append(_humanize_path_part(p))
                elif p_lower in ("to", "from", "and", "or", "on", "in", "of", "the", "for"):
                    continue
                elif len(p) > 2:
                    prefixes.append(_humanize_path_part(p))
            return prefixes

        # For flat URLs with no path structure, extract slug prefixes for sub-sections
        if not section_parts:
            # First try to match existing sidebar sections
            inferred_section = _infer_section_for_flat_page(tab_tree, page_slug)
            if inferred_section:
                # Page goes directly under inferred section - no extra sub-sections
                section_parts = [inferred_section]
            else:
                # Try category map
                mapped_category = _categorize_url(page_slug)
                if mapped_category:
                    section_parts = [mapped_category]
                else:
                    # Extract all slug prefixes for sub-sections
                    prefixes = _extract_all_prefixes(page_slug, skip_words=skip_words)
                    if prefixes:
                        section_parts = prefixes
                    else:
                        # Fallback: first prefix only
                        prefix = page_slug.split("-")[0] if page_slug else ""
                        if prefix and prefix.lower() in COMMON_PREFIXES:
                            section_parts = [COMMON_PREFIXES[prefix.lower()]]
                        elif prefix and len(prefix) > 3:
                            section_parts = [_humanize_path_part(prefix)]

        current_children = tab_tree
        current_depth = 1

        for part in section_parts:
            section_title = part if " " in part else _humanize_path_part(part)
            section_node = _find_or_create_section_node(current_children, section_title, current_depth)
            current_children = section_node["children"]
            current_depth += 1

        page_title = _humanize_path_part(page_slug)
        current_children.append({
            "title": page_title,
            "url": raw_url,
            "depth": current_depth,
            "children": [],
        })
        existing.add(normalized)
        added += 1

    return added


def _iter_tab_nodes(nodes: list[dict]) -> list[dict]:
    """Return every node marked as a tab anywhere in the tree."""
    out: list[dict] = []

    def _walk(items: list[dict]) -> None:
        for node in items:
            if node.get("_section_type") == "tab":
                out.append(node)
            children = node.get("children")
            if isinstance(children, list) and children:
                _walk(children)

    _walk(nodes)
    return out


# ---------------------------------------------------------------------------
# Playwright-based discovery — for JavaScript SPAs
# ---------------------------------------------------------------------------

_PLAYWRIGHT_AVAILABLE: bool | None = None


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
    """Discover versions from either modern or legacy DeveloperHub picker DOM."""
    versions: list[dict] = []

    # Modern picker
    try:
        locator = page_obj.locator(".version-picker-container").first
        if locator.count():
            locator.click(force=True)
            page_obj.wait_for_timeout(1000)
            items = page_obj.locator(".version-picker-container .dropdown-item").all()
            for idx, item in enumerate(items):
                text = item.inner_text().strip()
                if text:
                    versions.append({"label": text, "is_latest": idx == 0})
    except Exception:
        pass

    # Legacy picker fallback
    if not versions:
        try:
            page_obj.click("app-version-picker .top-picker", timeout=5000)
            page_obj.wait_for_timeout(1000)
            html = page_obj.content()
            soup = BeautifulSoup(html, "html.parser")
            picker = soup.find("app-version-picker")
            if picker:
                menu = picker.find("ul", class_="dropdown-menu")
                if menu:
                    for idx, li in enumerate(menu.find_all("li", role="menuitem")):
                        text = li.get_text(separator=" ", strip=True)
                        if text:
                            versions.append({"label": text, "is_latest": idx == 0})
        except Exception:
            pass

    # Deduplicate while preserving order
    deduped: list[dict] = []
    seen: set[str] = set()
    for ver in versions:
        label = str(ver.get("label") or "").strip()
        if not label:
            continue
        key = label.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(ver)

    if deduped:
        log.info("Discovered %d versions: %s", len(deduped), ", ".join(v["label"] for v in deduped))
    return deduped


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


def _pw_extract_nav_tree(page_obj: Any, base_url: str, max_depth: int = 3) -> list[dict]:
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


def _normalize_text(text: str) -> str:
    """Normalize text for comparison: lowercase and collapse whitespace."""
    import re
    return re.sub(r'\s+', ' ', text.strip().lower())

def _pw_extract_angular_deep(page_obj: Any, nav: Tag, base_url: str, max_depth: int = 3) -> list[dict]:
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
        # Use list comprehension for class_ attribute to avoid lambda issues
        all_divs = container.find_all("div", class_=True)
        all_tree = [d for d in all_divs if any("tree-node-level-" in c for c in d.get("class", []))]
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

    for sec_idx, sec_name in enumerate(section_names):
        if not sec_name:
            continue

        norm_sec_name = _normalize_text(sec_name)

        # Try to click the section to expand it
        try:
            # Find the section element by text and click it
            # Use JavaScript click to avoid stale element issues
            # Use case-insensitive normalized comparison
            clicked = page_obj.evaluate(f'''
                () => {{
                    const items = document.querySelectorAll(".category-container span");
                    for (const item of items) {{
                        if (item.innerText) {{
                            const itemNorm = item.innerText.replace(/\\s+/g, ' ').trim().toLowerCase();
                            if (itemNorm === "{norm_sec_name}") {{
                                item.click();
                                return true;
                            }}
                        }}
                    }}
                    return false;
                }}
            ''')
            if clicked:
                page_obj.wait_for_timeout(2000)  # Wait for Angular accordion animation
                log.info("  Expanded section '%s'", sec_name)
            else:
                log.warning("  Could not find section '%s' to click", sec_name)
        except Exception as exc:
            log.warning("  Could not expand section '%s': %s", sec_name, exc)

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
        # Use list comprehension for class_ attribute to avoid lambda issues
        all_divs = container.find_all("div", class_=True)
        all_tree = [d for d in all_divs if any("tree-node-level-" in c for c in d.get("class", []))]
        
        # Find the section by name and extract its L2 children
        section_node: dict[str, Any] = {
            "title": sec_name,
            "url": None,
            "depth": 0,
            "_section_type": "section",
            "children": [],
        }
        l2_count = 0
        
        # Find section position using normalized comparison
        sec_start = None
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
                    if span_cat and _normalize_text(span_cat.get_text(strip=True)) == norm_sec_name:
                        sec_start = idx
                        break
        
        if sec_start is None:
            log.warning("  Could not find section '%s' in tree", sec_name)
            continue
            
        # Find the next section or end of tree
        sec_end = len(all_tree)
        for idx in range(sec_start + 1, len(all_tree)):
            node = all_tree[idx]
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
                        sec_end = idx
                        break
        
        # Extract L2 children (tree-node-level-2) between section start and end
        for node in all_tree[sec_start + 1 : sec_end]:
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
            
            # If no title from sidebar, try URL slug
            if not l2_title:
                from urllib.parse import urlparse, unquote
                parsed = urlparse(l2_url)
                slug = parsed.path.rstrip("/").rsplit("/", 1)[-1]
                slug = unquote(slug)
                l2_title = slug.replace("-", " ").replace("_", " ").strip()
                l2_title = " ".join(word.capitalize() for word in l2_title.split() if word) or "Untitled"

            has_l3 = "tree-node-collapsed" in classes_str
            l2_node: dict[str, Any] = {
                "title": l2_title,
                "url": l2_url,
                "depth": 1,
                "children": [],
            }

            if has_l3:
                if max_depth and max_depth <= 2:
                    pass  # Limited depth, skip L3
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

    return tree


def _pw_extract_l3_from_page(page_obj: Any, page_url: str) -> list[dict]:
    """
    Extract level-3+ sub-pages from the sidebar when viewing a page that has them.

    When navigating to a page like /pulse/user-guide/analyze-cluster-health-in-detail,
    the sidebar shows all section headers + the current page's section with its
    L2 and L3 children expanded. We find the current L2 page node and extract
    all L3/L4 siblings that follow it until the next section header.
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

        # Use list comprehension for class_ attribute to avoid lambda issues
        all_divs = container.find_all("div", class_=True)
        all_tree = [d for d in all_divs if any("tree-node-level-" in c for c in d.get("class", []))]

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

        def extract_children(start_idx: int, current_level: int = 2, max_level: int = 4) -> list[dict]:
            """Extract children at the next level down from the current page.
            
            Only extracts nodes that are:
            1. At a deeper level than the current page
            2. Directly indented under the current page in the tree
            """
            children: list[dict] = []
            i = start_idx
            next_level_down = current_level + 1
            
            while i < len(all_tree):
                node = all_tree[i]
                classes = node.get("class", [])
                classes_str = " ".join(classes) if isinstance(classes, list) else classes or ""

                # Stop at next section header (L1 category)
                if "tree-node-level-1" in classes_str and "category-container" in classes_str:
                    break

                level = 0
                for c in classes:
                    if "tree-node-level-" in c:
                        try:
                            level = int(c.split("tree-node-level-")[1].split()[0])
                        except (IndexError, ValueError):
                            pass

                # Only extract nodes at the next level down
                if level == next_level_down:
                    anchor = node.find("a", href=True)
                    if anchor:
                        href = anchor.get("href", "")
                        if not href.startswith("#") and not href.startswith("javascript"):
                            has_sub = "tree-node-collapsed" in classes_str
                            node_dict = {
                                "title": anchor.get_text(separator=" ", strip=True),
                                "url": urljoin("https://docs.acceldata.io", href),
                                "depth": level - 1,
                                "children": [],
                            }
                            # Only recursively extract if this is a collapsed node with sub-nodes
                            if has_sub and level < max_level:
                                sub_url = urljoin("https://docs.acceldata.io", href)
                                node_dict["children"] = _extract_sub_children(page_obj, sub_url, level + 1)
                            children.append(node_dict)
                elif level <= current_level:
                    # Reached a sibling or parent - stop
                    break
                    
                i += 1
            return children

        def _extract_sub_children(pg: Any, sub_url: str, sub_level: int) -> list[dict]:
            """Extract children at sub_level from a sub-page."""
            try:
                pg.goto(sub_url, wait_until="networkidle", timeout=15000)
                pg.wait_for_timeout(2000)
            except Exception:
                try:
                    pg.goto(sub_url, wait_until="domcontentloaded", timeout=15000)
                    pg.wait_for_timeout(2000)
                except Exception:
                    return []
            
            sub_soup = BeautifulSoup(pg.content(), "html.parser")
            sub_tnc = sub_soup.find("tree-node-collection")
            if not sub_tnc:
                return []
            
            sub_container = sub_tnc.find("div")
            if not sub_container:
                return []
            
            # Use list comprehension for class_ attribute to avoid lambda issues
            sub_divs = sub_container.find_all("div", class_=True)
            sub_all = [d for d in sub_divs if any("tree-node-level-" in c for c in d.get("class", []))]
            
            sub_children = []
            for node in sub_all:
                classes = node.get("class", [])
                classes_str = " ".join(classes) if isinstance(classes, list) else classes or ""
                
                if f"tree-node-level-{sub_level}" in classes_str:
                    anchor = node.find("a", href=True)
                    if anchor:
                        href = anchor.get("href", "")
                        if not href.startswith("#") and not href.startswith("javascript"):
                            sub_children.append({
                                "title": anchor.get_text(separator=" ", strip=True),
                                "url": urljoin("https://docs.acceldata.io", href),
                                "depth": sub_level - 1,
                                "children": [],
                            })
                elif f"tree-node-level-{sub_level - 1}" in classes_str and "category-container" in classes_str:
                    break
            
            return sub_children

        return extract_children(current_idx + 1, current_level=2, max_level=4)
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
    all_versions: bool = False,
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
        versions: list[dict] = []
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
                    # Process all versions if flag set, otherwise latest only
                    versions_to_process = versions if all_versions else versions[:1]
                    if all_versions:
                        log.info(
                            f"Discovered {len(versions)} versions, processing all",
                        )
                    else:
                        log.info(
                            "Discovered %d versions, extracting tabs/sections for latest only: %s",
                            len(versions),
                            versions_to_process[0].get("label"),
                        )
                    all_urls: list[str] = []
                    for ver_idx, ver in enumerate(versions_to_process):
                        ver_name = str(ver.get("label") or f"Version {ver_idx + 1}")
                        try:
                            if ver_idx > 0 and not _pw_switch_version(page_obj, ver_name):
                                log.warning("  Failed to switch to version '%s' — skipping", ver_name)
                                continue
                            page_obj.wait_for_timeout(1200)
                            ver_url = page_obj.url
                            log.info("  Version %d/%d: %s -> %s", ver_idx + 1, len(versions_to_process), ver_name, ver_url)

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
                                skip_words = {ver_name.lower(), tab["name"].lower()}
                                try:
                                    page_obj.goto(tab_url, wait_until="domcontentloaded", timeout=15000)
                                    page_obj.wait_for_timeout(1000)
                                    tab_tree = _pw_extract_nav_tree(page_obj, tab_url, max_depth=max_depth) or []
                                    # Add sidebar section names to skip_words
                                    for node in tab_tree:
                                        if node.get("title") and not node.get("url"):
                                            skip_words.add(node.get("title", "").lower())
                                    tab_urls = _collect_all_urls(tab_tree)

                                    tab_sitemap_urls = _fetch_sitemap_urls(base_url, tab["url_path"])
                                    skip_words = {ver_name.lower(), tab["name"].lower()}
                                    if tab_sitemap_urls:
                                        merged_count = _merge_sitemap_urls_into_tab_tree(
                                            tab_tree=tab_tree,
                                            tab_url=tab_url,
                                            sitemap_urls=tab_sitemap_urls,
                                            skip_words=skip_words,
                                        )
                                        if merged_count > 0:
                                            log.info(
                                                "    %s: supplemented hierarchy with %d sitemap pages",
                                                tab["name"],
                                                merged_count,
                                            )
                                        tab_urls = _collect_all_urls(tab_tree)
                                    elif not tab_tree:
                                        log.info("    No sidebar for %s — trying sitemap", tab["name"])
                                        sitemap_urls = _fetch_sitemap_urls(base_url, tab["url_path"])
                                        if sitemap_urls:
                                            tab_tree = []
                                            _merge_sitemap_urls_into_tab_tree(
                                                tab_tree=tab_tree,
                                                tab_url=tab_url,
                                                sitemap_urls=sitemap_urls,
                                                skip_words=skip_words,
                                            )
                                            tab_urls = _collect_all_urls(tab_tree)
                                            log.info("    %s (sitemap): %d pages", tab["name"], len(tab_urls))
                                    
                                    all_urls.extend(tab_urls)
                                    version_page_count += len(tab_urls)
                                    tab_node = {
                                        "title": tab["name"],
                                        "url": tab_url,
                                        "depth": 0,
                                        "_section_type": "tab",
                                        "children": tab_tree or [],
                                    }
                                    version_node["children"].append(tab_node)
                                    if tab_tree:
                                        log.info("    %s: %d pages", tab["name"], len(tab_urls))
                                    else:
                                        log.info("    %s: no sidebar or sitemap", tab["name"])
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
                                    # Always add tab, even if sidebar is empty
                                    tree.append({
                                        "title": tab["name"],
                                        "url": tab_url,
                                        "depth": 0,
                                        "_section_type": "tab",
                                        "children": tab_tree or [],
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

            if len(all_urls) < 500 and apply_category_map:
                log.info(
                    "Playwright sidebar has only %d URLs (collapsed sections) — "
                    "supplementing with sitemap, preserving Playwright section hierarchy",
                    len(all_urls),
                )
                merged_total = 0
                tab_nodes = _iter_tab_nodes(tree)

                if tab_nodes:
                    for tab_node in tab_nodes:
                        tab_url = str(tab_node.get("url") or "").strip()
                        if not tab_url:
                            continue
                        parsed_tab = urlparse(tab_url)
                        tab_base = urlunparse((parsed_tab.scheme, parsed_tab.netloc, "", "", "", ""))
                        tab_prefix = parsed_tab.path.rstrip("/") + "/"
                        tab_sitemap_urls = _fetch_sitemap_urls(tab_base, tab_prefix)
                        if not tab_sitemap_urls:
                            tab_sitemap_urls = _fetch_sitemap_urls(tab_base, parsed_tab.path.rstrip("/"))
                        if not tab_sitemap_urls:
                            continue
                        tab_title = tab_node.get("title", "").lower()
                        ver_node = next((p for p in tree if any(c.get("title", "").lower() == tab_title for c in p.get("children", []))), None)
                        ver_title = ver_node.get("title", "").lower() if ver_node else ""
                        skip_words = {ver_title, tab_title}
                        # Add sidebar section names to skip_words
                        for node in tab_node.get("children", []):
                            if node.get("title") and not node.get("url"):
                                skip_words.add(node.get("title", "").lower())
                        merged_total += _merge_sitemap_urls_into_tab_tree(
                            tab_tree=tab_node.get("children", []),
                            tab_url=tab_url,
                            sitemap_urls=tab_sitemap_urls,
                            skip_words=skip_words,
                        )
                else:
                    sitemap_urls = _fetch_sitemap_urls(base_url, source_path + "/")
                    if not sitemap_urls:
                        sitemap_urls = _fetch_sitemap_urls(base_url, source_path)
                    if sitemap_urls:
                        merged_total += _merge_sitemap_urls_into_tab_tree(
                            tab_tree=tree,
                            tab_url=source_url,
                            sitemap_urls=sitemap_urls,
                        )

                if merged_total > 0:
                    merged_urls = _collect_all_urls(tree)
                    log.info(
                        "Merged tree: %d top-level nodes, %d total pages (+%d)",
                        len(tree),
                        len(merged_urls),
                        merged_total,
                    )
                fallback_links = _collect_all_urls(tree) or all_urls
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

    # Remove Angular component tags (app-*)
    for tag in list(soup_elem.find_all(True)):
        if tag.name and tag.name.startswith("app-"):
            tag.decompose()
        elif tag.name in ("app-root",):
            tag.decompose()

    # Remove known DeveloperHub UI chrome divs and popup elements
    ui_classes_to_remove = [
        "sidebar", "nav-", "breadcrumb", "footer", "header-",
        "feedback", "was-this-helpful", "edit-page", "table-of-contents",
        "on-this-page", "page-nav", "pagination",
        # Popup/menu elements that are UI, not content
        "link-selector", "in-doc-menu", "context-popper", "context-popper-container",
        "glossary-popper", "glossary-popper-container", "glossary-popper-arrow",
        "items-container", "notification-", "cookie-consent",
        # HR-like elements
        "hr", "line",
        # Hidden elements
        "d-none",
    ]
    for div in soup_elem.find_all("div"):
        try:
            classes = " ".join(div.get("class", []) or []).lower()
        except (AttributeError, TypeError):
            continue
        if any(x in classes for x in ui_classes_to_remove):
            div.decompose()

    # Convert <div class="hr"> to <hr>
    for hr_div in soup_elem.find_all("div", class_="hr"):
        hr = soup_elem.new_tag("hr")
        hr_div.replace_with(hr)

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


def _convert_code_block_components(soup_elem: Tag) -> None:
    """
    Extract code blocks from DeveloperHub <app-code-block> Angular components.

    DeveloperHub renders code in <app-code-block> components with the actual
    code stored in a pluginobject JSON attribute as URL-encoded data.

    Example structure:
        <app-code-block pluginobject='{"data":{"languageBlocks":[{"code":"...","language":"bash"}]}}'>

    We extract the code and language, then create proper <pre><code class="language-*"> blocks.
    """
    import json as _json
    from urllib.parse import unquote as _unquote

    code_blocks = list(soup_elem.find_all("app-code-block"))
    if not code_blocks:
        return

    for block in code_blocks:
        plugin_obj = block.get("pluginobject", "")
        if not plugin_obj:
            block.decompose()
            continue

        try:
            # URL-decode the JSON first
            decoded_plugin = _unquote(plugin_obj)
            data = _json.loads(decoded_plugin)
            language_blocks = data.get("data", {}).get("languageBlocks", [])
            if not language_blocks:
                block.decompose()
                continue

            # Create container for code blocks
            container = BeautifulSoup("", "html.parser").new_tag("div")
            container["class"] = "code-blocks"

            for lang_block in language_blocks:
                code = lang_block.get("code", "")
                language = lang_block.get("language", "")

                if not code:
                    continue

                # Create <pre><code class="language-{lang}">{code}</code></pre>
                pre_tag = BeautifulSoup("", "html.parser").new_tag("pre")
                code_tag = BeautifulSoup("", "html.parser").new_tag("code")
                if language:
                    code_tag["class"] = f"language-{language}"
                code_tag.string = _unquote(code)
                pre_tag.append(code_tag)
                container.append(pre_tag)

            # Replace the Angular component with our clean code blocks
            block.replace_with(container)

        except Exception as exc:
            log.debug("Failed to convert code block: %s", exc)
            block.decompose()


def _convert_custom_html_components(soup_elem: Tag) -> None:
    """
    Extract HTML content from DeveloperHub <app-custom-html> Angular components.

    DeveloperHub uses <app-custom-html> components to embed arbitrary HTML content
    (e.g., compatibility matrices, custom styled sections). The HTML is stored in
    a pluginobject JSON attribute as URL-encoded/HTML-escaped data.

    Example:
        <app-custom-html pluginobject='{"data":{"contents":"<!DOCTYPE html>..."}}'>

    We extract the HTML, parse it, strip the wrapper tags (doctype, html, head, body)
    and insert the body content directly.
    """
    import json as _json
    import html as _html
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

            # Decode HTML entities
            decoded_html = _html.unescape(contents)

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

    # If no title found, try to extract from URL slug
    if not title:
        from urllib.parse import urlparse, unquote
        parsed = urlparse(url)
        slug = parsed.path.rstrip("/").rsplit("/", 1)[-1]
        slug = unquote(slug)
        title = slug.replace("-", " ").replace("_", " ").replace("/", " ").strip()
        # Capitalize words
        title = " ".join(word.capitalize() for word in title.split() if word)

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

    # 5. Extract code blocks from <app-code-block> components
    #    (these contain shell commands, config files, etc.)
    _convert_code_block_components(content_elem)

    # 6. Convert CodeMirror-rendered code lines to proper <pre><code> blocks
    _convert_codemirror_to_code_blocks(content_elem)

    # 5. Clean up the HTML: strip DeveloperHub-specific wrappers, classes, scripts
    _clean_developerhub_html(content_elem)

    content_html = str(content_elem)

    # Strip <body> and </body> tags if present (they shouldn't be in the content)
    if content_html.startswith("<body>"):
        content_html = content_html[6:]
    if content_html.endswith("</body>"):
        content_html = content_html[:-7]
    content_html = content_html.strip()

    # Strip embedded HTML documents (from app-custom-html components)
    # These appear as <!DOCTYPE html>...<body>...</body> or <html><head>... embedded in content
    import re
    # Remove everything from <!DOCTYPE to </body> (the embedded doc)
    content_html = re.sub(
        r'<![^>]*DOCTYPE[^>]*>.*?</body>',
        '',
        content_html,
        flags=re.DOTALL | re.IGNORECASE
    )
    # Remove embedded <html><head>...</html> documents
    content_html = re.sub(
        r'<html[^>]*>.*?</html>',
        '',
        content_html,
        flags=re.DOTALL | re.IGNORECASE
    )
    # Also remove standalone body tags
    content_html = re.sub(r'<body[^>]*>', '', content_html, flags=re.IGNORECASE)
    content_html = re.sub(r'</body>', '', content_html, flags=re.IGNORECASE)
    content_html = content_html.strip()

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
            time.sleep(0.35)  # Rate limiting: 200 requests/min = 0.3s per request
        except Exception as exc:
            log.error("Failed to import page '%s' (%s): %s", title, url, exc)
            time.sleep(1)  # Longer delay on error

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
    p.add_argument(
        "--tab",
        type=str,
        default=None,
        help=(
            "Migrate a specific tab only (e.g. 'User Guide', 'Installation Guide'). "
            "Use with --version-id to target an existing version. Use --resume to continue "
            "from where you left off."
        ),
    )
    p.add_argument(
        "--version-id",
        type=int,
        default=None,
        help=(
            "The AccelDocs section ID of an existing version to import under. "
            "If not provided, a new version section will be created. "
            "Use this to import tabs into an already-created version."
        ),
    )
    p.add_argument(
        "--max-versions",
        type=int,
        default=0,
        help=(
            "Process N versions at a time. Use with --all-versions to process in chunks. "
            "E.g., --max-versions 3 processes first 3 versions then stops. "
            "Run again to process next 3."
        ),
    )
    p.add_argument(
        "--deep",
        action="store_true",
        help=(
            "Extract full deep hierarchy (L3+ sub-pages). Without this, only L2 sections "
            "are extracted for speed. Use this to get complete sidebar structure."
        ),
    )
    p.add_argument(
        "--version-idx",
        type=int,
        default=None,
        help=(
            "Process a specific version by index: 0=first, 1=second, etc. "
            "E.g., --version-idx 0 for first version, --version-idx 1 for second. "
            "Use with --all-versions."
        ),
    )
    p.add_argument(
        "--one-version",
        action="store_true",
        help=(
            "Do only ONE version then stop. Use with --all-versions to process "
            "versions one at a time (prevents crashes). Run again to do next version."
        ),
    )
    return p.parse_args()


def _discover_product_tree(
    product: ProductConfig,
    versions: list[ProductVersion],
    use_playwright: bool,
    use_category_map: bool,
    all_versions: bool = False,
    state: dict | None = None,
    target_tab: str | None = None,
) -> list[dict]:
    """Discover the full tree for one product (all tabs x versions).

    Returns a list of tree nodes. The product itself becomes a root node,
    with tab nodes underneath, each containing the discovered page hierarchy.

    When use_playwright=True and all_versions=True, auto-discovers versions
    from the version picker dropdown and iterates each one with version switching.
    """
    product_children: list[dict] = []
    if state is None:
        state = {}

    # Playwright path: use a headless browser for sidebar discovery.
    # When all_versions=True, also auto-discover and switch between versions.
    # When all_versions=False, use Playwright for sidebar only (latest version).
    if use_playwright and _check_playwright():
        if all_versions:
            # Full version discovery + switching via dropdown
            return _discover_product_tree_playwright(product, use_category_map, state)
        else:
            # Playwright for sidebar only, latest version, no version switching
            return _discover_product_tree_playwright_latest(product, use_category_map, state)

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
    state: dict | None = None,
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
        page_obj.goto(first_tab_url, wait_until="networkidle", timeout=60000)
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
                    page_obj.goto(tab_url, wait_until="networkidle", timeout=60000)
                    page_obj.wait_for_timeout(2000)
                except Exception as exc:
                    log.warning("  Could not navigate to tab %s: %s", tab_name, exc)
                    continue

            tab_tree = _pw_extract_nav_tree(page_obj, tab_url) or []

            # Always supplement with sitemap to get ALL pages
            parsed_tab = urlparse(tab_url)
            base = urlunparse((parsed_tab.scheme, parsed_tab.netloc, "", "", "", ""))
            tab_path = parsed_tab.path.rstrip("/") + "/"
            sitemap_urls = _fetch_sitemap_urls(base, tab_path)

            if sitemap_urls:
                merged = _merge_sitemap_urls_into_tab_tree(
                    tab_tree=tab_tree,
                    tab_url=tab_url,
                    sitemap_urls=sitemap_urls,
                )
                if merged > 0:
                    log.info("  %s > %s: merged %d missing sitemap pages", product.name, tab_name, merged)

            total_pages = len(_collect_all_urls(tab_tree))
            if total_pages > 0:
                log.info("  %s > %s: %d sections, %d pages", product.name, tab_name, len(tab_tree), total_pages)
                product_children.append({
                    "title": tab_name,
                    "url": None,
                    "depth": 0,
                    "children": tab_tree,
                    "_section_type": "tab",
                })
            else:
                log.info("  %s > %s: no sidebar or sitemap pages found", product.name, tab_name)


        browser.close()

    return product_children


def _discover_product_tree_playwright(
    product: ProductConfig,
    use_category_map: bool,
    state: dict | None = None,
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
                        page_obj.goto(tab_url, wait_until="networkidle", timeout=60000)
                        page_obj.wait_for_timeout(2000)
                    except Exception as exc:
                        log.warning("  Could not navigate to tab %s: %s", tab_name, exc)
                        continue

                # Extract sidebar tree from current page
                tab_tree = _pw_extract_nav_tree(page_obj, tab_url) or []
                parsed_tab = urlparse(tab_url)
                base = urlunparse((parsed_tab.scheme, parsed_tab.netloc, "", "", "", ""))
                sitemap_urls = _fetch_sitemap_urls(base, parsed_tab.path.rstrip("/") + "/")
                if sitemap_urls:
                    merged = _merge_sitemap_urls_into_tab_tree(
                        tab_tree=tab_tree,
                        tab_url=tab_url,
                        sitemap_urls=sitemap_urls,
                    )
                    if merged > 0:
                        log.info("  %s > %s > %s: merged %d missing sitemap pages",
                                 product.name, ver_label, tab_name, merged)

                total_tab_pages = len(_collect_all_urls(tab_tree))
                if total_tab_pages:
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
                    log.info("  %s > %s > %s: no sidebar or sitemap pages found",
                             product.name, ver_label, tab_name)

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
    max_versions = getattr(args, "max_versions", 0) or 0
    version_idx = getattr(args, "version_idx", None)
    one_version = getattr(args, "one_version", False)
    selected_product = getattr(args, "product", None)
    use_deep = getattr(args, "deep", False)
    
    # If --deep is set, use max_depth=3 to extract L3+ pages
    max_depth = 3 if use_deep else 0
    
    # If --one-version, process exactly 1 version
    if one_version and use_all_versions:
        max_versions = 1

    # Determine which products to process
    products = get_products_to_migrate(selected_product, use_all_products)

    log.info("=== DeveloperHub → AccelDocs Migration ===")
    log.info("Products: %s", ", ".join(p.name for p in products))
    log.info("All versions: %s", use_all_versions)
    log.info("Backend: %s", args.backend)
    log.info("Dry run: %s", args.dry_run)
    log.info("Playwright: %s", use_playwright)
    log.info("Create Drive docs: %s", getattr(args, "create_drive_docs", False))

    # Per-tab migration support
    selected_tab = getattr(args, "tab", None)
    version_id = getattr(args, "version_id", None)
    if selected_tab:
        log.info("Tab: %s (version_id=%s)", selected_tab, version_id)

    # Determine product slug for state management
    # If migrating a specific tab, include tab name in state file for resume support
    if selected_tab and selected_product:
        product_slug = f"{selected_product}_{selected_tab.lower().replace(' ', '-')}"
    else:
        product_slug = selected_product if selected_product else (products[0].slug if products else "")

    # Load or start fresh state (per-product)
    state: dict = {}
    if args.resume:
        state = load_state(product_slug)
        if state:
            state_file = _get_state_file(product_slug)
            log.info("Loaded existing state from %s", state_file)
        else:
            log.info("No existing state found for '%s' — starting fresh", product_slug)

    # -----------------------------------------------------------------------
    # Step 1: Discover structure — multi-product aware
    # -----------------------------------------------------------------------
    # New multi-product path: --all-products or --product <slug>
    if use_all_products or selected_product:
        tree = []
        for product in products:
            versions = get_versions_to_migrate(product, use_all_versions, max_versions, version_idx)
            log.info(
                "Product '%s': %d tabs, %d versions to migrate",
                product.name, len(product.tabs), len(versions),
            )

            # Load per-product state for this product
            prod_state = load_state(product.slug)
            
            product_children = _discover_product_tree(
                product, versions, use_playwright, use_category_map,
                all_versions=use_all_versions,
                state=prod_state,
                target_tab=selected_tab,  # Filter to specific tab if specified
            )

            # If --tab was specified, filter product_children to only that tab
            if selected_tab:
                product_children = [
                    child for child in product_children
                    if child.get("title", "").lower() == selected_tab.lower()
                ]
                log.info("Filtered to tab '%s': %d tab(s) found", selected_tab, len(product_children))

            if product_children:
                # Determine version name from the first discovered version or use "Latest"
                version_name = product.name  # Default version name
                if len(versions) == 1:
                    ver = versions[0]
                    version_name = ver.label if hasattr(ver, 'label') and ver.label else product.name
                
                tree.append({
                    "title": version_name,
                    "url": None,
                    "depth": 0,
                    "_section_type": "version",
                    "children": product_children,
                })
            
            # Save per-product state
            prod_state["tree"] = tree if product == products[0] else prod_state.get("tree", [])
            prod_state["source"] = args.source
            prod_state["product"] = product.slug
            prod_state["discovered_at"] = datetime.now(timezone.utc).isoformat()
            if version_id:
                prod_state["version_id"] = version_id
            save_state(prod_state, product.slug)

        state["tree"] = tree
        state["source"] = args.source
        state["products"] = [p.slug for p in products]
        state["discovered_at"] = datetime.now(timezone.utc).isoformat()

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
                all_versions=use_all_versions,
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
        save_state(state, product_slug)

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

    # Use Playwright for content fetching if --playwright is set (Angular-rendered pages)
    if use_playwright and urls_to_fetch:
        log.info("Fetching page content using Playwright (JavaScript-rendered Angular SPA)…")
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            )
            for idx, url in enumerate(urls_to_fetch, 1):
                log.info("[%d/%d] Fetching: %s", idx, len(urls_to_fetch), url)
                result = fetch_and_convert_page(url, pw_browser=browser)
                if result:
                    page_data[url] = result
                    state["page_data"] = page_data
                    if idx % 10 == 0:
                        save_state(state, product_slug)
            browser.close()
    else:
        log.info("Fetching page content using static HTTP…")
        for idx, url in enumerate(urls_to_fetch, 1):
            log.info("[%d/%d] Fetching: %s", idx, len(urls_to_fetch), url)
            result = fetch_and_convert_page(url)
            if result:
                page_data[url] = result
                state["page_data"] = page_data
                if idx % 10 == 0:
                    save_state(state, product_slug)

    save_state(state, product_slug)
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

    # If version_id was provided, add it to state so import_hierarchy uses it
    # The version node_path follows the pattern: /{slugified_version_name}
    if version_id:
        if "section_map" not in state:
            state["section_map"] = {}
        # Determine version name from tree
        version_name = "pulse-41x"  # Default
        for node in tree:
            if node.get("_section_type") == "version":
                version_name = _slugify(node.get("title", ""))
                break
        version_node_path = f"/{version_name}"
        state["section_map"][version_node_path] = version_id
        log.info("Using existing version_id=%d (node_path=%s)", version_id, version_node_path)
    
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

    state_file_path = _get_state_file(product_slug) if product_slug else STATE_DIR / "migration_state.json"
    print("\n" + "=" * 60)
    print("Migration Complete")
    print("=" * 60)
    print(f"  Products migrated:      {', '.join(p.name for p in products)}")
    print(f"  Total pages discovered: {total_pages}")
    print(f"  Pages imported:         {imported}")
    print(f"  Pages skipped/failed:   {skipped}")
    print(f"  Drive docs created:     {getattr(args, 'create_drive_docs', False)}")
    print(f"  State saved to:         {state_file_path}")
    print(f"  Full log in:            {LOG_FILE}")
    print("=" * 60)

    log.info(
        "Migration finished: %d imported, %d skipped of %d total",
        imported,
        skipped,
        total_pages,
    )

    state["completed_at"] = datetime.now(timezone.utc).isoformat()
    save_state(state, product_slug)


if __name__ == "__main__":
    main()
