import asyncio
import json
import logging
import os
import time
from datetime import datetime
from typing import Dict, List, Optional, Any, Literal
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Depends, status
from pydantic import BaseModel, Field, HttpUrl
from playwright.sync_api import sync_playwright

# Re-using the Pydantic models from the backend for consistency
# In a real-world deployment, these would likely be shared via a common library
# or regenerated from an OpenAPI spec. For now, we'll redefine/copy.

class MigrationPageContent(BaseModel):
    """Represents the content details of a single page."""
    url: HttpUrl
    title: str
    markdown: Optional[str] = None
    raw_html: Optional[str] = None
    drive_html: Optional[str] = None


class MigrationTreeNode(BaseModel):
    """Represents a node in the discovered navigation hierarchy."""
    title: str
    url: Optional[HttpUrl] = None
    depth: int
    children: List["MigrationTreeNode"] = Field(default_factory=list)
    _section_type: Literal["section", "tab", "version", "page"] = "section"

MigrationTreeNode.model_rebuild()


class MigrationIngestRequest(BaseModel):
    """
    Schema for the request body sent by the external Playwright service
    to ingest discovered migration data.
    Note: This is the *response* from the crawler, *request* to the backend.
    """
    source_url: HttpUrl
    tree: List[MigrationTreeNode]
    page_data: Dict[HttpUrl, MigrationPageContent]
    create_drive_docs: bool = False


class CrawlRequest(BaseModel):
    source_url: HttpUrl
    acceldocs_backend_url: HttpUrl
    acceldocs_token: str
    acceldocs_org_id: int
    acceldocs_target_section_id: int
    create_drive_docs: bool = False
    max_pages: Optional[int] = None
    delay_between_requests: float = 0.5


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="AccelDocs External Crawler")

# --- Configuration for external crawler ---
ACCELDOCS_BACKEND_URL = os.getenv("ACCELDOCS_BACKEND_URL")
ACCELDOCS_TOKEN = os.getenv("ACCELDOCS_TOKEN")
ACCELDOCS_ORG_ID = os.getenv("ACCELDOCS_ORG_ID")

if not ACCELDOCS_BACKEND_URL or not ACCELDOCS_TOKEN or not ACCELDOCS_ORG_ID:
    logger.warning("ACCELDOCS_BACKEND_URL, ACCELDOCS_TOKEN, ACCELDOCS_ORG_ID env vars not set. "
                   "Crawl endpoint won't function without these or if passed in request body.")

# --- Helper functions adapted from scripts/migrate_developerhub.py ---
# These are simplified and focus only on Playwright path
# Full implementations for HTML cleaning, markdown conversion, etc. would go here

def _fetch_html_playwright(page, url: str) -> Optional[str]:
    """Fetches HTML using Playwright."""
    try:
        page.goto(url, wait_until="networkidle")
        html_content = page.content()
        return html_content
    except Exception as e:
        logger.error(f"Playwright failed to fetch {url}: {e}")
        return None

def _clean_developerhub_html(html_content: str) -> str:
    """
    A simplified placeholder for HTML cleaning.
    Real implementation would come from app/lib/html_normalize.py or scripts/migrate_developerhub.py
    """
    if not html_content:
        return ""
    soup = BeautifulSoup(html_content, "html.parser")
    # Example: remove script and style tags
    for script_or_style in soup(["script", "style", "meta", "link", "noscript"]):
        script_or_style.decompose()
    # Remove all Angular-specific attributes (data-, ng-)
    for tag in soup.find_all(True):
        tag.attrs = {key: value for key, value in tag.attrs.items() if not key.startswith(('data-', 'ng-'))}
    # Remove app-specific tags
    for app_tag in soup.find_all(lambda tag: tag.name.startswith("app-") or tag.name.startswith("svg-icon")):
        app_tag.decompose()
    # Find main content (placeholder logic)
    main_content = soup.find("main") or soup.find("body")
    if main_content:
        return str(main_content)
    return str(soup)

