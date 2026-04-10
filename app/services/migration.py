import json
import logging
from typing import Dict, Any, List, Optional

import bleach
from sqlalchemy.orm import Session

from app.models import Migration, MigrationPage, Section, Page, User
from app.lib.slugify import to_slug # For consistent slug generation

# Assuming a utility function exists for converting AccelDocs HTML admonitions to GDocs blockquotes
# from app.lib.html_normalize import admonitions_to_blockquotes_for_gdocs as _admonitions_to_blockquotes_for_gdocs
# For now, will include a placeholder adaptation if not found

logger = logging.getLogger(__name__)

class MigrationServiceError(Exception):
    """Custom exception for migration service errors."""
    pass


def _build_search_text(html_content: str | None) -> str | None:
    """Generate plain-text search content from HTML."""
    cleaned = bleach.clean(html_content or "", tags=[], strip=True).strip()
    return cleaned or None


# Placeholder for admonition conversion if not directly in lib
def _admonitions_to_blockquotes_for_gdocs(html_content: str) -> str:
    """
    Placeholder: Converts AccelDocs-style admonition HTML to Google Docs-compatible blockquotes.
    This logic should ideally live in app.lib.html_normalize or similar.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html_content, "html.parser")
    for adv in list(soup.find_all("div", class_="admonition")):
        title_elem = adv.find("p", class_="admonition-title")
        body_elem = adv.find("div", class_="admonition-body")

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

        blockquote = soup.new_tag("blockquote")
        title_p = soup.new_tag("p")
        title_strong = soup.new_tag("strong")
        title_strong.string = f"{title_text}:"
        title_p.append(title_strong)
        blockquote.append(title_p)

        body_soup = BeautifulSoup(body_html, "html.parser")
        body_children = list(body_soup.children)
        if not body_children or all(isinstance(c, str) and c.strip() for c in body_children):
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


def _get_or_create_section(
    db: Session,
    organization_id: int,
    parent_section_id: Optional[int],
    name: str,
    section_type: str,
    display_order: int,
) -> Section:
    """
    Helper to get an existing section or create a new one.
    Ensures slug uniqueness within parent.
    """
    section_slug = to_slug(name)
    existing_section = db.query(Section).filter(
        Section.organization_id == organization_id,
        Section.parent_id == parent_section_id,
        Section.slug == section_slug
    ).first()

    if existing_section:
        logger.info("Re-using existing section '%s' (id=%d)", existing_section.name, existing_section.id)
        # Update display order if different
        if existing_section.display_order != display_order:
            existing_section.display_order = display_order
            db.add(existing_section)
        return existing_section
    else:
        new_section = Section(
            organization_id=organization_id,
            parent_id=parent_section_id,
            name=name,
            slug=section_slug,
            section_type=section_type,
            visibility="public", # Default to public for migration
            display_order=display_order,
            is_published=True, # Mark as published by default during migration
        )
        db.add(new_section)
        db.flush() # Flush to get the ID
        logger.info("Created section '%s' (id=%d)", name, new_section.id)
        return new_section


def _recursively_process_tree_nodes(
    db: Session,
    migration_id: int,
    organization_id: int,
    current_tree_nodes: List[dict[str, Any]],
    current_acceldocs_parent_id: int, # The actual AccelDocs Section ID
    migration_page_map: Dict[str, MigrationPage], # Map of source_url to MigrationPage object
    section_tree_map: Dict[str, int], # Map of node_path to AccelDocs Section ID
    path: str = "",
    order_prefix: str = "",
) -> None:
    """
    Recursively processes the migration tree.
    Creates sections and updates MigrationPage records with target_section_id and display_order.
    """
    for idx, node in enumerate(current_tree_nodes):
        node_title = str(node.get("title") or "").strip()
        if not node_title:
            continue
        node_children = node.get("children")
        children_list = node_children if isinstance(node_children, list) else []
        node_url = node.get("url")

        node_display_order = int(f"{order_prefix}{idx:04d}") if order_prefix else idx
        node_path_segment = to_slug(node_title)
        current_node_path = f"{path}/{node_path_segment}".strip('/')

        # Determine AccelDocs Section ID for this node
        acceldocs_section_id = current_acceldocs_parent_id

        if children_list: # It's a section-like node
            section_type = str(node.get("_section_type") or "section")

            # Check if this section has already been created and mapped
            if current_node_path in section_tree_map:
                acceldocs_section = db.query(Section).filter(
                    Section.id == section_tree_map[current_node_path],
                    Section.organization_id == organization_id
                ).first()
            else:
                acceldocs_section = _get_or_create_section(
                    db,
                    organization_id=organization_id,
                    parent_section_id=current_acceldocs_parent_id,
                    name=node_title,
                    section_type=section_type,
                    display_order=node_display_order,
                )
                section_tree_map[current_node_path] = acceldocs_section.id

            acceldocs_section_id = acceldocs_section.id
            
            # Recursively process children with the new parent section ID and path
            _recursively_process_tree_nodes(
                db,
                migration_id,
                organization_id,
                children_list,
                acceldocs_section_id,
                migration_page_map,
                section_tree_map,
                path=current_node_path,
                order_prefix=f"{order_prefix}{idx:04d}",
            )
        else: # It's a page node
            if node_url and str(node_url) in migration_page_map:
                mig_page = migration_page_map[str(node_url)]
                mig_page.target_section_id = acceldocs_section_id
                mig_page.display_order = node_display_order
                db.add(mig_page)
                db.flush() # Flush to update the MigrationPage record



def initialize_migration_sections_and_pages(
    db: Session,
    migration: Migration,
    current_user: User,
) -> None:
    """
    Initializes the section hierarchy in AccelDocs and updates MigrationPage records
    with their correct target_section_id and display_order.
    This function processes the `Migration.tree_json`.
    """
    if not migration.tree_json:
        raise MigrationServiceError("Migration tree_json is missing.")

    parsed_tree = json.loads(migration.tree_json)
    if not isinstance(parsed_tree, list):
        raise MigrationServiceError("Migration tree_json must be a list.")

    # Map for easy lookup of MigrationPage objects by source_url
    migration_pages = db.query(MigrationPage).filter(
        MigrationPage.migration_id == migration.id
    ).all()
    migration_page_map = {mp.source_url: mp for mp in migration_pages}

    # Map to store the AccelDocs Section IDs for the created hierarchy
    # Key: relative path (slug-based), Value: AccelDocs Section ID
    section_tree_map: Dict[str, int] = {}
    
    # Start recursive processing from the migration's target section
    # The `target_section_id` in Migration is the root for this migration.
    
    # To handle the case where a migration imports into an existing section,
    # we need to set the `current_node_path` correctly for the root of the tree.
    # Let's say `migration.target_section` has slug 'my-product'.
    # The first level of `tree_nodes` (e.g., 'Documentation', 'API Reference')
    # should become children of 'my-product'.
    
    # We pass the actual AccelDocs target section ID as the parent.
    # The node_path_segment will represent children of this.
    _recursively_process_tree_nodes(
        db,
        migration.id,
        migration.organization_id,
        parsed_tree,
        migration.target_section_id,
        migration_page_map,
        section_tree_map,
        path="",
        order_prefix="",
    )
    db.commit()
    logger.info(f"Initialized sections and page target_section_ids for Migration ID {migration.id}")


def import_page_into_acceldocs(
    db: Session,
    migration_page: MigrationPage,
    current_user: User, # The user initiating the migration
    create_drive_docs: bool,
) -> int:
    """
    Imports a single MigrationPage into AccelDocs as a new Page record.
    Returns the ID of the newly created Page.
    """
    if migration_page.target_page_id:
        logger.info(f"Page {migration_page.source_url} already imported as Page ID {migration_page.target_page_id}. Skipping.")
        return migration_page.target_page_id
    
    if not migration_page.target_section_id:
        raise MigrationServiceError(f"MigrationPage {migration_page.id} has no target_section_id assigned. Sections might not be initialized.")

    parent_acceldocs_section = db.query(Section).filter(
        Section.id == migration_page.target_section_id,
        Section.organization_id == current_user.organization_id
    ).first()

    if not parent_acceldocs_section:
        raise MigrationServiceError(f"Target AccelDocs section with ID {migration_page.target_section_id} not found for page {migration_page.source_url}.")

    page_slug = to_slug(migration_page.title)

    existing_page = db.query(Page).filter(
        Page.organization_id == current_user.organization_id,
        Page.section_id == parent_acceldocs_section.id,
        Page.slug == page_slug
    ).first()

    if existing_page:
        # Update existing page instead of creating a new one
        logger.info(f"Updating existing page '{existing_page.title}' (ID: {existing_page.id}) from {migration_page.source_url}")
        existing_page.title = migration_page.title
        existing_page.html_content = migration_page.html_content
        existing_page.published_html = migration_page.html_content # For now, set published to raw HTML
        existing_page.search_text = _build_search_text(migration_page.html_content)
        existing_page.status = "published"
        existing_page.is_published = True
        existing_page.display_order = migration_page.display_order
        existing_page.owner_id = current_user.id

        # Update google_doc_id if it's a placeholder
        if existing_page.google_doc_id and existing_page.google_doc_id.startswith("migrated-"):
            existing_page.google_doc_id = f"migrated-{migration_page.id}-{page_slug}" # Re-assign placeholder

        db.add(existing_page)
        db.flush()
        return existing_page.id
    else:
        # Create new page
        new_page = Page(
            organization_id=current_user.organization_id,
            section_id=parent_acceldocs_section.id,
            google_doc_id=f"migrated-{migration_page.id}-{page_slug}", # Placeholder
            title=migration_page.title,
            slug=page_slug,
            html_content=migration_page.html_content,
            published_html=migration_page.html_content, # Initially set published to raw HTML
            search_text=_build_search_text(migration_page.html_content),
            is_published=True, # Mark as published by default during migration
            status="published",
            display_order=migration_page.display_order,
            owner_id=current_user.id, # Assign current user as owner
        )
        
        db.add(new_page)
        db.flush() # Flush to get new_page.id
        logger.info(f"Imported page '{new_page.title}' (ID: {new_page.id}) from {migration_page.source_url}")
        return new_page.id


def resolve_migration_page_links(
    db: Session,
    migration_page: MigrationPage,
    old_url_to_page_id: Dict[str, int],
) -> None:
    """
    Resolves [[MIGRATED:path]] placeholders in the page's HTML content
    to actual /pages/{id} URLs based on the old_url_to_page_id map.
    Updates the actual AccelDocs Page model.
    """
    from bs4 import BeautifulSoup
    from urllib.parse import urlparse

    if not migration_page.target_page_id:
        logger.warning(f"MigrationPage {migration_page.id} has no target_page_id. Skipping link resolution.")
        return

    accel_page = db.query(Page).filter(Page.id == migration_page.target_page_id).first()
    if not accel_page:
        logger.warning(f"AccelDocs Page ID {migration_page.target_page_id} not found for migration page {migration_page.source_url}. Cannot resolve links.")
        return

    if not accel_page.html_content:
        logger.info(f"AccelDocs Page ID {accel_page.id} has no html_content. Skipping link resolution.")
        return

    soup = BeautifulSoup(accel_page.html_content, "html.parser")

    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if not href.startswith("[[MIGRATED:"):
            continue

        placeholder = href
        fragment = ""
        if "#" in href:
            placeholder, fragment = href.split("#", 1)
            fragment = f"#{fragment}"

        path = placeholder.replace("[[MIGRATED:", "").replace("]]", "")

        page_id = None
        for old_url, pid in old_url_to_page_id.items():
            old_path = urlparse(old_url).path.rstrip("/")
            if old_path == path:
                page_id = pid
                break

        if page_id:
            anchor["href"] = f"/pages/{page_id}{fragment}"
        else:
            logger.warning("Could not resolve migrated path: %s for page %s (AccelDocs ID: %s)", path, migration_page.source_url, accel_page.id)
            anchor.string = f"BROKEN LINK: {anchor.get_text()}" # Add text indicator
            anchor["href"] = "#broken-link"

    accel_page.html_content = str(soup)
    accel_page.published_html = str(soup) # Update published HTML as well

    # If the original migration_page had drive_html_content, re-convert the resolved HTML for GDocs
    if migration_page.drive_html_content:
        # Assuming _admonitions_to_blockquotes_for_gdocs converts resolved HTML
        migration_page.drive_html_content = _admonitions_to_blockquotes_for_gdocs(accel_page.html_content)
    
    db.add(accel_page)
    db.flush() # Flush changes to the Page model
    logger.info(f"Resolved links for page {migration_page.source_url} (AccelDocs ID: {accel_page.id})")


def process_migration_page_task(
    db: Session,
    migration_page_id: int,
    current_user: User, # User to perform actions on behalf of
    create_drive_docs: bool, # From migration settings
) -> MigrationPage:
    """
    Processes a single MigrationPage task:
    1. Imports the page content into AccelDocs.
    2. Updates the MigrationPage status.
    """
    migration_page = db.query(MigrationPage).filter(
        MigrationPage.id == migration_page_id,
        MigrationPage.status == "IN_PROGRESS" # Should be IN_PROGRESS from process-next endpoint
    ).first()

    if not migration_page:
        raise MigrationServiceError(f"MigrationPage {migration_page_id} not found or not in IN_PROGRESS status.")

    migration = migration_page.migration # Get parent migration object

    try:
        # --- Import the page ---
        page_id = import_page_into_acceldocs(
            db,
            migration_page,
            current_user,
            create_drive_docs,
        )
        
        migration_page.target_page_id = page_id
        migration_page.status = "IMPORTED"
        
        # Update parent Migration's page_id_map (stored as JSON)
        # We need to load, update, and dump it back
        page_id_map = json.loads(migration.page_id_map_json or "{}")
        page_id_map[migration_page.source_url] = page_id
        migration.page_id_map_json = json.dumps(page_id_map)

        migration.completed_pages = int(migration.completed_pages or 0) + 1

        db.add(migration_page)
        db.add(migration) # Add migration to session to track its changes
        db.commit()
        db.refresh(migration_page)
        
        return migration_page

    except Exception as e:
        db.rollback() # Rollback any changes from this page processing
        migration_page.status = "FAILED"
        migration_page.error_message = str(e)
        db.add(migration_page)
        db.add(migration) # Add migration to session to track its changes
        db.commit()
        db.refresh(migration_page)
        logger.error(f"Error processing MigrationPage {migration_page_id}: {e}", exc_info=True)
        raise MigrationServiceError(f"Failed to process page {migration_page.source_url}: {e}") from e


def resolve_all_migration_links_task(
    db: Session,
    migration_id: int,
    current_user: User,
) -> Migration:
    """
    Second pass: After all pages are imported, resolve internal links in all pages.
    This function will iterate through all IMPORТED pages of a migration and resolve their links.
    """
    migration = db.query(Migration).filter(
        Migration.id == migration_id,
        Migration.organization_id == current_user.organization_id,
    ).first()

    if not migration:
        raise MigrationServiceError(f"Migration {migration_id} not found or inaccessible.")

    if migration.status != "COMPLETED":
        logger.warning(f"Migration {migration_id} is not COMPLETED. Link resolution should typically happen after all pages are imported.")
        # Optionally, raise an error or just proceed with a warning

    if not migration.page_id_map_json:
        logger.warning(f"Migration {migration_id} has no page_id_map_json, cannot resolve links.")
        return migration

    old_url_to_page_id = json.loads(migration.page_id_map_json)

    # Fetch all pages that were part of this migration and successfully imported
    migration_pages_to_resolve = db.query(MigrationPage).filter(
        MigrationPage.migration_id == migration_id,
        MigrationPage.target_page_id.isnot(None),
        MigrationPage.status == "IMPORTED", # Only resolve for successfully imported pages
    ).all()

    num_resolved = 0
    num_failed_resolve = 0

    for mig_page in migration_pages_to_resolve:
        try:
            resolve_migration_page_links(db, mig_page, old_url_to_page_id)
            mig_page.status = "LINK_RESOLVED"
            db.add(mig_page)
            db.commit() # Commit each page's link resolution
            num_resolved += 1
            
        except Exception as e:
            db.rollback() # Rollback changes for this specific page if an error occurred
            mig_page.status = "FAILED"
            mig_page.error_message = f"Link resolution failed: {e}"
            db.add(mig_page)
            db.commit()
            num_failed_resolve += 1
            logger.error(f"Error resolving links for MigrationPage {mig_page.id}: {e}", exc_info=True)
            # Continue to next page even if one fails

    logger.info(f"Link resolution complete for migration {migration_id}: {num_resolved} resolved, {num_failed_resolve} failed.")
    db.refresh(migration)
    return migration
