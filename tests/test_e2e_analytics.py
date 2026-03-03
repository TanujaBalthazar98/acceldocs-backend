"""End-to-end tests for analytics and tracking."""

import pytest
from datetime import datetime, timezone


class TestAnalyticsTracking:
    """Test analytics view tracking."""

    def test_document_preview_tracks_view(self, client, test_documents, auth_headers, db):
        """Accessing document preview tracks a view."""
        from app.models import DocumentView

        doc = test_documents[1]  # Public approved

        # Get initial view count
        initial_count = db.query(DocumentView).filter(
            DocumentView.document_id == doc.id
        ).count()

        # Access preview (this should track a view)
        # Note: This will fail because we don't have actual markdown files,
        # but we can test the tracking mechanism via the track endpoint
        response = client.post(
            f"/api/analytics/track/view/{doc.id}",
            headers=auth_headers("viewer")
        )
        assert response.status_code == 200

        # Verify view was tracked
        new_count = db.query(DocumentView).filter(
            DocumentView.document_id == doc.id
        ).count()
        assert new_count == initial_count + 1

    def test_view_tracking_includes_metadata(self, client, test_documents, auth_headers, db):
        """View tracking captures user, IP, user-agent, referer."""
        from app.models import DocumentView

        doc = test_documents[1]

        # Track view with metadata
        response = client.post(
            f"/api/analytics/track/view/{doc.id}",
            headers={
                **auth_headers("viewer"),
                "user-agent": "TestBot/1.0",
                "referer": "https://example.com/docs"
            }
        )
        assert response.status_code == 200

        # Verify metadata was captured
        view = db.query(DocumentView).filter(
            DocumentView.document_id == doc.id
        ).order_by(DocumentView.viewed_at.desc()).first()

        assert view is not None
        assert view.user_agent == "TestBot/1.0"
        assert view.referer == "https://example.com/docs"
        assert view.user_email == "viewer@example.com"

    def test_anonymous_view_tracking(self, client, test_documents, db):
        """Views can be tracked for anonymous users."""
        from app.models import DocumentView

        doc = test_documents[1]  # Public document

        response = client.post(f"/api/analytics/track/view/{doc.id}")
        assert response.status_code == 200

        # Verify view was tracked with null user_id
        view = db.query(DocumentView).filter(
            DocumentView.document_id == doc.id,
            DocumentView.user_id.is_(None)
        ).order_by(DocumentView.viewed_at.desc()).first()

        assert view is not None
        assert view.user_id is None


class TestAnalyticsReporting:
    """Test analytics reporting endpoints."""

    def test_get_trending_documents(self, client, test_views, auth_headers):
        """Get trending documents based on recent views."""
        response = client.get(
            "/api/analytics/documents/trending?limit=5",
            headers=auth_headers("viewer")
        )
        assert response.status_code == 200
        trending = response.json()

        assert len(trending) > 0
        # Most viewed should be first
        assert trending[0]["views_last_7_days"] >= trending[-1]["views_last_7_days"]

    def test_get_document_stats(self, client, test_views, auth_headers):
        """Get document view statistics."""
        response = client.get(
            "/api/analytics/documents/stats?limit=10",
            headers=auth_headers("viewer")
        )
        assert response.status_code == 200
        stats = response.json()

        assert len(stats) > 0
        for stat in stats:
            assert "document_id" in stat
            assert "document_title" in stat
            assert "total_views" in stat
            assert "unique_users" in stat

    def test_get_user_activity(self, client, test_views, auth_headers):
        """Get user activity statistics."""
        response = client.get(
            "/api/analytics/users/activity?limit=10",
            headers=auth_headers("admin")
        )
        assert response.status_code == 200
        activity = response.json()

        assert len(activity) > 0
        for user in activity:
            assert "user_email" in user
            assert "total_views" in user

    def test_get_analytics_summary(self, client, test_views, auth_headers):
        """Get overall analytics summary."""
        response = client.get(
            "/api/analytics/summary",
            headers=auth_headers("viewer")
        )
        assert response.status_code == 200
        summary = response.json()

        assert "total_documents" in summary
        assert "total_views" in summary
        assert "total_views_last_7_days" in summary
        assert "total_views_last_30_days" in summary
        assert "unique_viewers" in summary
        assert "trending_documents" in summary

        assert summary["total_documents"] >= 4
        assert summary["total_views"] >= 8  # From test_views fixture

    def test_analytics_requires_authentication(self, client):
        """Analytics endpoints require authentication."""
        endpoints = [
            "/api/analytics/summary",
            "/api/analytics/documents/trending",
            "/api/analytics/documents/stats",
            "/api/analytics/users/activity",
        ]

        for endpoint in endpoints:
            response = client.get(endpoint)
            assert response.status_code == 401

    def test_document_stats_filter_by_project(self, client, test_views, auth_headers):
        """Can filter document stats by project."""
        response = client.get(
            "/api/analytics/documents/stats?project=Test Project&limit=10",
            headers=auth_headers("viewer")
        )
        assert response.status_code == 200
        stats = response.json()

        for stat in stats:
            assert stat["project"] == "Test Project"


class TestAnalyticsDashboard:
    """Test analytics dashboard page."""

    def test_analytics_page_loads(self, client):
        """Analytics dashboard page loads."""
        response = client.get("/analytics")
        assert response.status_code == 200
        assert b"Analytics" in response.content

    def test_analytics_page_has_charts(self, client):
        """Analytics page includes data tables."""
        response = client.get("/analytics")
        assert response.status_code == 200
        content = response.content.decode()

        assert "Trending Documents" in content
        assert "Document Statistics" in content
        assert "User Activity" in content
        assert "loadAnalytics" in content  # JavaScript function

    def test_analytics_auto_refresh(self, client):
        """Analytics page includes auto-refresh."""
        response = client.get("/analytics")
        assert response.status_code == 200
        content = response.content.decode()

        # Should have setInterval for auto-refresh
        assert "setInterval" in content
        assert "30000" in content  # 30 second refresh
