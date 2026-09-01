#!/usr/bin/env python3
"""Contracts for grammar-aware relocation of complete inline visual objects."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping


INLINE_RELOCATION_SCHEMA = "pdf-tw-localize/inline-visual-relocation/v1"
INLINE_SWEEP_SCHEMA = "pdf-tw-localize/inline-visual-sweep/v1"
INLINE_POLICY = "natural_inline_choice_sequence"
OBJECT_POLICY = "relocate_complete_visual_object_without_internal_edit"
POSITION_POLICY = "relocation_allowed_for_target_grammar"
INTERNAL_CONTENT_POLICY = "preserve_source_visual_content_exact"
COPY_METHOD = "show_pdf_page_source_clip"
OPAQUE_COVER_BASIS = "explicit_inspected_source_background"
LEGACY_TWO_STAGE_MASK_MODE = "stage1_remove_text_then_final_opaque_visual_occlusion"
GEOMETRY_TOLERANCE_PT = 0.02


def _issue(
    code: str,
    message: str,
    *,
    severity: str = "BLOCKING",
    segment_id: str | None = None,
    evidence: Any | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "severity": severity,
        "code": code,
        "message": message,
    }
    if segment_id:
        result["segment_id"] = segment_id
    if evidence is not None:
        result["evidence"] = evidence
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _bbox(values: Iterable[float]) -> list[float]:
    result = [round(float(value), 3) for value in values]
    if len(result) != 4 or not all(math.isfinite(value) for value in result):
        raise ValueError(f"Expected four finite bbox values, got {result}")
    if result[2] <= result[0] or result[3] <= result[1]:
        raise ValueError(f"Bbox is empty or inverted: {result}")
    return result


def _inside(inner: Iterable[float], outer: Iterable[float], tolerance: float = 0.1) -> bool:
    ix0, iy0, ix1, iy1 = _bbox(inner)
    ox0, oy0, ox1, oy1 = _bbox(outer)
    return (
        ix0 >= ox0 - tolerance
        and iy0 >= oy0 - tolerance
        and ix1 <= ox1 + tolerance
        and iy1 <= oy1 + tolerance
    )


def _same_size(first: Iterable[float], second: Iterable[float]) -> bool:
    first_box = _bbox(first)
    second_box = _bbox(second)
    return (
        abs((first_box[2] - first_box[0]) - (second_box[2] - second_box[0]))
        <= GEOMETRY_TOLERANCE_PT
        and abs((first_box[3] - first_box[1]) - (second_box[3] - second_box[1]))
        <= GEOMETRY_TOLERANCE_PT
    )


def _same_bbox(first: Iterable[float], second: Iterable[float]) -> bool:
    return all(
        abs(left - right) <= GEOMETRY_TOLERANCE_PT
        for left, right in zip(_bbox(first), _bbox(second), strict=True)
    )


def _vertical_overlap(first: Iterable[float], second: Iterable[float]) -> float:
    a = _bbox(first)
    b = _bbox(second)
    return max(0.0, min(a[3], b[3]) - max(a[1], b[1]))


def inline_contract(segment: Mapping[str, Any]) -> Mapping[str, Any] | None:
    value = (segment.get("render") or {}).get("inline_visual_relocation")
    return value if isinstance(value, Mapping) else None


def versioned_inline_contract(segment: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Return only the executable v1 contract, never a historical shape."""

    contract = inline_contract(segment)
    if contract is None or contract.get("schema") != INLINE_RELOCATION_SCHEMA:
        return None
    return contract


