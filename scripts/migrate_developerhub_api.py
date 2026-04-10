#!/usr/bin/env python3
"""
Fast migration using DeveloperHub (Mintlify) REST API instead of Playwright scraping.
Much faster and more reliable.

Usage:
    python scripts/migrate_developerhub_api.py \
        --api-key YOUR_MINTLIFY_API_KEY \
        --domain docs.acceldata.io \
        --product pulse \
        --backend https://acceldocs-backend.vercel.app \
        --token YOUR_ACCELDOCS_TOKEN \
        --org-id 3 \
        --product-id YOUR_PRODUCT_ID
"""

import argparse
import json
import logging
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

try:
    import requests
except ImportError:
    print("ERROR: 'requests' is required. Run: pip install requests", file=sys.stderr)
    sys.exit(1)

LOG_FILE = Path("migration_api.log")


def _setup_logging() -> logging.Logger:
    logger = logging.getLogger("migrate_api")
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


class MintlifyAPIClient:
    """Client for Mintlify/DeveloperHub REST API."""

    BASE_URL = "https://api.mintlify.com/discovery"

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        })

    def search(self, domain: str, query: str, page_size: int = 50) -> list[dict]:
        """Search documentation pages."""
        url = f"{self.BASE_URL}/v1/search/{domain}"
        try:
            resp = self.session.post(url, json={"query": query, "pageSize": page_size}, timeout=30)
            if resp.status_code == 200:
                return resp.json()
            log.warning("Search failed: %s - %s", resp.status_code, resp.text[:200])
            return []
        except Exception as exc:
            log.warning("Search error: %s", exc)
            return []

    def get_page_content(self, domain: str, path: str) -> str | None:
        """Get full content of a documentation page."""
        url = f"{self.BASE_URL}/v1/page/{domain}"
        try:
            resp = self.session.post(url, json={"path": path}, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("content", "")
            log.warning("Get page failed for %s: %s", path, resp.status_code)
            return None
        except Exception as exc:
            log.warning("Get page error for %s: %s", path, exc)
            return None


def fetch_sitemap(base_url: str) -> list[str]:
    """Fetch all URLs from sitemap.xml."""
    sitemap_url = f"{base_url}/sitemap.xml"
    try:
        resp = requests.get(sitemap_url, timeout=30)
        if resp.status_code == 200:
            urls = re.findall(r"<loc>([^<]+)</loc>", resp.text)
            log.info("Sitemap: found %d URLs", len(urls))
            return urls
    except Exception as exc:
        log.warning("Sitemap fetch failed: %s", exc)
    return []


def slug_from_path(path: str) -> str:
    """Extract slug from URL path."""
    path = path.rstrip("/")
    if "/" in path:
        slug = path.rsplit("/", 1)[-1]
    else:
        slug = path
    return slug


def title_from_slug(slug: str) -> str:
    """Convert slug to title."""
    title = slug.replace("-", " ").replace("_", " ")
    title = " ".join(word.capitalize() for word in title.split())
    return title


def convert_to_markdown(html_content: str) -> str:
    """Convert HTML to Markdown using pandoc or basic regex."""
    try:
        result = subprocess.run(
            ["pandoc", "-f", "html", "-t", "markdown", "--wrap=none"],
            input=html_content,
            capture_output=True,
            text=True,
            timeout=60
        )
        if result.returncode == 0:
            return result.stdout
    except Exception:
        pass

    content = html_content

    content = re.sub(r"<br\s*/?>", "\n", content, flags=re.IGNORECASE)
    content = re.sub(r"</p>", "\n\n", content, flags=re.IGNORECASE)
    content = re.sub(r"</div>", "\n", content, flags=re.IGNORECASE)
    content = re.sub(r"</h[1-6]>", "\n\n", content, flags=re.IGNORECASE)
    content = re.sub(r"<li>", "- ", content, flags=re.IGNORECASE)
    content = re.sub(r"</li>", "\n", content, flags=re.IGNORECASE)
    content = re.sub(r"<[^>]+>", "", content)
    content = re.sub(r"\n{3,}", "\n\n", content)
    import html
    content = html.unescape(content)
    return content.strip()


def import_pages_to_acceldocs(
    backend_url: str,
    token: str,
    org_id: int,
    product_id: int,
    pages: list[dict]
) -> tuple[int, int]:
    """Import pages to AccelDocs."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    success = 0
    failed = 0

    for page in pages:
        url = f"{backend_url}/api/pages/import"
        payload = {
            "organization_id": org_id,
            "section_id": product_id,
            "title": page["title"],
            "slug": page["slug"],
            "content": page["content"],
            "visibility": "public"
        }

        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=30)
            if resp.status_code in (200, 201):
                success += 1
                log.info("  Imported: %s", page["title"])
            else:
                failed += 1
                log.warning("  Failed (%d): %s - %s", resp.status_code, page["title"], resp.text[:200])
        except Exception as exc:
            failed += 1
            log.warning("  Error: %s - %s", page["title"], exc)

        time.sleep(0.5)

    return success, failed


def discover_pages_from_sitemap(base_url: str, product_prefix: str) -> list[dict]:
    """Discover pages from sitemap for a specific product."""
    all_urls = fetch_sitemap(base_url)
    pages = []

    for url in all_urls:
        if not url.startswith(f"{base_url}/{product_prefix}/"):
            continue
        if url.rstrip("/").endswith(f"{base_url}/{product_prefix}"):
            continue

        path = url.replace(base_url, "")
        slug = slug_from_path(path)
        title = title_from_slug(slug)

        pages.append({
            "url": url,
            "path": path.lstrip("/"),
            "slug": slug,
            "title": title
        })

    log.info("Discovered %d pages for %s", len(pages), product_prefix)
    return pages


def main():
    parser = argparse.ArgumentParser(description="Fast migration using Mintlify REST API")
    parser.add_argument("--api-key", required=True, help="Mintlify/DeveloperHub API key")
    parser.add_argument("--domain", default="docs.acceldata.io", help="Mintlify domain")
    parser.add_argument("--product", required=True, help="Product slug (pulse, adoc, odp)")
    parser.add_argument("--base-url", default="https://docs.acceldata.io", help="Base docs URL")
    parser.add_argument("--backend", default="https://acceldocs-backend.vercel.app")
    parser.add_argument("--token", required=True, help="AccelDocs JWT token")
    parser.add_argument("--org-id", type=int, required=True)
    parser.add_argument("--product-id", type=int, required=True)
    parser.add_argument("--max-pages", type=int, default=0, help="Max pages (0=all)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    log.info("=== Fast Migration via Mintlify API ===")
    log.info("Product: %s", args.product)

    client = MintlifyAPIClient(args.api_key)

    pages = discover_pages_from_sitemap(args.base_url, args.product)
    if args.max_pages > 0:
        pages = pages[:args.max_pages]

    log.info("Fetching content for %d pages...", len(pages))

    for i, page in enumerate(pages):
        log.info("[%d/%d] Fetching: %s", i + 1, len(pages), page["path"])

        content = client.get_page_content(args.domain, page["path"])
        if content:
            page["content"] = convert_to_markdown(content)
        else:
            page["content"] = f"# {page['title']}\n\n*Content unavailable*"

        time.sleep(0.3)

    if args.dry_run:
        log.info("\n=== DRY RUN - Would import %d pages ===", len(pages))
        for p in pages[:10]:
            log.info("  - %s (%s)", p["title"], p["slug"])
        if len(pages) > 10:
            log.info("  ... and %d more", len(pages) - 10)
        return

    log.info("\nImporting %d pages to AccelDocs...", len(pages))
    success, failed = import_pages_to_acceldocs(
        args.backend,
        args.token,
        args.org_id,
        args.product_id,
        pages
    )

    log.info("\n=== Migration Complete ===")
    log.info("Success: %d, Failed: %d", success, failed)


if __name__ == "__main__":
    main()
