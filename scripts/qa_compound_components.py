#!/usr/bin/env python3
"""Fail-closed QA for compound-component routing and protected vector members."""

from __future__ import annotations

import argparse
import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import fitz

from _compound_components import (
    COMPOUND_COMPONENT_SCHEMA,
    CompoundComponentError,
    candidate_drawing_match_count,
    candidate_member_bboxes,
    content_path_construction_signatures_equal,
    content_path_signatures_equal,
    evaluate_member_relations,
    evaluate_composited_visible_layouts,
    evaluate_repeated_component_layouts,
    evaluate_translation_dependent_geometry,
    expected_candidate_content_path_signatures,
    expected_candidate_drawing_signatures,
    parse_content_paths,
    text_span_match_count,
    validate_english_allowlist,
    verify_member_source_evidence,
)
from _console import emit_json
from _drawing_signature import drawing_records
from _segment_common import read_json, sha256_file, validate_manifest, write_json


def _blocking(code: str, message: str, evidence: Any = None) -> dict[str, Any]:
    item: dict[str, Any] = {
        "severity": "BLOCKING",
        "code": code,
        "message": message,
    }
    if evidence is not None:
        item["evidence"] = evidence
    return item


def _candidate_path_records(doc: fitz.Document, page: fitz.Page) -> list[dict[str, Any]]:
    return [
        record
        for xref in page.get_contents()
        for record in parse_content_paths(doc.xref_stream(int(xref)), stream_xref=int(xref))
    ]