def validate_inline_visual_sequence(
    segment: Mapping[str, Any], page_rect: Iterable[float] | None
) -> list[dict[str, Any]]:
    """Validate one declarative Chinese inline choice sequence.

    The contract distinguishes immutable internal visual content from movable
    object placement. It deliberately supports vertical relocation when a
    source icon row must join the translated Chinese sentence.
    """

    render = segment.get("render") or {}
    raw_contract = render.get("inline_visual_relocation")
    if raw_contract is None:
        return []
    segment_id = str(segment.get("segment_id", ""))
    if not isinstance(raw_contract, Mapping):
        return [
            _issue(
                "INLINE_VISUAL_CONTRACT_TYPE",
                "render.inline_visual_relocation must be an object",
                segment_id=segment_id,
            )
        ]
    contract = raw_contract
    if contract.get("schema") is None:
        return [
            _issue(
                "INLINE_VISUAL_LEGACY_UNVERSIONED",
                "Unversioned inline relocation remains readable for historical QA only; author new work with inline-visual-relocation/v1",
                severity="NEEDS_REVIEW",
                segment_id=segment_id,
            )
        ]
    issues: list[dict[str, Any]] = []
    required_values = {
        "schema": INLINE_RELOCATION_SCHEMA,
        "policy": INLINE_POLICY,
        "object_position_policy": POSITION_POLICY,
        "internal_content_policy": INTERNAL_CONTENT_POLICY,
        "cover_fill_basis": OPAQUE_COVER_BASIS,
    }
    for key, expected in required_values.items():
        actual = contract.get(key)
        if actual != expected:
            issues.append(
                _issue(
                    "INLINE_VISUAL_CONTRACT_FIELD",
                    f"{key} must be {expected!r}",
                    segment_id=segment_id,
                    evidence={"field": key, "actual": actual, "expected": expected},
                )
            )

    homologous_set_id = str(contract.get("homologous_set_id", "")).strip()
    if not homologous_set_id:
        issues.append(
            _issue(
                "INLINE_VISUAL_HOMOLOGOUS_SET",
                "homologous_set_id is required for a document-scope sweep",
                segment_id=segment_id,
            )
        )
    connector = str(contract.get("connector", "")).strip()
    if connector != "或":
        issues.append(
            _issue(
                "INLINE_VISUAL_CONNECTOR",
                "A zh-TW two-choice sequence must declare connector 或",
                segment_id=segment_id,
                evidence={"connector": connector},
            )
        )

    try:
        maximum_gap = float(contract.get("maximum_gap_pt"))
        if not math.isfinite(maximum_gap) or maximum_gap < 0.0 or maximum_gap > 6.0:
            raise ValueError
    except (TypeError, ValueError):
        maximum_gap = 0.0
        issues.append(
            _issue(
                "INLINE_VISUAL_MAXIMUM_GAP",
                "maximum_gap_pt must be finite and between 0 and 6 pt",
                segment_id=segment_id,
                evidence={"maximum_gap_pt": contract.get("maximum_gap_pt")},
            )
        )

    cover_values = contract.get("cover_bboxes")
    if not isinstance(cover_values, list) or not cover_values:
        issues.append(
            _issue(
                "INLINE_VISUAL_COVERS",
                "cover_bboxes must be a non-empty list",
                segment_id=segment_id,
            )
        )
        cover_values = []
    for index, values in enumerate(cover_values):
        try:
            cover = _bbox(values)
            if page_rect is not None and not _inside(cover, page_rect):
                raise ValueError("outside page")
        except (TypeError, ValueError) as exc:
            issues.append(
                _issue(
                    "INLINE_VISUAL_COVER_BBOX",
                    f"Invalid cover bbox: {exc}",
                    segment_id=segment_id,
                    evidence={"index": index, "bbox": values},
                )
            )
    fill = contract.get("cover_fill")
    if (
        not isinstance(fill, list)
        or len(fill) != 3
        or any(
            not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) < 0.0
            or float(value) > 1.0
            for value in fill
        )
    ):
        issues.append(
            _issue(
                "INLINE_VISUAL_COVER_FILL",
                "cover_fill must contain three finite 0..1 color channels",
                segment_id=segment_id,
                evidence={"cover_fill": fill},
            )
        )

    relocations = contract.get("relocations")
    if not isinstance(relocations, list) or len(relocations) != 2:
        issues.append(
            _issue(
                "INLINE_VISUAL_RELOCATION_COUNT",
                "natural_inline_choice_sequence requires exactly two complete visuals",
                segment_id=segment_id,
                evidence={"count": len(relocations) if isinstance(relocations, list) else None},
            )
        )
        relocations = []
    labels: list[str] = []
    target_visual_boxes: list[list[float]] = []
    for index, relocation in enumerate(relocations):
        if not isinstance(relocation, Mapping):
            issues.append(
                _issue(
                    "INLINE_VISUAL_RELOCATION_TYPE",
                    "Each relocation must be an object",
                    segment_id=segment_id,
                    evidence={"index": index},
                )
            )
            continue
        label = str(relocation.get("semantic_label", "")).strip()
        labels.append(label)
        if not label:
            issues.append(
                _issue(
                    "INLINE_VISUAL_SEMANTIC_LABEL",
                    "Every visual requires a semantic_label",
                    segment_id=segment_id,
                    evidence={"index": index},
                )
            )
        if relocation.get("object_policy") != OBJECT_POLICY:
            issues.append(
                _issue(
                    "INLINE_VISUAL_OBJECT_POLICY",
                    "The complete visual object must be relocated without internal editing",
                    segment_id=segment_id,
                    evidence={"index": index, "actual": relocation.get("object_policy")},
                )
            )
        if relocation.get("copy_method") != COPY_METHOD:
            issues.append(
                _issue(
                    "INLINE_VISUAL_COPY_METHOD",
                    "copy_method must copy the declared source clip through show_pdf_page",
                    segment_id=segment_id,
                    evidence={"index": index, "actual": relocation.get("copy_method")},
                )
            )
        try:
            source_box = _bbox(relocation["source_clip_bbox"])
            target_box = _bbox(relocation["target_clip_bbox"])
            target_visual_boxes.append(target_box)
            if page_rect is not None and (
                not _inside(source_box, page_rect) or not _inside(target_box, page_rect)
            ):
                raise ValueError("source or target clip is outside the page")
            if not _same_size(source_box, target_box):
                issues.append(
                    _issue(
                        "INLINE_VISUAL_INTERNAL_SCALING",
                        "A relocated visual must retain its exact source width and height",
                        segment_id=segment_id,
                        evidence={"index": index, "source": source_box, "target": target_box},
                    )
                )
            expected_dx = round(target_box[0] - source_box[0], 3)
            expected_dy = round(target_box[1] - source_box[1], 3)
            actual_dx = float(relocation.get("horizontal_shift_pt"))
            actual_dy = float(relocation.get("vertical_shift_pt"))
            if (
                not math.isfinite(actual_dx)
                or not math.isfinite(actual_dy)
                or abs(actual_dx - expected_dx) > GEOMETRY_TOLERANCE_PT
                or abs(actual_dy - expected_dy) > GEOMETRY_TOLERANCE_PT
            ):
                issues.append(
                    _issue(
                        "INLINE_VISUAL_SHIFT_MISMATCH",
                        "Declared shifts must equal the source-to-target clip translation",
                        segment_id=segment_id,
                        evidence={
                            "index": index,
                            "declared": [actual_dx, actual_dy],
                            "expected": [expected_dx, expected_dy],
                        },
                    )
                )
        except (KeyError, TypeError, ValueError) as exc:
            issues.append(
                _issue(
                    "INLINE_VISUAL_RELOCATION_GEOMETRY",
                    f"Invalid relocation geometry: {exc}",
                    segment_id=segment_id,
                    evidence={"index": index, "relocation": dict(relocation)},
                )
            )

    if len(labels) == 2 and len(set(labels)) != 2:
        issues.append(
            _issue(
                "INLINE_VISUAL_LABEL_DUPLICATE",
                "The two visual choices require distinct semantic labels",
                segment_id=segment_id,
                evidence={"labels": labels},
            )
        )

    fragments = render.get("fragments")
    role_map: dict[str, list[Mapping[str, Any]]] = {}
    if isinstance(fragments, list):
        for fragment in fragments:
            if isinstance(fragment, Mapping):
                role_map.setdefault(str(fragment.get("layout_role", "")), []).append(fragment)
    required_roles = {
        "left_of_first_choice_visual": "prefix",
        "between_choice_visuals": "connector",
        "right_of_second_choice_visual_first_line": "suffix",
    }
    selected_fragments: dict[str, Mapping[str, Any]] = {}
    for role, name in required_roles.items():
        matches = role_map.get(role, [])
        if len(matches) != 1:
            issues.append(
                _issue(
                    "INLINE_VISUAL_FRAGMENT_ROLE",
                    f"Expected exactly one {name} fragment with layout_role={role}",
                    segment_id=segment_id,
                    evidence={"role": role, "count": len(matches)},
                )
            )
        else:
            selected_fragments[name] = matches[0]
    if selected_fragments.get("connector", {}).get("zh_TW") != connector:
        issues.append(
            _issue(
                "INLINE_VISUAL_RENDERED_CONNECTOR",
                "The between-visual fragment must render the declared connector",
                segment_id=segment_id,
            )
        )

    if len(target_visual_boxes) == 2 and len(selected_fragments) == 3:
        try:
            prefix_box = _bbox(selected_fragments["prefix"]["target_bbox"])
            connector_box = _bbox(selected_fragments["connector"]["target_bbox"])
            suffix_box = _bbox(selected_fragments["suffix"]["target_bbox"])
            first_box, second_box = target_visual_boxes
            ordered = [
                ("prefix_to_first_visual", prefix_box[2], first_box[0]),
                ("first_visual_to_connector", first_box[2], connector_box[0]),
                ("connector_to_second_visual", connector_box[2], second_box[0]),
                ("second_visual_to_suffix", second_box[2], suffix_box[0]),
            ]
            for name, before, after in ordered:
                gap = after - before
                if gap < -GEOMETRY_TOLERANCE_PT or gap > maximum_gap + GEOMETRY_TOLERANCE_PT:
                    issues.append(
                        _issue(
                            "INLINE_VISUAL_UNNATURAL_GAP",
                            f"{name} gap is outside the declared natural range",
                            segment_id=segment_id,
                            evidence={"gap_name": name, "gap_pt": round(gap, 3), "maximum_gap_pt": maximum_gap},
                        )
                    )
            for name, text_box in (
                ("prefix", prefix_box),
                ("connector", connector_box),
                ("suffix", suffix_box),
            ):
                if not any(_vertical_overlap(text_box, visual_box) > 0.0 for visual_box in target_visual_boxes):
                    issues.append(
                        _issue(
                            "INLINE_VISUAL_VERTICAL_ALIGNMENT",
                            f"{name} fragment does not share the inline band with either visual",
                            segment_id=segment_id,
                        )
                    )
        except (KeyError, TypeError, ValueError) as exc:
            issues.append(
                _issue(
                    "INLINE_VISUAL_FRAGMENT_GEOMETRY",
                    f"Invalid inline fragment geometry: {exc}",
                    segment_id=segment_id,
                )
            )

    display = str(render.get("display_text_with_visual_semantics", ""))
    if len(labels) == 2 and all(labels):
        required_display = f"〔{labels[0]}圖示〕{connector}〔{labels[1]}圖示〕"
        if required_display not in display:
            issues.append(
                _issue(
                    "INLINE_VISUAL_DISPLAY_SEMANTICS",
                    "display_text_with_visual_semantics must encode visual A + connector + visual B",
                    segment_id=segment_id,
                    evidence={"required_substring": required_display, "actual": display},
                )
            )
    if render.get("mask_mode") != "remove_text_only" or float(
        render.get("mask_padding_pt", 0.0)
    ) != 0.0:
        issues.append(
            _issue(
                "INLINE_VISUAL_STAGE1_MASK",
                "Stage 1 must remove live text only with zero padding; final opaque covers are a separately reported operation",
                segment_id=segment_id,
                evidence={
                    "mask_mode": render.get("mask_mode"),
                    "mask_padding_pt": render.get("mask_padding_pt"),
                },
            )
        )
    return issues


