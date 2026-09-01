#!/usr/bin/env python3
"""Fail-closed QA for source visuals preserved with adjacent UI guidance."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import fitz

from _console import emit_json
from _compound_components import (
    COMPOUND_COMPONENT_SCHEMA,
    candidate_drawing_match_count,
    expected_candidate_drawing_signatures,
    validate_english_allowlist,
)
from _drawing_signature import (
    DrawingSignatureError,
    drawing_record_sequences_equal,
    drawing_records,
    filter_drawing_records,
    operator_counts,
    record_is_single_rect,
    rewrite_single_rect_record,
    signature_contract,
)
from _pdf_catalog import catalog_color_evidence
from _inline_visual_sequences import (
    LEGACY_TWO_STAGE_MASK_MODE,
    validate_legacy_two_stage_overlay_evidence,
)
from _segment_common import (
    PRESERVE_ACTIONS,
    bbox_intersection_area,
    normalize_bbox,
    page_scoped_intersection_area,
    read_json,
    sha256_file,
    validate_manifest,
    write_json,
)


def intersection_area(first: Iterable[float], second: Iterable[float]) -> float:
    return bbox_intersection_area(first, second)


def same_bbox(first: Iterable[float], second: Iterable[float], tolerance: float = 0.2) -> bool:
    return all(
        abs(left - right) <= tolerance
        for left, right in zip(normalize_bbox(first), normalize_bbox(second), strict=True)
    )


def decoded_layers(doc: fitz.Document, page: fitz.Page, bbox: Iterable[float]) -> list[dict[str, Any]]:
    layers: list[dict[str, Any]] = []
    for info in page.get_image_info(hashes=True, xrefs=True):
        if not same_bbox(info.get("bbox", ()), bbox):
            continue
        xref = int(info.get("xref", 0))
        if xref <= 0:
            continue
        pix = fitz.Pixmap(doc, xref)
        digest = info.get("digest")
        layers.append(
            {
                "xref": xref,
                "bbox": normalize_bbox(info.get("bbox", ())),
                "pixel_width": pix.width,
                "pixel_height": pix.height,
                "channels": pix.n,
                "alpha": bool(pix.alpha),
                "image_info_digest": (
                    digest.hex().upper() if isinstance(digest, bytes) else str(digest)
                ),
                "decoded_samples_sha256": hashlib.sha256(pix.samples).hexdigest().upper(),
            }
        )
    return sorted(layers, key=lambda item: (item["decoded_samples_sha256"], item["channels"]))


def comparable_layer_counter(layers: list[dict[str, Any]]) -> Counter[tuple[Any, ...]]:
    return Counter(
        (
            item["decoded_samples_sha256"],
            item["pixel_width"],
            item["pixel_height"],
            item["channels"],
            item["alpha"],
        )
        for item in layers
    )


def rendered_region(page: fitz.Page, bbox: Iterable[float], dpi: int) -> dict[str, Any]:
    scale = dpi / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=fitz.Rect(bbox), alpha=False)
    return {
        "dpi": dpi,
        "width": pix.width,
        "height": pix.height,
        "channels": pix.n,
        "samples_sha256": hashlib.sha256(pix.samples).hexdigest().upper(),
    }


def intersecting_text(page: fitz.Page, bbox: Iterable[float]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for block in page.get_text("dict", flags=fitz.TEXTFLAGS_DICT).get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = str(span.get("text", ""))
                span_bbox = span.get("bbox", ())
                if text and intersection_area(span_bbox, bbox) > 0.01:
                    result.append(
                        {
                            "text": text,
                            "bbox": normalize_bbox(span_bbox),
                            "font": span.get("font", ""),
                            "size_pt": round(float(span.get("size", 0.0)), 3),
                        }
                    )
    return result


def drawing_signatures(
    page: fitz.Page,
    bbox: Iterable[float],
    *,
    records: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return complete, ordered drawing signatures intersecting ``bbox``."""
    if records is not None:
        return filter_drawing_records(records, bbox)
    return drawing_records(page, bbox)


def declares_protected_background_member(segment: Mapping[str, Any]) -> bool:
    """Return whether a live-text route explicitly binds a background member.

    Plain live text on the page has no background drawing to prove. A compound
    component that names a background remains fail-closed and must also declare
    the exact ``preserve_background_bbox`` used for the drawing comparison.
    """

    members = ((segment.get("component_contract") or {}).get("members") or [])
    return any(
        isinstance(member, Mapping)
        and member.get("role") == "background"
        and member.get("policy") in {"preserve", "adjust_background"}
        for member in members
    )


