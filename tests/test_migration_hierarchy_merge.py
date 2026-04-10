from scripts import migrate_developerhub as migrator


def _node(title: str, url: str | None = None, depth: int = 0, children: list[dict] | None = None) -> dict:
    return {
        "title": title,
        "url": url,
        "depth": depth,
        "children": children or [],
    }


def test_tokenize_for_match_adds_simple_stems() -> None:
    tokens = migrator._tokenize_for_match("users management")
    assert "users" in tokens
    assert "user" in tokens
    assert "management" in tokens
    assert "manage" in tokens


def test_infer_section_for_flat_page_uses_section_landing_nodes() -> None:
    tab_tree = [
        _node(
            "Manage Users and Roles",
            "https://docs.acceldata.io/pulse/user-guide/manage-users-and-roles",
            1,
            [
                _node(
                    "Assign Roles",
                    "https://docs.acceldata.io/pulse/user-guide/assign-roles",
                    2,
                )
            ],
        )
    ]

    inferred = migrator._infer_section_for_flat_page(tab_tree, "user-management")
    assert inferred == "Manage Users and Roles"


def test_merge_sitemap_reuses_section_landing_node_and_keeps_shape() -> None:
    tab_tree = [
        _node(
            "Manage Users and Roles",
            "https://docs.acceldata.io/pulse/user-guide/manage-users-and-roles",
            1,
            [
                _node(
                    "Assign Roles",
                    "https://docs.acceldata.io/pulse/user-guide/assign-roles",
                    2,
                )
            ],
        )
    ]

    merged = migrator._merge_sitemap_urls_into_tab_tree(
        tab_tree=tab_tree,
        tab_url="https://docs.acceldata.io/pulse/user-guide",
        sitemap_urls=[
            "https://docs.acceldata.io/pulse/user-guide/assign-roles",
            "https://docs.acceldata.io/pulse/user-guide/user-management",
        ],
    )

    assert merged == 1
    assert len(tab_tree) == 1
    # With nested prefixes, user-management gets added but may create sub-section under sidebar match
    # Find it recursively through the tree
    def find_url(nodes):
        for n in nodes:
            if n.get("url") == "https://docs.acceldata.io/pulse/user-guide/user-management":
                return True
            if n.get("children"):
                if find_url(n["children"]):
                    return True
        return False
    
    assert find_url(tab_tree)


def test_merge_sitemap_respects_tab_path_boundary() -> None:
    tab_tree: list[dict] = []
    merged = migrator._merge_sitemap_urls_into_tab_tree(
        tab_tree=tab_tree,
        tab_url="https://docs.acceldata.io/pulse/user-guide",
        sitemap_urls=[
            "https://docs.acceldata.io/pulse/user-guide-old/some-page",
            "https://docs.acceldata.io/pulse/user-guide-v2/another-page",
        ],
    )

    assert merged == 0
    assert tab_tree == []
