#!/usr/bin/env python3
"""Verify migration parity between source tree and imported AccelDocs content.

Checks:
1) Hierarchy shape parity (sections/tabs/versions and nesting)
2) Page placement parity (page -> section)
3) Page ordering parity (display_order exactly matches source sibling index)
4) Optional content parity checks (exact normalized text or similarity ratio)

Primary input is a migration state JSON produced by migrate_developerhub.py.
For strict verification, state should include:
  - tree
  - section_map
  - page_id_map
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import requests
from bs4 import BeautifulSoup


def _slugify(text: str) -> str:
    s = (text or "").lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "page"


def _normalize_url_key(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") or "/"
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def _extract_text_from_html(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return _normalize_text(soup.get_text(separator=" ", strip=True))


@dataclass
class ExpectedSectionChild:
    path: str
    title: str
    order: int
    section_type: str


@dataclass
class ExpectedPage:
    url: str
    title: str
    order: int


@dataclass
class ExpectedShape:
    section_children_by_parent: dict[str, list[ExpectedSectionChild]]
    pages_by_section_path: dict[str, list[ExpectedPage]]
    ordered_urls: list[str]


def _build_expected_shape(tree: list[dict]) -> ExpectedShape:
    section_children_by_parent: dict[str, list[ExpectedSectionChild]] = defaultdict(list)
    pages_by_section_path: dict[str, list[ExpectedPage]] = defaultdict(list)
    ordered_urls: list[str] = []
    seen_urls: set[str] = set()

    def _add_page(section_path: str, url: str, title: str, order: int) -> None:
        norm = _normalize_url_key(url)
        if norm in seen_urls:
            return
        seen_urls.add(norm)
        pages_by_section_path[section_path].append(
            ExpectedPage(url=norm, title=title, order=order)
        )
        ordered_urls.append(norm)

    def _walk(nodes: list[dict], parent_path: str) -> None:
        for order, node in enumerate(nodes):
            title = str(node.get("title") or "").strip() or "Untitled"
            url = node.get("url")
            children = node.get("children")
            if not isinstance(children, list):
                children = []
            has_children = bool(children)

            node_path = f"{parent_path}/{_slugify(title)}"
            section_type = str(node.get("_section_type") or "section")

            if has_children:
                section_children_by_parent[parent_path].append(
                    ExpectedSectionChild(
                        path=node_path,
                        title=title,
                        order=order,
                        section_type=section_type,
                    )
                )
                if isinstance(url, str) and url:
                    _add_page(node_path, url, title, order)
                _walk(children, node_path)
            else:
                if isinstance(url, str) and url:
                    _add_page(parent_path, url, title, order)

    _walk(tree, "")
    return ExpectedShape(
        section_children_by_parent=dict(section_children_by_parent),
        pages_by_section_path=dict(pages_by_section_path),
        ordered_urls=ordered_urls,
    )


class ApiClient:
    def __init__(self, backend: str, token: str, org_id: int):
        self.base = backend.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "X-Org-Id": str(org_id),
            }
        )

    def get_json(self, path: str) -> dict[str, Any]:
        url = f"{self.base}{path}"
        resp = self.session.get(url, timeout=60)
        if not resp.ok:
            raise RuntimeError(f"GET {path} failed [{resp.status_code}]: {resp.text[:300]}")
        return resp.json()


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"State file not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _normalize_section_map(raw: dict[str, Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for k, v in raw.items():
        try:
            out[str(k)] = int(v)
        except Exception:
            continue
    return out


def _normalize_page_id_map(raw: dict[str, Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for url, page_id in raw.items():
        try:
            out[_normalize_url_key(str(url))] = int(page_id)
        except Exception:
            continue
    return out


def _compare_structure(
    *,
    expected: ExpectedShape,
    sections: list[dict],
    pages: list[dict],
    section_map: dict[str, int],
    page_id_map: dict[str, int],
    product_id: int,
    strict: bool,
) -> dict[str, Any]:
    sections_by_id = {int(s["id"]): s for s in sections}
    pages_by_id = {int(p["id"]): p for p in pages}

    child_sections_by_parent: dict[int, list[dict]] = defaultdict(list)
    for s in sections:
        pid = s.get("parent_id")
        if pid is None:
            continue
        child_sections_by_parent[int(pid)].append(s)
    for pid in list(child_sections_by_parent.keys()):
        child_sections_by_parent[pid].sort(key=lambda x: (int(x.get("display_order") or 0), int(x["id"])))

    pages_by_section: dict[int, list[dict]] = defaultdict(list)
    for p in pages:
        sid = p.get("section_id")
        if sid is None:
            continue
        pages_by_section[int(sid)].append(p)
    for sid in list(pages_by_section.keys()):
        pages_by_section[sid].sort(key=lambda x: (int(x.get("display_order") or 0), int(x["id"])))

    report: dict[str, Any] = {
        "missing_parent_section_mapping": [],
        "missing_section_mapping": [],
        "missing_section_in_backend": [],
        "section_parent_mismatch": [],
        "section_order_mismatch": [],
        "section_name_mismatch": [],
        "extra_sections_under_parent": [],
        "missing_page_mapping": [],
        "missing_page_in_backend": [],
        "page_section_mismatch": [],
        "page_order_mismatch": [],
        "page_title_mismatch": [],
        "extra_pages_under_section": [],
    }

    # Section parity
    for parent_path, expected_children in expected.section_children_by_parent.items():
        parent_id = product_id if parent_path == "" else section_map.get(parent_path)
        if parent_id is None:
            report["missing_parent_section_mapping"].append({"parent_path": parent_path})
            continue

        expected_child_ids: set[int] = set()
        for exp in expected_children:
            sid = section_map.get(exp.path)
            if sid is None:
                report["missing_section_mapping"].append(
                    {"parent_path": parent_path, "child_path": exp.path, "expected_title": exp.title}
                )
                continue
            expected_child_ids.add(sid)
            actual = sections_by_id.get(sid)
            if not actual:
                report["missing_section_in_backend"].append(
                    {"section_id": sid, "child_path": exp.path, "expected_title": exp.title}
                )
                continue

            actual_parent = actual.get("parent_id")
            if int(actual_parent) != int(parent_id):
                report["section_parent_mismatch"].append(
                    {
                        "section_id": sid,
                        "child_path": exp.path,
                        "expected_parent_id": parent_id,
                        "actual_parent_id": actual_parent,
                    }
                )

            actual_order = int(actual.get("display_order") or 0)
            if actual_order != exp.order:
                report["section_order_mismatch"].append(
                    {
                        "section_id": sid,
                        "child_path": exp.path,
                        "expected_order": exp.order,
                        "actual_order": actual_order,
                    }
                )

            if _slugify(str(actual.get("name") or "")) != _slugify(exp.title):
                report["section_name_mismatch"].append(
                    {
                        "section_id": sid,
                        "child_path": exp.path,
                        "expected_title": exp.title,
                        "actual_title": actual.get("name"),
                    }
                )

        if strict:
            actual_ids = {int(s["id"]) for s in child_sections_by_parent.get(int(parent_id), [])}
            extras = sorted(actual_ids - expected_child_ids)
            if extras:
                report["extra_sections_under_parent"].append(
                    {
                        "parent_path": parent_path,
                        "parent_id": parent_id,
                        "extra_section_ids": extras,
                    }
                )

    # Page parity
    for section_path, expected_pages in expected.pages_by_section_path.items():
        section_id = product_id if section_path == "" else section_map.get(section_path)
        if section_id is None:
            report["missing_parent_section_mapping"].append({"parent_path": section_path})
            continue

        expected_page_ids: set[int] = set()
        for exp in expected_pages:
            pid = page_id_map.get(exp.url)
            if pid is None:
                report["missing_page_mapping"].append(
                    {
                        "source_url": exp.url,
                        "section_path": section_path,
                        "expected_order": exp.order,
                    }
                )
                continue
            expected_page_ids.add(pid)

            actual = pages_by_id.get(pid)
            if not actual:
                report["missing_page_in_backend"].append(
                    {"page_id": pid, "source_url": exp.url, "section_path": section_path}
                )
                continue

            actual_sid = int(actual.get("section_id") or 0)
            if actual_sid != int(section_id):
                report["page_section_mismatch"].append(
                    {
                        "page_id": pid,
                        "source_url": exp.url,
                        "expected_section_id": section_id,
                        "actual_section_id": actual_sid,
                    }
                )

            actual_order = int(actual.get("display_order") or 0)
            if actual_order != exp.order:
                report["page_order_mismatch"].append(
                    {
                        "page_id": pid,
                        "source_url": exp.url,
                        "expected_order": exp.order,
                        "actual_order": actual_order,
                        "section_path": section_path,
                    }
                )

            if _slugify(str(actual.get("title") or "")) != _slugify(exp.title):
                report["page_title_mismatch"].append(
                    {
                        "page_id": pid,
                        "source_url": exp.url,
                        "expected_title": exp.title,
                        "actual_title": actual.get("title"),
                    }
                )

        if strict:
            actual_ids = {int(p["id"]) for p in pages_by_section.get(int(section_id), [])}
            extras = sorted(actual_ids - expected_page_ids)
            if extras:
                report["extra_pages_under_section"].append(
                    {
                        "section_path": section_path,
                        "section_id": section_id,
                        "extra_page_ids": extras,
                    }
                )

    return report


def _fetch_source_content(url: str) -> str:
    try:
        resp = requests.get(url, timeout=30)
        if not resp.ok:
            return ""
        soup = BeautifulSoup(resp.text, "html.parser")
        selectors = [
            ".content-container",
            ".editor-top-level",
            ".master-content",
            "main",
            "article",
            ".content-body",
            "[role='main']",
            ".page-content",
            ".docs-content",
            "body",
        ]
        for sel in selectors:
            elem = soup.select_one(sel)
            if elem and elem.get_text(strip=True):
                return _extract_text_from_html(str(elem))
    except Exception:
        return ""
    return ""


def _compare_content(
    *,
    client: ApiClient,
    urls: list[str],
    page_id_map: dict[str, int],
    state_page_data: dict[str, Any],
    check_mode: str,
    ratio_threshold: float,
    sample_size: int,
    fetch_source_if_missing: bool,
) -> dict[str, Any]:
    normalized_page_data: dict[str, Any] = {
        _normalize_url_key(k): v for k, v in (state_page_data or {}).items()
    }
    content_mismatches: list[dict[str, Any]] = []
    checked = 0
    skipped = 0

    candidates = urls if sample_size <= 0 else urls[:sample_size]
    for src_url in candidates:
        page_id = page_id_map.get(src_url)
        if page_id is None:
            skipped += 1
            continue

        source_entry = normalized_page_data.get(src_url) or {}
        source_text = ""

        raw_html = source_entry.get("raw_html") if isinstance(source_entry, dict) else ""
        markdown = source_entry.get("markdown") if isinstance(source_entry, dict) else ""
        if raw_html:
            source_text = _extract_text_from_html(str(raw_html))
        elif markdown:
            source_text = _normalize_text(str(markdown))
        elif fetch_source_if_missing:
            source_text = _fetch_source_content(src_url)

        if not source_text:
            skipped += 1
            continue

        try:
            page = client.get_json(f"/api/pages/{page_id}")
        except Exception:
            skipped += 1
            continue

        target_html = str(page.get("html_content") or "")
        target_text = _extract_text_from_html(target_html)
        if not target_text:
            skipped += 1
            continue

        checked += 1
        if check_mode == "exact":
            if source_text != target_text:
                content_mismatches.append(
                    {
                        "source_url": src_url,
                        "page_id": page_id,
                        "mode": "exact",
                        "source_length": len(source_text),
                        "target_length": len(target_text),
                    }
                )
        else:
            ratio = SequenceMatcher(None, source_text, target_text).ratio()
            if ratio < ratio_threshold:
                content_mismatches.append(
                    {
                        "source_url": src_url,
                        "page_id": page_id,
                        "mode": "ratio",
                        "ratio": round(ratio, 4),
                        "threshold": ratio_threshold,
                    }
                )

    return {
        "checked": checked,
        "skipped": skipped,
        "mismatches": content_mismatches,
    }


def _count_structure_issues(report: dict[str, Any]) -> int:
    return sum(len(v) for v in report.values() if isinstance(v, list))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Verify migration hierarchy + page parity.")
    p.add_argument("--backend", default=os.environ.get("ACCELDOCS_BACKEND", "http://localhost:8000"))
    p.add_argument("--token", default=os.environ.get("ACCELDOCS_TOKEN", ""))
    p.add_argument("--org-id", type=int, default=int(os.environ.get("ACCELDOCS_ORG_ID", "0") or 0))
    p.add_argument("--product-id", type=int, required=True, help="Destination root section ID (product section ID).")
    p.add_argument("--state-file", required=True, help="Path to migration state JSON.")
    p.add_argument(
        "--strict",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="When true, also fail on extra sections/pages not present in expected tree.",
    )
    p.add_argument("--check-content", action="store_true", help="Enable page content parity checks.")
    p.add_argument("--content-mode", choices=["exact", "ratio"], default="exact")
    p.add_argument("--content-threshold", type=float, default=0.98, help="Used only when --content-mode ratio.")
    p.add_argument("--content-sample", type=int, default=100, help="0 = all pages, else first N source URLs.")
    p.add_argument(
        "--fetch-source-if-missing",
        action="store_true",
        help="If state.page_data is missing, fetch source pages directly for content comparison.",
    )
    p.add_argument("--report-file", default="", help="Optional JSON report output path.")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    if not args.token:
        print("ERROR: --token or ACCELDOCS_TOKEN is required", file=sys.stderr)
        sys.exit(1)
    if not args.org_id:
        print("ERROR: --org-id or ACCELDOCS_ORG_ID is required", file=sys.stderr)
        sys.exit(1)

    state = _load_state(Path(args.state_file))
    tree = state.get("tree")
    if not isinstance(tree, list) or not tree:
        print("ERROR: state file does not contain a non-empty 'tree'", file=sys.stderr)
        sys.exit(1)

    section_map = _normalize_section_map(state.get("section_map") or {})
    page_id_map = _normalize_page_id_map(state.get("page_id_map") or {})

    if not section_map or not page_id_map:
        print(
            "ERROR: strict parity requires state.section_map and state.page_id_map.\n"
            "Run a non-dry migration first, then re-run this checker on that state file.",
            file=sys.stderr,
        )
        sys.exit(1)

    expected = _build_expected_shape(tree)
    client = ApiClient(args.backend, args.token, args.org_id)
    sections = client.get_json("/api/sections").get("sections", [])
    pages = client.get_json("/api/pages").get("pages", [])

    structure_report = _compare_structure(
        expected=expected,
        sections=sections,
        pages=pages,
        section_map=section_map,
        page_id_map=page_id_map,
        product_id=args.product_id,
        strict=args.strict,
    )
    structure_issue_count = _count_structure_issues(structure_report)

    content_report: dict[str, Any] | None = None
    content_issue_count = 0
    if args.check_content:
        content_report = _compare_content(
            client=client,
            urls=expected.ordered_urls,
            page_id_map=page_id_map,
            state_page_data=state.get("page_data") or {},
            check_mode=args.content_mode,
            ratio_threshold=args.content_threshold,
            sample_size=args.content_sample,
            fetch_source_if_missing=args.fetch_source_if_missing,
        )
        content_issue_count = len(content_report.get("mismatches", []))

    summary = {
        "expected_top_nodes": len(tree),
        "expected_sections_parsed": sum(len(v) for v in expected.section_children_by_parent.values()),
        "expected_pages_parsed": sum(len(v) for v in expected.pages_by_section_path.values()),
        "structure_issues": structure_issue_count,
        "content_checked": (content_report or {}).get("checked", 0),
        "content_skipped": (content_report or {}).get("skipped", 0),
        "content_issues": content_issue_count,
        "pass": structure_issue_count == 0 and content_issue_count == 0,
    }

    report = {
        "summary": summary,
        "structure": structure_report,
    }
    if content_report is not None:
        report["content"] = content_report

    print("=== Migration Parity Report ===")
    print(json.dumps(summary, indent=2))

    if args.report_file:
        out = Path(args.report_file)
        out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Report written: {out}")

    if summary["pass"]:
        print("PASS: hierarchy/page ordering/content parity checks succeeded.")
        sys.exit(0)

    print("FAIL: parity mismatches detected. Inspect the JSON report for details.", file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
