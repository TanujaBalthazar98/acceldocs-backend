"""SQLAlchemy ORM models."""

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    google_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(50), default="viewer")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    approvals: Mapped[list["Approval"]] = relationship(back_populates="user")
    document_views: Mapped[list["DocumentView"]] = relationship(back_populates="user")
    org_roles: Mapped[list["OrgRole"]] = relationship(back_populates="user")
    project_memberships: Mapped[list["ProjectMember"]] = relationship(back_populates="user")
    owned_documents: Mapped[list["Document"]] = relationship(back_populates="owner")
    sent_invitations: Mapped[list["Invitation"]] = relationship(back_populates="invited_by")
    join_requests: Mapped[list["JoinRequest"]] = relationship(back_populates="user")
    audit_logs: Mapped[list["AuditLog"]] = relationship(back_populates="user")
    google_tokens: Mapped[list["GoogleToken"]] = relationship(back_populates="user")


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str | None] = mapped_column(String(255), unique=True)
    domain: Mapped[str | None] = mapped_column(String(255), unique=True)
    custom_docs_domain: Mapped[str | None] = mapped_column(String(255))
    subdomain: Mapped[str | None] = mapped_column(String(255))
    logo_url: Mapped[str | None] = mapped_column(String(500))
    tagline: Mapped[str | None] = mapped_column(Text)
    primary_color: Mapped[str | None] = mapped_column(String(50))
    secondary_color: Mapped[str | None] = mapped_column(String(50))
    accent_color: Mapped[str | None] = mapped_column(String(50))
    font_heading: Mapped[str | None] = mapped_column(String(100))
    font_body: Mapped[str | None] = mapped_column(String(100))
    custom_css: Mapped[str | None] = mapped_column(Text)
    hero_title: Mapped[str | None] = mapped_column(String(500))
    hero_description: Mapped[str | None] = mapped_column(Text)
    hierarchy_mode: Mapped[str] = mapped_column(String(20), default="product")
    show_search_on_landing: Mapped[bool] = mapped_column(Boolean, default=True)
    show_featured_projects: Mapped[bool] = mapped_column(Boolean, default=True)
    custom_links: Mapped[str | None] = mapped_column(Text)  # JSON string
    mcp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    openapi_spec_json: Mapped[str | None] = mapped_column(Text)
    openapi_spec_url: Mapped[str | None] = mapped_column(String(500))
    drive_folder_id: Mapped[str | None] = mapped_column(String(255))
    drive_permissions_last_synced_at: Mapped[str | None] = mapped_column(String(50))
    # GitHub publishing
    github_username: Mapped[str | None] = mapped_column(String(255))
    github_token_encrypted: Mapped[str | None] = mapped_column(Text)
    github_repo_name: Mapped[str | None] = mapped_column(String(255))
    github_repo_full_name: Mapped[str | None] = mapped_column(String(500))
    github_pages_url: Mapped[str | None] = mapped_column(String(500))
    github_custom_domain: Mapped[str | None] = mapped_column(String(255))
    github_domain_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    last_published_at: Mapped[str | None] = mapped_column(String(50))
    # Zensical / docs-site configuration
    analytics_property_id: Mapped[str | None] = mapped_column(String(50))  # Google Analytics G-XXXXXXXX
    copyright: Mapped[str | None] = mapped_column(String(500))
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    projects: Mapped[list["Project"]] = relationship(back_populates="organization")
    org_roles: Mapped[list["OrgRole"]] = relationship(back_populates="organization")
    invitations: Mapped[list["Invitation"]] = relationship(back_populates="organization")
    join_requests: Mapped[list["JoinRequest"]] = relationship(back_populates="organization")
    document_caches: Mapped[list["DocumentCache"]] = relationship(back_populates="organization")
    domains: Mapped[list["Domain"]] = relationship(back_populates="organization")
    google_tokens: Mapped[list["GoogleToken"]] = relationship(back_populates="organization")
    external_access_grants: Mapped[list["ExternalAccessGrant"]] = relationship(back_populates="organization")


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    drive_folder_id: Mapped[str | None] = mapped_column(String(255))
    drive_parent_id: Mapped[str | None] = mapped_column(String(255))
    visibility: Mapped[str] = mapped_column(String(50), default="internal")  # internal/external/public
    is_published: Mapped[bool] = mapped_column(Boolean, default=False)
    show_version_switcher: Mapped[bool] = mapped_column(Boolean, default=True)
    default_visibility: Mapped[str] = mapped_column(String(50), default="public")
    require_approval: Mapped[bool] = mapped_column(Boolean, default=True)
    organization_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"))
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"))
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    organization: Mapped["Organization | None"] = relationship(back_populates="projects")
    parent: Mapped["Project | None"] = relationship("Project", remote_side=[id], back_populates="children")
    children: Mapped[list["Project"]] = relationship("Project", back_populates="parent")
    versions: Mapped[list["ProjectVersion"]] = relationship(back_populates="project")
    topics: Mapped[list["Topic"]] = relationship(back_populates="project")
    documents: Mapped[list["Document"]] = relationship(back_populates="project_rel")
    members: Mapped[list["ProjectMember"]] = relationship(back_populates="project")
    invitations: Mapped[list["Invitation"]] = relationship(back_populates="project")
    audit_logs: Mapped[list["AuditLog"]] = relationship(back_populates="project")
    domains: Mapped[list["Domain"]] = relationship(back_populates="project")


