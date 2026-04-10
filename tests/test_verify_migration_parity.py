from scripts.verify_migration_parity import (
    _build_expected_shape,
    _compare_structure,
)


def test_build_expected_shape_tracks_section_and_page_order() -> None:
    tree = [
        {
            "title": "Tab A",
            "url": None,
            "_section_type": "tab",
            "children": [
                {"title": "Page 1", "url": "https://docs.example.com/a/page-1", "children": []},
                {
                    "title": "Section X",
                    "url": None,
                    "children": [
                        {"title": "Page 2", "url": "https://docs.example.com/a/page-2", "children": []},
                    ],
                },
            ],
        }
    ]

    expected = _build_expected_shape(tree)
    root_children = expected.section_children_by_parent[""]
    assert len(root_children) == 1
    assert root_children[0].title == "Tab A"
    assert root_children[0].order == 0

    tab_path = root_children[0].path
    tab_pages = expected.pages_by_section_path[tab_path]
    assert len(tab_pages) == 1
    assert tab_pages[0].url == "https://docs.example.com/a/page-1"
    assert tab_pages[0].order == 0

    section_children = expected.section_children_by_parent[tab_path]
    assert len(section_children) == 1
    assert section_children[0].title == "Section X"
    assert section_children[0].order == 1


def test_compare_structure_detects_page_order_mismatch() -> None:
    tree = [
        {
            "title": "Tab A",
            "url": None,
            "_section_type": "tab",
            "children": [
                {"title": "Page 1", "url": "https://docs.example.com/a/page-1", "children": []},
                {"title": "Page 2", "url": "https://docs.example.com/a/page-2", "children": []},
            ],
        }
    ]
    expected = _build_expected_shape(tree)

    product_id = 10
    section_map = {
        "": product_id,  # not used directly but harmless
        "/tab-a": 11,
    }
    page_id_map = {
        "https://docs.example.com/a/page-1": 100,
        "https://docs.example.com/a/page-2": 101,
    }

    sections = [
        {"id": 11, "parent_id": 10, "name": "Tab A", "display_order": 0},
    ]
    pages = [
        {"id": 100, "section_id": 11, "title": "Page 1", "display_order": 1},  # wrong
        {"id": 101, "section_id": 11, "title": "Page 2", "display_order": 0},  # wrong
    ]

    report = _compare_structure(
        expected=expected,
        sections=sections,
        pages=pages,
        section_map=section_map,
        page_id_map=page_id_map,
        product_id=product_id,
        strict=True,
    )

    assert len(report["page_order_mismatch"]) == 2
    assert report["missing_page_mapping"] == []
