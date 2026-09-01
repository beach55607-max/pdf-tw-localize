#!/usr/bin/env python3
"""Deterministic planning, resume, merge, and timing helpers for full mode."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from _segment_common import SCHEMA as SEGMENT_MANIFEST_SCHEMA
from _segment_common import sha256_file


PLAN_SCHEMA = "pdf-tw-localize/full-run-plan/v1"
DEPENDENCY_SCHEMA = "pdf-tw-localize/full-run-dependencies/v1"
BATCH_REQUEST_SCHEMA = "pdf-tw-localize/full-run-batch-request/v1"
BATCH_RESULT_SCHEMA = "pdf-tw-localize/full-run-batch-result/v1"
TIMING_SCHEMA = "pdf-tw-localize/full-run-timing/v1"
TRANSLATION_IMPORT_SCHEMA = "pdf-tw-localize/translation-import/v1"

HIGH_RISK_SEMANTIC_TYPES = {
    "warning",
    "table-cell",
    "UI",
    "image-text",
    "protected",
}
ALLOWED_TIMING_STAGES = {
    "secure_preflight",
    "inspect",
    "extract",
    "plan",
    "translate",
    "import",
    "rebuild",
    "machine_qa",
    "render_review",
    "visual_review",
    "delivery",
}
ALLOWED_TIMING_STATUSES = {"COMPLETED", "FAILED", "BLOCKED", "NOT_CHECKED"}


class FullRunError(ValueError):
    """Raised when a full-run artifact is stale, incomplete, or unsafe."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def digest_payload(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest().upper()


def validate_plan_integrity(plan: dict[str, Any]) -> None:
    """Reject a plan whose deterministic body or coverage has been changed."""

    if plan.get("schema") != PLAN_SCHEMA:
        raise FullRunError("Unsupported full-run plan schema")
    plan_core = {
        key: copy.deepcopy(value)
        for key, value in plan.items()
        if key not in {"created_at_utc", "plan_digest", "status"}
    }
    expected_digest = digest_payload(plan_core)
    if plan.get("plan_digest") != expected_digest:
        raise FullRunError(
            "Full-run plan digest mismatch; regenerate the plan and batch requests"
        )

    batches = plan.get("batches") or []
    if not isinstance(batches, list) or not batches:
        raise FullRunError("Full-run plan has no batches")
    batch_ids = [str(batch.get("batch_id", "")) for batch in batches if isinstance(batch, dict)]
    if len(batch_ids) != len(batches) or any(not value for value in batch_ids):
        raise FullRunError("Full-run plan batches must be objects with nonempty IDs")
    duplicate_batches = [name for name, count in Counter(batch_ids).items() if count > 1]
    if duplicate_batches:
        raise FullRunError(f"Duplicate full-run batch IDs: {sorted(duplicate_batches)}")

    owned_ids: list[str] = []
    for batch in batches:
        segment_ids = [str(value) for value in batch.get("owned_segment_ids") or []]
        records = batch.get("segment_records") or {}
        if set(segment_ids) != set(records):
            raise FullRunError(
                f"{batch.get('batch_id')} segment records differ from owned segment IDs"
            )
        owned_ids.extend(segment_ids)
    duplicate_segments = [name for name, count in Counter(owned_ids).items() if count > 1]
    if duplicate_segments:
        raise FullRunError(
            f"Full-run segments are owned by multiple batches: {sorted(duplicate_segments)}"
        )
    segment_order = [str(value) for value in plan.get("segment_order") or []]
    if len(segment_order) != len(set(segment_order)):
        raise FullRunError("Full-run manifest segment order contains duplicate IDs")
    if set(owned_ids) != set(segment_order):
        raise FullRunError("Full-run batch coverage differs from the manifest segment order")


def read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FullRunError(f"Cannot read JSON object {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise FullRunError(f"Expected a JSON object: {path}")
    return value


def write_json_new(path: Path, value: Any) -> None:
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite existing output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    if temporary.exists():
        raise FullRunError(f"Temporary output already exists: {temporary}")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


class _UnionFind:
    def __init__(self, values: Iterable[int]) -> None:
        self.parent = {value: value for value in values}

    def find(self, value: int) -> int:
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, first: int, second: int) -> None:
        root_first = self.find(first)
        root_second = self.find(second)
        if root_first == root_second:
            return
        low, high = sorted((root_first, root_second))
        self.parent[high] = low


def _as_page(value: Any, selected_pages: set[int], field: str) -> int:
    try:
        page = int(value)
    except (TypeError, ValueError) as exc:
        raise FullRunError(f"{field} is not an integer page: {value!r}") from exc
    if page not in selected_pages:
        raise FullRunError(f"{field} references an unselected page: {page}")
    return page


