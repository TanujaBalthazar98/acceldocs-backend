#!/usr/bin/env python3
"""Migrate data from Strapi SQLite database to the new backend schema.

Usage:
    python scripts/migrate_strapi.py --strapi-db ../acceldocs/strapi/.tmp/data.db
"""

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from slugify import slugify


def migrate(strapi_db_path: str, target_db_path: str = "acceldocs.db") -> None:
    strapi = sqlite3.connect(strapi_db_path)
    strapi.row_factory = sqlite3.Row

    target = sqlite3.connect(target_db_path)
    target.execute("PRAGMA journal_mode=WAL")

    # Create target tables if they don't exist
    target.executescript("""
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY,
            google_doc_id TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            slug TEXT NOT NULL,
            project TEXT NOT NULL,
            version TEXT NOT NULL,
            section TEXT,
            visibility TEXT DEFAULT 'public',
            status TEXT DEFAULT 'draft',
            description TEXT,
            tags TEXT,
            drive_modified_at TEXT,
            last_synced_at TEXT,
            last_published_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            google_id TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            name TEXT,
            role TEXT DEFAULT 'viewer',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS approvals (
            id INTEGER PRIMARY KEY,
            document_id INTEGER REFERENCES documents(id),
            user_id INTEGER REFERENCES users(id),
            action TEXT NOT NULL,
            comment TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS sync_log (
            id INTEGER PRIMARY KEY,
            document_id INTEGER REFERENCES documents(id),
            action TEXT NOT NULL,
            branch TEXT,
            commit_sha TEXT,
            error TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # Migrate documents
    docs = strapi.execute("""
        SELECT d.*, p.name as project_name, p.slug as project_slug,
               v.name as version_name, v.slug as version_slug,
               t.name as topic_name
        FROM documents d
        LEFT JOIN projects p ON d.project_id = p.id
        LEFT JOIN project_versions v ON d.project_version_id = v.id
        LEFT JOIN topics t ON d.topic_id = t.id
    """).fetchall()

    now = datetime.now(timezone.utc).isoformat()
    migrated = 0

    for doc in docs:
        google_doc_id = doc["google_doc_id"]
        if not google_doc_id:
            continue

        status = "approved" if doc["is_published"] else "draft"
        project_name = doc["project_name"] or "unknown"
        version_name = doc["version_slug"] or doc["version_name"] or "v1.0"

        try:
            target.execute(
                """INSERT OR IGNORE INTO documents
                   (google_doc_id, title, slug, project, version, section,
                    visibility, status, drive_modified_at, last_synced_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    google_doc_id,
                    doc["title"] or "Untitled",
                    doc["slug"] or slugify(doc["title"] or "untitled"),
                    project_name,
                    version_name,
                    doc["topic_name"],
                    doc["visibility"] or "public",
                    status,
                    doc["google_modified_at"],
                    doc["last_synced_at"],
                    now,
                    now,
                ),
            )
            migrated += 1
        except Exception as e:
            print(f"  Skipped doc {google_doc_id}: {e}")

    target.commit()
    target.close()
    strapi.close()

    print(f"Migration complete: {migrated} documents migrated to {target_db_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate Strapi data to new backend")
    parser.add_argument("--strapi-db", required=True, help="Path to Strapi SQLite database")
    parser.add_argument("--target-db", default="acceldocs.db", help="Target database path")
    args = parser.parse_args()

    if not Path(args.strapi_db).exists():
        print(f"Error: Strapi database not found at {args.strapi_db}")
        exit(1)

    migrate(args.strapi_db, args.target_db)
