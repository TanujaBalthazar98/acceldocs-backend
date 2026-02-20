# Jinja2 Dashboard Enhancement Plan

Complete feature parity with the React app for the new Drive-first architecture.

---

## Feature 1: User Management 👥

**What:** CRUD interface for managing users, roles, and permissions

**Pages:**
- `/users` — List all users with roles
- `/users/{id}` — View/edit user details
- `/users/new` — Invite new user (sends email)

**API Endpoints:**
- `GET /api/users` — List all users
- `GET /api/users/{id}` — Get user details
- `POST /api/users` — Create/invite user
- `PUT /api/users/{id}` — Update user role
- `DELETE /api/users/{id}` — Remove user

**UI Components:**
- Users table with role badges (Owner, Admin, Editor, Reviewer, Viewer)
- Role dropdown with RBAC validation (can't demote yourself, can't assign higher role than yours)
- Invite form with email input and role selection
- Delete confirmation modal

**Implementation Steps:**
1. Create `app/api/users.py` with CRUD routes
2. Create `app/templates/users.html` with user list table
3. Create `app/templates/user_detail.html` for editing
4. Add invite functionality (email via SendGrid or SMTP)
5. Add role change validation using existing `app/lib/rbac.py`
6. Add to sidebar navigation

**Estimated Lines:** ~300 (API: 120, Templates: 150, Tests: 30)

---

## Feature 2: Settings Page ⚙️

**What:** Web UI to configure system settings without editing .env files

**Page:**
- `/settings` — Single page with tabbed sections

**Sections:**
1. **Google Drive** — Root folder ID, service account upload, OAuth token refresh
2. **Git Publishing** — Docs repo path, remote URL, branch names
3. **Authentication** — Google OAuth client ID/secret, JWT secret rotation
4. **Netlify** — Site ID, auth token, manual deploy trigger
5. **System** — Database backup/restore, log level, allowed origins

**API Endpoints:**
- `GET /api/settings` — Get all settings (redact secrets)
- `PUT /api/settings` — Update settings (validate and save to .env or DB)
- `POST /api/settings/test-drive` — Test Drive connection
- `POST /api/settings/backup-db` — Create DB backup
- `POST /api/settings/deploy-netlify` — Trigger Netlify deploy

**UI Components:**
- Tabbed interface (Bootstrap tabs or custom CSS)
- Form inputs with validation
- "Test Connection" buttons for Drive/Netlify
- Secret fields with show/hide toggle
- Save confirmation toasts

**Implementation Steps:**
1. Create settings model/storage (`.env` writer or new `settings` table)
2. Create `app/api/settings.py` with get/update routes
3. Create `app/templates/settings.html` with tabbed form
4. Add `.env` file parser and writer utilities
5. Add connection test endpoints
6. Add validation for all settings fields
7. Add to sidebar navigation (admin-only)

**Estimated Lines:** ~400 (API: 150, Templates: 200, Utils: 50)

---

## Feature 3: Document Preview 📄

**What:** Render markdown preview of a document before approving

**Pages:**
- `/documents/{id}/preview` — Full-page markdown preview
- `/documents` — Add "Preview" button to each row

**API Endpoints:**
- `GET /api/documents/{id}/preview` — Get rendered HTML from markdown
- `GET /api/documents/{id}/raw` — Get raw markdown source

**UI Components:**
- Modal or full-page preview with styled markdown
- Side-by-side view (markdown source + rendered HTML)
- "Approve from Preview" button
- Syntax highlighting for code blocks (Prism.js or Highlight.js)

**Implementation Steps:**
1. Add markdown → HTML rendering endpoint in `app/api/documents.py`
2. Create `app/templates/document_preview.html` with preview UI
3. Add CSS for markdown styling (GitHub-style or Material-style)
4. Add syntax highlighting library for code blocks
5. Add "Preview" button to documents table
6. Add approve/reject actions from preview page

**Estimated Lines:** ~250 (API: 50, Templates: 150, CSS: 50)

---

## Feature 4: Search & Advanced Filters 🔍

**What:** Full-text search and multi-criteria filtering

**Enhancements to `/documents`:**
- Search bar (searches title, description, tags, content)
- Filter by: project, version, status, visibility, date range
- Sort by: title, last synced, last published, modified date

**API Endpoints:**
- `GET /api/documents/search?q={query}` — Full-text search
- `GET /api/documents?filters={json}` — Multi-criteria filter

**UI Components:**
- Search input with autocomplete suggestions
- Advanced filters panel (collapsible)
- Sort dropdown
- Clear filters button
- Search result highlighting

**Implementation Steps:**
1. Update `app/api/documents.py` with search endpoint
2. Add full-text search using SQLite FTS5 or simple LIKE queries
3. Update `app/templates/documents.html` with search UI
4. Add JavaScript for live search and filter updates
5. Add search result highlighting
6. Add pagination for large result sets