def vector_rule_signatures(
    page: fitz.Page, bbox: Iterable[float], tolerance: float = 0.75
) -> list[dict[str, Any]]:
    bx0, by0, bx1, by1 = normalize_bbox(bbox)
    result: list[dict[str, Any]] = []
    for drawing in page.get_drawings():
        if "s" not in str(drawing.get("type", "")):
            continue
        matched_lines: list[dict[str, Any]] = []
        for item in drawing.get("items") or []:
            if item[0] != "l":
                continue
            start, end = item[1], item[2]
            lx0, lx1 = sorted((float(start.x), float(end.x)))
            ly0, ly1 = sorted((float(start.y), float(end.y)))
            horizontal_match = (
                lx1 >= bx0 - tolerance
                and lx0 <= bx1 + tolerance
                and ly0 >= by0 - tolerance
                and ly1 <= by1 + tolerance
            )
            vertical_match = (
                ly1 >= by0 - tolerance
                and ly0 <= by1 + tolerance
                and lx0 >= bx0 - tolerance
                and lx1 <= bx1 + tolerance
            )
            if horizontal_match or vertical_match:
                matched_lines.append(
                    {
                        "start": [round(float(start.x), 3), round(float(start.y), 3)],
                        "end": [round(float(end.x), 3), round(float(end.y), 3)],
                    }
                )
        if matched_lines:
            result.append(
                {
                    "lines": matched_lines,
                    "color": [round(float(value), 6) for value in drawing.get("color") or []],
                    "width": drawing.get("width"),
                    "dash": drawing.get("dashes"),
                }
            )
    return result


