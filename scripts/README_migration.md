# DeveloperHub → AccelDocs Migration Guide

## Overview

`migrate_developerhub.py` crawls the public Acceldata DeveloperHub documentation
site, converts every page to Markdown, and imports the full hierarchy into
AccelDocs with proper section grouping.

### What gets preserved

| Source element | How it lands in AccelDocs |
|---|---|
| Sidebar section heading (no URL) | Section in AccelDocs |
| Sidebar page link | Page under its section |
| DeveloperHub callout / alert boxes | MkDocs `!!! note/warning/danger/tip` admonition |
| DeveloperHub tab groups | MkDocs `=== "Tab"` tab syntax |
| Code blocks | Fenced code blocks (language preserved via pandoc) |
| Tables | GFM pipe tables |
| Images | Absolute URL (images stay hosted on source) |
| Internal links | `[[MIGRATED:slug]]` placeholder (resolved after import) |

> **Note on the Acceldata site**: docs.acceldata.io is a JavaScript-rendered SPA.
> The sidebar navigation cannot be extracted from static HTML, so the script falls
> back to `sitemap.xml` discovery. The built-in `ACCELDATA_CATEGORY_MAP` groups
> the flat sitemap URLs into proper sections (Getting Started, Core Concepts,
> Integrations, etc.).

---

## Prerequisites

### Python packages

```bash
pip install requests beautifulsoup4
```

Optional (better HTML→Markdown):

```bash
pip install html2text
```

### Pandoc (strongly recommended)

```bash
# macOS
brew install pandoc

# Ubuntu/Debian
sudo apt-get install pandoc

pandoc --version  # verify
```

### Google Drive (optional)

By default the script imports pages **without** Google Drive. Add
`--create-drive-docs` if you need pages to be editable in Google Docs later
(requires Drive to be connected in AccelDocs Settings → Integrations).

---

## Getting your AccelDocs JWT token

1. Log in to AccelDocs in your browser.
2. Open DevTools → Network tab → reload any API call (e.g. `/api/sections`).
3. Copy the `Authorization` header value — pass the part after "Bearer " to `--token`.

Or set the env var: `export ACCELDOCS_TOKEN="eyJhbGciOi..."`

---

## Workflow

### 1. Dry run — verify discovered hierarchy

```bash
python scripts/migrate_developerhub.py \
  --source https://docs.acceldata.io/documentation \
  --dry-run
```

Prints the full section hierarchy without touching AccelDocs.

### 2. Standard import (no Drive)

```bash
python scripts/migrate_developerhub.py \
  --source https://docs.acceldata.io/documentation \
  --backend https://your-backend.vercel.app \
  --token eyJhbGciOi... \
  --org-id 42 \
  --product-id 17
```

### 3. Import with Google Drive docs (editable later)

```bash
python scripts/migrate_developerhub.py \
  --source https://docs.acceldata.io/documentation \
  --backend https://your-backend.vercel.app \
  --token eyJhbGciOi... \
  --org-id 42 \
  --product-id 17 \
  --create-drive-docs
```

### 4. Resume after interruption

Add `--resume` to continue from where the last run left off.

---

## Full CLI reference

| Flag | Default | Description |
|---|---|---|
| `--source` | required | Public DeveloperHub docs URL |
| `--backend` | `http://localhost:8000` | AccelDocs backend URL |
| `--token` | env `ACCELDOCS_TOKEN` | JWT bearer token |
| `--org-id` | env `ACCELDOCS_ORG_ID` | AccelDocs org ID |
| `--product-id` | env `ACCELDOCS_PRODUCT_ID` | Section to import under |
| `--dry-run` | false | Print hierarchy only, no API calls |
| `--resume` | false | Skip already-imported pages |
| `--delay` | 0.5s | Seconds between page fetches |
| `--max-pages` | 0 (all) | Limit for testing |
| `--create-drive-docs` | false | Create Google Docs in Drive folders |
| `--no-category-map` | false | Use path-based hierarchy instead of Acceldata category map |

---

## Troubleshooting

**Rate limiting (429):** Add `--delay 2.0`

**Flat hierarchy / pages in "Other":** Check `migration.log`. Add the slug prefixes
to `ACCELDATA_CATEGORY_MAP` at the top of the script.

**Drive not connected error:** Either run without `--create-drive-docs`, or connect
Drive in AccelDocs Settings → Integrations first.

**Pandoc not found:** Install pandoc. Without it, callout/table rendering degrades.

---

## State files

| File | Purpose |
|---|---|
| `migration_state.json` | Full crawl + import progress (auto-saved every 10 pages) |
| `migration.log` | Timestamped log of every action |

Both are safe to delete to start a fresh import.

---

## Using Playwright for the real multi-level hierarchy (Recommended)

docs.acceldata.io has a **4-5 level deep sidebar hierarchy** (e.g. User Guide →
Data Reliability → Discover Assets → Asset Details → Find Similar Assets). Because
the site is a JavaScript SPA, the sidebar is invisible to plain HTTP crawlers.

The `--playwright` flag launches a headless Chromium browser that:
1. Navigates to the docs URL and waits for JS to render
2. Clicks open all collapsed sidebar sections to reveal every level
3. Extracts the full multi-level tree from the rendered DOM
4. Also uses the browser for page content fetching (no SSR issues)

### Setup

```bash
pip install playwright
playwright install chromium
```

### Usage

```bash
# Dry run first — see the real hierarchy
python scripts/migrate_developerhub.py \
  --source https://docs.acceldata.io/documentation \
  --playwright \
  --dry-run

# Full import with real hierarchy
python scripts/migrate_developerhub.py \
  --source https://docs.acceldata.io/documentation \
  --backend https://your-backend.vercel.app \
  --token eyJhbGciOi... \
  --org-id 42 \
  --product-id 17 \
  --playwright
```

### Comparison

| Mode | Hierarchy depth | Accuracy | Speed |
|---|---|---|---|
| `--playwright` | Full (4-5 levels) | Exact sidebar | ~2–3s/page |
| Sitemap + category map | 2 levels | Approximate grouping | ~0.5s/page |
| Static HTML | 1–2 levels | Only if sidebar is in HTML | ~0.5s/page |

If playwright is not installed the script falls back to the sitemap + category
map approach automatically.
