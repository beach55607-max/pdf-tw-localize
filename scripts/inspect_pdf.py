#!/usr/bin/env python3
"""Inspect PDF page structure and suggest conservative localization routes."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from _console import emit_json, emit_text

SCHEMA = "pdf-tw-localize/page-inspection/v1"
ENGLISH_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9'./:+_-]{1,}")


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


def rect_area(rect: Any) -> float:
    return max(0.0, float(rect.width)) * max(0.0, float(rect.height))


def estimate_image_area(page: Any) -> tuple[int, float]:
    page_area = max(1.0, rect_area(page.rect))
    images = page.get_images(full=True)
    rectangles = []
    for image in images:
        xref = image[0]
        try:
            rectangles.extend(page.get_image_rects(xref))
        except Exception:
            continue
    summed = sum(rect_area(rect & page.rect) for rect in rectangles)
    return len(images), min(1.0, summed / page_area)


def text_blocks(page: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for block in page.get_text("dict", sort=True).get("blocks", []):
        if block.get("type") != 0:
            continue
        text = "\n".join(
            "".join(span.get("text", "") for span in line.get("spans", []))
            for line in block.get("lines", [])
        ).strip()
        if not text:
            continue
        records.append({"text": text, "bbox": block.get("bbox", [0, 0, 0, 0])})
    return records


def estimate_columns(blocks: list[dict[str, Any]], page_width: float) -> dict[str, Any]:
    if page_width <= 0:
        return {"anchors": [], "count": 0, "cross_column_pairs": 0}
    useful = [block for block in blocks if len(block["text"].strip()) >= 8]
    anchors = Counter(round(float(block["bbox"][0]) / page_width, 1) for block in useful)
    stable = sorted(anchor for anchor, count in anchors.items() if count >= 2)
    cross_pairs = 0
    for index, left in enumerate(useful):
        left_box = left["bbox"]
        for right in useful[index + 1 :]:
            right_box = right["bbox"]
            vertical_overlap = min(left_box[3], right_box[3]) - max(left_box[1], right_box[1])
            min_height = min(left_box[3] - left_box[1], right_box[3] - right_box[1])
            separated = min(left_box[2], right_box[2]) < max(left_box[0], right_box[0])
            if min_height > 0 and vertical_overlap / min_height >= 0.4 and separated:
                cross_pairs += 1
    return {"anchors": stable, "count": len(stable), "cross_column_pairs": cross_pairs}


def choose_route(metrics: dict[str, Any]) -> tuple[str, list[str]]:
    reasons: list[str] = []
    chars = metrics["text_characters"]
    image_ratio = metrics["image_area_ratio"]
    images = metrics["image_count"]
    drawings = metrics["drawing_count"]
    blocks = metrics["text_block_count"]
    small_ratio = metrics["small_text_block_ratio"]
    columns = metrics["column_signals"]["count"]
    cross_pairs = metrics["column_signals"]["cross_column_pairs"]

    if chars < 20 and image_ratio >= 0.65:
        return "scanned-rebuild", [
            "Very little extractable text and most of the page is raster imagery."
        ]
    if chars < 20 and images == 0:
        return "manual-review", [
            "Very little extractable text and no dominant image was detected."
        ]
    if image_ratio >= 0.35 or (images >= 2 and chars < 180):
        reasons.append("Image coverage indicates possible baked-in labels or screenshots.")
        if drawings >= 20 or blocks >= 12:
            reasons.append("Mixed vector/text structure also requires region-level reconstruction.")
        return "image-overlay", reasons
    if drawings >= 25:
        reasons.append("Many vector drawings or rules suggest tables, boxes, or diagrams.")
    if blocks >= 18 and small_ratio >= 0.4:
        reasons.append("Many short text blocks suggest labels, bullets, or table cells.")
    elif blocks >= 6 and small_ratio >= 0.5:
        reasons.append("Short-block density suggests callouts, compact labels, or segmented instructions.")
    if columns >= 3 or cross_pairs >= 4:
        reasons.append("Multiple column signals indicate non-linear reading order.")
    if reasons:
        return "structured", reasons
    return "prose", ["Low-complexity extractable text with no strong structured-page signal."]


def inspect_page(page: Any, page_number: int) -> dict[str, Any]:
    blocks = text_blocks(page)
    text = "\n".join(block["text"] for block in blocks)
    nonspace_chars = len(re.sub(r"\s+", "", text))
    tokens = ENGLISH_TOKEN.findall(text)
    images, image_ratio = estimate_image_area(page)
    try:
        drawing_count = len(page.get_drawings())
    except Exception:
        drawing_count = 0
    small_blocks = sum(1 for block in blocks if len(block["text"].strip()) < 40)
    columns = estimate_columns(blocks, float(page.rect.width))
    metrics: dict[str, Any] = {
        "text_characters": nonspace_chars,
        "english_token_count": len(tokens),
        "english_token_examples": sorted(set(tokens), key=str.casefold)[:30],
        "text_block_count": len(blocks),
        "small_text_block_ratio": round(small_blocks / max(1, len(blocks)), 3),
        "image_count": images,
        "image_area_ratio": round(image_ratio, 3),
        "drawing_count": drawing_count,
        "column_signals": columns,
    }
    route, reasons = choose_route(metrics)
    return {
        "page": page_number,
        "width_pt": round(float(page.rect.width), 3),
        "height_pt": round(float(page.rect.height), 3),
        "rotation": int(page.rotation),
        "metrics": metrics,
        "suggested_route": route,
        "route_reasons": reasons,
        "review_status": "NOT_CHECKED",
        "review_notes": "",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect each PDF page and suggest a conservative localization route."
    )
    parser.add_argument("pdf", type=Path, help="PDF to inspect.")
    parser.add_argument("--pages", help="1-based pages, for example 1,3-5. Default: all.")
    parser.add_argument("--output", type=Path, help="Write JSON report to this path.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not args.pdf.is_file():
        emit_text(f"BLOCKED: input does not exist: {args.pdf}", stream=sys.stderr)
        return 2
    try:
        import fitz
    except ImportError:
        emit_text("BLOCKED: PyMuPDF is required.", stream=sys.stderr)
        return 2

    try:
        document = fitz.open(args.pdf)
        selected = parse_pages(args.pages, document.page_count)
        pages = [inspect_page(document[index], index + 1) for index in selected]
        route_counts = Counter(page["suggested_route"] for page in pages)
        report = {
            "schema": SCHEMA,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "pdf": {
                "path": str(args.pdf.resolve()),
                "sha256": sha256_file(args.pdf),
                "page_count": document.page_count,
                "selected_page_count": len(selected),
                "encrypted": bool(document.needs_pass),
            },
            "route_counts": dict(sorted(route_counts.items())),
            "pages": pages,
            "status": "NEEDS_REVIEW",
            "status_reason": "Route suggestions require page-level human confirmation.",
        }
        document.close()
    except (OSError, RuntimeError, ValueError) as exc:
        emit_text(f"BLOCKED: {exc}", stream=sys.stderr)
        return 2

    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    emit_json(report)
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
