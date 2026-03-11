from app.models import Document, Project
from app.services.documents import _resolve_publish_path


def _make_doc_with_project(project: Project) -> Document:
    return Document(
        google_doc_id="doc-path-test",
        title="Test Doc",
        slug="test-doc",
        project="legacy-project",
        version="v1.0",
        visibility="public",
        status="approved",
        project_rel=project,
    )


def test_resolve_publish_path_replaces_placeholder_project_slug():
    parent = Project(name="ADOC", slug="new-project")
    child = Project(name="Release Notes", slug="new-project", parent=parent)
    doc = _make_doc_with_project(child)

    project_slug, version_slug, section, doc_slug, product_slug = _resolve_publish_path(doc)

    assert project_slug == "release-notes"
    assert product_slug == "adoc"
    assert version_slug == "v1.0"
    assert section is None
    assert doc_slug == "test-doc"


def test_resolve_publish_path_keeps_custom_project_slug():
    parent = Project(name="ADOC", slug="adoc")
    child = Project(name="Release Notes", slug="release-notes", parent=parent)
    doc = _make_doc_with_project(child)

    project_slug, _, _, _, product_slug = _resolve_publish_path(doc)

    assert project_slug == "release-notes"
    assert product_slug == "adoc"