def _signature(entry: dict[str, Any]) -> dict[str, Any]:
    value = entry.get("signature")
    if not isinstance(value, dict):
        raise CompoundComponentError(
            "CONTENT_PATH_SIGNATURE_MISSING",
            "A declared content path has no signature object",
            entry,
        )
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--rebuild-report", required=True, type=Path)
    parser.add_argument("--allowlist", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    source_path = args.source.resolve()
    candidate_path = args.candidate.resolve()
    manifest_path = args.manifest.resolve()
    rebuild_report_path = args.rebuild_report.resolve()
    allowlist_path = args.allowlist.resolve()
    output_path = args.output.resolve()
    manifest = read_json(manifest_path)
    rebuild_report = read_json(rebuild_report_path)
    allowlist = read_json(allowlist_path)

    blocking: list[dict[str, Any]] = [
        item
        for item in validate_manifest(
            manifest, require_translation=True, require_render=True
        )
        if item.get("severity") == "BLOCKING"
    ]
    blocking.extend(
        _blocking(item["code"], item["message"], item)
        for item in validate_english_allowlist(allowlist)
    )
    if Path(manifest.get("source", {}).get("path", "")).resolve() != source_path:
        blocking.append(_blocking("SOURCE_PATH_MISMATCH", "Manifest source path differs"))
    if manifest.get("source", {}).get("sha256") != sha256_file(source_path):
        blocking.append(_blocking("SOURCE_SHA256_MISMATCH", "Manifest source hash differs"))
    if rebuild_report.get("output", {}).get("sha256") != sha256_file(candidate_path):
        blocking.append(_blocking("CANDIDATE_SHA256_MISMATCH", "Rebuild report output hash differs"))

    segment_ids = {str(item.get("segment_id")) for item in manifest.get("segments") or []}
    selected_pages = [int(value) for value in manifest.get("selected_pages") or []]
    for index, entry in enumerate(
        [
            *(allowlist.get("allowed") or []),
            *(allowlist.get("allowed_ui_english") or []),
            *(allowlist.get("allowed_visual_english") or []),
        ]
    ):
        scope = entry.get("scope") or {}
        if not set(map(int, scope.get("pages") or [])).issubset(selected_pages):
            blocking.append(
                _blocking(
                    "ALLOWLIST_ENTRY_PAGE_SCOPE",
                    "An allowlist entry escapes selected pages",
                    {"entry_index": index, "scope": scope},
                )
            )
        if not set(map(str, scope.get("segment_ids") or [])).issubset(segment_ids):
            blocking.append(
                _blocking(
                    "ALLOWLIST_ENTRY_SEGMENT_SCOPE",
                    "An allowlist entry refers to an unknown segment",
                    {"entry_index": index, "scope": scope},
                )
            )

    group_contracts: dict[str, dict[str, Any]] = {}
    group_pages: dict[str, int] = {}
    for segment in manifest.get("segments") or []:
        contract = segment.get("component_contract") or {}
        if contract.get("schema") != COMPOUND_COMPONENT_SCHEMA:
            continue
        group_id = str(contract.get("group_id", ""))
        existing = group_contracts.setdefault(group_id, contract)
        if existing.get("members") != contract.get("members"):
            blocking.append(
                _blocking(
                    "COMPONENT_GROUP_MISMATCH",
                    "A group has conflicting component declarations",
                    group_id,
                )
            )
        group_pages[group_id] = int(segment["page"])

    adjusted_member_reports: dict[str, dict[str, Any]] = {}
    for report in [
        *(rebuild_report.get("background_adjustments") or []),
        *(rebuild_report.get("vector_rule_adjustments") or []),
    ]:
        component_id = str(report.get("component_id", ""))
        if not component_id or component_id in adjusted_member_reports:
            blocking.append(
                _blocking(
                    "ADJUSTED_MEMBER_REPORT_DUPLICATE",
                    "Every adjusted member requires one unique rebuild report",
                    component_id,
                )
            )
            continue
        adjusted_member_reports[component_id] = report
    adjusted_member_bboxes = {
        component_id: report.get("target_bbox")
        for component_id, report in adjusted_member_reports.items()
        if report.get("target_bbox") is not None
    }
    expected_adjusted_member_ids = {
        str(member.get("component_id", ""))
        for contract in group_contracts.values()
        for member in contract.get("members") or []
        if member.get("policy") in {"adjust_background", "adjust_vector_rule"}
    }
    if set(adjusted_member_reports) != expected_adjusted_member_ids:
        blocking.append(
            _blocking(
                "ADJUSTED_MEMBER_REPORT_SET_MISMATCH",
                "The rebuild report must contain every and only declared adjusted member",
                {
                    "expected": sorted(expected_adjusted_member_ids),
                    "reported": sorted(adjusted_member_reports),
                },
            )
        )

    source_doc = fitz.open(source_path)
    candidate_doc = fitz.open(candidate_path)
    page_map = {source_page: index for index, source_page in enumerate(selected_pages)}
    source_checks: list[dict[str, Any]] = []
    candidate_checks: list[dict[str, Any]] = []
    relation_checks: list[dict[str, Any]] = []
    dependency_checks: list[dict[str, Any]] = []
    candidate_bboxes_global: dict[str, Any] = {}
    member_pages: dict[str, int] = {}
    source_drawing_cache: dict[int, list[dict[str, Any]]] = {}
    candidate_drawing_cache: dict[int, list[dict[str, Any]]] = {}
    source_path_cache: dict[int, dict[int, list[dict[str, Any]]]] = {}
    candidate_path_cache: dict[int, list[dict[str, Any]]] = {}
    try:
        for group_id, contract in group_contracts.items():
            source_page_number = group_pages[group_id]
            if source_page_number not in page_map:
                blocking.append(
                    _blocking(
                        "COMPONENT_PAGE_NOT_SELECTED",
                        "A component group is outside selected pages",
                        group_id,
                    )
                )
                continue
            source_page = source_doc[source_page_number - 1]
            candidate_page = candidate_doc[page_map[source_page_number]]
            if source_page_number not in source_drawing_cache:
                source_drawing_cache[source_page_number] = drawing_records(source_page)
            if source_page_number not in candidate_drawing_cache:
                candidate_drawing_cache[source_page_number] = drawing_records(candidate_page)
            if source_page_number not in source_path_cache:
                source_path_cache[source_page_number] = {
                    int(xref): parse_content_paths(
                        source_doc.xref_stream(int(xref)), stream_xref=int(xref)
                    )
                    for xref in source_page.get_contents()
                }
            if source_page_number not in candidate_path_cache:
                candidate_path_cache[source_page_number] = _candidate_path_records(
                    candidate_doc, candidate_page
                )
            source_drawings = source_drawing_cache[source_page_number]
            candidate_drawings = candidate_drawing_cache[source_page_number]
            candidate_paths = candidate_path_cache[source_page_number]
            for member in contract.get("members") or []:
                component_id = str(member.get("component_id", ""))
                try:
                    candidate_member = copy.deepcopy(member)
                    adjustment_report = adjusted_member_reports.get(component_id)
                    if adjustment_report is not None:
                        candidate_member["target_bbox"] = adjustment_report["target_bbox"]
                        if adjustment_report.get("translation_delta_pt") is not None:
                            candidate_member["translation_delta_pt"] = adjustment_report[
                                "translation_delta_pt"
                            ]
                    source_checks.append(
                        verify_member_source_evidence(
                            source_doc,
                            source_page,
                            member,
                            drawing_records_cache=source_drawings,
                            content_path_records_cache=source_path_cache[source_page_number],
                        )
                    )
                    source_drawing_declarations = (
                        (member.get("source_evidence") or {})
                        .get("ordered_path_signatures", {})
                        .get("drawing_signatures", [])
                    )
                    source_drawing_counts = [
                        candidate_drawing_match_count(
                            candidate_page,
                            declaration,
                            records=candidate_drawings,
                        )
                        for declaration in source_drawing_declarations
                    ]
                    drawing_declarations = expected_candidate_drawing_signatures(candidate_member)
                    drawing_counts = [
                        candidate_drawing_match_count(
                            candidate_page,
                            declaration,
                            records=candidate_drawings,
                        )
                        for declaration in drawing_declarations
                    ]
                    entries = (
                        (member.get("source_evidence") or {})
                        .get("ordered_path_signatures", {})
                        .get("content_path_signatures", [])
                    )
                    path_counts = [
                        sum(
                            content_path_signatures_equal(record["signature"], _signature(entry))
                            for record in candidate_paths
                        )
                        for entry in entries
                    ]
                    construction_path_counts = [
                        sum(
                            content_path_construction_signatures_equal(
                                record["signature"], _signature(entry)
                            )
                            for record in candidate_paths
                        )
                        for entry in entries
                    ]
                    target_path_signatures = expected_candidate_content_path_signatures(
                        candidate_member
                    )
                    target_path_counts = [
                        sum(
                            content_path_signatures_equal(
                                record["signature"], signature
                            )
                            for record in candidate_paths
                        )
                        for signature in target_path_signatures
                    ]
                    target_construction_path_counts = [
                        sum(
                            content_path_construction_signatures_equal(
                                record["signature"], signature
                            )
                            for record in candidate_paths
                        )
                        for signature in target_path_signatures
                    ]
                    text_counts = [
                        text_span_match_count(candidate_page, declaration)
                        for declaration in (member.get("source_evidence") or {}).get(
                            "text_spans", []
                        )
                    ]
                    policy = str(member.get("policy", ""))
                    if policy == "replace_vector_outlined_text":
                        passed = all(
                            count == 0
                            for count in drawing_counts
                            + path_counts
                            + construction_path_counts
                        )
                        expected = "ABSENT_AFTER_EXACT_REPLACEMENT"
                    elif policy == "replace_live_text":
                        passed = all(count == 0 for count in text_counts)
                        expected = "SOURCE_LIVE_TEXT_ABSENT_AFTER_REPLACEMENT"
                    elif policy == "adjust_background":
                        passed = all(count == 1 for count in drawing_counts)
                        expected = "ADJUSTED_DRAWINGS_PRESENT_ONCE"
                    elif policy == "adjust_vector_rule":
                        passed = (
                            source_drawing_counts == [0]
                            and drawing_counts == [1]
                            and construction_path_counts == [0]
                            and target_construction_path_counts == [1]
                        )
                        expected = "SOURCE_RULE_ABSENT_TARGET_RULE_PRESENT_ONCE"
                    else:
                        passed = all(
                            count == 1
                            for count in drawing_counts + text_counts
                        )
                        expected = "PROTECTED_SIGNATURES_PRESENT_ONCE"
                    check = {
                        "group_id": group_id,
                        "component_id": component_id,
                        "role": member.get("role"),
                        "policy": policy,
                        "expected": expected,
                        "drawing_match_counts": drawing_counts,
                        "source_drawing_match_counts": source_drawing_counts,
                        "content_path_match_counts": path_counts,
                        "source_construction_path_match_counts": construction_path_counts,
                        "target_content_path_match_counts": target_path_counts,
                        "target_construction_path_match_counts": target_construction_path_counts,
                        "candidate_content_path_gate": (
                            (
                                "FINAL_CONSTRUCTION_SIGNATURE_WITH_EXACT_DRAWING_STYLE"
                                if policy == "adjust_vector_rule"
                                else "REMOVAL_RESIDUE_GATE"
                            )
                            if policy in {
                                "replace_vector_outlined_text",
                                "adjust_vector_rule",
                            }
                            else "DIAGNOSTIC_ONLY_AFTER_CANDIDATE_STREAM_RESERIALIZATION"
                        ),
                        "text_span_match_counts": text_counts,
                        "status": "PASS" if passed else "FAIL",
                    }
                    candidate_checks.append(check)
                    if not passed:
                        blocking.append(
                            _blocking(
                                "CANDIDATE_COMPONENT_SIGNATURE_MISMATCH",
                                "A removed, adjusted, or protected member failed its signature gate",
                                check,
                            )
                        )
                except (CompoundComponentError, KeyError, TypeError, ValueError) as exc:
                    evidence = exc.as_dict() if isinstance(exc, CompoundComponentError) else str(exc)
                    blocking.append(
                        _blocking(
                            "COMPONENT_EVIDENCE_VERIFICATION_FAILED",
                            f"Component evidence could not be verified: {component_id}",
                            evidence,
                        )
                    )

            try:
                candidate_bboxes = candidate_member_bboxes(
                    contract,
                    rebuild_report.get("segments") or [],
                    candidate_page=candidate_page,
                    adjusted_member_bboxes=adjusted_member_bboxes,
                )
            except (CompoundComponentError, KeyError, TypeError, ValueError) as exc:
                evidence = exc.as_dict() if isinstance(exc, CompoundComponentError) else str(exc)
                blocking.append(
                    _blocking(
                        "CANDIDATE_COMPONENT_BBOX_EVIDENCE_FAILED",
                        "Actual candidate member geometry could not be bound fail-closed",
                        {"group_id": group_id, "detail": evidence},
                    )
                )
                continue
            results, relation_issues = evaluate_member_relations(contract, candidate_bboxes)
            relation_checks.extend(results)
            blocking.extend(
                _blocking(item["code"], "A declared component relation failed", item)
                for item in relation_issues
            )
            dependency_results, dependency_issues = evaluate_translation_dependent_geometry(
                contract,
                candidate_bboxes,
                page_rect=candidate_page.rect,
            )
            dependency_checks.extend(dependency_results)
            blocking.extend(
                _blocking(
                    item["code"],
                    "A translated-text dependent member failed its resolved geometry contract",
                    item,
                )
                for item in dependency_issues
            )
            for component_id, bbox in candidate_bboxes.items():
                prior = candidate_bboxes_global.get(component_id)
                if prior is not None and list(prior) != list(bbox):
                    blocking.append(
                        _blocking(
                            "CANDIDATE_COMPONENT_BBOX_CONFLICT",
                            "A component id resolved to conflicting candidate geometry",
                            {"component_id": component_id, "first": prior, "second": bbox},
                        )
                    )
                candidate_bboxes_global[component_id] = bbox
                prior_page = member_pages.get(component_id)
                if prior_page is not None and prior_page != source_page_number:
                    blocking.append(
                        _blocking(
                            "CANDIDATE_COMPONENT_PAGE_CONFLICT",
                            "A component id was declared on multiple source pages",
                            {"component_id": component_id, "pages": [prior_page, source_page_number]},
                        )
                    )
                member_pages[component_id] = source_page_number
    finally:
        source_doc.close()
        candidate_doc.close()

    repeated_layout_contracts: list[dict[str, Any]] = []
    for context in manifest.get("page_contexts") or []:
        page_number = int(context.get("page", 0))
        declarations = context.get("repeated_component_layouts")
        if declarations is None:
            continue
        if not isinstance(declarations, list):
            blocking.append(
                _blocking(
                    "REPEATED_LAYOUT_CONTRACT_FIELDS",
                    "page_context repeated_component_layouts must be a list",
                    {"page": page_number},
                )
            )
            continue
        for declaration in declarations:
            repeated_layout_contracts.append(declaration)
            if not isinstance(declaration, dict):
                continue
            for instance in declaration.get("instances") or []:
                if not isinstance(instance, dict):
                    continue
                component_ids = [
                    str(instance.get("anchor_member_id", "")),
                    *[
                        str(value)
                        for value in (instance.get("member_ids") or {}).values()
                    ],
                ]
                for component_id in component_ids:
                    declared_page = member_pages.get(component_id)
                    if declared_page is not None and declared_page != page_number:
                        blocking.append(
                            _blocking(
                                "REPEATED_LAYOUT_PAGE_SCOPE",
                                "A repeated-layout instance member escapes its page context",
                                {
                                    "page": page_number,
                                    "component_id": component_id,
                                    "declared_page": declared_page,
                                },
                            )
                        )
    repeated_layout_checks, repeated_layout_issues = evaluate_repeated_component_layouts(
        repeated_layout_contracts, candidate_bboxes_global
    )
    blocking.extend(
        _blocking(item["code"], "A repeated component layout contract failed", item)
        for item in repeated_layout_issues
    )

    visible_layout_contracts: list[dict[str, Any]] = []
    for context in manifest.get("page_contexts") or []:
        page_number = int(context.get("page", 0))
        declarations = context.get("composited_visible_layouts")
        if declarations is None:
            continue
        if not isinstance(declarations, list):
            blocking.append(
                _blocking(
                    "VISIBLE_LAYOUT_CONTRACT_FIELDS",
                    "page_context composited_visible_layouts must be a list",
                    {"page": page_number},
                )
            )
            continue
        for declaration in declarations:
            visible_layout_contracts.append(declaration)
            if not isinstance(declaration, dict):
                continue
            for instance in declaration.get("instances") or []:
                if not isinstance(instance, dict):
                    continue
                component_ids = [
                    str(instance.get("anchor_member_id", "")),
                    str(instance.get("subject_member_id", "")),
                    *[
                        str(value)
                        for value in instance.get("opaque_occluder_member_ids") or []
                    ],
                ]
                for component_id in component_ids:
                    declared_page = member_pages.get(component_id)
                    if declared_page is not None and declared_page != page_number:
                        blocking.append(
                            _blocking(
                                "VISIBLE_LAYOUT_PAGE_SCOPE",
                                "A composited-visible instance member escapes its page context",
                                {
                                    "page": page_number,
                                    "component_id": component_id,
                                    "declared_page": declared_page,
                                },
                            )
                        )
    visible_layout_checks, visible_layout_issues = evaluate_composited_visible_layouts(
        visible_layout_contracts, candidate_bboxes_global
    )
    blocking.extend(
        _blocking(item["code"], "A composited-visible layout contract failed", item)
        for item in visible_layout_issues
    )

    replacement_report = {
        str(item.get("component_id")): item
        for item in rebuild_report.get("vector_path_replacements") or []
    }
    expected_replacements = {
        str(member.get("component_id"))
        for contract in group_contracts.values()
        for member in contract.get("members") or []
        if member.get("policy") == "replace_vector_outlined_text"
    }
    if set(replacement_report) != expected_replacements or any(
        item.get("status") != "APPLIED_VERIFIED"
        or int(item.get("residue_count", -1)) != 0
        or int(item.get("selected_path_count", 0)) <= 0
        for item in replacement_report.values()
    ):
        blocking.append(
            _blocking(
                "VECTOR_REPLACEMENT_REPORT_MISMATCH",
                "The rebuild report does not prove every declared vector replacement",
                {
                    "expected": sorted(expected_replacements),
                    "reported": replacement_report,
                },
            )
        )

    rule_adjustment_report = {
        str(item.get("component_id")): item
        for item in rebuild_report.get("vector_rule_adjustments") or []
    }
    expected_rule_adjustments = {
        str(member.get("component_id"))
        for contract in group_contracts.values()
        for member in contract.get("members") or []
        if member.get("policy") == "adjust_vector_rule"
    }
    if set(rule_adjustment_report) != expected_rule_adjustments or any(
        item.get("status") != "APPLIED_VERIFIED"
        or int(item.get("source_path_count_after", -1)) != 0
        or int(item.get("target_path_count_after", 0)) != 1
        or int(item.get("source_drawing_count_after", -1)) != 0
        or int(item.get("target_drawing_count_after", 0)) != 1
        for item in rule_adjustment_report.values()
    ):
        blocking.append(
            _blocking(
                "VECTOR_RULE_ADJUSTMENT_REPORT_MISMATCH",
                "The rebuild report does not prove every declared rule-path adjustment",
                {
                    "expected": sorted(expected_rule_adjustments),
                    "reported": rule_adjustment_report,
                },
            )
        )

    payload = {
        "schema": "pdf-tw-localize/compound-component-qa/v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "inputs": {
            "source": {"path": str(source_path), "sha256": sha256_file(source_path)},
            "candidate": {"path": str(candidate_path), "sha256": sha256_file(candidate_path)},
            "manifest": {"path": str(manifest_path), "sha256": sha256_file(manifest_path)},
            "rebuild_report": {
                "path": str(rebuild_report_path),
                "sha256": sha256_file(rebuild_report_path),
            },
            "allowlist": {"path": str(allowlist_path), "sha256": sha256_file(allowlist_path)},
        },
        "compound_group_count": len(group_contracts),
        "source_member_checks": source_checks,
        "candidate_member_checks": candidate_checks,
        "relation_checks": relation_checks,
        "translation_dependency_checks": dependency_checks,
        "repeated_component_layout_checks": repeated_layout_checks,
        "composited_visible_layout_checks": visible_layout_checks,
        "vector_path_replacements": list(replacement_report.values()),
        "vector_rule_adjustments": list(rule_adjustment_report.values()),
        "blocking_issues": blocking,
        "machine_qa": "PASS" if not blocking else "BLOCKED_FAIL_CLOSED",
        "human_visual_observation": "NOT_CHECKED",
        "user_acceptance": "NOT_CHECKED",
        "status": "PASS" if not blocking else "BLOCKED",
    }
    write_json(output_path, payload)
    emit_json(payload)
    return 0 if not blocking else 2


if __name__ == "__main__":
    raise SystemExit(main())
