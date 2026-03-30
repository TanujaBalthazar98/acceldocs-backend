# DeveloperHub → AccelDocs Migration Guide

## Overview

`migrate_developerhub.py` crawls any public DeveloperHub documentation site,
converts every page to Markdown, and imports the full hierarchy into AccelDocs.

---

## Prerequisites

### Python packages

```bash
pip install requests beautifulsoup4
```

Optional (better HTML→Markdown conversion):

```bash
pip install html2text
```

### Pandoc (recommended)

Pandoc produces much cleaner Markdown than the Python fallback.

```bash
# macOS
brew install pandoc

# Ubuntu/Debian
sudo apt-get install pandoc

# Check it works
pandoc --version
```

The script auto-detects pandoc. If it isn't found it falls back to
`html2text` (if installed) or a basic BeautifulSoup extractor.

### Google Drive configured in AccelDocs

Page import uses AccelDocs' `/api/drive/import/local` endpoint, which uploads
Markdown files as Google Docs. Your AccelDocs workspace **must** have Google
Drive connected before running a live import.

---

## How to get your AccelDocs JWT token

1. Log in to AccelDocs in your browser.
2. Open DevTools → Network tab.
3. Reload any API call (e.g. `/api/sections`).
4. Copy the `Authorization` header value — it looks like `Bearer eyJ...`.
5. Pass just the token part (without "Bearer ") to `--token`.

Alternatively, set the environment variable:

```bash
export ACCELDOCS_TOKEN="eyJhbGciOi..."
```

---

## How to find org-id and product-id

Both IDs appear in the AccelDocs URL when you have a product open:

```
https://app.acceldocs.io/org/42/product/17/...
                              ↑          ↑
                           org-id    product-id
```

You can also copy them from the browser DevTools Network tab — they appear as
`X-Org-Id` in request headers and as `parent_id` / `section_id` in API responses.

Set them as environment variables if you run multiple imports:

```bash
export ACCELDOCS_ORG_ID=42
export ACCELDOCS_PRODUCT_ID=17
```

---

## Workflow

### 1. Dry run first — verify discovered structure

```bash
python scripts/migrate_developerhub.py \
  --source https://docs.acceldata.io/documentation \
  --dry-run
```

This prints the full navigation hierarchy and page count **without** touching
AccelDocs. Use this to confirm the script is reading the sidebar correctly.

Sample output:

```
============================================================
DRY RUN — Source: https://docs.acceldata.io/documentation
============================================================

Discovered 87 page URLs
Top-level tree nodes: 8

Navigation Hierarchy:
----------------------------------------
• Getting Started → https://docs.acceldata.io/documentation/getting-started
  ├─ Installation → https://docs.acceldata.io/documentation/installation
  ├─ Configuration → https://docs.acceldata.io/documentation/configuration
• Reference → (section heading)
  ├─ API Reference → https://docs.acceldata.io/documentation/api-reference
  ...
```

### 2. Run the full import

```bash
python scripts/migrate_developerhub.py \
  --source https://docs.acceldata.io/documentation \
  --backend http://localhost:8000 \
  --token eyJhbGciOi... \
  --org-id 42 \
  --product-id 17
```

Progress is logged to stdout and to `migration.log`. State is saved to
`migration_state.json` after every 10 pages so you can resume if interrupted.

### 3. Resume an interrupted import

If the import is interrupted (network error, timeout, etc.), re-run with
`--resume`:

```bash
python scripts/migrate_developerhub.py \
  --source https://docs.acceldata.io/documentation \
  --backend http://localhost:8000 \
  --token eyJhbGciOi... \
  --org-id 42 \
  --product-id 17 \
  --resume
```

The script loads `migration_state.json`, skips pages that were already
successfully imported, and continues from where it left off.

---

## Full CLI reference

```
usage: migrate_developerhub.py [-h] --source SOURCE [--backend BACKEND]
                                [--token TOKEN] [--org-id ORG_ID]
                                [--product-id PRODUCT_ID] [--dry-run]
                                [--resume] [--delay DELAY]
                                [--max-pages MAX_PAGES]

options:
  --source SOURCE       Public DeveloperHub docs URL to crawl (required)
  --backend BACKEND     AccelDocs backend URL (default: http://localhost:8000)
  --token TOKEN         JWT token (or set ACCELDOCS_TOKEN env var)
  --org-id ORG_ID       AccelDocs org ID (or set ACCELDOCS_ORG_ID env var)
  --product-id          AccelDocs product/section ID to import under
  --dry-run             Crawl only, print hierarchy, no API calls
  --resume              Load migration_state.json and skip completed items
  --delay DELAY         Seconds between page fetches (default: 0.5)
  --max-pages N         Limit pages fetched (0 = unlimited, useful for testing)
```

---

## What gets imported

| DeveloperHub concept | AccelDocs equivalent |
|---|---|
| Sidebar section heading (no URL) | Section (`POST /api/sections`) |
| Sidebar section heading (with URL) | Section + page inside it |
| Leaf page (with URL) | Page via Drive import (`POST /api/drive/import/local`) |
| Nested sections | Nested sections (parent_id set correctly) |
| Tabs in page content | Converted to MkDocs `===` tab syntax in Markdown |
| Internal links | Rewritten to `[[MIGRATED:slug]]` placeholders |

---

## Troubleshooting

### Wrong sections detected / sidebar not found

The script tries these selectors in order:

1. `nav[aria-label]`
2. `.sidebar-nav`
3. `[class*="sidebar"] nav`
4. `[class*="navigation"]`
5. `nav` (generic fallback)

Check `migration.log` to see which selector was used. If the wrong element is
picked, delete `migration_state.json` and re-run. You can also fork the script
and add a custom selector at the top of `_SIDEBAR_SELECTORS`.

### Drive not connected error

The `POST /api/drive/import/local` endpoint requires Google Drive to be
configured. Go to AccelDocs Settings → Integrations → Connect Google Drive
before running a live import.

### Pages upload 0 files

Check `migration.log` for `failed_file_errors`. Common causes:

- Drive quota exceeded
- Section not linked to a Drive folder — try creating the section manually in
  AccelDocs first, then use its ID as `--product-id`
- File name collision — the script uses `<slug>.md` as the filename; if a file
  with the same name already exists in the Drive folder the upload may silently
  skip it

### Pandoc not found warning

Install pandoc (see Prerequisites). The fallback conversion is functional but
may lose some formatting (tables, complex code blocks, etc.).

### Rate limiting (429 responses)

Increase `--delay`:

```bash
python scripts/migrate_developerhub.py ... --delay 2.0
```

---

## State files

| File | Purpose |
|---|---|
| `migration_state.json` | Full crawl + import progress (auto-generated) |
| `migration.log` | Timestamped log of every action |

Both files are safe to delete to start a fresh import.

---

## Internal link resolution

During crawl the script rewrites internal links to `[[MIGRATED:slug]]`
placeholders. These are preserved in the imported Markdown. After all pages
are imported you can do a second pass to replace them with actual AccelDocs
page URLs — the mapping from old URL to AccelDocs page ID is stored in
`migration_state.json` under `page_id_map`.
