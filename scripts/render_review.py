#!/usr/bin/env python3
"""Render hash-bound SOURCE / optional BASELINE / CANDIDATE review evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from _console import emit_json, emit_text

SCHEMA = "pdf-tw-localize/render-review/v2"


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


def render_page(page: Any, output: Path, dpi: int, fitz_module: Any) -> None:
    scale = dpi / 72.0
    pixmap = page.get_pixmap(matrix=fitz_module.Matrix(scale, scale), alpha=False)
    pixmap.save(output)


def file_evidence(path: Path) -> dict[str, str]:
    return {"path": str(path.resolve()), "sha256": sha256_file(path)}


def compare_image(
    panels: list[tuple[str, Path]],
    output: Path,
    page_number: int,
) -> None:
    from PIL import Image, ImageDraw

    loaded: list[tuple[str, Any]] = []
    try:
        for label, path in panels:
            loaded.append((label, Image.open(path).convert("RGB")))
        header = 72
        gap = 12
        width = sum(image.width for _, image in loaded) + gap * (len(loaded) - 1)
        height = max(image.height for _, image in loaded) + header
        canvas = Image.new("RGB", (width, height), "white")
        draw = ImageDraw.Draw(canvas)
        x = 0
        for label, image in loaded:
            draw.rectangle((x, 0, x + image.width, header), fill="#E5E7EB")
            draw.text((x + 16, 12), f"PAGE {page_number} | {label}", fill="black")
            draw.text((x + 16, 36), f"{image.width} x {image.height} px", fill="#374151")
            canvas.paste(image, (x, header))
            x += image.width + gap
        canvas.save(output, format="PNG", optimize=True)
    finally:
        for _, image in loaded:
            image.close()


def contact_sheets(
    compare_paths: list[tuple[int, Path]],
    output_dir: Path,
    batch_size: int,
    columns: int,
) -> list[Path]:
    from PIL import Image, ImageDraw

    results: list[Path] = []
    thumb_width = 720
    label_height = 28
    gap = 14
    for batch_index in range(0, len(compare_paths), batch_size):
        batch = compare_paths[batch_index : batch_index + batch_size]
        thumbs: list[tuple[int, Any]] = []
        max_height = 0
        for page_number, path in batch:
            with Image.open(path) as raw:
                image = raw.convert("RGB")
                ratio = thumb_width / image.width
                thumb = image.resize(
                    (thumb_width, max(1, round(image.height * ratio))),
                    Image.Resampling.LANCZOS,
                )
            thumbs.append((page_number, thumb))
            max_height = max(max_height, thumb.height)
        rows = math.ceil(len(thumbs) / columns)
        sheet_width = columns * thumb_width + (columns + 1) * gap
        sheet_height = rows * (max_height + label_height) + (rows + 1) * gap
        sheet = Image.new("RGB", (sheet_width, sheet_height), "#E5E7EB")
        draw = ImageDraw.Draw(sheet)
        for index, (page_number, thumb) in enumerate(thumbs):
            row, column = divmod(index, columns)
            x = gap + column * (thumb_width + gap)
            y = gap + row * (max_height + label_height + gap)
            draw.text((x, y), f"Page {page_number} - navigation only", fill="black")
            sheet.paste(thumb, (x, y + label_height))
            thumb.close()
        first_page = batch[0][0]
        last_page = batch[-1][0]
        output = output_dir / f"contact_p{first_page:03d}-p{last_page:03d}.png"
        sheet.save(output, format="PNG", optimize=True)
        sheet.close()
        results.append(output)
    return results


def pdf_identity(path: Path, page_count: int) -> dict[str, Any]:
    return {
        "path": str(path.resolve()),
        "sha256": sha256_file(path),
        "page_count": page_count,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render SOURCE / optional BASELINE / CANDIDATE comparisons and an all-NOT_CHECKED v2 review manifest."
    )
    parser.add_argument("source", type=Path, help="Original source PDF.")
    parser.add_argument("candidate", type=Path, help="Localized candidate PDF.")
    parser.add_argument("--baseline", type=Path, help="Optional prior practical-success or accepted baseline PDF.")
    parser.add_argument("--out-dir", type=Path, required=True, help="New empty directory for review artifacts.")
    parser.add_argument("--pages", help="1-based pages, for example 1,3-5. Default: all.")
    parser.add_argument("--dpi", type=int, default=300, help="Render resolution, default 300.")
    parser.add_argument("--contact-batch-size", type=int, default=8, help="Pages per navigation contact sheet.")
    parser.add_argument("--contact-columns", type=int, default=2, help="Navigation contact sheet columns.")
    parser.add_argument("--manifest", type=Path, help="Manifest path. Defaults to OUT-DIR/review_manifest.json.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.dpi < 72 or args.dpi > 600:
        emit_text("BLOCKED: dpi must be between 72 and 600.", stream=sys.stderr)
        return 2
    if args.contact_batch_size < 1 or args.contact_columns < 1:
        emit_text("BLOCKED: contact sheet sizes must be positive.", stream=sys.stderr)
        return 2
    input_paths = [args.source, args.candidate]
    if args.baseline:
        input_paths.append(args.baseline)
    for path in input_paths:
        if not path.is_file():
            emit_text(f"BLOCKED: input does not exist: {path}", stream=sys.stderr)
            return 2

    manifest_path = args.manifest or args.out_dir / "review_manifest.json"
    if manifest_path.exists():
        emit_text(f"BLOCKED: refusing to overwrite a review manifest: {manifest_path}", stream=sys.stderr)
        return 2
    if args.out_dir.exists() and any(args.out_dir.iterdir()):
        emit_text(f"BLOCKED: review output directory must be empty: {args.out_dir}", stream=sys.stderr)
        return 2

    try:
        import fitz
        from PIL import Image

        _ = Image.Resampling.LANCZOS
    except ImportError:
        emit_text("BLOCKED: PyMuPDF and Pillow are required.", stream=sys.stderr)
        return 2

    args.out_dir.mkdir(parents=True, exist_ok=True)
    source_dir = args.out_dir / "source"
    baseline_dir = args.out_dir / "baseline"
    candidate_dir = args.out_dir / "candidate"
    compare_dir = args.out_dir / "compare"
    contact_dir = args.out_dir / "contact"
    directories = [source_dir, candidate_dir, compare_dir, contact_dir]
    if args.baseline:
        directories.append(baseline_dir)
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)

    source_doc = None
    baseline_doc = None
    candidate_doc = None
    try:
        source_doc = fitz.open(args.source)
        candidate_doc = fitz.open(args.candidate)
        if source_doc.page_count != candidate_doc.page_count:
            raise ValueError(
                f"source/candidate page count mismatch: {source_doc.page_count} != {candidate_doc.page_count}"
            )
        if args.baseline:
            baseline_doc = fitz.open(args.baseline)
            if source_doc.page_count != baseline_doc.page_count:
                raise ValueError(
                    f"source/baseline page count mismatch: {source_doc.page_count} != {baseline_doc.page_count}"
                )
        selected = parse_pages(args.pages, source_doc.page_count)
        records: list[dict[str, Any]] = []
        compare_paths: list[tuple[int, Path]] = []

        for index in selected:
            page_number = index + 1
            source_image = source_dir / f"p{page_number:03d}.png"
            candidate_image = candidate_dir / f"p{page_number:03d}.png"
            comparison = compare_dir / f"p{page_number:03d}_compare.png"
            render_page(source_doc[index], source_image, args.dpi, fitz)
            render_page(candidate_doc[index], candidate_image, args.dpi, fitz)
            panels: list[tuple[str, Path]] = [("SOURCE", source_image)]
            baseline_image = None
            if baseline_doc is not None:
                baseline_image = baseline_dir / f"p{page_number:03d}.png"
                render_page(baseline_doc[index], baseline_image, args.dpi, fitz)
                panels.append(("BASELINE", baseline_image))
            panels.append(("CANDIDATE", candidate_image))
            compare_image(panels, comparison, page_number)
            compare_paths.append((page_number, comparison))

            images: dict[str, Any] = {
                "source": file_evidence(source_image),
                "candidate": file_evidence(candidate_image),
            }
            if baseline_image is not None:
                images["baseline"] = file_evidence(baseline_image)
            records.append(
                {
                    "page": page_number,
                    "images": images,
                    "comparison": file_evidence(comparison),
                    "review": {
                        "visual_status": "NOT_CHECKED",
                        "image_text_status": "NOT_CHECKED",
                        "geometry_status": "NOT_CHECKED",
                        "legibility_status": "NOT_CHECKED",
                        "reviewer": None,
                        "reviewed_at": None,
                        "page_reference": None,
                        "review_dpi": None,
                        "reviewed_compare_sha256": None,
                        "notes": "",
                    },
                }
            )

        sheets = contact_sheets(compare_paths, contact_dir, args.contact_batch_size, args.contact_columns)
        manifest = {
            "schema": SCHEMA,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "status": "RENDERED",
            "source": pdf_identity(args.source, source_doc.page_count),
            "baseline": pdf_identity(args.baseline, baseline_doc.page_count) if baseline_doc is not None else None,
            "candidate": pdf_identity(args.candidate, candidate_doc.page_count),
            "render": {
                "dpi": args.dpi,
                "selected_pages": [index + 1 for index in selected],
                "all_document_pages_rendered": len(selected) == source_doc.page_count,
                "comparison_mode": "SOURCE_BASELINE_CANDIDATE" if baseline_doc is not None else "SOURCE_CANDIDATE",
                "contact_sheets": [file_evidence(path) for path in sheets],
                "contact_sheet_role": "NAVIGATION_ONLY",
            },
            "states": {
                "machine_qa": "NOT_RUN",
                "visual_review": "NOT_CHECKED",
                "user_acceptance": "NOT_CHECKED",
            },
            "pages": records,
            "review_instructions": [
                "Open every comparison image individually at readable zoom; contact sheets are navigation only.",
                "Edit one page review record at a time after viewing that page.",
                "Bind reviewed_compare_sha256 to the current comparison.sha256.",
                "Use a unique timezone-aware reviewed_at and a distinct page-specific note.",
                "Do not use a script or loop to fill review status, reviewer, reviewed_at, hashes, or notes.",
                "Run validate_visual_review.py after manual review.",
            ],
        }
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (OSError, RuntimeError, ValueError) as exc:
        emit_text(f"BLOCKED: {exc}", stream=sys.stderr)
        return 2
    finally:
        for document in (source_doc, baseline_doc, candidate_doc):
            if document is not None:
                document.close()

    emit_json(manifest)
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