class ProjectVersion(Base):
    __tablename__ = "project_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    is_published: Mapped[bool] = mapped_column(Boolean, default=False)
    semver_major: Mapped[int | None] = mapped_column(Integer)
    semver_minor: Mapped[int | None] = mapped_column(Integer)
    semver_patch: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    project: Mapped["Project"] = relationship(back_populates="versions")
    topics: Mapped[list["Topic"]] = relationship(back_populates="project_version")
    documents: Mapped[list["Document"]] = relationship(back_populates="project_version")


class Topic(Base):
    __tablename__ = "topics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False)
    project_version_id: Mapped[int | None] = mapped_column(ForeignKey("project_versions.id"))
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("topics.id"))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, default=0)
    drive_folder_id: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    project: Mapped["Project"] = relationship(back_populates="topics")
    project_version: Mapped["ProjectVersion | None"] = relationship(back_populates="topics")
    parent: Mapped["Topic | None"] = relationship("Topic", remote_side=[id], back_populates="children")
    children: Mapped[list["Topic"]] = relationship("Topic", back_populates="parent")
    documents: Mapped[list["Document"]] = relationship(back_populates="topic")


class OrgRole(Base):
    __tablename__ = "org_roles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    role: Mapped[str] = mapped_column(String(50), nullable=False)  # owner/admin/editor/viewer
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    organization: Mapped["Organization"] = relationship(back_populates="org_roles")
    user: Mapped["User"] = relationship(back_populates="org_roles")


class ProjectMember(Base):
    __tablename__ = "project_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    role: Mapped[str] = mapped_column(String(50), nullable=False)  # admin/editor/reviewer/viewer
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    project: Mapped["Project"] = relationship(back_populates="members")
    user: Mapped["User"] = relationship(back_populates="project_memberships")


class Invitation(Base):
    __tablename__ = "invitations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"))
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"))
    invited_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(50), nullable=False)
    token: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    organization: Mapped["Organization | None"] = relationship(back_populates="invitations")
    project: Mapped["Project | None"] = relationship(back_populates="invitations")
    invited_by: Mapped["User"] = relationship(back_populates="sent_invitations", foreign_keys=[invited_by_id])


class JoinRequest(Base):
    __tablename__ = "join_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    message: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(50), default="pending")  # pending/approved/rejected
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    organization: Mapped["Organization"] = relationship(back_populates="join_requests")
    user: Mapped["User"] = relationship(back_populates="join_requests")


