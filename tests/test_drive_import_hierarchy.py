from app.api import drive as drive_api
from app.models import Organization, Section, User


def _folder(id_: str, name: str) -> dict:
    return {
        "id": id_,
        "name": name,
        "mimeType": drive_api.DRIVE_FOLDER_MIME,
        "modifiedTime": "2026-04-21T00:00:00Z",
    }


def _doc(id_: str, name: str) -> dict:
    return {
        "id": id_,
        "name": name,
        "mimeType": drive_api.GOOGLE_DOC_MIME,
        "modifiedTime": "2026-04-21T00:00:00Z",
    }


def test_scan_folder_maps_first_level_folders_to_tabs_for_product_target(db, monkeypatch):
    owner = User(google_id="owner-drive-import-1", email="owner1@example.com", name="Owner 1")
    db.add(owner)
    db.flush()

    org = Organization(name="Import Org 1", slug="import-org-1", owner_id=owner.id)
    db.add(org)
    db.flush()

    product = Section(
        organization_id=org.id,
        parent_id=None,
        name="ADOC",
        slug="adoc",
        section_type="section",
        drive_folder_id="drive-product-root",
        visibility="public",
        is_published=True,
    )
    db.add(product)
    db.commit()

    tree = {
        "drive-product-root": [
            _folder("drive-docs", "Documentation"),
            _folder("drive-release", "Release Notes"),
        ],
        "drive-docs": [_doc("doc-a", "Intro")],
        "drive-release": [_folder("drive-rn-section", "v26.4")],
        "drive-rn-section": [_doc("doc-b", "Highlights")],
    }

    monkeypatch.setattr(drive_api, "_list_folder_items", lambda _service, folder_id: tree.get(folder_id, []))

    counts = drive_api._scan_folder(
        service=None,
        folder_id="drive-product-root",
        parent_section_id=product.id,
        org_id=org.id,
        user_id=owner.id,
        db=db,
        target_type="product",
    )

    docs_tab = db.query(Section).filter(Section.drive_folder_id == "drive-docs").first()
    release_tab = db.query(Section).filter(Section.drive_folder_id == "drive-release").first()
    rn_section = db.query(Section).filter(Section.drive_folder_id == "drive-rn-section").first()

    assert counts["sections"] == 3
    assert counts["pages"] == 2
    assert docs_tab is not None and docs_tab.section_type == "tab" and docs_tab.parent_id == product.id
    assert release_tab is not None and release_tab.section_type == "tab" and release_tab.parent_id == product.id
    assert rn_section is not None and rn_section.section_type == "section" and rn_section.parent_id == release_tab.id


def test_scan_folder_maps_first_level_folders_to_sections_for_tab_target(db, monkeypatch):
    owner = User(google_id="owner-drive-import-2", email="owner2@example.com", name="Owner 2")
    db.add(owner)
    db.flush()

    org = Organization(name="Import Org 2", slug="import-org-2", owner_id=owner.id)
    db.add(org)
    db.flush()

    product = Section(
        organization_id=org.id,
        parent_id=None,
        name="ADOC",
        slug="adoc",
        section_type="section",
        drive_folder_id="drive-product-root-2",
        visibility="public",
        is_published=True,
    )
    db.add(product)
    db.flush()

    tab = Section(
        organization_id=org.id,
        parent_id=product.id,
        name="Documentation",
        slug="documentation",
        section_type="tab",
        drive_folder_id="drive-tab-root",
        visibility="public",
        is_published=True,
    )
    db.add(tab)
    db.commit()

    tree = {
        "drive-tab-root": [_folder("drive-getting-started", "Getting Started")],
        "drive-getting-started": [_doc("doc-c", "Install")],
    }
    monkeypatch.setattr(drive_api, "_list_folder_items", lambda _service, folder_id: tree.get(folder_id, []))

    drive_api._scan_folder(
        service=None,
        folder_id="drive-tab-root",
        parent_section_id=tab.id,
        org_id=org.id,
        user_id=owner.id,
        db=db,
        target_type="tab",
    )

    section = db.query(Section).filter(Section.drive_folder_id == "drive-getting-started").first()
    assert section is not None
    assert section.section_type == "section"
    assert section.parent_id == tab.id


def test_targeted_scan_root_wrapper_uses_tab_type_for_product_target(db):
    owner = User(google_id="owner-drive-import-3", email="owner3@example.com", name="Owner 3")
    db.add(owner)
    db.flush()

    org = Organization(name="Import Org 3", slug="import-org-3", owner_id=owner.id)
    db.add(org)
    db.flush()

    product = Section(
        organization_id=org.id,
        parent_id=None,
        name="ADOC",
        slug="adoc",
        section_type="section",
        drive_folder_id="product-root-folder",
        visibility="public",
        is_published=True,
    )
    db.add(product)
    db.commit()

    wrapper = drive_api._upsert_target_import_root_section(
        org_id=org.id,
        target_section=product,
        target_type="product",
        folder_id="import-root-folder",
        folder_name="Documentation",
        db=db,
    )
    db.commit()

    assert wrapper.section_type == "tab"
    assert wrapper.parent_id == product.id
