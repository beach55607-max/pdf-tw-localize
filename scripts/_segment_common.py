#!/usr/bin/env python3
"""Shared deterministic helpers for the segment localization pipeline."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from _compound_components import (
    COMPOUND_COMPONENT_SCHEMA,
    validate_compound_component_contract,
)


SCHEMA = "pdf-tw-localize/segment-manifest/v1"
TEXT_ALIGNMENT_SCHEMA = "pdf-tw-localize/text-alignment/v1"
TABLE_CELL_PHRASE_SCHEMA = "pdf-tw-localize/table-cell-phrase/v1"
ALLOWED_SEMANTIC_TYPES = {
    "heading",
    "paragraph",
    "list",
    "table-cell",
    "UI",
    "image-text",
    "caption",
    "warning",
    "footer",
    "protected",
}
ALLOWED_STATUS = {"EXTRACTED", "TRANSLATED", "VALIDATED", "RENDERED"}
PRESERVE_ACTIONS = {"preserve", "preserve_source_visual_with_textual_guidance"}
ALLOWED_RENDER_ACTIONS = {
    "replace",
    "replace_vector_outlined_text",
    *PRESERVE_ACTIONS,
}
VERIFIED_CONTEXT_STATUS = "VERIFIED_SOURCE_CONTEXT"
ALLOWED_CUE_KINDS = {
    "parameter",
    "role",
    "mode",
    "condition",
    "comparison",
    "consequence",
    "general",
}
ALLOWED_CUE_SCOPES = {"segment", "binding_phrase"}
ALLOWED_CLARIFICATION_MODES = {
    "none",
    "source_derived_inline",
    "source_derived_note",
}
ALLOWED_VISUAL_KINDS = {"raster_image", "vector_component"}
ALLOWED_COMPONENT_ROLES = {
    "live_text",
    "translatable_live_text",
    "translatable_vector_outlined_text",
    "dingbat_marker",
    "symbol",
    "vector_rule",
    "frame",
    "background",
    "icon",
    "neighbor_container",
    "vector_outlined_text",
    "raster_image",
}
ALLOWED_COMPONENT_POLICIES = {
    "replace_live_text",
    "replace_vector_outlined_text",
    "preserve",
    "preserve_complete_visual",
    "adjust_background",
    "adjust_vector_rule",
}
ALLOWED_SEGMENT_COMPONENT_ROLES = {
    "live_text",
    "translatable_live_text",
    "translatable_vector_outlined_text",
    "preserved_component",
    "complete_visual",
}
ALLOWED_MASK_POLICIES = {"source_text_spans_only", "none"}
ALLOWED_COMPONENT_RELATIONS = {"member_of", "visual_annotation_of", "guidance_for"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def parse_pages(spec: str, page_count: int) -> list[int]:
    pages: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            start, end = int(start_text), int(end_text)
            if end < start:
                raise ValueError(f"Invalid page range: {part}")
            pages.update(range(start, end + 1))
        else:
            pages.add(int(part))
    invalid = sorted(page for page in pages if page < 1 or page > page_count)
    if invalid:
        raise ValueError(f"Pages outside document range 1-{page_count}: {invalid}")
    if not pages:
        raise ValueError("No pages selected")
    return sorted(pages)


def normalize_bbox(values: Iterable[float]) -> list[float]:
    result = [round(float(value), 3) for value in values]
    if len(result) != 4:
        raise ValueError(f"Expected four bbox values, got {result}")
    return result


def union_bbox(boxes: Iterable[Iterable[float]]) -> list[float]:
    normalized = [normalize_bbox(box) for box in boxes]
    if not normalized:
        raise ValueError("Cannot create a union bbox without boxes")
    return [
        min(box[0] for box in normalized),
        min(box[1] for box in normalized),
        max(box[2] for box in normalized),
        max(box[3] for box in normalized),
    ]


def bbox_inside(inner: Iterable[float], outer: Iterable[float], tolerance: float = 0.1) -> bool:
    ix0, iy0, ix1, iy1 = normalize_bbox(inner)
    ox0, oy0, ox1, oy1 = normalize_bbox(outer)
    return (
        ix0 >= ox0 - tolerance
        and iy0 >= oy0 - tolerance
        and ix1 <= ox1 + tolerance
        and iy1 <= oy1 + tolerance
        and ix1 > ix0
        and iy1 > iy0
    )


def bbox_intersection_area(first: Iterable[float], second: Iterable[float]) -> float:
    """Return geometric intersection area for bboxes known to share a page."""

    ax0, ay0, ax1, ay1 = normalize_bbox(first)
    bx0, by0, bx1, by1 = normalize_bbox(second)
    return max(0.0, min(ax1, bx1) - max(ax0, bx0)) * max(
        0.0, min(ay1, by1) - max(ay0, by0)
    )


def page_scoped_intersection_area(
    first_page: int,
    first_bbox: Iterable[float],
    second_page: int,
    second_bbox: Iterable[float],
) -> float:
    """Return zero when bboxes belong to different source pages.

    Page identity is checked before any coordinate comparison so identically
    placed objects on different pages cannot become false intersections.
    """

    if int(first_page) != int(second_page):
        return 0.0
    return bbox_intersection_area(first_bbox, second_bbox)


def stable_segment_id(document_id: str, page: int, key: str) -> str:
    safe_document = re.sub(r"[^a-z0-9_-]+", "-", document_id.lower()).strip("-")
    safe_key = re.sub(r"[^a-z0-9_-]+", "-", key.lower()).strip("-")
    if not safe_document or not safe_key:
        raise ValueError(f"Invalid stable ID parts: document_id={document_id!r}, key={key!r}")
    return f"{safe_document}.p{page:04d}.{safe_key}"


def _covered(start: int, end: int, occupied: list[tuple[int, int]]) -> bool:
    return any(start < other_end and end > other_start for other_start, other_end in occupied)


def detect_protected_tokens(text: str) -> list[dict[str, Any]]:
    """Return exact protected substrings without overlapping matches."""
    patterns = (
        ("temperature", re.compile(r"(?<!\w)\d+(?:\.\d+)?\s*°[CF](?!\w)")),
        (
            "model_or_code",
            re.compile(r"\b(?:PM\d+(?:\.\d+)?|[A-Z]{2,}[A-Z0-9._/-]*\d[A-Z0-9._/-]*)\b"),
        ),
        ("measurement", re.compile(r"(?<!\w)\d+(?:\.\d+)?\s*(?:kW|W|V|Hz|Pa|mm|cm|kg|%)(?!\w)")),
        ("number", re.compile(r"(?<![A-Za-z0-9])\d+(?:\.\d+)?(?![A-Za-z0-9])")),
    )
    occupied: list[tuple[int, int]] = []
    found: list[tuple[int, str, str, str | None]] = []
    for kind, pattern in patterns:
        for match in pattern.finditer(text):
            if _covered(match.start(), match.end(), occupied):
                continue
            occupied.append((match.start(), match.end()))
            found.append((match.start(), match.group(0), kind, None))
    number_words = {
        "zero": "0",
        "one": "1",
        "two": "2",
        "three": "3",
        "four": "4",
        "five": "5",
        "six": "6",
        "seven": "7",
        "eight": "8",
        "nine": "9",
        "ten": "10",
        "eleven": "11",
        "twelve": "12",
    }
    for match in re.finditer(r"\b(?:" + "|".join(number_words) + r")\b", text, flags=re.IGNORECASE):
        if _covered(match.start(), match.end(), occupied):
            continue
        occupied.append((match.start(), match.end()))
        found.append((match.start(), match.group(0), "number_word", number_words[match.group(0).lower()]))
    counts = Counter(token for _, token, _, _ in found)
    first_kind: dict[str, str] = {}
    first_position: dict[str, int] = {}
    target_token: dict[str, str | None] = {}
    for position, token, kind, target in found:
        first_kind.setdefault(token, kind)
        first_position.setdefault(token, position)
        target_token.setdefault(token, target)
    result = []
    for token in sorted(counts, key=lambda value: first_position[value]):
        item = {"token": token, "kind": first_kind[token], "source_count": counts[token]}
        if target_token[token] is not None:
            item["target_token"] = target_token[token]
        result.append(item)
    return result


def add_explicit_tokens(
    detected: list[dict[str, Any]], source_text: str, explicit: Iterable[Any]
) -> list[dict[str, Any]]:
    by_token = {item["token"]: dict(item) for item in detected}
    for item in explicit:
        if isinstance(item, str):
            token, kind = item, "explicit"
        else:
            token = str(item["token"])
            kind = str(item.get("kind", "explicit"))
        count = source_text.count(token)
        if count < 1:
            raise ValueError(f"Explicit protected token is absent from source_text: {token!r}")
        by_token[token] = {"token": token, "kind": kind, "source_count": count}
    return list(by_token.values())


def issue(
    severity: str,
    code: str,
    message: str,
    *,
    segment_id: str | None = None,
    evidence: Any | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"severity": severity, "code": code, "message": message}
    if segment_id is not None:
        payload["segment_id"] = segment_id
    if evidence is not None:
        payload["evidence"] = evidence
    return payload


def protected_target_map(segment: dict[str, Any]) -> dict[str, str]:
    """Map exact source protected tokens to their required target forms."""
    result: dict[str, str] = {}
    for item in segment.get("protected_tokens") or []:
        if isinstance(item, dict):
            source_token = str(item.get("token", ""))
            target_token = str(item.get("target_token", source_token))
        else:
            source_token = str(item)
            target_token = source_token
        if source_token:
            result[source_token] = target_token
    return result


def _rgb_to_srgb_int(value: Any) -> int:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError("text_color must contain exactly three channels")
    channels = []
    for channel in value:
        if isinstance(channel, bool) or not isinstance(channel, (int, float)):
            raise ValueError("text_color channels must be numeric")
        parsed = float(channel)
        if not 0.0 <= parsed <= 1.0:
            raise ValueError("text_color channels must be between 0 and 1")
        channels.append(max(0, min(255, round(parsed * 255))))
    return (channels[0] << 16) | (channels[1] << 8) | channels[2]


def validate_text_color_contract(segment: dict[str, Any]) -> list[dict[str, Any]]:
    segment_id = str(segment.get("segment_id", ""))
    render = segment.get("render") or {}
    if render.get("action", "replace") in PRESERVE_ACTIONS:
        return []
    issues: list[dict[str, Any]] = []
    source_colors = list(
        dict.fromkeys((segment.get("font_style") or {}).get("source_colors") or [])
    )
    for value in source_colors:
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 0xFFFFFF:
            issues.append(
                issue(
                    "BLOCKING",
                    "SOURCE_TEXT_COLOR",
                    f"Invalid 24-bit source text color: {value!r}",
                    segment_id=segment_id,
                )
            )
    policy = str(render.get("text_color_policy", "")).strip()
    allowed_policies = {"", "preserve_source_exact", "explicit_inspected", "fragment_source_exact"}
    if policy not in allowed_policies:
        issues.append(
            issue(
                "BLOCKING",
                "TEXT_COLOR_POLICY",
                f"Unsupported text_color_policy: {policy!r}",
                segment_id=segment_id,
            )
        )
    explicit = render.get("text_color")
    explicit_srgb = None
    if explicit is not None:
        try:
            explicit_srgb = _rgb_to_srgb_int(explicit)
        except Exception as exc:
            issues.append(issue("BLOCKING", "TEXT_COLOR", str(exc), segment_id=segment_id))
    fragments = render.get("fragments") or []
    if len(source_colors) > 1:
        if policy == "fragment_source_exact" and fragments:
            for index, fragment in enumerate(fragments):
                try:
                    _rgb_to_srgb_int(fragment.get("text_color"))
                except Exception as exc:
                    issues.append(
                        issue(
                            "BLOCKING",
                            "FRAGMENT_TEXT_COLOR",
                            str(exc),
                            segment_id=segment_id,
                            evidence={"fragment_index": index},
                        )
                    )
                if not str(fragment.get("text_color_basis", "")).strip():
                    issues.append(
                        issue(
                            "BLOCKING",
                            "FRAGMENT_TEXT_COLOR_BASIS",
                            "Each multi-color fragment needs text_color_basis",
                            segment_id=segment_id,
                            evidence={"fragment_index": index},
                        )
                    )
        elif policy == "explicit_inspected" and explicit is not None:
            if not str(render.get("text_color_basis", "")).strip():
                issues.append(
                    issue(
                        "BLOCKING",
                        "TEXT_COLOR_BASIS",
                        "An inspected multi-color override needs text_color_basis",
                        segment_id=segment_id,
                    )
                )
        else:
            issues.append(
                issue(
                    "BLOCKING",
                    "AMBIGUOUS_SOURCE_TEXT_COLOR",
                    "Multiple source text colors require fragment_source_exact or explicit_inspected routing",
                    segment_id=segment_id,
                    evidence=source_colors,
                )
            )
    elif len(source_colors) == 1 and explicit_srgb is not None:
        if explicit_srgb != source_colors[0] and policy != "explicit_inspected":
            issues.append(
                issue(
                    "BLOCKING",
                    "SOURCE_TEXT_COLOR_CHANGED",
                    "Explicit text_color differs from the single verified source color",
                    segment_id=segment_id,
                    evidence={"source_srgb": source_colors[0], "explicit_srgb": explicit_srgb},
                )
            )
        if policy == "explicit_inspected" and not str(render.get("text_color_basis", "")).strip():
            issues.append(
                issue(
                    "BLOCKING",
                    "TEXT_COLOR_BASIS",
                    "An inspected color override needs text_color_basis",
                    segment_id=segment_id,
                )
            )
    elif not source_colors and policy == "preserve_source_exact":
        issues.append(
            issue(
                "BLOCKING",
                "SOURCE_TEXT_COLOR_MISSING",
                "preserve_source_exact requires source color evidence",
                segment_id=segment_id,
            )
        )
    return issues


def validate_text_alignment_contract(
    segment: dict[str, Any], page_rect: list[float] | None
) -> list[dict[str, Any]]:
    segment_id = str(segment.get("segment_id", ""))
    render = segment.get("render") or {}
    contract = render.get("alignment_contract")
    if contract is None:
        return []
    if not isinstance(contract, dict):
        return [issue("BLOCKING", "TEXT_ALIGNMENT_FIELDS", "alignment_contract must be an object", segment_id=segment_id)]
    required = {
        "schema",
        "alignment",
        "source_reference_bbox",
        "target_reference_bbox",
        "source_text_bbox",
        "maximum_delta_pt",
        "measurement_basis",
    }
    missing = sorted(required - set(contract))
    if missing:
        return [issue("BLOCKING", "TEXT_ALIGNMENT_FIELDS", f"Missing alignment fields: {missing}", segment_id=segment_id)]
    issues: list[dict[str, Any]] = []
    if contract.get("schema") != TEXT_ALIGNMENT_SCHEMA:
        issues.append(issue("BLOCKING", "TEXT_ALIGNMENT_SCHEMA", f"Expected {TEXT_ALIGNMENT_SCHEMA!r}", segment_id=segment_id))
    alignment = str(contract.get("alignment", ""))
    if alignment not in {"left", "right", "center"}:
        issues.append(issue("BLOCKING", "TEXT_ALIGNMENT_MODE", f"Unsupported alignment: {alignment!r}", segment_id=segment_id))
    if render.get("align", "left") != alignment:
        issues.append(issue("BLOCKING", "TEXT_ALIGNMENT_RENDER_MISMATCH", "render.align must match alignment_contract.alignment", segment_id=segment_id))
    if contract.get("measurement_basis") != "actual_candidate_text_span_bbox":
        issues.append(issue("BLOCKING", "TEXT_ALIGNMENT_MEASUREMENT", "Alignment QA must use actual_candidate_text_span_bbox", segment_id=segment_id))
    try:
        maximum = float(contract.get("maximum_delta_pt"))
        if maximum < 0:
            raise ValueError("maximum_delta_pt must be non-negative")
    except Exception as exc:
        issues.append(issue("BLOCKING", "TEXT_ALIGNMENT_TOLERANCE", str(exc), segment_id=segment_id))
    try:
        source_reference = normalize_bbox(contract["source_reference_bbox"])
        target_reference = normalize_bbox(contract["target_reference_bbox"])
        source_text = normalize_bbox(contract["source_text_bbox"])
        target_bbox = normalize_bbox(render["target_bbox"])
        if page_rect is not None:
            for name, bbox in (
                ("source_reference_bbox", source_reference),
                ("target_reference_bbox", target_reference),
                ("source_text_bbox", source_text),
            ):
                if not bbox_inside(bbox, page_rect):
                    issues.append(issue("BLOCKING", "TEXT_ALIGNMENT_BBOX_PAGE", f"{name} is outside the page", segment_id=segment_id))
        if not bbox_inside(source_text, source_reference, tolerance=0.001):
            issues.append(issue("BLOCKING", "TEXT_ALIGNMENT_SOURCE_CONTAINMENT", "source_text_bbox must be inside source_reference_bbox", segment_id=segment_id))
        if not bbox_inside(target_bbox, target_reference, tolerance=0.001):
            issues.append(issue("BLOCKING", "TEXT_ALIGNMENT_TARGET_CONTAINMENT", "render.target_bbox must be inside target_reference_bbox", segment_id=segment_id))
    except Exception as exc:
        issues.append(issue("BLOCKING", "TEXT_ALIGNMENT_BBOX", str(exc), segment_id=segment_id))
    return issues


def validate_table_cell_phrase_groups(
    segments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    by_id = {str(segment.get("segment_id", "")): segment for segment in segments}
    groups: dict[str, dict[str, Any]] = {}
    declarations: dict[str, list[str]] = {}
    for segment in segments:
        contract = (segment.get("relationships") or {}).get("table_cell_phrase")
        if contract is None:
            continue
        segment_id = str(segment.get("segment_id", ""))
        if not isinstance(contract, dict):
            issues.append(issue("BLOCKING", "TABLE_CELL_PHRASE_FIELDS", "table_cell_phrase must be an object", segment_id=segment_id))
            continue
        required = {"schema", "group_id", "table_id", "cell_bbox", "segment_ids", "source_phrase", "target_phrase", "source_separator", "target_separator"}
        missing = sorted(required - set(contract))
        if missing:
            issues.append(issue("BLOCKING", "TABLE_CELL_PHRASE_FIELDS", f"Missing table-cell phrase fields: {missing}", segment_id=segment_id))
            continue
        if contract.get("schema") != TABLE_CELL_PHRASE_SCHEMA:
            issues.append(issue("BLOCKING", "TABLE_CELL_PHRASE_SCHEMA", f"Expected {TABLE_CELL_PHRASE_SCHEMA!r}", segment_id=segment_id))
        group_id = str(contract.get("group_id", "")).strip()
        if not group_id:
            issues.append(issue("BLOCKING", "TABLE_CELL_PHRASE_GROUP_ID", "group_id is required", segment_id=segment_id))
            continue
        declarations.setdefault(group_id, []).append(segment_id)
        canonical = json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if group_id in groups and groups[group_id]["canonical"] != canonical:
            issues.append(issue("BLOCKING", "TABLE_CELL_PHRASE_CONFLICT", "Every member must declare the identical table-cell phrase contract", segment_id=segment_id, evidence=group_id))
        else:
            groups[group_id] = {"contract": contract, "canonical": canonical}

    for group_id, item in groups.items():
        contract = item["contract"]
        member_ids = [str(value) for value in contract.get("segment_ids") or []]
        if len(member_ids) < 2 or len(set(member_ids)) != len(member_ids):
            issues.append(issue("BLOCKING", "TABLE_CELL_PHRASE_MEMBERS", "segment_ids must contain at least two unique ordered IDs", evidence=group_id))
            continue
        missing_ids = [identifier for identifier in member_ids if identifier not in by_id]
        if missing_ids:
            issues.append(issue("BLOCKING", "TABLE_CELL_PHRASE_MEMBER_MISSING", "A table-cell phrase member is missing", evidence={"group_id": group_id, "missing": missing_ids}))
            continue
        if set(declarations.get(group_id, [])) != set(member_ids):
            issues.append(issue("BLOCKING", "TABLE_CELL_PHRASE_DECLARATION_COVERAGE", "Every listed phrase member must declare the contract", evidence=group_id))
        members = [by_id[identifier] for identifier in member_ids]
        pages = {int(member.get("page", 0)) for member in members}
        if len(pages) != 1 or any(member.get("semantic_type") != "table-cell" for member in members):
            issues.append(issue("BLOCKING", "TABLE_CELL_PHRASE_CONTEXT", "Phrase members must be same-page table-cell segments", evidence=group_id))
        try:
            cell_bbox = normalize_bbox(contract["cell_bbox"])
            for member in members:
                container = (member.get("render") or {}).get("container_bbox")
                if container is None or any(abs(a - b) > 0.001 for a, b in zip(normalize_bbox(container), cell_bbox)):
                    issues.append(issue("BLOCKING", "TABLE_CELL_PHRASE_CONTAINER", "Each phrase member must bind the declared cell_bbox", segment_id=str(member.get("segment_id", "")), evidence=group_id))
        except Exception as exc:
            issues.append(issue("BLOCKING", "TABLE_CELL_PHRASE_BBOX", str(exc), evidence=group_id))
        source_joined = str(contract.get("source_separator", "")).join(str(member.get("source_text", "")) for member in members)
        target_joined = str(contract.get("target_separator", "")).join(str(member.get("zh_TW", "")) for member in members)
        if source_joined != str(contract.get("source_phrase", "")):
            issues.append(issue("BLOCKING", "TABLE_CELL_SOURCE_PHRASE_MISMATCH", "Joined source fragments do not equal source_phrase", evidence={"group_id": group_id, "joined": source_joined}))
        if target_joined != str(contract.get("target_phrase", "")):
            issues.append(issue("BLOCKING", "TABLE_CELL_TARGET_PHRASE_MISMATCH", "Joined translations do not equal target_phrase", evidence={"group_id": group_id, "joined": target_joined}))
    return issues


def normalized_target_cue(cue: Any) -> dict[str, str]:
    if isinstance(cue, str):
        return {"text": cue, "kind": "general", "scope": "segment"}
    if not isinstance(cue, dict):
        raise ValueError("required_target_cues entries must be strings or objects")
    return {
        "text": str(cue.get("text", "")).strip(),
        "kind": str(cue.get("kind", "general")).strip(),
        "scope": str(cue.get("scope", "segment")).strip(),
    }


def validate_manifest(
    manifest: dict[str, Any], *, require_translation: bool = False, require_render: bool = False
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    if manifest.get("schema") != SCHEMA:
        issues.append(issue("BLOCKING", "SCHEMA", f"Expected schema {SCHEMA!r}"))

    source = manifest.get("source") or {}
    source_hash = source.get("sha256")
    if not isinstance(source_hash, str) or not re.fullmatch(r"[A-F0-9]{64}", source_hash):
        issues.append(issue("BLOCKING", "SOURCE_SHA256", "source.sha256 must be uppercase SHA-256"))

    page_rects: dict[int, list[float]] = {}
    preserved_visuals: dict[str, dict[str, Any]] = {}
    scoped_ui_entries: dict[tuple[str, str], dict[str, Any]] = {}
    document_context_refs: dict[str, dict[str, Any]] = {}
    for packet in manifest.get("page_contexts") or []:
        try:
            page_number = int(packet["page"])
            page_rects[page_number] = normalize_bbox(packet["page_bbox"])
        except Exception as exc:
            issues.append(issue("BLOCKING", "PAGE_CONTEXT", str(exc), evidence=packet))
            continue
        for context_ref in packet.get("document_context_refs") or []:
            context_ref_id = str(context_ref.get("context_ref_id", "")).strip()
            if not context_ref_id:
                issues.append(
                    issue(
                        "BLOCKING",
                        "DOCUMENT_CONTEXT_REF_ID",
                        "document_context_refs[].context_ref_id is required",
                        evidence=context_ref,
                    )
                )
                continue
            if context_ref_id in document_context_refs:
                issues.append(
                    issue(
                        "BLOCKING",
                        "DUPLICATE_DOCUMENT_CONTEXT_REF",
                        f"Duplicate document context reference: {context_ref_id}",
                    )
                )
                continue
            document_context_refs[context_ref_id] = context_ref
            try:
                referenced_page = int(context_ref["page"])
                if referenced_page < 1 or referenced_page > int(source.get("page_count", 0)):
                    raise ValueError(
                        f"document context page {referenced_page} is outside source page range"
                    )
            except Exception as exc:
                issues.append(
                    issue(
                        "BLOCKING",
                        "DOCUMENT_CONTEXT_REF_PAGE",
                        str(exc),
                        evidence=context_ref,
                    )
                )
            if not str(context_ref.get("source_excerpt", "")).strip():
                issues.append(
                    issue(
                        "BLOCKING",
                        "DOCUMENT_CONTEXT_REF_SOURCE",
                        f"source_excerpt is required for {context_ref_id}",
                    )
                )
        for visual in packet.get("preserved_visuals") or []:
            visual_id = str(visual.get("visual_id", "")).strip()
            if not visual_id:
                issues.append(issue("BLOCKING", "VISUAL_ID", "preserved_visuals[].visual_id is required"))
                continue
            if visual_id in preserved_visuals:
                issues.append(issue("BLOCKING", "DUPLICATE_VISUAL_ID", f"Duplicate visual_id: {visual_id}"))
                continue
            preserved_visuals[visual_id] = visual
            if visual.get("policy") != "preserve_source_visual_with_textual_guidance":
                issues.append(
                    issue(
                        "BLOCKING",
                        "VISUAL_POLICY",
                        f"Unsupported preserved visual policy for {visual_id}",
                    )
                )
            visual_kind = str(visual.get("visual_kind", "raster_image"))
            if visual_kind not in ALLOWED_VISUAL_KINDS:
                issues.append(
                    issue(
                        "BLOCKING",
                        "VISUAL_KIND",
                        f"Unsupported preserved visual kind for {visual_id}: {visual_kind!r}",
                    )
                )
            try:
                if not bbox_inside(visual["bbox"], page_rects[page_number]):
                    issues.append(issue("BLOCKING", "VISUAL_BBOX", f"Visual bbox is outside page: {visual_id}"))
            except Exception as exc:
                issues.append(issue("BLOCKING", "VISUAL_BBOX", str(exc), evidence=visual))
            if visual_kind == "raster_image":
                for key in ("pixel_width", "pixel_height"):
                    if not isinstance(visual.get(key), int) or int(visual[key]) <= 0:
                        issues.append(issue("BLOCKING", "VISUAL_PIXEL_SIZE", f"{visual_id}.{key} must be positive"))
                decoded_hash = visual.get("decoded_image_sha256")
                if not isinstance(decoded_hash, str) or not re.fullmatch(r"[A-F0-9]{64}", decoded_hash):
                    issues.append(issue("BLOCKING", "VISUAL_SHA256", f"Invalid decoded image SHA-256: {visual_id}"))
                for layer in visual.get("image_layers") or []:
                    layer_hash = layer.get("decoded_samples_sha256")
                    if not isinstance(layer_hash, str) or not re.fullmatch(r"[A-F0-9]{64}", layer_hash):
                        issues.append(issue("BLOCKING", "VISUAL_LAYER_SHA256", f"Invalid layer SHA-256: {visual_id}"))
            else:
                component_roles = visual.get("component_roles") or []
                if not isinstance(component_roles, list) or not component_roles:
                    issues.append(
                        issue(
                            "BLOCKING",
                            "VECTOR_VISUAL_COMPONENT_ROLES",
                            f"Vector visual {visual_id} must declare component_roles",
                        )
                    )
                unknown_roles = sorted(set(map(str, component_roles)) - ALLOWED_COMPONENT_ROLES)
                if unknown_roles:
                    issues.append(
                        issue(
                            "BLOCKING",
                            "VECTOR_VISUAL_COMPONENT_ROLE",
                            f"Unsupported vector component roles for {visual_id}",
                            evidence=unknown_roles,
                        )
                    )
                if not str(visual.get("component_group_id", "")).strip():
                    issues.append(
                        issue(
                            "BLOCKING",
                            "VECTOR_VISUAL_COMPONENT_GROUP",
                            f"Vector visual {visual_id} must declare component_group_id",
                        )
                    )
            allowed_entries = [
                *(visual.get("allowed_ui_english") or []),
                *(visual.get("allowed_visual_english") or []),
            ]
            for entry in allowed_entries:
                source_text = str(entry.get("source_text", "")).strip()
                zh_text = str(entry.get("zh_TW", "")).strip()
                if not source_text or not zh_text:
                    issues.append(
                        issue(
                            "BLOCKING",
                            "UI_ALLOWLIST_MAPPING",
                            f"UI mapping requires source_text and zh_TW: {visual_id}",
                            evidence=entry,
                        )
                    )
                    continue
                key = (visual_id, source_text)
                if key in scoped_ui_entries:
                    issues.append(issue("BLOCKING", "DUPLICATE_UI_ALLOWLIST", f"Duplicate UI mapping: {key}"))
                scoped_ui_entries[key] = entry
                guidance_ids = entry.get("guidance_segment_ids") or []
                if not isinstance(guidance_ids, list):
                    issues.append(issue("BLOCKING", "UI_GUIDANCE_IDS", f"guidance_segment_ids must be a list: {key}"))
                if entry.get("guidance_required", True) and not guidance_ids:
                    issues.append(issue("BLOCKING", "UI_GUIDANCE_REQUIRED", f"Guidance is required for UI mapping: {key}"))

    if document_context_refs:
        source_path = Path(str(source.get("path", "")))
        if not source_path.is_file():
            issues.append(
                issue(
                    "NEEDS_REVIEW",
                    "DOCUMENT_CONTEXT_SOURCE_NOT_CHECKED",
                    "Source PDF is unavailable for document-context excerpt verification",
                )
            )
        else:
            try:
                import fitz

                source_doc = fitz.open(source_path)
                for context_ref_id, context_ref in document_context_refs.items():
                    referenced_page = int(context_ref["page"])
                    excerpt = re.sub(r"\s+", " ", str(context_ref.get("source_excerpt", ""))).strip()
                    page_text = re.sub(
                        r"\s+", " ", source_doc[referenced_page - 1].get_text()
                    ).strip()
                    if excerpt and excerpt not in page_text:
                        issues.append(
                            issue(
                                "BLOCKING",
                                "DOCUMENT_CONTEXT_REF_MISMATCH",
                                f"Source excerpt is not present on page {referenced_page}: {context_ref_id}",
                                evidence={"source_excerpt": excerpt},
                            )
                        )
                source_doc.close()
            except ImportError:
                issues.append(
                    issue(
                        "NEEDS_REVIEW",
                        "DOCUMENT_CONTEXT_SOURCE_NOT_CHECKED",
                        "PyMuPDF is required to verify document-context source excerpts",
                    )
                )
            except Exception as exc:
                issues.append(
                    issue(
                        "NEEDS_REVIEW",
                        "DOCUMENT_CONTEXT_SOURCE_NOT_CHECKED",
                        f"Could not verify document-context source excerpts: {exc}",
                    )
                )

    segments = manifest.get("segments")
    if not isinstance(segments, list) or not segments:
        return issues + [issue("BLOCKING", "SEGMENTS_EMPTY", "segments must be a non-empty list")]

    ids: list[str] = []
    reading_orders: list[tuple[int, int]] = []
    all_semantic_binding_ids: set[str] = set()
    component_group_signatures: dict[str, str] = {}
    for segment in segments:
        segment_id = str(segment.get("segment_id", ""))
        ids.append(segment_id)
        required = (
            "page",
            "segment_id",
            "semantic_type",
            "bbox",
            "source_lines",
            "source_spans",
            "font_style",
            "rotation",
            "reading_order",
            "protected_tokens",
            "source_text",
            "zh_TW",
            "status",
        )
        missing = [key for key in required if key not in segment]
        if missing:
            issues.append(
                issue("BLOCKING", "SEGMENT_FIELDS", f"Missing fields: {missing}", segment_id=segment_id)
            )
            continue
        if segment["semantic_type"] not in ALLOWED_SEMANTIC_TYPES:
            issues.append(
                issue(
                    "BLOCKING",
                    "SEMANTIC_TYPE",
                    f"Unsupported semantic_type: {segment['semantic_type']!r}",
                    segment_id=segment_id,
                )
            )
        if segment["status"] not in ALLOWED_STATUS:
            issues.append(
                issue("BLOCKING", "STATUS", f"Unsupported status: {segment['status']!r}", segment_id=segment_id)
            )
        page = int(segment["page"])
        render = segment.get("render") or {}
        relationships = segment.get("relationships") or {}
        action = render.get("action", "replace")
        if action not in ALLOWED_RENDER_ACTIONS:
            issues.append(
                issue(
                    "BLOCKING",
                    "RENDER_ACTION",
                    f"Unsupported render.action: {action!r}",
                    segment_id=segment_id,
                )
            )
        if action == "preserve_source_visual_with_textual_guidance":
            if segment.get("semantic_type") not in {"image-text", "UI", "warning", "protected"}:
                issues.append(
                    issue(
                        "BLOCKING",
                        "PRESERVED_VISUAL_TYPE",
                        "preserve_source_visual_with_textual_guidance requires image-text, UI, warning, or protected",
                        segment_id=segment_id,
                    )
                )
            if segment.get("extraction_method") != "visual_annotation":
                issues.append(
                    issue(
                        "BLOCKING",
                        "PRESERVED_VISUAL_EXTRACTION",
                        "Preserved image/UI text must use visual_annotation",
                        segment_id=segment_id,
                    )
                )
            if "mask_bbox" in render or render.get("mask_mode"):
                issues.append(
                    issue(
                        "BLOCKING",
                        "PRESERVED_VISUAL_MASK_FORBIDDEN",
                        "A complete preserved visual component must not declare a mask",
                        segment_id=segment_id,
                    )
                )
            if not relationships.get("visual_id"):
                issues.append(
                    issue(
                        "BLOCKING",
                        "PRESERVED_VISUAL_RELATIONSHIP",
                        "relationships.visual_id is required",
                        segment_id=segment_id,
                    )
                )
            if not isinstance(render.get("guidance_segment_ids", []), list):
                issues.append(
                    issue(
                        "BLOCKING",
                        "PRESERVED_VISUAL_GUIDANCE",
                        "render.guidance_segment_ids must be a list",
                        segment_id=segment_id,
                    )
                )
        reading_orders.append((page, int(segment["reading_order"])))
        page_rect = page_rects.get(page)
        try:
            bbox = normalize_bbox(segment["bbox"])
            if page_rect is None or not bbox_inside(bbox, page_rect):
                issues.append(
                    issue("BLOCKING", "BBOX_PAGE", "bbox is outside the declared page", segment_id=segment_id)
                )
            target_bbox = (segment.get("render") or {}).get("target_bbox", bbox)
            if page_rect is None or not bbox_inside(target_bbox, page_rect):
                issues.append(
                    issue(
                        "BLOCKING",
                        "TARGET_BBOX_PAGE",
                        "render.target_bbox is outside the declared page",
                        segment_id=segment_id,
                    )
                )
            fragments = render.get("fragments")
            if fragments is not None:
                if not isinstance(fragments, list) or not fragments:
                    issues.append(
                        issue(
                            "BLOCKING",
                            "RENDER_FRAGMENTS_TYPE",
                            "render.fragments must be a non-empty list",
                            segment_id=segment_id,
                        )
                    )
                else:
                    fragment_ids: list[str] = []
                    for fragment_index, fragment in enumerate(fragments):
                        if not isinstance(fragment, dict):
                            issues.append(
                                issue(
                                    "BLOCKING",
                                    "RENDER_FRAGMENT_FIELDS",
                                    "Each render fragment must be an object",
                                    segment_id=segment_id,
                                    evidence={"fragment_index": fragment_index},
                                )
                            )
                            continue
                        fragment_id = str(fragment.get("fragment_id", "")).strip()
                        fragment_ids.append(fragment_id)
                        missing_fragment_fields = [
                            key
                            for key in ("fragment_id", "source_text", "target_bbox")
                            if key not in fragment or not str(fragment.get(key, "")).strip()
                        ]
                        if missing_fragment_fields:
                            issues.append(
                                issue(
                                    "BLOCKING",
                                    "RENDER_FRAGMENT_FIELDS",
                                    f"Fragment fields are missing: {missing_fragment_fields}",
                                    segment_id=segment_id,
                                    evidence={"fragment_index": fragment_index},
                                )
                            )
                            continue
                        fragment_bbox = normalize_bbox(fragment["target_bbox"])
                        if page_rect is None or not bbox_inside(fragment_bbox, page_rect):
                            issues.append(
                                issue(
                                    "BLOCKING",
                                    "RENDER_FRAGMENT_BBOX_PAGE",
                                    "A render fragment target_bbox is outside the page",
                                    segment_id=segment_id,
                                    evidence={"fragment_id": fragment_id},
                                )
                            )
                        if not bbox_inside(fragment_bbox, target_bbox, tolerance=0.001):
                            issues.append(
                                issue(
                                    "BLOCKING",
                                    "RENDER_FRAGMENT_BBOX_TARGET",
                                    "A render fragment must stay inside render.target_bbox",
                                    segment_id=segment_id,
                                    evidence={"fragment_id": fragment_id},
                                )
                            )
                        if require_translation and not str(
                            fragment.get("zh_TW", "")
                        ).strip():
                            issues.append(
                                issue(
                                    "BLOCKING",
                                    "RENDER_FRAGMENT_TRANSLATION_EMPTY",
                                    "Each render fragment needs non-empty zh_TW",
                                    segment_id=segment_id,
                                    evidence={"fragment_id": fragment_id},
                                )
                            )
                    duplicates = sorted(
                        fragment_id
                        for fragment_id, count in Counter(fragment_ids).items()
                        if fragment_id and count > 1
                    )
                    if duplicates:
                        issues.append(
                            issue(
                                "BLOCKING",
                                "RENDER_FRAGMENT_ID_DUPLICATE",
                                f"Duplicate fragment IDs: {duplicates}",
                                segment_id=segment_id,
                            )
                        )
        except Exception as exc:
            issues.append(issue("BLOCKING", "BBOX", str(exc), segment_id=segment_id))

        issues.extend(validate_text_color_contract(segment))
        issues.extend(validate_text_alignment_contract(segment, page_rect))

        component_contract = segment.get("component_contract")
        if component_contract is not None:
            if not isinstance(component_contract, dict):
                issues.append(
                    issue(
                        "BLOCKING",
                        "COMPONENT_CONTRACT_TYPE",
                        "component_contract must be an object",
                        segment_id=segment_id,
                    )
                )
                component_contract = {}
            if component_contract.get("schema") == COMPOUND_COMPONENT_SCHEMA:
                issues.extend(validate_compound_component_contract(segment, page_rect))
            group_id = str(component_contract.get("group_id", "")).strip()
            segment_role = str(component_contract.get("segment_role", "")).strip()
            mask_policy = str(component_contract.get("mask_policy", "")).strip()
            relation = str(relationships.get("component_relation", "")).strip()
            relationship_group = str(relationships.get("component_group_id", "")).strip()
            if not group_id:
                issues.append(issue("BLOCKING", "COMPONENT_GROUP_ID", "component_contract.group_id is required", segment_id=segment_id))
            if relationship_group != group_id:
                issues.append(
                    issue(
                        "BLOCKING",
                        "COMPONENT_GROUP_RELATIONSHIP",
                        "relationships.component_group_id must match component_contract.group_id",
                        segment_id=segment_id,
                    )
                )
            if relation not in ALLOWED_COMPONENT_RELATIONS:
                issues.append(
                    issue(
                        "BLOCKING",
                        "COMPONENT_RELATION",
                        f"Unsupported component relation: {relation!r}",
                        segment_id=segment_id,
                    )
                )
            if segment_role not in ALLOWED_SEGMENT_COMPONENT_ROLES:
                issues.append(
                    issue(
                        "BLOCKING",
                        "COMPONENT_SEGMENT_ROLE",
                        f"Unsupported component segment role: {segment_role!r}",
                        segment_id=segment_id,
                    )
                )
            if mask_policy not in ALLOWED_MASK_POLICIES:
                issues.append(
                    issue(
                        "BLOCKING",
                        "COMPONENT_MASK_POLICY",
                        f"Unsupported component mask policy: {mask_policy!r}",
                        segment_id=segment_id,
                    )
                )
            members = component_contract.get("members") or []
            if not isinstance(members, list) or not members:
                issues.append(
                    issue(
                        "BLOCKING",
                        "COMPONENT_MEMBERS",
                        "component_contract.members must be a non-empty list",
                        segment_id=segment_id,
                    )
                )
                members = []
            member_ids: list[str] = []
            live_members: list[dict[str, Any]] = []
            for member in members:
                if not isinstance(member, dict):
                    issues.append(issue("BLOCKING", "COMPONENT_MEMBER_TYPE", "Each component member must be an object", segment_id=segment_id, evidence=member))
                    continue
                component_id = str(member.get("component_id", "")).strip()
                role = str(member.get("role", "")).strip()
                policy = str(member.get("policy", "")).strip()
                member_ids.append(component_id)
                if not component_id:
                    issues.append(issue("BLOCKING", "COMPONENT_ID", "component_id is required", segment_id=segment_id))
                if role not in ALLOWED_COMPONENT_ROLES:
                    issues.append(issue("BLOCKING", "COMPONENT_ROLE", f"Unsupported component role: {role!r}", segment_id=segment_id))
                if policy not in ALLOWED_COMPONENT_POLICIES:
                    issues.append(issue("BLOCKING", "COMPONENT_POLICY", f"Unsupported component policy: {policy!r}", segment_id=segment_id))
                try:
                    if page_rect is None or not bbox_inside(member["bbox"], page_rect):
                        issues.append(issue("BLOCKING", "COMPONENT_BBOX", f"Component bbox is outside page: {component_id}", segment_id=segment_id))
                except Exception as exc:
                    issues.append(issue("BLOCKING", "COMPONENT_BBOX", str(exc), segment_id=segment_id, evidence=member))
                if role in {"live_text", "translatable_live_text"}:
                    live_members.append(member)
                    if policy != "replace_live_text":
                        issues.append(issue("BLOCKING", "LIVE_TEXT_COMPONENT_POLICY", "A live-text component must use replace_live_text", segment_id=segment_id))
                elif policy == "replace_live_text":
                    issues.append(issue("BLOCKING", "NON_TEXT_COMPONENT_REPLACEMENT", "Only a translatable live-text member may use replace_live_text", segment_id=segment_id, evidence=component_id))
                if policy == "adjust_background":
                    if role != "background":
                        issues.append(
                            issue(
                                "BLOCKING",
                                "BACKGROUND_ADJUSTMENT_ROLE",
                                "Only a separately identified background member may use adjust_background",
                                segment_id=segment_id,
                                evidence=component_id,
                            )
                        )
                    if member.get("adjustment_method") != "rewrite_untransformed_rect":
                        issues.append(
                            issue(
                                "BLOCKING",
                                "BACKGROUND_ADJUSTMENT_METHOD",
                                "adjust_background requires adjustment_method=rewrite_untransformed_rect",
                                segment_id=segment_id,
                                evidence=component_id,
                            )
                        )
                    dependency = member.get("dependent_geometry")
                    target_background_bbox = None
                    if dependency is None:
                        try:
                            target_background_bbox = normalize_bbox(member["target_bbox"])
                            if page_rect is None or not bbox_inside(target_background_bbox, page_rect):
                                issues.append(
                                    issue(
                                        "BLOCKING",
                                        "BACKGROUND_TARGET_BBOX_PAGE",
                                        "Adjusted background target_bbox is outside the page",
                                        segment_id=segment_id,
                                        evidence=component_id,
                                    )
                                )
                            if not bbox_inside(target_background_bbox, member["bbox"], tolerance=0.01):
                                issues.append(
                                    issue(
                                        "BLOCKING",
                                        "BACKGROUND_ADJUSTMENT_EXPANDS",
                                        "A background adjustment may shrink only within the declared source background bbox",
                                        segment_id=segment_id,
                                        evidence=component_id,
                                    )
                                )
                        except Exception as exc:
                            issues.append(
                                issue(
                                    "BLOCKING",
                                    "BACKGROUND_TARGET_BBOX",
                                    str(exc),
                                    segment_id=segment_id,
                                    evidence=component_id,
                                )
                            )
                    expected_fill = member.get("expected_fill")
                    if (
                        not isinstance(expected_fill, list)
                        or len(expected_fill) != 3
                        or any(
                            not isinstance(channel, (int, float))
                            or float(channel) < 0
                            or float(channel) > 1
                            for channel in expected_fill
                        )
                    ):
                        issues.append(
                            issue(
                                "BLOCKING",
                                "BACKGROUND_EXPECTED_FILL",
                                "adjust_background requires expected_fill with three values between 0 and 1",
                                segment_id=segment_id,
                                evidence=component_id,
                            )
                        )
                    avoid_regions = member.get("avoid_regions")
                    if not isinstance(avoid_regions, list) or not avoid_regions:
                        issues.append(
                            issue(
                                "BLOCKING",
                                "BACKGROUND_AVOID_REGIONS",
                                "adjust_background requires at least one explicit avoid_region",
                                segment_id=segment_id,
                                evidence=component_id,
                            )
                        )
                    else:
                        for avoid_region in avoid_regions:
                            try:
                                if not str(avoid_region.get("region_id", "")).strip():
                                    raise ValueError("avoid_region.region_id is required")
                                avoid_bbox = normalize_bbox(avoid_region["bbox"])
                                if page_rect is None or not bbox_inside(avoid_bbox, page_rect):
                                    raise ValueError("avoid_region bbox is outside the page")
                                source_overlap = bbox_intersection_area(member["bbox"], avoid_bbox)
                                target_overlap = (
                                    bbox_intersection_area(target_background_bbox, avoid_bbox)
                                    if target_background_bbox is not None
                                    else 0.0
                                )
                                if source_overlap <= 0:
                                    raise ValueError("source background does not intersect the avoid_region")
                                if target_overlap > 0.0001:
                                    raise ValueError("adjusted background still intersects the avoid_region")
                            except Exception as exc:
                                issues.append(
                                    issue(
                                        "BLOCKING",
                                        "BACKGROUND_AVOID_REGION_INVALID",
                                        str(exc),
                                        segment_id=segment_id,
                                        evidence={"component_id": component_id, "avoid_region": avoid_region},
                                    )
                                )
                if (
                    policy == "adjust_vector_rule"
                    and component_contract.get("schema") != COMPOUND_COMPONENT_SCHEMA
                ):
                    issues.append(
                        issue(
                            "BLOCKING",
                            "VECTOR_RULE_ADJUSTMENT_SCHEMA",
                            "adjust_vector_rule is valid only in a v2 compound-component contract",
                            segment_id=segment_id,
                            evidence=component_id,
                        )
                    )
                if role == "vector_outlined_text" and policy != "preserve_complete_visual":
                    issues.append(
                        issue(
                            "BLOCKING",
                            "VECTOR_COMPONENT_PARTIAL_MASK",
                            "Vector outlined text must be preserved as a complete visual component",
                            segment_id=segment_id,
                            evidence=component_id,
                        )
                    )
            duplicates = sorted(identifier for identifier, count in Counter(member_ids).items() if identifier and count > 1)
            if duplicates:
                issues.append(issue("BLOCKING", "DUPLICATE_COMPONENT_ID", "Duplicate component IDs in group", segment_id=segment_id, evidence=duplicates))
            if group_id and members:
                signature = json.dumps(members, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
                previous = component_group_signatures.setdefault(group_id, signature)
                if previous != signature:
                    issues.append(issue("BLOCKING", "COMPONENT_GROUP_MISMATCH", "Segments in a component group must declare identical members", segment_id=segment_id, evidence=group_id))

            if segment_role == "live_text":
                if action != "replace" or mask_policy != "source_text_spans_only":
                    issues.append(issue("BLOCKING", "LIVE_TEXT_COMPONENT_ACTION", "A live_text component must replace source spans only", segment_id=segment_id))
                if segment.get("extraction_method") != "source_spans":
                    issues.append(issue("BLOCKING", "LIVE_TEXT_COMPONENT_EXTRACTION", "A live_text component must use source_spans", segment_id=segment_id))
                if render.get("mask_mode") != "remove_text_only" or float(render.get("mask_padding_pt", 0.0)) != 0.0:
                    issues.append(issue("BLOCKING", "LIVE_TEXT_MASK_POLICY", "Live text requires remove_text_only with zero mask padding", segment_id=segment_id))
                if len(live_members) != 1:
                    issues.append(issue("BLOCKING", "LIVE_TEXT_COMPONENT_COUNT", "A live_text segment must declare exactly one live_text member", segment_id=segment_id))
                elif not bbox_inside(render.get("mask_bbox", segment["bbox"]), live_members[0]["bbox"], tolerance=0.1):
                    issues.append(issue("BLOCKING", "LIVE_TEXT_MASK_ESCAPES_COMPONENT", "The live-text mask escapes its declared member bbox", segment_id=segment_id))
            elif segment_role == "preserved_component":
                if action != "preserve" or mask_policy != "none" or "mask_bbox" in render:
                    issues.append(issue("BLOCKING", "PRESERVED_COMPONENT_ACTION", "A preserved component must use action=preserve and mask_policy=none", segment_id=segment_id))
            elif segment_role == "complete_visual":
                if action != "preserve_source_visual_with_textual_guidance" or mask_policy != "none" or "mask_bbox" in render:
                    issues.append(issue("BLOCKING", "COMPLETE_VISUAL_ACTION", "A complete visual must be preserved without any mask", segment_id=segment_id))

        source_text = str(segment.get("source_text", ""))
        zh_text = str(segment.get("zh_TW", ""))
        if not source_text.strip():
            issues.append(issue("BLOCKING", "SOURCE_TEXT_EMPTY", "source_text is empty", segment_id=segment_id))

        semantic_bindings = segment.get("semantic_bindings") or []
        if not isinstance(semantic_bindings, list):
            issues.append(
                issue(
                    "BLOCKING",
                    "SEMANTIC_BINDINGS_TYPE",
                    "semantic_bindings must be a list",
                    segment_id=segment_id,
                )
            )
            semantic_bindings = []
        binding_by_id: dict[str, dict[str, Any]] = {}
        token_map = protected_target_map(segment)
        for binding in semantic_bindings:
            if not isinstance(binding, dict):
                issues.append(
                    issue(
                        "BLOCKING",
                        "SEMANTIC_BINDING_TYPE",
                        "Each semantic binding must be an object",
                        segment_id=segment_id,
                        evidence=binding,
                    )
                )
                continue
            binding_id = str(binding.get("binding_id", "")).strip()
            if not binding_id:
                issues.append(
                    issue(
                        "BLOCKING",
                        "SEMANTIC_BINDING_ID",
                        "semantic_bindings[].binding_id is required",
                        segment_id=segment_id,
                    )
                )
                continue
            if binding_id in binding_by_id or binding_id in all_semantic_binding_ids:
                issues.append(
                    issue(
                        "BLOCKING",
                        "DUPLICATE_SEMANTIC_BINDING",
                        f"Duplicate semantic binding ID: {binding_id}",
                        segment_id=segment_id,
                    )
                )
                continue
            binding_by_id[binding_id] = binding
            all_semantic_binding_ids.add(binding_id)
            for field in ("parameter", "role"):
                if not str(binding.get(field, "")).strip():
                    issues.append(
                        issue(
                            "BLOCKING",
                            "SEMANTIC_BINDING_FIELDS",
                            f"{binding_id}.{field} is required",
                            segment_id=segment_id,
                        )
                    )
            source_tokens = binding.get("source_tokens") or []
            if not isinstance(source_tokens, list) or not source_tokens:
                issues.append(
                    issue(
                        "BLOCKING",
                        "SEMANTIC_SOURCE_TOKENS",
                        f"{binding_id}.source_tokens must be a non-empty list",
                        segment_id=segment_id,
                    )
                )
                source_tokens = []
            for source_token in source_tokens:
                source_token = str(source_token)
                if source_token not in source_text:
                    issues.append(
                        issue(
                            "BLOCKING",
                            "SEMANTIC_SOURCE_TOKEN_MISSING",
                            f"Semantic source token is absent from source_text: {source_token!r}",
                            segment_id=segment_id,
                            evidence={"binding_id": binding_id},
                        )
                    )
                if source_token not in token_map:
                    issues.append(
                        issue(
                            "BLOCKING",
                            "SEMANTIC_SOURCE_TOKEN_NOT_PROTECTED",
                            f"Semantic source token is not protected: {source_token!r}",
                            segment_id=segment_id,
                            evidence={"binding_id": binding_id},
                        )
                    )
            clarification_mode = str(binding.get("clarification_mode", "none")).strip()
            if clarification_mode not in ALLOWED_CLARIFICATION_MODES:
                issues.append(
                    issue(
                        "BLOCKING",
                        "CLARIFICATION_POLICY_INVALID",
                        (
                            f"{binding_id}.clarification_mode must be one of "
                            f"{sorted(ALLOWED_CLARIFICATION_MODES)}"
                        ),
                        segment_id=segment_id,
                        evidence=clarification_mode,
                    )
                )
            if clarification_mode != "none":
                for field in ("comparison", "consequence"):
                    if not str(binding.get(field, "")).strip():
                        issues.append(
                            issue(
                                "BLOCKING",
                                "CLARIFICATION_BINDING_FIELDS",
                                (
                                    f"{binding_id}.{field} is required for "
                                    f"{clarification_mode}"
                                ),
                                segment_id=segment_id,
                            )
                        )

            target_cues = binding.get("required_target_cues") or []
            if not isinstance(target_cues, list) or not target_cues:
                issues.append(
                    issue(
                        "BLOCKING",
                        "SEMANTIC_TARGET_CUES",
                        f"{binding_id}.required_target_cues must be a non-empty list",
                        segment_id=segment_id,
                    )
                )
                target_cues = []
            cue_kinds: set[str] = set()
            for cue in target_cues:
                try:
                    cue_data = normalized_target_cue(cue)
                    if not cue_data["text"]:
                        raise ValueError("target cue text is empty")
                    if cue_data["kind"] not in ALLOWED_CUE_KINDS:
                        raise ValueError(f"unsupported target cue kind: {cue_data['kind']}")
                    if cue_data["scope"] not in ALLOWED_CUE_SCOPES:
                        raise ValueError(f"unsupported target cue scope: {cue_data['scope']}")
                    if cue_data["kind"] == "mode" and not str(binding.get("mode", "")).strip():
                        raise ValueError("a mode cue requires binding.mode")
                    if cue_data["kind"] == "condition" and not str(binding.get("condition", "")).strip():
                        raise ValueError("a condition cue requires binding.condition")
                    if cue_data["kind"] == "comparison" and not str(binding.get("comparison", "")).strip():
                        raise ValueError("a comparison cue requires binding.comparison")
                    if cue_data["kind"] == "consequence" and not str(binding.get("consequence", "")).strip():
                        raise ValueError("a consequence cue requires binding.consequence")
                    cue_kinds.add(cue_data["kind"])
                except Exception as exc:
                    issues.append(
                        issue(
                            "BLOCKING",
                            "SEMANTIC_TARGET_CUE",
                            str(exc),
                            segment_id=segment_id,
                            evidence={"binding_id": binding_id, "cue": cue},
                        )
                    )
            if clarification_mode != "none":
                missing_clarification_cues = sorted(
                    {"comparison", "consequence"} - cue_kinds
                )
                if missing_clarification_cues:
                    issues.append(
                        issue(
                            "BLOCKING",
                            "CLARIFICATION_TARGET_CUES",
                            (
                                f"{binding_id} source-derived clarification must declare "
                                "comparison and consequence target cues"
                            ),
                            segment_id=segment_id,
                            evidence=missing_clarification_cues,
                        )
                    )
            context_ref_ids = binding.get("context_ref_ids") or []
            if not isinstance(context_ref_ids, list):
                issues.append(
                    issue(
                        "BLOCKING",
                        "SEMANTIC_CONTEXT_REF_IDS",
                        f"{binding_id}.context_ref_ids must be a list",
                        segment_id=segment_id,
                    )
                )
                context_ref_ids = []
            if binding.get("context_required", False) and not context_ref_ids:
                issues.append(
                    issue(
                        "NEEDS_REVIEW",
                        "CROSS_PAGE_CONTEXT_NOT_CHECKED",
                        f"{binding_id} requires verified source context",
                        segment_id=segment_id,
                    )
                )
            if clarification_mode != "none" and (
                not binding.get("context_required", False) or not context_ref_ids
            ):
                issues.append(
                    issue(
                        "NEEDS_REVIEW",
                        "CLARIFICATION_SOURCE_NOT_VERIFIED",
                        (
                            f"{binding_id} may add explanatory wording only when exact "
                            "same-document context is required and referenced"
                        ),
                        segment_id=segment_id,
                    )
                )
            for context_ref_id in context_ref_ids:
                context_ref_id = str(context_ref_id)
                context_ref = document_context_refs.get(context_ref_id)
                if context_ref is None or context_ref.get("review_status") != VERIFIED_CONTEXT_STATUS:
                    issues.append(
                        issue(
                            "NEEDS_REVIEW",
                            "CROSS_PAGE_CONTEXT_NOT_CHECKED",
                            f"Unverified source context reference: {context_ref_id}",
                            segment_id=segment_id,
                            evidence={"binding_id": binding_id},
                        )
                    )

        if require_translation:
            if action != "preserve" and not zh_text.strip():
                issues.append(issue("BLOCKING", "TRANSLATION_EMPTY", "zh_TW is empty", segment_id=segment_id))
            if segment.get("status") not in {"TRANSLATED", "VALIDATED", "RENDERED"}:
                issues.append(
                    issue("BLOCKING", "TRANSLATION_STATUS", "segment is not translated", segment_id=segment_id)
                )
            for token_item in segment.get("protected_tokens") or []:
                token = token_item["token"] if isinstance(token_item, dict) else str(token_item)
                target_token = token_item.get("target_token", token) if isinstance(token_item, dict) else token
                required_count = (
                    int(token_item.get("source_count", source_text.count(token)))
                    if isinstance(token_item, dict)
                    else source_text.count(token)
                )
                actual_count = zh_text.count(target_token)
                if actual_count < required_count:
                    issues.append(
                        issue(
                            "BLOCKING",
                            "PROTECTED_TOKEN_LOST",
                            f"Protected token {token!r} -> {target_token!r}: expected {required_count}, found {actual_count}",
                            segment_id=segment_id,
                        )
                    )

            translation_assertions = segment.get("translation_assertions") or []
            if not isinstance(translation_assertions, list):
                issues.append(
                    issue(
                        "BLOCKING",
                        "TRANSLATION_ASSERTIONS_TYPE",
                        "translation_assertions must be a list",
                        segment_id=segment_id,
                    )
                )
                translation_assertions = []
            assertion_ids = [str(item.get("binding_id", "")) for item in translation_assertions if isinstance(item, dict)]
            duplicate_assertions = sorted(
                identifier for identifier, count in Counter(assertion_ids).items() if count > 1
            )
            if duplicate_assertions:
                issues.append(
                    issue(
                        "BLOCKING",
                        "DUPLICATE_TRANSLATION_ASSERTION",
                        "A semantic binding was asserted more than once",
                        segment_id=segment_id,
                        evidence=duplicate_assertions,
                    )
                )
            assertion_by_id = {
                str(item.get("binding_id", "")): item
                for item in translation_assertions
                if isinstance(item, dict) and str(item.get("binding_id", ""))
            }
            missing_assertions = sorted(set(binding_by_id) - set(assertion_by_id))
            unknown_assertions = sorted(set(assertion_by_id) - set(binding_by_id))
            if missing_assertions:
                issues.append(
                    issue(
                        "BLOCKING",
                        "SEMANTIC_BINDING_MISSING",
                        "Translation assertions do not cover every semantic binding",
                        segment_id=segment_id,
                        evidence=missing_assertions,
                    )
                )
            if unknown_assertions:
                issues.append(
                    issue(
                        "BLOCKING",
                        "SEMANTIC_ASSERTION_UNKNOWN_BINDING",
                        "Translation assertions reference unknown semantic bindings",
                        segment_id=segment_id,
                        evidence=unknown_assertions,
                    )
                )
            target_tokens_by_binding = {
                binding_id: {
                    token_map.get(str(source_token), str(source_token))
                    for source_token in binding.get("source_tokens") or []
                }
                for binding_id, binding in binding_by_id.items()
            }
            for binding_id, binding in binding_by_id.items():
                assertion = assertion_by_id.get(binding_id)
                if assertion is None:
                    continue
                for field in ("parameter", "role"):
                    if str(assertion.get(field, "")) != str(binding.get(field, "")):
                        issues.append(
                            issue(
                                "BLOCKING",
                                "VALUE_ROLE_SWAPPED",
                                f"{binding_id}.{field} differs between source binding and translation assertion",
                                segment_id=segment_id,
                            )
                        )
                if str(assertion.get("mode", "")) != str(binding.get("mode", "")):
                    issues.append(
                        issue(
                            "BLOCKING",
                            "MODE_CONTEXT_DROPPED",
                            f"{binding_id}.mode differs between source binding and translation assertion",
                            segment_id=segment_id,
                        )
                    )
                if str(assertion.get("condition", "")) != str(binding.get("condition", "")):
                    issues.append(
                        issue(
                            "BLOCKING",
                            "CONDITION_SCOPE_DROPPED",
                            f"{binding_id}.condition differs between source binding and translation assertion",
                            segment_id=segment_id,
                        )
                    )
                if str(assertion.get("comparison", "")) != str(binding.get("comparison", "")):
                    issues.append(
                        issue(
                            "BLOCKING",
                            "COMPARISON_LOGIC_DROPPED",
                            (
                                f"{binding_id}.comparison differs between source binding "
                                "and translation assertion"
                            ),
                            segment_id=segment_id,
                        )
                    )
                if str(assertion.get("consequence", "")) != str(binding.get("consequence", "")):
                    issues.append(
                        issue(
                            "BLOCKING",
                            "CONSEQUENCE_DROPPED",
                            (
                                f"{binding_id}.consequence differs between source binding "
                                "and translation assertion"
                            ),
                            segment_id=segment_id,
                        )
                    )
                if str(assertion.get("clarification_mode", "none")) != str(
                    binding.get("clarification_mode", "none")
                ):
                    issues.append(
                        issue(
                            "BLOCKING",
                            "CLARIFICATION_POLICY_DROPPED",
                            (
                                f"{binding_id}.clarification_mode differs between source "
                                "binding and translation assertion"
                            ),
                            segment_id=segment_id,
                        )
                    )
                target_phrase = str(assertion.get("target_phrase", "")).strip()
                if not target_phrase or target_phrase not in zh_text:
                    issues.append(
                        issue(
                            "BLOCKING",
                            "SEMANTIC_TARGET_PHRASE_NOT_FOUND",
                            f"{binding_id}.target_phrase is absent from zh_TW",
                            segment_id=segment_id,
                            evidence=target_phrase,
                        )
                    )
                own_tokens = target_tokens_by_binding.get(binding_id, set())
                missing_role_tokens = sorted(token for token in own_tokens if token not in target_phrase)
                other_tokens = set().union(
                    *(tokens for other_id, tokens in target_tokens_by_binding.items() if other_id != binding_id)
                ) if len(target_tokens_by_binding) > 1 else set()
                foreign_role_tokens = sorted((other_tokens - own_tokens).intersection(
                    token for token in other_tokens if token in target_phrase
                ))
                if missing_role_tokens or foreign_role_tokens:
                    issues.append(
                        issue(
                            "BLOCKING",
                            "VALUE_ROLE_SWAPPED",
                            "The asserted target phrase does not isolate the source value-role binding",
                            segment_id=segment_id,
                            evidence={
                                "binding_id": binding_id,
                                "missing_own_tokens": missing_role_tokens,
                                "foreign_role_tokens": foreign_role_tokens,
                            },
                        )
                    )
                for cue in binding.get("required_target_cues") or []:
                    try:
                        cue_data = normalized_target_cue(cue)
                    except Exception:
                        continue
                    haystack = target_phrase if cue_data["scope"] == "binding_phrase" else zh_text
                    if cue_data["text"] in haystack:
                        continue
                    cue_code = {
                        "mode": "MODE_CONTEXT_DROPPED",
                        "condition": "CONDITION_SCOPE_DROPPED",
                        "comparison": "COMPARISON_LOGIC_DROPPED",
                        "consequence": "CONSEQUENCE_DROPPED",
                        "parameter": "VALUE_ROLE_SWAPPED",
                        "role": "VALUE_ROLE_SWAPPED",
                    }.get(cue_data["kind"], "SEMANTIC_TARGET_CUE_DROPPED")
                    issues.append(
                        issue(
                            "BLOCKING",
                            cue_code,
                            f"Required semantic cue is absent from zh_TW: {cue_data['text']!r}",
                            segment_id=segment_id,
                            evidence={"binding_id": binding_id, "cue": cue_data},
                        )
                    )

        if require_render:
            if action not in PRESERVE_ACTIONS:
                requested_size = render.get("font_size_pt")
                source_size = (segment.get("font_style") or {}).get("source_font_size_pt")
                if not isinstance(requested_size, (int, float)) or requested_size <= 0:
                    issues.append(
                        issue("BLOCKING", "FONT_SIZE", "render.font_size_pt must be positive", segment_id=segment_id)
                    )
                if isinstance(requested_size, (int, float)) and isinstance(source_size, (int, float)):
                    minimum = max(6.0, 0.75 * float(source_size))
                    exception = render.get("source_small_exception")
                    if float(requested_size) + 1e-6 < minimum and not exception:
                        issues.append(
                            issue(
                                "BLOCKING",
                                "FONT_RATIO",
                                f"Requested {requested_size} pt is below minimum {minimum:.2f} pt",
                                segment_id=segment_id,
                            )
                        )

    issues.extend(validate_table_cell_phrase_groups(segments))

    duplicate_ids = sorted(identifier for identifier, count in Counter(ids).items() if count > 1)
    if duplicate_ids:
        issues.append(issue("BLOCKING", "DUPLICATE_ID", "Duplicate segment IDs", evidence=duplicate_ids))
    duplicate_orders = sorted(value for value, count in Counter(reading_orders).items() if count > 1)
    if duplicate_orders:
        issues.append(
            issue("BLOCKING", "DUPLICATE_READING_ORDER", "Duplicate page reading_order values", evidence=duplicate_orders)
        )

    id_set = set(ids)
    for segment in segments:
        segment_id = str(segment.get("segment_id", ""))
        render = segment.get("render") or {}
        if render.get("action") != "preserve_source_visual_with_textual_guidance":
            continue
        relationships = segment.get("relationships") or {}
        visual_id = str(relationships.get("visual_id", ""))
        if visual_id not in preserved_visuals:
            issues.append(
                issue(
                    "BLOCKING",
                    "UNKNOWN_VISUAL_ID",
                    f"Unknown preserved visual ID: {visual_id}",
                    segment_id=segment_id,
                )
            )
            continue
        visual = preserved_visuals[visual_id]
        if str(visual.get("visual_kind", "raster_image")) == "vector_component":
            contract = segment.get("component_contract") or {}
            if contract.get("segment_role") != "complete_visual":
                issues.append(
                    issue(
                        "BLOCKING",
                        "VECTOR_VISUAL_COMPLETE_COMPONENT_REQUIRED",
                        "Vector outlined text and its frame/symbols must be routed as one complete visual component",
                        segment_id=segment_id,
                    )
                )
            if str(contract.get("group_id", "")) != str(visual.get("component_group_id", "")):
                issues.append(
                    issue(
                        "BLOCKING",
                        "VECTOR_VISUAL_GROUP_MISMATCH",
                        "Vector visual and segment component_group_id values differ",
                        segment_id=segment_id,
                    )
                )
        ui_entry = scoped_ui_entries.get((visual_id, str(segment.get("source_text", ""))))
        if ui_entry is None:
            issues.append(
                issue(
                    "BLOCKING",
                    "UI_ALLOWLIST_MISSING",
                    "Preserved image/UI source_text is not declared in the scoped allowlist",
                    segment_id=segment_id,
                )
            )
        elif require_translation and str(ui_entry.get("zh_TW", "")) != str(segment.get("zh_TW", "")):
            issues.append(
                issue(
                    "BLOCKING",
                    "UI_ALLOWLIST_TRANSLATION",
                    "Segment zh_TW differs from the scoped UI mapping",
                    segment_id=segment_id,
                )
            )
        guidance_ids = render.get("guidance_segment_ids") or []
        unknown_guidance = sorted(set(guidance_ids) - id_set)
        if unknown_guidance:
            issues.append(
                issue(
                    "BLOCKING",
                    "UNKNOWN_GUIDANCE_ID",
                    f"Unknown guidance IDs: {unknown_guidance}",
                    segment_id=segment_id,
                )
            )
    for (visual_id, source_text), entry in scoped_ui_entries.items():
        unknown_guidance = sorted(set(entry.get("guidance_segment_ids") or []) - id_set)
        if unknown_guidance:
            issues.append(
                issue(
                    "BLOCKING",
                    "UNKNOWN_UI_GUIDANCE_ID",
                    f"Unknown guidance IDs for {(visual_id, source_text)}: {unknown_guidance}",
                )
            )
    mapped_source: list[str] = []
    mapped_target: list[str] = []
    for mapping in manifest.get("mappings") or []:
        operation = mapping.get("operation")
        source_ids = mapping.get("source_ids") or []
        target_ids = mapping.get("target_ids") or []
        if operation not in {"one_to_one", "split", "merge"}:
            issues.append(issue("BLOCKING", "MAPPING_OPERATION", f"Invalid mapping operation: {operation!r}"))
        if operation == "one_to_one" and (len(source_ids) != 1 or len(target_ids) != 1):
            issues.append(issue("BLOCKING", "MAPPING_CARDINALITY", "one_to_one mapping must contain one source and target"))
        for identifier in source_ids + target_ids:
            if identifier not in id_set:
                issues.append(issue("BLOCKING", "MAPPING_UNKNOWN_ID", f"Unknown mapped ID: {identifier}"))
        mapped_source.extend(source_ids)
        mapped_target.extend(target_ids)

    unmapped_source = sorted(id_set - set(mapped_source))
    unmapped_target = sorted(id_set - set(mapped_target))
    duplicate_source = sorted(identifier for identifier, count in Counter(mapped_source).items() if count > 1)
    duplicate_target = sorted(identifier for identifier, count in Counter(mapped_target).items() if count > 1)
    if unmapped_source or unmapped_target:
        issues.append(
            issue(
                "BLOCKING",
                "UNMAPPED_ID",
                "Every segment must participate in an explicit mapping",
                evidence={"unmapped_source": unmapped_source, "unmapped_target": unmapped_target},
            )
        )
    if duplicate_source or duplicate_target:
        issues.append(
            issue(
                "BLOCKING",
                "DUPLICATE_MAPPING_ID",
                "A mapping ID was used more than once",
                evidence={"duplicate_source": duplicate_source, "duplicate_target": duplicate_target},
            )
        )

    coverage = manifest.get("coverage") or {}
    if coverage.get("unmapped_source_refs"):
        issues.append(
            issue("BLOCKING", "UNMAPPED_SOURCE_REF", "Source span coverage is incomplete", evidence=coverage["unmapped_source_refs"])
        )
    if coverage.get("duplicate_source_refs"):
        issues.append(
            issue("BLOCKING", "DUPLICATE_SOURCE_REF", "A source span is mapped more than once", evidence=coverage["duplicate_source_refs"])
        )

    for segment in segments:
        segment_id = segment.get("segment_id")
        relationships = segment.get("relationships") or {}
        for key, value in relationships.items():
            if key.endswith("_ids") and isinstance(value, list):
                unknown = sorted(set(value) - id_set)
                if unknown:
                    issues.append(
                        issue(
                            "BLOCKING",
                            "RELATIONSHIP_UNKNOWN_ID",
                            f"{key} references unknown IDs: {unknown}",
                            segment_id=segment_id,
                        )
                    )
            elif key.endswith("_id") and value and key not in {
                "context_id",
                "table_id",
                "group_id",
                "visual_id",
                "component_group_id",
            }:
                if value not in id_set:
                    issues.append(
                        issue(
                            "BLOCKING",
                            "RELATIONSHIP_UNKNOWN_ID",
                            f"{key} references unknown ID: {value}",
                            segment_id=segment_id,
                        )
                    )
    return issues