class DocumentCache(Base):
    __tablename__ = "document_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), nullable=False)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), nullable=False)
    content_html_encrypted: Mapped[str | None] = mapped_column(Text)
    content_text_encrypted: Mapped[str | None] = mapped_column(Text)
    headings_encrypted: Mapped[str | None] = mapped_column(Text)
    published_content_html_encrypted: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    document: Mapped["Document"] = relationship(back_populates="cache")
    organization: Mapped["Organization"] = relationship(back_populates="document_caches")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"))
    action: Mapped[str] = mapped_column(String(255), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(100))
    entity_id: Mapped[int | None] = mapped_column(Integer)
    audit_metadata: Mapped[str | None] = mapped_column("metadata", Text)  # JSON string, column name is "metadata"
    success: Mapped[bool] = mapped_column(Boolean, default=True)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    user: Mapped["User | None"] = relationship(back_populates="audit_logs")
    project: Mapped["Project | None"] = relationship(back_populates="audit_logs")


class Domain(Base):
    __tablename__ = "domains"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), nullable=False)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"))
    domain: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    domain_type: Mapped[str] = mapped_column(String(50), nullable=False)  # custom/subdomain
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    verification_token: Mapped[str | None] = mapped_column(String(255))
    ssl_status: Mapped[str | None] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    organization: Mapped["Organization"] = relationship(back_populates="domains")
    project: Mapped["Project | None"] = relationship(back_populates="domains")


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    google_doc_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    slug: Mapped[str] = mapped_column(String(500), nullable=False)
    project: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[str] = mapped_column(String(100), nullable=False)
    section: Mapped[str | None] = mapped_column(String(255))
    visibility: Mapped[str] = mapped_column(String(50), default="public")
    status: Mapped[str] = mapped_column(String(50), default="draft")
    description: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[str | None] = mapped_column(Text)
    # New FK relationships
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"))
    project_version_id: Mapped[int | None] = mapped_column(ForeignKey("project_versions.id"))
    topic_id: Mapped[int | None] = mapped_column(ForeignKey("topics.id"))
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    # Content fields
    is_published: Mapped[bool] = mapped_column(Boolean, default=False)
    content_html: Mapped[str | None] = mapped_column(Text)
    published_content_html: Mapped[str | None] = mapped_column(Text)
    content_id: Mapped[str | None] = mapped_column(String(255))
    published_content_id: Mapped[str | None] = mapped_column(String(255))
    # Media fields
    video_url: Mapped[str | None] = mapped_column(String(500))
    video_title: Mapped[str | None] = mapped_column(String(255))
    display_order: Mapped[int] = mapped_column(Integer, default=0)
    # Timestamp fields
    google_modified_at: Mapped[str | None] = mapped_column(String(50))
    drive_modified_at: Mapped[str | None] = mapped_column(String(50))
    last_synced_at: Mapped[str | None] = mapped_column(String(50))
    last_published_at: Mapped[str | None] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    # Relationships
    project_rel: Mapped["Project | None"] = relationship(back_populates="documents")
    project_version: Mapped["ProjectVersion | None"] = relationship(back_populates="documents")
    topic: Mapped["Topic | None"] = relationship(back_populates="documents")
    owner: Mapped["User | None"] = relationship(back_populates="owned_documents")
    approvals: Mapped[list["Approval"]] = relationship(back_populates="document")
    sync_logs: Mapped[list["SyncLog"]] = relationship(back_populates="document")
    views: Mapped[list["DocumentView"]] = relationship(back_populates="document")
    cache: Mapped["DocumentCache | None"] = relationship(back_populates="document", uselist=False)


class Approval(Base):
    __tablename__ = "approvals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), nullable=False)
    # "document" (legacy) or "page" (current clean-arch flow)
    entity_type: Mapped[str | None] = mapped_column(String(20))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    document: Mapped["Document"] = relationship(back_populates="approvals")
    user: Mapped["User"] = relationship(back_populates="approvals")


