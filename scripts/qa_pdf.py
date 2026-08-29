#!/usr/bin/env python3
"""Machine QA with separate visual-review and user-acceptance states."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from _console import emit_json, emit_text
from _compound_components import ENGLISH_ALLOWLIST_SCHEMA, validate_english_allowlist
from validate_visual_review import validate_manifest


SCHEMA = "pdf-tw-localize/qa-report/v2"
REBUILT_QA_SCHEMA = "pdf-tw-localize/rebuilt-machine-qa/v1"
ENGLISH_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9'./:+_-]{0,}")
URL_OR_EMAIL = re.compile(
    r"(?:https?://|www\.)\S+|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
    re.IGNORECASE,
)
MODEL_TOKEN = re.compile(
    r"\b(?=[A-Z0-9._/-]{4,}\b)(?=[A-Z0-9._/-]*[A-Z])(?=[A-Z0-9._/-]*\d)"
    r"[A-Z0-9][A-Z0-9._/-]{2,}\b"
)
VALUE_UNIT = re.compile(
    r"(?<![\w.])[+-]?\d+(?:[.,]\d+)?\s*(?:°[CF]|%|V|A|W|kW|kWh|Hz|mm|cm|m|"
    r"kg|g|Pa|kPa|MPa|psi|BTU(?:/h)?|RPM|rpm|dB|L|mL)(?![A-Za-z0-9_])"
)
PLACEHOLDER_PATTERNS = [
    re.compile(r"\{\{[^{}]{0,200}\}\}"),
    re.compile(r"\{v\d+\}", re.IGNORECASE),
    re.compile(r"\[(?:TRANSLATE|TRANSLATION|TODO|TBD)\]", re.IGNORECASE),
    re.compile(r"__(?:TRANSLATE|TRANSLATION|TODO|TBD)__", re.IGNORECASE),
    re.compile("\uFFFD"),
]
DEFAULT_ALLOWLIST = {
    "AC", "Bluetooth", "BTU", "C", "CE", "cm", "dB", "DC", "F", "FCC",
    "g", "Hz", "IEC", "IP", "ISO", "kg", "kPa", "kW", "kWh", "L", "LCD",
    "LED", "m", "mL", "mm", "MPa", "Pa", "psi", "QR", "RoHS", "RPM",
    "rpm", "UL", "USB", "V", "W", "Wi-Fi",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def parse_pages(spec: str | None, total: int) -> list[int]:
    if not spec:
        return list(range(total))
    pages: set[int] = set()
    for item in spec.split(","):
        token = item.strip()
        if not token:
            continue
        if "-" in token:
            start_text, end_text = token.split("-", 1)
            start, end = int(start_text), int(end_text)
            if start > end:
                raise ValueError(f"Invalid descending page range: {token}")
            pages.update(range(start - 1, end))
        else:
            pages.add(int(token) - 1)
    invalid = sorted(page + 1 for page in pages if page < 0 or page >= total)
    if invalid:
        raise ValueError(f"Page selection is outside 1-{total}: {invalid}")
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


def area(rect: Any) -> float:
    return max(0.0, float(rect.width)) * max(0.0, float(rect.height))


def span_records(page: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for block_index, block in enumerate(page.get_text("dict", sort=True).get("blocks", [])):
        if block.get("type") != 0:
            continue
        for line_index, line in enumerate(block.get("lines", [])):
            for span_index, span in enumerate(line.get("spans", [])):
                text = str(span.get("text", "")).strip()
                if not text:
                    continue
                records.append(
                    {
                        "block": block_index,
                        "line": line_index,
                        "span": span_index,
                        "text": text,
                        "size": round(float(span.get("size", 0.0)), 3),
                        "bbox": [round(float(value), 3) for value in span.get("bbox", [0, 0, 0, 0])],
                    }
                )
    return records


def line_records(page: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for block_index, block in enumerate(page.get_text("dict", sort=True).get("blocks", [])):
        if block.get("type") != 0:
            continue
        for line_index, line in enumerate(block.get("lines", [])):
            text = "".join(span.get("text", "") for span in line.get("spans", [])).strip()
            if text:
                records.append(
                    {
                        "block": block_index,
                        "line": line_index,
                        "text": text,
                        "bbox": [round(float(value), 3) for value in line.get("bbox", [0, 0, 0, 0])],
                    }
                )
    return records


def geometry_findings(page: Any) -> dict[str, Any]:
    import fitz

    lines = line_records(page)
    overlaps: list[dict[str, Any]] = []
    for index, left in enumerate(lines):
        left_rect = fitz.Rect(left["bbox"])
        for right in lines[index + 1 :]:
            right_rect = fitz.Rect(right["bbox"])
            intersection = left_rect & right_rect
            minimum = min(area(left_rect), area(right_rect))
            if minimum <= 0:
                continue
            ratio = area(intersection) / minimum
            if ratio >= 0.35:
                overlaps.append(
                    {
                        "ratio": round(ratio, 3),
                        "left": left["text"][:160],
                        "right": right["text"][:160],
                        "left_bbox": left["bbox"],
                        "right_bbox": right["bbox"],
                    }
                )
    overlaps.sort(key=lambda item: item["ratio"], reverse=True)
    page_rect = page.rect
    tolerance = 0.75
    outside = []
    for line in lines:
        rect = fitz.Rect(line["bbox"])
        if (
            rect.x0 < page_rect.x0 - tolerance
            or rect.y0 < page_rect.y0 - tolerance
            or rect.x1 > page_rect.x1 + tolerance
            or rect.y1 > page_rect.y1 + tolerance
        ):
            outside.append(line)
    return {
        "line_count": len(lines),
        "overlap_count": len(overlaps),
        "overlaps": overlaps[:50],
        "out_of_bounds_count": len(outside),
        "out_of_bounds": outside[:50],
    }


def font_findings(source_page: Any, candidate_page: Any, min_size: float, min_ratio: float) -> dict[str, Any]:
    import fitz

    source = span_records(source_page)
    candidate = span_records(candidate_page)
    small: list[dict[str, Any]] = []
    reduced: list[dict[str, Any]] = []
    for span in candidate:
        if not re.search(r"[\u3400-\u9fff]", span["text"]):
            continue
        if span["size"] < min_size:
            small.append(span)
        candidate_rect = fitz.Rect(span["bbox"])
        if area(candidate_rect) <= 0:
            continue
        best_source = None
        best_overlap = 0.0
        for source_span in source:
            source_rect = fitz.Rect(source_span["bbox"])
            overlap = area(candidate_rect & source_rect) / area(candidate_rect)
            if overlap > best_overlap:
                best_overlap = overlap
                best_source = source_span
        if best_source and best_overlap >= 0.5 and best_source["size"] > 0:
            ratio = span["size"] / best_source["size"]
            if ratio < min_ratio:
                reduced.append(
                    {
                        "candidate": span,
                        "source": best_source,
                        "bbox_overlap_ratio": round(best_overlap, 3),
                        "font_size_ratio": round(ratio, 3),
                    }
                )
    return {
        "minimum_font_size_pt": min_size,
        "minimum_source_ratio": min_ratio,
        "candidate_cjk_span_count": sum(1 for item in candidate if re.search(r"[\u3400-\u9fff]", item["text"])),
        "small_cjk_span_count": len(small),
        "small_cjk_spans": small[:100],
        "reduced_mapped_span_count": len(reduced),
        "reduced_mapped_spans": reduced[:100],
        "classification": "VISUAL_CONFIRMATION_REQUIRED" if small or reduced else "NO_SUSPICION",
    }


def _allowlisted_words(value: str) -> set[str]:
    """Return the same English word units used by the residue scanner."""

    words: set[str] = set()
    for token in ENGLISH_TOKEN.findall(value):
        cleaned = token.strip("'./:+_-")
        if len(cleaned) > 1:
            words.add(cleaned.casefold())
    return words


def load_allowlist(
    path: Path | None,
) -> tuple[set[str], set[str], dict[int, set[str]], dict[int, set[str]], dict[str, Any]]:
    """Load legacy line lists or strict page-scoped v3 JSON exceptions.

    The PDF-wide QA has no stable-ID geometry mapping of its own, so a v3 JSON
    exception is narrowed to the declared page here. Stable-ID and visual-ID
    binding remains the responsibility of qa_rebuilt_pdf.py and
    qa_preserved_visuals.py. Invalid JSON contracts fail closed.
    """

    exact = {item.casefold() for item in DEFAULT_ALLOWLIST}
    regexes: set[str] = set()
    scoped_exact: dict[int, set[str]] = {}
    scoped_regexes: dict[int, set[str]] = {}
    metadata: dict[str, Any] = {
        "format": "DEFAULT_ONLY" if path is None else "LEGACY_LINES",
        "schema": None,
        "entry_count": 0,
        "scoped_page_token_binding_count": 0,
    }
    if path is None:
        return exact, regexes, scoped_exact, scoped_regexes, metadata

    raw = path.read_text(encoding="utf-8-sig")
    if not raw.lstrip().startswith("{"):
        custom_count = 0
        for raw_line in raw.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            custom_count += 1
            if line.startswith("re:"):
                regexes.add(line[3:])
            else:
                exact.add(line.casefold())
        metadata["entry_count"] = custom_count
        return exact, regexes, scoped_exact, scoped_regexes, metadata

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON English allowlist: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != ENGLISH_ALLOWLIST_SCHEMA:
        raise ValueError(
            f"JSON English allowlist must use schema {ENGLISH_ALLOWLIST_SCHEMA}"
        )
    contract_issues = validate_english_allowlist(payload)
    if contract_issues:
        raise ValueError(
            "Invalid scoped English allowlist: "
            + json.dumps(contract_issues, ensure_ascii=False, sort_keys=True)
        )

    entries = [
        *(payload.get("allowed") or []),
        *(payload.get("allowed_ui_english") or []),
        *(payload.get("allowed_visual_english") or []),
    ]
    for entry in entries:
        value = str(entry.get("token") or entry.get("source_text") or "")
        words = _allowlisted_words(value)
        for page_number in entry["scope"]["pages"]:
            scoped_exact.setdefault(int(page_number), set()).update(words)

    metadata.update(
        {
            "format": "SCOPED_JSON",
            "schema": ENGLISH_ALLOWLIST_SCHEMA,
            "entry_count": len(entries),
            "scoped_page_token_binding_count": sum(
                len(tokens) for tokens in scoped_exact.values()
            ),
        }
    )
    return exact, regexes, scoped_exact, scoped_regexes, metadata


def is_model_like(token: str) -> bool:
    return (
        len(token) >= 4
        and any(char.isdigit() for char in token)
        and any(char.isalpha() for char in token)
        and (token.upper() == token or any(char in token for char in "_/-"))
    )


def english_residue(
    text: str,
    exact_allowlist: set[str],
    regex_allowlist: list[re.Pattern[str]],
    *,
    page_exact_allowlist: set[str] | None = None,
    page_regex_allowlist: list[re.Pattern[str]] | None = None,
) -> Counter[str]:
    scrubbed = URL_OR_EMAIL.sub(" ", text)
    residue: Counter[str] = Counter()
    page_exact = page_exact_allowlist or set()
    page_regexes = page_regex_allowlist or []
    for token in ENGLISH_TOKEN.findall(scrubbed):
        cleaned = token.strip("'./:+_-")
        if len(cleaned) <= 1:
            continue
        if (
            cleaned.casefold() in exact_allowlist
            or cleaned.casefold() in page_exact
            or is_model_like(cleaned)
        ):
            continue
        if any(pattern.fullmatch(cleaned) for pattern in [*regex_allowlist, *page_regexes]):
            continue
        residue[cleaned] += 1
    return residue


def critical_tokens(text: str) -> Counter[str]:
    tokens = [re.sub(r"\s+", "", match.group(0)) for match in MODEL_TOKEN.finditer(text)]
    tokens.extend(re.sub(r"\s+", "", match.group(0)) for match in VALUE_UNIT.finditer(text))
    return Counter(tokens)


def placeholder_findings(text: str) -> list[str]:
    matches: list[str] = []
    for pattern in PLACEHOLDER_PATTERNS:
        matches.extend(match.group(0)[:120] for match in pattern.finditer(text))
    for char in text:
        category = unicodedata.category(char)
        if category == "Cc" and char not in {"\n", "\r", "\t", "\f"}:
            matches.append(f"CONTROL-U+{ord(char):04X}")
    return sorted(set(matches))


def load_stable_qa_report(
    path: Path | None,
    *,
    source_hash: str,
    candidate_hash: str,
) -> dict[str, Any] | None:
    """Accept only a successful hash-bound stable-ID QA report.

    This report is stronger than raw page-text token comparison because it
    binds protected tokens to stable segments and candidate evidence. It may
    therefore resolve extraction-only differences caused by source outlined
    text becoming embedded live text, but it never resolves English residue,
    page-size, placeholder, geometry, font, or visual-review findings.
    """

    if path is None:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Stable-ID QA report cannot be read: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != REBUILT_QA_SCHEMA:
        raise ValueError(f"Stable-ID QA report must use schema {REBUILT_QA_SCHEMA}")
    report_source_hash = str((payload.get("source") or {}).get("sha256", "")).upper()
    report_candidate_hash = str((payload.get("candidate") or {}).get("sha256", "")).upper()
    if report_source_hash != source_hash.upper():
        raise ValueError("Stable-ID QA source SHA-256 does not match qa_pdf input")
    if report_candidate_hash != candidate_hash.upper():
        raise ValueError("Stable-ID QA candidate SHA-256 does not match qa_pdf input")
    if payload.get("machine_qa") != "MACHINE_QA_PASS":
        raise ValueError("Stable-ID QA report is not MACHINE_QA_PASS")
    if payload.get("blocking_issue_count") != 0 or payload.get("blocking_issues"):
        raise ValueError("Stable-ID QA report contains blocking issues")
    protected = payload.get("protected_token_evidence")
    if not isinstance(protected, list) or not protected:
        raise ValueError("Stable-ID QA report lacks protected-token evidence")
    unresolved = [
        item
        for item in protected
        if not isinstance(item, dict)
        or str(item.get("status", ""))
        not in {
            "PRESERVED",
            "TRANSLATED_EQUIVALENT",
            "PRESERVED_SOURCE_IMAGE_REQUIRES_VISUAL_CONFIRMATION",
        }
    ]
    if unresolved:
        raise ValueError("Stable-ID QA report contains unresolved protected tokens")
    return {
        "path": str(path.resolve()),
        "sha256": sha256_file(path),
        "schema": REBUILT_QA_SCHEMA,
        "source_sha256": report_source_hash,
        "candidate_sha256": report_candidate_hash,
        "protected_token_evidence_count": len(protected),
        "machine_qa": "MACHINE_QA_PASS",
    }


def machine_status(issues: list[dict[str, Any]]) -> str:
    return "BLOCKED" if any(item["severity"] == "BLOCKED" for item in issues) else "MACHINE_QA_PASS"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run automated PDF checks and report MACHINE_QA, VISUAL_REVIEW, and USER_ACCEPTANCE independently."
    )
    parser.add_argument("source", type=Path, help="Original source PDF.")
    parser.add_argument("candidate", type=Path, help="Localized candidate PDF.")
    parser.add_argument("--baseline", type=Path, help="Optional baseline PDF bound by the review manifest.")
    parser.add_argument("--pages", help="1-based pages, for example 1,3-5. Default: all.")
    parser.add_argument(
        "--allowlist",
        type=Path,
        help=(
            "Strict page-scoped english-allowlist/v3 JSON, or a legacy line list "
            "of tokens where regex entries are prefixed with re:."
        ),
    )
    parser.add_argument(
        "--stable-qa-report",
        type=Path,
        help=(
            "Optional hash-bound rebuilt-machine-qa/v1 PASS report. When supplied, "
            "its stable-ID protected-token evidence supersedes raw extraction-count "
            "differences only."
        ),
    )
    parser.add_argument("--review-manifest", type=Path, help="Manifest created by render_review.py.")
    parser.add_argument("--page-size-tolerance", type=float, default=0.75, help="Allowed page width/height difference in points.")
    parser.add_argument("--minimum-font-size", type=float, default=6.0, help="Suspicious CJK font-size threshold in points.")
    parser.add_argument("--minimum-font-ratio", type=float, default=0.75, help="Suspicious mapped source/candidate font ratio.")
    parser.add_argument("--output", type=Path, help="Write JSON report to this path.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    input_paths = [args.source, args.candidate]
    if args.baseline:
        input_paths.append(args.baseline)
    for path in input_paths:
        if not path.is_file():
            emit_text(f"BLOCKED: input does not exist: {path}", stream=sys.stderr)
            return 2
    if args.minimum_font_size <= 0 or not 0 < args.minimum_font_ratio <= 1:
        emit_text("BLOCKED: font thresholds are invalid.", stream=sys.stderr)
        return 2
    try:
        import fitz
    except ImportError:
        emit_text("BLOCKED: PyMuPDF is required.", stream=sys.stderr)
        return 2

    source_hash = sha256_file(args.source)
    candidate_hash = sha256_file(args.candidate)
    baseline_hash = sha256_file(args.baseline) if args.baseline else None
    try:
        stable_qa_report = load_stable_qa_report(
            args.stable_qa_report,
            source_hash=source_hash,
            candidate_hash=candidate_hash,
        )
    except ValueError as exc:
        emit_text(f"BLOCKED: {exc}", stream=sys.stderr)
        return 2
    machine_issues: list[dict[str, Any]] = []
    machine_suspicions: list[dict[str, Any]] = []
    page_reports: list[dict[str, Any]] = []
    source_doc = None
    candidate_doc = None
    try:
        source_doc = fitz.open(args.source)
        candidate_doc = fitz.open(args.candidate)
        if source_doc.needs_pass or candidate_doc.needs_pass:
            add_issue(machine_issues, "BLOCKED", "ENCRYPTED_PDF", "Encrypted PDFs require an explicitly approved decryption step.")
        if source_doc.page_count != candidate_doc.page_count:
            add_issue(
                machine_issues,
                "BLOCKED",
                "PAGE_COUNT_MISMATCH",
                "Source and candidate page counts differ.",
                {"source": source_doc.page_count, "candidate": candidate_doc.page_count},
            )
        comparable_pages = min(source_doc.page_count, candidate_doc.page_count)
        selected = parse_pages(args.pages, comparable_pages)
        (
            exact_allowlist,
            regex_strings,
            scoped_exact_allowlist,
            scoped_regex_strings,
            allowlist_metadata,
        ) = load_allowlist(args.allowlist)
        regex_allowlist = [re.compile(item) for item in sorted(regex_strings)]
        scoped_regex_allowlist = {
            page_number: [re.compile(item) for item in sorted(items)]
            for page_number, items in scoped_regex_strings.items()
        }

        for index in selected:
            page_number = index + 1
            source_page = source_doc[index]
            candidate_page = candidate_doc[index]
            page_issues: list[dict[str, Any]] = []
            page_suspicions: list[dict[str, Any]] = []

            width_delta = abs(float(source_page.rect.width) - float(candidate_page.rect.width))
            height_delta = abs(float(source_page.rect.height) - float(candidate_page.rect.height))
            if width_delta > args.page_size_tolerance or height_delta > args.page_size_tolerance:
                add_issue(
                    page_issues,
                    "BLOCKED",
                    "PAGE_SIZE_MISMATCH",
                    f"Page {page_number} dimensions differ beyond tolerance.",
                    {
                        "source": [source_page.rect.width, source_page.rect.height],
                        "candidate": [candidate_page.rect.width, candidate_page.rect.height],
                        "tolerance": args.page_size_tolerance,
                    },
                )

            source_text = source_page.get_text("text")
            candidate_text = candidate_page.get_text("text")
            source_chars = len(re.sub(r"\s+", "", source_text))
            candidate_chars = len(re.sub(r"\s+", "", candidate_text))
            if source_chars >= 20 and candidate_chars == 0:
                add_issue(page_issues, "BLOCKED", "CANDIDATE_TEXT_MISSING", f"Page {page_number} has source text but no extractable candidate text.")
            elif source_chars >= 100 and candidate_chars < source_chars * 0.15:
                add_issue(
                    page_issues,
                    "BLOCKED",
                    "POSSIBLE_CONTENT_LOSS",
                    f"Page {page_number} candidate text is unusually sparse.",
                    {"source_characters": source_chars, "candidate_characters": candidate_chars},
                )

            residue = english_residue(
                candidate_text,
                exact_allowlist,
                regex_allowlist,
                page_exact_allowlist=scoped_exact_allowlist.get(page_number),
                page_regex_allowlist=scoped_regex_allowlist.get(page_number),
            )
            if residue:
                add_issue(
                    page_issues,
                    "BLOCKED",
                    "UNAPPROVED_EXTRACTABLE_ENGLISH",
                    f"Page {page_number} contains unapproved extractable English.",
                    dict(residue.most_common(50)),
                )

            placeholders = placeholder_findings(candidate_text)
            if placeholders:
                add_issue(
                    page_issues,
                    "BLOCKED",
                    "PLACEHOLDER_OR_CONTROL_CHARACTER",
                    f"Page {page_number} contains a placeholder or invalid control text.",
                    placeholders[:50],
                )

            source_critical = critical_tokens(source_text)
            candidate_critical = critical_tokens(candidate_text)
            missing = source_critical - candidate_critical
            extra = candidate_critical - source_critical
            if missing:
                if stable_qa_report:
                    add_issue(
                        page_suspicions,
                        "SUSPICIOUS",
                        "CRITICAL_TOKEN_EXTRACTION_DIFFERENCE_STABLE_QA_BOUND",
                        f"Page {page_number} has raw extraction differences, but exact stable-ID protected-token QA is hash-bound and passed.",
                        {"direction": "missing_from_candidate_extraction", "tokens": dict(missing)},
                    )
                else:
                    add_issue(page_issues, "BLOCKED", "CRITICAL_TOKEN_MISSING", f"Page {page_number} is missing protected model/value/unit tokens.", dict(missing))
            if extra:
                if stable_qa_report:
                    add_issue(
                        page_suspicions,
                        "SUSPICIOUS",
                        "CRITICAL_TOKEN_EXTRACTION_DIFFERENCE_STABLE_QA_BOUND",
                        f"Page {page_number} has raw extraction differences, but exact stable-ID protected-token QA is hash-bound and passed.",
                        {"direction": "added_to_candidate_extraction", "tokens": dict(extra)},
                    )
                else:
                    add_issue(page_issues, "BLOCKED", "CRITICAL_TOKEN_ADDED", f"Page {page_number} adds or duplicates protected model/value/unit tokens.", dict(extra))

            source_geometry = geometry_findings(source_page)
            candidate_geometry = geometry_findings(candidate_page)
            if candidate_geometry["overlap_count"] or candidate_geometry["out_of_bounds_count"]:
                add_issue(
                    page_suspicions,
                    "SUSPICIOUS",
                    "GEOMETRY_VISUAL_CONFIRMATION_REQUIRED",
                    f"Page {page_number} has extraction-box geometry findings that require visual confirmation.",
                    candidate_geometry,
                )

            fonts = font_findings(source_page, candidate_page, args.minimum_font_size, args.minimum_font_ratio)
            if fonts["classification"] != "NO_SUSPICION":
                add_issue(
                    page_suspicions,
                    "SUSPICIOUS",
                    "FONT_LEGIBILITY_VISUAL_CONFIRMATION_REQUIRED",
                    f"Page {page_number} has small or reduced CJK spans; inspect at 300 dpi and document whether they are source-small image/UI labels or blocking ordinary text.",
                    fonts,
                )

            machine_issues.extend(page_issues)
            machine_suspicions.extend(page_suspicions)
            page_reports.append(
                {
                    "page": page_number,
                    "page_size_delta_pt": {"width": round(width_delta, 3), "height": round(height_delta, 3)},
                    "characters": {"source": source_chars, "candidate": candidate_chars},
                    "english_residue": dict(residue.most_common()),
                    "critical_tokens": {
                        "source": dict(source_critical),
                        "candidate": dict(candidate_critical),
                        "missing": dict(missing),
                        "extra": dict(extra),
                    },
                    "geometry": {"source": source_geometry, "candidate": candidate_geometry},
                    "font_evidence": fonts,
                    "machine_issues": page_issues,
                    "machine_suspicions": page_suspicions,
                }
            )
    except (OSError, RuntimeError, ValueError, re.error) as exc:
        emit_text(f"BLOCKED: {exc}", stream=sys.stderr)
        return 2
    finally:
        for document in (source_doc, candidate_doc):
            if document is not None:
                document.close()

    checked_pages = [item["page"] for item in page_reports]
    if args.review_manifest:
        review_report = validate_manifest(
            args.review_manifest,
            expected_source_hash=source_hash,
            expected_candidate_hash=candidate_hash,
            expected_baseline_hash=baseline_hash,
            expected_pages=checked_pages,
        )
        visual_state = review_report["status"]
    else:
        review_report = {
            "status": "NOT_CHECKED",
            "issues": [
                {
                    "severity": "NEEDS_REVIEW",
                    "code": "VISUAL_REVIEW_MANIFEST_NOT_SUPPLIED",
                    "message": "No hash-bound review manifest was supplied.",
                }
            ],
            "reviewed_pages": [],
            "user_acceptance": "NOT_CHECKED",
        }
        visual_state = "NOT_CHECKED"

    machine_state = machine_status(machine_issues)
    if machine_state == "BLOCKED" or visual_state == "BLOCKED":
        decision = "BLOCKED"
    elif machine_state == "MACHINE_QA_PASS" and visual_state == "VISUAL_REVIEWED":
        decision = "INTERNAL_QA_COMPLETE"
    else:
        decision = "NEEDS_REVIEW"

    report = {
        "schema": SCHEMA,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "candidate_decision": decision,
        "machine_qa": {
            "status": machine_state,
            "scope": "FULL_DOCUMENT" if len(checked_pages) == comparable_pages else "SELECTED_PAGES",
            "full_document_pass": machine_state == "MACHINE_QA_PASS" and len(checked_pages) == comparable_pages,
            "checked_pages": checked_pages,
            "issues": machine_issues,
            "suspicions": machine_suspicions,
        },
        "visual_review": {
            "status": visual_state,
            "manifest": str(args.review_manifest.resolve()) if args.review_manifest else None,
            "reviewed_pages": review_report.get("reviewed_pages", []),
            "issues": review_report.get("issues", []),
        },
        "user_acceptance": {"status": "NOT_CHECKED", "settable_by": "USER_ONLY"},
        "source": {"path": str(args.source.resolve()), "sha256": source_hash},
        "baseline": {"path": str(args.baseline.resolve()), "sha256": baseline_hash} if args.baseline else None,
        "candidate": {"path": str(args.candidate.resolve()), "sha256": candidate_hash},
        "stable_qa_report": stable_qa_report,
        "allowlist": {
            "path": str(args.allowlist.resolve()) if args.allowlist else None,
            "default_token_count": len(DEFAULT_ALLOWLIST),
            "custom_exact_or_regex_count": len(exact_allowlist) - len({item.casefold() for item in DEFAULT_ALLOWLIST}) + len(regex_strings),
            **allowlist_metadata,
        },
        "pages": page_reports,
        "limitations": [
            "Geometry and font findings are extraction-based suspicions; they require the separate hash-bound visual review.",
            "Image-text completion cannot be granted by extraction or OCR alone.",
            "For v3 JSON allowlists this coarse PDF-wide scanner enforces page scope; stable-ID and visual-ID scope is verified by the dedicated rebuilt-PDF and preserved-visual QA gates.",
            "When a hash-bound rebuilt-machine-qa/v1 PASS report is supplied, only raw protected-token extraction-count differences are downgraded; the exact stable-ID gate remains authoritative.",
            "A selected-page MACHINE_QA_PASS is not a full-document pass.",
            "This script always reports USER_ACCEPTANCE as NOT_CHECKED.",
        ],
    }
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    emit_json(report)
    return {"INTERNAL_QA_COMPLETE": 0, "BLOCKED": 2, "NEEDS_REVIEW": 3}[decision]


if __name__ == "__main__":
    raise SystemExit(main())
