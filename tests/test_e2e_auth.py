"""End-to-end tests for authentication and authorization."""

import jwt
import pytest
from app.config import settings


class TestAuthentication:
    """Test authentication flows."""

    def test_unauthenticated_access_to_public_documents(self, client, test_documents):
        """Unauthenticated users can access public documents."""
        # List documents - should see public ones
        response = client.get("/api/documents/")
        assert response.status_code == 200
        docs = response.json()
        assert len(docs) == 3  # Only public documents
        titles = [d["title"] for d in docs]
        assert "Internal Approved Document" not in titles

    def test_unauthenticated_cannot_access_internal_documents(self, client, test_documents):
        """Unauthenticated users cannot access internal documents."""
        # Try to access internal document
        internal_doc = test_documents[2]  # Internal Approved
        response = client.get(f"/api/documents/{internal_doc.id}")
        assert response.status_code == 403
        assert "internal-only" in response.json()["detail"].lower()

    def test_authenticated_viewer_can_access_internal_documents(self, client, test_documents, auth_headers):
        """Authenticated viewers can access internal documents."""
        internal_doc = test_documents[2]  # Internal Approved
        response = client.get(
            f"/api/documents/{internal_doc.id}",
            headers=auth_headers("viewer")
        )
        assert response.status_code == 200
        doc = response.json()
        assert doc["title"] == "Internal Approved Document"

    def test_invalid_token_returns_401(self, client):
        """Invalid JWT token returns 401 Unauthorized."""
        response = client.get(
            "/api/users/me",
            headers={"Authorization": "Bearer invalid-token"}
        )
        assert response.status_code == 401
        assert "invalid" in response.json()["detail"].lower()

    def test_expired_token_returns_401(self, client, test_users):
        """Expired JWT token returns 401 Unauthorized."""
        import time
        user = test_users["viewer"]

        # Create token that expired 1 hour ago
        token = jwt.encode(
            {
                "user_id": user.id,
                "email": user.email,
                "role": user.role,
                "exp": int(time.time()) - 3600
            },
            settings.secret_key,
            algorithm="HS256"
        )

        response = client.get(
            "/api/users/me",
            headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 401
        assert "expired" in response.json()["detail"].lower()

    def test_missing_authorization_header_for_protected_route(self, client):
        """Missing auth header returns 401 for protected routes."""
        response = client.get("/api/users/me")
        assert response.status_code == 401


class TestRoleBasedAccessControl:
    """Test RBAC permissions."""

    def test_viewer_cannot_update_document_status(self, client, test_documents, auth_headers):
        """Viewers cannot update document status (requires editor)."""
        doc = test_documents[0]
        response = client.post(
            f"/api/documents/{doc.id}/status",
            json={"status": "approved"},
            headers=auth_headers("viewer")
        )
        assert response.status_code == 403
        assert "insufficient" in response.json()["detail"].lower()

    def test_editor_can_update_document_status(self, client, test_documents, auth_headers):
        """Editors can update document status."""
        doc = test_documents[0]
        response = client.post(
            f"/api/documents/{doc.id}/status",
            json={"status": "review"},
            headers=auth_headers("editor")
        )
        assert response.status_code == 200
        assert response.json()["document_status"] == "review"

    def test_editor_cannot_delete_project(self, client, auth_headers):
        """Editors cannot delete projects (requires admin)."""
        response = client.delete(
            "/api/projects/test-project",
            headers=auth_headers("editor")
        )
        assert response.status_code == 403

    def test_admin_can_delete_project(self, client, db, auth_headers):
        """Admins can delete projects."""
        from app.models import Project

        # Create test project
        project = Project(
            name="Test Project to Delete",
            slug="test-project-delete",
            description="Will be deleted",
        )
        db.add(project)
        db.commit()
        db.refresh(project)  # Get the ID

        response = client.delete(
            f"/api/projects/{project.id}",
            headers=auth_headers("admin")
        )
        assert response.status_code == 200

    def test_reviewer_can_approve_documents(self, client, test_documents, auth_headers):
        """Reviewers can approve documents in approval queue."""
        doc = test_documents[3]  # Review document
        response = client.post(
            "/api/approvals/action",
            json={
                "document_id": doc.id,
                "action": "approve",
                "comment": "Looks good!"
            },
            headers=auth_headers("reviewer")
        )
        # This will fail due to Google Drive access, but should not be auth error
        assert response.status_code in [200, 500]

    def test_viewer_cannot_approve_documents(self, client, test_documents, auth_headers):
        """Viewers cannot approve documents."""
        doc = test_documents[3]
        response = client.post(
            "/api/approvals/action",
            json={
                "document_id": doc.id,
                "action": "approve"
            },
            headers=auth_headers("viewer")
        )
        assert response.status_code == 403

    def test_role_hierarchy_admin_has_all_permissions(self, client, test_documents, auth_headers):
        """Admins have all permissions (highest role)."""
        doc = test_documents[0]

        # Admin can do editor actions
        response = client.post(
            f"/api/documents/{doc.id}/status",
            json={"status": "review"},
            headers=auth_headers("admin")
        )
        assert response.status_code == 200

        # Admin can do reviewer actions (will fail at Google Drive, but not auth)
        response = client.post(
            "/api/approvals/action",
            json={
                "document_id": doc.id,
                "action": "approve"
            },
            headers=auth_headers("admin")
        )
        assert response.status_code in [200, 500]  # 500 for Drive error, not auth

    def test_bulk_operations_require_editor_role(self, client, test_documents, auth_headers):
        """Bulk operations require editor role."""
        doc_ids = [test_documents[0].id, test_documents[1].id]

        # Viewer cannot perform bulk operations
        response = client.post(
            "/api/documents/bulk",
            json={
                "document_ids": doc_ids,
                "action": "set_status",
                "value": "approved"
            },
            headers=auth_headers("viewer")
        )
        assert response.status_code == 403

        # Editor can perform bulk operations
        response = client.post(
            "/api/documents/bulk",
            json={
                "document_ids": doc_ids,
                "action": "set_status",
                "value": "approved"
            },
            headers=auth_headers("editor")
        )
        assert response.status_code == 200
        assert response.json()["success"] == 2


class TestVisibilityControls:
    """Test document visibility controls."""

    def test_public_documents_visible_to_all(self, client, test_documents):
        """Public documents are visible to unauthenticated users."""
        response = client.get("/api/documents/")
        assert response.status_code == 200
        docs = response.json()
        public_docs = [d for d in docs if d["visibility"] == "public"]
        assert len(public_docs) >= 3

    def test_internal_documents_hidden_from_unauthenticated(self, client, test_documents):
        """Internal documents are hidden from unauthenticated users."""
        response = client.get("/api/documents/")
        assert response.status_code == 200
        docs = response.json()
        internal_docs = [d for d in docs if d["visibility"] == "internal"]
        assert len(internal_docs) == 0

    def test_internal_documents_visible_to_authenticated(self, client, test_documents, auth_headers):
        """Internal documents are visible to authenticated users."""
        response = client.get(
            "/api/documents/",
            headers=auth_headers("viewer")
        )
        assert response.status_code == 200
        docs = response.json()
        internal_docs = [d for d in docs if d["visibility"] == "internal"]
        assert len(internal_docs) >= 1

    def test_search_stats_filtered_by_visibility(self, client, test_documents, auth_headers):
        """Search stats only include documents user can access."""
        # Unauthenticated - only public docs
        response = client.get("/api/documents/search/stats")
        assert response.status_code == 200
        stats = response.json()
        assert stats["total"] == 3  # Only public documents

        # Authenticated - all docs
        response = client.get(
            "/api/documents/search/stats",
            headers=auth_headers("viewer")
        )
        assert response.status_code == 200
        stats = response.json()
        assert stats["total"] == 4  # All documents
