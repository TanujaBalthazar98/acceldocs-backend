"""Tests for Drive folder classification and parent folder lookup."""

from app.ingestion.drive import (
    DriveFile,
    DriveFolder,
    DriveTree,
    _find_parent_folder,
    classify_folder,
)


def _make_tree():
    """Build a simple test tree: Root/ProjectA/v1.0/Public."""
    root = DriveFolder(id="r", name="Documentation", parent_id=None, path="Documentation", depth=0, children=[])
    proj = DriveFolder(id="p", name="ProjectA", parent_id="r", path="Documentation/ProjectA", depth=1, children=[])
    ver = DriveFolder(id="v", name="v1.0", parent_id="p", path="Documentation/ProjectA/v1.0", depth=2, children=[])
    pub = DriveFolder(id="pub", name="Public", parent_id="v", path="Documentation/ProjectA/v1.0/Public", depth=3, children=[])

    root.children = [proj]
    proj.children = [ver]
    ver.children = [pub]

    doc = DriveFile(id="d1", name="Getting Started", mime_type="application/vnd.google-apps.document", parent_folder_id="pub")

    return DriveTree(root=root, folders=[root, proj, ver, pub], files=[doc])


def test_classify_project_folder():
    tree = _make_tree()
    folder = tree.folders[1]  # ProjectA
    c = classify_folder(folder)
    assert c["project"] == "ProjectA"
    assert c["version"] is None


def test_classify_deep_folder():
    tree = _make_tree()
    folder = tree.folders[3]  # Public
    c = classify_folder(folder)
    assert c["project"] == "ProjectA"
    assert c["version"] == "v1.0"
    assert c["visibility"] == "Public"


def test_find_parent_folder():
    tree = _make_tree()
    doc = tree.files[0]
    parent = _find_parent_folder(doc, tree)
    assert parent is not None
    assert parent.id == "pub"
    assert parent.name == "Public"


def test_find_parent_folder_missing():
    tree = _make_tree()
    orphan = DriveFile(id="x", name="Orphan", mime_type="application/vnd.google-apps.document", parent_folder_id="nonexistent")
    result = _find_parent_folder(orphan, tree)
    assert result is None


def test_find_parent_folder_no_id():
    tree = _make_tree()
    no_parent = DriveFile(id="x", name="NoParent", mime_type="application/vnd.google-apps.document")
    result = _find_parent_folder(no_parent, tree)
    assert result is None
