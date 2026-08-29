#!/usr/bin/env python3
"""Machine QA for source-bound stable-ID proof PDFs."""

from __future__ import annotations

import argparse
import copy
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import fitz

from _console import emit_json
from _compound_components import (
    expected_candidate_drawing_signatures,
    validate_english_allowlist,
)
from _drawing_signature import (
    DrawingSignatureError,
    drawing_records,
    drawing_records_equal,
    operator_counts,
    record_is_single_rect,
    signature_contract,
    unmatched_drawing_records,
)
from _segment_common import (
    PRESERVE_ACTIONS,
    bbox_inside,
    normalize_bbox,
    read_json,
    sha256_file,
    validate_manifest,
    write_json,
)
from rebuild_pdf import SOURCE_IMAGE_BBOX_TOLERANCE_PT, image_signature, match_source_image_placements


ENGLISH_RE = re.compile(r"[A-Za-z][A-Za-z0-9._/-]+")


def scoped_allowlist_tokens(
    allowed_items: Iterable[Any],
) -> tuple[dict[int, set[str]], set[tuple[int, str, str]]]:
    """Index ordinary English exceptions by their exact page and segment scope."""

    tokens_by_page: dict[int, set[str]] = {}
    exact_text_by_page_and_segment: set[tuple[int, str, str]] = set()
    for item in allowed_items:
        if not isinstance(item, dict):
            continue
        item_text = str(item.get("token", ""))
        item_scope = item.get("scope") or {}
        item_pages = [int(value) for value in item_scope.get("pages") or []]
        item_segment_ids = [str(value) for value in item_scope.get("segment_ids") or []]
        for page_number in item_pages:
            tokens_by_page.setdefault(page_number, set()).update(
                ENGLISH_RE.findall(item_text)
            )
            for segment_id in item_segment_ids:
                exact_text_by_page_and_segment.add(
                    (page_number, segment_id, item_text)
                )
    return tokens_by_page, exact_text_by_page_and_segment


def drawing_signatures(page: fitz.Page) -> list[dict[str, Any]]:
    """Return complete, ordered, multiplicity-preserving drawing signatures."""
    return drawing_records(page)


def matching_rendered_spans(
    page: fitz.Page, text: str, bbox: Iterable[float]
) -> list[dict[str, Any]]:
    normalized_target = re.sub(r"\s+", "", text)
    result: list[dict[str, Any]] = []
    for block in page.get_text("dict", flags=fitz.TEXTFLAGS_DICT).get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                span_text = str(span.get("text", ""))
                if (
                    span_text
                    and re.sub(r"\s+", "", span_text) == normalized_target
                    and intersection_area(span.get("bbox", ()), bbox) > 0.01
                ):
                    result.append(
                        {
                            "text": span_text,
                            "bbox": normalize_bbox(span.get("bbox", ())),
                            "font": str(span.get("font", "")),
                            "size_pt": round(float(span.get("size", 0.0)), 3),
                            "flags": int(span.get("flags", 0)),
                            "is_bold": bool(int(span.get("flags", 0)) & fitz.TEXT_FONT_BOLD),
                            "color_srgb": int(span.get("color", 0)) & 0xFFFFFF,
                            "color_rgb": [
                                round(((int(span.get("color", 0)) >> shift) & 0xFF) / 255.0, 6)
                                for shift in (16, 8, 0)
                            ],
                        }
                    )
    return result


def intersection_area(first: Iterable[float], second: Iterable[float]) -> float:
    ax0, ay0, ax1, ay1 = normalize_bbox(first)
    bx0, by0, bx1, by1 = normalize_bbox(second)
    return max(0.0, min(ax1, bx1) - max(ax0, bx0)) * max(0.0, min(ay1, by1) - max(ay0, by0))


def union_bbox(items: Iterable[Iterable[float]]) -> list[float]:
    boxes = [normalize_bbox(item) for item in items]
    if not boxes:
        raise ValueError("At least one bbox is required")
    return [
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    ]


def alignment_measurement(
    contract: dict[str, Any], candidate_text_bbox: Iterable[float]
) -> dict[str, Any]:
    """Compare source and candidate anchor insets using actual extracted text bounds."""

    alignment = str(contract["alignment"])
    source_reference = normalize_bbox(contract["source_reference_bbox"])
    target_reference = normalize_bbox(contract["target_reference_bbox"])
    source_text = normalize_bbox(contract["source_text_bbox"])
    candidate_text = normalize_bbox(candidate_text_bbox)
    if alignment == "left":
        source_anchor_offset = source_text[0] - source_reference[0]
        candidate_anchor_offset = candidate_text[0] - target_reference[0]
        anchor = "left_inset"
    elif alignment == "right":
        source_anchor_offset = source_reference[2] - source_text[2]
        candidate_anchor_offset = target_reference[2] - candidate_text[2]
        anchor = "right_inset"
    elif alignment == "center":
        source_anchor_offset = ((source_text[0] + source_text[2]) / 2.0) - (
            (source_reference[0] + source_reference[2]) / 2.0
        )
        candidate_anchor_offset = ((candidate_text[0] + candidate_text[2]) / 2.0) - (
            (target_reference[0] + target_reference[2]) / 2.0
        )
        anchor = "center_offset"
    else:
        raise ValueError(f"Unsupported alignment: {alignment!r}")
    delta = abs(candidate_anchor_offset - source_anchor_offset)
    maximum = float(contract["maximum_delta_pt"])
    return {
        "alignment": alignment,
        "anchor": anchor,
        "source_anchor_offset_pt": round(source_anchor_offset, 4),
        "candidate_anchor_offset_pt": round(candidate_anchor_offset, 4),
        "delta_pt": round(delta, 4),
        "maximum_delta_pt": maximum,
        "source_reference_bbox": source_reference,
        "target_reference_bbox": target_reference,
        "source_text_bbox": source_text,
        "candidate_text_bbox": candidate_text,
        "status": "PASS" if delta <= maximum + 1e-6 else "FAIL",
    }


