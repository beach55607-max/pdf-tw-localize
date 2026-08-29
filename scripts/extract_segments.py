#!/usr/bin/env python3
"""Extract semantic PDF segments with stable IDs and source-span provenance."""

from __future__ import annotations

import argparse
import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import fitz

from _console import emit_text
from _segment_common import (
    PRESERVE_ACTIONS,
    SCHEMA,
    add_explicit_tokens,
    detect_protected_tokens,
    normalize_bbox,
    parse_pages,
    read_json,
    sha256_file,
    stable_segment_id,
    union_bbox,
    write_json,
)


def line_rotation(line: dict[str, Any]) -> float:
    direction = line.get("dir") or (1.0, 0.0)
    return round(math.degrees(math.atan2(float(direction[1]), float(direction[0]))), 3)


def span_payload(span: dict[str, Any], block_index: int, line_index: int, span_index: int) -> dict[str, Any]:
    return {
        "ref": f"b{block_index}.l{line_index}.s{span_index}",
        "block_index": block_index,
        "line_index": line_index,
        "span_index": span_index,
        "text": span.get("text", ""),
        "bbox": normalize_bbox(span.get("bbox", (0, 0, 0, 0))),
        "origin": [round(float(value), 3) for value in span.get("origin", ())],
        "font": span.get("font", ""),
        "size_pt": round(float(span.get("size", 0.0)), 3),
        "flags": int(span.get("flags", 0)),
        "color": int(span.get("color", 0)),
    }


def page_text_index(page: fitz.Page) -> tuple[dict[tuple[int, int], dict[str, Any]], set[str]]:
    blocks = page.get_text("dict", flags=fitz.TEXTFLAGS_DICT).get("blocks", [])
    index: dict[tuple[int, int], dict[str, Any]] = {}
    all_span_refs: set[str] = set()
    for block_index, block in enumerate(blocks):
        if block.get("type") != 0:
            continue
        for line_index, line in enumerate(block.get("lines", [])):
            spans = [
                span_payload(span, block_index, line_index, span_index)
                for span_index, span in enumerate(line.get("spans", []))
                if span.get("text", "")
            ]
            for span in spans:
                all_span_refs.add(span["ref"])
            index[(block_index, line_index)] = {
                "ref": f"b{block_index}.l{line_index}",
                "block_index": block_index,
                "line_index": line_index,
                "bbox": normalize_bbox(line.get("bbox", (0, 0, 0, 0))),
                "rotation": line_rotation(line),
                "text": "".join(span["text"] for span in spans),
                "spans": spans,
            }
    return index, all_span_refs


def classify(text: str, spans: list[dict[str, Any]]) -> str:
    maximum = max((span["size_pt"] for span in spans), default=0.0)
    flags = [span["flags"] for span in spans]
    if text.lstrip().startswith(("•", "-")):
        return "list"
    if maximum >= 11 or (len(text) < 70 and any(flag & 16 for flag in flags)):
        return "heading"
    return "paragraph"


def build_style(spans: list[dict[str, Any]], source_style: dict[str, Any] | None = None) -> dict[str, Any]:
    if not spans:
        style = dict(source_style or {})
        style.setdefault("primary_font", "VISUAL_ANNOTATION")
        style.setdefault("source_font_size_pt", 8.0)
        style.setdefault("min_source_font_size_pt", style["source_font_size_pt"])
        style.setdefault("max_source_font_size_pt", style["source_font_size_pt"])
        style.setdefault("bold", False)
        style.setdefault("italic", False)
        style.setdefault("source_colors", [])
        return style
    fonts = Counter(span["font"] for span in spans)
    sizes = [float(span["size_pt"]) for span in spans if span["size_pt"] > 0]
    return {
        "primary_font": fonts.most_common(1)[0][0],
        "source_font_size_pt": round(sum(sizes) / len(sizes), 3) if sizes else 0.0,
        "min_source_font_size_pt": round(min(sizes), 3) if sizes else 0.0,
        "max_source_font_size_pt": round(max(sizes), 3) if sizes else 0.0,
        "bold": any(span["flags"] & 16 for span in spans),
        "italic": any(span["flags"] & 2 for span in spans),
        "source_colors": sorted(set(span["color"] for span in spans)),
    }