def _convert_html_to_markdown(html_content: str) -> Optional[str]:
    """
    A simplified placeholder for HTML to Markdown conversion.
    Ideally would use Pandoc, or a robust library like `html2text`.
    For now, a basic conversion.
    """
    if not html_content:
        return None
    # This is a very basic HTML to Markdown converter for demonstration.
    # The actual implementation should be robust (e.g., using `pandoc` or `html2text` with proper configuration).
    # from html2text import html2text # This would require `pip install html2text` in the Dockerfile
    # return html2text(html_content)
    
    # Placeholder: strip tags
    soup = BeautifulSoup(html_content, "html.parser")
    return soup.get_text(separator="\n\n")


def _process_page_content(
    pw_page,
    url: str,
    title: str,
    delay: float = 0.5
) -> Optional[MigrationPageContent]:
    """Fetches, cleans, and converts a single page's content."""
    logger.info(f"Processing page: {url} (Title: {title})")
    
    html_content = _fetch_html_playwright(pw_page, url)
    if not html_content:
        return None

    cleaned_html = _clean_developerhub_html(html_content)
    markdown_content = _convert_html_to_markdown(cleaned_html) # Use cleaned HTML for MD conversion

    # Placeholder for drive_html (admonitions to blockquotes)
    drive_html_content = cleaned_html # For now, same as cleaned_html

    time.sleep(delay) # Respect rate limits

    return MigrationPageContent(
        url=HttpUrl(url),
        title=title,
        markdown=markdown_content,
        raw_html=cleaned_html,
        drive_html=drive_html_content,
    )


# --- Playwright-based Discovery Logic (Simplified) ---
_SIDEBAR_SELECTOR = "div.sidebar-nav-container" # This selector is crucial and comes from original script analysis
_VERSION_SELECTOR = "div.version-switcher a" # Example
_TAB_SELECTOR = "div.top-nav-tabs a" # Example


def _extract_nav_tree(page, base_url: str, current_depth: int = 0) -> List[MigrationTreeNode]:
    """
    Extracts navigation tree from the sidebar using Playwright.
    This is a simplified version of the deep Angular extraction in the original script.
    """
    nodes = []
    
    # This assumes a flat list of links or a simple nested structure
    # The original script had complex logic for Angular SPAs.
    # We will need to refine this significantly based on the actual DOM structure of docs.acceldata.io
    
    # For now, let's try to find links in the sidebar container
    sidebar_container = page.locator(_SIDEBAR_SELECTOR)
    if not sidebar_container.count():
        logger.warning(f"Sidebar container '{_SIDEBAR_SELECTOR}' not found on {page.url}")
        return nodes

    # Look for links within the sidebar
    links = sidebar_container.locator("a")
    for i in range(links.count()):
        link_elem = links.nth(i)
        href = link_elem.get_attribute("href")
        text = link_elem.text_content()
        
        if href and text and href.startswith("/"): # Only internal links
            full_url = urljoin(base_url, href)
            # This is a flat list, the original script inferred depth from DOM structure
            # For a truly multi-level hierarchy, we'd need more complex DOM traversal
            nodes.append(MigrationTreeNode(
                title=text.strip(),
                url=HttpUrl(full_url) if full_url else None,
                depth=current_depth,
                _section_type="page", # Default to page for direct links
                children=[],
            ))
            
    return nodes