def text_color_measurement(
    expected_srgb: int, actual_spans: Iterable[dict[str, Any]]
) -> dict[str, Any]:
    """Compare an expected 24-bit sRGB value with every bound candidate span."""

    expected = int(expected_srgb) & 0xFFFFFF
    actual = [int(span.get("color_srgb", -1)) for span in actual_spans]
    return {
        "expected_srgb": expected,
        "actual_srgb": actual,
        "status": "PASS" if actual and all(value == expected for value in actual) else "FAIL",
    }


def add_block(blocking: list[dict[str, Any]], code: str, message: str, evidence: Any = None) -> None:
    item: dict[str, Any] = {"code": code, "message": message}
    if evidence is not None:
        item["evidence"] = evidence
    blocking.append(item)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run machine QA on a rebuilt proof PDF.")
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--rebuild-report", required=True, type=Path)
    parser.add_argument("--allowlist", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    source_path = args.source.resolve()
    candidate_path = args.candidate.resolve()
    manifest_path = args.manifest.resolve()
    rebuild_path = args.rebuild_report.resolve()
    allowlist_path = args.allowlist.resolve()
    manifest = read_json(manifest_path)
    rebuild = read_json(rebuild_path)
    allowlist_payload = read_json(allowlist_path)
    allowed_items = allowlist_payload.get("allowed") or []
    (
        allowed_tokens_by_page,
        allowed_exact_text_by_page_and_segment,
    ) = scoped_allowlist_tokens(allowed_items)
    allowed_ui_items = [
        *(allowlist_payload.get("allowed_ui_english") or []),
        *(allowlist_payload.get("allowed_visual_english") or []),
    ]
    blocking: list[dict[str, Any]] = []
    suspicious: list[dict[str, Any]] = []

    for allowlist_issue in validate_english_allowlist(allowlist_payload):
        add_block(
            blocking,
            allowlist_issue["code"],
            allowlist_issue["message"],
            allowlist_issue,
        )

    manifest_by_id = {
        str(segment["segment_id"]): segment for segment in manifest.get("segments") or []
    }
    for entry_index, item in enumerate(allowed_items):
        if not isinstance(item, dict):
            continue
        item_text = str(item.get("token", ""))
        item_scope = item.get("scope") or {}
        item_pages = {int(value) for value in item_scope.get("pages") or []}
        for segment_id in item_scope.get("segment_ids") or []:
            segment = manifest_by_id.get(str(segment_id))
            if segment is None:
                add_block(
                    blocking,
                    "ALLOWLIST_SEGMENT_NOT_FOUND",
                    "Allowlist scope names a segment outside the current manifest",
                    {"entry_index": entry_index, "segment_id": segment_id},
                )
                continue
            segment_page = int(segment.get("page", 0))
            if segment_page not in item_pages:
                add_block(
                    blocking,
                    "ALLOWLIST_SEGMENT_PAGE_MISMATCH",
                    "Allowlist entry pages do not contain every scoped segment page",
                    {
                        "entry_index": entry_index,
                        "segment_id": segment_id,
                        "segment_page": segment_page,
                    },
                )
            bound_text = "\n".join(
                [str(segment.get("source_text", "")), str(segment.get("zh_TW", ""))]
            )
            if item_text not in bound_text:
                add_block(
                    blocking,
                    "ALLOWLIST_TEXT_NOT_IN_SCOPED_SEGMENT",
                    "Allowlist text is absent from a scoped segment's source and target text",
                    {
                        "entry_index": entry_index,
                        "segment_id": segment_id,
                        "token": item_text,
                    },
                )

    manifest_issues = validate_manifest(manifest, require_translation=True, require_render=True)
    for item in manifest_issues:
        if item.get("severity") in {"BLOCKING", "NEEDS_REVIEW"}:
            add_block(blocking, "MANIFEST_BLOCKED", item.get("message", "manifest issue"), item)
    semantic_binding_count = sum(
        len(segment.get("semantic_bindings") or []) for segment in manifest.get("segments") or []
    )
    semantic_issue_codes = {
        "CROSS_PAGE_CONTEXT_NOT_CHECKED",
        "DOCUMENT_CONTEXT_SOURCE_NOT_CHECKED",
        "DOCUMENT_CONTEXT_REF_MISMATCH",
        "SEMANTIC_BINDING_MISSING",
        "DUPLICATE_SEMANTIC_BINDING",
        "DUPLICATE_TRANSLATION_ASSERTION",
        "SEMANTIC_ASSERTION_UNKNOWN_BINDING",
        "SEMANTIC_TARGET_PHRASE_NOT_FOUND",
        "SEMANTIC_TARGET_CUE_DROPPED",
        "VALUE_ROLE_SWAPPED",
        "MODE_CONTEXT_DROPPED",
        "CONDITION_SCOPE_DROPPED",
        "COMPARISON_LOGIC_DROPPED",
        "CONSEQUENCE_DROPPED",
        "CLARIFICATION_POLICY_INVALID",
        "CLARIFICATION_BINDING_FIELDS",
        "CLARIFICATION_TARGET_CUES",
        "CLARIFICATION_SOURCE_NOT_VERIFIED",
        "CLARIFICATION_POLICY_DROPPED",
    }
    semantic_manifest_issues = [
        item for item in manifest_issues if item.get("code") in semantic_issue_codes
    ]

    if not source_path.is_file() or not candidate_path.is_file():
        raise FileNotFoundError("Source and candidate must both exist")
    source_hash = sha256_file(source_path)
    candidate_hash = sha256_file(candidate_path)
    if source_hash != manifest["source"]["sha256"]:
        add_block(blocking, "SOURCE_HASH_MISMATCH", "Source SHA-256 differs from manifest")
    if candidate_hash != rebuild["output"]["sha256"]:
        add_block(blocking, "CANDIDATE_HASH_MISMATCH", "Candidate SHA-256 differs from rebuild report")
    if sha256_file(manifest_path) != rebuild["manifest"]["sha256"]:
        add_block(blocking, "MANIFEST_HASH_MISMATCH", "Rebuild report is bound to a different manifest")

    report_segments = rebuild.get("segments") or []
    report_by_id = {item["segment_id"]: item for item in report_segments}
    ordinary_allowed_token_counts_by_page: dict[int, Counter[str]] = {}
    for item in allowed_items:
        if not isinstance(item, dict):
            continue
        item_tokens = set(ENGLISH_RE.findall(str(item.get("token", ""))))
        for segment_id in (item.get("scope") or {}).get("segment_ids") or []:
            segment = manifest_by_id.get(str(segment_id))
            if segment is None:
                continue
            page_number = int(segment.get("page", 0))
            report_text = "\n".join(
                str(line.get("text", ""))
                for line in (
                    report_by_id.get(str(segment_id), {}).get("rendered_lines") or []
                )
            )
            if report_text:
                bound_candidate_text = report_text
            elif str((segment.get("render") or {}).get("action", "")) in PRESERVE_ACTIONS:
                bound_candidate_text = str(segment.get("source_text", ""))
            else:
                bound_candidate_text = str(segment.get("zh_TW", ""))
            observed = Counter(ENGLISH_RE.findall(bound_candidate_text))
            page_counts = ordinary_allowed_token_counts_by_page.setdefault(
                page_number, Counter()
            )
            for token in item_tokens:
                page_counts[token] += observed.get(token, 0)
    background_adjustments = rebuild.get("background_adjustments") or []
    adjustments_by_page: dict[int, list[dict[str, Any]]] = {}
    for adjustment in background_adjustments:
        page_number = int(adjustment.get("source_page", 0))
        adjustments_by_page.setdefault(page_number, []).append(adjustment)
        if adjustment.get("status") != "APPLIED_VERIFIED":
            add_block(
                blocking,
                "BACKGROUND_ADJUSTMENT_NOT_VERIFIED",
                "A declared background adjustment lacks verified application evidence",
                adjustment,
            )
        if int(adjustment.get("source_rect_count_before", 0)) != 1:
            add_block(
                blocking,
                "BACKGROUND_SOURCE_RECT_COUNT",
                "A background adjustment did not bind exactly one source rectangle",
                adjustment,
            )
        if int(adjustment.get("source_rect_count_after", -1)) != 0 or int(
            adjustment.get("target_rect_count_after", 0)
        ) != 1:
            add_block(
                blocking,
                "BACKGROUND_RECT_REWRITE_COUNT",
                "Adjusted source/target rectangle counts are invalid",
                adjustment,
            )
        for avoid in adjustment.get("avoid_regions") or []:
            if float(avoid.get("target_intersection_area", 0.0)) > 0.0001:
                add_block(
                    blocking,
                    "BACKGROUND_AVOID_REGION_INTERSECTION",
                    "Adjusted background still intersects a declared avoid region",
                    {"component_id": adjustment.get("component_id"), "avoid_region": avoid},
                )

    vector_removals_by_page: dict[int, list[dict[str, Any]]] = {}
    vector_rule_members_by_page: dict[int, dict[str, dict[str, Any]]] = {}
    for segment in manifest.get("segments") or []:
        for member in (segment.get("component_contract") or {}).get("members") or []:
            if member.get("policy") == "adjust_vector_rule":
                page_number = int(member.get("source_page", segment.get("page", 0)))
                component_id = str(member.get("component_id", ""))
                previous = vector_rule_members_by_page.setdefault(page_number, {}).setdefault(
                    component_id, member
                )
                if previous != member:
                    add_block(
                        blocking,
                        "VECTOR_RULE_ADJUSTMENT_DECLARATION_CONFLICT",
                        "A vector-rule adjustment has conflicting repeated declarations",
                        component_id,
                    )
            if member.get("policy") != "replace_vector_outlined_text":
                continue
            page_number = int(member.get("source_page", segment.get("page", 0)))
            declarations = (
                (member.get("source_evidence") or {})
                .get("ordered_path_signatures", {})
                .get("drawing_signatures", [])
            )
            bucket = vector_removals_by_page.setdefault(page_number, [])
            for declaration in declarations:
                if declaration not in bucket:
                    bucket.append(declaration)

    vector_rule_adjustments = rebuild.get("vector_rule_adjustments") or []
    rule_adjustment_report_by_id = {
        str(item.get("component_id")): item for item in vector_rule_adjustments
    }
    expected_rule_adjustment_ids = {
        component_id
        for page_members in vector_rule_members_by_page.values()
        for component_id in page_members
    }
    if set(rule_adjustment_report_by_id) != expected_rule_adjustment_ids:
        add_block(
            blocking,
            "VECTOR_RULE_ADJUSTMENT_REPORT_SCOPE",
            "Rebuild rule-adjustment evidence does not match the manifest scope",
            {
                "expected": sorted(expected_rule_adjustment_ids),
                "reported": sorted(rule_adjustment_report_by_id),
            },
        )
    for adjustment in vector_rule_adjustments:
        if (
            adjustment.get("status") != "APPLIED_VERIFIED"
            or int(adjustment.get("source_path_count_after", -1)) != 0
            or int(adjustment.get("target_path_count_after", 0)) != 1
            or int(adjustment.get("source_drawing_count_after", -1)) != 0
            or int(adjustment.get("target_drawing_count_after", 0)) != 1
        ):
            add_block(
                blocking,
                "VECTOR_RULE_ADJUSTMENT_NOT_VERIFIED",
                "A vector-rule adjustment lacks exact source-absence and target-presence evidence",
                adjustment,
            )

    font_role_validation = rebuild.get("font_role_validation") or {}
    if font_role_validation.get("status") != "PASS":
        add_block(
            blocking,
            "FONT_ROLE_VALIDATION_MISSING",
            "Rebuild report lacks a passing regular/bold font-role validation",
            font_role_validation,
        )
    scoped_ui_token_counts: Counter[str] = Counter()
    scoped_ui_token_counts_by_page: dict[int, Counter[str]] = {}
    scoped_ui_keys: set[tuple[str, str]] = set()
    for entry in allowed_ui_items:
        source_text = str(entry.get("source_text", ""))
        zh_text = str(entry.get("zh_TW", ""))
        visual_ids = [str(value) for value in entry.get("visual_ids") or []]
        guidance_ids = [str(value) for value in entry.get("guidance_segment_ids") or []]
        if not source_text or not zh_text or not visual_ids:
            add_block(blocking, "UI_ALLOWLIST_INVALID", "Scoped UI allowlist entry is incomplete", entry)
            continue
        for visual_id in visual_ids:
            scoped_ui_keys.add((visual_id, source_text))
        phrase_token_counts = Counter(ENGLISH_RE.findall(source_text))
        observed_phrase_count = 0
        for guidance_id in guidance_ids:
            rendered_text = "\n".join(
                line.get("text", "")
                for line in (report_by_id.get(guidance_id, {}).get("rendered_lines") or [])
            )
            phrase_count = rendered_text.count(source_text)
            observed_phrase_count += phrase_count
            guidance_page = int(report_by_id.get(guidance_id, {}).get("page", 0))
            for token, count in phrase_token_counts.items():
                scoped_ui_token_counts[token] += count * phrase_count
                if guidance_page > 0:
                    scoped_ui_token_counts_by_page.setdefault(guidance_page, Counter())[token] += count * phrase_count
        if entry.get("guidance_required", True) and observed_phrase_count < 1:
            add_block(
                blocking,
                "UI_GUIDANCE_MISSING",
                f"Required adjacent UI phrase is absent: {zh_text}（{source_text}）",
                {"guidance_segment_ids": guidance_ids},
            )

    source_doc = fitz.open(source_path)
    candidate_doc = fitz.open(candidate_path)
    selected_pages = [int(page) for page in manifest["selected_pages"]]
    if candidate_doc.page_count != len(selected_pages):
        add_block(
            blocking,
            "PAGE_COUNT",
            "Candidate page count does not equal selected proof scope",
            {"expected": len(selected_pages), "actual": candidate_doc.page_count},
        )

    page_results: list[dict[str, Any]] = []
    candidate_text_by_page: dict[int, str] = {}
    image_text_status_by_page = {
        int(packet["page"]): packet.get("image_text_inventory_status", "NOT_CHECKED")
        for packet in manifest.get("page_contexts") or []
    }
    for output_index, source_page_number in enumerate(selected_pages):
        source_page = source_doc[source_page_number - 1]
        if output_index >= candidate_doc.page_count:
            continue
        candidate_page = candidate_doc[output_index]
        source_size = [round(source_page.rect.width, 3), round(source_page.rect.height, 3)]
        candidate_size = [round(candidate_page.rect.width, 3), round(candidate_page.rect.height, 3)]
        size_match = source_size == candidate_size
        if not size_match:
            add_block(
                blocking,
                "PAGE_SIZE",
                f"Page size changed for source page {source_page_number}",
                {"source": source_size, "candidate": candidate_size},
            )

        source_drawings: list[dict[str, Any]] = []
        candidate_drawings: list[dict[str, Any]] = []
        drawing_signature_errors: list[dict[str, Any]] = []
        for input_role, input_page in (
            ("source", source_page),
            ("candidate", candidate_page),
        ):
            try:
                records = drawing_signatures(input_page)
                if input_role == "source":
                    source_drawings = records
                else:
                    candidate_drawings = records
            except DrawingSignatureError as exc:
                evidence = {
                    "input_role": input_role,
                    "source_page": source_page_number,
                    "candidate_page": output_index + 1,
                    **exc.as_dict(),
                }
                drawing_signature_errors.append(evidence)
                add_block(
                    blocking,
                    "DRAWING_SIGNATURE_FAIL_CLOSED",
                    f"{input_role.title()} drawing cannot be signed without discarding path data",
                    evidence,
                )

        allowed_adjusted_source_signatures: list[dict[str, Any]] = []
        missing_adjusted_targets: list[dict[str, Any]] = []
        allowed_adjusted_rule_source_signatures: list[dict[str, Any]] = []
        missing_adjusted_rule_targets: list[dict[str, Any]] = []
        if not drawing_signature_errors:
            for adjustment in adjustments_by_page.get(source_page_number, []):
                try:
                    source_matches = [
                        record
                        for record in source_drawings
                        if record_is_single_rect(
                            record,
                            adjustment.get("source_bbox", ()),
                            adjustment.get("expected_fill"),
                        )
                    ]
                    target_matches = [
                        record
                        for record in candidate_drawings
                        if record_is_single_rect(
                            record,
                            adjustment.get("target_bbox", ()),
                            adjustment.get("expected_fill"),
                        )
                    ]
                except DrawingSignatureError as exc:
                    add_block(
                        blocking,
                        "BACKGROUND_DRAWING_SIGNATURE_INVALID",
                        "A declared background adjustment cannot be matched safely",
                        {
                            "component_id": adjustment.get("component_id"),
                            **exc.as_dict(),
                        },
                    )
                    continue
                if len(source_matches) != 1:
                    add_block(
                        blocking,
                        "BACKGROUND_SOURCE_DRAWING_SIGNATURE_COUNT",
                        "A declared adjustment must bind exactly one complete source rectangle drawing",
                        {
                            "component_id": adjustment.get("component_id"),
                            "match_count": len(source_matches),
                            "source_bbox": adjustment.get("source_bbox"),
                        },
                    )
                else:
                    allowed_adjusted_source_signatures.extend(source_matches)
                if len(target_matches) != 1:
                    missing_adjusted_targets.append(
                        {
                            "component_id": adjustment.get("component_id"),
                            "target_bbox": adjustment.get("target_bbox"),
                            "match_count": len(target_matches),
                        }
                    )

            for component_id, member in vector_rule_members_by_page.get(
                source_page_number, {}
            ).items():
                candidate_member = copy.deepcopy(member)
                report_item = rule_adjustment_report_by_id.get(component_id) or {}
                if report_item.get("target_bbox") is not None:
                    candidate_member["target_bbox"] = report_item["target_bbox"]
                if report_item.get("translation_delta_pt") is not None:
                    candidate_member["translation_delta_pt"] = report_item[
                        "translation_delta_pt"
                    ]
                source_declarations = (
                    (member.get("source_evidence") or {})
                    .get("ordered_path_signatures", {})
                    .get("drawing_signatures", [])
                )
                try:
                    target_declarations = expected_candidate_drawing_signatures(
                        candidate_member
                    )
                except Exception as exc:
                    add_block(
                        blocking,
                        "VECTOR_RULE_TARGET_SIGNATURE_INVALID",
                        "An adjusted rule target signature cannot be derived safely",
                        {"component_id": component_id, "message": str(exc)},
                    )
                    continue
                source_matches = [
                    record
                    for record in source_drawings
                    if any(
                        drawing_records_equal(record, declaration)
                        for declaration in source_declarations
                    )
                ]
                target_matches = [
                    record
                    for record in candidate_drawings
                    if any(
                        drawing_records_equal(record, declaration)
                        for declaration in target_declarations
                    )
                ]
                if (
                    len(source_declarations) != 1
                    or len(source_matches) != 1
                    or report_item.get("status") != "APPLIED_VERIFIED"
                    or normalize_bbox(report_item.get("source_bbox", ()))
                    != normalize_bbox(member.get("bbox", ()))
                    or normalize_bbox(report_item.get("target_bbox", ()))
                    != normalize_bbox(member.get("target_bbox", ()))
                ):
                    add_block(
                        blocking,
                        "VECTOR_RULE_SOURCE_SIGNATURE_COUNT",
                        "A declared rule adjustment must bind one exact source drawing and matching report evidence",
                        {
                            "component_id": component_id,
                            "source_declaration_count": len(source_declarations),
                            "source_match_count": len(source_matches),
                            "report": report_item,
                        },
                    )
                else:
                    allowed_adjusted_rule_source_signatures.extend(source_matches)
                if len(target_declarations) != 1 or len(target_matches) != 1:
                    missing_adjusted_rule_targets.append(
                        {
                            "component_id": component_id,
                            "target_bbox": member.get("target_bbox"),
                            "target_declaration_count": len(target_declarations),
                            "target_match_count": len(target_matches),
                        }
                    )

        raw_missing_drawings = (
            unmatched_drawing_records(source_drawings, candidate_drawings)
            if not drawing_signature_errors
            else []
        )
        allowed_vector_source_signatures = vector_removals_by_page.get(
            source_page_number, []
        )
        allowed_source_changes = [
            *allowed_adjusted_source_signatures,
            *allowed_adjusted_rule_source_signatures,
            *allowed_vector_source_signatures,
        ]
        missing_drawings = unmatched_drawing_records(
            raw_missing_drawings, allowed_source_changes
        )
        missing_declared_adjustments = unmatched_drawing_records(
            allowed_adjusted_source_signatures, raw_missing_drawings
        )
        missing_declared_vector_removals = unmatched_drawing_records(
            allowed_vector_source_signatures, raw_missing_drawings
        )
        missing_declared_rule_adjustments = unmatched_drawing_records(
            allowed_adjusted_rule_source_signatures, raw_missing_drawings
        )
        if missing_declared_adjustments:
            add_block(
                blocking,
                "BACKGROUND_SOURCE_DRAWING_NOT_REPLACED",
                f"A declared source background still remains on page {source_page_number}",
                missing_declared_adjustments,
            )
        if missing_adjusted_targets:
            add_block(
                blocking,
                "BACKGROUND_TARGET_DRAWING_MISSING",
                f"An adjusted background target is missing on page {source_page_number}",
                missing_adjusted_targets,
            )
        if missing_declared_rule_adjustments:
            add_block(
                blocking,
                "VECTOR_RULE_SOURCE_DRAWING_NOT_MOVED",
                f"A declared source rule still remains on page {source_page_number}",
                missing_declared_rule_adjustments,
            )
        if missing_adjusted_rule_targets:
            add_block(
                blocking,
                "VECTOR_RULE_TARGET_DRAWING_MISSING",
                f"An adjusted rule target is missing on page {source_page_number}",
                missing_adjusted_rule_targets,
            )
        if missing_declared_vector_removals:
            add_block(
                blocking,
                "VECTOR_OUTLINED_TEXT_NOT_REMOVED",
                f"Declared outlined-text paths still remain on page {source_page_number}",
                missing_declared_vector_removals,
            )
        if missing_drawings:
            add_block(
                blocking,
                "SOURCE_LINE_ART_MISSING",
                f"Source line art is missing on page {source_page_number}",
                missing_drawings[:25],
            )

        source_images = source_page.get_image_info(hashes=True, xrefs=True)
        candidate_images = candidate_page.get_image_info(hashes=True, xrefs=True)
        image_matches, missing_image_items = match_source_image_placements(
            source_images, candidate_images
        )
        missing_images = [image_signature(item) for item in missing_image_items]
        if missing_images:
            add_block(
                blocking,
                "SOURCE_IMAGE_MISSING",
                f"Source images are missing on page {source_page_number}",
                missing_images,
            )

        candidate_text = candidate_page.get_text("text")
        candidate_text_by_page[source_page_number] = candidate_text
        english_tokens = ENGLISH_RE.findall(candidate_text)
        english_counts = Counter(english_tokens)
        page_scoped_counts = scoped_ui_token_counts_by_page.get(source_page_number, Counter())
        ordinary_allowed_counts = ordinary_allowed_token_counts_by_page.get(
            source_page_number, Counter()
        )
        combined_allowed_counts = ordinary_allowed_counts + page_scoped_counts
        residue_counts = {
            token: count - combined_allowed_counts.get(token, 0)
            for token, count in english_counts.items()
            if count > combined_allowed_counts.get(token, 0)
        }
        residue = sorted(residue_counts)
        if residue:
            add_block(
                blocking,
                "ENGLISH_RESIDUE",
                f"Unallowlisted extractable English remains on page {source_page_number}",
                residue_counts,
            )
        page_results.append(
            {
                "source_page": source_page_number,
                "candidate_page": output_index + 1,
                "source_size": source_size,
                "candidate_size": candidate_size,
                "size_match": size_match,
                "source_drawing_signature_count": len(source_drawings),
                "candidate_drawing_signature_count": len(candidate_drawings),
                "source_path_operator_counts": operator_counts(source_drawings),
                "candidate_path_operator_counts": operator_counts(candidate_drawings),
                "drawing_signature_status": (
                    "PASS" if not drawing_signature_errors else "BLOCKED_FAIL_CLOSED"
                ),
                "drawing_signature_errors": drawing_signature_errors,
                "missing_source_drawing_count": len(missing_drawings),
                "raw_missing_source_drawing_count": len(raw_missing_drawings),
                "declared_adjusted_source_drawing_count": len(
                    allowed_adjusted_source_signatures
                ),
                "declared_adjusted_rule_source_drawing_count": len(
                    allowed_adjusted_rule_source_signatures
                ),
                "declared_removed_vector_drawing_count": len(
                    allowed_vector_source_signatures
                ),
                "missing_declared_vector_removal_count": len(
                    missing_declared_vector_removals
                ),
                "missing_adjusted_target_count": len(missing_adjusted_targets),
                "missing_adjusted_rule_target_count": len(
                    missing_adjusted_rule_targets
                ),
                "source_image_count": len(source_images),
                "candidate_image_count": len(candidate_images),
                "matched_source_image_count": len(image_matches),
                "source_image_bbox_tolerance_pt": SOURCE_IMAGE_BBOX_TOLERANCE_PT,
                "maximum_matched_image_bbox_delta_pt": max(
                    (item["maximum_bbox_delta_pt"] for item in image_matches),
                    default=0.0,
                ),
                "missing_source_image_count": len(missing_images),
                "extractable_english_tokens": english_tokens,
                "unallowlisted_english_residue": residue,
                "unallowlisted_english_residue_counts": residue_counts,
                "ordinary_scoped_allowed_token_counts": dict(ordinary_allowed_counts),
                "scoped_ui_allowed_token_counts": dict(page_scoped_counts),
                "image_text_residue_machine_status": (
                    "PRESERVED_SOURCE_VISUAL_REQUIRES_IDENTITY_QA_AND_300_DPI_VISUAL_REVIEW"
                    if str(image_text_status_by_page.get(source_page_number, "")).startswith(
                        "NOT_APPLICABLE_USER_PERMITTED_SOURCE_UI"
                    )
                    else (
                        "NOT_APPLICABLE_NO_IMAGE_TEXT"
                        if str(image_text_status_by_page.get(source_page_number, "")).startswith("NOT_APPLICABLE")
                        else "NOT_CHECKED_REQUIRES_300_DPI_VISUAL_REVIEW"
                    )
                ),
            }
        )

    manifest_ids = {segment["segment_id"] for segment in manifest["segments"]}
    report_ids = {segment["segment_id"] for segment in report_segments}
    if manifest_ids != report_ids:
        add_block(
            blocking,
            "RENDER_COVERAGE",
            "Rebuild report does not cover every manifest segment exactly once",
            {"missing": sorted(manifest_ids - report_ids), "unexpected": sorted(report_ids - manifest_ids)},
        )
    duplicate_report_ids = sorted(
        identifier for identifier, count in Counter(item["segment_id"] for item in report_segments).items() if count > 1
    )
    if duplicate_report_ids:
        add_block(blocking, "DUPLICATE_RENDER_ID", "Duplicate rendered segment IDs", duplicate_report_ids)

    rendered_lines: list[dict[str, Any]] = []
    rendered_font_roles: list[dict[str, Any]] = []
    foreground_color_validation: list[dict[str, Any]] = []
    text_alignment_validation: list[dict[str, Any]] = []
    for segment_result in report_segments:
        if segment_result.get("action") in PRESERVE_ACTIONS:
            continue
        used_size = float(segment_result["used_font_size_pt"])
        source_size = float(segment_result.get("source_font_size_pt") or 0)
        segment = next(item for item in manifest["segments"] if item["segment_id"] == segment_result["segment_id"])
        requested_font_role = "bold" if bool((segment.get("font_style") or {}).get("bold")) else "regular"
        declared_font_role = str(segment_result.get("requested_font_role", ""))
        declared_font_evidence = segment_result.get("font_role_evidence") or {}
        if declared_font_role != requested_font_role:
            add_block(
                blocking,
                "SEGMENT_FONT_ROLE_MISMATCH",
                f"Rendered segment used the wrong font role: {segment_result['segment_id']}",
                {"requested": requested_font_role, "reported": declared_font_role},
            )
        if requested_font_role == "bold" and not bool(declared_font_evidence.get("is_bold")):
            add_block(
                blocking,
                "DECLARED_BOLD_FACE_NOT_BOLD",
                f"Bold segment lacks bold-face evidence: {segment_result['segment_id']}",
                declared_font_evidence,
            )
        exception = segment_result.get("source_small_exception")
        minimum = max(6.0, source_size * 0.75)
        if used_size + 1e-6 < minimum and not exception:
            add_block(
                blocking,
                "FONT_RATIO",
                f"Rendered font is below minimum for {segment_result['segment_id']}",
                {"used": used_size, "minimum": minimum},
            )
        if exception:
            suspicious.append(
                {
                    "code": "SOURCE_SMALL_EXCEPTION",
                    "segment_id": segment_result["segment_id"],
                    "reason": exception,
                    "used_font_size_pt": used_size,
                }
            )
        container = segment_result.get("container_bbox")
        if segment["semantic_type"] == "table-cell" and not container:
            add_block(
                blocking,
                "TABLE_CONTAINER_MISSING",
                f"table-cell lacks render.container_bbox: {segment_result['segment_id']}",
            )
        segment_lines = segment_result.get("rendered_lines") or []
        actual_spans_for_segment: list[dict[str, Any]] = []
        bound_line_count = 0
        for line in segment_lines:
            record = {
                "segment_id": segment_result["segment_id"],
                "page": segment_result["page"],
                "bbox": line["bbox"],
                "text": line["text"],
            }
            rendered_lines.append(record)
            if not bbox_inside(line["bbox"], segment_result["target_bbox"], tolerance=0.35):
                add_block(blocking, "CLIPPING", "Rendered line leaves target bbox", record)
            if container and not bbox_inside(line["bbox"], container, tolerance=0.35):
                add_block(blocking, "TABLE_CONTAINMENT", "Rendered line leaves table cell", record)
            output_index = selected_pages.index(int(segment_result["page"]))
            actual_spans = matching_rendered_spans(
                candidate_doc[output_index], str(line["text"]), line["bbox"]
            )
            if actual_spans:
                bound_line_count += 1
                actual_spans_for_segment.extend(actual_spans)
            role_result = {
                "segment_id": segment_result["segment_id"],
                "page": segment_result["page"],
                "requested_role": requested_font_role,
                "declared_role": declared_font_role,
                "declared_font": declared_font_evidence,
                "actual_spans": actual_spans,
            }
            rendered_font_roles.append(role_result)
            expected_srgb = line.get("text_color_srgb")
            if expected_srgb is not None:
                color_result = {
                    "segment_id": segment_result["segment_id"],
                    "fragment_id": line.get("fragment_id"),
                    "page": segment_result["page"],
                    "text": line["text"],
                    "text_color_resolution": line.get("text_color_resolution"),
                    **text_color_measurement(int(expected_srgb), actual_spans),
                }
                foreground_color_validation.append(color_result)
                if color_result["status"] != "PASS":
                    add_block(
                        blocking,
                        "ACTUAL_TEXT_COLOR_MISMATCH",
                        f"Candidate text color differs from the declared source-bound color: {segment_result['segment_id']}",
                        color_result,
                    )
            if not actual_spans:
                add_block(
                    blocking,
                    "RENDERED_FONT_SPAN_NOT_FOUND",
                    f"Could not bind rendered text to an extracted font span: {segment_result['segment_id']}",
                    record,
                )
            elif requested_font_role == "bold" and not any(
                span["is_bold"] or "bold" in span["font"].lower() for span in actual_spans
            ):
                add_block(
                    blocking,
                    "ACTUAL_BOLD_SPAN_NOT_BOLD",
                    f"Candidate identifies a requested bold line as a non-bold face: {segment_result['segment_id']}",
                    role_result,
                )
            elif requested_font_role == "regular" and all(
                span["is_bold"] or "bold" in span["font"].lower() for span in actual_spans
            ):
                add_block(
                    blocking,
                    "ACTUAL_REGULAR_SPAN_IS_BOLD",
                    f"Candidate identifies a requested regular line as bold: {segment_result['segment_id']}",
                    role_result,
                )

        alignment_contract = (segment.get("render") or {}).get("alignment_contract")
        if alignment_contract is not None:
            alignment_result: dict[str, Any] = {
                "segment_id": segment_result["segment_id"],
                "page": segment_result["page"],
                "measurement_basis": alignment_contract.get("measurement_basis"),
                "expected_line_count": len(segment_lines),
                "bound_line_count": bound_line_count,
            }
            if not actual_spans_for_segment or bound_line_count != len(segment_lines):
                alignment_result["status"] = "BLOCKED_UNBOUND_ACTUAL_TEXT"
                add_block(
                    blocking,
                    "TEXT_ALIGNMENT_ACTUAL_TEXT_UNBOUND",
                    f"Alignment cannot be verified against every actual candidate line: {segment_result['segment_id']}",
                    alignment_result,
                )
            else:
                try:
                    unique_span_boxes = {
                        tuple(round(value, 4) for value in span["bbox"])
                        for span in actual_spans_for_segment
                    }
                    measured = alignment_measurement(
                        alignment_contract, union_bbox(unique_span_boxes)
                    )
                    alignment_result.update(measured)
                    if measured["status"] != "PASS":
                        add_block(
                            blocking,
                            "ACTUAL_TEXT_ALIGNMENT_MISMATCH",
                            f"Candidate text anchor differs from the source alignment contract: {segment_result['segment_id']}",
                            alignment_result,
                        )
                except Exception as exc:
                    alignment_result.update(
                        {"status": "BLOCKED_MEASUREMENT_ERROR", "message": str(exc)}
                    )
                    add_block(
                        blocking,
                        "TEXT_ALIGNMENT_MEASUREMENT_FAILED",
                        f"Candidate text alignment could not be measured: {segment_result['segment_id']}",
                        alignment_result,
                    )
            text_alignment_validation.append(alignment_result)

    overlaps: list[dict[str, Any]] = []
    for index, first in enumerate(rendered_lines):
        for second in rendered_lines[index + 1 :]:
            if first["page"] != second["page"] or first["segment_id"] == second["segment_id"]:
                continue
            area = intersection_area(first["bbox"], second["bbox"])
            if area > 0.05:
                overlaps.append(
                    {
                        "first_segment_id": first["segment_id"],
                        "second_segment_id": second["segment_id"],
                        "intersection_area": round(area, 4),
                    }
                )
    if overlaps:
        add_block(blocking, "TEXT_OVERLAP", "Rendered segment lines overlap", overlaps)

    protected_token_evidence: list[dict[str, Any]] = []
    for segment in manifest["segments"]:
        page_number = int(segment["page"])
        candidate_text = candidate_text_by_page.get(page_number, "")
        action = (segment.get("render") or {}).get("action", "replace")
        extraction_method = segment.get("extraction_method")
        if action in PRESERVE_ACTIONS and extraction_method == "visual_annotation":
            for token_item in segment.get("protected_tokens") or []:
                token = token_item["token"] if isinstance(token_item, dict) else str(token_item)
                target_token = token_item.get("target_token", token) if isinstance(token_item, dict) else token
                protected_token_evidence.append(
                    {
                        "segment_id": segment["segment_id"],
                        "source_token": token,
                        "target_token": target_token,
                        "status": "PRESERVED_SOURCE_IMAGE_REQUIRES_VISUAL_CONFIRMATION",
                    }
                )
            continue
        rendered_text = "\n".join(
            line.get("text", "")
            for line in (report_by_id.get(segment["segment_id"], {}).get("rendered_lines") or [])
        )
        for token_item in segment.get("protected_tokens") or []:
            token = token_item["token"] if isinstance(token_item, dict) else str(token_item)
            target_token = token_item.get("target_token", token) if isinstance(token_item, dict) else token
            evidence_method = None
            if target_token in candidate_text:
                evidence_method = "candidate_text_extraction"
            elif target_token in rendered_text:
                evidence_method = "hash_bound_rebuild_rendered_lines"
            elif action in PRESERVE_ACTIONS:
                evidence_method = "preserved_source_vector_text"
            if evidence_method is None:
                add_block(
                    blocking,
                    "PROTECTED_TOKEN_RENDER_MISSING",
                    f"Protected target token is missing from render evidence: {target_token!r}",
                    {"segment_id": segment["segment_id"], "source_token": token},
                )
            else:
                protected_token_evidence.append(
                    {
                        "segment_id": segment["segment_id"],
                        "source_token": token,
                        "target_token": target_token,
                        "status": "PRESERVED",
                        "evidence_method": evidence_method,
                    }
                )

    image_text_segments = [
        segment for segment in manifest["segments"] if segment["semantic_type"] == "image-text"
    ]
    for segment in image_text_segments:
        action = segment["render"].get("action", "replace")
        exact_allowlist_key = (
            int(segment["page"]),
            str(segment["segment_id"]),
            str(segment["source_text"]),
        )
        if action == "preserve" and exact_allowlist_key not in allowed_exact_text_by_page_and_segment:
            add_block(
                blocking,
                "IMAGE_TEXT_UNTRANSLATED",
                f"Image text was preserved without an allowlist entry: {segment['segment_id']}",
            )
        if action == "preserve_source_visual_with_textual_guidance":
            visual_id = str((segment.get("relationships") or {}).get("visual_id", ""))
            key = (visual_id, str(segment.get("source_text", "")))
            if key not in scoped_ui_keys:
                add_block(
                    blocking,
                    "SCOPED_UI_ALLOWLIST_MISSING",
                    f"Preserved source UI is absent from the exact scoped allowlist: {segment['segment_id']}",
                    {"visual_id": visual_id, "source_text": segment.get("source_text")},
                )

    embedded_fonts = rebuild.get("fonts") or []
    proof_fonts = [
        item
        for item in embedded_fonts
        if str(item.get("resource_name", "")).startswith("TWProof")
        or "NotoSans" in str(item.get("basefont", ""))
    ]
    if not proof_fonts or not any(int(item.get("embedded_program_bytes", 0)) > 0 for item in proof_fonts):
        add_block(blocking, "FONT_NOT_EMBEDDED", "No embedded zh-TW proof font program was verified")

    source_doc.close()
    candidate_doc.close()
    report = {
        "schema": "pdf-tw-localize/rebuilt-machine-qa/v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": {"source_pages": selected_pages, "candidate_page_count": len(selected_pages)},
        "source": {"path": str(source_path), "sha256": source_hash},
        "candidate": {"path": str(candidate_path), "sha256": candidate_hash},
        "manifest": {"path": str(manifest_path), "sha256": sha256_file(manifest_path)},
        "rebuild_report": {"path": str(rebuild_path), "sha256": sha256_file(rebuild_path)},
        "allowlist": {
            "path": str(allowlist_path),
            "sha256": sha256_file(allowlist_path),
            "entries": allowed_items,
            "allowed_ui_english": allowed_ui_items,
            "allowed_visual_english": allowlist_payload.get("allowed_visual_english") or [],
        },
        "drawing_signature_contract": signature_contract(),
        "coverage": {
            "manifest_segment_count": len(manifest_ids),
            "render_report_segment_count": len(report_ids),
            "unmapped_source_refs": (manifest.get("coverage") or {}).get("unmapped_source_refs"),
            "duplicate_source_refs": (manifest.get("coverage") or {}).get("duplicate_source_refs"),
            "manual_image_text_segment_count": len(image_text_segments),
        },
        "pages": page_results,
        "font_evidence": proof_fonts,
        "font_role_validation": font_role_validation,
        "rendered_font_roles": rendered_font_roles,
        "foreground_color_validation": foreground_color_validation,
        "text_alignment_validation": text_alignment_validation,
        "background_adjustments": background_adjustments,
        "vector_rule_adjustments": vector_rule_adjustments,
        "protected_token_evidence": protected_token_evidence,
        "semantic_binding_count": semantic_binding_count,
        "semantic_assertion_count": sum(
            len(segment.get("translation_assertions") or []) for segment in manifest.get("segments") or []
        ),
        "semantic_qa": (
            "NOT_APPLICABLE"
            if not semantic_binding_count
            else "SEMANTIC_QA_PASS"
            if not semantic_manifest_issues
            else "BLOCKED"
        ),
        "overlap_count": len(overlaps),
        "blocking_issue_count": len(blocking),
        "blocking_issues": blocking,
        "suspicious_issue_count": len(suspicious),
        "suspicious_issues": suspicious,
        "machine_qa": "MACHINE_QA_PASS" if not blocking else "BLOCKED",
        "visual_review": "NOT_CHECKED",
        "user_acceptance": "NOT_CHECKED",
        "full_document_pass": False,
    }
    write_json(args.output, report)
    emit_json(report)
    return 0 if not blocking else 2


if __name__ == "__main__":
    raise SystemExit(main())