def collect_refs(
    segment_spec: dict[str, Any], index: dict[tuple[int, int], dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    lines: list[dict[str, Any]] = []
    spans: list[dict[str, Any]] = []
    atomic_refs: list[str] = []
    for ref_spec in segment_spec.get("source_refs", []):
        block_index = int(ref_spec["block"])
        line_index = int(ref_spec["line"])
        line = index.get((block_index, line_index))
        if line is None:
            raise ValueError(f"Unknown source ref b{block_index}.l{line_index}")
        requested_spans = ref_spec.get("spans")
        selected = (
            line["spans"]
            if requested_spans is None
            else [line["spans"][int(span_index)] for span_index in requested_spans]
        )
        if not selected:
            raise ValueError(f"Source ref selects no spans: {ref_spec}")
        lines.append(
            {
                "ref": line["ref"],
                "block_index": block_index,
                "line_index": line_index,
                "bbox": normalize_bbox(union_bbox(span["bbox"] for span in selected)),
                "rotation": line["rotation"],
                "text": "".join(span["text"] for span in selected),
                "selected_span_indexes": [span["span_index"] for span in selected],
            }
        )
        spans.extend(selected)
        atomic_refs.extend(span["ref"] for span in selected)
    return lines, spans, atomic_refs


def segment_from_spec(
    document_id: str,
    page_number: int,
    order: int,
    segment_spec: dict[str, Any],
    index: dict[tuple[int, int], dict[str, Any]],
    context_id: str,
) -> tuple[dict[str, Any], list[str]]:
    lines, spans, atomic_refs = collect_refs(segment_spec, index)
    if "source_text" in segment_spec:
        source_text = str(segment_spec["source_text"])
    else:
        joiner = str(segment_spec.get("line_joiner", " "))
        source_text = joiner.join(line["text"].strip() for line in lines).strip()
    if not source_text:
        raise ValueError(f"Segment {segment_spec.get('key')} has empty source text")
    if "bbox" in segment_spec:
        bbox = normalize_bbox(segment_spec["bbox"])
    elif spans:
        bbox = union_bbox(span["bbox"] for span in spans)
    else:
        raise ValueError(f"Manual segment {segment_spec.get('key')} requires bbox")

    key = str(segment_spec["key"])
    segment_id = stable_segment_id(document_id, page_number, key)
    semantic_type = segment_spec.get("semantic_type") or classify(source_text, spans)
    render = dict(segment_spec.get("render") or {})
    render.setdefault("action", "replace")
    render["target_bbox"] = normalize_bbox(render.get("target_bbox", bbox))
    if render["action"] == "replace_vector_outlined_text":
        render.pop("mask_bbox", None)
        render.pop("mask_mode", None)
        render.pop("mask_padding_pt", None)
    elif render["action"] not in PRESERVE_ACTIONS:
        render.setdefault("mask_bbox", bbox)
        render["mask_bbox"] = normalize_bbox(render["mask_bbox"])
    else:
        render.pop("mask_bbox", None)
        render.pop("mask_mode", None)
        render.pop("mask_padding_pt", None)
    render.setdefault("line_spacing", 1.18)
    render.setdefault("align", "left")
    render.setdefault("valign", "top")

    explicit_tokens = segment_spec.get("protected_tokens") or []
    protected = add_explicit_tokens(detect_protected_tokens(source_text), source_text, explicit_tokens)
    rotation = float(segment_spec.get("rotation", lines[0]["rotation"] if lines else 0.0))
    relationships = dict(segment_spec.get("relationships") or {})
    relationships.setdefault("context_id", context_id)
    return (
        {
            "page": page_number,
            "segment_id": segment_id,
            "segment_key": key,
            "semantic_type": semantic_type,
            "bbox": bbox,
            "source_lines": lines,
            "source_spans": spans,
            "font_style": build_style(spans, segment_spec.get("source_style")),
            "rotation": rotation,
            "reading_order": int(segment_spec.get("reading_order", order)),
            "protected_tokens": protected,
            "source_text": source_text,
            "zh_TW": "",
            "status": "EXTRACTED",
            "extraction_method": segment_spec.get(
                "extraction_method", "source_spans" if spans else "visual_annotation"
            ),
            "relationships": relationships,
            **(
                {"component_contract": dict(segment_spec["component_contract"])}
                if "component_contract" in segment_spec
                else {}
            ),
            "semantic_bindings": list(segment_spec.get("semantic_bindings") or []),
            "translation_assertions": [],
            "render": render,
            "notes": segment_spec.get("notes", ""),
        },
        atomic_refs,
    )


def auto_page_spec(page_number: int, index: dict[tuple[int, int], dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[int, list[int]] = {}
    for block_index, line_index in index:
        grouped.setdefault(block_index, []).append(line_index)
    segments = []
    for order, block_index in enumerate(sorted(grouped), start=1):
        segments.append(
            {
                "key": f"auto-block-{block_index:03d}",
                "source_refs": [
                    {"block": block_index, "line": line_index}
                    for line_index in sorted(grouped[block_index])
                ],
                "reading_order": order,
                "notes": "Automatic block grouping; refine structured, table, UI, and image-text pages with a layout spec.",
            }
        )
    return {
        "context": {
            "purpose": "AUTO_NOT_CHECKED",
            "heading_hierarchy": [],
            "neighboring_context": [],
            "table_context": [],
            "condition_pairs": [],
            "ui_state": [],
            "image_text_inventory_status": "NOT_CHECKED",
        },
        "segments": segments,
        "ignored_source_refs": [],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract stable-ID semantic segments from selected PDF pages."
    )
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--pages", required=True, help="1-based pages, for example 14 or 2,5-7")
    parser.add_argument("--document-id", required=True)
    parser.add_argument("--layout-spec", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    source_path = args.pdf.resolve()
    doc = fitz.open(source_path)
    selected_pages = parse_pages(args.pages, doc.page_count)
    layout_spec = read_json(args.layout_spec) if args.layout_spec else None
    if layout_spec and layout_spec.get("schema") != "pdf-tw-localize/layout-spec/v1":
        raise ValueError("Unsupported layout spec schema")
    if layout_spec and layout_spec.get("document_id") not in {None, args.document_id}:
        raise ValueError("layout spec document_id does not match --document-id")

    segments: list[dict[str, Any]] = []
    mappings: list[dict[str, Any]] = []
    page_contexts: list[dict[str, Any]] = []
    all_source_refs: set[str] = set()
    mapped_refs: list[str] = []
    ignored_refs: set[str] = set()
    manual_image_text_count = 0

    for page_number in selected_pages:
        page = doc[page_number - 1]
        index, page_source_refs = page_text_index(page)
        all_source_refs.update(f"p{page_number:04d}.{ref}" for ref in page_source_refs)
        page_spec = (
            (layout_spec.get("pages") or {}).get(str(page_number))
            if layout_spec
            else None
        ) or auto_page_spec(page_number, index)
        context_id = f"{args.document_id}.p{page_number:04d}.context"
        context = dict(page_spec.get("context") or {})
        context.update(
            {
                "context_id": context_id,
                "page": page_number,
                "page_bbox": normalize_bbox(page.rect),
                "rotation": page.rotation,
            }
        )
        page_contexts.append(context)
        for ignored in page_spec.get("ignored_source_refs") or []:
            ref = ignored["ref"] if isinstance(ignored, dict) else str(ignored)
            ignored_refs.add(f"p{page_number:04d}.{ref}")
        for order, segment_spec in enumerate(page_spec.get("segments") or [], start=1):
            segment, refs = segment_from_spec(
                args.document_id, page_number, order, segment_spec, index, context_id
            )
            segments.append(segment)
            mapped_refs.extend(f"p{page_number:04d}.{ref}" for ref in refs)
            if segment["semantic_type"] == "image-text":
                manual_image_text_count += 1
            mappings.append(
                {
                    "operation": "one_to_one",
                    "source_ids": [segment["segment_id"]],
                    "target_ids": [segment["segment_id"]],
                    "reason": "Stable-ID translation preserves the semantic segment boundary.",
                }
            )

    doc.close()
    duplicate_refs = sorted(ref for ref, count in Counter(mapped_refs).items() if count > 1)
    mapped_set = set(mapped_refs)
    unmapped_refs = sorted(all_source_refs - mapped_set - ignored_refs)
    unknown_mapped_refs = sorted(mapped_set - all_source_refs)
    if unknown_mapped_refs:
        raise ValueError(f"Layout spec mapped unknown source refs: {unknown_mapped_refs}")
    if unknown_ignored := sorted(ignored_refs - all_source_refs):
        raise ValueError(f"Layout spec ignored unknown source refs: {unknown_ignored}")

    manifest = {
        "schema": SCHEMA,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "document_id": args.document_id,
        "source": {
            "path": str(source_path),
            "sha256": sha256_file(source_path),
            "size": source_path.stat().st_size,
            "page_count": len(fitz.open(source_path)),
        },
        "selected_pages": selected_pages,
        "page_contexts": page_contexts,
        "segments": sorted(segments, key=lambda item: (item["page"], item["reading_order"])),
        "mappings": mappings,
        "coverage": {
            "source_span_ref_count": len(all_source_refs),
            "mapped_source_span_ref_count": len(mapped_set),
            "ignored_source_span_ref_count": len(ignored_refs),
            "manual_image_text_segment_count": manual_image_text_count,
            "unmapped_source_refs": unmapped_refs,
            "duplicate_source_refs": duplicate_refs,
        },
        "translation_contract": {
            "model_call_by_script": False,
            "required_context_packet": True,
            "stable_id_round_trip": True,
            "monolingual_output": "zh-TW",
        },
        "status": "EXTRACTED",
        "machine_qa": "NOT_CHECKED",
        "semantic_qa": "NOT_CHECKED",
        "visual_review": "NOT_CHECKED",
        "user_acceptance": "NOT_CHECKED",
    }
    write_json(args.output, manifest)
    emit_text(
        f"EXTRACTED segments={len(segments)} mapped_refs={len(mapped_set)} "
        f"unmapped_refs={len(unmapped_refs)} duplicate_refs={len(duplicate_refs)} output={args.output}"
    )
    return 0 if not unmapped_refs and not duplicate_refs else 2


if __name__ == "__main__":
    raise SystemExit(main())