async def _perform_crawl(
    source_url: HttpUrl,
    max_pages: Optional[int] = None,
    delay_between_requests: float = 0.5
) -> MigrationIngestRequest:
    """Orchestrates the Playwright crawling process."""
    all_page_data: Dict[HttpUrl, MigrationPageContent] = {}
    navigation_tree: List[MigrationTreeNode] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        logger.info(f"Navigating to source URL: {source_url}")
        page.goto(str(source_url), wait_until="domcontentloaded")
        time.sleep(delay_between_requests) # Give JS time to render

        # --- Step 1: Discover Navigation Tree and collect URLs ---
        # This part needs significant refinement from the original script's deep Angular crawling
        # For now, a simplified approach
        
        # Start with the main navigation tree from the initial page
        discovered_nodes = _extract_nav_tree(page, str(source_url))
        
        # In a full implementation, you'd recursively traverse the tree,
        # possibly clicking on sections to expand them and extract sub-nodes
        # This requires detailed knowledge of the source site's DOM and JS behavior.
        
        # For now, we'll treat discovered_nodes as the initial tree.
        navigation_tree = discovered_nodes # Simplified
        
        urls_to_fetch = set()
        
        # Collect all unique URLs from the discovered tree
        def _collect_urls_from_tree(nodes: List[MigrationTreeNode]):
            for node in nodes:
                if node.url:
                    urls_to_fetch.add(str(node.url))
                _collect_urls_from_tree(node.children)
        _collect_urls_from_tree(navigation_tree)

        logger.info(f"Discovered {len(urls_to_fetch)} unique URLs.")
        if max_pages:
            urls_to_fetch = list(urls_to_fetch)[:max_pages]
            logger.info(f"Limiting to {len(urls_to_fetch)} pages based on max_pages setting.")

        # --- Step 2: Fetch and Process Content for each URL ---
        for url in urls_to_fetch:
            content = _process_page_content(page, url, "Placeholder Title", delay_between_requests) # Title from tree node
            if content:
                all_page_data[content.url] = content
            else:
                logger.warning(f"Failed to get content for {url}")
        
        browser.close()

    # Need to update titles in `navigation_tree` from `all_page_data` once fetched
    # And potentially refine the tree structure if titles were placeholders
    def _update_titles_in_tree(nodes: List[MigrationTreeNode]):
        for node in nodes:
            if node.url and node.url in all_page_data:
                node.title = all_page_data[node.url].title
            _update_titles_in_tree(node.children)
    _update_titles_in_tree(navigation_tree)

    return MigrationIngestRequest(
        source_url=source_url,
        tree=navigation_tree,
        page_data=all_page_data,
        create_drive_docs=False # This will be set by the calling endpoint
    )

@app.post("/crawl")
async def start_crawl(request: CrawlRequest):
    """
    Initiates the crawling process and sends the data to the AccelDocs backend.
    """
    if not ACCELDOCS_BACKEND_URL and not request.acceldocs_backend_url:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="AccelDocs backend URL not provided.")
    
    if not ACCELDOCS_TOKEN and not request.acceldocs_token:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="AccelDocs token not provided.")

    if not ACCELDOCS_ORG_ID and not request.acceldocs_org_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="AccelDocs organization ID not provided.")

    backend_url = request.acceldocs_backend_url or HttpUrl(ACCELDOCS_BACKEND_URL)
    token = request.acceldocs_token or ACCELDOCS_TOKEN
    org_id = request.acceldocs_org_id or ACCELDOCS_ORG_ID

    # Perform the crawl
    migration_data = await _perform_crawl(request.source_url, request.max_pages, request.delay_between_requests)
    migration_data.create_drive_docs = request.create_drive_docs

    # Send data to AccelDocs backend
    target_endpoint = f"{backend_url}/api/migration/{request.acceldocs_target_section_id}/start"
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Org-Id": str(org_id),
        "Content-Type": "application/json"
    }

    logger.info(f"Sending migration data to AccelDocs backend: {target_endpoint}")
    try:
        response = requests.post(target_endpoint, headers=headers, json=migration_data.model_dump())
        response.raise_for_status()
        logger.info(f"Successfully initiated migration on AccelDocs backend. Response: {response.json()}")
        return {"status": "success", "message": "Crawl initiated and data sent to AccelDocs backend.", "backend_response": response.json()}
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to send migration data to AccelDocs backend: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to send data to AccelDocs backend: {e}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