class SyncLog(Base):
    __tablename__ = "sync_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), nullable=False)
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    branch: Mapped[str | None] = mapped_column(String(100))
    commit_sha: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    document: Mapped["Document"] = relationship(back_populates="sync_logs")


class DocumentView(Base):
    """Track individual document views for analytics."""
    __tablename__ = "document_views"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), nullable=False)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))  # Null for anonymous
    user_email: Mapped[str | None] = mapped_column(String(255))  # For authenticated users
    ip_address: Mapped[str | None] = mapped_column(String(50))
    user_agent: Mapped[str | None] = mapped_column(Text)
    referer: Mapped[str | None] = mapped_column(Text)
    viewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)

    document: Mapped["Document"] = relationship(back_populates="views")
    user: Mapped["User | None"] = relationship(back_populates="document_views")


class GoogleToken(Base):
    """Store encrypted Google OAuth refresh tokens for Drive API access.

    Each user can have one refresh token per organization for Google Drive integration.
    Tokens are encrypted at rest using Fernet symmetric encryption.
    """
    __tablename__ = "google_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    encrypted_refresh_token: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    token_created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    # Relationships
    user: Mapped["User"] = relationship(back_populates="google_tokens")
    organization: Mapped["Organization"] = relationship(back_populates="google_tokens")

    # Unique constraint: one token per user per organization
    __table_args__ = (
        UniqueConstraint("user_id", "organization_id", name="unique_user_org_token"),
    )


# ---------------------------------------------------------------------------
# Clean architecture models — sections and pages replace the old
# Project / Topic / Document hierarchy.
# ---------------------------------------------------------------------------

class Section(Base):
    """A section groups related pages. Sections can nest (parent_id for sub-sections).

    Top-level sections map to "products" or "books"; sub-sections map to chapters/topics.
    Each section can optionally be backed by a Google Drive folder.
    """
    __tablename__ = "sections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("sections.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False)
    # "section" = regular nested section, "tab" = top-level tab inside a product section
    section_type: Mapped[str] = mapped_column(String(50), default="section")
    visibility: Mapped[str] = mapped_column(String(20), default="public")
    drive_folder_id: Mapped[str | None] = mapped_column(String(255))
    display_order: Mapped[int] = mapped_column(Integer, default=0)
    is_published: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    organization: Mapped["Organization"] = relationship("Organization", foreign_keys=[organization_id])
    parent: Mapped["Section | None"] = relationship("Section", remote_side=[id], back_populates="children")
    children: Mapped[list["Section"]] = relationship("Section", back_populates="parent", order_by="Section.display_order")
    pages: Mapped[list["Page"]] = relationship(back_populates="section", cascade="all, delete-orphan", order_by="Page.display_order")

    __table_args__ = (
        UniqueConstraint("organization_id", "parent_id", "slug", name="uq_section_slug_within_parent"),
    )


class Page(Base):
    """A single documentation page, sourced from a Google Doc.

    html_content is updated on every Drive sync.
    published_html is a snapshot taken when the page is published — it is what
    end-users see and is never modified by sync.
    """
    __tablename__ = "pages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    section_id: Mapped[int | None] = mapped_column(ForeignKey("sections.id", ondelete="SET NULL"))
    google_doc_id: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    slug: Mapped[str] = mapped_column(String(500), nullable=False)
    slug_locked: Mapped[bool] = mapped_column(Boolean, default=False)
    visibility_override: Mapped[str | None] = mapped_column(String(20))
    html_content: Mapped[str | None] = mapped_column(Text)          # latest synced from Drive
    published_html: Mapped[str | None] = mapped_column(Text)        # snapshot at publish time
    search_text: Mapped[str | None] = mapped_column(Text)            # plain text for full-text search
    is_published: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(50), default="draft")  # draft | published
    display_order: Mapped[int] = mapped_column(Integer, default=0)
    drive_modified_at: Mapped[str | None] = mapped_column(String(50))
    last_synced_at: Mapped[str | None] = mapped_column(String(50))
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    organization: Mapped["Organization"] = relationship("Organization", foreign_keys=[organization_id])
    section: Mapped["Section | None"] = relationship(back_populates="pages")
    owner: Mapped["User | None"] = relationship("User", foreign_keys=[owner_id])

    __table_args__ = (
        UniqueConstraint("organization_id", "google_doc_id", name="uq_page_google_doc_per_org"),
        UniqueConstraint("organization_id", "slug", name="uq_page_slug_per_org"),
        Index("ix_pages_org_visibility_override", "organization_id", "visibility_override"),
    )