**Estimated Lines:** ~300 (API: 100, Templates: 150, JS: 50)

---

## Feature 5: Bulk Actions 🔄

**What:** Select multiple documents and perform actions

**Actions:**
- Approve all selected
- Reject all selected
- Set status (draft/review/approved/rejected)
- Delete all selected (with confirmation)
- Change visibility (public/internal)
- Re-sync selected from Drive

**UI Components:**
- Checkboxes on document rows
- "Select All" checkbox in header
- Bulk action dropdown (appears when items selected)
- Confirmation modal for destructive actions
- Progress indicator for bulk operations

**Implementation Steps:**
1. Add `POST /api/documents/bulk` endpoint with action parameter
2. Add checkbox column to documents table
3. Add JavaScript for checkbox selection state
4. Add bulk action dropdown with action buttons
5. Add confirmation modal for destructive actions
6. Add progress/status feedback for long operations
7. Add transaction handling for atomic bulk updates

**Estimated Lines:** ~350 (API: 120, Templates: 150, JS: 80)

---

## Feature 6: Sync History 📜

**What:** Detailed log viewer for sync runs with error tracking

**Page:**
- `/sync/history` — List of all sync runs with stats

**Display:**
- Sync run timestamp
- Status (success/partial/failed)
- Stats (created, updated, skipped, errors)
- Duration
- Error details (expandable)
- Filtered by: date range, status, errors only

**API Endpoints:**
- `GET /api/sync/history` — List sync runs (from sync_log table)
- `GET /api/sync/history/{run_id}` — Get detailed run log
- `DELETE /api/sync/history/{run_id}` — Delete old logs (cleanup)

**UI Components:**
- Timeline view of sync runs
- Expandable error details
- Stats summary cards (total runs, success rate, avg duration)
- Filter/date picker for historical runs
- Export to CSV button

**Implementation Steps:**
1. Update sync pipeline to create run-level log records (new `sync_runs` table)
2. Create `app/api/sync_history.py` with list/detail routes
3. Create `app/templates/sync_history.html` with timeline UI
4. Add expandable error details panels
5. Add stats aggregation queries
6. Add export functionality
7. Add to sidebar navigation

**Estimated Lines:** ~400 (API: 120, Templates: 200, DB: 50, Tests: 30)

---

## Feature 7: Google Drive Browser 🗂️

**What:** Visual folder tree browser with manual sync triggers

**Page:**
- `/drive` — Browse Drive folder structure

**Features:**
- Tree view of folders starting from root
- Show file counts per folder
- Expand/collapse folders
- Manual sync button per folder (or individual file)
- Link to open in Google Drive
- Show sync status (last synced time, errors)

**API Endpoints:**
- `GET /api/drive/tree` — Get folder tree structure
- `GET /api/drive/folder/{id}` — Get folder contents
- `POST /api/drive/sync/{file_id}` — Sync specific file
- `POST /api/drive/sync-folder/{folder_id}` — Sync folder

**UI Components:**
- Tree view component (jstree or custom)
- Folder/file icons
- Sync status badges
- Right-click context menu (sync, open in Drive, view details)
- Loading states for async operations

**Implementation Steps:**
1. Create `app/api/drive_browser.py` with tree/sync routes
2. Reuse `app/ingestion/drive.py` for tree building
3. Create `app/templates/drive_browser.html` with tree UI
4. Add JavaScript tree view library or custom implementation
5. Add single-file sync functionality
6. Add folder-level sync
7. Add status indicators and last-sync timestamps
8. Add to sidebar navigation

**Estimated Lines:** ~500 (API: 150, Templates: 250, JS: 100)

---

## Implementation Order

**Phase 1 (Core Admin):**
1. User Management — Required for production multi-user setup
2. Settings Page — Removes dependency on .env file editing

**Phase 2 (Content Workflow):**
3. Document Preview — Improves approval workflow quality
4. Search & Filters — Essential for large doc sets

**Phase 3 (Power Features):**
5. Bulk Actions — Efficiency for managing many docs
6. Sync History — Debugging and monitoring

**Phase 4 (Advanced):**
7. Drive Browser — Nice-to-have for manual control

---

## Total Estimated Effort

- **Lines of Code:** ~2,500 (API: 810, Templates: 1,250, JS: 280, Utils: 100, Tests: 60)
- **New Files:** 15 (7 templates, 5 API modules, 3 utility modules)
- **Database Changes:** 2 new tables (`sync_runs`, possibly `settings`)
- **Dependencies:** Possibly jstree, Prism.js, or Highlight.js
- **Time Estimate:** 1-2 features per session

---

## Next Step

**Start with Feature 1: User Management?**

This is the foundation for multi-user production deployment. Once we have user CRUD working, all other features can use proper role-based access control.

Ready to begin?
