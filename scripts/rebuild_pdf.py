#!/usr/bin/env python3
"""Rebuild selected proof pages from the English source and translated segments."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import fitz

from _console import emit_json
from _compound_components import (
    CompoundComponentError,
    apply_vector_path_replacements,
    apply_vector_rule_adjustments,
    candidate_member_bboxes,
    content_path_construction_signature_sha256,
    content_path_reserialization_equivalent,
    parse_content_paths,
    resolve_translation_dependent_bbox,
    restore_reserialized_paths_from_source_batch,
    signature_sha256,
)
from _pdf_catalog import catalog_color_evidence, clone_output_intents
from _inline_visual_sequences import COPY_METHOD, inline_contract
from _segment_common import (
    PRESERVE_ACTIONS,
    bbox_inside,
    normalize_bbox,
    read_json,
    sha256_file,
    validate_manifest,
    write_json,
)


def color(value: Any, default: tuple[float, float, float]) -> tuple[float, float, float]:
    if value is None:
        return default
    if len(value) != 3:
        raise ValueError(f"Color must contain three values: {value}")
    parsed = tuple(float(channel) for channel in value)
    if any(channel < 0 or channel > 1 for channel in parsed):
        raise ValueError(f"Color channels must be between 0 and 1: {value}")
    return parsed  # type: ignore[return-value]


def inline_cover_bboxes(segment: dict[str, Any]) -> list[list[float]]:
    contract = inline_contract(segment)
    if contract is None:
        return []
    return [normalize_bbox(values) for values in contract.get("cover_bboxes") or []]


def union_rects(boxes: list[list[float]]) -> list[float]:
    if not boxes:
        raise ValueError("Cannot union an empty bbox list")
    return [
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    ]


def apply_inline_visual_relocations(
    page: fitz.Page,
    source_doc: fitz.Document,
    source_page_number: int,
    page_segments: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Apply opaque source cleanup, then copy complete source visual clips.

    Text removal is performed earlier with transparent redaction. This function
    records the later opaque cover separately so `mask_mode=remove_text_only`
    remains a truthful stage-1 statement.
    """

    inline_segments = [segment for segment in page_segments if inline_contract(segment)]
    if not inline_segments:
        return {}
    results: dict[str, dict[str, Any]] = {}
    for segment in inline_segments:
        contract = inline_contract(segment) or {}
        fill = color(contract.get("cover_fill"), (1.0, 1.0, 1.0))
        covers = inline_cover_bboxes(segment)
        for values in covers:
            page.draw_rect(
                fitz.Rect(values),
                color=None,
                fill=fill,
                width=0.0,
                overlay=True,
            )
        results[str(segment["segment_id"])] = {
            "schema": contract.get("schema"),
            "policy": contract.get("policy"),
            "homologous_set_id": contract.get("homologous_set_id"),
            "connector": contract.get("connector"),
            "object_position_policy": contract.get("object_position_policy"),
            "internal_content_policy": contract.get("internal_content_policy"),
            "opaque_cover": {
                "bboxes": covers,
                "fill": list(fill),
                "basis": contract.get("cover_fill_basis"),
                "applied_after_text_only_redaction": True,
            },
            "copied_visuals": [],
            "prior_live_text_removed_before_final_overlay": True,
            "status": "PENDING_VISUAL_COPY",
        }
    for segment in inline_segments:
        contract = inline_contract(segment) or {}
        result = results[str(segment["segment_id"])]
        for index, visual in enumerate(contract.get("relocations") or [], start=1):
            source_clip = fitz.Rect(visual["source_clip_bbox"])
            target_clip = fitz.Rect(visual["target_clip_bbox"])
            xref = page.show_pdf_page(
                target_clip,
                source_doc,
                source_page_number - 1,
                keep_proportion=False,
                overlay=True,
                clip=source_clip,
            )
            result["copied_visuals"].append(
                {
                    "index": index,
                    "semantic_label": visual["semantic_label"],
                    "source_clip_bbox": normalize_bbox(source_clip),
                    "target_clip_bbox": normalize_bbox(target_clip),
                    "horizontal_shift_pt": float(visual["horizontal_shift_pt"]),
                    "vertical_shift_pt": float(visual["vertical_shift_pt"]),
                    "object_policy": visual["object_policy"],
                    "copy_method": COPY_METHOD,
                    "internal_content_edited": False,
                    "form_xref": int(xref),
                }
            )
        result["status"] = "APPLIED_SOURCE_CLIP_COPY"
    return results


def color_to_srgb_int(value: tuple[float, float, float]) -> int:
    channels = [max(0, min(255, round(float(component) * 255))) for component in value]
    return (channels[0] << 16) | (channels[1] << 8) | channels[2]


def source_srgb_to_color(value: Any) -> tuple[float, float, float]:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 0xFFFFFF:
        raise ValueError(f"Source text color must be a 24-bit sRGB integer: {value!r}")
    red, green, blue = fitz.sRGB_to_rgb(value)
    return (red / 255.0, green / 255.0, blue / 255.0)


def resolve_text_color(
    segment: dict[str, Any], render: dict[str, Any]
) -> tuple[tuple[float, float, float], str, int]:
    explicit = render.get("text_color")
    if explicit is not None:
        resolved = color(explicit, (0.0, 0.0, 0.0))
        return resolved, "manifest_explicit", color_to_srgb_int(resolved)

    source_colors = list(
        dict.fromkeys((segment.get("font_style") or {}).get("source_colors") or [])
    )
    if len(source_colors) == 1:
        source_srgb = int(source_colors[0])
        return (
            source_srgb_to_color(source_srgb),
            "preserve_single_source_color",
            source_srgb,
        )
    if not source_colors:
        return (0.0, 0.0, 0.0), "legacy_default_black_no_source_color", 0
    raise ValueError(
        f"{segment.get('segment_id')}: multiple source text colors require explicit "
        "fragment or inspected text_color declarations"
    )


def expand_bbox(values: Any, padding: float, page_rect: fitz.Rect) -> fitz.Rect:
    x0, y0, x1, y1 = normalize_bbox(values)
    return fitz.Rect(
        max(page_rect.x0, x0 - padding),
        max(page_rect.y0, y0 - padding),
        min(page_rect.x1, x1 + padding),
        min(page_rect.y1, y1 + padding),
    )


PDF_NUMBER = rb"[+-]?(?:\d+(?:\.\d*)?|\.\d+)"
RECT_OPERATOR_RE = re.compile(
    rb"(?<![A-Za-z0-9.])"
    rb"(?P<x>" + PDF_NUMBER + rb")\s+"
    rb"(?P<y>" + PDF_NUMBER + rb")\s+"
    rb"(?P<w>" + PDF_NUMBER + rb")\s+"
    rb"(?P<h>" + PDF_NUMBER + rb")\s+re\b"
)


def bbox_intersection_area(first: Any, second: Any) -> float:
    ax0, ay0, ax1, ay1 = normalize_bbox(first)
    bx0, by0, bx1, by1 = normalize_bbox(second)
    return max(0.0, min(ax1, bx1) - max(ax0, bx0)) * max(
        0.0, min(ay1, by1) - max(ay0, by0)
    )


def same_bbox(first: Any, second: Any, tolerance: float = 0.02) -> bool:
    return all(
        abs(left - right) <= tolerance
        for left, right in zip(normalize_bbox(first), normalize_bbox(second), strict=True)
    )


