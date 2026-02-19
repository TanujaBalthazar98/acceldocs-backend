"""SQLAlchemy ORM models."""

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
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
    drive_modified_at: Mapped[str | None] = mapped_column(String(50))
    last_synced_at: Mapped[str | None] = mapped_column(String(50))
    last_published_at: Mapped[str | None] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    approvals: Mapped[list["Approval"]] = relationship(back_populates="document")
    sync_logs: Mapped[list["SyncLog"]] = relationship(back_populates="document")


class Approval(Base):
    __tablename__ = "approvals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), nullable=False)
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
