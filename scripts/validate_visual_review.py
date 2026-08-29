#!/usr/bin/env python3
"""Validate hash-bound, page-specific visual review evidence without editing it."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from _console import emit_json, emit_text

REVIEW_SCHEMA = "pdf-tw-localize/render-review/v2"
SCHEMA = "pdf-tw-localize/visual-review-validation/v1"
STATUS_FIELDS = (
    "visual_status",
    "image_text_status",
    "geometry_status",
    "legibility_status",
)
GENERIC_NOTES = {
    "ok",
    "pass",
    "passed",
    "looks good",
    "no issue",
    "no issues",
    "checked",
    "reviewed",
    "已檢查",
    "已確認",
    "通過",
    "無異常",
    "沒有問題",
    "版面正常",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def parse_pages(spec: str | None) -> list[int] | None:
    if not spec:
        return None
    pages: set[int] = set()
    for item in spec.split(","):
        token = item.strip()
        if not token:
            continue
        if "-" in token:
            start_text, end_text = token.split("-", 1)
            start, end = int(start_text), int(end_text)
            if start < 1 or start > end:
                raise ValueError(f"Invalid page range: {token}")
            pages.update(range(start, end + 1))
        else:
            page = int(token)
            if page < 1:
                raise ValueError(f"Invalid page number: {token}")
            pages.add(page)
    return sorted(pages)


def add_issue(
    issues: list[dict[str, Any]],
    severity: str,
    code: str,
    message: str,
    evidence: Any = None,
) -> None:
    item: dict[str, Any] = {"severity": severity, "code": code, "message": message}
    if evidence is not None:
        item["evidence"] = evidence
    issues.append(item)


def normalized_note(value: str) -> str:
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE).casefold()


def parse_review_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def validate_file_evidence(
    page_number: int,
    label: str,
    evidence: Any,
    issues: list[dict[str, Any]],
) -> str | None:
    if not isinstance(evidence, dict):
        add_issue(
            issues,
            "BLOCKED",
            "IMAGE_EVIDENCE_MISSING",
            f"Page {page_number} lacks {label} image evidence.",
        )
        return None
    raw_path = str(evidence.get("path") or "").strip()
    recorded_hash = str(evidence.get("sha256") or "").strip().upper()
    if not raw_path or not recorded_hash:
        add_issue(
            issues,
            "BLOCKED",
            "IMAGE_EVIDENCE_INCOMPLETE",
            f"Page {page_number} has incomplete {label} image evidence.",
        )
        return None
    path = Path(raw_path)
    if not path.is_file():
        add_issue(
            issues,
            "BLOCKED",
            "IMAGE_EVIDENCE_FILE_MISSING",
            f"Page {page_number} {label} image does not exist.",
            raw_path,
        )
        return None
    actual_hash = sha256_file(path)
    if actual_hash != recorded_hash:
        add_issue(
            issues,
            "BLOCKED",
            "IMAGE_EVIDENCE_HASH_MISMATCH",
            f"Page {page_number} {label} image hash is stale.",
            {"path": raw_path, "recorded": recorded_hash, "actual": actual_hash},
        )
        return actual_hash
    return actual_hash


def validate_pdf_identity(
    label: str,
    item: Any,
    expected_hash: str | None,
    issues: list[dict[str, Any]],
) -> str | None:
    if item is None:
        if expected_hash:
            add_issue(
                issues,
                "BLOCKED",
                "PDF_IDENTITY_MISSING",
                f"Manifest lacks required {label} identity.",
            )
        return None
    if not isinstance(item, dict):
        add_issue(
            issues,
            "BLOCKED",
            "PDF_IDENTITY_INVALID",
            f"Manifest {label} identity is invalid.",
        )
        return None
    raw_path = str(item.get("path") or "").strip()
    recorded_hash = str(item.get("sha256") or "").strip().upper()
    path = Path(raw_path)
    if not raw_path or not path.is_file():
        add_issue(
            issues,
            "BLOCKED",
            "PDF_FILE_MISSING",
            f"Manifest {label} PDF does not exist.",
            raw_path,
        )
        return None
    actual_hash = sha256_file(path)
    if actual_hash != recorded_hash:
        add_issue(
            issues,
            "BLOCKED",
            "PDF_HASH_MISMATCH",
            f"Manifest {label} PDF hash is stale.",
            {"recorded": recorded_hash, "actual": actual_hash, "path": raw_path},
        )
    if expected_hash and actual_hash != expected_hash.upper():
        add_issue(
            issues,
            "BLOCKED",
            "PDF_UNEXPECTED_HASH",
            f"Manifest {label} PDF does not match the expected QA input.",
            {"expected": expected_hash.upper(), "actual": actual_hash},
        )
    return actual_hash


def validate_manifest(
    manifest_path: Path,
    *,
    expected_source_hash: str | None = None,
    expected_candidate_hash: str | None = None,
    expected_baseline_hash: str | None = None,
    expected_pages: list[int] | None = None,
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "schema": SCHEMA,
            "manifest": str(manifest_path.resolve()),
            "status": "BLOCKED",
            "issues": [
                {
                    "severity": "BLOCKED",
                    "code": "MANIFEST_INVALID",
                    "message": f"Review manifest cannot be read: {exc}",
                }
            ],
            "pages": [],
            "user_acceptance": "NOT_CHECKED",
        }

    if manifest.get("schema") != REVIEW_SCHEMA:
        add_issue(
            issues,
            "BLOCKED",
            "MANIFEST_SCHEMA",
            f"Expected {REVIEW_SCHEMA}; older review evidence is not accepted.",
            manifest.get("schema"),
        )

    validate_pdf_identity(
        "source", manifest.get("source"), expected_source_hash, issues
    )
    validate_pdf_identity(
        "candidate", manifest.get("candidate"), expected_candidate_hash, issues
    )
    validate_pdf_identity(
        "baseline", manifest.get("baseline"), expected_baseline_hash, issues
    )

    render = manifest.get("render") if isinstance(manifest.get("render"), dict) else {}
    render_dpi = render.get("dpi")
    if not isinstance(render_dpi, int) or render_dpi < 72:
        add_issue(issues, "BLOCKED", "RENDER_DPI_INVALID", "Render DPI is missing or invalid.")

    raw_pages = manifest.get("pages")
    if not isinstance(raw_pages, list):
        raw_pages = []
        add_issue(issues, "BLOCKED", "PAGE_RECORDS_MISSING", "Manifest pages must be a list.")

    page_numbers = [item.get("page") for item in raw_pages if isinstance(item, dict)]
    valid_page_numbers = [item for item in page_numbers if isinstance(item, int)]
    duplicates = sorted(page for page, count in Counter(valid_page_numbers).items() if count > 1)
    if duplicates:
        add_issue(
            issues,
            "BLOCKED",
            "DUPLICATE_PAGE_RECORD",
            "Manifest repeats page records.",
            duplicates,
        )
    actual_pages = sorted(set(valid_page_numbers))
    if expected_pages is not None and actual_pages != sorted(expected_pages):
        add_issue(
            issues,
            "BLOCKED",
            "PAGE_SCOPE_MISMATCH",
            "Manifest page scope does not match the requested review scope.",
            {"expected": sorted(expected_pages), "actual": actual_pages},
        )

    page_reports: list[dict[str, Any]] = []
    completed_times: list[tuple[int, str]] = []
    completed_notes: list[tuple[int, str]] = []
    completed_count = 0
    failed_count = 0

    for item in raw_pages:
        if not isinstance(item, dict) or not isinstance(item.get("page"), int):
            add_issue(issues, "BLOCKED", "PAGE_RECORD_INVALID", "A page record is invalid.")
            continue
        page_number = item["page"]
        page_issues: list[dict[str, Any]] = []
        images = item.get("images") if isinstance(item.get("images"), dict) else {}
        validate_file_evidence(page_number, "source", images.get("source"), page_issues)
        if manifest.get("baseline") is not None:
            validate_file_evidence(page_number, "baseline", images.get("baseline"), page_issues)
        validate_file_evidence(page_number, "candidate", images.get("candidate"), page_issues)
        compare_hash = validate_file_evidence(
            page_number, "comparison", item.get("comparison"), page_issues
        )

        review = item.get("review") if isinstance(item.get("review"), dict) else {}
        statuses = {field: str(review.get(field, "NOT_CHECKED")).upper() for field in STATUS_FIELDS}
        allowed = {
            "visual_status": {"NOT_CHECKED", "PASS", "FAIL"},
            "image_text_status": {"NOT_CHECKED", "PASS", "FAIL", "NOT_APPLICABLE"},
            "geometry_status": {"NOT_CHECKED", "PASS", "FAIL"},
            "legibility_status": {"NOT_CHECKED", "PASS", "FAIL"},
        }
        for field, value in statuses.items():
            if value not in allowed[field]:
                add_issue(
                    page_issues,
                    "BLOCKED",
                    "REVIEW_STATUS_INVALID",
                    f"Page {page_number} {field} has invalid value {value!r}.",
                )

        values = list(statuses.values())
        has_failure = "FAIL" in values
        complete = (
            statuses["visual_status"] == "PASS"
            and statuses["image_text_status"] in {"PASS", "NOT_APPLICABLE"}
            and statuses["geometry_status"] == "PASS"
            and statuses["legibility_status"] == "PASS"
        )
        untouched = all(value == "NOT_CHECKED" for value in values)

        metadata_fields = {
            "reviewer": review.get("reviewer"),
            "reviewed_at": review.get("reviewed_at"),
            "page_reference": review.get("page_reference"),
            "review_dpi": review.get("review_dpi"),
            "reviewed_compare_sha256": review.get("reviewed_compare_sha256"),
            "notes": review.get("notes"),
        }
        has_metadata = any(value not in (None, "") for value in metadata_fields.values())

        if untouched and has_metadata:
            add_issue(
                page_issues,
                "BLOCKED",
                "UNREVIEWED_METADATA_PRESENT",
                f"Page {page_number} has review metadata while all statuses are NOT_CHECKED.",
            )
        elif not untouched and not complete and not has_failure:
            add_issue(
                page_issues,
                "NEEDS_REVIEW",
                "REVIEW_INCOMPLETE",
                f"Page {page_number} has a partially filled review.",
                statuses,
            )

        if has_failure:
            failed_count += 1
            add_issue(
                page_issues,
                "BLOCKED",
                "PAGE_REVIEW_FAILED",
                f"Page {page_number} contains a failed review status.",
                statuses,
            )

        if complete:
            completed_count += 1
            reviewer = str(review.get("reviewer") or "").strip()
            reviewed_at = str(review.get("reviewed_at") or "").strip()
            page_reference = str(review.get("page_reference") or "").strip()
            notes = str(review.get("notes") or "").strip()
            reviewed_hash = str(review.get("reviewed_compare_sha256") or "").strip().upper()
            review_dpi = review.get("review_dpi")

            if len(reviewer) < 2:
                add_issue(page_issues, "BLOCKED", "REVIEWER_MISSING", f"Page {page_number} lacks a reviewer.")
            if parse_review_time(reviewed_at) is None:
                add_issue(
                    page_issues,
                    "BLOCKED",
                    "REVIEW_TIME_INVALID",
                    f"Page {page_number} review time must be timezone-aware ISO 8601.",
                    reviewed_at,
                )
            else:
                completed_times.append((page_number, reviewed_at))
            expected_reference = f"p{page_number:03d}"
            if page_reference.casefold() != expected_reference.casefold():
                add_issue(
                    page_issues,
                    "BLOCKED",
                    "PAGE_REFERENCE_INVALID",
                    f"Page {page_number} must use page_reference {expected_reference}.",
                    page_reference,
                )
            if not isinstance(review_dpi, int) or not isinstance(render_dpi, int) or review_dpi < render_dpi:
                add_issue(
                    page_issues,
                    "BLOCKED",
                    "REVIEW_DPI_INVALID",
                    f"Page {page_number} review DPI must be at least the rendered DPI.",
                    {"review_dpi": review_dpi, "render_dpi": render_dpi},
                )
            if compare_hash is None or reviewed_hash != compare_hash:
                add_issue(
                    page_issues,
                    "BLOCKED",
                    "REVIEW_HASH_NOT_BOUND",
                    f"Page {page_number} review is not bound to the current comparison image.",
                    {"reviewed": reviewed_hash, "current": compare_hash},
                )
            note_key = normalized_note(notes)
            if len(note_key) < 20 or note_key in {normalized_note(item) for item in GENERIC_NOTES}:
                add_issue(
                    page_issues,
                    "BLOCKED",
                    "REVIEW_NOTE_GENERIC",
                    f"Page {page_number} review note is too short or generic.",
                    notes,
                )
            if str(page_number) not in notes and expected_reference.casefold() not in notes.casefold():
                add_issue(
                    page_issues,
                    "BLOCKED",
                    "REVIEW_NOTE_PAGE_UNBOUND",
                    f"Page {page_number} note must identify the page it describes.",
                    notes,
                )
            completed_notes.append((page_number, note_key))

        if has_failure:
            page_status = "BLOCKED"
        elif complete and not any(issue["severity"] == "BLOCKED" for issue in page_issues):
            page_status = "VISUAL_REVIEWED"
        elif untouched:
            page_status = "NOT_CHECKED"
        else:
            page_status = "PARTIAL"

        issues.extend(page_issues)
        page_reports.append(
            {
                "page": page_number,
                "status": page_status,
                "statuses": statuses,
                "issues": page_issues,
            }
        )

    time_counts = Counter(value for _, value in completed_times)
    for value, count in time_counts.items():
        if count > 1:
            pages = [page for page, item in completed_times if item == value]
            add_issue(
                issues,
                "BLOCKED",
                "REPEATED_REVIEW_TIME",
                "Multiple reviewed pages share the same timestamp; page review times must be unique.",
                {"reviewed_at": value, "pages": pages},
            )
    note_counts = Counter(value for _, value in completed_notes if value)
    for value, count in note_counts.items():
        if count > 1:
            pages = [page for page, item in completed_notes if item == value]
            add_issue(
                issues,
                "BLOCKED",
                "REPEATED_REVIEW_NOTE",
                "Multiple reviewed pages reuse the same normalized note.",
                {"pages": pages},
            )

    if any(issue["severity"] == "BLOCKED" for issue in issues) or failed_count:
        status = "BLOCKED"
    elif page_reports and completed_count == len(page_reports):
        status = "VISUAL_REVIEWED"
    elif completed_count:
        status = "PARTIAL"
    else:
        status = "NOT_CHECKED"

    return {
        "schema": SCHEMA,
        "manifest": str(manifest_path.resolve()),
        "status": status,
        "requested_pages": expected_pages if expected_pages is not None else actual_pages,
        "reviewed_pages": [item["page"] for item in page_reports if item["status"] == "VISUAL_REVIEWED"],
        "issues": issues,
        "pages": page_reports,
        "user_acceptance": "NOT_CHECKED",
        "limitations": [
            "This validator checks evidence binding and review-record quality; it cannot prove that a human actually viewed the image.",
            "Only the user may change USER_ACCEPTANCE from NOT_CHECKED.",
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate a v2 render-review manifest without modifying review evidence."
    )
    parser.add_argument("manifest", type=Path, help="Manifest created by render_review.py.")
    parser.add_argument("--pages", help="Expected 1-based page scope, for example 1,3-5.")
    parser.add_argument("--source-sha256", help="Expected source PDF SHA-256.")
    parser.add_argument("--candidate-sha256", help="Expected candidate PDF SHA-256.")
    parser.add_argument("--baseline-sha256", help="Expected baseline PDF SHA-256.")
    parser.add_argument("--output", type=Path, help="Write validation JSON to this path.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        pages = parse_pages(args.pages)
        report = validate_manifest(
            args.manifest,
            expected_source_hash=args.source_sha256,
            expected_candidate_hash=args.candidate_sha256,
            expected_baseline_hash=args.baseline_sha256,
            expected_pages=pages,
        )
    except ValueError as exc:
        emit_text(f"BLOCKED: {exc}", stream=sys.stderr)
        return 2
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    emit_json(report)
    return {"VISUAL_REVIEWED": 0, "BLOCKED": 2, "PARTIAL": 3, "NOT_CHECKED": 3}[report["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
