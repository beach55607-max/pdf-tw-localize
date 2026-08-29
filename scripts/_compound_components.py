#!/usr/bin/env python3
"""Fail-closed compound-component routing and exact PDF path replacement."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import numbers
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from _drawing_signature import (
    COORDINATE_TOLERANCE_PT,
    drawing_records,
    drawing_records_equal,
    rewrite_single_rect_record,
    translate_drawing_record,
)


COMPOUND_COMPONENT_SCHEMA = "pdf-tw-localize/compound-component/v2"
CONTENT_PATH_SIGNATURE_SCHEMA = "pdf-tw-localize/content-path-signature/v1"
ORDERED_PATH_SET_SCHEMA = "pdf-tw-localize/ordered-path-signature-set/v1"
ENGLISH_ALLOWLIST_SCHEMA = "pdf-tw-localize/english-allowlist/v3"
TRANSLATION_DEPENDENT_GEOMETRY_SCHEMA = (
    "pdf-tw-localize/translation-dependent-geometry/v1"
)
COMPOSITED_VISIBLE_LAYOUT_SCHEMA = "pdf-tw-localize/composited-visible-layout/v1"

CANONICAL_COMPONENT_ROLES = {
    "translatable_live_text",
    "translatable_vector_outlined_text",
    "icon",
    "frame",
    "vector_rule",
    "background",
    "neighbor_container",
}
CANONICAL_COMPONENT_POLICIES = {
    "replace_live_text",
    "replace_vector_outlined_text",
    "preserve",
    "preserve_complete_visual",
    "adjust_background",
    "adjust_vector_rule",
}
CANONICAL_SEGMENT_ROLES = {
    "translatable_live_text",
    "translatable_vector_outlined_text",
    "preserved_component",
}
TRANSLATABILITY_VALUES = {"required", "not_translatable", "user_preserved"}
RELATION_TYPES = {
    "contains",
    "adjacent",
    "avoid",
    "align_center_y",
    "align_optical_offset_y",
}
PATH_CONSTRUCTION_OPERATORS = {
    b"m": 2,
    b"l": 2,
    b"c": 6,
    b"v": 4,
    b"y": 4,
    b"h": 0,
    b"re": 4,
}
PATH_PAINT_OPERATORS = {b"S", b"s", b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*", b"n"}
CLIP_OPERATORS = {b"W", b"W*"}
STYLE_OPERATORS = {
    b"w",
    b"J",
    b"j",
    b"M",
    b"d",
    b"ri",
    b"i",
    b"gs",
    b"CS",
    b"cs",
    b"SC",
    b"SCN",
    b"sc",
    b"scn",
    b"G",
    b"g",
    b"RG",
    b"rg",
    b"K",
    b"k",
}
PATH_DISPOSITION_STATUSES = {
    "APPLICABLE",
    "NOT_APPLICABLE_LIVE_TEXT",
    "NOT_APPLICABLE_TEXT_GLYPH",
}
ALLOWLIST_TYPES = {
    "brand",
    "model_or_part_number",
    "standard_identifier",
    "source_ui_user_preserved",
    "protected_proper_name",
}
ALLOWLIST_BASIS_TYPES = {
    "user_instruction",
    "translation_policy",
    "protected_content_policy",
}
DIFFICULTY_REASON_FRAGMENTS = {
    "tool difficulty",
    "technical difficulty",
    "unable to replace",
    "cannot replace",
    "too difficult",
    "工具困難",
    "技術困難",
    "無法替換",
}


class CompoundComponentError(ValueError):
    """A compound-component operation cannot be proved safe."""

    def __init__(self, code: str, message: str, evidence: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.evidence = evidence

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "fail_closed": True,
        }
        if self.evidence is not None:
            result["evidence"] = self.evidence
        return result


def _issue(code: str, message: str, *, segment_id: str, evidence: Any = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "severity": "BLOCKING",
        "code": code,
        "message": message,
        "segment_id": segment_id,
    }
    if evidence is not None:
        result["evidence"] = evidence
    return result


def _finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise CompoundComponentError(
            "CONTENT_PATH_OPERAND_MALFORMED",
            f"{field} must be a finite real number",
            {"value_type": type(value).__name__},
        )
    result = float(value)
    if not math.isfinite(result):
        raise CompoundComponentError(
            "CONTENT_PATH_OPERAND_MALFORMED",
            f"{field} must be finite",
            {"value": repr(result)},
        )
    return round(result, 6)


def _canonical_operand(value: Any, field: str) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, numbers.Real):
        return _finite_number(value, field)
    if isinstance(value, bytes):
        return {"bytes_hex": value.hex().upper()}
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_operand(item, f"{field}.{key}")
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, Sequence):
        return [
            _canonical_operand(item, f"{field}[{index}]")
            for index, item in enumerate(value)
        ]
    text = str(value)
    if not text:
        raise CompoundComponentError(
            "CONTENT_PATH_OPERAND_MALFORMED",
            f"{field} cannot be serialized deterministically",
        )
    return text


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def signature_sha256(signature: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(signature)).hexdigest().upper()


def _matrix_multiply(left: Sequence[float], right: Sequence[float]) -> list[float]:
    a1, b1, c1, d1, e1, f1 = left
    a2, b2, c2, d2, e2, f2 = right
    return [
        round(a1 * a2 + c1 * b2, 6),
        round(b1 * a2 + d1 * b2, 6),
        round(a1 * c2 + c1 * d2, 6),
        round(b1 * c2 + d1 * d2, 6),
        round(a1 * e2 + c1 * f2 + e1, 6),
        round(b1 * e2 + d1 * f2 + f1, 6),
    ]


def _content_stream(data: bytes) -> Any:
    try:
        from pypdf.generic import ContentStream, DecodedStreamObject
    except ImportError as exc:
        raise CompoundComponentError(
            "PYPDF_REQUIRED",
            "pypdf is required for exact content-stream path routing",
        ) from exc
    stream = DecodedStreamObject()
    stream.set_data(data)
    try:
        return ContentStream(stream, None)
    except Exception as exc:
        raise CompoundComponentError(
            "CONTENT_STREAM_PARSE_FAILED",
            "The PDF content stream could not be parsed deterministically",
            {"error": str(exc)},
        ) from exc


def content_path_signature(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": CONTENT_PATH_SIGNATURE_SCHEMA,
        "ctm": copy.deepcopy(record["ctm"]),
        "graphics_state": copy.deepcopy(record["graphics_state"]),
        "path_operators": copy.deepcopy(record["path_operators"]),
        "clip_operator": record.get("clip_operator"),
        "paint_operator": record["paint_operator"],
    }


def parse_content_paths(data: bytes, *, stream_xref: int | None = None) -> list[dict[str, Any]]:
    """Parse every constructed path without dropping operator order or operands."""
    content = _content_stream(data)
    state: dict[str, Any] = {
        "ctm": [1.0, 0.0, 0.0, 1.0, 0.0, 0.0],
        "style": {},
    }
    stack: list[dict[str, Any]] = []
    path_operators: list[dict[str, Any]] = []
    path_operation_indices: list[int] = []
    clip_operator: str | None = None
    records: list[dict[str, Any]] = []

    for operation_index, (raw_operands, operator) in enumerate(content.operations):
        if not isinstance(operator, bytes):
            raise CompoundComponentError(
                "CONTENT_OPERATOR_MALFORMED",
                "A content-stream operator is not bytes",
                {"operation_index": operation_index},
            )
        if operator in PATH_CONSTRUCTION_OPERATORS:
            expected = PATH_CONSTRUCTION_OPERATORS[operator]
            if len(raw_operands) != expected:
                raise CompoundComponentError(
                    "CONTENT_PATH_OPERATOR_MALFORMED",
                    f"Path operator {operator.decode('ascii')} requires {expected} operands",
                    {
                        "operation_index": operation_index,
                        "actual_operand_count": len(raw_operands),
                    },
                )
            canonical_operands = [
                _finite_number(value, f"operations[{operation_index}].operands[{index}]")
                for index, value in enumerate(raw_operands)
            ]
            path_operators.append(
                {
                    "operator": operator.decode("ascii"),
                    "operands": canonical_operands,
                }
            )
            path_operation_indices.append(operation_index)
            continue

        if path_operators:
            if operator in CLIP_OPERATORS:
                if raw_operands or clip_operator is not None:
                    raise CompoundComponentError(
                        "CONTENT_PATH_CLIP_MALFORMED",
                        "A constructed path has an invalid or repeated clipping operator",
                        {"operation_index": operation_index},
                    )
                clip_operator = operator.decode("ascii")
                path_operation_indices.append(operation_index)
                continue
            if operator not in PATH_PAINT_OPERATORS:
                raise CompoundComponentError(
                    "CONTENT_PATH_INTERWOVEN_OPERATOR",
                    "A non-path operator is interwoven with a constructed path",
                    {
                        "operation_index": operation_index,
                        "operator": operator.decode("latin1"),
                    },
                )
            if raw_operands:
                raise CompoundComponentError(
                    "CONTENT_PATH_PAINT_MALFORMED",
                    "A path-paint operator unexpectedly has operands",
                    {"operation_index": operation_index},
                )
            record = {
                "stream_xref": stream_xref,
                "path_index": len(records),
                "operation_indices": [*path_operation_indices, operation_index],
                "ctm": copy.deepcopy(state["ctm"]),
                "graphics_state": copy.deepcopy(state["style"]),
                "path_operators": path_operators,
                "clip_operator": clip_operator,
                "paint_operator": operator.decode("ascii"),
            }
            signature = content_path_signature(record)
            record["signature"] = signature
            record["signature_sha256"] = signature_sha256(signature)
            records.append(record)
            path_operators = []
            path_operation_indices = []
            clip_operator = None
            continue

        if operator == b"q":
            if raw_operands:
                raise CompoundComponentError(
                    "GRAPHICS_STATE_OPERATOR_MALFORMED",
                    "q must not have operands",
                    {"operation_index": operation_index},
                )
            stack.append(copy.deepcopy(state))
        elif operator == b"Q":
            if raw_operands or not stack:
                raise CompoundComponentError(
                    "GRAPHICS_STATE_STACK_MALFORMED",
                    "Q has operands or no matching q",
                    {"operation_index": operation_index},
                )
            state = stack.pop()
        elif operator == b"cm":
            if len(raw_operands) != 6:
                raise CompoundComponentError(
                    "CONTENT_CTM_MALFORMED",
                    "cm requires six finite operands",
                    {"operation_index": operation_index},
                )
            matrix = [
                _finite_number(value, f"operations[{operation_index}].cm[{index}]")
                for index, value in enumerate(raw_operands)
            ]
            state["ctm"] = _matrix_multiply(state["ctm"], matrix)
        elif operator in STYLE_OPERATORS:
            state["style"][operator.decode("ascii")] = [
                _canonical_operand(value, f"operations[{operation_index}].style[{index}]")
                for index, value in enumerate(raw_operands)
            ]

    if path_operators:
        raise CompoundComponentError(
            "CONTENT_PATH_UNTERMINATED",
            "The content stream ends before a constructed path is painted or discarded",
            {"operation_indices": path_operation_indices},
        )
    if stack:
        raise CompoundComponentError(
            "GRAPHICS_STATE_STACK_MALFORMED",
            "The content stream ends with unmatched q operators",
            {"unclosed_depth": len(stack)},
        )
    return records


def _values_equal(left: Any, right: Any) -> bool:
    if isinstance(left, numbers.Real) and not isinstance(left, bool):
        return (
            isinstance(right, numbers.Real)
            and not isinstance(right, bool)
            and abs(float(left) - float(right)) <= COORDINATE_TOLERANCE_PT
        )
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        return set(left) == set(right) and all(
            _values_equal(left[key], right[key]) for key in left
        )
    if (
        isinstance(left, Sequence)
        and not isinstance(left, (str, bytes))
        and isinstance(right, Sequence)
        and not isinstance(right, (str, bytes))
    ):
        return len(left) == len(right) and all(
            _values_equal(a, b) for a, b in zip(left, right, strict=True)
        )
    return left == right


def content_path_signatures_equal(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return _values_equal(left, right)


def content_path_construction_signatures_equal(
    left: Mapping[str, Any], right: Mapping[str, Any]
) -> bool:
    """Compare exact path construction independently of stream state serialization.

    The full source signature, including graphics state, remains mandatory for unique
    source selection. Final candidate QA may use this narrower comparison only together
    with an exact candidate drawing-signature match, which independently proves the
    effective stroke/fill style after a PDF library reserializes inherited state.
    """
    left_validated = _declared_signature({"signature": left})
    right_validated = _declared_signature({"signature": right})
    keys = (
        "schema",
        "ctm",
        "path_operators",
        "clip_operator",
        "paint_operator",
    )
    return all(
        _values_equal(left_validated.get(key), right_validated.get(key)) for key in keys
    )


def content_path_construction_signature_sha256(
    signature: Mapping[str, Any],
) -> str:
    """Hash the exact construction fields used by final operator verification."""

    validated = _declared_signature({"signature": signature})
    construction = {
        key: validated.get(key)
        for key in (
            "schema",
            "ctm",
            "path_operators",
            "clip_operator",
            "paint_operator",
        )
    }
    return hashlib.sha256(_canonical_json(construction)).hexdigest().upper()


def content_path_reserialization_equivalent(
    source: Mapping[str, Any], candidate: Mapping[str, Any]
) -> bool:
    """Recognize one narrowly safe content-stream path reserialization.

    Some PDF serializers replace ``y x1 y1 x3 y3`` with ``l x3 y3`` when
    the first control point is exactly the end point.  Both operators then
    describe the same straight segment, but the replacement loses the source
    operator and therefore fails the ordered-path preservation contract.  This
    predicate accepts only that exact, coordinate-preserving normalization.  It
    deliberately does not infer general curve equivalence or ignore unknown
    operators, operator order, multiplicity, CTM, clipping, or paint changes.

    Graphics-state serialization is not compared here: the candidate keeps its
    own graphics-state operators and only the matched path-construction
    operators are restored.  Full drawing-signature QA remains mandatory after
    the repair and verifies the effective visual style.
    """

    source_validated = _declared_signature({"signature": source})
    candidate_validated = _declared_signature({"signature": candidate})
    for key in ("schema", "ctm", "clip_operator", "paint_operator"):
        if not _values_equal(source_validated.get(key), candidate_validated.get(key)):
            return False
    source_operators = source_validated["path_operators"]
    candidate_operators = candidate_validated["path_operators"]
    if len(source_operators) != len(candidate_operators):
        return False

    restored_operator_count = 0
    for source_operator, candidate_operator in zip(
        source_operators, candidate_operators, strict=True
    ):
        if _values_equal(source_operator, candidate_operator):
            continue
        if (
            source_operator.get("operator") == "y"
            and candidate_operator.get("operator") == "l"
        ):
            source_operands = source_operator.get("operands") or []
            candidate_operands = candidate_operator.get("operands") or []
            if (
                len(source_operands) == 4
                and len(candidate_operands) == 2
                and _values_equal(source_operands[:2], source_operands[2:])
                and _values_equal(candidate_operands, source_operands[2:])
            ):
                restored_operator_count += 1
                continue
        return False
    return restored_operator_count > 0


def restore_reserialized_path_from_source(
    source_data: bytes,
    candidate_data: bytes,
    source_signature: Mapping[str, Any],
    candidate_signature: Mapping[str, Any],
    *,
    source_stream_xref: int | None = None,
    candidate_stream_xref: int | None = None,
) -> tuple[bytes, dict[str, Any]]:
    """Restore one unique safe-normalized path from exact source operations.

    Both the source and candidate records must bind exactly once in their
    respective streams.  The operation ranges must be contiguous and equal in
    length.  No graphics-state, clipping, or neighboring operation is copied.
    """

    source_records = parse_content_paths(source_data, stream_xref=source_stream_xref)
    candidate_records = parse_content_paths(
        candidate_data, stream_xref=candidate_stream_xref
    )
    source_matches = [
        record
        for record in source_records
        if content_path_signatures_equal(record["signature"], source_signature)
    ]
    candidate_matches = [
        record
        for record in candidate_records
        if content_path_signatures_equal(record["signature"], candidate_signature)
    ]
    if len(source_matches) != 1:
        raise CompoundComponentError(
            "SOURCE_RESERIALIZED_PATH_MATCH_COUNT",
            "The source path for operator restoration must match exactly once",
            {
                "source_stream_xref": source_stream_xref,
                "match_count": len(source_matches),
                "signature_sha256": signature_sha256(source_signature),
            },
        )
    if len(candidate_matches) != 1:
        raise CompoundComponentError(
            "CANDIDATE_RESERIALIZED_PATH_MATCH_COUNT",
            "The candidate path for operator restoration must match exactly once",
            {
                "candidate_stream_xref": candidate_stream_xref,
                "match_count": len(candidate_matches),
                "signature_sha256": signature_sha256(candidate_signature),
            },
        )
    source_record = source_matches[0]
    candidate_record = candidate_matches[0]
    if not content_path_reserialization_equivalent(
        source_record["signature"], candidate_record["signature"]
    ):
        raise CompoundComponentError(
            "RESERIALIZED_PATH_NOT_SAFE_EQUIVALENT",
            "The candidate path is not the narrowly supported source-equivalent normalization",
            {
                "source_signature_sha256": source_record["signature_sha256"],
                "candidate_signature_sha256": candidate_record["signature_sha256"],
            },
        )

    source_indices = [int(value) for value in source_record["operation_indices"]]
    candidate_indices = [int(value) for value in candidate_record["operation_indices"]]
    if source_indices != list(range(source_indices[0], source_indices[-1] + 1)):
        raise CompoundComponentError(
            "SOURCE_RESERIALIZED_PATH_RANGE_NOT_CONTIGUOUS",
            "The source path operation range is not contiguous",
            source_indices,
        )
    if candidate_indices != list(
        range(candidate_indices[0], candidate_indices[-1] + 1)
    ):
        raise CompoundComponentError(
            "CANDIDATE_RESERIALIZED_PATH_RANGE_NOT_CONTIGUOUS",
            "The candidate path operation range is not contiguous",
            candidate_indices,
        )
    if len(source_indices) != len(candidate_indices):
        raise CompoundComponentError(
            "RESERIALIZED_PATH_OPERATION_COUNT_CHANGED",
            "Source and candidate path operation ranges differ in length",
            {
                "source_count": len(source_indices),
                "candidate_count": len(candidate_indices),
            },
        )

    source_content = _content_stream(source_data)
    candidate_content = _content_stream(candidate_data)
    candidate_content.operations[
        candidate_indices[0] : candidate_indices[-1] + 1
    ] = copy.deepcopy(
        source_content.operations[source_indices[0] : source_indices[-1] + 1]
    )
    updated = candidate_content.get_data()
    updated_records = parse_content_paths(updated, stream_xref=candidate_stream_xref)
    construction_matches = [
        record
        for record in updated_records
        if content_path_construction_signatures_equal(
            record["signature"], source_record["signature"]
        )
    ]
    if len(construction_matches) != 1:
        raise CompoundComponentError(
            "RESERIALIZED_PATH_RESTORATION_VERIFY_FAILED",
            "The exact source path construction was not restored exactly once",
            {
                "candidate_stream_xref": candidate_stream_xref,
                "match_count": len(construction_matches),
                "source_signature_sha256": source_record["signature_sha256"],
            },
        )
    evidence = {
        "source_stream_xref": source_stream_xref,
        "candidate_stream_xref": candidate_stream_xref,
        "source_path_index": source_record["path_index"],
        "candidate_path_index": candidate_record["path_index"],
        "source_signature_sha256": source_record["signature_sha256"],
        "candidate_signature_sha256_before": candidate_record["signature_sha256"],
        "stream_sha256_before": hashlib.sha256(candidate_data).hexdigest().upper(),
        "stream_sha256_after": hashlib.sha256(updated).hexdigest().upper(),
        "restored_operation_count": len(candidate_indices),
        "status": "RESTORED_SOURCE_OPERATOR_SEQUENCE_VERIFIED",
    }
    return updated, evidence


def restore_reserialized_paths_from_source_batch(
    source_streams: Mapping[int, bytes],
    candidate_data: bytes,
    declarations: Sequence[Mapping[str, Any]],
    *,
    candidate_stream_xref: int | None = None,
) -> tuple[bytes, list[dict[str, Any]]]:
    """Batch the same unique operator restoration without repeated reparsing."""

    if not declarations:
        return candidate_data, []
    candidate_records = parse_content_paths(
        candidate_data, stream_xref=candidate_stream_xref
    )
    candidate_content = _content_stream(candidate_data)
    source_record_cache: dict[int, list[dict[str, Any]]] = {}
    source_content_cache: dict[int, Any] = {}
    replacements: list[dict[str, Any]] = []
    occupied_candidate_indices: set[int] = set()
    for declaration_index, declaration in enumerate(declarations):
        source_xref = int(declaration.get("source_stream_xref", 0))
        source_signature = declaration.get("source_signature")
        candidate_signature = declaration.get("candidate_signature")
        if source_xref not in source_streams:
            raise CompoundComponentError(
                "SOURCE_RESERIALIZED_STREAM_MISSING",
                "A declared source stream is unavailable for batch restoration",
                {"declaration_index": declaration_index, "source_stream_xref": source_xref},
            )
        if not isinstance(source_signature, Mapping) or not isinstance(
            candidate_signature, Mapping
        ):
            raise CompoundComponentError(
                "RESERIALIZED_PATH_DECLARATION_FIELDS",
                "Batch restoration declarations require source and candidate signatures",
                {"declaration_index": declaration_index},
            )
        if source_xref not in source_record_cache:
            source_record_cache[source_xref] = parse_content_paths(
                source_streams[source_xref], stream_xref=source_xref
            )
            source_content_cache[source_xref] = _content_stream(
                source_streams[source_xref]
            )
        source_matches = [
            record
            for record in source_record_cache[source_xref]
            if content_path_signatures_equal(record["signature"], source_signature)
        ]
        candidate_matches = [
            record
            for record in candidate_records
            if content_path_signatures_equal(record["signature"], candidate_signature)
        ]
        if len(source_matches) != 1:
            raise CompoundComponentError(
                "SOURCE_RESERIALIZED_PATH_MATCH_COUNT",
                "The source path for operator restoration must match exactly once",
                {
                    "declaration_index": declaration_index,
                    "source_stream_xref": source_xref,
                    "match_count": len(source_matches),
                },
            )
        if len(candidate_matches) != 1:
            raise CompoundComponentError(
                "CANDIDATE_RESERIALIZED_PATH_MATCH_COUNT",
                "The candidate path for operator restoration must match exactly once",
                {
                    "declaration_index": declaration_index,
                    "candidate_stream_xref": candidate_stream_xref,
                    "match_count": len(candidate_matches),
                },
            )
        source_record = source_matches[0]
        candidate_record = candidate_matches[0]
        if not content_path_reserialization_equivalent(
            source_record["signature"], candidate_record["signature"]
        ):
            raise CompoundComponentError(
                "RESERIALIZED_PATH_NOT_SAFE_EQUIVALENT",
                "The batch candidate path is not a safe source-equivalent normalization",
                {"declaration_index": declaration_index},
            )
        source_indices = [int(value) for value in source_record["operation_indices"]]
        candidate_indices = [
            int(value) for value in candidate_record["operation_indices"]
        ]
        if source_indices != list(range(source_indices[0], source_indices[-1] + 1)):
            raise CompoundComponentError(
                "SOURCE_RESERIALIZED_PATH_RANGE_NOT_CONTIGUOUS",
                "A batch source path range is not contiguous",
                {"declaration_index": declaration_index, "indices": source_indices},
            )
        if candidate_indices != list(
            range(candidate_indices[0], candidate_indices[-1] + 1)
        ):
            raise CompoundComponentError(
                "CANDIDATE_RESERIALIZED_PATH_RANGE_NOT_CONTIGUOUS",
                "A batch candidate path range is not contiguous",
                {"declaration_index": declaration_index, "indices": candidate_indices},
            )
        if len(source_indices) != len(candidate_indices):
            raise CompoundComponentError(
                "RESERIALIZED_PATH_OPERATION_COUNT_CHANGED",
                "A batch source and candidate path range differ in length",
                {"declaration_index": declaration_index},
            )
        overlap = occupied_candidate_indices.intersection(candidate_indices)
        if overlap:
            raise CompoundComponentError(
                "RESERIALIZED_PATH_OPERATION_RANGE_OVERLAP",
                "Batch candidate path operation ranges overlap",
                {"declaration_index": declaration_index, "overlap": sorted(overlap)},
            )
        occupied_candidate_indices.update(candidate_indices)
        replacements.append(
            {
                "source_xref": source_xref,
                "source_record": source_record,
                "candidate_record": candidate_record,
                "source_indices": source_indices,
                "candidate_indices": candidate_indices,
            }
        )

    for replacement in sorted(
        replacements, key=lambda item: item["candidate_indices"][0], reverse=True
    ):
        source_indices = replacement["source_indices"]
        candidate_indices = replacement["candidate_indices"]
        source_content = source_content_cache[replacement["source_xref"]]
        candidate_content.operations[
            candidate_indices[0] : candidate_indices[-1] + 1
        ] = copy.deepcopy(
            source_content.operations[source_indices[0] : source_indices[-1] + 1]
        )
    updated = candidate_content.get_data()
    updated_records = parse_content_paths(updated, stream_xref=candidate_stream_xref)
    construction_counts = Counter(
        content_path_construction_signature_sha256(record["signature"])
        for record in updated_records
    )
    evidence: list[dict[str, Any]] = []
    for replacement in replacements:
        source_record = replacement["source_record"]
        source_construction_sha256 = content_path_construction_signature_sha256(
            source_record["signature"]
        )
        match_count = construction_counts[source_construction_sha256]
        if match_count != 1:
            raise CompoundComponentError(
                "RESERIALIZED_PATH_RESTORATION_VERIFY_FAILED",
                "A batched source path construction was not restored exactly once",
                {
                    "candidate_stream_xref": candidate_stream_xref,
                    "match_count": match_count,
                    "source_signature_sha256": source_record["signature_sha256"],
                    "source_construction_sha256": source_construction_sha256,
                },
            )
        candidate_record = replacement["candidate_record"]
        evidence.append(
            {
                "source_stream_xref": replacement["source_xref"],
                "candidate_stream_xref": candidate_stream_xref,
                "source_path_index": source_record["path_index"],
                "candidate_path_index": candidate_record["path_index"],
                "source_signature_sha256": source_record["signature_sha256"],
                "candidate_signature_sha256_before": candidate_record[
                    "signature_sha256"
                ],
                "stream_sha256_before": hashlib.sha256(candidate_data)
                .hexdigest()
                .upper(),
                "stream_sha256_after": hashlib.sha256(updated).hexdigest().upper(),
                "restored_operation_count": len(replacement["candidate_indices"]),
                "status": "RESTORED_SOURCE_OPERATOR_SEQUENCE_VERIFIED",
            }
        )
    return updated, evidence


def _declared_signature(entry: Any) -> dict[str, Any]:
    signature = entry.get("signature") if isinstance(entry, Mapping) else None
    if not isinstance(signature, Mapping):
        raise CompoundComponentError(
            "CONTENT_PATH_SIGNATURE_MISSING",
            "Each selected path entry must contain a signature object",
            entry,
        )
    if signature.get("schema") != CONTENT_PATH_SIGNATURE_SCHEMA:
        raise CompoundComponentError(
            "CONTENT_PATH_SIGNATURE_SCHEMA",
            "The selected content-path signature schema is unsupported",
            signature.get("schema"),
        )
    required = {
        "ctm",
        "graphics_state",
        "path_operators",
        "clip_operator",
        "paint_operator",
    }
    missing = sorted(required - set(signature))
    if missing:
        raise CompoundComponentError(
            "CONTENT_PATH_SIGNATURE_FIELDS",
            "A selected content-path signature is incomplete",
            missing,
        )
    if not isinstance(signature.get("path_operators"), list) or not signature["path_operators"]:
        raise CompoundComponentError(
            "CONTENT_PATH_SIGNATURE_FIELDS",
            "A selected content-path signature requires ordered path operators",
        )
    return dict(signature)


def match_declared_path_signatures(
    records: Sequence[Mapping[str, Any]], declared: Sequence[Mapping[str, Any]]
) -> list[int]:
    """Require one unique path record per declared signature, preserving multiplicity."""
    selected: list[int] = []
    used: set[int] = set()
    for declared_index, entry in enumerate(declared):
        signature = _declared_signature(entry)
        matches = [
            index
            for index, record in enumerate(records)
            if content_path_signatures_equal(record.get("signature") or {}, signature)
        ]
        if not matches:
            raise CompoundComponentError(
                "CONTENT_PATH_SIGNATURE_NOT_FOUND",
                "A declared source path signature did not match",
                {
                    "declared_index": declared_index,
                    "signature_sha256": signature_sha256(signature),
                },
            )
        if len(matches) != 1:
            raise CompoundComponentError(
                "CONTENT_PATH_SIGNATURE_DUPLICATE_MATCH",
                "A declared path signature matched more than one path",
                {
                    "declared_index": declared_index,
                    "match_indices": matches,
                    "signature_sha256": signature_sha256(signature),
                },
            )
        match = matches[0]
        if match in used:
            raise CompoundComponentError(
                "CONTENT_PATH_SIGNATURE_REUSED",
                "Two declarations selected the same source path",
                {"record_index": match},
            )
        used.add(match)
        selected.append(match)
    return selected


def remove_selected_paths_from_stream(
    data: bytes, declared: Sequence[Mapping[str, Any]], *, stream_xref: int | None = None
) -> tuple[bytes, dict[str, Any]]:
    """Remove only uniquely matched path construction and paint operations."""
    records = parse_content_paths(data, stream_xref=stream_xref)
    selected_indices = match_declared_path_signatures(records, declared)
    operation_indices: set[int] = set()
    selected_evidence: list[dict[str, Any]] = []
    for index in selected_indices:
        record = records[index]
        if record.get("clip_operator") is not None:
            raise CompoundComponentError(
                "CONTENT_PATH_CLIP_REPLACEMENT_FORBIDDEN",
                "A clipping path cannot be routed as outlined text",
                {"path_index": index},
            )
        indices = [int(value) for value in record["operation_indices"]]
        if indices != list(range(indices[0], indices[-1] + 1)):
            raise CompoundComponentError(
                "CONTENT_PATH_OPERATION_RANGE_NOT_UNIQUE",
                "The exact path operators are not one contiguous removable range",
                {"path_index": index, "operation_indices": indices},
            )
        if operation_indices.intersection(indices):
            raise CompoundComponentError(
                "CONTENT_PATH_OPERATION_RANGE_OVERLAP",
                "Selected path operation ranges overlap",
                {"path_index": index},
            )
        operation_indices.update(indices)
        selected_evidence.append(
            {
                "path_index": index,
                "operation_indices": indices,
                "signature_sha256": record["signature_sha256"],
            }
        )

    content = _content_stream(data)
    content.operations = [
        operation
        for index, operation in enumerate(content.operations)
        if index not in operation_indices
    ]
    updated = content.get_data()
    updated_records = parse_content_paths(updated, stream_xref=stream_xref)
    residue: list[dict[str, Any]] = []
    for entry in declared:
        signature = _declared_signature(entry)
        count = sum(
            content_path_signatures_equal(record["signature"], signature)
            for record in updated_records
        )
        if count:
            residue.append(
                {
                    "signature_sha256": signature_sha256(signature),
                    "residue_count": count,
                }
            )
    if residue:
        raise CompoundComponentError(
            "CONTENT_PATH_RESIDUE",
            "Selected outlined-text path signatures remain after removal",
            residue,
        )
    evidence = {
        "stream_xref": stream_xref,
        "stream_sha256_before": hashlib.sha256(data).hexdigest().upper(),
        "stream_sha256_after": hashlib.sha256(updated).hexdigest().upper(),
        "selected_path_count": len(selected_indices),
        "removed_operation_count": len(operation_indices),
        "selected_paths": selected_evidence,
        "residue_count": 0,
        "status": "APPLIED_VERIFIED",
    }
    return updated, evidence


def translated_content_path_signature(
    signature: Mapping[str, Any], delta_pt: Sequence[float]
) -> dict[str, Any]:
    """Return the exact expected signature after a page-space path translation."""
    source = _declared_signature({"signature": signature})
    delta = list(delta_pt)
    if len(delta) != 2:
        raise CompoundComponentError(
            "VECTOR_RULE_TRANSLATION_FIELDS",
            "translation_delta_pt must contain page-space dx and dy",
            delta_pt,
        )
    dx = _finite_number(delta[0], "translation_delta_pt[0]")
    dy = _finite_number(delta[1], "translation_delta_pt[1]")
    ctm = source.get("ctm")
    if not isinstance(ctm, Sequence) or isinstance(ctm, (str, bytes)) or len(ctm) != 6:
        raise CompoundComponentError(
            "VECTOR_RULE_TRANSLATION_CTM",
            "An adjusted vector rule requires a six-number CTM",
            ctm,
        )
    normalized_ctm = [
        _finite_number(value, f"content_path.ctm[{index}]")
        for index, value in enumerate(ctm)
    ]
    if any(
        abs(actual - expected) > COORDINATE_TOLERANCE_PT
        for actual, expected in zip(normalized_ctm[:4], [1.0, 0.0, 0.0, 1.0], strict=True)
    ):
        raise CompoundComponentError(
            "VECTOR_RULE_TRANSLATION_CTM",
            "adjust_vector_rule supports only an axis-aligned unit-scale source path",
            normalized_ctm,
        )
    if source.get("clip_operator") is not None or source.get("paint_operator") not in {"S", "s"}:
        raise CompoundComponentError(
            "VECTOR_RULE_TRANSLATION_PATH_TYPE",
            "adjust_vector_rule requires one unclipped stroked path",
            {
                "clip_operator": source.get("clip_operator"),
                "paint_operator": source.get("paint_operator"),
            },
        )

    local_dx = dx
    local_dy = -dy
    result = copy.deepcopy(source)
    for operator_index, operator in enumerate(result["path_operators"]):
        kind = str(operator.get("operator", ""))
        operands = list(operator.get("operands") or [])
        if kind == "h":
            if operands:
                raise CompoundComponentError(
                    "VECTOR_RULE_TRANSLATION_OPERATOR",
                    "Close-path operator cannot carry operands",
                    {"operator_index": operator_index, "operands": operands},
                )
            continue
        if kind == "re":
            coordinate_indices = (0, 1)
        elif kind in {"m", "l", "c", "v", "y"}:
            coordinate_indices = tuple(range(len(operands)))
        else:
            raise CompoundComponentError(
                "VECTOR_RULE_TRANSLATION_OPERATOR",
                "An adjusted vector rule contains an unsupported path operator",
                {"operator_index": operator_index, "operator": kind},
            )
        if len(coordinate_indices) % 2:
            raise CompoundComponentError(
                "VECTOR_RULE_TRANSLATION_OPERATOR",
                "An adjusted vector rule has an incomplete coordinate pair",
                {"operator_index": operator_index, "operator": kind},
            )
        for pair_index in range(0, len(coordinate_indices), 2):
            x_index = coordinate_indices[pair_index]
            y_index = coordinate_indices[pair_index + 1]
            operands[x_index] = round(
                _finite_number(operands[x_index], "path.x") + local_dx, 6
            )
            operands[y_index] = round(
                _finite_number(operands[y_index], "path.y") + local_dy, 6
            )
        operator["operands"] = operands
    return result


def translate_selected_paths_in_stream(
    data: bytes,
    adjustments: Sequence[Mapping[str, Any]],
    *,
    stream_xref: int | None = None,
) -> tuple[bytes, dict[str, Any]]:
    """Translate uniquely selected stroked paths while preserving every other operation."""
    if not adjustments:
        return data, {
            "stream_xref": stream_xref,
            "stream_sha256_before": hashlib.sha256(data).hexdigest().upper(),
            "stream_sha256_after": hashlib.sha256(data).hexdigest().upper(),
            "adjusted_path_count": 0,
            "adjustments": [],
            "status": "NO_CHANGES",
        }
    try:
        from pypdf.generic import FloatObject
    except ImportError as exc:
        raise CompoundComponentError(
            "PYPDF_REQUIRED",
            "pypdf is required for exact vector-rule translation",
        ) from exc

    records = parse_content_paths(data, stream_xref=stream_xref)
    selected: list[tuple[Mapping[str, Any], dict[str, Any], dict[str, Any]]] = []
    used_path_indices: set[int] = set()
    used_operation_indices: set[int] = set()
    for adjustment in adjustments:
        declaration = adjustment.get("declaration")
        if not isinstance(declaration, Mapping):
            raise CompoundComponentError(
                "VECTOR_RULE_TRANSLATION_FIELDS",
                "Each rule adjustment requires one declaration",
                adjustment,
            )
        indices = match_declared_path_signatures(records, [declaration])
        path_index = indices[0]
        if path_index in used_path_indices:
            raise CompoundComponentError(
                "VECTOR_RULE_TRANSLATION_PATH_REUSED",
                "Two rule adjustments selected the same path",
                {"path_index": path_index},
            )
        used_path_indices.add(path_index)
        record = records[path_index]
        operation_indices = [int(value) for value in record["operation_indices"]]
        if operation_indices != list(range(operation_indices[0], operation_indices[-1] + 1)):
            raise CompoundComponentError(
                "VECTOR_RULE_TRANSLATION_RANGE_NOT_UNIQUE",
                "The exact vector-rule operators are not one contiguous range",
                {"path_index": path_index, "operation_indices": operation_indices},
            )
        if used_operation_indices.intersection(operation_indices):
            raise CompoundComponentError(
                "VECTOR_RULE_TRANSLATION_RANGE_OVERLAP",
                "Two vector-rule operator ranges overlap",
                {"path_index": path_index},
            )
        used_operation_indices.update(operation_indices)
        target_signature = translated_content_path_signature(
            record["signature"], adjustment.get("translation_delta_pt") or []
        )
        if content_path_signatures_equal(record["signature"], target_signature):
            raise CompoundComponentError(
                "VECTOR_RULE_TRANSLATION_ZERO_DELTA",
                "An adjusted vector rule must move by a non-zero declared delta",
                adjustment.get("translation_delta_pt"),
            )
        selected.append((adjustment, record, target_signature))

    content = _content_stream(data)
    adjustment_evidence: list[dict[str, Any]] = []
    for adjustment, record, target_signature in selected:
        page_dx, page_dy = [
            _finite_number(value, "translation_delta_pt")
            for value in adjustment["translation_delta_pt"]
        ]
        local_dx = page_dx
        local_dy = -page_dy
        for operation_index in record["operation_indices"]:
            operands, operator = content.operations[int(operation_index)]
            if operator in PATH_PAINT_OPERATORS:
                continue
            if operator not in PATH_CONSTRUCTION_OPERATORS:
                raise CompoundComponentError(
                    "VECTOR_RULE_TRANSLATION_INTERWOVEN_OPERATOR",
                    "A non-path operator is interwoven with the adjusted vector rule",
                    {"operation_index": operation_index, "operator": str(operator)},
                )
            if operator == b"h":
                continue
            coordinate_indices = (0, 1) if operator == b"re" else tuple(range(len(operands)))
            if len(coordinate_indices) % 2:
                raise CompoundComponentError(
                    "VECTOR_RULE_TRANSLATION_OPERATOR",
                    "A vector-rule operator has an incomplete coordinate pair",
                    {"operation_index": operation_index, "operator": operator.decode("ascii")},
                )
            for pair_index in range(0, len(coordinate_indices), 2):
                x_index = coordinate_indices[pair_index]
                y_index = coordinate_indices[pair_index + 1]
                operands[x_index] = FloatObject(
                    str(round(float(operands[x_index]) + local_dx, 6))
                )
                operands[y_index] = FloatObject(
                    str(round(float(operands[y_index]) + local_dy, 6))
                )
        adjustment_evidence.append(
            {
                "component_id": adjustment.get("component_id"),
                "path_index": record["path_index"],
                "operation_indices": record["operation_indices"],
                "translation_delta_pt": [page_dx, page_dy],
                "source_signature_sha256": record["signature_sha256"],
                "target_signature_sha256": signature_sha256(target_signature),
            }
        )

    updated = content.get_data()
    updated_records = parse_content_paths(updated, stream_xref=stream_xref)
    for adjustment, record, target_signature in selected:
        source_count = sum(
            content_path_signatures_equal(item["signature"], record["signature"])
            for item in updated_records
        )
        target_count = sum(
            content_path_signatures_equal(item["signature"], target_signature)
            for item in updated_records
        )
        if source_count or target_count != 1:
            raise CompoundComponentError(
                "VECTOR_RULE_TRANSLATION_POSTCHECK",
                "The adjusted stream does not contain exactly one translated rule and zero source rule",
                {
                    "component_id": adjustment.get("component_id"),
                    "source_count": source_count,
                    "target_count": target_count,
                },
            )
    return updated, {
        "stream_xref": stream_xref,
        "stream_sha256_before": hashlib.sha256(data).hexdigest().upper(),
        "stream_sha256_after": hashlib.sha256(updated).hexdigest().upper(),
        "adjusted_path_count": len(selected),
        "adjustments": adjustment_evidence,
        "status": "APPLIED_VERIFIED",
    }


def _normalize_bbox(values: Any) -> list[float]:
    if isinstance(values, (str, bytes)):
        raise ValueError("bbox must be a four-number sequence")
    result = [_finite_number(value, "bbox") for value in values]
    if len(result) != 4 or result[2] < result[0] or result[3] < result[1]:
        raise ValueError("bbox must contain ordered x0, y0, x1, y1 values")
    return result


def _bbox_inside(inner: Any, outer: Any, tolerance: float = 0.001) -> bool:
    ix0, iy0, ix1, iy1 = _normalize_bbox(inner)
    ox0, oy0, ox1, oy1 = _normalize_bbox(outer)
    return (
        ix0 >= ox0 - tolerance
        and iy0 >= oy0 - tolerance
        and ix1 <= ox1 + tolerance
        and iy1 <= oy1 + tolerance
    )


def bbox_intersection_area(first: Any, second: Any) -> float:
    ax0, ay0, ax1, ay1 = _normalize_bbox(first)
    bx0, by0, bx1, by1 = _normalize_bbox(second)
    return max(0.0, min(ax1, bx1) - max(ax0, bx0)) * max(
        0.0, min(ay1, by1) - max(ay0, by0)
    )


def bbox_clearance_pt(first: Any, second: Any) -> float:
    ax0, ay0, ax1, ay1 = _normalize_bbox(first)
    bx0, by0, bx1, by1 = _normalize_bbox(second)
    dx = max(bx0 - ax1, ax0 - bx1, 0.0)
    dy = max(by0 - ay1, ay0 - by1, 0.0)
    return math.hypot(dx, dy)


def _path_entries(member: Mapping[str, Any]) -> list[dict[str, Any]]:
    source_evidence = member.get("source_evidence") or {}
    path_set = source_evidence.get("ordered_path_signatures") or {}
    entries = path_set.get("content_path_signatures") or []
    return [dict(entry) for entry in entries if isinstance(entry, Mapping)]


def text_span_records(page: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for block in page.get_text("dict").get("blocks") or []:
        if block.get("type") != 0:
            continue
        for line in block.get("lines") or []:
            for span in line.get("spans") or []:
                records.append(
                    {
                        "text": str(span.get("text", "")),
                        "bbox": _normalize_bbox(span.get("bbox", ())),
                        "font": str(span.get("font", "")),
                        "size_pt": round(float(span.get("size", 0.0)), 6),
                    }
                )
    return records


def text_span_match_count(page: Any, declaration: Mapping[str, Any]) -> int:
    required = {"text", "bbox", "font", "size_pt"}
    if not required.issubset(declaration):
        raise CompoundComponentError(
            "COMPONENT_TEXT_SPAN_FIELDS",
            "A declared text span requires text, bbox, font, and size_pt",
            declaration,
        )
    expected_bbox = _normalize_bbox(declaration["bbox"])
    expected_size = _finite_number(declaration["size_pt"], "text_span.size_pt")
    return sum(
        record["text"] == str(declaration["text"])
        and record["font"] == str(declaration["font"])
        and abs(record["size_pt"] - expected_size) <= COORDINATE_TOLERANCE_PT
        and _values_equal(record["bbox"], expected_bbox)
        for record in text_span_records(page)
    )


def validate_compound_component_contract(
    segment: Mapping[str, Any], page_rect: Any
) -> list[dict[str, Any]]:
    """Validate the strict reusable v2 contract without opening the source PDF."""
    segment_id = str(segment.get("segment_id", ""))
    contract = segment.get("component_contract") or {}
    issues: list[dict[str, Any]] = []
    if contract.get("schema") != COMPOUND_COMPONENT_SCHEMA:
        return issues
    group_id = str(contract.get("group_id", "")).strip()
    segment_role = str(contract.get("segment_role", "")).strip()
    mask_policy = str(contract.get("mask_policy", "")).strip()
    render = segment.get("render") or {}
    action = str(render.get("action", "replace"))
    if not group_id:
        issues.append(_issue("COMPONENT_GROUP_ID", "group_id is required", segment_id=segment_id))
    if segment_role not in CANONICAL_SEGMENT_ROLES:
        issues.append(
            _issue(
                "COMPONENT_SEGMENT_ROLE",
                f"Unsupported v2 segment role: {segment_role!r}",
                segment_id=segment_id,
            )
        )
    if mask_policy not in {"source_text_spans_only", "none"}:
        issues.append(
            _issue(
                "COMPONENT_MASK_POLICY",
                f"Unsupported v2 mask policy: {mask_policy!r}",
                segment_id=segment_id,
            )
        )

    members = contract.get("members")
    if not isinstance(members, list) or not members:
        return [
            *issues,
            _issue(
                "COMPONENT_MEMBERS",
                "A v2 compound component requires a non-empty members list",
                segment_id=segment_id,
            ),
        ]
    member_ids: list[str] = []
    member_by_id: dict[str, Mapping[str, Any]] = {}
    signature_owners: dict[str, list[tuple[str, str]]] = {}
    translatable_members: list[Mapping[str, Any]] = []

    for member in members:
        if not isinstance(member, Mapping):
            issues.append(
                _issue(
                    "COMPONENT_MEMBER_TYPE",
                    "Every v2 component member must be an object",
                    segment_id=segment_id,
                    evidence=member,
                )
            )
            continue
        component_id = str(member.get("component_id", "")).strip()
        role = str(member.get("role", "")).strip()
        policy = str(member.get("policy", "")).strip()
        translatability = str(member.get("translatability", "")).strip()
        member_ids.append(component_id)
        if component_id:
            member_by_id[component_id] = member
        missing = [
            field
            for field in (
                "component_id",
                "source_page",
                "bbox",
                "role",
                "translatability",
                "policy",
                "source_evidence",
                "relations",
            )
            if field not in member
        ]
        if missing:
            issues.append(
                _issue(
                    "COMPONENT_MEMBER_FIELDS",
                    "A v2 component member is missing required fields",
                    segment_id=segment_id,
                    evidence={"component_id": component_id, "missing": missing},
                )
            )
        if not component_id:
            issues.append(_issue("COMPONENT_ID", "component_id is required", segment_id=segment_id))
        if role not in CANONICAL_COMPONENT_ROLES:
            issues.append(
                _issue(
                    "COMPONENT_ROLE",
                    f"Unsupported v2 component role: {role!r}",
                    segment_id=segment_id,
                    evidence=component_id,
                )
            )
        if policy not in CANONICAL_COMPONENT_POLICIES:
            issues.append(
                _issue(
                    "COMPONENT_POLICY",
                    f"Unsupported v2 component policy: {policy!r}",
                    segment_id=segment_id,
                    evidence=component_id,
                )
            )
        if translatability not in TRANSLATABILITY_VALUES:
            issues.append(
                _issue(
                    "COMPONENT_TRANSLATABILITY",
                    "translatability must be required, not_translatable, or user_preserved",
                    segment_id=segment_id,
                    evidence=component_id,
                )
            )
        if translatability == "required":
            translatable_members.append(member)
        try:
            if int(member.get("source_page", 0)) != int(segment.get("page", 0)):
                raise ValueError("member source_page differs from segment page")
            if not _bbox_inside(member["bbox"], page_rect):
                raise ValueError("member bbox is outside the source page")
        except Exception as exc:
            issues.append(
                _issue(
                    "COMPONENT_SOURCE_GEOMETRY",
                    str(exc),
                    segment_id=segment_id,
                    evidence=component_id,
                )
            )

        if role == "translatable_live_text":
            if translatability != "required" or policy != "replace_live_text":
                issues.append(
                    _issue(
                        "LIVE_TEXT_COMPONENT_POLICY",
                        "translatable_live_text requires required + replace_live_text",
                        segment_id=segment_id,
                        evidence=component_id,
                    )
                )
        elif role == "translatable_vector_outlined_text":
            if translatability != "required" or policy != "replace_vector_outlined_text":
                issues.append(
                    _issue(
                        "VECTOR_TEXT_COMPONENT_POLICY",
                        "translatable_vector_outlined_text requires required + replace_vector_outlined_text",
                        segment_id=segment_id,
                        evidence=component_id,
                    )
                )
        elif policy in {"replace_live_text", "replace_vector_outlined_text"}:
            issues.append(
                _issue(
                    "NON_TEXT_COMPONENT_REPLACEMENT",
                    "Only the corresponding translatable text role may use a replacement policy",
                    segment_id=segment_id,
                    evidence=component_id,
                )
            )
        if policy == "adjust_background" and role != "background":
            issues.append(
                _issue(
                    "BACKGROUND_ADJUSTMENT_ROLE",
                    "Only background may use adjust_background",
                    segment_id=segment_id,
                    evidence=component_id,
                )
            )
        if policy == "adjust_vector_rule" and role != "vector_rule":
            issues.append(
                _issue(
                    "VECTOR_RULE_ADJUSTMENT_ROLE",
                    "Only vector_rule may use adjust_vector_rule",
                    segment_id=segment_id,
                    evidence=component_id,
                )
            )
        if policy == "adjust_vector_rule" and translatability != "not_translatable":
            issues.append(
                _issue(
                    "VECTOR_RULE_ADJUSTMENT_TRANSLATABILITY",
                    "An adjusted vector rule must remain explicitly not_translatable",
                    segment_id=segment_id,
                    evidence=component_id,
                )
            )

        source_evidence = member.get("source_evidence")
        if not isinstance(source_evidence, Mapping):
            issues.append(
                _issue(
                    "COMPONENT_SOURCE_EVIDENCE",
                    "source_evidence must be an object",
                    segment_id=segment_id,
                    evidence=component_id,
                )
            )
            continue
        if not isinstance(source_evidence.get("page_object_xref"), int):
            issues.append(
                _issue(
                    "COMPONENT_SOURCE_PAGE_XREF",
                    "source_evidence.page_object_xref is required",
                    segment_id=segment_id,
                    evidence=component_id,
                )
            )
        object_xrefs = source_evidence.get("object_xrefs")
        if not isinstance(object_xrefs, list) or not object_xrefs or not all(
            isinstance(value, int) and value > 0 for value in object_xrefs
        ):
            issues.append(
                _issue(
                    "COMPONENT_SOURCE_OBJECT_XREFS",
                    "source_evidence.object_xrefs requires positive xrefs",
                    segment_id=segment_id,
                    evidence=component_id,
                )
            )
        streams = source_evidence.get("content_streams")
        if not isinstance(streams, list) or not streams:
            issues.append(
                _issue(
                    "COMPONENT_SOURCE_STREAMS",
                    "source_evidence.content_streams is required",
                    segment_id=segment_id,
                    evidence=component_id,
                )
            )
        else:
            for stream in streams:
                if (
                    not isinstance(stream, Mapping)
                    or not isinstance(stream.get("xref"), int)
                    or not isinstance(stream.get("sha256"), str)
                    or len(str(stream.get("sha256"))) != 64
                ):
                    issues.append(
                        _issue(
                            "COMPONENT_SOURCE_STREAM_FIELDS",
                            "Each source content stream needs xref and SHA-256",
                            segment_id=segment_id,
                            evidence={"component_id": component_id, "stream": stream},
                        )
                    )
        path_set = source_evidence.get("ordered_path_signatures")
        if not isinstance(path_set, Mapping) or path_set.get("schema") != ORDERED_PATH_SET_SCHEMA:
            issues.append(
                _issue(
                    "COMPONENT_PATH_SIGNATURE_SET",
                    "Every member requires an ordered path-signature disposition",
                    segment_id=segment_id,
                    evidence=component_id,
                )
            )
            continue
        disposition = str(path_set.get("status", ""))
        drawing_signatures = path_set.get("drawing_signatures")
        content_signatures = path_set.get("content_path_signatures")
        if disposition not in PATH_DISPOSITION_STATUSES:
            issues.append(
                _issue(
                    "COMPONENT_PATH_SIGNATURE_STATUS",
                    "Unsupported ordered path-signature disposition",
                    segment_id=segment_id,
                    evidence={"component_id": component_id, "status": disposition},
                )
            )
        if not isinstance(drawing_signatures, list) or not isinstance(content_signatures, list):
            issues.append(
                _issue(
                    "COMPONENT_PATH_SIGNATURE_FIELDS",
                    "drawing_signatures and content_path_signatures must be lists",
                    segment_id=segment_id,
                    evidence=component_id,
                )
            )
            continue
        if disposition == "APPLICABLE" and not drawing_signatures:
            issues.append(
                _issue(
                    "COMPONENT_DRAWING_SIGNATURES_MISSING",
                    "An applicable vector member needs complete ordered drawing signatures",
                    segment_id=segment_id,
                    evidence=component_id,
                )
            )
        if disposition in {"NOT_APPLICABLE_LIVE_TEXT", "NOT_APPLICABLE_TEXT_GLYPH"}:
            text_spans = source_evidence.get("text_spans")
            if not isinstance(text_spans, list) or not text_spans:
                issues.append(
                    _issue(
                        "COMPONENT_TEXT_SPANS_MISSING",
                        "A text or glyph member requires exact source text-span evidence",
                        segment_id=segment_id,
                        evidence=component_id,
                    )
                )
            else:
                for declaration in text_spans:
                    try:
                        if not isinstance(declaration, Mapping):
                            raise CompoundComponentError(
                                "COMPONENT_TEXT_SPAN_FIELDS",
                                "A declared text span must be an object",
                            )
                        required_text_fields = {"text", "bbox", "font", "size_pt", "ref"}
                        if not required_text_fields.issubset(declaration):
                            raise CompoundComponentError(
                                "COMPONENT_TEXT_SPAN_FIELDS",
                                "A declared text span requires ref, text, bbox, font, and size_pt",
                                declaration,
                            )
                        _normalize_bbox(declaration["bbox"])
                        _finite_number(declaration["size_pt"], "text_span.size_pt")
                    except (CompoundComponentError, TypeError, ValueError) as exc:
                        issues.append(
                            _issue(
                                getattr(exc, "code", "COMPONENT_TEXT_SPAN_FIELDS"),
                                str(exc),
                                segment_id=segment_id,
                                evidence={"component_id": component_id, "span": declaration},
                            )
                        )
        if role == "translatable_vector_outlined_text" and not content_signatures:
            issues.append(
                _issue(
                    "VECTOR_TEXT_CONTENT_SIGNATURES_MISSING",
                    "Outlined-text replacement requires explicit content-path signatures",
                    segment_id=segment_id,
                    evidence=component_id,
                )
            )
        for entry in content_signatures:
            try:
                signature = _declared_signature(entry)
                owner_key = signature_sha256(signature)
                signature_owners.setdefault(owner_key, []).append((component_id, role))
                if not isinstance(entry.get("content_stream_xref"), int):
                    raise CompoundComponentError(
                        "CONTENT_PATH_STREAM_XREF_MISSING",
                        "Each content-path signature must name its source stream xref",
                    )
            except CompoundComponentError as exc:
                issues.append(
                    _issue(
                        exc.code,
                        exc.message,
                        segment_id=segment_id,
                        evidence={"component_id": component_id, "detail": exc.evidence},
                    )
                )

        if policy == "adjust_vector_rule":
            try:
                if member.get("adjustment_method") != "translate_exact_stroked_path":
                    raise CompoundComponentError(
                        "VECTOR_RULE_ADJUSTMENT_METHOD",
                        "adjust_vector_rule requires translate_exact_stroked_path",
                    )
                source_bbox = _normalize_bbox(member["bbox"])
                dependency = member.get("dependent_geometry")
                if dependency is None:
                    target_bbox = _normalize_bbox(member["target_bbox"])
                    if not _bbox_inside(target_bbox, page_rect):
                        raise CompoundComponentError(
                            "VECTOR_RULE_TARGET_GEOMETRY",
                            "The adjusted vector rule target bbox is outside the page",
                            target_bbox,
                        )
                    delta = list(member["translation_delta_pt"])
                    if len(delta) != 2:
                        raise CompoundComponentError(
                            "VECTOR_RULE_TRANSLATION_FIELDS",
                            "translation_delta_pt must contain dx and dy",
                            delta,
                        )
                    dx = _finite_number(delta[0], "translation_delta_pt[0]")
                    dy = _finite_number(delta[1], "translation_delta_pt[1]")
                    expected_target = [
                        round(source_bbox[0] + dx, 6),
                        round(source_bbox[1] + dy, 6),
                        round(source_bbox[2] + dx, 6),
                        round(source_bbox[3] + dy, 6),
                    ]
                    if not _values_equal(expected_target, target_bbox):
                        raise CompoundComponentError(
                            "VECTOR_RULE_TARGET_GEOMETRY",
                            "target_bbox must be the source bbox translated by translation_delta_pt",
                            {
                                "source_bbox": source_bbox,
                                "target_bbox": target_bbox,
                                "expected_target_bbox": expected_target,
                            },
                        )
                    if abs(dx) <= COORDINATE_TOLERANCE_PT and abs(dy) <= COORDINATE_TOLERANCE_PT:
                        raise CompoundComponentError(
                            "VECTOR_RULE_TRANSLATION_ZERO_DELTA",
                            "adjust_vector_rule requires a non-zero path translation",
                            delta,
                        )
                if len(drawing_signatures) != 1 or len(content_signatures) != 1:
                    raise CompoundComponentError(
                        "VECTOR_RULE_SIGNATURE_COUNT",
                        "adjust_vector_rule requires exactly one drawing and one content-path signature",
                        {
                            "drawing_count": len(drawing_signatures),
                            "content_path_count": len(content_signatures),
                        },
                    )
                drawing = drawing_signatures[0]
                if "s" not in str(drawing.get("type", "")):
                    raise CompoundComponentError(
                        "VECTOR_RULE_TRANSLATION_PATH_TYPE",
                        "The adjusted drawing signature must be stroked",
                        drawing.get("type"),
                    )
                if dependency is None:
                    translated_content_path_signature(
                        _declared_signature(content_signatures[0]), [dx, dy]
                    )
                    translate_drawing_record(drawing, [dx, dy])
                else:
                    _declared_signature(content_signatures[0])
            except (CompoundComponentError, KeyError, TypeError, ValueError) as exc:
                issues.append(
                    _issue(
                        getattr(exc, "code", "VECTOR_RULE_ADJUSTMENT_FIELDS"),
                        getattr(exc, "message", str(exc)),
                        segment_id=segment_id,
                        evidence={
                            "component_id": component_id,
                            "detail": getattr(exc, "evidence", None),
                        },
                    )
                )

        relations = member.get("relations")
        if not isinstance(relations, list):
            issues.append(
                _issue(
                    "COMPONENT_RELATIONS",
                    "relations must be a list",
                    segment_id=segment_id,
                    evidence=component_id,
                )
            )

    duplicates = sorted(
        component_id
        for component_id, count in Counter(member_ids).items()
        if component_id and count > 1
    )
    if duplicates:
        issues.append(
            _issue(
                "DUPLICATE_COMPONENT_ID",
                "Component IDs must be unique within a group",
                segment_id=segment_id,
                evidence=duplicates,
            )
        )

    for member in members:
        if isinstance(member, Mapping):
            issues.extend(
                validate_translation_dependent_geometry(
                    member,
                    member_by_id,
                    page_rect,
                    segment_id=segment_id,
                )
            )

    for signature_hash, owners in signature_owners.items():
        unique_owners = sorted(set(owners))
        if len(unique_owners) > 1:
            issues.append(
                _issue(
                    "PATH_MEMBER_INTERWOVEN",
                    "One exact painted path is assigned to multiple component members",
                    segment_id=segment_id,
                    evidence={"signature_sha256": signature_hash, "owners": unique_owners},
                )
            )

    for member in members:
        if not isinstance(member, Mapping):
            continue
        component_id = str(member.get("component_id", ""))
        for relation in member.get("relations") or []:
            if not isinstance(relation, Mapping):
                issues.append(
                    _issue(
                        "COMPONENT_RELATION_FIELDS",
                        "Each member relation must be an object",
                        segment_id=segment_id,
                        evidence=component_id,
                    )
                )
                continue
            relation_type = str(relation.get("type", ""))
            target_id = str(relation.get("target_member_id", ""))
            if relation_type not in RELATION_TYPES or target_id not in member_by_id or target_id == component_id:
                issues.append(
                    _issue(
                        "COMPONENT_RELATION_FIELDS",
                        "A relation requires a supported type and another member target",
                        segment_id=segment_id,
                        evidence={"component_id": component_id, "relation": relation},
                    )
                )
            if relation_type in {"adjacent", "avoid"}:
                clearance = relation.get("minimum_clearance_pt")
                if (
                    isinstance(clearance, bool)
                    or not isinstance(clearance, numbers.Real)
                    or float(clearance) < 0
                ):
                    issues.append(
                        _issue(
                            "COMPONENT_MINIMUM_CLEARANCE",
                            "adjacent/avoid relations require non-negative minimum_clearance_pt",
                            segment_id=segment_id,
                            evidence={"component_id": component_id, "relation": relation},
                        )
                    )
            if relation_type == "align_center_y":
                maximum = relation.get("maximum_delta_pt")
                if (
                    isinstance(maximum, bool)
                    or not isinstance(maximum, numbers.Real)
                    or float(maximum) < 0
                ):
                    issues.append(
                        _issue(
                            "COMPONENT_ALIGNMENT_TOLERANCE",
                            "align_center_y relations require non-negative maximum_delta_pt",
                            segment_id=segment_id,
                            evidence={"component_id": component_id, "relation": relation},
                        )
                    )
            if relation_type == "align_optical_offset_y":
                expected_offset = relation.get("expected_target_minus_member_center_pt")
                maximum = relation.get("maximum_delta_pt")
                if (
                    isinstance(expected_offset, bool)
                    or not isinstance(expected_offset, numbers.Real)
                    or isinstance(maximum, bool)
                    or not isinstance(maximum, numbers.Real)
                    or float(maximum) < 0
                    or relation.get("measurement_basis")
                    != "actual_candidate_text_span_bbox"
                    or member.get("role")
                    not in {"translatable_live_text", "translatable_vector_outlined_text"}
                    or (member_by_id.get(target_id) or {}).get("role") != "vector_rule"
                ):
                    issues.append(
                        _issue(
                            "COMPONENT_OPTICAL_ALIGNMENT_FIELDS",
                            "align_optical_offset_y requires a text member, vector-rule target, signed expected offset, non-negative tolerance, and actual candidate text-span measurement",
                            segment_id=segment_id,
                            evidence={"component_id": component_id, "relation": relation},
                        )
                    )

    if any(member.get("policy") == "preserve_complete_visual" for member in members) and translatable_members:
        issues.append(
            _issue(
                "PRESERVE_TRANSLATABLE_COMPONENT",
                "preserve_complete_visual cannot cover a group with required translatable members",
                segment_id=segment_id,
                evidence=[str(member.get("component_id")) for member in translatable_members],
            )
        )
    for member in members:
        if (
            isinstance(member, Mapping)
            and member.get("translatability") == "user_preserved"
            and not str(member.get("preservation_basis", "")).strip()
        ):
            issues.append(
                _issue(
                    "USER_PRESERVATION_BASIS_MISSING",
                    "user_preserved requires an explicit preservation_basis",
                    segment_id=segment_id,
                    evidence=member.get("component_id"),
                )
            )

    if segment_role == "translatable_live_text":
        live_members = [
            member
            for member in members
            if isinstance(member, Mapping)
            and member.get("role") == "translatable_live_text"
        ]
        if action != "replace" or mask_policy != "source_text_spans_only":
            issues.append(
                _issue(
                    "LIVE_TEXT_COMPONENT_ACTION",
                    "A translatable live-text segment must use action=replace",
                    segment_id=segment_id,
                )
            )
        if segment.get("extraction_method") != "source_spans":
            issues.append(
                _issue(
                    "LIVE_TEXT_COMPONENT_EXTRACTION",
                    "Live text requires source-span extraction",
                    segment_id=segment_id,
                )
            )
        if render.get("mask_mode") != "remove_text_only" or float(
            render.get("mask_padding_pt", -1)
        ) != 0.0:
            issues.append(
                _issue(
                    "LIVE_TEXT_MASK_POLICY",
                    "Live text requires remove_text_only with zero padding",
                    segment_id=segment_id,
                )
            )
        if len(live_members) != 1:
            issues.append(
                _issue(
                    "LIVE_TEXT_COMPONENT_COUNT",
                    "A live-text segment must bind exactly one live-text member",
                    segment_id=segment_id,
                )
            )
        elif not _bbox_inside(
            render.get("mask_bbox", segment.get("bbox")), live_members[0].get("bbox")
        ):
            issues.append(
                _issue(
                    "LIVE_TEXT_MASK_ESCAPES_COMPONENT",
                    "The live-text mask escapes its declared member bbox",
                    segment_id=segment_id,
                )
            )
    elif segment_role == "translatable_vector_outlined_text":
        vector_members = [
            member
            for member in members
            if isinstance(member, Mapping)
            and member.get("role") == "translatable_vector_outlined_text"
        ]
        if action != "replace_vector_outlined_text" or mask_policy != "none":
            issues.append(
                _issue(
                    "VECTOR_TEXT_COMPONENT_ACTION",
                    "Outlined text requires action=replace_vector_outlined_text and no mask",
                    segment_id=segment_id,
                )
            )
        if segment.get("extraction_method") != "visual_annotation":
            issues.append(
                _issue(
                    "VECTOR_TEXT_COMPONENT_EXTRACTION",
                    "Outlined text requires explicit visual annotation",
                    segment_id=segment_id,
                )
            )
        if any(key in render for key in ("mask_bbox", "mask_mode", "mask_padding_pt")):
            issues.append(
                _issue(
                    "VECTOR_TEXT_MASK_FORBIDDEN",
                    "Outlined-text path replacement cannot declare a rectangular mask",
                    segment_id=segment_id,
                )
            )
        if len(vector_members) != 1 or render.get("vector_member_id") != (
            vector_members[0].get("component_id") if vector_members else None
        ):
            issues.append(
                _issue(
                    "VECTOR_TEXT_MEMBER_BINDING",
                    "Outlined-text render must bind exactly one vector_member_id",
                    segment_id=segment_id,
                )
            )
    elif segment_role == "preserved_component" and action != "preserve":
        issues.append(
            _issue(
                "PRESERVED_COMPONENT_ACTION",
                "A preserved-component segment must use action=preserve",
                segment_id=segment_id,
            )
        )
    return issues


def validate_english_allowlist(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Validate strict v3 English exceptions; older schemas remain historical."""
    if payload.get("schema") != ENGLISH_ALLOWLIST_SCHEMA:
        return []
    issues: list[dict[str, Any]] = []
    scope = payload.get("scope")
    scope_pages = scope.get("pages") if isinstance(scope, Mapping) else None
    if (
        not isinstance(scope, Mapping)
        or not str(scope.get("document_id", "")).strip()
        or not isinstance(scope_pages, list)
        or not scope_pages
        or any(
            isinstance(page, bool) or not isinstance(page, int) or page <= 0
            for page in scope_pages
        )
        or len(scope_pages) != len(set(scope_pages))
    ):
        issues.append(
            {
                "code": "ALLOWLIST_SCOPE",
                "message": "v3 allowlist scope requires document_id and pages",
            }
        )
    entries = [
        *(payload.get("allowed") or []),
        *(payload.get("allowed_ui_english") or []),
        *(payload.get("allowed_visual_english") or []),
    ]
    entry_keys: set[tuple[str, tuple[int, ...], tuple[str, ...]]] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            issues.append(
                {
                    "code": "ALLOWLIST_ENTRY_TYPE",
                    "message": "Every v3 allowlist entry must be an object",
                    "entry_index": index,
                }
            )
            continue
        text = str(entry.get("token") or entry.get("source_text") or "").strip()
        entry_type = str(entry.get("type", "")).strip()
        reason = str(entry.get("reason", "")).strip()
        entry_scope = entry.get("scope")
        basis = entry.get("basis")
        if not text or entry_type not in ALLOWLIST_TYPES:
            issues.append(
                {
                    "code": "ALLOWLIST_ENTRY_TYPE",
                    "message": "Entry needs text and an approved exception type",
                    "entry_index": index,
                }
            )
        if len(reason) < 12 or any(fragment in reason.casefold() for fragment in DIFFICULTY_REASON_FRAGMENTS):
            issues.append(
                {
                    "code": "ALLOWLIST_ENTRY_REASON",
                    "message": "Entry needs a substantive non-tool-difficulty reason",
                    "entry_index": index,
                }
            )
        entry_pages = entry_scope.get("pages") if isinstance(entry_scope, Mapping) else None
        entry_segment_ids = (
            entry_scope.get("segment_ids") if isinstance(entry_scope, Mapping) else None
        )
        if (
            not isinstance(entry_scope, Mapping)
            or not isinstance(entry_pages, list)
            or not entry_pages
            or any(
                isinstance(page, bool) or not isinstance(page, int) or page <= 0
                for page in entry_pages
            )
            or len(entry_pages) != len(set(entry_pages))
            or not isinstance(entry_segment_ids, list)
            or not entry_segment_ids
            or any(not isinstance(value, str) or not value.strip() for value in entry_segment_ids)
            or len(entry_segment_ids) != len(set(entry_segment_ids))
            or entry_scope.get("exact") is not True
        ):
            issues.append(
                {
                    "code": "ALLOWLIST_ENTRY_SCOPE",
                    "message": "Entry scope requires pages, segment_ids, and exact=true",
                    "entry_index": index,
                }
            )
        elif isinstance(scope_pages, list) and not set(entry_pages).issubset(
            set(scope_pages)
        ):
            issues.append(
                {
                    "code": "ALLOWLIST_ENTRY_SCOPE_OUTSIDE_DOCUMENT",
                    "message": "Entry pages must be a subset of the document allowlist scope",
                    "entry_index": index,
                }
            )
        else:
            entry_key = (
                text,
                tuple(sorted(entry_pages)),
                tuple(sorted(entry_segment_ids)),
            )
            if entry_key in entry_keys:
                issues.append(
                    {
                        "code": "ALLOWLIST_ENTRY_DUPLICATE",
                        "message": "Duplicate exact allowlist entry scope is not permitted",
                        "entry_index": index,
                    }
                )
            entry_keys.add(entry_key)
        if (
            not isinstance(basis, Mapping)
            or basis.get("type") not in ALLOWLIST_BASIS_TYPES
            or not str(basis.get("reference", "")).strip()
        ):
            issues.append(
                {
                    "code": "ALLOWLIST_ENTRY_BASIS",
                    "message": "Entry needs an explicit user or policy basis",
                    "entry_index": index,
                }
            )
    return issues