class PageFeedback(Base):
    """Per-page feedback votes with optional freeform notes."""
    __tablename__ = "page_feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    page_id: Mapped[int] = mapped_column(ForeignKey("pages.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    user_email: Mapped[str | None] = mapped_column(String(255))
    vote: Mapped[str] = mapped_column(String(16), nullable=False)  # up | down
    message: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(String(20))  # public | internal | external
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        Index("ix_page_feedback_org_page_vote", "organization_id", "page_id", "vote"),
        Index("ix_page_feedback_page_created", "page_id", "created_at"),
    )


class PageComment(Base):
    """End-user comments attached to rendered documentation pages."""
    __tablename__ = "page_comments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    page_id: Mapped[int] = mapped_column(ForeignKey("pages.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    user_email: Mapped[str | None] = mapped_column(String(255))
    display_name: Mapped[str | None] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str | None] = mapped_column(String(20))  # public | internal | external
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        Index("ix_page_comments_org_page_created", "organization_id", "page_id", "created_at"),
        Index("ix_page_comments_page_deleted", "page_id", "is_deleted"),
    )


class PageRedirect(Base):
    """Redirect map for historical/retired page links.

    Used to preserve old links when slugs change or pages are deleted.
    """
    __tablename__ = "page_redirects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    source_page_id: Mapped[int | None] = mapped_column(Integer)
    source_slug: Mapped[str] = mapped_column(String(500), nullable=False)
    target_page_id: Mapped[int | None] = mapped_column(Integer)
    target_url: Mapped[str | None] = mapped_column(String(1000))
    status_code: Mapped[int] = mapped_column(Integer, default=307)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        UniqueConstraint(
            "organization_id", "source_page_id", "source_slug", name="uq_page_redirect_source_page_slug"
        ),
        Index("ix_page_redirect_org_slug_active", "organization_id", "source_slug", "is_active"),
        Index("ix_page_redirect_org_page_active", "organization_id", "source_page_id", "is_active"),
    )


class ExternalAccessGrant(Base):
    """Allow an external email to access external docs for an organization."""
    __tablename__ = "external_access_grants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    organization: Mapped["Organization"] = relationship(back_populates="external_access_grants")
    created_by_user: Mapped["User | None"] = relationship("User", foreign_keys=[created_by_user_id])

    __table_args__ = (
        UniqueConstraint("organization_id", "email", name="uq_external_access_org_email"),
        Index("ix_external_access_org_active", "organization_id", "is_active"),
    )


class JiraCredential(Base):
    """Encrypted Jira API token per user per org."""
    __tablename__ = "jira_credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    jira_domain: Mapped[str] = mapped_column(String(255), nullable=False)
    jira_email: Mapped[str] = mapped_column(String(255), nullable=False)
    encrypted_api_token: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    user: Mapped["User"] = relationship("User", foreign_keys=[user_id])
    organization: Mapped["Organization"] = relationship("Organization", foreign_keys=[organization_id])

    __table_args__ = (
        UniqueConstraint("user_id", "organization_id", name="uq_jira_credential_user_org"),
    )