def format_pdf_number(value: float) -> str:
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def page_bbox_from_untransformed_pdf_rect(
    operands: tuple[float, float, float, float], page: fitz.Page
) -> list[float]:
    if page.rotation != 0 or abs(page.rect.x0) > 0.001 or abs(page.rect.y0) > 0.001:
        raise ValueError(
            "rewrite_untransformed_rect requires an unrotated page whose visible rect starts at (0, 0)"
        )
    x, y, width, height = operands
    x0, x1 = sorted((x, x + width))
    pdf_y0, pdf_y1 = sorted((y, y + height))
    return normalize_bbox([x0, page.rect.height - pdf_y1, x1, page.rect.height - pdf_y0])


def untransformed_pdf_rect_from_page_bbox(values: Any, page: fitz.Page) -> tuple[float, ...]:
    if page.rotation != 0 or abs(page.rect.x0) > 0.001 or abs(page.rect.y0) > 0.001:
        raise ValueError(
            "rewrite_untransformed_rect requires an unrotated page whose visible rect starts at (0, 0)"
        )
    x0, y0, x1, y1 = normalize_bbox(values)
    return (x0, page.rect.height - y1, x1 - x0, y1 - y0)


def matching_rect_drawings(
    page: fitz.Page, bbox: Any, expected_fill: Any
) -> list[dict[str, Any]]:
    expected = tuple(round(float(channel), 5) for channel in expected_fill)
    result: list[dict[str, Any]] = []
    for drawing in page.get_drawings():
        rect = drawing.get("rect")
        fill = drawing.get("fill")
        if rect is None or fill is None or not same_bbox(rect, bbox):
            continue
        actual = tuple(round(float(channel), 5) for channel in fill)
        if actual != expected:
            continue
        if not any(item[0] == "re" for item in drawing.get("items") or []):
            continue
        result.append(
            {
                "bbox": normalize_bbox(rect),
                "fill": list(actual),
                "type": drawing.get("type"),
                "item_count": len(drawing.get("items") or []),
            }
        )
    return result


def collect_background_adjustments(
    page_segments: list[dict[str, Any]], *, dependent: bool | None = None
) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for segment in page_segments:
        for member in (segment.get("component_contract") or {}).get("members") or []:
            if member.get("policy") != "adjust_background":
                continue
            is_dependent = member.get("dependent_geometry") is not None
            if dependent is not None and is_dependent != dependent:
                continue
            component_id = str(member.get("component_id", ""))
            previous = by_id.setdefault(component_id, member)
            if previous != member:
                raise ValueError(f"Conflicting background adjustment declarations: {component_id}")
    return list(by_id.values())