def validate_translation_dependent_geometry(
    member: Mapping[str, Any],
    member_by_id: Mapping[str, Mapping[str, Any]],
    page_rect: Any,
    *,
    segment_id: str,
) -> list[dict[str, Any]]:
    """Validate a manifest-declared bbox dependency without resolving translation text."""

    dependency = member.get("dependent_geometry")
    if dependency is None:
        return []
    component_id = str(member.get("component_id", ""))
    issues: list[dict[str, Any]] = []
    if not isinstance(dependency, Mapping):
        return [
            _issue(
                "TRANSLATION_DEPENDENCY_FIELDS",
                "dependent_geometry must be an object",
                segment_id=segment_id,
                evidence=component_id,
            )
        ]
    policy = str(member.get("policy", ""))
    if policy not in {"adjust_background", "adjust_vector_rule"}:
        issues.append(
            _issue(
                "TRANSLATION_DEPENDENCY_POLICY_UNSUPPORTED",
                "A translation-dependent member requires a supported exact adjustment policy",
                segment_id=segment_id,
                evidence={"component_id": component_id, "policy": policy},
            )
        )
    maximum_delta = dependency.get("maximum_delta_pt")
    minimum_width = dependency.get("minimum_width_pt", 0.0)
    minimum_height = dependency.get("minimum_height_pt", 0.0)
    header_valid = (
        dependency.get("schema") == TRANSLATION_DEPENDENT_GEOMETRY_SCHEMA
        and dependency.get("measurement_basis")
        == "actual_candidate_text_span_bbox"
        and dependency.get("bounds_policy") in {"within_source_bbox", "within_page"}
        and not isinstance(maximum_delta, bool)
        and isinstance(maximum_delta, numbers.Real)
        and float(maximum_delta) >= 0
        and not isinstance(minimum_width, bool)
        and isinstance(minimum_width, numbers.Real)
        and float(minimum_width) >= 0
        and not isinstance(minimum_height, bool)
        and isinstance(minimum_height, numbers.Real)
        and float(minimum_height) >= 0
    )
    if not header_valid:
        issues.append(
            _issue(
                "TRANSLATION_DEPENDENCY_FIELDS",
                "A dependency requires its schema, actual-candidate measurement basis, bounds policy, and non-negative tolerances",
                segment_id=segment_id,
                evidence={"component_id": component_id, "dependency": dependency},
            )
        )
    bindings = dependency.get("edge_bindings")
    if not isinstance(bindings, Mapping) or set(map(str, bindings)) != DEPENDENT_BBOX_EDGES:
        issues.append(
            _issue(
                "TRANSLATION_DEPENDENCY_EDGE_BINDINGS",
                "edge_bindings must define x0, y0, x1, and y1 exactly once",
                segment_id=segment_id,
                evidence={"component_id": component_id, "edge_bindings": bindings},
            )
        )
        return issues
    candidate_drivers: set[str] = set()
    for target_edge, binding in bindings.items():
        if not isinstance(binding, Mapping):
            issues.append(
                _issue(
                    "TRANSLATION_DEPENDENCY_EDGE_BINDING_FIELDS",
                    "Every edge binding must be an object",
                    segment_id=segment_id,
                    evidence={"component_id": component_id, "target_edge": target_edge},
                )
            )
            continue
        basis = str(binding.get("basis", ""))
        source_edge = str(binding.get("edge", ""))
        offset = binding.get("offset_pt")
        driver_id = str(binding.get("member_id", "")).strip()
        valid = (
            basis in DEPENDENT_GEOMETRY_BASIS
            and source_edge in DEPENDENT_BBOX_EDGES
            and not isinstance(offset, bool)
            and isinstance(offset, numbers.Real)
        )
        if basis == "source_bbox":
            valid = valid and not driver_id
        elif basis == "candidate_member_bbox":
            driver = member_by_id.get(driver_id)
            valid = (
                valid
                and bool(driver_id)
                and driver_id != component_id
                and driver is not None
                and driver.get("role")
                in {"translatable_live_text", "translatable_vector_outlined_text"}
            )
            if valid:
                candidate_drivers.add(driver_id)
        if not valid:
            issues.append(
                _issue(
                    "TRANSLATION_DEPENDENCY_EDGE_BINDING_FIELDS",
                    "An edge binding must use a source edge or an actual translated-text member edge with a finite offset",
                    segment_id=segment_id,
                    evidence={
                        "component_id": component_id,
                        "target_edge": str(target_edge),
                        "binding": binding,
                    },
                )
            )
    if not candidate_drivers:
        issues.append(
            _issue(
                "TRANSLATION_DEPENDENCY_DRIVER_MISSING",
                "At least one bbox edge must depend on an actual translated-text member",
                segment_id=segment_id,
                evidence=component_id,
            )
        )
    try:
        if dependency.get("bounds_policy") == "within_page" and not _bbox_inside(
            member["bbox"], page_rect
        ):
            raise ValueError("source member bbox is outside the page")
    except Exception as exc:
        issues.append(
            _issue(
                "TRANSLATION_DEPENDENCY_BOUNDS",
                str(exc),
                segment_id=segment_id,
                evidence=component_id,
            )
        )
    return issues