def validate_inline_visual_sweeps(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    segments = manifest.get("segments") or []
    sequence_segments = {
        str(segment.get("segment_id", "")): segment
        for segment in segments
        if inline_contract(segment) is not None
    }
    sweeps = manifest.get("inline_visual_sweeps")
    if not sequence_segments and not sweeps:
        return []
    issues: list[dict[str, Any]] = []
    legacy_ids = sorted(
        segment_id
        for segment_id, segment in sequence_segments.items()
        if (inline_contract(segment) or {}).get("schema") is None
    )
    versioned_ids = sorted(set(sequence_segments) - set(legacy_ids))
    if legacy_ids and not versioned_ids and not sweeps:
        return [
            _issue(
                "INLINE_VISUAL_SWEEP_LEGACY_UNDECLARED",
                "Historical unversioned inline relocations do not contain a homologous sweep; new work must declare one",
                severity="NEEDS_REVIEW",
                evidence={"segment_ids": legacy_ids},
            )
        ]
    if legacy_ids and versioned_ids:
        issues.append(
            _issue(
                "INLINE_VISUAL_SCHEMA_MIXED",
                "Versioned and unversioned inline visual contracts cannot be mixed in one manifest",
                evidence={"legacy": legacy_ids, "versioned": versioned_ids},
            )
        )
    if not isinstance(sweeps, list) or not sweeps:
        return issues + [
            _issue(
                "INLINE_VISUAL_SWEEP_MISSING",
                "Every inline visual sequence requires a declared-scope homologous-instance sweep",
            )
        ]

    selected_pages = sorted(int(page) for page in manifest.get("selected_pages") or [])
    source_page_count = int((manifest.get("source") or {}).get("page_count", 0))
    full_document = selected_pages == list(range(1, source_page_count + 1))
    expected_status = "DOCUMENT_WIDE_COMPLETE" if full_document else "DECLARED_SCOPE_COMPLETE"
    expected_basis = (
        "document_wide_source_visual_scan"
        if full_document
        else "declared_scope_source_visual_scan"
    )
    sweep_ids: list[str] = []
    covered_segments: list[str] = []
    for index, sweep in enumerate(sweeps):
        if not isinstance(sweep, Mapping):
            issues.append(
                _issue(
                    "INLINE_VISUAL_SWEEP_TYPE",
                    "Each inline_visual_sweeps entry must be an object",
                    evidence={"index": index},
                )
            )
            continue
        sweep_id = str(sweep.get("sweep_id", "")).strip()
        sweep_ids.append(sweep_id)
        required = {
            "schema": INLINE_SWEEP_SCHEMA,
            "pattern": "choice_between_two_complete_visuals",
            "connector": "或",
            "discovery_status": expected_status,
            "detection_basis": expected_basis,
        }
        for key, expected in required.items():
            if sweep.get(key) != expected:
                issues.append(
                    _issue(
                        "INLINE_VISUAL_SWEEP_FIELD",
                        f"{key} must be {expected!r} for this selected-page scope",
                        evidence={"sweep_id": sweep_id, "field": key, "actual": sweep.get(key)},
                    )
                )
        if not sweep_id:
            issues.append(_issue("INLINE_VISUAL_SWEEP_ID", "sweep_id is required"))
        try:
            scope_pages = sorted(int(page) for page in sweep.get("scope_pages") or [])
        except (TypeError, ValueError):
            scope_pages = []
        if scope_pages != selected_pages:
            issues.append(
                _issue(
                    "INLINE_VISUAL_SWEEP_SCOPE",
                    "scope_pages must equal the manifest selected_pages",
                    evidence={"sweep_id": sweep_id, "scope_pages": scope_pages, "selected_pages": selected_pages},
                )
            )
        expected_ids = [str(value) for value in sweep.get("expected_segment_ids") or []]
        if not expected_ids or len(expected_ids) != len(set(expected_ids)):
            issues.append(
                _issue(
                    "INLINE_VISUAL_SWEEP_SEGMENTS",
                    "expected_segment_ids must be non-empty and unique",
                    evidence={"sweep_id": sweep_id, "expected_segment_ids": expected_ids},
                )
            )
        try:
            expected_count = int(sweep.get("expected_instance_count"))
        except (TypeError, ValueError):
            expected_count = -1
        if expected_count != len(expected_ids):
            issues.append(
                _issue(
                    "INLINE_VISUAL_SWEEP_COUNT",
                    "expected_instance_count must equal expected_segment_ids length",
                    evidence={"sweep_id": sweep_id, "expected_instance_count": expected_count, "actual": len(expected_ids)},
                )
            )
        actual_ids = sorted(
            segment_id
            for segment_id, segment in sequence_segments.items()
            if str((inline_contract(segment) or {}).get("homologous_set_id", "")) == sweep_id
        )
        if sorted(expected_ids) != actual_ids:
            issues.append(
                _issue(
                    "INLINE_VISUAL_SWEEP_COVERAGE",
                    "The homologous sweep does not exactly cover its declared inline sequence segments",
                    evidence={"sweep_id": sweep_id, "expected": sorted(expected_ids), "actual": actual_ids},
                )
            )
        unknown = sorted(set(expected_ids) - set(sequence_segments))
        if unknown:
            issues.append(
                _issue(
                    "INLINE_VISUAL_SWEEP_UNKNOWN_SEGMENT",
                    "The sweep references segments without inline visual contracts",
                    evidence={"sweep_id": sweep_id, "segment_ids": unknown},
                )
            )
        covered_segments.extend(expected_ids)

    duplicates = sorted(
        segment_id
        for segment_id, count in Counter(covered_segments).items()
        if count > 1
    )
    missing = sorted(set(sequence_segments) - set(covered_segments))
    if duplicates or missing:
        issues.append(
            _issue(
                "INLINE_VISUAL_SWEEP_GLOBAL_COVERAGE",
                "Every inline visual sequence must occur in exactly one sweep",
                evidence={"missing": missing, "duplicates": duplicates},
            )
        )
    duplicate_sweeps = sorted(
        sweep_id for sweep_id, count in Counter(sweep_ids).items() if sweep_id and count > 1
    )
    if duplicate_sweeps:
        issues.append(
            _issue(
                "INLINE_VISUAL_SWEEP_ID_DUPLICATE",
                "sweep_id values must be unique",
                evidence={"sweep_ids": duplicate_sweeps},
            )
        )
    return issues


def validate_inline_superseded_segments(
    manifest: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Validate live-text fragments intentionally absorbed by an inline sequence."""

    raw_mapping = manifest.get("post_rebuild_superseded_segments")
    if raw_mapping is None:
        return []
    if not isinstance(raw_mapping, Mapping):
        return [
            _issue(
                "INLINE_SUPERSEDED_MAPPING_TYPE",
                "post_rebuild_superseded_segments must be an object mapping old segment IDs to inline-sequence owners",
            )
        ]
    segments = {
        str(segment.get("segment_id", "")): segment
        for segment in manifest.get("segments") or []
    }
    issues: list[dict[str, Any]] = []
    for raw_segment_id, raw_owner_id in raw_mapping.items():
        segment_id = str(raw_segment_id)
        owner_id = str(raw_owner_id)
        segment = segments.get(segment_id)
        owner = segments.get(owner_id)
        if not segment or not owner:
            issues.append(
                _issue(
                    "INLINE_SUPERSEDED_UNKNOWN_SEGMENT",
                    "A superseded segment or its owner does not exist",
                    evidence={"segment_id": segment_id, "owner_id": owner_id},
                )
            )
            continue
        if segment_id == owner_id or inline_contract(owner) is None:
            issues.append(
                _issue(
                    "INLINE_SUPERSEDED_OWNER",
                    "A superseded segment must name a different owner with an inline visual sequence contract",
                    evidence={"segment_id": segment_id, "owner_id": owner_id},
                )
            )
            continue
        if int(segment.get("page", 0)) != int(owner.get("page", -1)):
            issues.append(
                _issue(
                    "INLINE_SUPERSEDED_PAGE",
                    "A superseded segment and its inline-sequence owner must share one source page",
                    evidence={"segment_id": segment_id, "owner_id": owner_id},
                )
            )
            continue
        try:
            segment_bbox = _bbox(segment["bbox"])
            covers = [
                _bbox(values)
                for values in (inline_contract(owner) or {}).get("cover_bboxes") or []
            ]
            if not any(_inside(segment_bbox, cover, tolerance=0.2) for cover in covers):
                issues.append(
                    _issue(
                        "INLINE_SUPERSEDED_NOT_COVERED",
                        "The owner's declared cover does not fully remove the superseded source fragment",
                        evidence={
                            "segment_id": segment_id,
                            "owner_id": owner_id,
                            "segment_bbox": segment_bbox,
                            "cover_bboxes": covers,
                        },
                    )
                )
        except (KeyError, TypeError, ValueError) as exc:
            issues.append(
                _issue(
                    "INLINE_SUPERSEDED_GEOMETRY",
                    f"Invalid superseded-segment geometry: {exc}",
                    evidence={"segment_id": segment_id, "owner_id": owner_id},
                )
            )
    return issues


def validate_legacy_two_stage_overlay_evidence(
    *,
    rebuild: Mapping[str, Any],
    rebuild_path: Path,
    report_item: Mapping[str, Any],
    segment_id: str,
    source_path: Path,
    candidate_path: Path,
    manifest_path: Path,
) -> tuple[bool, dict[str, Any], list[dict[str, Any]]]:
    """Validate a legacy post-rebuild two-stage overlay without normalizing it.

    This accepts the truthful legacy report mode only when the external overlay
    evidence is hash-bound to the exact source, manifest, candidate, and segment,
    and proves zero residual live text plus complete source-drawing preservation.
    """

    problems: list[dict[str, Any]] = []
    if report_item.get("mask_mode") != LEGACY_TWO_STAGE_MASK_MODE:
        problems.append(
            {"code": "INLINE_OVERLAY_MASK_MODE", "actual": report_item.get("mask_mode")}
        )
    fill = report_item.get("mask_fill")
    if fill != [1.0, 1.0, 1.0]:
        problems.append({"code": "INLINE_OVERLAY_MASK_FILL", "actual": fill})
    relocation = report_item.get("post_rebuild_inline_visual_relocation") or {}
    if relocation.get("prior_live_text_removed_before_final_overlay") is not True:
        problems.append({"code": "INLINE_OVERLAY_PRIOR_TEXT_NOT_REMOVED"})
    if not str(relocation.get("policy", "")).strip():
        problems.append(
            {"code": "INLINE_OVERLAY_POLICY_MISSING"}
        )
    if not isinstance(relocation.get("copied_visuals"), list) or not relocation.get(
        "copied_visuals"
    ):
        problems.append({"code": "INLINE_OVERLAY_COPIED_VISUALS_MISSING"})

    pointer = rebuild.get("post_rebuild_final_inline_visual_overlay") or {}
    raw_path = str(pointer.get("evidence_path", "")).strip()
    evidence_path = Path(raw_path)
    if raw_path and not evidence_path.is_absolute():
        evidence_path = (rebuild_path.parent / evidence_path).resolve()
    if not raw_path or not evidence_path.is_file():
        problems.append(
            {"code": "INLINE_OVERLAY_EVIDENCE_MISSING", "path": raw_path}
        )
        return False, {"status": "BLOCKED", "path": raw_path}, problems
    actual_evidence_hash = _sha256(evidence_path)
    if actual_evidence_hash != str(pointer.get("evidence_sha256", "")).upper():
        problems.append(
            {
                "code": "INLINE_OVERLAY_EVIDENCE_HASH",
                "expected": pointer.get("evidence_sha256"),
                "actual": actual_evidence_hash,
            }
        )
    try:
        overlay = json.loads(evidence_path.read_text(encoding="utf-8"))
    except Exception as exc:
        problems.append({"code": "INLINE_OVERLAY_EVIDENCE_INVALID", "message": str(exc)})
        return False, {"status": "BLOCKED", "path": str(evidence_path)}, problems

    bindings = {
        "source": (overlay.get("source") or {}).get("sha256"),
        "manifest": (overlay.get("manifest") or {}).get("sha256"),
        "candidate": (overlay.get("output") or {}).get("sha256"),
    }
    expected_bindings = {
        "source": _sha256(source_path),
        "manifest": _sha256(manifest_path),
        "candidate": _sha256(candidate_path),
    }
    mismatched = {
        key: {"expected": expected_bindings[key], "actual": str(value).upper()}
        for key, value in bindings.items()
        if str(value).upper() != expected_bindings[key]
    }
    if mismatched:
        problems.append({"code": "INLINE_OVERLAY_BINDING_MISMATCH", "bindings": mismatched})
    stage1 = overlay.get("stage1") or {}
    stage2 = overlay.get("stage2") or {}
    final_drawings = overlay.get("source_drawing_verification") or {}
    if (
        stage1.get("status") != "PASS"
        or stage1.get("residual_text_span_count") != 0
        or (stage1.get("source_drawing_verification") or {}).get("status") != "PASS"
    ):
        problems.append(
            {
                "code": "INLINE_OVERLAY_STAGE1_INCOMPLETE",
                "status": stage1.get("status"),
                "residual_text_span_count": stage1.get("residual_text_span_count"),
                "drawing_status": (stage1.get("source_drawing_verification") or {}).get("status"),
            }
        )
    if (
        stage2.get("status") != "PASS"
        or overlay.get("status") != "PASS"
        or final_drawings.get("status") != "PASS"
        or final_drawings.get("missing_source_record_count") != 0
    ):
        problems.append(
            {
                "code": "INLINE_OVERLAY_FINAL_INCOMPLETE",
                "stage2_status": stage2.get("status"),
                "overlay_status": overlay.get("status"),
                "drawing_status": final_drawings.get("status"),
                "missing_source_record_count": final_drawings.get("missing_source_record_count"),
            }
        )
    operations = [
        item
        for item in stage2.get("operations") or []
        if str(item.get("segment_id", "")) == segment_id
    ]
    if len(operations) != 1 or operations[0].get("status") != "PASS":
        problems.append(
            {
                "code": "INLINE_OVERLAY_SEGMENT_OPERATION",
                "segment_id": segment_id,
                "operation_count": len(operations),
            }
        )
    evidence = {
        "status": "PASS" if not problems else "BLOCKED",
        "path": str(evidence_path),
        "sha256": actual_evidence_hash,
        "source_sha256": expected_bindings["source"],
        "manifest_sha256": expected_bindings["manifest"],
        "candidate_sha256": expected_bindings["candidate"],
        "residual_text_span_count": stage1.get("residual_text_span_count"),
        "source_drawing_verification": final_drawings.get("status"),
        "segment_operation_count": len(operations),
    }
    return not problems, evidence, problems