def collect_vector_path_replacements(
    page_segments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Collect each explicitly bound outlined-text member exactly once."""
    by_id: dict[str, dict[str, Any]] = {}
    for segment in page_segments:
        render = segment.get("render") or {}
        if render.get("action") != "replace_vector_outlined_text":
            continue
        member_id = str(render.get("vector_member_id", ""))
        matching = [
            member
            for member in (segment.get("component_contract") or {}).get("members") or []
            if str(member.get("component_id", "")) == member_id
            and member.get("role") == "translatable_vector_outlined_text"
            and member.get("policy") == "replace_vector_outlined_text"
        ]
        if len(matching) != 1:
            raise ValueError(
                f"Outlined-text segment must bind exactly one declared member: "
                f"{segment.get('segment_id')} -> {member_id!r}"
            )
        previous = by_id.setdefault(member_id, matching[0])
        if previous != matching[0]:
            raise ValueError(f"Conflicting outlined-text member declarations: {member_id}")
    return list(by_id.values())


def collect_vector_rule_adjustments(
    page_segments: list[dict[str, Any]], *, dependent: bool | None = None
) -> list[dict[str, Any]]:
    """Collect each exact-path rule adjustment once across repeated group contracts."""
    by_id: dict[str, dict[str, Any]] = {}
    for segment in page_segments:
        for member in (segment.get("component_contract") or {}).get("members") or []:
            if member.get("policy") != "adjust_vector_rule":
                continue
            is_dependent = member.get("dependent_geometry") is not None
            if dependent is not None and is_dependent != dependent:
                continue
            component_id = str(member.get("component_id", ""))
            previous = by_id.setdefault(component_id, member)
            if previous != member:
                raise ValueError(f"Conflicting vector-rule adjustment declarations: {component_id}")
    return list(by_id.values())


def component_contract_for_member(
    page_segments: list[dict[str, Any]], component_id: str
) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    for segment in page_segments:
        contract = segment.get("component_contract") or {}
        if any(
            str(member.get("component_id", "")) == component_id
            for member in contract.get("members") or []
            if isinstance(member, dict)
        ):
            matches.append(contract)
    if not matches:
        raise ValueError(f"No component contract declares dependent member: {component_id}")
    first = matches[0]
    if any(item.get("members") != first.get("members") for item in matches[1:]):
        raise ValueError(f"Conflicting component contracts for dependent member: {component_id}")
    return first


def apply_background_adjustment(
    doc: fitz.Document, page: fitz.Page, source_page_number: int, member: dict[str, Any]
) -> tuple[fitz.Page, dict[str, Any]]:
    component_id = str(member["component_id"])
    if member.get("role") != "background" or member.get("policy") != "adjust_background":
        raise ValueError(f"Invalid background adjustment member: {component_id}")
    if member.get("adjustment_method") != "rewrite_untransformed_rect":
        raise ValueError(f"Unsupported background adjustment method: {component_id}")
    source_bbox = normalize_bbox(member["bbox"])
    target_bbox = normalize_bbox(member["target_bbox"])
    expected_fill = member["expected_fill"]
    if not bbox_inside(target_bbox, source_bbox, tolerance=0.01):
        raise ValueError(f"Background adjustment expands outside its source bbox: {component_id}")
    avoid_results: list[dict[str, Any]] = []
    for avoid in member.get("avoid_regions") or []:
        source_overlap = bbox_intersection_area(source_bbox, avoid["bbox"])
        target_overlap = bbox_intersection_area(target_bbox, avoid["bbox"])
        if source_overlap <= 0 or target_overlap > 0.0001:
            raise ValueError(f"Background adjustment does not clear avoid region: {component_id}")
        avoid_results.append(
            {
                "region_id": avoid["region_id"],
                "bbox": normalize_bbox(avoid["bbox"]),
                "source_intersection_area": round(source_overlap, 6),
                "target_intersection_area": round(target_overlap, 6),
            }
        )

    before_source = matching_rect_drawings(page, source_bbox, expected_fill)
    if len(before_source) != 1:
        raise ValueError(
            f"Expected exactly one source background rectangle for {component_id}; found {len(before_source)}"
        )
    matches: list[tuple[int, re.Match[bytes], bytes]] = []
    for xref in page.get_contents():
        stream = doc.xref_stream(xref)
        for match in RECT_OPERATOR_RE.finditer(stream):
            operands = tuple(float(match.group(name)) for name in ("x", "y", "w", "h"))
            if same_bbox(page_bbox_from_untransformed_pdf_rect(operands, page), source_bbox):
                matches.append((xref, match, stream))
    if len(matches) != 1:
        raise ValueError(
            f"Expected exactly one untransformed PDF rectangle operator for {component_id}; found {len(matches)}"
        )
    xref, match, stream = matches[0]
    target_operands = untransformed_pdf_rect_from_page_bbox(target_bbox, page)
    replacement = (" ".join(format_pdf_number(value) for value in target_operands) + " re").encode(
        "ascii"
    )
    updated_stream = stream[: match.start()] + replacement + stream[match.end() :]
    doc.update_stream(xref, updated_stream, compress=1)
    page = doc.reload_page(page)

    after_source = matching_rect_drawings(page, source_bbox, expected_fill)
    after_target = matching_rect_drawings(page, target_bbox, expected_fill)
    if after_source or len(after_target) != 1:
        raise ValueError(
            f"Background rectangle rewrite verification failed for {component_id}: "
            f"source={len(after_source)} target={len(after_target)}"
        )
    evidence = {
        "source_page": source_page_number,
        "component_id": component_id,
        "role": "background",
        "policy": "adjust_background",
        "method": "rewrite_untransformed_rect",
        "source_bbox": source_bbox,
        "target_bbox": target_bbox,
        "expected_fill": [round(float(value), 6) for value in expected_fill],
        "avoid_regions": avoid_results,
        "source_operator_xref": xref,
        "source_operator": match.group(0).decode("ascii"),
        "target_operator": replacement.decode("ascii"),
        "stream_sha256_before": hashlib.sha256(stream).hexdigest().upper(),
        "stream_sha256_after": hashlib.sha256(updated_stream).hexdigest().upper(),
        "source_rect_count_before": len(before_source),
        "source_rect_count_after": len(after_source),
        "target_rect_count_after": len(after_target),
        "status": "APPLIED_VERIFIED",
    }
    return page, evidence


def font_role_evidence(font: fitz.Font, path: Path, role: str) -> dict[str, Any]:
    flags = dict(font.flags)
    return {
        "role": role,
        "path": str(path),
        "sha256": sha256_file(path),
        "font_name": font.name,
        "is_bold": bool(font.is_bold),
        "is_italic": bool(font.is_italic),
        "flags": flags,
    }


TOKEN_RE = re.compile(r"\s+|[A-Za-z0-9]+(?:[._/°%+\-][A-Za-z0-9]+)*|.", re.DOTALL)
CLOSING_PUNCTUATION = set("。，、；：！？）》】」』〉〕）］｝…")


def wrap_paragraph(text: str, font: fitz.Font, size: float, width: float) -> list[str]:
    if not text:
        return [""]
    tokens = TOKEN_RE.findall(text)
    lines: list[str] = []
    current = ""
    for token in tokens:
        if token.isspace():
            if current and not current.endswith(" "):
                current += " "
            continue
        candidate = current + token
        if current and font.text_length(candidate.rstrip(), fontsize=size) > width:
            if token in CLOSING_PUNCTUATION:
                current = candidate
            else:
                lines.append(current.rstrip())
                current = token.lstrip()
        else:
            current = candidate
    if current.strip() or not lines:
        lines.append(current.rstrip())
    return lines


def wrap_text(text: str, font: fitz.Font, size: float, width: float, layout: str) -> list[str]:
    if layout == "vertical-chars":
        return [character for character in text if not character.isspace()]
    result: list[str] = []
    for paragraph in text.splitlines() or [text]:
        if not paragraph:
            result.append("")
            continue
        result.extend(wrap_paragraph(paragraph, font, size, width))
    return result


def fit_text(
    text: str,
    target: fitz.Rect,
    font: fitz.Font,
    requested_size: float,
    minimum_size: float,
    line_spacing: float,
    layout: str,
) -> tuple[float, list[str]]:
    size = requested_size
    while size + 1e-6 >= minimum_size:
        lines = wrap_text(text, font, size, target.width, layout)
        height = len(lines) * size * line_spacing
        widest = max((font.text_length(line, fontsize=size) for line in lines), default=0.0)
        if height <= target.height + 1e-6 and widest <= target.width + 1e-6:
            return round(size, 3), lines
        size = round(size - 0.1, 3)
    raise ValueError(
        f"Text does not fit target bbox without violating {minimum_size:.2f} pt minimum: {text!r}"
    )


def insert_lines(
    page: fitz.Page,
    segment: dict[str, Any],
    font: fitz.Font,
    font_name: str,
    font_role: dict[str, Any],
) -> dict[str, Any]:
    render = segment["render"]
    fragments = render.get("fragments")
    if fragments is not None:
        if not isinstance(fragments, list) or not fragments:
            raise ValueError(
                f"{segment['segment_id']}: render.fragments must be a non-empty list"
            )
        fragment_results: list[dict[str, Any]] = []
        rendered_lines: list[dict[str, Any]] = []
        for fragment in fragments:
            fragment_id = str(fragment.get("fragment_id", "")).strip()
            if not fragment_id:
                raise ValueError(
                    f"{segment['segment_id']}: each render fragment needs fragment_id"
                )
            fragment_segment = copy.deepcopy(segment)
            fragment_segment["segment_id"] = (
                f"{segment['segment_id']}.fragment.{fragment_id}"
            )
            fragment_render = copy.deepcopy(render)
            fragment_render.pop("fragments", None)
            for key in (
                "target_bbox",
                "container_bbox",
                "font_size_pt",
                "min_font_size_pt",
                "line_spacing",
                "align",
                "valign",
                "layout",
                "text_color",
                "text_color_policy",
                "text_color_basis",
                "source_small_exception",
            ):
                if key in fragment:
                    fragment_render[key] = copy.deepcopy(fragment[key])
            fragment_render["text_override"] = str(fragment.get("zh_TW", ""))
            fragment_segment["render"] = fragment_render
            result = insert_lines(
                page, fragment_segment, font, font_name, font_role
            )
            result["fragment_id"] = fragment_id
            result["source_fragment_text"] = str(fragment.get("source_text", ""))
            for line in result["rendered_lines"]:
                line["fragment_id"] = fragment_id
            fragment_results.append(result)
            rendered_lines.extend(result["rendered_lines"])
        source_size = float(segment["font_style"].get("source_font_size_pt", 0.0))
        used_sizes = [float(item["used_font_size_pt"]) for item in fragment_results]
        return {
            "segment_id": segment["segment_id"],
            "page": segment["page"],
            "semantic_type": segment["semantic_type"],
            "action": render.get("action", "replace"),
            "source_bbox": segment["bbox"],
            "target_bbox": normalize_bbox(render["target_bbox"]),
            "container_bbox": render.get("container_bbox"),
            "requested_font_size_pt": max(
                float(item["requested_font_size_pt"]) for item in fragment_results
            ),
            "used_font_size_pt": min(used_sizes),
            "source_font_size_pt": source_size,
            "font_ratio": (
                round(min(used_sizes) / source_size, 4) if source_size else None
            ),
            "line_count": len(rendered_lines),
            "rendered_lines": rendered_lines,
            "fragment_results": fragment_results,
            "fit_status": "FIT_FRAGMENTED_LIVE_TEXT",
            "source_small_exception": render.get("source_small_exception"),
            "component_contract": segment.get("component_contract"),
            "requested_font_role": font_role["role"],
            "font_role_evidence": font_role,
            "text_color_resolution": "fragment_specific",
            "alignment_contract": render.get("alignment_contract"),
            "render_align": render.get("align", "left"),
        }
    target = fitz.Rect(render["target_bbox"])
    source_size = float(segment["font_style"].get("source_font_size_pt", 0.0))
    requested_size = float(render["font_size_pt"])
    rule_minimum = max(6.0, source_size * 0.75)
    exception = render.get("source_small_exception")
    minimum_size = float(render.get("min_font_size_pt", requested_size))
    if exception:
        minimum_size = max(1.0, minimum_size)
    else:
        minimum_size = max(rule_minimum, minimum_size)
    layout = str(render.get("layout", "horizontal"))
    text = str(render.get("text_override", segment["zh_TW"]))
    try:
        used_size, lines = fit_text(
            text,
            target,
            font,
            requested_size,
            minimum_size,
            float(render.get("line_spacing", 1.18)),
            layout,
        )
    except ValueError as exc:
        raise ValueError(f"{segment['segment_id']}: {exc}") from exc
    line_height = used_size * float(render.get("line_spacing", 1.18))
    total_height = len(lines) * line_height
    valign = render.get("valign", "top")
    if valign == "middle":
        top = target.y0 + (target.height - total_height) / 2
    elif valign == "bottom":
        top = target.y1 - total_height
    else:
        top = target.y0
    # Keep the insertion baseline consistent with the conservative ink bbox
    # used below.  A smaller offset makes every top-aligned line appear to
    # escape its own target by 0.02 * font size, which only becomes visible for
    # large translated headings.
    baseline_offset = used_size * 0.88
    text_color, text_color_resolution, text_color_srgb = resolve_text_color(
        segment, render
    )
    align = render.get("align", "left")
    rendered_lines: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        width = font.text_length(line, fontsize=used_size)
        if align == "center":
            x = target.x0 + (target.width - width) / 2
        elif align == "right":
            x = target.x1 - width
        else:
            x = target.x0
        baseline = top + index * line_height + baseline_offset
        page.insert_text(
            fitz.Point(x, baseline),
            line,
            fontsize=used_size,
            fontname=font_name,
            color=text_color,
            overlay=True,
        )
        line_bbox = [
            round(x, 3),
            round(baseline - used_size * 0.88, 3),
            round(x + width, 3),
            round(baseline + used_size * 0.18, 3),
        ]
        if not bbox_inside(line_bbox, target, tolerance=0.35):
            raise ValueError(f"Rendered line escaped target bbox: {segment['segment_id']} {line_bbox}")
        rendered_lines.append(
            {
                "text": line,
                "bbox": line_bbox,
                "text_color": [round(component, 6) for component in text_color],
                "text_color_srgb": text_color_srgb,
                "text_color_resolution": text_color_resolution,
                "align": align,
            }
        )
    return {
        "segment_id": segment["segment_id"],
        "page": segment["page"],
        "semantic_type": segment["semantic_type"],
        "action": render.get("action", "replace"),
        "source_bbox": segment["bbox"],
        "target_bbox": normalize_bbox(target),
        "container_bbox": render.get("container_bbox"),
        "requested_font_size_pt": requested_size,
        "used_font_size_pt": used_size,
        "source_font_size_pt": source_size,
        "font_ratio": round(used_size / source_size, 4) if source_size else None,
        "line_count": len(lines),
        "rendered_lines": rendered_lines,
        "fit_status": "FIT",
        "source_small_exception": exception,
        "component_contract": segment.get("component_contract"),
        "requested_font_role": font_role["role"],
        "font_role_evidence": font_role,
        "text_color": [round(component, 6) for component in text_color],
        "text_color_srgb": text_color_srgb,
        "text_color_resolution": text_color_resolution,
        "alignment_contract": render.get("alignment_contract"),
        "render_align": align,
    }


def embedded_fonts(doc: fitz.Document) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[int] = set()
    for page in doc:
        for item in page.get_fonts(full=True):
            xref = int(item[0])
            if xref in seen:
                continue
            seen.add(xref)
            extracted_size = 0
            try:
                extracted = doc.extract_font(xref)
                if extracted and len(extracted) >= 4 and extracted[3]:
                    extracted_size = len(extracted[3])
            except Exception:
                pass
            result.append(
                {
                    "xref": xref,
                    "extension": item[1],
                    "type": item[2],
                    "basefont": item[3],
                    "resource_name": item[4],
                    "encoding": item[5],
                    "embedded_program_bytes": extracted_size,
                }
            )
    return result


def image_signature(item: dict[str, Any]) -> tuple[str, tuple[float, ...]]:
    digest = item.get("digest")
    digest_text = digest.hex().upper() if isinstance(digest, bytes) else str(digest)
    bbox = tuple(round(float(value), 2) for value in item.get("bbox", ()))
    return digest_text, bbox


SOURCE_IMAGE_BBOX_TOLERANCE_PT = 0.001


def match_source_image_placements(
    source_items: list[dict[str, Any]],
    candidate_items: list[dict[str, Any]],
    *,
    bbox_tolerance_pt: float = SOURCE_IMAGE_BBOX_TOLERANCE_PT,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Match exact image pixels with a sub-millipoint placement tolerance.

    Serialization can perturb a placement matrix by a few ten-thousandths of
    a point. The image digest remains exact; only the four bbox coordinates use
    the declared tolerance. Candidate placements are consumed once so image
    multiplicity remains part of the preservation contract.
    """

    remaining = set(range(len(candidate_items)))
    matches: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for source_index, source_item in enumerate(source_items):
        source_digest, source_bbox = image_signature(source_item)
        eligible: list[tuple[float, int]] = []
        for candidate_index in remaining:
            candidate_digest, candidate_bbox = image_signature(
                candidate_items[candidate_index]
            )
            if source_digest != candidate_digest or len(source_bbox) != len(
                candidate_bbox
            ):
                continue
            maximum_delta = max(
                abs(float(left) - float(right))
                for left, right in zip(
                    source_item.get("bbox", ()),
                    candidate_items[candidate_index].get("bbox", ()),
                    strict=True,
                )
            )
            if maximum_delta <= bbox_tolerance_pt:
                eligible.append((maximum_delta, candidate_index))
        if not eligible:
            missing.append(source_item)
            continue
        maximum_delta, candidate_index = min(eligible)
        remaining.remove(candidate_index)
        matches.append(
            {
                "source_index": source_index,
                "candidate_index": candidate_index,
                "digest": source_digest,
                "source_bbox": list(source_item.get("bbox", ())),
                "candidate_bbox": list(
                    candidate_items[candidate_index].get("bbox", ())
                ),
                "maximum_bbox_delta_pt": maximum_delta,
            }
        )
    return matches, missing


def restore_uniquely_matched_source_path_operators(
    source_path: Path,
    candidate_path: Path,
    output_path: Path,
    selected_pages: list[int],
) -> dict[str, Any]:
    """Restore source path operators after a safe serializer normalization.

    The source and candidate are compared page-wide, across every content
    stream.  A repair is allowed only when the source and candidate path form a
    one-to-one match under the narrowly supported reserialization equivalence.
    Any duplicate edge is ambiguous and therefore blocks the rebuild.  The
    candidate keeps its graphics state; only the exact source path-construction
    and paint operation range is copied back.
    """

    if output_path.exists():
        raise FileExistsError(f"Refusing to overwrite path-restoration output: {output_path}")
    source = fitz.open(source_path)
    candidate = fitz.open(candidate_path)
    repairs: list[dict[str, Any]] = []
    page_results: list[dict[str, Any]] = []
    unscanned_streams: list[dict[str, Any]] = []
    expected_by_page: dict[int, list[dict[str, Any]]] = {}
    try:
        for candidate_index, source_page_number in enumerate(selected_pages):
            source_page = source[source_page_number - 1]
            candidate_page = candidate[candidate_index]
            source_records: list[dict[str, Any]] = []
            candidate_records: list[dict[str, Any]] = []
            source_data_by_xref: dict[int, bytes] = {}
            candidate_data_by_xref: dict[int, bytes] = {}
            for xref in source_page.get_contents():
                stream_xref = int(xref)
                data = source.xref_stream(stream_xref)
                source_data_by_xref[stream_xref] = data
                try:
                    source_records.extend(
                        parse_content_paths(data, stream_xref=stream_xref)
                    )
                except CompoundComponentError as exc:
                    unscanned_streams.append(
                        {
                            "input_role": "source",
                            "source_page": source_page_number,
                            "stream_xref": stream_xref,
                            "code": exc.code,
                            "message": exc.message,
                            "status": "NOT_SCANNED_REQUIRES_FINAL_DRAWING_QA",
                        }
                    )
            for xref in candidate_page.get_contents():
                stream_xref = int(xref)
                data = candidate.xref_stream(stream_xref)
                candidate_data_by_xref[stream_xref] = data
                try:
                    candidate_records.extend(
                        parse_content_paths(data, stream_xref=stream_xref)
                    )
                except CompoundComponentError as exc:
                    unscanned_streams.append(
                        {
                            "input_role": "candidate",
                            "source_page": source_page_number,
                            "candidate_page": candidate_index + 1,
                            "stream_xref": stream_xref,
                            "code": exc.code,
                            "message": exc.message,
                            "status": "NOT_SCANNED_REQUIRES_FINAL_DRAWING_QA",
                        }
                    )

            edges: list[tuple[int, int]] = []
            eligible_source = [
                (source_record_index, source_record)
                for source_record_index, source_record in enumerate(source_records)
                if any(
                    operator.get("operator") == "y"
                    and len(operator.get("operands") or []) == 4
                    and operator["operands"][:2] == operator["operands"][2:]
                    for operator in source_record["signature"]["path_operators"]
                )
            ]
            candidate_buckets: dict[
                tuple[int, str | None, str | None, tuple[float, ...]],
                list[tuple[int, dict[str, Any]]],
            ] = {}
            for candidate_record_index, candidate_record in enumerate(candidate_records):
                if not any(
                    operator.get("operator") == "l"
                    for operator in candidate_record["signature"]["path_operators"]
                ):
                    continue
                signature = candidate_record["signature"]
                key = (
                    len(signature["path_operators"]),
                    signature.get("clip_operator"),
                    signature.get("paint_operator"),
                    tuple(round(float(value), 6) for value in signature["ctm"]),
                )
                candidate_buckets.setdefault(key, []).append(
                    (candidate_record_index, candidate_record)
                )
            for source_record_index, source_record in eligible_source:
                source_signature = source_record["signature"]
                key = (
                    len(source_signature["path_operators"]),
                    source_signature.get("clip_operator"),
                    source_signature.get("paint_operator"),
                    tuple(round(float(value), 6) for value in source_signature["ctm"]),
                )
                for candidate_record_index, candidate_record in candidate_buckets.get(
                    key, []
                ):
                    if content_path_reserialization_equivalent(
                        source_record["signature"], candidate_record["signature"]
                    ):
                        edges.append((source_record_index, candidate_record_index))
            source_degree = Counter(source_index for source_index, _ in edges)
            candidate_degree = Counter(candidate_index for _, candidate_index in edges)
            ambiguous = [
                {
                    "source_record_index": source_index,
                    "candidate_record_index": candidate_record_index,
                    "source_match_count": source_degree[source_index],
                    "candidate_match_count": candidate_degree[candidate_record_index],
                }
                for source_index, candidate_record_index in edges
                if source_degree[source_index] != 1
                or candidate_degree[candidate_record_index] != 1
            ]
            if ambiguous:
                raise ValueError(
                    "Source path operator restoration is ambiguous: "
                    f"page={source_page_number} matches={ambiguous[:25]}"
                )

            page_repairs: list[dict[str, Any]] = []
            declarations_by_candidate_xref: dict[int, list[dict[str, Any]]] = {}
            for source_record_index, candidate_record_index in edges:
                source_record = source_records[source_record_index]
                candidate_record = candidate_records[candidate_record_index]
                source_xref = int(source_record["stream_xref"])
                candidate_xref = int(candidate_record["stream_xref"])
                declarations_by_candidate_xref.setdefault(candidate_xref, []).append(
                    {
                        "source_stream_xref": source_xref,
                        "source_signature": source_record["signature"],
                        "candidate_signature": candidate_record["signature"],
                    }
                )
                expected_by_page.setdefault(candidate_index + 1, []).append(
                    source_record["signature"]
                )
            for candidate_xref, declarations in declarations_by_candidate_xref.items():
                updated, evidence_items = restore_reserialized_paths_from_source_batch(
                    source_data_by_xref,
                    candidate_data_by_xref[candidate_xref],
                    declarations,
                    candidate_stream_xref=candidate_xref,
                )
                candidate.update_stream(candidate_xref, updated)
                candidate_data_by_xref[candidate_xref] = updated
                for evidence in evidence_items:
                    evidence.update(
                        {
                            "source_page": source_page_number,
                            "candidate_page": candidate_index + 1,
                        }
                    )
                    page_repairs.append(evidence)
                    repairs.append(evidence)
            page_results.append(
                {
                    "source_page": source_page_number,
                    "candidate_page": candidate_index + 1,
                    "source_path_count": len(source_records),
                    "candidate_path_count_before": len(candidate_records),
                    "safe_unique_repair_count": len(page_repairs),
                    "status": "APPLIED_VERIFIED" if page_repairs else "NOT_REQUIRED",
                }
            )
        candidate.save(output_path, garbage=4, deflate=True, clean=False)
    finally:
        candidate.close()
        source.close()

    verified = fitz.open(output_path)
    try:
        for candidate_page_number, expected_signatures in expected_by_page.items():
            records: list[dict[str, Any]] = []
            page = verified[candidate_page_number - 1]
            for xref in page.get_contents():
                records.extend(
                    parse_content_paths(
                        verified.xref_stream(int(xref)), stream_xref=int(xref)
                    )
                )
            construction_counts = Counter(
                content_path_construction_signature_sha256(record["signature"])
                for record in records
            )
            for expected in expected_signatures:
                construction_sha256 = content_path_construction_signature_sha256(
                    expected
                )
                match_count = construction_counts[construction_sha256]
                if match_count != 1:
                    raise ValueError(
                        "Restored source path construction did not survive serialization: "
                        f"candidate_page={candidate_page_number} "
                        f"signature={signature_sha256(expected)} "
                        f"construction={construction_sha256} count={match_count}"
                    )
    finally:
        verified.close()
    return {
        "method": "restore_unique_source_path_operator_sequence_after_safe_reserialization",
        "supported_equivalence": "source_y_control_equals_endpoint_to_candidate_l_endpoint",
        "repair_count": len(repairs),
        "repairs": repairs,
        "pages": page_results,
        "unscanned_streams": unscanned_streams,
        "unscanned_stream_count": len(unscanned_streams),
        "status": "APPLIED_VERIFIED" if repairs else "NOT_REQUIRED",
    }


def restore_missing_source_images(
    source_path: Path,
    candidate_path: Path,
    output_path: Path,
    selected_pages: list[int],
) -> dict[str, Any]:
    """Restore source image placements lost by text-only redaction serialization.

    PyMuPDF can report line-strip image XObjects as present immediately after
    ``apply_redactions(images=NONE)`` yet omit their placements when the file is
    serialized.  ``remove_text_only`` promises that source images are untouched,
    so reopen the serialized proof, restore only missing exact-digest source
    placements, then verify their bbox within the sub-millipoint serializer
    tolerance above.
    """

    source = fitz.open(source_path)
    candidate = fitz.open(candidate_path)
    restorations: list[dict[str, Any]] = []
    try:
        for candidate_index, source_page_number in enumerate(selected_pages):
            source_page = source[source_page_number - 1]
            candidate_page = candidate[candidate_index]
            source_items = source_page.get_image_info(hashes=True, xrefs=True)
            candidate_items = candidate_page.get_image_info(hashes=True, xrefs=True)
            _, missing_items = match_source_image_placements(
                source_items, candidate_items
            )
            for item in missing_items:
                signature = image_signature(item)
                source_xref = int(item.get("xref", 0))
                if source_xref <= 0:
                    raise ValueError(
                        "Cannot restore a missing inline source image without an xref: "
                        f"page={source_page_number} signature={signature}"
                    )
                pixmap = fitz.Pixmap(source, source_xref)
                inserted_xref = candidate_page.insert_image(
                    fitz.Rect(item["bbox"]),
                    pixmap=pixmap,
                    keep_proportion=False,
                    overlay=False,
                )
                pixmap = None
                restorations.append(
                    {
                        "source_page": source_page_number,
                        "candidate_page": candidate_index + 1,
                        "source_xref": source_xref,
                        "inserted_xref": int(inserted_xref),
                        "digest": signature[0],
                        "bbox": list(signature[1]),
                        "status": "RESTORED_FROM_SOURCE_IMAGE_XREF",
                    }
                )
        candidate.save(output_path, garbage=4, deflate=True, clean=False)
    finally:
        candidate.close()
        source.close()

    source = fitz.open(source_path)
    verified = fitz.open(output_path)
    verification: list[dict[str, Any]] = []
    try:
        for candidate_index, source_page_number in enumerate(selected_pages):
            source_items = source[source_page_number - 1].get_image_info(
                hashes=True, xrefs=True
            )
            candidate_items = verified[candidate_index].get_image_info(
                hashes=True, xrefs=True
            )
            matches, missing_items = match_source_image_placements(
                source_items, candidate_items
            )
            missing = [image_signature(item) for item in missing_items]
            verification.append(
                {
                    "source_page": source_page_number,
                    "source_image_count": len(source_items),
                    "candidate_image_count": len(candidate_items),
                    "matched_image_count": len(matches),
                    "bbox_tolerance_pt": SOURCE_IMAGE_BBOX_TOLERANCE_PT,
                    "maximum_matched_bbox_delta_pt": max(
                        (item["maximum_bbox_delta_pt"] for item in matches),
                        default=0.0,
                    ),
                    "missing": [[digest, list(bbox)] for digest, bbox in missing],
                    "status": "PASS" if not missing else "BLOCKED",
                }
            )
            if missing:
                raise ValueError(
                    "Source image restoration verification failed: "
                    f"page={source_page_number} missing={missing}"
                )
    finally:
        verified.close()
        source.close()
    return {
        "method": "restore_missing_exact_source_image_digest_and_bbox_with_declared_tolerance",
        "bbox_tolerance_pt": SOURCE_IMAGE_BBOX_TOLERANCE_PT,
        "restoration_count": len(restorations),
        "restorations": restorations,
        "verification": verification,
        "status": "PASS",
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Copy selected English source pages, mask mapped text only, and refill zh-TW."
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--font", required=True, type=Path)
    parser.add_argument("--bold-font", type=Path, help="Optional embedded bold zh-TW font")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()

    source_path = args.source.resolve()
    manifest_path = args.manifest.resolve()
    output_path = args.output.resolve()
    report_path = args.report.resolve()
    font_path = args.font.resolve()
    bold_font_path = args.bold_font.resolve() if args.bold_font else font_path
    if output_path.exists():
        raise FileExistsError(f"Refusing to overwrite existing output: {output_path}")
    if not font_path.is_file():
        raise FileNotFoundError(font_path)
    if not bold_font_path.is_file():
        raise FileNotFoundError(bold_font_path)

    manifest = read_json(manifest_path)
    issues = validate_manifest(manifest, require_translation=True, require_render=True)
    blocking = [item for item in issues if item.get("severity") == "BLOCKING"]
    if blocking:
        raise ValueError(f"Manifest is blocked: {json.dumps(blocking, ensure_ascii=False)}")
    if Path(manifest["source"]["path"]).resolve() != source_path:
        raise ValueError("Manifest source path does not match the requested source")
    source_hash = sha256_file(source_path)
    if source_hash != manifest["source"]["sha256"]:
        raise ValueError("Source SHA-256 does not match the manifest")

    source_doc = fitz.open(source_path)
    output_doc = fitz.open()
    selected_pages = [int(page) for page in manifest["selected_pages"]]
    source_sizes: dict[int, list[float]] = {}
    for page_number in selected_pages:
        source_page = source_doc[page_number - 1]
        source_sizes[page_number] = [round(source_page.rect.width, 3), round(source_page.rect.height, 3)]
        output_doc.insert_pdf(source_doc, from_page=page_number - 1, to_page=page_number - 1)
    regular_font = fitz.Font(fontfile=str(font_path))
    bold_font = fitz.Font(fontfile=str(bold_font_path))
    replace_segments = [
        segment
        for segment in manifest["segments"]
        if (segment.get("render") or {}).get("action", "replace") not in PRESERVE_ACTIONS
    ]
    regular_requested = any(
        not bool((segment.get("font_style") or {}).get("bold"))
        for segment in replace_segments
    )
    bold_requested = any(
        bool((segment.get("font_style") or {}).get("bold"))
        for segment in replace_segments
    )
    regular_role = font_role_evidence(regular_font, font_path, "regular")
    bold_role = font_role_evidence(bold_font, bold_font_path, "bold")
    font_role_issues: list[dict[str, Any]] = []
    if regular_requested and regular_role["is_bold"]:
        font_role_issues.append(
            {
                "code": "REGULAR_FONT_ROLE_MISMATCH",
                "message": "The regular font path resolves to a bold face",
                "evidence": regular_role,
            }
        )
    if bold_requested and not bold_role["is_bold"]:
        font_role_issues.append(
            {
                "code": "BOLD_FONT_ROLE_MISMATCH",
                "message": "The requested bold font path does not provide a bold face",
                "evidence": bold_role,
            }
        )
    if regular_requested and bold_requested and font_path == bold_font_path:
        font_role_issues.append(
            {
                "code": "SHARED_REGULAR_BOLD_FONT_PATH",
                "message": "Regular and bold roles cannot share one unresolved font instance when both roles are used",
                "evidence": {"path": str(font_path), "font_name": regular_font.name},
            }
        )
    if font_role_issues:
        raise ValueError(f"Font role validation failed: {json.dumps(font_role_issues, ensure_ascii=False)}")
    font_role_validation = {
        "status": "PASS",
        "regular_requested": regular_requested,
        "bold_requested": bold_requested,
        "roles": {"regular": regular_role, "bold": bold_role},
        "issues": [],
    }
    render_results: list[dict[str, Any]] = []
    mask_evidence: dict[str, dict[str, Any]] = {}
    background_adjustment_evidence: list[dict[str, Any]] = []
    vector_rule_adjustment_evidence: list[dict[str, Any]] = []
    vector_path_replacement_evidence: list[dict[str, Any]] = []
    inline_relocation_evidence: list[dict[str, Any]] = []
    superseded_by = {
        str(segment_id): str(owner_id)
        for segment_id, owner_id in (
            manifest.get("post_rebuild_superseded_segments") or {}
        ).items()
    }
    page_map = {source_page: index for index, source_page in enumerate(selected_pages)}
    for source_page_number, output_index in page_map.items():
        page = output_doc[output_index]
        font_name = f"TWProof{output_index + 1}"
        bold_font_name = f"TWProofBold{output_index + 1}"
        page_segments = sorted(
            [item for item in manifest["segments"] if int(item["page"]) == source_page_number],
            key=lambda item: int(item["reading_order"]),
        )
        dependent_background_members = collect_background_adjustments(
            page_segments, dependent=True
        )
        dependent_rule_members = collect_vector_rule_adjustments(
            page_segments, dependent=True
        )
        for member in collect_background_adjustments(page_segments, dependent=False):
            page, evidence = apply_background_adjustment(
                output_doc, page, source_page_number, member
            )
            background_adjustment_evidence.append(evidence)
        rule_members = collect_vector_rule_adjustments(page_segments, dependent=False)
        if rule_members:
            page, evidence = apply_vector_rule_adjustments(
                output_doc,
                page,
                source_doc,
                source_page_number,
                rule_members,
            )
            vector_rule_adjustment_evidence.extend(evidence)
        vector_members = collect_vector_path_replacements(page_segments)
        if vector_members:
            page, evidence = apply_vector_path_replacements(
                output_doc,
                page,
                source_doc,
                source_page_number,
                vector_members,
            )
            vector_path_replacement_evidence.extend(evidence)
        for segment in page_segments:
            render = segment["render"]
            action = render.get("action", "replace")
            segment_id = str(segment["segment_id"])
            if segment_id in superseded_by:
                continue
            if action in PRESERVE_ACTIONS:
                render_results.append(
                    {
                        "segment_id": segment["segment_id"],
                        "page": source_page_number,
                        "semantic_type": segment["semantic_type"],
                        "action": action,
                        "source_bbox": segment["bbox"],
                        "target_bbox": render["target_bbox"],
                        "fit_status": (
                            "PRESERVED_SOURCE_VISUAL"
                            if action == "preserve_source_visual_with_textual_guidance"
                            else "PRESERVED"
                        ),
                        "protected_tokens": segment.get("protected_tokens", []),
                        "visual_id": (segment.get("relationships") or {}).get("visual_id"),
                        "guidance_segment_ids": render.get("guidance_segment_ids", []),
                        "component_contract": segment.get("component_contract"),
                    }
                )
                continue
            if action == "replace_vector_outlined_text":
                continue
            inline_covers = inline_cover_bboxes(segment)
            if inline_covers:
                for cover in inline_covers:
                    page.add_redact_annot(
                        fitz.Rect(cover),
                        fill=None,
                        cross_out=False,
                    )
                mask_evidence[segment_id] = {
                    "mask_bbox": normalize_bbox(union_rects(inline_covers)),
                    "mask_bboxes": inline_covers,
                    "mask_mode": "remove_text_only",
                    "mask_fill": None,
                }
                continue
            mask = expand_bbox(
                render.get("mask_bbox", segment["bbox"]),
                float(render.get("mask_padding_pt", 0.35)),
                page.rect,
            )
            mask_mode = str(render.get("mask_mode", "fill"))
            if mask_mode not in {"fill", "remove_text_only"}:
                raise ValueError(f"Unsupported mask_mode for {segment['segment_id']}: {mask_mode}")
            mask_fill = (
                None
                if mask_mode == "remove_text_only"
                else color(render.get("mask_fill"), (1.0, 1.0, 1.0))
            )
            page.add_redact_annot(
                mask,
                fill=mask_fill,
                cross_out=False,
            )
            mask_evidence[segment["segment_id"]] = {
                "mask_bbox": normalize_bbox(mask),
                "mask_mode": mask_mode,
                "mask_fill": list(mask_fill) if mask_fill is not None else None,
            }
        page.apply_redactions(
            images=fitz.PDF_REDACT_IMAGE_NONE,
            graphics=fitz.PDF_REDACT_LINE_ART_NONE,
            text=fitz.PDF_REDACT_TEXT_REMOVE,
        )
        page.insert_font(fontname=font_name, fontfile=str(font_path))
        if bold_font_path != font_path:
            page.insert_font(fontname=bold_font_name, fontfile=str(bold_font_path))

        page_inline_results = apply_inline_visual_relocations(
            page, source_doc, source_page_number, page_segments
        )
        inline_relocation_evidence.extend(
            {
                "segment_id": segment_id,
                "page": source_page_number,
                **copy.deepcopy(evidence),
            }
            for segment_id, evidence in page_inline_results.items()
        )

        for segment in page_segments:
            render = segment["render"]
            segment_id = str(segment["segment_id"])
            if segment_id in superseded_by:
                render_results.append(
                    {
                        "segment_id": segment_id,
                        "page": source_page_number,
                        "semantic_type": segment["semantic_type"],
                        "action": render.get("action", "replace"),
                        "source_bbox": segment["bbox"],
                        "target_bbox": render["target_bbox"],
                        "container_bbox": render.get("container_bbox"),
                        "requested_font_size_pt": float(render.get("font_size_pt", 0.0)),
                        "used_font_size_pt": None,
                        "source_font_size_pt": float(
                            (segment.get("font_style") or {}).get(
                                "source_font_size_pt", 0.0
                            )
                        ),
                        "font_ratio": None,
                        "line_count": 0,
                        "rendered_lines": [],
                        "fit_status": "SUPERSEDED_BY_FINAL_INLINE_VISUAL_SEQUENCE",
                        "superseded_by_segment_id": superseded_by[segment_id],
                        "prior_live_text_removed_before_final_overlay": True,
                        "component_contract": segment.get("component_contract"),
                    }
                )
                continue
            if render.get("action", "replace") in PRESERVE_ACTIONS:
                continue
            if render.get("draw_box"):
                box = fitz.Rect(render.get("box_bbox", render["target_bbox"]))
                page.draw_rect(
                    box,
                    color=color(render.get("box_color"), (0.1, 0.1, 0.1)),
                    fill=color(render.get("box_fill"), tuple(render.get("mask_fill", (1, 1, 1)))),
                    width=float(render.get("box_width_pt", 0.45)),
                    overlay=True,
                )
            use_bold = bool(segment.get("font_style", {}).get("bold"))
            result = insert_lines(
                page,
                segment,
                bold_font if use_bold else regular_font,
                bold_font_name if use_bold and bold_font_path != font_path else font_name,
                bold_role if use_bold else regular_role,
            )
            if render.get("action", "replace") == "replace":
                live_members = [
                    member
                    for member in (segment.get("component_contract") or {}).get("members")
                    or []
                    if member.get("role") in {"live_text", "translatable_live_text"}
                    and member.get("policy") == "replace_live_text"
                ]
                if len(live_members) > 1:
                    raise ValueError(
                        f"A translated segment must bind one live-text member for dependency routing: {segment['segment_id']}"
                    )
                if live_members:
                    result["text_member_id"] = str(live_members[0]["component_id"])
            if render.get("action") == "replace_vector_outlined_text":
                result["vector_member_id"] = render["vector_member_id"]
                result["path_replacement"] = next(
                    item
                    for item in vector_path_replacement_evidence
                    if item["component_id"] == render["vector_member_id"]
                    and int(item["source_page"]) == source_page_number
                )
                result["mask_bbox"] = None
                result["mask_mode"] = None
                result["mask_fill"] = None
            else:
                result.update(mask_evidence[segment["segment_id"]])
            if segment_id in page_inline_results:
                result["post_rebuild_inline_visual_relocation"] = copy.deepcopy(
                    page_inline_results[segment_id]
                )
                if render.get("display_text_with_visual_semantics"):
                    result["post_rebuild_inline_visual_relocation"][
                        "display_text_with_visual_semantics"
                    ] = render["display_text_with_visual_semantics"]
            render_results.append(result)

        resolved_rule_members: list[dict[str, Any]] = []
        dependency_resolution_by_id: dict[str, dict[str, Any]] = {}
        for member in [*dependent_background_members, *dependent_rule_members]:
            component_id = str(member.get("component_id", ""))
            contract = component_contract_for_member(page_segments, component_id)
            driver_bboxes = candidate_member_bboxes(
                contract,
                render_results,
                candidate_page=None,
                require_adjusted_members=False,
            )
            target_bbox, resolution = resolve_translation_dependent_bbox(
                member, driver_bboxes, page_rect=page.rect
            )
            resolved = copy.deepcopy(member)
            resolved["target_bbox"] = target_bbox
            if member.get("policy") == "adjust_vector_rule":
                resolved["translation_delta_pt"] = resolution["translation_delta_pt"]
                resolved_rule_members.append(resolved)
            else:
                page, evidence = apply_background_adjustment(
                    output_doc, page, source_page_number, resolved
                )
                evidence["dependent_geometry_resolution"] = resolution
                background_adjustment_evidence.append(evidence)
            dependency_resolution_by_id[component_id] = resolution
        if resolved_rule_members:
            page, evidence_items = apply_vector_rule_adjustments(
                output_doc,
                page,
                source_doc,
                source_page_number,
                resolved_rule_members,
            )
            for evidence in evidence_items:
                evidence["dependent_geometry_resolution"] = dependency_resolution_by_id[
                    str(evidence["component_id"])
                ]
            vector_rule_adjustment_evidence.extend(evidence_items)

    source_doc.close()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    render_partial = output_path.with_name(f".{output_path.name}.render-partial-{os.getpid()}")
    path_partial = output_path.with_name(f".{output_path.name}.path-partial-{os.getpid()}")
    image_partial = output_path.with_name(f".{output_path.name}.image-partial-{os.getpid()}")
    catalog_partial = output_path.with_name(f".{output_path.name}.catalog-partial-{os.getpid()}")
    catalog_transfer: dict[str, Any] | None = None
    source_image_restoration: dict[str, Any] | None = None
    source_path_operator_restoration: dict[str, Any] | None = None
    try:
        output_doc.set_metadata(
            {
                "title": f"{manifest['document_id']} zh-TW stable-ID proof",
                "subject": "Candidate proof generated directly from the English source",
                "creator": "pdf-tw-localize segment pipeline",
            }
        )
        output_doc.save(render_partial, garbage=4, deflate=True, clean=True)
        output_doc.close()
        source_path_operator_restoration = restore_uniquely_matched_source_path_operators(
            source_path,
            render_partial,
            path_partial,
            selected_pages,
        )
        source_image_restoration = restore_missing_source_images(
            source_path,
            path_partial,
            image_partial,
            selected_pages,
        )
        source_catalog = catalog_color_evidence(source_path)
        if source_catalog["catalog_output_intents_present"]:
            catalog_transfer = clone_output_intents(
                source_path, image_partial, catalog_partial
            )
            final_partial = catalog_partial
        else:
            candidate_catalog = catalog_color_evidence(image_partial)
            catalog_transfer = {
                "method": "source_has_no_output_intents",
                "source": source_catalog,
                "candidate": candidate_catalog,
                "output_intents_exact": source_catalog == candidate_catalog,
            }
            final_partial = image_partial
        if not catalog_transfer["output_intents_exact"]:
            raise ValueError("Candidate Catalog OutputIntents differ from the source")
        reopened = fitz.open(final_partial)
        output_sizes = [
            [round(page.rect.width, 3), round(page.rect.height, 3)] for page in reopened
        ]
        expected_sizes = [source_sizes[page] for page in selected_pages]
        if output_sizes != expected_sizes:
            reopened.close()
            raise ValueError(f"Output page sizes changed: expected={expected_sizes} actual={output_sizes}")
        fonts = embedded_fonts(reopened)
        reopened.close()
        final_partial.replace(output_path)
    finally:
        for temporary in (render_partial, path_partial, image_partial, catalog_partial):
            if temporary.exists():
                temporary.unlink()

    report = {
        "schema": "pdf-tw-localize/rebuild-report/v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": {"path": str(source_path), "sha256": source_hash},
        "manifest": {"path": str(manifest_path), "sha256": sha256_file(manifest_path)},
        "fonts_requested": {
            "regular": {"path": str(font_path), "sha256": sha256_file(font_path)},
            "bold": {"path": str(bold_font_path), "sha256": sha256_file(bold_font_path)},
        },
        "font_role_validation": font_role_validation,
        "background_adjustments": background_adjustment_evidence,
        "vector_rule_adjustments": vector_rule_adjustment_evidence,
        "vector_path_replacements": vector_path_replacement_evidence,
        "inline_visual_relocations": inline_relocation_evidence,
        "source_path_operator_restoration": source_path_operator_restoration,
        "source_image_restoration": source_image_restoration,
        "output": {
            "path": str(output_path),
            "sha256": sha256_file(output_path),
            "page_count": len(selected_pages),
            "page_sizes": expected_sizes,
        },
        "segment_count": len(manifest["segments"]),
        "rendered_segment_count": sum(item["action"] not in PRESERVE_ACTIONS for item in render_results),
        "preserved_segment_count": sum(item["action"] in PRESERVE_ACTIONS for item in render_results),
        "preserved_visual_segment_count": sum(
            item["action"] == "preserve_source_visual_with_textual_guidance"
            for item in render_results
        ),
        "segments": render_results,
        "fonts": fonts,
        "catalog_color_management": catalog_transfer,
        "machine_qa": "NOT_CHECKED",
        "visual_review": "NOT_CHECKED",
        "user_acceptance": "NOT_CHECKED",
        "status": "RENDERED",
    }
    write_json(report_path, report)
    emit_json(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