def resolve_translation_dependent_bbox(
    member: Mapping[str, Any],
    candidate_bboxes: Mapping[str, Any],
    *,
    page_rect: Any | None = None,
) -> tuple[list[float], dict[str, Any]]:
    """Resolve a dependent member bbox from actual translated-member geometry."""

    component_id = str(member.get("component_id", ""))
    dependency = member.get("dependent_geometry")
    if not isinstance(dependency, Mapping):
        raise CompoundComponentError(
            "TRANSLATION_DEPENDENCY_FIELDS",
            "The adjusted member has no valid dependent_geometry object",
            component_id,
        )
    if dependency.get("schema") != TRANSLATION_DEPENDENT_GEOMETRY_SCHEMA:
        raise CompoundComponentError(
            "TRANSLATION_DEPENDENCY_FIELDS",
            "The dependent geometry schema is missing or unsupported",
            component_id,
        )
    if dependency.get("measurement_basis") != "actual_candidate_text_span_bbox":
        raise CompoundComponentError(
            "TRANSLATION_DEPENDENCY_MEASUREMENT_BASIS",
            "Dependent geometry must be based on the actual candidate text span bbox",
            component_id,
        )
    bindings = dependency.get("edge_bindings")
    if not isinstance(bindings, Mapping) or set(map(str, bindings)) != DEPENDENT_BBOX_EDGES:
        raise CompoundComponentError(
            "TRANSLATION_DEPENDENCY_EDGE_BINDINGS",
            "Dependent geometry requires exactly four bbox edge bindings",
            component_id,
        )
    source_bbox = _normalize_bbox(member.get("bbox") or ())
    edge_index = {"x0": 0, "y0": 1, "x1": 2, "y1": 3}
    resolved: dict[str, float] = {}
    driver_evidence: dict[str, list[float]] = {}
    for target_edge in ("x0", "y0", "x1", "y1"):
        binding = bindings[target_edge]
        if not isinstance(binding, Mapping):
            raise CompoundComponentError(
                "TRANSLATION_DEPENDENCY_EDGE_BINDING_FIELDS",
                "A dependent edge binding is not an object",
                {"component_id": component_id, "target_edge": target_edge},
            )
        basis = str(binding.get("basis", ""))
        source_edge = str(binding.get("edge", ""))
        if source_edge not in edge_index:
            raise CompoundComponentError(
                "TRANSLATION_DEPENDENCY_EDGE_BINDING_FIELDS",
                "A dependent edge binding uses an unsupported source edge",
                {"component_id": component_id, "target_edge": target_edge},
            )
        if basis == "source_bbox":
            basis_bbox = source_bbox
            driver_id = None
        elif basis == "candidate_member_bbox":
            driver_id = str(binding.get("member_id", "")).strip()
            if not driver_id or driver_id not in candidate_bboxes:
                raise CompoundComponentError(
                    "TRANSLATION_DEPENDENCY_DRIVER_BBOX_MISSING",
                    "The actual candidate driver bbox is missing or ambiguous",
                    {"component_id": component_id, "driver_member_id": driver_id},
                )
            basis_bbox = _normalize_bbox(candidate_bboxes[driver_id])
            driver_evidence[driver_id] = basis_bbox
        else:
            raise CompoundComponentError(
                "TRANSLATION_DEPENDENCY_EDGE_BINDING_FIELDS",
                "A dependent edge binding uses an unsupported basis",
                {"component_id": component_id, "target_edge": target_edge, "basis": basis},
            )
        resolved[target_edge] = round(
            basis_bbox[edge_index[source_edge]]
            + _finite_number(binding.get("offset_pt"), f"{target_edge}.offset_pt"),
            6,
        )
    target_bbox = [
        resolved["x0"],
        resolved["y0"],
        resolved["x1"],
        resolved["y1"],
    ]
    width = target_bbox[2] - target_bbox[0]
    height = target_bbox[3] - target_bbox[1]
    minimum_width = _finite_number(dependency.get("minimum_width_pt", 0.0), "minimum_width_pt")
    minimum_height = _finite_number(
        dependency.get("minimum_height_pt", 0.0), "minimum_height_pt"
    )
    if width <= 0 or height <= 0 or width + 0.000001 < minimum_width or height + 0.000001 < minimum_height:
        raise CompoundComponentError(
            "TRANSLATION_DEPENDENCY_TARGET_GEOMETRY",
            "Resolved dependent geometry is empty, inverted, or below its declared minimum size",
            {
                "component_id": component_id,
                "target_bbox": target_bbox,
                "minimum_width_pt": minimum_width,
                "minimum_height_pt": minimum_height,
            },
        )
    bounds_policy = str(dependency.get("bounds_policy", ""))
    if bounds_policy == "within_source_bbox":
        inside = _bbox_inside(target_bbox, source_bbox, tolerance=0.01)
    elif bounds_policy == "within_page" and page_rect is not None:
        inside = _bbox_inside(target_bbox, page_rect, tolerance=0.01)
    else:
        raise CompoundComponentError(
            "TRANSLATION_DEPENDENCY_BOUNDS",
            "The dependency bounds policy is unsupported or lacks the page bbox",
            {"component_id": component_id, "bounds_policy": bounds_policy},
        )
    if not inside:
        raise CompoundComponentError(
            "TRANSLATION_DEPENDENCY_BOUNDS",
            "Resolved dependent geometry escapes its declared bounds",
            {
                "component_id": component_id,
                "target_bbox": target_bbox,
                "bounds_policy": bounds_policy,
            },
        )
    policy = str(member.get("policy", ""))
    translation_delta: list[float] | None = None
    if policy == "adjust_vector_rule":
        dx0 = target_bbox[0] - source_bbox[0]
        dx1 = target_bbox[2] - source_bbox[2]
        dy0 = target_bbox[1] - source_bbox[1]
        dy1 = target_bbox[3] - source_bbox[3]
        if (
            abs(dx0 - dx1) > COORDINATE_TOLERANCE_PT
            or abs(dy0 - dy1) > COORDINATE_TOLERANCE_PT
            or (abs(dx0) <= COORDINATE_TOLERANCE_PT and abs(dy0) <= COORDINATE_TOLERANCE_PT)
        ):
            raise CompoundComponentError(
                "TRANSLATION_DEPENDENCY_VECTOR_TRANSLATION",
                "An exact dependent vector rule must resolve to one non-zero translation without resize",
                {"component_id": component_id, "source_bbox": source_bbox, "target_bbox": target_bbox},
            )
        translation_delta = [round(dx0, 6), round(dy0, 6)]
    elif policy != "adjust_background":
        raise CompoundComponentError(
            "TRANSLATION_DEPENDENCY_POLICY_UNSUPPORTED",
            "The resolved dependent member has no supported exact adjustment route",
            {"component_id": component_id, "policy": policy},
        )
    evidence = {
        "schema": TRANSLATION_DEPENDENT_GEOMETRY_SCHEMA,
        "component_id": component_id,
        "measurement_basis": dependency.get("measurement_basis"),
        "source_bbox": source_bbox,
        "driver_candidate_bboxes": driver_evidence,
        "edge_bindings": copy.deepcopy(bindings),
        "resolved_target_bbox": target_bbox,
        "bounds_policy": bounds_policy,
        "translation_delta_pt": translation_delta,
        "status": "RESOLVED_FAIL_CLOSED",
    }
    return target_bbox, evidence