def _walk_relation_tokens(value: Any, field_name: str = "") -> Iterable[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _walk_relation_tokens(item, str(key))
    elif isinstance(value, list):
        for item in value:
            yield from _walk_relation_tokens(item, field_name)
    elif isinstance(value, str) and (
        field_name.endswith("_id") or field_name.endswith("_ids")
    ):
        token = value.strip()
        if token:
            yield token


def _source_segment_digest(segment: dict[str, Any]) -> str:
    evidence = {
        "segment_id": segment.get("segment_id"),
        "page": segment.get("page"),
        "semantic_type": segment.get("semantic_type"),
        "source_text": segment.get("source_text"),
        "protected_tokens": segment.get("protected_tokens") or [],
        "relationships": segment.get("relationships") or {},
        "semantic_bindings": segment.get("semantic_bindings") or [],
        "render_action": (segment.get("render") or {}).get("action", "replace"),
    }
    return digest_payload(evidence)


def _protected_token_requirements(segment: dict[str, Any]) -> list[dict[str, Any]]:
    source_text = str(segment.get("source_text", ""))
    required: list[dict[str, Any]] = []
    for item in segment.get("protected_tokens") or []:
        if isinstance(item, dict):
            source_token = str(item.get("token", ""))
            token = str(item.get("target_token", source_token))
            raw_count = item.get("source_count", source_text.count(source_token))
        else:
            source_token = str(item)
            token = source_token
            raw_count = source_text.count(source_token)
        try:
            required_count = int(raw_count)
        except (TypeError, ValueError) as exc:
            raise FullRunError(
                f"Invalid protected token count for {segment.get('segment_id')}: {raw_count!r}"
            ) from exc
        if not source_token or not token or required_count < 1:
            raise FullRunError(
                f"Invalid protected token declaration for {segment.get('segment_id')}: {item!r}"
            )
        required.append(
            {
                "source_token": source_token,
                "target_token": token,
                "required_count": required_count,
            }
        )
    return required


def _semantic_binding_ids(segment: dict[str, Any]) -> list[str]:
    identifiers: list[str] = []
    for binding in segment.get("semantic_bindings") or []:
        if not isinstance(binding, dict):
            raise FullRunError(
                f"Semantic binding must be an object for {segment.get('segment_id')}"
            )
        binding_id = str(binding.get("binding_id", "")).strip()
        if not binding_id:
            raise FullRunError(
                f"Semantic binding ID is empty for {segment.get('segment_id')}"
            )
        identifiers.append(binding_id)
    duplicates = [name for name, count in Counter(identifiers).items() if count > 1]
    if duplicates:
        raise FullRunError(
            f"Duplicate semantic binding IDs for {segment.get('segment_id')}: "
            f"{sorted(duplicates)}"
        )
    return identifiers


def _translation_segment(segment: dict[str, Any]) -> dict[str, Any]:
    copied = copy.deepcopy(segment)
    copied["source_segment_sha256"] = _source_segment_digest(segment)
    copied["zh_TW"] = ""
    copied["status"] = "EXTRACTED"
    copied["translation_assertions"] = []
    copied.pop("translation_note", None)
    return copied


def _context_only_segment(segment: dict[str, Any]) -> dict[str, Any]:
    return {
        "segment_id": segment.get("segment_id"),
        "page": segment.get("page"),
        "semantic_type": segment.get("semantic_type"),
        "source_text": segment.get("source_text"),
        "protected_tokens": copy.deepcopy(segment.get("protected_tokens") or []),
        "relationships": copy.deepcopy(segment.get("relationships") or {}),
        "semantic_bindings": copy.deepcopy(segment.get("semantic_bindings") or []),
        "context_only": True,
    }


def _load_glossaries(paths: list[Path]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    payloads: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in paths:
        resolved = path.resolve()
        digest = sha256_file(resolved)
        if digest in seen:
            raise FullRunError(f"Duplicate glossary content: {resolved}")
        seen.add(digest)
        try:
            content = resolved.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise FullRunError(f"Glossary is not readable UTF-8 text: {resolved}") from exc
        record = {"name": resolved.name, "sha256": digest}
        records.append(record)
        payloads.append({**record, "content_utf8": content})
    records.sort(key=lambda item: (item["name"].casefold(), item["sha256"]))
    payloads.sort(key=lambda item: (item["name"].casefold(), item["sha256"]))
    return records, payloads


def _dependency_groups(
    manifest: dict[str, Any],
    dependency_spec: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], set[int]]:
    selected = sorted({int(page) for page in manifest.get("selected_pages") or []})
    if not selected:
        raise FullRunError("Manifest selected_pages is empty")
    selected_set = set(selected)
    union = _UnionFind(selected)
    edges: list[tuple[int, int, str]] = []
    explicit_high_risk_pages: set[int] = set()

    def connect(first: int, second: int, reason: str) -> None:
        if first == second:
            return
        union.union(first, second)
        edges.append((first, second, reason))

    contexts = manifest.get("page_contexts") or []
    context_page_by_id: dict[str, int] = {}
    for context in contexts:
        if not isinstance(context, dict):
            raise FullRunError("page_contexts entries must be objects")
        page = _as_page(context.get("page"), selected_set, "page_contexts.page")
        context_id = str(context.get("context_id", "")).strip()
        if context_id:
            if context_id in context_page_by_id:
                raise FullRunError(f"Duplicate page context ID: {context_id}")
            context_page_by_id[context_id] = page
        for ref in context.get("document_context_refs") or []:
            if not isinstance(ref, dict):
                raise FullRunError("document_context_refs entries must be objects")
            target = _as_page(ref.get("page"), selected_set, "document_context_refs.page")
            connect(page, target, f"document_context_ref:{ref.get('context_ref_id', '')}")

    segments = manifest.get("segments") or []
    segment_page_by_id: dict[str, int] = {}
    relation_pages: dict[str, set[int]] = defaultdict(set)
    for segment in segments:
        if not isinstance(segment, dict):
            raise FullRunError("segments entries must be objects")
        segment_id = str(segment.get("segment_id", "")).strip()
        if not segment_id:
            raise FullRunError("Every segment requires segment_id")
        if segment_id in segment_page_by_id:
            raise FullRunError(f"Duplicate segment ID: {segment_id}")
        page = _as_page(segment.get("page"), selected_set, f"{segment_id}.page")
        segment_page_by_id[segment_id] = page
        for token in _walk_relation_tokens(segment.get("relationships") or {}):
            relation_pages[token].add(page)
        for binding in segment.get("semantic_bindings") or []:
            if not isinstance(binding, dict):
                raise FullRunError(f"{segment_id} semantic binding must be an object")
            for context_id in binding.get("context_ref_ids") or []:
                target = context_page_by_id.get(str(context_id))
                if target is not None:
                    connect(page, target, f"semantic_context_ref:{context_id}")

    for token, pages in relation_pages.items():
        ordered = sorted(pages)
        for page in ordered[1:]:
            connect(ordered[0], page, f"shared_relationship:{token}")

    for segment in segments:
        page = int(segment["page"])
        for token in _walk_relation_tokens(segment.get("relationships") or {}):
            if token in segment_page_by_id:
                connect(page, segment_page_by_id[token], f"segment_reference:{token}")
            elif token in context_page_by_id:
                connect(page, context_page_by_id[token], f"context_reference:{token}")

    if dependency_spec is not None:
        if dependency_spec.get("schema") != DEPENDENCY_SCHEMA:
            raise FullRunError("Unsupported full-run dependency schema")
        groups = dependency_spec.get("groups") or []
        identifiers: list[str] = []
        for group in groups:
            if not isinstance(group, dict):
                raise FullRunError("Dependency groups must be objects")
            group_id = str(group.get("group_id", "")).strip()
            if not group_id:
                raise FullRunError("Dependency group requires group_id")
            identifiers.append(group_id)
            pages = [
                _as_page(page, selected_set, f"dependency group {group_id}")
                for page in group.get("pages") or []
            ]
            if not pages:
                raise FullRunError(f"Dependency group {group_id} has no pages")
            reason = str(group.get("reason", "")).strip()
            if not reason:
                raise FullRunError(f"Dependency group {group_id} requires a reason")
            for page in pages[1:]:
                connect(pages[0], page, f"explicit:{group_id}:{reason}")
            risk = str(group.get("risk", "standard"))
            if risk not in {"standard", "high"}:
                raise FullRunError(f"Dependency group {group_id} has invalid risk")
            if risk == "high":
                explicit_high_risk_pages.update(pages)
        duplicates = [name for name, count in Counter(identifiers).items() if count > 1]
        if duplicates:
            raise FullRunError(f"Duplicate dependency group IDs: {sorted(duplicates)}")

    components: dict[int, list[int]] = defaultdict(list)
    for page in selected:
        components[union.find(page)].append(page)
    groups_out: list[dict[str, Any]] = []
    for index, pages in enumerate(sorted(components.values(), key=lambda item: min(item)), start=1):
        page_set = set(pages)
        reasons = sorted(
            {
                reason
                for first, second, reason in edges
                if first in page_set and second in page_set
            }
        )
        groups_out.append(
            {
                "group_id": f"semantic-group-{index:04d}",
                "pages": sorted(pages),
                "dependency_reasons": reasons,
            }
        )
    return groups_out, explicit_high_risk_pages


def _group_is_high_risk(
    pages: set[int],
    segments_by_page: dict[int, list[dict[str, Any]]],
    contexts_by_page: dict[int, dict[str, Any]],
    explicit_high_risk_pages: set[int],
) -> bool:
    if pages & explicit_high_risk_pages:
        return True
    for page in pages:
        context = contexts_by_page.get(page) or {}
        if context.get("preserved_visuals") or context.get("repeated_component_layouts"):
            return True
        for segment in segments_by_page.get(page) or []:
            if segment.get("semantic_type") in HIGH_RISK_SEMANTIC_TYPES:
                return True
            if segment.get("semantic_bindings") or segment.get("component_contract"):
                return True
    return False


def _pack_groups(
    groups: list[dict[str, Any]],
    segments_by_page: dict[int, list[dict[str, Any]]],
    contexts_by_page: dict[int, dict[str, Any]],
    explicit_high_risk_pages: set[int],
    target_segments: int,
    max_batch_pages: int,
) -> list[dict[str, Any]]:
    if target_segments < 1 or max_batch_pages < 1:
        raise FullRunError("Batch limits must be positive")
    packed: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    current_pages: set[int] = set()
    current_segments = 0

    def flush() -> None:
        nonlocal current, current_pages, current_segments
        if not current:
            return
        packed.append(
            {
                "groups": copy.deepcopy(current),
                "pages": sorted(current_pages),
                "segment_count": current_segments,
                "risk": (
                    "high_context"
                    if _group_is_high_risk(
                        current_pages,
                        segments_by_page,
                        contexts_by_page,
                        explicit_high_risk_pages,
                    )
                    else "standard_context"
                ),
                "dependency_group_exceeds_soft_limit": (
                    len(current_pages) > max_batch_pages
                    or current_segments > target_segments
                ),
            }
        )
        current = []
        current_pages = set()
        current_segments = 0

    for group in groups:
        pages = set(int(page) for page in group["pages"])
        count = sum(len(segments_by_page.get(page) or []) for page in pages)
        next_pages = current_pages | pages
        if current and (
            len(next_pages) > max_batch_pages
            or current_segments + count > target_segments
        ):
            flush()
        current.append(group)
        current_pages.update(pages)
        current_segments += count
    flush()
    return packed


def _assign_parallel_waves(batches: list[dict[str, Any]], max_parallel: int) -> None:
    if max_parallel < 1:
        raise FullRunError("max_parallel_batches must be positive")
    waves: list[dict[str, int]] = []
    for batch in batches:
        is_high = batch["risk"] == "high_context"
        selected_wave: int | None = None
        for index, wave in enumerate(waves):
            if wave["count"] >= max_parallel:
                continue
            if is_high and wave["high"] >= 1:
                continue
            selected_wave = index
            break
        if selected_wave is None:
            waves.append({"count": 0, "high": 0})
            selected_wave = len(waves) - 1
        waves[selected_wave]["count"] += 1
        if is_high:
            waves[selected_wave]["high"] += 1
        batch["parallel_wave"] = selected_wave + 1


def create_plan(
    manifest_path: Path,
    *,
    validation_report_path: Path,
    glossary_paths: list[Path] | None = None,
    dependency_spec_path: Path | None = None,
    target_segments: int = 80,
    max_batch_pages: int = 8,
    context_pages: int = 1,
    max_parallel_batches: int = 4,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    manifest_path = manifest_path.resolve()
    manifest = read_json_object(manifest_path)
    if manifest.get("schema") != SEGMENT_MANIFEST_SCHEMA:
        raise FullRunError("Unsupported segment manifest schema")
    if manifest.get("status") not in {"EXTRACTED", "VALIDATED"}:
        raise FullRunError("Full-run planning requires an extracted, untranslated manifest")
    selected_pages = sorted({int(page) for page in manifest.get("selected_pages") or []})
    if context_pages < 0:
        raise FullRunError("context_pages cannot be negative")
    manifest_sha256 = sha256_file(manifest_path)
    validation_report_path = validation_report_path.resolve()
    validation_report = read_json_object(validation_report_path)
    if validation_report.get("schema") != "pdf-tw-localize/segment-validation/v1":
        raise FullRunError("Unsupported extraction validation report schema")
    if validation_report.get("stage") != "extraction":
        raise FullRunError("Full-run planning requires an extraction-stage validation report")
    if validation_report.get("manifest_sha256") != manifest_sha256:
        raise FullRunError("Extraction validation report is stale for this manifest")
    try:
        validation_blocking = int(validation_report.get("blocking_issue_count", -1))
        validation_needs_review = int(
            validation_report.get("needs_review_issue_count", -1)
        )
    except (TypeError, ValueError) as exc:
        raise FullRunError("Extraction validation issue counts are invalid") from exc
    if (
        validation_report.get("status") != "PASS"
        or validation_blocking != 0
        or validation_needs_review != 0
    ):
        raise FullRunError(
            "Extraction validation must be PASS with zero blocking or needs-review issues"
        )
    validation_record = {
        "name": validation_report_path.name,
        "sha256": sha256_file(validation_report_path),
        "stage": "extraction",
        "status": "PASS",
    }
    glossary_records, glossary_payloads = _load_glossaries(glossary_paths or [])
    dependency_spec: dict[str, Any] | None = None
    dependency_record: dict[str, Any] | None = None
    if dependency_spec_path is not None:
        resolved_dependency = dependency_spec_path.resolve()
        dependency_spec = read_json_object(resolved_dependency)
        dependency_record = {
            "name": resolved_dependency.name,
            "sha256": sha256_file(resolved_dependency),
        }

    segments = manifest.get("segments") or []
    segment_ids = [str(segment.get("segment_id", "")) for segment in segments]
    duplicates = [name for name, count in Counter(segment_ids).items() if count > 1]
    if duplicates:
        raise FullRunError(f"Duplicate segment IDs: {sorted(duplicates)}")
    if not segment_ids or any(not name for name in segment_ids):
        raise FullRunError("Manifest must contain nonempty segment IDs")
    segments_by_page: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for segment in segments:
        page = int(segment.get("page"))
        if page not in selected_pages:
            raise FullRunError(f"Segment references unselected page: {segment.get('segment_id')}")
        segments_by_page[page].append(segment)
    for page in segments_by_page:
        segments_by_page[page].sort(
            key=lambda item: (int(item.get("reading_order", 0)), str(item.get("segment_id")))
        )

    contexts_by_page: dict[int, dict[str, Any]] = {}
    for context in manifest.get("page_contexts") or []:
        page = int(context.get("page"))
        if page in contexts_by_page:
            raise FullRunError(f"Duplicate page context for page {page}")
        contexts_by_page[page] = context
    missing_contexts = sorted(set(selected_pages) - set(contexts_by_page))
    if missing_contexts:
        raise FullRunError(f"Missing page contexts: {missing_contexts}")

    dependency_groups, explicit_high = _dependency_groups(manifest, dependency_spec)
    packed = _pack_groups(
        dependency_groups,
        segments_by_page,
        contexts_by_page,
        explicit_high,
        target_segments,
        max_batch_pages,
    )
    _assign_parallel_waves(packed, max_parallel_batches)

    selected_set = set(selected_pages)
    requests: dict[str, dict[str, Any]] = {}
    plan_batches: list[dict[str, Any]] = []
    for index, packed_batch in enumerate(packed, start=1):
        batch_id = f"batch-{index:04d}"
        owned_pages = list(packed_batch["pages"])
        owned_set = set(owned_pages)
        context_set: set[int] = set()
        for page in owned_pages:
            for candidate in range(page - context_pages, page + context_pages + 1):
                if candidate in selected_set and candidate not in owned_set:
                    context_set.add(candidate)
        owned_segments = [
            _translation_segment(segment)
            for page in owned_pages
            for segment in segments_by_page.get(page) or []
        ]
        context_only_segments = [
            _context_only_segment(segment)
            for page in sorted(context_set)
            for segment in segments_by_page.get(page) or []
        ]
        request_core = {
            "schema": BATCH_REQUEST_SCHEMA,
            "source_manifest_sha256": manifest_sha256,
            "document_id": manifest.get("document_id"),
            "document_context": copy.deepcopy(manifest.get("document_context") or {}),
            "batch_id": batch_id,
            "risk": packed_batch["risk"],
            "owned_pages": owned_pages,
            "context_only_pages": sorted(context_set),
            "page_contexts": [copy.deepcopy(contexts_by_page[page]) for page in owned_pages],
            "context_only_page_contexts": [
                copy.deepcopy(contexts_by_page[page]) for page in sorted(context_set)
            ],
            "segments": owned_segments,
            "context_only_segments": context_only_segments,
            "translation_contract": copy.deepcopy(manifest.get("translation_contract") or {}),
            "glossaries": copy.deepcopy(glossary_payloads),
            "required_output": {
                "schema": BATCH_RESULT_SCHEMA,
                "return_only_owned_segment_ids": True,
                "preserve_stable_ids": True,
                "preserve_protected_tokens": True,
                "include_source_segment_sha256": True,
                "include_request_sha256": True,
                "include_translation_assertions": True,
                "commentary_or_markdown_forbidden": True,
            },
        }
        batch_digest = digest_payload(request_core)
        request_core["batch_digest"] = batch_digest
        requests[batch_id] = request_core
        segment_records = {
            str(segment["segment_id"]): {
                "source_segment_sha256": _source_segment_digest(segment),
                "protected_token_requirements": _protected_token_requirements(segment),
                "semantic_binding_ids": _semantic_binding_ids(segment),
                "render_action": (segment.get("render") or {}).get("action", "replace"),
            }
            for page in owned_pages
            for segment in segments_by_page.get(page) or []
        }
        plan_batches.append(
            {
                "batch_id": batch_id,
                "batch_digest": batch_digest,
                "request_filename": f"{batch_id}.request.json",
                "result_filename": f"{batch_id}.result.json",
                "owned_pages": owned_pages,
                "context_only_pages": sorted(context_set),
                "owned_segment_ids": [segment["segment_id"] for segment in owned_segments],
                "segment_records": segment_records,
                "dependency_groups": copy.deepcopy(packed_batch["groups"]),
                "dependency_group_exceeds_soft_limit": packed_batch[
                    "dependency_group_exceeds_soft_limit"
                ],
                "risk": packed_batch["risk"],
                "parallel_wave": packed_batch["parallel_wave"],
            }
        )

    plan_core = {
        "schema": PLAN_SCHEMA,
        "source_manifest": {
            "name": manifest_path.name,
            "sha256": manifest_sha256,
            "document_id": manifest.get("document_id"),
            "source_pdf_sha256": (manifest.get("source") or {}).get("sha256"),
        },
        "extraction_validation": validation_record,
        "selected_pages": selected_pages,
        "segment_order": segment_ids,
        "glossaries": glossary_records,
        "dependency_spec": dependency_record,
        "configuration": {
            "target_segments_per_batch": target_segments,
            "max_batch_pages_soft_limit": max_batch_pages,
            "context_neighbor_pages": context_pages,
            "max_parallel_batches": max_parallel_batches,
            "high_context_batches_per_wave": 1,
        },
        "batches": plan_batches,
        "quality_gates": {
            "source_generated_candidate_required": True,
            "full_document_machine_qa_required": True,
            "semantic_qa_required_when_declared": True,
            "individual_visual_review_required": True,
            "cached_qa_pass_forbidden": True,
            "cached_user_acceptance_forbidden": True,
            "user_acceptance": "NOT_CHECKED",
        },
    }
    plan_digest = digest_payload(plan_core)
    plan = {
        **plan_core,
        "created_at_utc": utc_now(),
        "plan_digest": plan_digest,
        "status": "PLANNED",
    }
    return plan, requests


def write_plan_bundle(
    plan_path: Path,
    requests_dir: Path,
    plan: dict[str, Any],
    requests: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    plan_path = plan_path.resolve()
    requests_dir = requests_dir.resolve()
    validate_plan_integrity(plan)
    if plan_path == requests_dir or plan_path.is_relative_to(requests_dir):
        raise FullRunError("Plan output must be outside the requests directory")
    if requests_dir.exists():
        raise FileExistsError(f"Refusing existing requests directory: {requests_dir}")
    write_json_new(plan_path, plan)
    plan_sha256 = sha256_file(plan_path)
    requests_dir.mkdir(parents=True, exist_ok=False)
    request_records: list[dict[str, Any]] = []
    for batch in plan["batches"]:
        batch_id = str(batch["batch_id"])
        request = {
            **copy.deepcopy(requests[batch_id]),
            "plan_digest": plan["plan_digest"],
            "plan_sha256": plan_sha256,
        }
        path = requests_dir / str(batch["request_filename"])
        write_json_new(path, request)
        request_records.append(
            {
                "batch_id": batch_id,
                "path": str(path),
                "sha256": sha256_file(path),
                "batch_digest": batch["batch_digest"],
            }
        )
    return {
        "schema": "pdf-tw-localize/full-run-plan-write/v1",
        "status": "PLANNED",
        "plan": str(plan_path),
        "plan_sha256": plan_sha256,
        "plan_digest": plan["plan_digest"],
        "batch_count": len(plan["batches"]),
        "requests": request_records,
        "user_acceptance": "NOT_CHECKED",
    }


def _validate_timing(value: Any, batch_id: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise FullRunError(f"{batch_id} timing must be an object")
    try:
        elapsed = float(value.get("elapsed_seconds"))
    except (TypeError, ValueError) as exc:
        raise FullRunError(f"{batch_id} timing elapsed_seconds is invalid") from exc
    if not math.isfinite(elapsed) or elapsed < 0:
        raise FullRunError(f"{batch_id} timing elapsed_seconds must be finite and non-negative")
    return {
        "started_at_utc": value.get("started_at_utc"),
        "completed_at_utc": value.get("completed_at_utc"),
        "elapsed_seconds": elapsed,
    }


def validate_batch_request(
    plan: dict[str, Any],
    plan_path: Path,
    batch: dict[str, Any],
    request_path: Path,
) -> str:
    batch_id = str(batch["batch_id"])
    request = read_json_object(request_path)
    bindings = {
        "schema": BATCH_REQUEST_SCHEMA,
        "plan_sha256": sha256_file(plan_path.resolve()),
        "plan_digest": plan.get("plan_digest"),
        "source_manifest_sha256": (plan.get("source_manifest") or {}).get("sha256"),
        "batch_id": batch_id,
        "batch_digest": batch.get("batch_digest"),
    }
    for field, expected in bindings.items():
        if request.get(field) != expected:
            raise FullRunError(
                f"{batch_id} request binding mismatch for {field}: "
                f"expected {expected!r}, got {request.get(field)!r}"
            )
    request_core = copy.deepcopy(request)
    request_core.pop("plan_sha256", None)
    request_core.pop("plan_digest", None)
    request_core.pop("batch_digest", None)
    if digest_payload(request_core) != batch.get("batch_digest"):
        raise FullRunError(f"{batch_id} request content digest mismatch")
    request_ids = [
        str(segment.get("segment_id", ""))
        for segment in request.get("segments") or []
        if isinstance(segment, dict)
    ]
    if request_ids != list(batch.get("owned_segment_ids") or []):
        raise FullRunError(f"{batch_id} request ownership differs from the plan")
    return sha256_file(request_path)


def validate_batch_result(
    plan: dict[str, Any],
    plan_path: Path,
    batch: dict[str, Any],
    result: dict[str, Any],
    request_sha256: str,
) -> dict[str, Any]:
    batch_id = str(batch["batch_id"])
    if result.get("schema") != BATCH_RESULT_SCHEMA:
        raise FullRunError(f"{batch_id} result has unsupported schema")
    if result.get("status") != "TRANSLATED":
        raise FullRunError(f"{batch_id} result status must be TRANSLATED")
    expected_plan_sha256 = sha256_file(plan_path.resolve())
    bindings = {
        "plan_sha256": expected_plan_sha256,
        "plan_digest": plan.get("plan_digest"),
        "source_manifest_sha256": (plan.get("source_manifest") or {}).get("sha256"),
        "batch_id": batch_id,
        "batch_digest": batch.get("batch_digest"),
        "request_sha256": request_sha256,
    }
    for field, expected in bindings.items():
        if result.get(field) != expected:
            raise FullRunError(
                f"{batch_id} result binding mismatch for {field}: "
                f"expected {expected!r}, got {result.get(field)!r}"
            )
    translations = result.get("translations") or []
    if not isinstance(translations, list):
        raise FullRunError(f"{batch_id} translations must be a list")
    identifiers = [str(entry.get("segment_id", "")) for entry in translations if isinstance(entry, dict)]
    if len(identifiers) != len(translations):
        raise FullRunError(f"{batch_id} translations entries must be objects")
    duplicates = [name for name, count in Counter(identifiers).items() if count > 1]
    if duplicates:
        raise FullRunError(f"{batch_id} duplicate translation IDs: {sorted(duplicates)}")
    expected_ids = list(batch.get("owned_segment_ids") or [])
    if set(identifiers) != set(expected_ids):
        raise FullRunError(
            f"{batch_id} translation coverage mismatch: "
            f"missing={sorted(set(expected_ids) - set(identifiers))} "
            f"unexpected={sorted(set(identifiers) - set(expected_ids))}"
        )
    records = batch.get("segment_records") or {}
    normalized: list[dict[str, Any]] = []
    by_id = {str(entry["segment_id"]): entry for entry in translations}
    for segment_id in expected_ids:
        entry = by_id[segment_id]
        record = records.get(segment_id) or {}
        if entry.get("source_segment_sha256") != record.get("source_segment_sha256"):
            raise FullRunError(f"{batch_id} stale source binding for {segment_id}")
        zh_text = str(entry.get("zh_TW", ""))
        if record.get("render_action") != "preserve" and not zh_text.strip():
            raise FullRunError(f"{batch_id} empty translation for {segment_id}")
        for requirement in record.get("protected_token_requirements") or []:
            token = str(requirement["target_token"])
            required_count = int(requirement["required_count"])
            actual_count = zh_text.count(token)
            if actual_count < required_count:
                raise FullRunError(
                    f"{batch_id} protected token missing for {segment_id}: "
                    f"{token} expected={required_count} actual={actual_count}"
                )
        if "translation_assertions" not in entry:
            raise FullRunError(
                f"{batch_id} translation_assertions is required for {segment_id}"
            )
        assertions = entry.get("translation_assertions")
        if not isinstance(assertions, list):
            raise FullRunError(f"{batch_id} assertions must be a list for {segment_id}")
        if any(not isinstance(assertion, dict) for assertion in assertions):
            raise FullRunError(f"{batch_id} assertions must contain objects for {segment_id}")
        assertion_ids = [str(assertion.get("binding_id", "")) for assertion in assertions]
        duplicate_assertions = [
            name for name, count in Counter(assertion_ids).items() if count > 1
        ]
        if duplicate_assertions:
            raise FullRunError(
                f"{batch_id} duplicate assertion IDs for {segment_id}: "
                f"{sorted(duplicate_assertions)}"
            )
        expected_assertion_ids = [
            str(value) for value in record.get("semantic_binding_ids") or []
        ]
        if set(assertion_ids) != set(expected_assertion_ids):
            raise FullRunError(
                f"{batch_id} assertion coverage mismatch for {segment_id}: "
                f"missing={sorted(set(expected_assertion_ids) - set(assertion_ids))} "
                f"unexpected={sorted(set(assertion_ids) - set(expected_assertion_ids))}"
            )
        normalized.append(
            {
                "segment_id": segment_id,
                "zh_TW": zh_text,
                "note": str(entry.get("note", "")),
                "translation_assertions": copy.deepcopy(assertions),
                "source_segment_sha256": record.get("source_segment_sha256"),
            }
        )
    timing = _validate_timing(result.get("timing"), batch_id)
    return {
        "batch_id": batch_id,
        "translations": normalized,
        "translator": str(result.get("translator", "context-capable language model")),
        "translated_at_utc": result.get("translated_at_utc"),
        "timing": timing,
    }


def status_report(
    plan_path: Path,
    requests_dir: Path,
    results_dir: Path,
) -> dict[str, Any]:
    plan_path = plan_path.resolve()
    requests_dir = requests_dir.resolve()
    results_dir = results_dir.resolve()
    plan = read_json_object(plan_path)
    validate_plan_integrity(plan)
    completed: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    wave_durations: dict[int, list[float]] = defaultdict(list)
    total_translation_seconds = 0.0
    for batch in plan.get("batches") or []:
        request_path = requests_dir / str(batch["request_filename"])
        try:
            request_sha256 = validate_batch_request(
                plan, plan_path, batch, request_path
            )
        except (FullRunError, OSError) as exc:
            blocked.append(
                {
                    "batch_id": batch["batch_id"],
                    "request_path": str(request_path),
                    "error": str(exc),
                }
            )
            continue
        result_path = results_dir / str(batch["result_filename"])
        if not result_path.exists():
            pending.append(
                {
                    "batch_id": batch["batch_id"],
                    "result_path": str(result_path),
                    "parallel_wave": batch["parallel_wave"],
                }
            )
            continue
        try:
            result = read_json_object(result_path)
            normalized = validate_batch_result(
                plan, plan_path, batch, result, request_sha256
            )
        except (FullRunError, OSError, json.JSONDecodeError) as exc:
            blocked.append(
                {
                    "batch_id": batch["batch_id"],
                    "result_path": str(result_path),
                    "error": str(exc),
                }
            )
            continue
        timing = normalized.get("timing")
        elapsed = float(timing["elapsed_seconds"]) if timing else 0.0
        total_translation_seconds += elapsed
        wave_durations[int(batch["parallel_wave"])].append(elapsed)
        completed.append(
            {
                "batch_id": batch["batch_id"],
                "result_path": str(result_path),
                "result_sha256": sha256_file(result_path),
                "request_sha256": request_sha256,
                "parallel_wave": batch["parallel_wave"],
                "risk": batch["risk"],
                "elapsed_seconds": elapsed if timing else None,
            }
        )
    observed_parallel_seconds = sum(
        max(values) for _, values in sorted(wave_durations.items()) if values
    )
    status = "BLOCKED" if blocked else ("COMPLETE" if not pending else "IN_PROGRESS")
    return {
        "schema": "pdf-tw-localize/full-run-status/v1",
        "status": status,
        "plan": str(plan_path),
        "plan_sha256": sha256_file(plan_path),
        "plan_digest": plan.get("plan_digest"),
        "batch_count": len(plan.get("batches") or []),
        "completed_count": len(completed),
        "pending_count": len(pending),
        "blocked_count": len(blocked),
        "completed": completed,
        "pending": pending,
        "blocked": blocked,
        "timing": {
            "translation_worker_seconds_observed": total_translation_seconds,
            "translation_parallel_wall_seconds_observed": observed_parallel_seconds,
            "timing_complete_for_every_batch": all(
                item["elapsed_seconds"] is not None for item in completed
            ) and not pending,
        },
        "machine_qa": "NOT_CHECKED",
        "visual_review": "NOT_CHECKED",
        "user_acceptance": "NOT_CHECKED",
    }


def merge_results(
    plan_path: Path,
    requests_dir: Path,
    results_dir: Path,
) -> dict[str, Any]:
    plan_path = plan_path.resolve()
    requests_dir = requests_dir.resolve()
    results_dir = results_dir.resolve()
    plan = read_json_object(plan_path)
    validate_plan_integrity(plan)
    status = status_report(plan_path, requests_dir, results_dir)
    if status["status"] != "COMPLETE":
        raise FullRunError(
            f"Cannot merge incomplete or blocked run: pending={status['pending_count']} "
            f"blocked={status['blocked_count']}"
        )
    translations_by_id: dict[str, dict[str, Any]] = {}
    translators: set[str] = set()
    translated_times: list[str] = []
    result_records: list[dict[str, Any]] = []
    for batch in plan.get("batches") or []:
        result_path = results_dir / str(batch["result_filename"])
        request_path = requests_dir / str(batch["request_filename"])
        request_sha256 = validate_batch_request(plan, plan_path, batch, request_path)
        result = read_json_object(result_path)
        normalized = validate_batch_result(
            plan, plan_path, batch, result, request_sha256
        )
        translators.add(normalized["translator"])
        if normalized.get("translated_at_utc"):
            translated_times.append(str(normalized["translated_at_utc"]))
        result_records.append(
            {
                "batch_id": batch["batch_id"],
                "batch_digest": batch["batch_digest"],
                "request_sha256": request_sha256,
                "result_sha256": sha256_file(result_path),
            }
        )
        for entry in normalized["translations"]:
            segment_id = str(entry["segment_id"])
            if segment_id in translations_by_id:
                raise FullRunError(f"Cross-batch duplicate segment ID: {segment_id}")
            translations_by_id[segment_id] = entry
    order = list(plan.get("segment_order") or [])
    if set(order) != set(translations_by_id):
        raise FullRunError("Merged translation coverage differs from plan segment order")
    translations = [
        {
            "segment_id": segment_id,
            "zh_TW": translations_by_id[segment_id]["zh_TW"],
            "note": translations_by_id[segment_id]["note"],
            "translation_assertions": translations_by_id[segment_id][
                "translation_assertions"
            ],
        }
        for segment_id in order
    ]
    return {
        "schema": TRANSLATION_IMPORT_SCHEMA,
        "source_manifest_sha256": plan["source_manifest"]["sha256"],
        "translator": "; ".join(sorted(translators)) or "context-capable language model",
        "translated_at_utc": max(translated_times) if translated_times else utc_now(),
        "translations": translations,
        "full_run": {
            "schema": "pdf-tw-localize/full-run-merge-evidence/v1",
            "plan_sha256": sha256_file(plan_path),
            "plan_digest": plan["plan_digest"],
            "batch_results": result_records,
            "resume_validation": "PASS",
            "coverage_validation": "PASS",
            "cached_qa_pass_used": False,
            "user_acceptance": "NOT_CHECKED",
        },
    }


def _parse_timestamp(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FullRunError(f"{field} is not an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise FullRunError(f"{field} must include a timezone")
    return parsed


def record_stage_timing(
    plan_path: Path,
    ledger_path: Path,
    *,
    stage: str,
    attempt_id: str,
    started_at_utc: str,
    completed_at_utc: str,
    status: str,
    note: str = "",
) -> dict[str, Any]:
    plan_path = plan_path.resolve()
    ledger_path = ledger_path.resolve()
    if stage not in ALLOWED_TIMING_STAGES:
        raise FullRunError(f"Unsupported timing stage: {stage}")
    if status not in ALLOWED_TIMING_STATUSES:
        raise FullRunError(f"Unsupported timing status: {status}")
    attempt_id = attempt_id.strip()
    if not attempt_id:
        raise FullRunError("attempt_id cannot be empty")
    started = _parse_timestamp(started_at_utc, "started_at_utc")
    completed = _parse_timestamp(completed_at_utc, "completed_at_utc")
    elapsed = (completed - started).total_seconds()
    if elapsed < 0:
        raise FullRunError("completed_at_utc precedes started_at_utc")
    plan = read_json_object(plan_path)
    validate_plan_integrity(plan)
    plan_sha256 = sha256_file(plan_path)
    if ledger_path.exists():
        ledger = read_json_object(ledger_path)
        if ledger.get("schema") != TIMING_SCHEMA:
            raise FullRunError("Unsupported full-run timing schema")
        if ledger.get("plan_sha256") != plan_sha256:
            raise FullRunError("Timing ledger is bound to a different plan")
    else:
        ledger = {
            "schema": TIMING_SCHEMA,
            "plan_sha256": plan_sha256,
            "plan_digest": plan.get("plan_digest"),
            "records": [],
            "machine_qa": "NOT_CHECKED",
            "visual_review": "NOT_CHECKED",
            "user_acceptance": "NOT_CHECKED",
        }
    if any(record.get("attempt_id") == attempt_id for record in ledger["records"]):
        raise FullRunError(f"Duplicate timing attempt_id: {attempt_id}")
    ledger["records"].append(
        {
            "attempt_id": attempt_id,
            "stage": stage,
            "started_at_utc": started_at_utc,
            "completed_at_utc": completed_at_utc,
            "elapsed_seconds": elapsed,
            "status": status,
            "note": note,
        }
    )
    ledger["updated_at_utc"] = utc_now()
    ledger["total_recorded_seconds"] = sum(
        float(record["elapsed_seconds"]) for record in ledger["records"]
    )
    atomic_write_json(ledger_path, ledger)
    return ledger