def expected_drawings_after_adjustments(
    source_drawings: list[dict[str, Any]], adjustments: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    expected = list(source_drawings)
    for adjustment in adjustments:
        matches = [
            index
            for index, item in enumerate(expected)
            if record_is_single_rect(
                item,
                adjustment.get("source_bbox", ()),
                adjustment.get("expected_fill"),
            )
        ]
        if len(matches) != 1:
            raise ValueError(
                f"Expected one source drawing for declared adjustment {adjustment.get('component_id')}; "
                f"found {len(matches)}"
            )
        expected[matches[0]] = rewrite_single_rect_record(
            expected[matches[0]],
            adjustment["source_bbox"],
            adjustment["target_bbox"],
        )
    return expected


def normalized_text(value: str) -> str:
    return re.sub(r"\s+", "", value)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify source visuals preserved with adjacent localized UI guidance."
    )
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--rebuild-report", required=True, type=Path)
    parser.add_argument("--allowlist", required=True, type=Path)
    parser.add_argument("--dpi", type=int, default=300)
    parser.add_argument("--forbid-live-text", action="append", default=[])
    parser.add_argument("--require-live-text", action="append", default=[])
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    if args.dpi < 300:
        raise ValueError("Preserved visual QA requires 300 dpi or higher")
    source_path = args.source.resolve()
    candidate_path = args.candidate.resolve()
    manifest_path = args.manifest.resolve()
    rebuild_path = args.rebuild_report.resolve()
    allowlist_path = args.allowlist.resolve()
    manifest = read_json(manifest_path)
    rebuild = read_json(rebuild_path)
    allowlist = read_json(allowlist_path)
    blocking: list[dict[str, Any]] = []
    for allowlist_issue in validate_english_allowlist(allowlist):
        blocking.append(
            {
                "code": allowlist_issue["code"],
                "message": allowlist_issue["message"],
                "evidence": allowlist_issue,
            }
        )

    for item in validate_manifest(manifest, require_translation=True, require_render=True):
        if item.get("severity") == "BLOCKING":
            blocking.append({"code": "MANIFEST_BLOCKED", "evidence": item})
    if sha256_file(source_path) != manifest.get("source", {}).get("sha256"):
        blocking.append({"code": "SOURCE_HASH_MISMATCH"})
    if sha256_file(candidate_path) != rebuild.get("output", {}).get("sha256"):
        blocking.append({"code": "CANDIDATE_HASH_MISMATCH"})
    if sha256_file(manifest_path) != rebuild.get("manifest", {}).get("sha256"):
        blocking.append({"code": "MANIFEST_HASH_MISMATCH"})

    source_doc = fitz.open(source_path)
    candidate_doc = fitz.open(candidate_path)
    source_catalog = catalog_color_evidence(source_path)
    candidate_catalog = catalog_color_evidence(candidate_path)
    output_intents_exact = source_catalog == candidate_catalog
    if not output_intents_exact:
        blocking.append(
            {
                "code": "OUTPUT_INTENT_CHANGED",
                "source": source_catalog,
                "candidate": candidate_catalog,
            }
        )
    rebuild_catalog = rebuild.get("catalog_color_management") or {}
    if (
        rebuild_catalog.get("source") != source_catalog
        or rebuild_catalog.get("candidate") != candidate_catalog
        or bool(rebuild_catalog.get("output_intents_exact")) != output_intents_exact
    ):
        blocking.append(
            {
                "code": "REBUILD_OUTPUT_INTENT_EVIDENCE_MISMATCH",
                "rebuild_report": rebuild_catalog,
                "qa_source": source_catalog,
                "qa_candidate": candidate_catalog,
            }
        )
    selected_pages = [int(value) for value in manifest.get("selected_pages") or []]
    page_map = {page: index for index, page in enumerate(selected_pages)}
    source_drawing_cache = {
        page_number: drawing_records(source_doc[page_number - 1])
        for page_number in selected_pages
    }
    candidate_drawing_cache = {
        page_number: drawing_records(candidate_doc[page_map[page_number]])
        for page_number in selected_pages
    }
    report_segments = rebuild.get("segments") or []
    report_by_id = {item.get("segment_id"): item for item in report_segments}
    manifest_by_id = {item.get("segment_id"): item for item in manifest.get("segments") or []}
    background_adjustments = rebuild.get("background_adjustments") or []
    adjustments_by_component = {
        str(item.get("component_id")): item
        for item in [
            *background_adjustments,
            *(rebuild.get("vector_rule_adjustments") or []),
        ]
    }
    for adjustment in background_adjustments:
        if adjustment.get("status") != "APPLIED_VERIFIED":
            blocking.append(
                {"code": "BACKGROUND_ADJUSTMENT_NOT_VERIFIED", "evidence": adjustment}
            )
        for avoid in adjustment.get("avoid_regions") or []:
            if float(avoid.get("target_intersection_area", 0.0)) > 0.0001:
                blocking.append(
                    {
                        "code": "BACKGROUND_ADJUSTMENT_INTERSECTS_AVOID_REGION",
                        "component_id": adjustment.get("component_id"),
                        "avoid_region": avoid,
                    }
                )

    external_ui_map: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in [
        *(allowlist.get("allowed_ui_english") or []),
        *(allowlist.get("allowed_visual_english") or []),
    ]:
        for visual_id in entry.get("visual_ids") or []:
            external_ui_map[(str(visual_id), str(entry.get("source_text", "")))] = entry

    visual_results: list[dict[str, Any]] = []
    declared_ui_keys: set[tuple[str, str]] = set()
    for packet in manifest.get("page_contexts") or []:
        source_page_number = int(packet["page"])
        output_index = page_map.get(source_page_number)
        if output_index is None or output_index >= candidate_doc.page_count:
            blocking.append({"code": "CANDIDATE_PAGE_MISSING", "page": source_page_number})
            continue
        source_page = source_doc[source_page_number - 1]
        candidate_page = candidate_doc[output_index]
        for visual in packet.get("preserved_visuals") or []:
            visual_id = str(visual["visual_id"])
            visual_kind = str(visual.get("visual_kind", "raster_image"))
            bbox = normalize_bbox(visual["bbox"])
            source_layers: list[dict[str, Any]] = []
            candidate_layers: list[dict[str, Any]] = []
            source_layer_counter: Counter[tuple[Any, ...]] = Counter()
            candidate_layer_counter: Counter[tuple[Any, ...]] = Counter()
            primary_hash: str | None = None
            if visual_kind == "raster_image":
                source_layers = decoded_layers(source_doc, source_page, bbox)
                candidate_layers = decoded_layers(candidate_doc, candidate_page, bbox)
                source_layer_counter = comparable_layer_counter(source_layers)
                candidate_layer_counter = comparable_layer_counter(candidate_layers)
                primary_hash = str(visual.get("decoded_image_sha256", ""))
                primary_source_matches = [
                    item
                    for item in source_layers
                    if item["decoded_samples_sha256"] == primary_hash
                    and item["pixel_width"] == visual.get("pixel_width")
                    and item["pixel_height"] == visual.get("pixel_height")
                ]
                primary_candidate_matches = [
                    item
                    for item in candidate_layers
                    if item["decoded_samples_sha256"] == primary_hash
                    and item["pixel_width"] == visual.get("pixel_width")
                    and item["pixel_height"] == visual.get("pixel_height")
                ]
                if not primary_source_matches:
                    blocking.append({"code": "DECLARED_SOURCE_IMAGE_IDENTITY_MISMATCH", "visual_id": visual_id})
                if not primary_candidate_matches:
                    blocking.append({"code": "CANDIDATE_IMAGE_IDENTITY_MISMATCH", "visual_id": visual_id})
                if source_layer_counter != candidate_layer_counter:
                    blocking.append(
                        {
                            "code": "DECODED_IMAGE_LAYERS_CHANGED",
                            "visual_id": visual_id,
                            "source": list(source_layer_counter.elements()),
                            "candidate": list(candidate_layer_counter.elements()),
                        }
                    )

            source_render = rendered_region(source_page, bbox, args.dpi)
            candidate_render = rendered_region(candidate_page, bbox, args.dpi)
            render_identity_match = source_render == candidate_render
            if not render_identity_match:
                blocking.append(
                    {
                        "code": "COLOR_MANAGED_RENDER_CHANGED",
                        "visual_id": visual_id,
                        "source": source_render,
                        "candidate": candidate_render,
                        "output_intents_exact": output_intents_exact,
                    }
                )
            source_text = intersecting_text(source_page, bbox)
            candidate_text = intersecting_text(candidate_page, bbox)
            if source_text != candidate_text:
                blocking.append(
                    {
                        "code": "TEXT_INTERSECTS_PRESERVED_VISUAL",
                        "visual_id": visual_id,
                        "source": source_text,
                        "candidate": candidate_text,
                    }
                )

            rebuild_intersections: list[dict[str, Any]] = []
            page_mismatch_rejections: list[dict[str, Any]] = []
            for segment_result in report_segments:
                if segment_result.get("action") in PRESERVE_ACTIONS:
                    continue
                segment_source_page = int(segment_result.get("page", -1))
                mask_bbox = segment_result.get("mask_bbox")
                if (
                    segment_source_page != source_page_number
                    and mask_bbox
                    and intersection_area(mask_bbox, bbox) > 0.01
                ):
                    page_mismatch_rejections.append(
                        {
                            "kind": "mask",
                            "segment_id": segment_result.get("segment_id"),
                            "segment_source_page": segment_source_page,
                            "visual_source_page": source_page_number,
                            "reason": "PAGE_MISMATCH_REJECTED_BEFORE_INTERSECTION",
                        }
                    )
                if mask_bbox and page_scoped_intersection_area(
                    segment_source_page,
                    mask_bbox,
                    source_page_number,
                    bbox,
                ) > 0.01:
                    rebuild_intersections.append(
                        {
                            "kind": "mask",
                            "segment_id": segment_result.get("segment_id"),
                            "source_page": segment_source_page,
                            "candidate_page": page_map.get(segment_source_page),
                            "bbox": mask_bbox,
                        }
                    )
                for line in segment_result.get("rendered_lines") or []:
                    if (
                        segment_source_page != source_page_number
                        and intersection_area(line.get("bbox", ()), bbox) > 0.01
                    ):
                        page_mismatch_rejections.append(
                            {
                                "kind": "inserted_text",
                                "segment_id": segment_result.get("segment_id"),
                                "segment_source_page": segment_source_page,
                                "visual_source_page": source_page_number,
                                "reason": "PAGE_MISMATCH_REJECTED_BEFORE_INTERSECTION",
                            }
                        )
                    if page_scoped_intersection_area(
                        segment_source_page,
                        line.get("bbox", ()),
                        source_page_number,
                        bbox,
                    ) > 0.01:
                        rebuild_intersections.append(
                            {
                                "kind": "inserted_text",
                                "segment_id": segment_result.get("segment_id"),
                                "source_page": segment_source_page,
                                "candidate_page": page_map.get(segment_source_page),
                                "bbox": line.get("bbox"),
                                "text": line.get("text"),
                            }
                        )
            if rebuild_intersections:
                blocking.append(
                    {
                        "code": "REBUILD_INTERSECTS_PRESERVED_VISUAL",
                        "visual_id": visual_id,
                        "intersections": rebuild_intersections,
                    }
                )

            ui_results: list[dict[str, Any]] = []
            for ui_entry in [
                *(visual.get("allowed_ui_english") or []),
                *(visual.get("allowed_visual_english") or []),
            ]:
                source_ui = str(ui_entry["source_text"])
                zh_ui = str(ui_entry["zh_TW"])
                key = (visual_id, source_ui)
                declared_ui_keys.add(key)
                external = external_ui_map.get(key)
                guidance_ids = [str(value) for value in ui_entry.get("guidance_segment_ids") or []]
                guidance_required = bool(ui_entry.get("guidance_required", True))
                external_guidance_ids = (
                    [str(value) for value in external.get("guidance_segment_ids") or []]
                    if external is not None
                    else []
                )
                external_guidance_required = (
                    bool(external.get("guidance_required", True))
                    if external is not None
                    else None
                )
                if (
                    external is None
                    or str(external.get("zh_TW", "")) != zh_ui
                    or set(external_guidance_ids) != set(guidance_ids)
                    or external_guidance_required != guidance_required
                ):
                    blocking.append(
                        {
                            "code": "EXTERNAL_UI_ALLOWLIST_MISMATCH",
                            "visual_id": visual_id,
                            "source_text": source_ui,
                            "manifest": {
                                "zh_TW": zh_ui,
                                "guidance_required": guidance_required,
                                "guidance_segment_ids": guidance_ids,
                            },
                            "external": external,
                        }
                    )
                phrase = f"{zh_ui}（{source_ui}）"
                guidance_evidence: list[dict[str, Any]] = []
                phrase_found = False
                for guidance_id in guidance_ids:
                    manifest_segment = manifest_by_id.get(guidance_id) or {}
                    manifest_text = str(manifest_segment.get("zh_TW", ""))
                    rendered_text = "".join(
                        line.get("text", "")
                        for line in (report_by_id.get(guidance_id, {}).get("rendered_lines") or [])
                    )
                    found = phrase in manifest_text and phrase in rendered_text
                    phrase_found = phrase_found or found
                    guidance_evidence.append(
                        {
                            "segment_id": guidance_id,
                            "expected_phrase": phrase,
                            "manifest_contains": phrase in manifest_text,
                            "render_contains": phrase in rendered_text,
                        }
                    )
                if guidance_required and not phrase_found:
                    blocking.append(
                        {
                            "code": "ADJACENT_UI_GUIDANCE_MISSING",
                            "visual_id": visual_id,
                            "expected_phrase": phrase,
                            "guidance_segment_ids": guidance_ids,
                        }
                    )
                ui_results.append(
                    {
                        "source_text": source_ui,
                        "zh_TW": zh_ui,
                        "guidance_required": guidance_required,
                        "guidance_segment_ids": guidance_ids,
                        "guidance_evidence": guidance_evidence,
                    }
                )

            visual_results.append(
                {
                    "visual_id": visual_id,
                    "visual_kind": visual_kind,
                    "page": source_page_number,
                    "source_page": source_page_number,
                    "candidate_page": output_index + 1,
                    "page_mapping_checked_before_intersection": True,
                    "bbox": bbox,
                    "declared_pixel_size": (
                        [visual.get("pixel_width"), visual.get("pixel_height")]
                        if visual_kind == "raster_image"
                        else None
                    ),
                    "declared_decoded_image_sha256": primary_hash,
                    "source_layers": source_layers,
                    "candidate_layers": candidate_layers,
                    "decoded_layers_match": (
                        source_layer_counter == candidate_layer_counter
                        if visual_kind == "raster_image"
                        else None
                    ),
                    "decoded_image_identity": (
                        {
                            "status": "MATCH" if source_layer_counter == candidate_layer_counter else "MISMATCH",
                            "applicable": True,
                            "match": source_layer_counter == candidate_layer_counter,
                        }
                        if visual_kind == "raster_image"
                        else {
                            "status": "NOT_APPLICABLE_VECTOR_COMPONENT",
                            "applicable": False,
                            "match": None,
                        }
                    ),
                    "source_rendered_region": source_render,
                    "candidate_rendered_region": candidate_render,
                    "rendered_region_match": render_identity_match,
                    "color_managed_render_identity": {
                        "status": "MATCH" if render_identity_match else "MISMATCH",
                        "match": render_identity_match,
                        "output_intents_exact": output_intents_exact,
                        "dpi": args.dpi,
                    },
                    "source_intersecting_text": source_text,
                    "candidate_intersecting_text": candidate_text,
                    "rebuild_intersections": rebuild_intersections,
                    "page_mismatch_rejections": page_mismatch_rejections,
                    "allowed_ui_english": ui_results,
                }
            )

    unexpected_external = sorted(set(external_ui_map) - declared_ui_keys)
    if unexpected_external:
        blocking.append(
            {"code": "UNBOUND_EXTERNAL_UI_ALLOWLIST", "entries": unexpected_external}
        )

    candidate_text_all = "\n".join(page.get_text("text") for page in candidate_doc)
    live_text_results = {"forbidden": [], "required": []}
    for value in args.forbid_live_text:
        count = len(re.findall(re.escape(value), candidate_text_all, flags=re.IGNORECASE))
        live_text_results["forbidden"].append({"text": value, "count": count})
        if count:
            blocking.append({"code": "FORBIDDEN_LIVE_TEXT", "text": value, "count": count})
    normalized_candidate = normalized_text(candidate_text_all)
    for value in args.require_live_text:
        count = normalized_candidate.count(normalized_text(value))
        span_evidence: list[dict[str, Any]] = []
        required_chars = set(value)
        for output_page, page in enumerate(candidate_doc, start=1):
            for block in page.get_text("dict", flags=fitz.TEXTFLAGS_DICT).get("blocks", []):
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        text = str(span.get("text", ""))
                        if text and set(text).issubset(required_chars):
                            span_evidence.append(
                                {
                                    "candidate_page": output_page,
                                    "text": text,
                                    "bbox": normalize_bbox(span.get("bbox", ())),
                                    "font": span.get("font", ""),
                                    "size_pt": round(float(span.get("size", 0.0)), 3),
                                }
                            )
        live_text_results["required"].append(
            {"text": value, "normalized_count": count, "span_evidence": span_evidence}
        )
        if count < 1 or not span_evidence:
            blocking.append({"code": "REQUIRED_LIVE_TEXT_MISSING", "text": value})

    background_results: list[dict[str, Any]] = []
    inline_overlay_mask_contracts: list[dict[str, Any]] = []
    for segment in manifest.get("segments") or []:
        render = segment.get("render") or {}
        if render.get("mask_mode") != "remove_text_only":
            continue
        segment_id = segment["segment_id"]
        background_bbox = render.get("preserve_background_bbox")
        report_item = report_by_id.get(segment_id) or {}
        direct_text_only = (
            report_item.get("mask_mode") == "remove_text_only"
            and report_item.get("mask_fill") is None
        )
        if direct_text_only:
            inline_overlay_mask_contracts.append(
                {
                    "segment_id": segment_id,
                    "stage1_mask_mode": "remove_text_only",
                    "final_opaque_overlay": "NOT_DECLARED",
                    "status": "PASS",
                }
            )
        elif report_item.get("mask_mode") == LEGACY_TWO_STAGE_MASK_MODE:
            valid_overlay, overlay_evidence, overlay_problems = (
                validate_legacy_two_stage_overlay_evidence(
                    rebuild=rebuild,
                    rebuild_path=rebuild_path,
                    report_item=report_item,
                    segment_id=str(segment_id),
                    source_path=source_path,
                    candidate_path=candidate_path,
                    manifest_path=manifest_path,
                )
            )
            inline_overlay_mask_contracts.append(
                {
                    "segment_id": segment_id,
                    "stage1_mask_mode": "remove_text_only",
                    "final_opaque_overlay": "HASH_BOUND_TWO_STAGE_EVIDENCE",
                    "evidence": overlay_evidence,
                    "status": "PASS" if valid_overlay else "BLOCKED",
                }
            )
            if not valid_overlay:
                blocking.append(
                    {
                        "code": "TEXT_ONLY_MASK_EVIDENCE_MISSING",
                        "segment_id": segment_id,
                        "overlay_problems": overlay_problems,
                    }
                )
        else:
            blocking.append(
                {"code": "TEXT_ONLY_MASK_EVIDENCE_MISSING", "segment_id": segment_id}
            )
        if not background_bbox and declares_protected_background_member(segment):
            blocking.append({"code": "PRESERVED_BACKGROUND_BBOX_MISSING", "segment_id": segment_id})
        if not background_bbox:
            continue
        page_number = int(segment["page"])
        output_index = page_map[page_number]
        source_drawings: list[dict[str, Any]] = []
        candidate_drawings: list[dict[str, Any]] = []
        signature_errors: list[dict[str, Any]] = []
        for input_role, input_page in (
            ("source", source_doc[page_number - 1]),
            ("candidate", candidate_doc[output_index]),
        ):
            try:
                records = drawing_signatures(
                    input_page,
                    background_bbox,
                    records=(
                        source_drawing_cache[page_number]
                        if input_role == "source"
                        else candidate_drawing_cache[page_number]
                    ),
                )
                if input_role == "source":
                    source_drawings = records
                else:
                    candidate_drawings = records
            except DrawingSignatureError as exc:
                evidence = {
                    "input_role": input_role,
                    "segment_id": segment_id,
                    "source_page": page_number,
                    "candidate_page": output_index + 1,
                    **exc.as_dict(),
                }
                signature_errors.append(evidence)
                blocking.append(
                    {
                        "code": "DRAWING_SIGNATURE_FAIL_CLOSED",
                        "evidence": evidence,
                    }
                )
        relevant_adjustments = [
            item
            for item in background_adjustments
            if int(item.get("source_page", 0)) == page_number
            and (
                intersection_area(item.get("source_bbox", ()), background_bbox) > 0.01
                or intersection_area(item.get("target_bbox", ()), background_bbox) > 0.01
            )
        ]
        try:
            expected_candidate_drawings = (
                expected_drawings_after_adjustments(source_drawings, relevant_adjustments)
                if not signature_errors
                else []
            )
        except (DrawingSignatureError, ValueError) as exc:
            expected_candidate_drawings = []
            blocking.append(
                {
                    "code": "BACKGROUND_ADJUSTMENT_EXPECTATION_FAILED",
                    "segment_id": segment_id,
                    "message": str(exc),
                }
            )
        drawings_match = not signature_errors and drawing_record_sequences_equal(
            expected_candidate_drawings, candidate_drawings
        )
        if not drawings_match:
            blocking.append(
                {
                    "code": "PRESERVED_BACKGROUND_DRAWING_CHANGED",
                    "segment_id": segment_id,
                    "source": source_drawings,
                    "expected_candidate": expected_candidate_drawings,
                    "candidate": candidate_drawings,
                }
            )
        background_results.append(
            {
                "segment_id": segment_id,
                "bbox": normalize_bbox(background_bbox),
                "mask_mode": report_item.get("mask_mode"),
                "mask_fill": report_item.get("mask_fill"),
                "source_drawings": source_drawings,
                "source_path_operator_counts": operator_counts(source_drawings),
                "declared_adjustments": relevant_adjustments,
                "expected_candidate_drawings": expected_candidate_drawings,
                "candidate_drawings": candidate_drawings,
                "candidate_path_operator_counts": operator_counts(candidate_drawings),
                "drawing_signature_errors": signature_errors,
                "drawing_signatures_match": drawings_match,
                "seam_visual_status": "NOT_CHECKED_REQUIRES_300_DPI_VISUAL_REVIEW",
            }
        )

    component_results: list[dict[str, Any]] = []
    seen_components: set[tuple[int, str, str]] = set()
    drawing_roles = {"symbol", "vector_rule", "frame", "background", "icon"}
    for segment in manifest.get("segments") or []:
        contract = segment.get("component_contract") or {}
        if not contract:
            continue
        segment_id = str(segment["segment_id"])
        report_item = report_by_id.get(segment_id) or {}
        reported_contract = report_item.get("component_contract")
        if reported_contract is not None and reported_contract != contract:
            blocking.append(
                {
                    "code": "REBUILD_COMPONENT_CONTRACT_MISMATCH",
                    "segment_id": segment_id,
                }
            )
        page_number = int(segment["page"])
        output_index = page_map[page_number]
        source_page = source_doc[page_number - 1]
        candidate_page = candidate_doc[output_index]
        group_id = str(contract.get("group_id", ""))
        for member in contract.get("members") or []:
            if member.get("policy") == "replace_live_text":
                continue
            component_id = str(member.get("component_id", ""))
            identity = (page_number, group_id, component_id)
            if identity in seen_components:
                continue
            seen_components.add(identity)
            role = str(member.get("role", ""))
            bbox = normalize_bbox(member["bbox"])
            item: dict[str, Any] = {
                "source_page": page_number,
                "candidate_page": output_index + 1,
                "component_group_id": group_id,
                "component_id": component_id,
                "role": role,
                "policy": member.get("policy"),
                "bbox": bbox,
            }
            if contract.get("schema") == COMPOUND_COMPONENT_SCHEMA:
                candidate_member = copy.deepcopy(member)
                adjustment = adjustments_by_component.get(component_id)
                if adjustment is not None:
                    candidate_member["target_bbox"] = adjustment["target_bbox"]
                    if adjustment.get("translation_delta_pt") is not None:
                        candidate_member["translation_delta_pt"] = adjustment[
                            "translation_delta_pt"
                        ]
                source_declarations = (
                    (member.get("source_evidence") or {})
                    .get("ordered_path_signatures", {})
                    .get("drawing_signatures", [])
                )
                candidate_declarations = expected_candidate_drawing_signatures(
                    candidate_member
                )
                source_counts = [
                    candidate_drawing_match_count(
                        source_page,
                        declaration,
                        records=source_drawing_cache[page_number],
                    )
                    for declaration in source_declarations
                ]
                candidate_counts = [
                    candidate_drawing_match_count(
                        candidate_page,
                        declaration,
                        records=candidate_drawing_cache[page_number],
                    )
                    for declaration in candidate_declarations
                ]
                if not source_declarations and (member.get("source_evidence") or {}).get(
                    "text_spans"
                ):
                    source_region = rendered_region(source_page, bbox, args.dpi)
                    candidate_region = rendered_region(candidate_page, bbox, args.dpi)
                    match = source_region == candidate_region
                    expectation = "TEXT_GLYPH_REGION_IDENTICAL_AT_300_DPI"
                    item["source_region"] = source_region
                    item["candidate_region"] = candidate_region
                elif member.get("policy") == "replace_vector_outlined_text":
                    match = all(count == 1 for count in source_counts) and all(
                        count == 0 for count in candidate_counts
                    )
                    expectation = "SOURCE_ONCE_CANDIDATE_ABSENT"
                else:
                    match = all(count == 1 for count in source_counts) and all(
                        count == 1 for count in candidate_counts
                    )
                    expectation = "SOURCE_AND_CANDIDATE_ONCE"
                item.update(
                    {
                        "identity_method": "manifest_bound_complete_ordered_drawing_signatures",
                        "expectation": expectation,
                        "source_match_counts": source_counts,
                        "candidate_match_counts": candidate_counts,
                        "match": match,
                    }
                )
                if not match:
                    blocking.append(
                        {
                            "code": "COMPOUND_MEMBER_DRAWING_SIGNATURE_MISMATCH",
                            "component_id": component_id,
                            "source_page": page_number,
                            "evidence": item,
                        }
                    )
                component_results.append(item)
                continue
            if role == "dingbat_marker":
                source_render = rendered_region(source_page, bbox, args.dpi)
                candidate_render = rendered_region(candidate_page, bbox, args.dpi)
                match = source_render == candidate_render
                item.update(
                    {
                        "identity_method": "300_dpi_rendered_region",
                        "source": source_render,
                        "candidate": candidate_render,
                        "match": match,
                    }
                )
                if not match:
                    blocking.append(
                        {
                            "code": "PRESERVED_DINGBAT_CHANGED",
                            "component_id": component_id,
                            "source_page": page_number,
                        }
                    )
            elif role == "vector_rule":
                source_rules = vector_rule_signatures(source_page, bbox)
                candidate_rules = vector_rule_signatures(candidate_page, bbox)
                match = bool(source_rules) and source_rules == candidate_rules
                item.update(
                    {
                        "identity_method": "vector_rule_stroke_signatures",
                        "source": source_rules,
                        "candidate": candidate_rules,
                        "match": match,
                    }
                )
                if not match:
                    blocking.append(
                        {
                            "code": "PRESERVED_VECTOR_RULE_CHANGED",
                            "component_id": component_id,
                            "source_page": page_number,
                        }
                    )
            elif member.get("policy") == "adjust_background":
                adjustment = adjustments_by_component.get(component_id)
                expected_target_bbox = member.get("target_bbox")
                if expected_target_bbox is None and adjustment:
                    expected_target_bbox = (
                        adjustment.get("dependent_geometry_resolution") or {}
                    ).get("resolved_target_bbox")
                match = bool(
                    adjustment
                    and adjustment.get("status") == "APPLIED_VERIFIED"
                    and same_bbox(adjustment.get("source_bbox", ()), bbox)
                    and same_bbox(
                        adjustment.get("target_bbox", ()), expected_target_bbox or ()
                    )
                    and all(
                        float(region.get("target_intersection_area", 0.0)) <= 0.0001
                        for region in adjustment.get("avoid_regions") or []
                    )
                )
                item.update(
                    {
                        "identity_method": "declared_background_rect_rewrite",
                        "source": {"bbox": bbox},
                        "candidate": adjustment,
                        "match": match,
                    }
                )
                if not match:
                    blocking.append(
                        {
                            "code": "ADJUSTED_BACKGROUND_EVIDENCE_MISMATCH",
                            "component_id": component_id,
                            "source_page": page_number,
                        }
                    )
            elif role in drawing_roles:
                source_drawings: list[dict[str, Any]] = []
                candidate_drawings: list[dict[str, Any]] = []
                signature_errors: list[dict[str, Any]] = []
                for input_role, input_page in (
                    ("source", source_page),
                    ("candidate", candidate_page),
                ):
                    try:
                        records = drawing_signatures(
                            input_page,
                            bbox,
                            records=(
                                source_drawing_cache[page_number]
                                if input_role == "source"
                                else candidate_drawing_cache[page_number]
                            ),
                        )
                        if input_role == "source":
                            source_drawings = records
                        else:
                            candidate_drawings = records
                    except DrawingSignatureError as exc:
                        evidence = {
                            "input_role": input_role,
                            "component_id": component_id,
                            "source_page": page_number,
                            "candidate_page": output_index + 1,
                            **exc.as_dict(),
                        }
                        signature_errors.append(evidence)
                        blocking.append(
                            {
                                "code": "DRAWING_SIGNATURE_FAIL_CLOSED",
                                "evidence": evidence,
                            }
                        )
                match = not signature_errors and drawing_record_sequences_equal(
                    source_drawings, candidate_drawings
                )
                item.update(
                    {
                        "identity_method": "ordered_complete_vector_drawing_signatures",
                        "source": source_drawings,
                        "candidate": candidate_drawings,
                        "source_path_operator_counts": operator_counts(source_drawings),
                        "candidate_path_operator_counts": operator_counts(candidate_drawings),
                        "drawing_signature_errors": signature_errors,
                        "match": match,
                    }
                )
                if not match:
                    blocking.append(
                        {
                            "code": "PRESERVED_COMPONENT_DRAWING_CHANGED",
                            "component_id": component_id,
                            "source_page": page_number,
                        }
                    )
            else:
                item.update(
                    {
                        "identity_method": "covered_by_complete_visual_gate",
                        "match": None,
                    }
                )
            component_results.append(item)

    source_doc.close()
    candidate_doc.close()
    report = {
        "schema": "pdf-tw-localize/preserved-visual-qa/v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": {"path": str(source_path), "sha256": sha256_file(source_path)},
        "candidate": {"path": str(candidate_path), "sha256": sha256_file(candidate_path)},
        "manifest": {"path": str(manifest_path), "sha256": sha256_file(manifest_path)},
        "rebuild_report": {"path": str(rebuild_path), "sha256": sha256_file(rebuild_path)},
        "allowlist": {"path": str(allowlist_path), "sha256": sha256_file(allowlist_path)},
        "dpi": args.dpi,
        "drawing_signature_contract": signature_contract(),
        "catalog_color_management": {
            "source": source_catalog,
            "candidate": candidate_catalog,
            "output_intents_exact": output_intents_exact,
            "decoded_image_identity_is_separate": True,
            "color_managed_render_identity_is_separate": True,
        },
        "page_aware_intersection_gate": {
            "mapping_checked_before_bbox_intersection": True,
            "cross_page_false_intersection_count": 0,
            "page_mismatch_rejection_count": sum(
                len(item.get("page_mismatch_rejections") or []) for item in visual_results
            ),
            "same_page_intersection_count": sum(
                len(item.get("rebuild_intersections") or []) for item in visual_results
            ),
        },
        "visuals": visual_results,
        "live_text": live_text_results,
        "preserved_backgrounds": background_results,
        "inline_overlay_mask_contracts": inline_overlay_mask_contracts,
        "component_preservation": component_results,
        "background_adjustments": background_adjustments,
        "blocking_issue_count": len(blocking),
        "blocking_issues": blocking,
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