def evaluate_translation_dependent_geometry(
    contract: Mapping[str, Any],
    candidate_bboxes: Mapping[str, Any],
    *,
    page_rect: Any | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Compare resolved dependency formulas with actual adjusted candidate members."""

    results: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    for member in contract.get("members") or []:
        if not isinstance(member, Mapping) or member.get("dependent_geometry") is None:
            continue
        component_id = str(member.get("component_id", ""))
        try:
            expected_bbox, resolution = resolve_translation_dependent_bbox(
                member, candidate_bboxes, page_rect=page_rect
            )
            if component_id not in candidate_bboxes:
                raise CompoundComponentError(
                    "TRANSLATION_DEPENDENCY_TARGET_BBOX_MISSING",
                    "The adjusted dependent member is missing from candidate evidence",
                    component_id,
                )
            actual_bbox = _normalize_bbox(candidate_bboxes[component_id])
            deltas = {
                edge: abs(actual_bbox[index] - expected_bbox[index])
                for index, edge in enumerate(("x0", "y0", "x1", "y1"))
            }
            maximum = float((member.get("dependent_geometry") or {})["maximum_delta_pt"])
            passed = all(delta <= maximum + 0.000001 for delta in deltas.values())
            result = {
                "component_id": component_id,
                "policy": member.get("policy"),
                "measurement_basis": resolution["measurement_basis"],
                "driver_candidate_bboxes": resolution["driver_candidate_bboxes"],
                "expected_target_bbox": expected_bbox,
                "actual_candidate_bbox": actual_bbox,
                "absolute_edge_deltas_pt": {
                    edge: round(delta, 6) for edge, delta in deltas.items()
                },
                "maximum_delta_pt": round(maximum, 6),
                "status": "PASS" if passed else "FAIL",
            }
            results.append(result)
            if not passed:
                issues.append(
                    {
                        "code": "TRANSLATION_DEPENDENT_GEOMETRY_MISMATCH",
                        "evidence": result,
                    }
                )
        except (CompoundComponentError, KeyError, TypeError, ValueError) as exc:
            issues.append(
                {
                    "code": (
                        exc.code
                        if isinstance(exc, CompoundComponentError)
                        else "TRANSLATION_DEPENDENCY_EVALUATION_FAILED"
                    ),
                    "component_id": component_id,
                    "evidence": exc.as_dict() if isinstance(exc, CompoundComponentError) else str(exc),
                }
            )
    return results, issues


REPEATED_LAYOUT_METRICS = {
    "x0",
    "y0",
    "x1",
    "y1",
    "width",
    "height",
    "center_x",
    "center_y",
}
DEPENDENT_BBOX_EDGES = {"x0", "y0", "x1", "y1"}
DEPENDENT_GEOMETRY_BASIS = {"source_bbox", "candidate_member_bbox"}
VISIBLE_LAYOUT_METRICS = {
    "start",
    "end",
    "length",
    "thickness",
    "center_cross",
}


def _local_bbox_metrics(value: Any, anchor: Any) -> dict[str, float]:
    box = _normalize_bbox(value)
    anchor_box = _normalize_bbox(anchor)
    local_x0 = box[0] - anchor_box[0]
    local_y0 = box[1] - anchor_box[1]
    local_x1 = box[2] - anchor_box[0]
    local_y1 = box[3] - anchor_box[1]
    return {
        "x0": local_x0,
        "y0": local_y0,
        "x1": local_x1,
        "y1": local_y1,
        "width": box[2] - box[0],
        "height": box[3] - box[1],
        "center_x": (local_x0 + local_x1) / 2,
        "center_y": (local_y0 + local_y1) / 2,
    }


def evaluate_repeated_component_layouts(
    contracts: Iterable[Any], candidate_bboxes: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Compare repeated component instances in anchor-local coordinates.

    A contract names corresponding members semantically (for example marker,
    label, and rule) and explicitly selects which local bbox metrics must be
    identical. Omitting label width is therefore an auditable manifest choice,
    not a heuristic inferred from translated character count.
    """

    results: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    seen_contract_ids: set[str] = set()
    for contract_index, contract in enumerate(contracts):
        if not isinstance(contract, Mapping):
            issues.append(
                {
                    "code": "REPEATED_LAYOUT_CONTRACT_FIELDS",
                    "contract_index": contract_index,
                    "message": "A repeated-component layout contract must be an object",
                }
            )
            continue
        contract_id = str(contract.get("contract_id", "")).strip()
        normalization = str(contract.get("normalization", ""))
        maximum_delta = contract.get("maximum_delta_pt")
        instances = contract.get("instances")
        compare = contract.get("compare")
        valid_header = (
            bool(contract_id)
            and contract_id not in seen_contract_ids
            and normalization == "anchor_top_left"
            and not isinstance(maximum_delta, bool)
            and isinstance(maximum_delta, numbers.Real)
            and float(maximum_delta) >= 0
            and isinstance(instances, list)
            and len(instances) >= 2
            and isinstance(compare, Mapping)
            and bool(compare)
        )
        if not valid_header:
            issues.append(
                {
                    "code": "REPEATED_LAYOUT_CONTRACT_FIELDS",
                    "contract_index": contract_index,
                    "contract_id": contract_id,
                    "message": "Contract requires a unique id, anchor_top_left normalization, non-negative tolerance, at least two instances, and explicit metric comparisons",
                }
            )
            continue
        seen_contract_ids.add(contract_id)

        compare_metrics: dict[str, list[str]] = {}
        compare_valid = True
        for semantic_name, metric_names in compare.items():
            semantic = str(semantic_name).strip()
            if (
                not semantic
                or not isinstance(metric_names, list)
                or not metric_names
                or any(str(metric) not in REPEATED_LAYOUT_METRICS for metric in metric_names)
                or len({str(metric) for metric in metric_names}) != len(metric_names)
            ):
                compare_valid = False
                break
            compare_metrics[semantic] = [str(metric) for metric in metric_names]
        if not compare_valid:
            issues.append(
                {
                    "code": "REPEATED_LAYOUT_COMPARE_FIELDS",
                    "contract_id": contract_id,
                    "message": "Each semantic member requires a non-empty unique list of supported local bbox metrics",
                }
            )
            continue

        instance_metrics: list[dict[str, Any]] = []
        instance_ids: set[str] = set()
        instance_failed = False
        for instance_index, instance in enumerate(instances):
            if not isinstance(instance, Mapping):
                issues.append(
                    {
                        "code": "REPEATED_LAYOUT_INSTANCE_FIELDS",
                        "contract_id": contract_id,
                        "instance_index": instance_index,
                    }
                )
                instance_failed = True
                continue
            instance_id = str(instance.get("instance_id", "")).strip()
            anchor_id = str(instance.get("anchor_member_id", "")).strip()
            member_ids = instance.get("member_ids")
            if (
                not instance_id
                or instance_id in instance_ids
                or not anchor_id
                or not isinstance(member_ids, Mapping)
                or set(map(str, member_ids)) != set(compare_metrics)
            ):
                issues.append(
                    {
                        "code": "REPEATED_LAYOUT_INSTANCE_FIELDS",
                        "contract_id": contract_id,
                        "instance_index": instance_index,
                        "instance_id": instance_id,
                        "message": "Each instance requires a unique id, anchor member, and exactly the declared semantic members",
                    }
                )
                instance_failed = True
                continue
            instance_ids.add(instance_id)
            anchor_bbox = candidate_bboxes.get(anchor_id)
            if anchor_bbox is None:
                issues.append(
                    {
                        "code": "REPEATED_LAYOUT_BBOX_MISSING",
                        "contract_id": contract_id,
                        "instance_id": instance_id,
                        "component_id": anchor_id,
                    }
                )
                instance_failed = True
                continue
            semantic_metrics: dict[str, Any] = {}
            for semantic, component_value in member_ids.items():
                component_id = str(component_value).strip()
                bbox = candidate_bboxes.get(component_id)
                if not component_id or bbox is None:
                    issues.append(
                        {
                            "code": "REPEATED_LAYOUT_BBOX_MISSING",
                            "contract_id": contract_id,
                            "instance_id": instance_id,
                            "component_id": component_id,
                            "semantic_member": str(semantic),
                        }
                    )
                    instance_failed = True
                    continue
                semantic_metrics[str(semantic)] = {
                    "component_id": component_id,
                    "bbox": _normalize_bbox(bbox),
                    "local_metrics": _local_bbox_metrics(bbox, anchor_bbox),
                }
            instance_metrics.append(
                {
                    "instance_id": instance_id,
                    "anchor_member_id": anchor_id,
                    "anchor_bbox": _normalize_bbox(anchor_bbox),
                    "members": semantic_metrics,
                }
            )
        if instance_failed or len(instance_metrics) != len(instances):
            continue

        reference = instance_metrics[0]
        for candidate in instance_metrics[1:]:
            for semantic, metric_names in compare_metrics.items():
                reference_member = reference["members"].get(semantic)
                candidate_member = candidate["members"].get(semantic)
                if reference_member is None or candidate_member is None:
                    issues.append(
                        {
                            "code": "REPEATED_LAYOUT_INSTANCE_FIELDS",
                            "contract_id": contract_id,
                            "semantic_member": semantic,
                        }
                    )
                    continue
                deltas = {
                    metric: abs(
                        float(candidate_member["local_metrics"][metric])
                        - float(reference_member["local_metrics"][metric])
                    )
                    for metric in metric_names
                }
                passed = all(
                    delta <= float(maximum_delta) + 0.000001
                    for delta in deltas.values()
                )
                result = {
                    "contract_id": contract_id,
                    "reference_instance_id": reference["instance_id"],
                    "candidate_instance_id": candidate["instance_id"],
                    "semantic_member": semantic,
                    "normalization": normalization,
                    "compared_metrics": metric_names,
                    "reference_local_metrics": {
                        metric: round(
                            float(reference_member["local_metrics"][metric]), 6
                        )
                        for metric in metric_names
                    },
                    "candidate_local_metrics": {
                        metric: round(
                            float(candidate_member["local_metrics"][metric]), 6
                        )
                        for metric in metric_names
                    },
                    "absolute_deltas_pt": {
                        metric: round(delta, 6) for metric, delta in deltas.items()
                    },
                    "maximum_delta_pt": round(float(maximum_delta), 6),
                    "status": "PASS" if passed else "FAIL",
                }
                results.append(result)
                if not passed:
                    issues.append(
                        {
                            "code": "REPEATED_COMPONENT_LAYOUT_MISMATCH",
                            "evidence": result,
                        }
                    )
    return results, issues


def _composited_visible_horizontal_metrics(
    subject_bbox: Any,
    occluder_bboxes: Sequence[Any],
    anchor_bbox: Any,
) -> tuple[dict[str, float], dict[str, Any]]:
    subject = _normalize_bbox(subject_bbox)
    anchor = _normalize_bbox(anchor_bbox)
    width = subject[2] - subject[0]
    thickness = subject[3] - subject[1]
    if width <= 0 or thickness <= 0 or width <= thickness:
        raise CompoundComponentError(
            "VISIBLE_LAYOUT_SUBJECT_GEOMETRY",
            "A horizontal visible-layout subject must have positive width and thickness",
            subject,
        )
    intervals: list[tuple[float, float]] = [(subject[0], subject[2])]
    normalized_occluders: list[list[float]] = []
    for index, value in enumerate(occluder_bboxes):
        occluder = _normalize_bbox(value)
        normalized_occluders.append(occluder)
        if (
            occluder[1] > subject[1] + COORDINATE_TOLERANCE_PT
            or occluder[3] < subject[3] - COORDINATE_TOLERANCE_PT
        ):
            raise CompoundComponentError(
                "VISIBLE_LAYOUT_OCCLUDER_CROSS_AXIS",
                "A declared opaque occluder does not cover the full rule thickness",
                {"occluder_index": index, "subject_bbox": subject, "occluder_bbox": occluder},
            )
        cut_start = max(subject[0], occluder[0])
        cut_end = min(subject[2], occluder[2])
        if cut_end <= cut_start + 0.000001:
            raise CompoundComponentError(
                "VISIBLE_LAYOUT_OCCLUDER_NO_INTERSECTION",
                "A declared opaque occluder does not intersect the visible-layout subject",
                {"occluder_index": index, "subject_bbox": subject, "occluder_bbox": occluder},
            )
        next_intervals: list[tuple[float, float]] = []
        for start, end in intervals:
            if cut_end <= start + 0.000001 or cut_start >= end - 0.000001:
                next_intervals.append((start, end))
                continue
            if cut_start > start + 0.000001:
                next_intervals.append((start, min(cut_start, end)))
            if cut_end < end - 0.000001:
                next_intervals.append((max(cut_end, start), end))
        intervals = [item for item in next_intervals if item[1] - item[0] > 0.000001]
    if len(intervals) != 1:
        raise CompoundComponentError(
            "VISIBLE_LAYOUT_INTERVAL_NOT_UNIQUE",
            "Opaque compositing must leave exactly one uniquely measurable visible interval",
            {
                "subject_bbox": subject,
                "occluder_bboxes": normalized_occluders,
                "visible_intervals": intervals,
            },
        )
    start, end = intervals[0]
    metrics = {
        "start": start - anchor[0],
        "end": end - anchor[0],
        "length": end - start,
        "thickness": thickness,
        "center_cross": ((subject[1] + subject[3]) / 2) - anchor[1],
    }
    return metrics, {
        "subject_bbox": subject,
        "anchor_bbox": anchor,
        "occluder_bboxes": normalized_occluders,
        "visible_interval": [start, end],
    }


def evaluate_composited_visible_layouts(
    contracts: Iterable[Any], candidate_bboxes: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Compare effective visible rule geometry after declared opaque occlusion."""

    results: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    seen_contract_ids: set[str] = set()
    for contract_index, contract in enumerate(contracts):
        if not isinstance(contract, Mapping):
            issues.append(
                {
                    "code": "VISIBLE_LAYOUT_CONTRACT_FIELDS",
                    "contract_index": contract_index,
                    "message": "A composited-visible layout contract must be an object",
                }
            )
            continue
        contract_id = str(contract.get("contract_id", "")).strip()
        maximum = contract.get("maximum_delta_pt")
        compare = contract.get("compare")
        instances = contract.get("instances")
        valid_header = (
            contract.get("schema") == COMPOSITED_VISIBLE_LAYOUT_SCHEMA
            and bool(contract_id)
            and contract_id not in seen_contract_ids
            and contract.get("normalization") == "anchor_top_left"
            and contract.get("axis") == "horizontal"
            and not isinstance(maximum, bool)
            and isinstance(maximum, numbers.Real)
            and float(maximum) >= 0
            and isinstance(compare, list)
            and bool(compare)
            and len({str(value) for value in compare}) == len(compare)
            and all(str(value) in VISIBLE_LAYOUT_METRICS for value in compare)
            and isinstance(instances, list)
            and len(instances) >= 2
        )
        if not valid_header:
            issues.append(
                {
                    "code": "VISIBLE_LAYOUT_CONTRACT_FIELDS",
                    "contract_index": contract_index,
                    "contract_id": contract_id,
                    "message": "A visible-layout contract requires its schema, horizontal anchor-local normalization, non-negative tolerance, supported metrics, and at least two instances",
                }
            )
            continue
        seen_contract_ids.add(contract_id)
        instance_ids: set[str] = set()
        resolved_instances: list[dict[str, Any]] = []
        instance_failed = False
        for instance_index, instance in enumerate(instances):
            if not isinstance(instance, Mapping):
                issues.append(
                    {
                        "code": "VISIBLE_LAYOUT_INSTANCE_FIELDS",
                        "contract_id": contract_id,
                        "instance_index": instance_index,
                    }
                )
                instance_failed = True
                continue
            instance_id = str(instance.get("instance_id", "")).strip()
            anchor_id = str(instance.get("anchor_member_id", "")).strip()
            subject_id = str(instance.get("subject_member_id", "")).strip()
            occluder_ids = instance.get("opaque_occluder_member_ids")
            if (
                not instance_id
                or instance_id in instance_ids
                or not anchor_id
                or not subject_id
                or not isinstance(occluder_ids, list)
                or not occluder_ids
                or len({str(value) for value in occluder_ids}) != len(occluder_ids)
                or any(not str(value).strip() for value in occluder_ids)
            ):
                issues.append(
                    {
                        "code": "VISIBLE_LAYOUT_INSTANCE_FIELDS",
                        "contract_id": contract_id,
                        "instance_index": instance_index,
                        "instance_id": instance_id,
                    }
                )
                instance_failed = True
                continue
            instance_ids.add(instance_id)
            missing = [
                component_id
                for component_id in [anchor_id, subject_id, *map(str, occluder_ids)]
                if component_id not in candidate_bboxes
            ]
            if missing:
                issues.append(
                    {
                        "code": "VISIBLE_LAYOUT_BBOX_MISSING",
                        "contract_id": contract_id,
                        "instance_id": instance_id,
                        "component_ids": missing,
                    }
                )
                instance_failed = True
                continue
            try:
                metrics, evidence = _composited_visible_horizontal_metrics(
                    candidate_bboxes[subject_id],
                    [candidate_bboxes[str(value)] for value in occluder_ids],
                    candidate_bboxes[anchor_id],
                )
                resolved_instances.append(
                    {
                        "instance_id": instance_id,
                        "anchor_member_id": anchor_id,
                        "subject_member_id": subject_id,
                        "opaque_occluder_member_ids": [str(value) for value in occluder_ids],
                        "metrics": metrics,
                        **evidence,
                    }
                )
            except CompoundComponentError as exc:
                issues.append(
                    {
                        "code": exc.code,
                        "contract_id": contract_id,
                        "instance_id": instance_id,
                        "evidence": exc.as_dict(),
                    }
                )
                instance_failed = True
        if instance_failed or len(resolved_instances) != len(instances):
            continue
        reference = resolved_instances[0]
        for candidate in resolved_instances[1:]:
            deltas = {
                str(metric): abs(
                    float(candidate["metrics"][str(metric)])
                    - float(reference["metrics"][str(metric)])
                )
                for metric in compare
            }
            passed = all(delta <= float(maximum) + 0.000001 for delta in deltas.values())
            result = {
                "contract_id": contract_id,
                "reference_instance": reference,
                "candidate_instance": candidate,
                "compared_metrics": [str(value) for value in compare],
                "absolute_deltas_pt": {
                    metric: round(delta, 6) for metric, delta in deltas.items()
                },
                "maximum_delta_pt": round(float(maximum), 6),
                "measurement_basis": "actual_candidate_member_bboxes_after_declared_opaque_compositing",
                "status": "PASS" if passed else "FAIL",
            }
            results.append(result)
            if not passed:
                issues.append(
                    {
                        "code": "COMPOSITED_VISIBLE_LAYOUT_MISMATCH",
                        "evidence": result,
                    }
                )
    return results, issues


def evaluate_member_relations(
    contract: Mapping[str, Any], candidate_bboxes: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    results: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    for member in contract.get("members") or []:
        if not isinstance(member, Mapping):
            continue
        component_id = str(member.get("component_id", ""))
        first_bbox = candidate_bboxes.get(component_id)
        if first_bbox is None:
            issues.append(
                {
                    "code": "COMPONENT_CANDIDATE_BBOX_MISSING",
                    "component_id": component_id,
                }
            )
            continue
        for relation in member.get("relations") or []:
            target_id = str(relation.get("target_member_id", ""))
            second_bbox = candidate_bboxes.get(target_id)
            if second_bbox is None:
                issues.append(
                    {
                        "code": "COMPONENT_RELATION_TARGET_BBOX_MISSING",
                        "component_id": component_id,
                        "target_member_id": target_id,
                    }
                )
                continue
            relation_type = str(relation.get("type", ""))
            intersection = bbox_intersection_area(first_bbox, second_bbox)
            clearance = bbox_clearance_pt(first_bbox, second_bbox)
            minimum = float(relation.get("minimum_clearance_pt", 0.0))
            first_normalized = _normalize_bbox(first_bbox)
            second_normalized = _normalize_bbox(second_bbox)
            center_y_delta = abs(
                (first_normalized[1] + first_normalized[3]) / 2
                - (second_normalized[1] + second_normalized[3]) / 2
            )
            target_minus_member_center = (
                (second_normalized[1] + second_normalized[3]) / 2
                - (first_normalized[1] + first_normalized[3]) / 2
            )
            expected_optical_offset = float(
                relation.get("expected_target_minus_member_center_pt", 0.0)
            )
            optical_offset_error = abs(
                target_minus_member_center - expected_optical_offset
            )
            maximum_delta = float(relation.get("maximum_delta_pt", 0.0))
            passed = True
            if relation_type == "contains":
                passed = _bbox_inside(second_bbox, first_bbox)
            elif relation_type in {"adjacent", "avoid"}:
                passed = intersection <= 0.000001 and clearance + 0.000001 >= minimum
            elif relation_type == "align_center_y":
                passed = center_y_delta <= maximum_delta + 0.000001
            elif relation_type == "align_optical_offset_y":
                passed = optical_offset_error <= maximum_delta + 0.000001
            result = {
                "component_id": component_id,
                "target_member_id": target_id,
                "type": relation_type,
                "component_bbox": _normalize_bbox(first_bbox),
                "target_bbox": _normalize_bbox(second_bbox),
                "intersection_area_pt2": round(intersection, 6),
                "clearance_pt": round(clearance, 6),
                "minimum_clearance_pt": round(minimum, 6),
                "minimum_clearance_px_300dpi": round(minimum * 300 / 72, 3),
                "center_y_delta_pt": round(center_y_delta, 6),
                "maximum_delta_pt": round(maximum_delta, 6),
                "center_y_delta_px_300dpi": round(center_y_delta * 300 / 72, 3),
                "target_minus_member_center_pt": round(target_minus_member_center, 6),
                "expected_target_minus_member_center_pt": round(
                    expected_optical_offset, 6
                ),
                "optical_offset_error_pt": round(optical_offset_error, 6),
                "optical_offset_error_px_300dpi": round(
                    optical_offset_error * 300 / 72, 3
                ),
                "measurement_basis": relation.get("measurement_basis"),
                "status": "PASS" if passed else "FAIL",
            }
            results.append(result)
            if not passed:
                issues.append(
                    {
                        "code": (
                            "COMPONENT_CONTAINMENT_FAILED"
                            if relation_type == "contains"
                            else (
                                "COMPONENT_ALIGNMENT_FAILED"
                                if relation_type
                                in {"align_center_y", "align_optical_offset_y"}
                                else "COMPONENT_MINIMUM_CLEARANCE_FAILED"
                            )
                        ),
                        "evidence": result,
                    }
                )
    return results, issues


def union_bboxes(values: Iterable[Any]) -> list[float]:
    boxes = [_normalize_bbox(value) for value in values]
    if not boxes:
        raise ValueError("At least one bbox is required")
    return [
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    ]


def _actual_candidate_text_bbox(
    candidate_page: Any, report_items: Sequence[Mapping[str, Any]]
) -> list[float]:
    records = text_span_records(candidate_page)
    matched_bboxes: list[list[float]] = []
    used_records: set[int] = set()
    for item in report_items:
        target = _normalize_bbox(item.get("target_bbox") or ())
        tolerance = max(3.0, float(item.get("used_font_size_pt") or 0.0) * 0.35)
        for line in item.get("rendered_lines") or []:
            text = str(line.get("text", ""))
            matches = []
            for index, record in enumerate(records):
                if index in used_records or record["text"] != text:
                    continue
                bbox = _normalize_bbox(record["bbox"])
                center_x = (bbox[0] + bbox[2]) / 2
                center_y = (bbox[1] + bbox[3]) / 2
                if (
                    target[0] - tolerance <= center_x <= target[2] + tolerance
                    and target[1] - tolerance <= center_y <= target[3] + tolerance
                ):
                    matches.append(index)
            if len(matches) != 1:
                raise CompoundComponentError(
                    "CANDIDATE_TEXT_SPAN_MATCH_COUNT",
                    "A rendered replacement line is missing or ambiguous in the reopened candidate",
                    {
                        "text": text,
                        "target_bbox": target,
                        "match_indices": matches,
                    },
                )
            used_records.add(matches[0])
            matched_bboxes.append(records[matches[0]]["bbox"])
    if not matched_bboxes:
        raise CompoundComponentError(
            "CANDIDATE_TEXT_SPAN_MISSING",
            "No actual candidate text-span bbox was found for the replacement member",
        )
    return union_bboxes(matched_bboxes)


def candidate_member_bboxes(
    contract: Mapping[str, Any],
    report_segments: Sequence[Mapping[str, Any]],
    *,
    candidate_page: Any | None = None,
    adjusted_member_bboxes: Mapping[str, Any] | None = None,
    require_adjusted_members: bool = True,
) -> dict[str, list[float]]:
    group_id = str(contract.get("group_id", ""))
    related_reports = [
        item
        for item in report_segments
        if str((item.get("component_contract") or {}).get("group_id", "")) == group_id
    ]
    result: dict[str, list[float]] = {}
    adjusted = adjusted_member_bboxes or {}
    for member in contract.get("members") or []:
        component_id = str(member.get("component_id", ""))
        policy = str(member.get("policy", ""))
        if policy in {"adjust_background", "adjust_vector_rule"}:
            if component_id in adjusted:
                result[component_id] = _normalize_bbox(adjusted[component_id])
            elif "target_bbox" in member:
                result[component_id] = _normalize_bbox(member["target_bbox"])
            elif require_adjusted_members:
                raise CompoundComponentError(
                    "TRANSLATION_DEPENDENCY_TARGET_BBOX_MISSING",
                    "A dynamically adjusted member has no rebuild-report target bbox",
                    component_id,
                )
            continue
        if policy in {"replace_live_text", "replace_vector_outlined_text"}:
            matching_reports = [
                item
                for item in related_reports
                if (
                    (policy == "replace_live_text" and item.get("action") == "replace")
                    or (
                        policy == "replace_vector_outlined_text"
                        and item.get("action") == "replace_vector_outlined_text"
                    )
                )
                and (
                    policy != "replace_live_text"
                    or not item.get("text_member_id")
                    or item.get("text_member_id") == component_id
                )
                and (
                    policy != "replace_vector_outlined_text"
                    or item.get("vector_member_id") == component_id
                )
            ]
            if matching_reports and candidate_page is not None:
                result[component_id] = _actual_candidate_text_bbox(
                    candidate_page, matching_reports
                )
            else:
                line_bboxes = [
                    line["bbox"]
                    for item in matching_reports
                    for line in item.get("rendered_lines") or []
                ]
                if line_bboxes:
                    result[component_id] = union_bboxes(line_bboxes)
            continue
        result[component_id] = _normalize_bbox(member["bbox"])
    return result


def _member_content_streams(member: Mapping[str, Any]) -> dict[int, str]:
    streams = (member.get("source_evidence") or {}).get("content_streams") or []
    return {int(item["xref"]): str(item["sha256"]).upper() for item in streams}


def verify_member_source_evidence(
    doc: Any,
    page: Any,
    member: Mapping[str, Any],
    *,
    drawing_records_cache: Sequence[Mapping[str, Any]] | None = None,
    content_path_records_cache: Mapping[int, Sequence[Mapping[str, Any]]] | None = None,
) -> dict[str, Any]:
    component_id = str(member.get("component_id", ""))
    evidence = member.get("source_evidence") or {}
    actual_page_xref = int(doc.page_xref(page.number))
    declared_page_xref = int(evidence.get("page_object_xref", 0))
    if actual_page_xref != declared_page_xref:
        raise CompoundComponentError(
            "SOURCE_PAGE_XREF_MISMATCH",
            "The source page object xref differs from the member declaration",
            {
                "component_id": component_id,
                "declared": declared_page_xref,
                "actual": actual_page_xref,
            },
        )
    object_results: list[dict[str, Any]] = []
    for xref in evidence.get("object_xrefs") or []:
        xref = int(xref)
        if xref <= 0 or xref >= int(doc.xref_length()):
            raise CompoundComponentError(
                "SOURCE_OBJECT_XREF_MISMATCH",
                "A declared source object xref is outside the source xref table",
                {"component_id": component_id, "xref": xref},
            )
        try:
            object_text = doc.xref_object(xref, compressed=False)
        except Exception as exc:
            raise CompoundComponentError(
                "SOURCE_OBJECT_XREF_UNREADABLE",
                "A declared source object xref cannot be read",
                {"component_id": component_id, "xref": xref, "error": str(exc)},
            ) from exc
        object_results.append(
            {
                "xref": xref,
                "object_sha256": hashlib.sha256(
                    object_text.encode("utf-8")
                ).hexdigest().upper(),
                "readable": True,
            }
        )
    actual_stream_xrefs = [int(value) for value in page.get_contents()]
    declared_streams = _member_content_streams(member)
    stream_results: list[dict[str, Any]] = []
    for xref, expected_hash in declared_streams.items():
        if xref not in actual_stream_xrefs:
            raise CompoundComponentError(
                "SOURCE_CONTENT_STREAM_XREF_MISMATCH",
                "A declared source content stream xref is absent",
                {"component_id": component_id, "xref": xref},
            )
        actual_hash = hashlib.sha256(doc.xref_stream(xref)).hexdigest().upper()
        if actual_hash != expected_hash:
            raise CompoundComponentError(
                "SOURCE_CONTENT_STREAM_SHA256_MISMATCH",
                "A declared source content stream changed",
                {
                    "component_id": component_id,
                    "xref": xref,
                    "declared": expected_hash,
                    "actual": actual_hash,
                },
            )
        stream_results.append({"xref": xref, "sha256": actual_hash, "match": True})

    path_set = evidence.get("ordered_path_signatures") or {}
    drawing_declarations = path_set.get("drawing_signatures") or []
    source_drawings = (
        list(drawing_records_cache)
        if drawing_records_cache is not None
        else drawing_records(page)
    )
    drawing_results: list[dict[str, Any]] = []
    for declaration_index, declaration in enumerate(drawing_declarations):
        matches = [
            index
            for index, record in enumerate(source_drawings)
            if drawing_records_equal(record, declaration)
        ]
        if len(matches) != 1:
            raise CompoundComponentError(
                "SOURCE_DRAWING_SIGNATURE_MATCH_COUNT",
                "A declared source drawing signature is missing or ambiguous",
                {
                    "component_id": component_id,
                    "declaration_index": declaration_index,
                    "match_indices": matches,
                },
            )
        drawing_results.append(
            {"declaration_index": declaration_index, "drawing_index": matches[0]}
        )

    path_results: list[dict[str, Any]] = []
    entries_by_stream: dict[int, list[dict[str, Any]]] = {}
    for entry in _path_entries(member):
        entries_by_stream.setdefault(int(entry["content_stream_xref"]), []).append(entry)
    for xref, entries in entries_by_stream.items():
        if xref not in declared_streams:
            raise CompoundComponentError(
                "CONTENT_PATH_STREAM_NOT_BOUND",
                "A path signature refers to a stream not bound by source evidence",
                {"component_id": component_id, "xref": xref},
            )
        records = (
            list(content_path_records_cache[xref])
            if content_path_records_cache is not None
            and xref in content_path_records_cache
            else parse_content_paths(doc.xref_stream(xref), stream_xref=xref)
        )
        matches = match_declared_path_signatures(records, entries)
        path_results.append(
            {
                "xref": xref,
                "declared_count": len(entries),
                "matched_path_indices": matches,
            }
        )
    text_results: list[dict[str, Any]] = []
    for declaration_index, declaration in enumerate(evidence.get("text_spans") or []):
        count = text_span_match_count(page, declaration)
        if count != 1:
            raise CompoundComponentError(
                "SOURCE_TEXT_SPAN_MATCH_COUNT",
                "A declared source text span is missing or ambiguous",
                {
                    "component_id": component_id,
                    "declaration_index": declaration_index,
                    "match_count": count,
                },
            )
        text_results.append(
            {"declaration_index": declaration_index, "match_count": count}
        )
    return {
        "component_id": component_id,
        "source_page": int(member.get("source_page", 0)),
        "page_object_xref": actual_page_xref,
        "object_xrefs": object_results,
        "content_streams": stream_results,
        "drawing_signatures": drawing_results,
        "content_path_signatures": path_results,
        "text_spans": text_results,
        "status": "VERIFIED",
    }


def apply_vector_path_replacements(
    output_doc: Any,
    output_page: Any,
    source_doc: Any,
    source_page_number: int,
    members: Sequence[Mapping[str, Any]],
) -> tuple[Any, list[dict[str, Any]]]:
    """Verify source bindings, then remove exact outlined-text paths from the copy."""
    if not members:
        return output_page, []
    source_page = source_doc[source_page_number - 1]
    source_evidence_by_member: dict[str, dict[str, Any]] = {}
    declarations_by_member: dict[str, list[dict[str, Any]]] = {}
    for member in members:
        component_id = str(member.get("component_id", ""))
        source_evidence_by_member[component_id] = verify_member_source_evidence(
            source_doc, source_page, member
        )
        declarations = _path_entries(member)
        if not declarations:
            raise CompoundComponentError(
                "VECTOR_TEXT_CONTENT_SIGNATURES_MISSING",
                "Outlined-text member has no selected content-path signatures",
                component_id,
            )
        declarations_by_member[component_id] = declarations

    output_streams = {
        int(xref): output_doc.xref_stream(int(xref)) for xref in output_page.get_contents()
    }
    output_records = {
        xref: parse_content_paths(data, stream_xref=xref)
        for xref, data in output_streams.items()
    }
    selected_by_output_stream: dict[int, list[dict[str, Any]]] = {}
    matched_output_path_keys: set[tuple[int, int]] = set()
    member_matches: dict[str, list[dict[str, Any]]] = {}
    for component_id, declarations in declarations_by_member.items():
        member_matches[component_id] = []
        for entry in declarations:
            signature = _declared_signature(entry)
            matches: list[tuple[int, int]] = []
            for xref, records in output_records.items():
                matches.extend(
                    (xref, index)
                    for index, record in enumerate(records)
                    if content_path_signatures_equal(record["signature"], signature)
                )
            if not matches:
                raise CompoundComponentError(
                    "COPIED_CONTENT_PATH_NOT_FOUND",
                    "A source-bound outlined-text path is absent from the copied page",
                    {
                        "component_id": component_id,
                        "signature_sha256": signature_sha256(signature),
                    },
                )
            if len(matches) != 1:
                raise CompoundComponentError(
                    "COPIED_CONTENT_PATH_DUPLICATE_MATCH",
                    "A source-bound outlined-text path is ambiguous in the copied page",
                    {
                        "component_id": component_id,
                        "signature_sha256": signature_sha256(signature),
                        "matches": matches,
                    },
                )
            match = matches[0]
            if match in matched_output_path_keys:
                raise CompoundComponentError(
                    "COPIED_CONTENT_PATH_REUSED",
                    "Two members selected the same copied path",
                    {"component_id": component_id, "match": match},
                )
            matched_output_path_keys.add(match)
            output_xref, path_index = match
            selected_by_output_stream.setdefault(output_xref, []).append(entry)
            member_matches[component_id].append(
                {
                    "output_stream_xref": output_xref,
                    "output_path_index": path_index,
                    "signature_sha256": signature_sha256(signature),
                }
            )

    stream_evidence: dict[int, dict[str, Any]] = {}
    for xref, declarations in selected_by_output_stream.items():
        updated, evidence = remove_selected_paths_from_stream(
            output_streams[xref], declarations, stream_xref=xref
        )
        output_doc.update_stream(xref, updated, compress=1)
        stream_evidence[xref] = evidence
    output_page = output_doc.reload_page(output_page)

    post_records = [
        record
        for xref in output_page.get_contents()
        for record in parse_content_paths(
            output_doc.xref_stream(int(xref)), stream_xref=int(xref)
        )
    ]
    results: list[dict[str, Any]] = []
    for member in members:
        component_id = str(member.get("component_id", ""))
        residue: list[dict[str, Any]] = []
        for entry in declarations_by_member[component_id]:
            signature = _declared_signature(entry)
            count = sum(
                content_path_signatures_equal(record["signature"], signature)
                for record in post_records
            )
            if count:
                residue.append(
                    {
                        "signature_sha256": signature_sha256(signature),
                        "count": count,
                    }
                )
        if residue:
            raise CompoundComponentError(
                "VECTOR_TEXT_PATH_RESIDUE",
                "Outlined-text paths remain after copied-page removal",
                {"component_id": component_id, "residue": residue},
            )
        touched_xrefs = sorted(
            {item["output_stream_xref"] for item in member_matches[component_id]}
        )
        results.append(
            {
                "source_page": source_page_number,
                "candidate_page": output_page.number + 1,
                "component_id": component_id,
                "role": member.get("role"),
                "policy": member.get("policy"),
                "source_evidence": source_evidence_by_member[component_id],
                "copied_path_matches": member_matches[component_id],
                "stream_rewrites": [stream_evidence[xref] for xref in touched_xrefs],
                "selected_path_count": len(member_matches[component_id]),
                "residue_count": 0,
                "status": "APPLIED_VERIFIED",
            }
        )
    return output_page, results


def apply_vector_rule_adjustments(
    output_doc: Any,
    output_page: Any,
    source_doc: Any,
    source_page_number: int,
    members: Sequence[Mapping[str, Any]],
) -> tuple[Any, list[dict[str, Any]]]:
    """Move exact source-bound rule paths and prove source absence plus target presence."""
    if not members:
        return output_page, []
    source_page = source_doc[source_page_number - 1]
    source_checks: dict[str, dict[str, Any]] = {}
    declarations: dict[str, dict[str, Any]] = {}
    target_signatures: dict[str, dict[str, Any]] = {}
    for member in members:
        component_id = str(member.get("component_id", ""))
        if member.get("role") != "vector_rule" or member.get("policy") != "adjust_vector_rule":
            raise CompoundComponentError(
                "VECTOR_RULE_ADJUSTMENT_ROLE",
                "Only adjust_vector_rule members can enter the vector-rule rewrite stage",
                component_id,
            )
        source_checks[component_id] = verify_member_source_evidence(
            source_doc, source_page, member
        )
        entries = _path_entries(member)
        if len(entries) != 1:
            raise CompoundComponentError(
                "VECTOR_RULE_SIGNATURE_COUNT",
                "An adjusted vector rule must bind exactly one content-path signature",
                {"component_id": component_id, "count": len(entries)},
            )
        declarations[component_id] = entries[0]
        target_signatures[component_id] = translated_content_path_signature(
            _declared_signature(entries[0]), member.get("translation_delta_pt") or []
        )

    output_streams = {
        int(xref): output_doc.xref_stream(int(xref)) for xref in output_page.get_contents()
    }
    output_records = {
        xref: parse_content_paths(data, stream_xref=xref)
        for xref, data in output_streams.items()
    }
    selected_by_stream: dict[int, list[dict[str, Any]]] = {}
    match_by_component: dict[str, tuple[int, int]] = {}
    used_matches: set[tuple[int, int]] = set()
    for member in members:
        component_id = str(member["component_id"])
        source_signature = _declared_signature(declarations[component_id])
        matches = [
            (xref, index)
            for xref, records in output_records.items()
            for index, record in enumerate(records)
            if content_path_signatures_equal(record["signature"], source_signature)
        ]
        if len(matches) != 1:
            raise CompoundComponentError(
                "COPIED_VECTOR_RULE_MATCH_COUNT",
                "The copied page must contain exactly one source-bound rule path",
                {"component_id": component_id, "matches": matches},
            )
        match = matches[0]
        if match in used_matches:
            raise CompoundComponentError(
                "COPIED_VECTOR_RULE_PATH_REUSED",
                "Two adjusted members selected the same copied rule path",
                {"component_id": component_id, "match": match},
            )
        used_matches.add(match)
        match_by_component[component_id] = match
        selected_by_stream.setdefault(match[0], []).append(
            {
                "component_id": component_id,
                "declaration": declarations[component_id],
                "translation_delta_pt": member["translation_delta_pt"],
            }
        )

    stream_evidence: dict[int, dict[str, Any]] = {}
    for xref, stream_adjustments in selected_by_stream.items():
        updated, evidence = translate_selected_paths_in_stream(
            output_streams[xref], stream_adjustments, stream_xref=xref
        )
        output_doc.update_stream(xref, updated, compress=1)
        stream_evidence[xref] = evidence
    output_page = output_doc.reload_page(output_page)
    candidate_records = [
        record
        for xref in output_page.get_contents()
        for record in parse_content_paths(
            output_doc.xref_stream(int(xref)), stream_xref=int(xref)
        )
    ]

    results: list[dict[str, Any]] = []
    for member in members:
        component_id = str(member["component_id"])
        source_signature = _declared_signature(declarations[component_id])
        target_signature = target_signatures[component_id]
        source_path_count = sum(
            content_path_signatures_equal(record["signature"], source_signature)
            for record in candidate_records
        )
        target_path_count = sum(
            content_path_signatures_equal(record["signature"], target_signature)
            for record in candidate_records
        )
        source_drawings = (
            (member.get("source_evidence") or {})
            .get("ordered_path_signatures", {})
            .get("drawing_signatures", [])
        )
        target_drawings = expected_candidate_drawing_signatures(member)
        source_drawing_count = sum(
            candidate_drawing_match_count(output_page, declaration)
            for declaration in source_drawings
        )
        target_drawing_counts = [
            candidate_drawing_match_count(output_page, declaration)
            for declaration in target_drawings
        ]
        if (
            source_path_count != 0
            or target_path_count != 1
            or source_drawing_count != 0
            or target_drawing_counts != [1]
        ):
            raise CompoundComponentError(
                "VECTOR_RULE_TRANSLATION_POSTCHECK",
                "The candidate rule path failed exact source-absence or target-presence verification",
                {
                    "component_id": component_id,
                    "source_path_count": source_path_count,
                    "target_path_count": target_path_count,
                    "source_drawing_count": source_drawing_count,
                    "target_drawing_counts": target_drawing_counts,
                },
            )
        output_xref, output_path_index = match_by_component[component_id]
        results.append(
            {
                "source_page": source_page_number,
                "candidate_page": output_page.number + 1,
                "component_id": component_id,
                "role": member.get("role"),
                "policy": member.get("policy"),
                "method": member.get("adjustment_method"),
                "source_bbox": _normalize_bbox(member["bbox"]),
                "target_bbox": _normalize_bbox(member["target_bbox"]),
                "translation_delta_pt": [
                    _finite_number(value, "translation_delta_pt")
                    for value in member["translation_delta_pt"]
                ],
                "source_evidence": source_checks[component_id],
                "copied_path_match": {
                    "output_stream_xref": output_xref,
                    "output_path_index": output_path_index,
                },
                "stream_rewrite": stream_evidence[output_xref],
                "source_path_count_after": source_path_count,
                "target_path_count_after": target_path_count,
                "source_drawing_count_after": source_drawing_count,
                "target_drawing_count_after": target_drawing_counts[0],
                "source_signature_sha256": signature_sha256(source_signature),
                "target_signature_sha256": signature_sha256(target_signature),
                "status": "APPLIED_VERIFIED",
            }
        )
    return output_page, results


def candidate_drawing_match_count(
    page: Any,
    declaration: Mapping[str, Any],
    *,
    records: Sequence[Mapping[str, Any]] | None = None,
) -> int:
    candidate_records = list(records) if records is not None else drawing_records(page)
    return sum(
        drawing_records_equal(record, declaration) for record in candidate_records
    )


def expected_candidate_drawing_signatures(member: Mapping[str, Any]) -> list[dict[str, Any]]:
    path_set = (member.get("source_evidence") or {}).get("ordered_path_signatures") or {}
    declarations = [copy.deepcopy(item) for item in path_set.get("drawing_signatures") or []]
    if member.get("policy") == "adjust_background":
        return [
            rewrite_single_rect_record(
                declaration, member.get("bbox"), member.get("target_bbox")
            )
            for declaration in declarations
        ]
    if member.get("policy") == "adjust_vector_rule":
        return [
            translate_drawing_record(declaration, member.get("translation_delta_pt") or [])
            for declaration in declarations
        ]
    return declarations


def expected_candidate_content_path_signatures(
    member: Mapping[str, Any]
) -> list[dict[str, Any]]:
    entries = _path_entries(member)
    if member.get("policy") != "adjust_vector_rule":
        return [_declared_signature(entry) for entry in entries]
    return [
        translated_content_path_signature(
            _declared_signature(entry), member.get("translation_delta_pt") or []
        )
        for entry in entries
    ]
