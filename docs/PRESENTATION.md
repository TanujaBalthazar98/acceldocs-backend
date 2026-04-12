# Docspeare vs DeveloperHub
## Why We're Switching Our Documentation Platform

---

## Slide 1: The Problem

**What we're paying now:**
- DeveloperHub: $129/month base + $99/month AI add-on = $228+/month
- Plus additional add-ons for analytics, custom domains, RBAC
- Total: **$2,700+ per year**

**What we're dealing with:**
- Our data lives on their servers (vendor lock-in)
- Basic features locked behind expensive add-ons
- No control over our own infrastructure

**The Goal:** Own our documentation platform, pay a fraction of the cost

---

## Slide 2: The Solution - Docspeare

**What is Docspeare?**
- Our own documentation platform built in-house
- Runs on our infrastructure: Vercel + Neon PostgreSQL + GitHub
- Content migrated from Google Docs → stored in our DB
- AI-powered chat (use our own Gemini/Groq API keys)

**Live Today:** https://www.docspeare.com

---

## Slide 3: Cost Comparison (The Big Number)

| | DeveloperHub | Docspeare | You Save |
|---|:---:|:---:|:---:|
| Platform | $129/mo | $0 | $129/mo |
| AI Chat | $99/mo | $0 | $99/mo |
| Database | Included | $0 (Neon) | — |
| Analytics | Add-on | Included | $XX/mo |
| Custom Domain | Add-on | Included | $XX/mo |
| RBAC | Add-on | Included | $XX/mo |
| **Total/Month** | **$228+** | **~$0-25** | **~$200+/mo** |

**Annual Savings: $2,700+** (or 92%+ reduction)

---

## Slide 4: Features - Already Built

**Content Management:**
- Section hierarchy with tabs (Docs, API Reference, Release Notes)
- Google Drive sync - import directly from Drive folders
- Full-text search across all pages
- Page feedback (helpful/not helpful)
- Comments on pages

**Publishing Workflow:**
- Draft → Submit for Review → Approve/Reject
- Version tabs (v1.0, v2.0, etc.)
- Scheduled publishing
- Automatic link redirects

**Access & Security:**
- RBAC: Owner / Admin / Editor / Reviewer / Viewer
- Google OAuth authentication
- External access grants (guest access)
- Rate limiting, encryption at rest

---

## Slide 5: Migration Status

**Already Migrated:**
- 146 pages → Documentation
- 113 pages → API Reference
- 10 pages → Release Notes
- **Total: ~270 pages**

**Remaining in DeveloperHub:** 6,000-8,000 pages

**Infrastructure (FREE tier):**
- Neon: 0.5 GB storage (generous for docs)
- Vercel: 100 GB/mo bandwidth (docs = low traffic)

**Migration script:** Ready to run for remaining pages

---

## Slide 6: Honest Assessment

**What's NOT Built (yet):**
- SSO/SAML single sign-on (enterprise feature)
- Webhooks for integrations
- Full audit logging
- Slack/Microsoft Teams integration

**Why it doesn't matter:**
- These are nice-to-haves, not blockers
- Basic auth (Google OAuth) works for our team
- Can add later if needed

**What's the Same:**
- Documentation content
- Google Drive integration
- Search, comments, feedback
- Publishing workflow

---

## Slide 7: Why Switch? Summary

**1. Save $200+/month** - 92%+ cost reduction

**2. Own Your Data** - Your docs on your Neon database, not vendor servers

**3. More Features Included** - AI chat, analytics, RBAC, custom domain - all in base price

**4. We Built It** - Customizable for our exact needs, we control the code

**5. Already In Progress** - 270 pages migrated, migration script ready to run

---

## Slide 8: Next Steps

1. **Complete full migration** - Run script for remaining 6,000+ pages
2. **Enable AI chat** - Add Gemini API key (or use free tier)
3. **Set custom domain** - docpeare.com → our instance
4. **Team testing** - Have documentation team try it out
5. **Switch production** - Update DNS to point to Docspeare

---

## Slide 9: Questions?

Let's discuss:
- What features do you need that might be missing?
- What's the timeline for switching?
- Any concerns about the migration?