"""Built-in documentation templates for AI-powered document generation."""

from __future__ import annotations

BUILTIN_TEMPLATES: list[dict] = [
    {
        "name": "API Reference",
        "slug": "api-reference",
        "category": "api",
        "description": "Document a REST API endpoint with parameters, examples, and error codes.",
        "content": """# {title}

## Overview

Brief description of what this endpoint does.

## Request

**Method**: `GET` / `POST` / `PUT` / `DELETE`
**URL**: `/api/v1/{resource}`

### Headers

| Header | Required | Description |
|--------|----------|-------------|
| Authorization | Yes | Bearer token |

### Parameters

| Name | Type | Required | Description |
|------|------|----------|-------------|
| id | string | Yes | Resource identifier |

### Request Body

```json
{{}}
```

## Response

### Success (200)

```json
{{}}
```

### Errors

| Status | Description |
|--------|-------------|
| 400 | Bad request — invalid parameters |
| 401 | Unauthorized — missing or invalid token |
| 404 | Not found |

## Examples

### cURL

```bash
curl -X GET https://api.example.com/v1/resource \\
  -H "Authorization: Bearer YOUR_TOKEN"
```
""",
    },
    {
        "name": "Getting Started",
        "slug": "getting-started",
        "category": "guide",
        "description": "Introductory guide with prerequisites, installation, and first steps.",
        "content": """# {title}

## Overview

What this product/feature is and why it matters.

## Prerequisites

- Requirement 1
- Requirement 2

## Installation

Step-by-step installation instructions.

## Quick Start

### Step 1: Set up

Description of the first step.

### Step 2: Configure

Description of the second step.

### Step 3: Verify

How to verify everything is working.

## Next Steps

- Link to related documentation
- Link to advanced guides
""",
    },
    {
        "name": "FAQ",
        "slug": "faq",
        "category": "reference",
        "description": "Frequently asked questions in Q&A format.",
        "content": """# {title}

## General

### What is this?

Answer to the question.

### How does it work?

Answer to the question.

## Setup & Configuration

### How do I install it?

Answer to the question.

### What are the system requirements?

Answer to the question.

## Troubleshooting

### Why is it not working?

Answer to the question.

### How do I reset my configuration?

Answer to the question.
""",
    },
    {
        "name": "Changelog",
        "slug": "changelog",
        "category": "reference",
        "description": "Version history with added, changed, fixed, and removed sections.",
        "content": """# {title}

## [Unreleased]

### Added

- New feature description

### Changed

- Changed behavior description

### Fixed

- Bug fix description

### Removed

- Removed feature description

---

## [1.0.0] - YYYY-MM-DD

### Added

- Initial release features
""",
    },
    {
        "name": "How-To Guide",
        "slug": "how-to-guide",
        "category": "guide",
        "description": "Step-by-step guide to accomplish a specific task.",
        "content": """# {title}

## Goal

What the reader will accomplish by following this guide.

## Prerequisites

- What you need before starting

## Steps

### Step 1: Title

Detailed instructions for step 1.

### Step 2: Title

Detailed instructions for step 2.

### Step 3: Title

Detailed instructions for step 3.

## Verification

How to confirm the task was completed successfully.

## Troubleshooting

### Common Issue 1

**Symptom**: What the user sees.
**Solution**: How to fix it.

### Common Issue 2

**Symptom**: What the user sees.
**Solution**: How to fix it.
""",
    },
    {
        "name": "Troubleshooting",
        "slug": "troubleshooting",
        "category": "reference",
        "description": "Common issues with symptoms, causes, and solutions.",
        "content": """# {title}

## Overview

Common issues and their solutions.

---

### Issue: Problem Title

**Symptoms**: What the user observes.

**Cause**: Why this happens.

**Solution**:

1. First step to resolve
2. Second step to resolve
3. Verify the fix

---

### Issue: Another Problem

**Symptoms**: What the user observes.

**Cause**: Why this happens.

**Solution**:

1. First step to resolve
2. Second step to resolve

## Getting Help

If none of the above solutions work, contact support.
""",
    },
]


def get_template_by_slug(slug: str) -> dict | None:
    """Look up a built-in template by slug."""
    for t in BUILTIN_TEMPLATES:
        if t["slug"] == slug:
            return t
    return None


def list_template_summaries() -> list[dict]:
    """Return a lightweight list of available templates."""
    return [
        {"name": t["name"], "slug": t["slug"], "category": t["category"], "description": t["description"]}
        for t in BUILTIN_TEMPLATES
    ]
