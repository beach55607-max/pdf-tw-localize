#!/usr/bin/env python3
"""Fail-closed QA for grammar-aware complete inline visual relocation."""

from __future__ import annotations

import argparse
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import fitz

from _console import emit_json
from _inline_visual_sequences import (
    COPY_METHOD,
    LEGACY_TWO_STAGE_MASK_MODE,
    inline_contract,
    validate_inline_visual_sequence,
    validate_inline_visual_sweeps,
    validate_legacy_two_stage_overlay_evidence,
    versioned_inline_contract,
)
from _segment_common import normalize_bbox, read_json, sha256_file, write_json


def same_bbox(first: Iterable[float], second: Iterable[float], tolerance: float = 0.02) -> bool:
    return all(
        abs(left - right) <= tolerance
        for left, right in zip(
            normalize_bbox(first), normalize_bbox(second), strict=True
        )
    )


def rendered_region(page: fitz.Page, bbox: Iterable[float], dpi: int) -> dict[str, Any]:
    scale = dpi / 72.0
    pix = page.get_pixmap(
        matrix=fitz.Matrix(scale, scale), clip=fitz.Rect(bbox), alpha=False
    )
    return {
        "dpi": dpi,
        "width": pix.width,
        "height": pix.height,
        "channels": pix.n,
        "samples_sha256": hashlib.sha256(pix.samples).hexdigest().upper(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify prefix + complete visual A + 或 + complete visual B + suffix sequences."
    )
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--rebuild-report", required=True, type=Path)
    parser.add_argument("--dpi", type=int, default=600)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.dpi < 300:
        raise ValueError("Inline visual identity QA requires 300 dpi or higher")

    source_path = args.source.resolve()
    candidate_path = args.candidate.resolve()
    manifest_path = args.manifest.resolve()
    rebuild_path = args.rebuild_report.resolve()
    manifest = read_json(manifest_path)
    rebuild = read_json(rebuild_path)
    blockers: list[dict[str, Any]] = []

    source_hash = sha256_file(source_path)
    candidate_hash = sha256_file(candidate_path)
    manifest_hash = sha256_file(manifest_path)
    if source_hash != str((manifest.get("source") or {}).get("sha256", "")).upper():
        blockers.append({"code": "SOURCE_HASH_MISMATCH"})
    if candidate_hash != str((rebuild.get("output") or {}).get("sha256", "")).upper():
        blockers.append({"code": "CANDIDATE_HASH_MISMATCH"})
    if manifest_hash != str((rebuild.get("manifest") or {}).get("sha256", "")).upper():
        blockers.append({"code": "MANIFEST_HASH_MISMATCH"})

    source_doc = fitz.open(source_path)
    candidate_doc = fitz.open(candidate_path)
    selected_pages = [int(page) for page in manifest.get("selected_pages") or []]
    page_map = {source_page: index for index, source_page in enumerate(selected_pages)}
    page_rects = {
        source_page: normalize_bbox(source_doc[source_page - 1].rect)
        for source_page in selected_pages
    }
    report_by_id = {
        str(item.get("segment_id", "")): item
        for item in rebuild.get("segments") or []
    }

    manifest_contract_issues: list[dict[str, Any]] = []
    sequence_segments: list[dict[str, Any]] = []
    legacy_segment_ids: list[str] = []
    for segment in manifest.get("segments") or []:
        contract = inline_contract(segment)
        if contract is None:
            continue
        manifest_contract_issues.extend(
            validate_inline_visual_sequence(
                segment, page_rects.get(int(segment.get("page", 0)))
            )
        )
        if versioned_inline_contract(segment) is None:
            legacy_segment_ids.append(str(segment.get("segment_id", "")))
            continue
        sequence_segments.append(segment)
    manifest_contract_issues.extend(validate_inline_visual_sweeps(manifest))
    blockers.extend(
        {"code": "INLINE_MANIFEST_BLOCKED", "evidence": item}
        for item in manifest_contract_issues
    )
    applicable = bool(sequence_segments or legacy_segment_ids)

    checks: list[dict[str, Any]] = []
    for segment in sequence_segments:
        segment_id = str(segment["segment_id"])
        source_page_number = int(segment["page"])
        contract = inline_contract(segment) or {}
        report_item = report_by_id.get(segment_id) or {}
        report_contract = report_item.get("post_rebuild_inline_visual_relocation") or {}
        issues: list[dict[str, Any]] = []
        legacy_evidence: dict[str, Any] | None = None
        if not report_item:
            issues.append({"code": "INLINE_REPORT_SEGMENT_MISSING"})
        if report_contract.get("policy") != contract.get("policy"):
            issues.append(
                {
                    "code": "INLINE_REPORT_POLICY_MISMATCH",
                    "expected": contract.get("policy"),
                    "actual": report_contract.get("policy"),
                }
            )
        if report_contract.get("display_text_with_visual_semantics") != (
            segment.get("render") or {}
        ).get("display_text_with_visual_semantics"):
            issues.append({"code": "INLINE_REPORT_DISPLAY_SEMANTICS_MISMATCH"})
        if report_contract.get("prior_live_text_removed_before_final_overlay") is not True:
            issues.append({"code": "INLINE_PRIOR_TEXT_REMOVAL_UNPROVEN"})

        direct_build = report_item.get("mask_mode") == "remove_text_only" and report_item.get("mask_fill") is None
        legacy_build = report_item.get("mask_mode") == LEGACY_TWO_STAGE_MASK_MODE
        if legacy_build:
            valid, legacy_evidence, legacy_problems = validate_legacy_two_stage_overlay_evidence(
                rebuild=rebuild,
                rebuild_path=rebuild_path,
                report_item=report_item,
                segment_id=segment_id,
                source_path=source_path,
                candidate_path=candidate_path,
                manifest_path=manifest_path,
            )
            if not valid:
                issues.append(
                    {
                        "code": "INLINE_LEGACY_OVERLAY_EVIDENCE_BLOCKED",
                        "problems": legacy_problems,
                    }
                )
        elif not direct_build:
            issues.append(
                {
                    "code": "INLINE_STAGE1_MASK_EVIDENCE",
                    "mask_mode": report_item.get("mask_mode"),
                    "mask_fill": report_item.get("mask_fill"),
                }
            )

        declared_relocations = contract.get("relocations") or []
        actual_relocations = report_contract.get("copied_visuals") or []
        if len(actual_relocations) != len(declared_relocations):
            issues.append(
                {
                    "code": "INLINE_COPIED_VISUAL_COUNT",
                    "expected": len(declared_relocations),
                    "actual": len(actual_relocations),
                }
            )
        identity_results: list[dict[str, Any]] = []
        for index, (declared, actual) in enumerate(
            zip(declared_relocations, actual_relocations, strict=False), start=1
        ):
            mismatches: list[str] = []
            if declared.get("semantic_label") != actual.get("semantic_label"):
                mismatches.append("semantic_label")
            if not same_bbox(
                declared.get("source_clip_bbox", ()),
                actual.get("source_clip_bbox", ()),
            ):
                mismatches.append("source_clip_bbox")
            if not same_bbox(
                declared.get("target_clip_bbox", ()),
                actual.get("target_clip_bbox", ()),
            ):
                mismatches.append("target_clip_bbox")
            if direct_build and (
                actual.get("copy_method") != COPY_METHOD
                or actual.get("internal_content_edited") is not False
                or actual.get("object_policy") != declared.get("object_policy")
            ):
                mismatches.append("complete_object_copy_contract")
            source_render = rendered_region(
                source_doc[source_page_number - 1],
                declared["source_clip_bbox"],
                args.dpi,
            )
            candidate_render = rendered_region(
                candidate_doc[page_map[source_page_number]],
                declared["target_clip_bbox"],
                args.dpi,
            )
            visual_identity = source_render == candidate_render
            identity_results.append(
                {
                    "index": index,
                    "semantic_label": declared.get("semantic_label"),
                    "source_clip_bbox": normalize_bbox(declared["source_clip_bbox"]),
                    "target_clip_bbox": normalize_bbox(declared["target_clip_bbox"]),
                    "source_render": source_render,
                    "candidate_render": candidate_render,
                    "rendered_visual_identity": (
                        "MATCH"
                        if visual_identity
                        else "RASTER_PHASE_DIFFERENCE_REQUIRES_VISUAL_REVIEW"
                    ),
                    "machine_identity_basis": (
                        "source_clip_copy_method_plus_equal_unscaled_geometry"
                    ),
                    "status": "PASS" if not mismatches else "BLOCKED",
                    "mismatches": mismatches,
                }
            )
            if mismatches:
                issues.append(
                    {
                        "code": "INLINE_VISUAL_COPY_MISMATCH",
                        "index": index,
                        "mismatches": mismatches,
                    }
                )

        connector_fragment = next(
            (
                fragment
                for fragment in (segment.get("render") or {}).get("fragments") or []
                if fragment.get("layout_role") == "between_choice_visuals"
            ),
            {},
        )
        connector_fragment_id = connector_fragment.get("fragment_id")
        rendered_connector = [
            line
            for line in report_item.get("rendered_lines") or []
            if line.get("fragment_id") == connector_fragment_id
            and line.get("text") == contract.get("connector")
        ]
        if len(rendered_connector) != 1:
            issues.append(
                {
                    "code": "INLINE_RENDERED_CONNECTOR_COUNT",
                    "expected": 1,
                    "actual": len(rendered_connector),
                }
            )
        check = {
            "segment_id": segment_id,
            "page": source_page_number,
            "homologous_set_id": contract.get("homologous_set_id"),
            "sequence": "prefix + complete visual A + 或 + complete visual B + suffix",
            "identity_dpi": args.dpi,
            "visual_identity": identity_results,
            "legacy_two_stage_evidence": legacy_evidence,
            "issues": issues,
            "status": "PASS" if not issues else "BLOCKED",
        }
        checks.append(check)
        blockers.extend(
            {"code": item["code"], "segment_id": segment_id, "evidence": item}
            for item in issues
        )

    source_doc.close()
    candidate_doc.close()
    result = {
        "schema": "pdf-tw-localize/inline-visual-sequence-qa/v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": {"path": str(source_path), "sha256": source_hash},
        "candidate": {"path": str(candidate_path), "sha256": candidate_hash},
        "manifest": {"path": str(manifest_path), "sha256": manifest_hash},
        "rebuild_report": {"path": str(rebuild_path), "sha256": sha256_file(rebuild_path)},
        "scope": {
            "selected_pages": selected_pages,
            "inline_sequence_count": len(sequence_segments),
            "legacy_unversioned_sequence_count": len(legacy_segment_ids),
            "legacy_unversioned_segment_ids": legacy_segment_ids,
            "identity_dpi": args.dpi,
        },
        "checks": checks,
        "blocking_issue_count": len(blockers),
        "blocking_issues": blockers,
        "machine_qa": (
            "NOT_APPLICABLE"
            if not applicable
            else ("MACHINE_QA_PASS" if not blockers else "BLOCKED")
        ),
        "visual_review": "NOT_CHECKED",
        "user_acceptance": "NOT_CHECKED",
        "status": (
            "NOT_APPLICABLE"
            if not applicable
            else ("PASS" if not blockers else "BLOCKED")
        ),
    }
    write_json(args.output, result)
    emit_json(result)
    return 0 if not blockers else 2


if __name__ == "__main__":
    raise SystemExit(main())
