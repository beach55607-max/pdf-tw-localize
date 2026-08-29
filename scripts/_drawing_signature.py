#!/usr/bin/env python3
"""Canonical, fail-closed PyMuPDF drawing signatures.

The signature preserves drawing order, ordered path operators, repeated
operators, every operator coordinate, and the visual style fields exposed by
``Page.get_drawings()``. Sequence numbers are intentionally excluded because
they are display-list ordinals and can change when an authorized rebuild adds
unrelated text while leaving the drawing unchanged.
"""

from __future__ import annotations

import copy
import math
import numbers
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Any


COORDINATE_TOLERANCE_PT = 0.001
"""Maximum harmless coordinate delta: 0.001 pt (about 0.00035 mm)."""

STYLE_TOLERANCE = 0.000001
"""Tolerance for normalized color channels, opacity, and join values."""

NUMERIC_REPRESENTATION_DECIMALS = 6
"""Only removes binary-float representation noise before comparison."""

SUPPORTED_PATH_OPERATORS = ("l", "re", "qu", "c")


class DrawingSignatureError(ValueError):
    """A drawing cannot be signed without silently discarding information."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        drawing_index: int | None = None,
        operator_index: int | None = None,
        operator: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.drawing_index = drawing_index
        self.operator_index = operator_index
        self.operator = operator

    def with_drawing_index(self, drawing_index: int) -> "DrawingSignatureError":
        return DrawingSignatureError(
            self.code,
            self.message,
            drawing_index=drawing_index,
            operator_index=self.operator_index,
            operator=self.operator,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "drawing_index": self.drawing_index,
            "operator_index": self.operator_index,
            "operator": self.operator,
            "fail_closed": True,
        }


def signature_contract() -> dict[str, Any]:
    return {
        "schema": "pdf-tw-localize/ordered-drawing-signature/v1",
        "supported_path_operators": list(SUPPORTED_PATH_OPERATORS),
        "operator_order_preserved": True,
        "operator_multiplicity_preserved": True,
        "cubic_points": ["start", "control_1", "control_2", "end"],
        "coordinate_tolerance_pt": COORDINATE_TOLERANCE_PT,
        "coordinate_tolerance_reason": (
            "0.001 pt absorbs parser-level float representation noise while remaining "
            "about 240 times smaller than one 300 dpi pixel (0.24 pt)."
        ),
        "numeric_representation_decimals": NUMERIC_REPRESENTATION_DECIMALS,
        "unknown_or_malformed_operator": "BLOCKED_FAIL_CLOSED",
        "drawing_sequence_number_included": False,
        "drawing_sequence_number_exclusion_reason": (
            "PyMuPDF seqno is a display-list ordinal, not path geometry or visual style."
        ),
    }


def _finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise DrawingSignatureError(
            "DRAWING_FIELD_MALFORMED",
            f"{field} must be a finite real number; got {type(value).__name__}",
        )
    result = float(value)
    if not math.isfinite(result):
        raise DrawingSignatureError(
            "DRAWING_FIELD_MALFORMED",
            f"{field} must be finite; got {result!r}",
        )
    return round(result, NUMERIC_REPRESENTATION_DECIMALS)


def _point(value: Any, field: str) -> list[float]:
    if not hasattr(value, "x") or not hasattr(value, "y"):
        raise DrawingSignatureError(
            "DRAWING_OPERATOR_MALFORMED",
            f"{field} must be a PyMuPDF point-like value with x and y",
        )
    return [_finite_number(value.x, f"{field}.x"), _finite_number(value.y, f"{field}.y")]


def _fixed_sequence(value: Any, length: int, field: str) -> list[Any]:
    if isinstance(value, (str, bytes)):
        raise DrawingSignatureError(
            "DRAWING_FIELD_MALFORMED",
            f"{field} must be a sequence of length {length}",
        )
    try:
        result = list(value)
    except TypeError as exc:
        raise DrawingSignatureError(
            "DRAWING_FIELD_MALFORMED",
            f"{field} must be a sequence of length {length}",
        ) from exc
    if len(result) != length:
        raise DrawingSignatureError(
            "DRAWING_FIELD_MALFORMED",
            f"{field} must have length {length}; got {len(result)}",
        )
    return result


def _rect(value: Any, field: str) -> list[float]:
    return [
        _finite_number(item, f"{field}[{index}]")
        for index, item in enumerate(_fixed_sequence(value, 4, field))
    ]


def _quad(value: Any, field: str) -> list[list[float]]:
    return [
        _point(item, f"{field}[{index}]")
        for index, item in enumerate(_fixed_sequence(value, 4, field))
    ]


def _optional_numeric(value: Any, field: str) -> float | None:
    return None if value is None else _finite_number(value, field)


def _optional_color(value: Any, field: str) -> list[float] | None:
    if value is None:
        return None
    if isinstance(value, (str, bytes)):
        raise DrawingSignatureError(
            "DRAWING_FIELD_MALFORMED",
            f"{field} must be a numeric sequence or null",
        )
    try:
        values = list(value)
    except TypeError as exc:
        raise DrawingSignatureError(
            "DRAWING_FIELD_MALFORMED",
            f"{field} must be a numeric sequence or null",
        ) from exc
    return [
        _finite_number(item, f"{field}[{index}]") for index, item in enumerate(values)
    ]


def _optional_bool(value: Any, field: str) -> bool | None:
    if value is None or isinstance(value, bool):
        return value
    raise DrawingSignatureError(
        "DRAWING_FIELD_MALFORMED",
        f"{field} must be boolean or null",
    )


def _optional_text(value: Any, field: str) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise DrawingSignatureError(
        "DRAWING_FIELD_MALFORMED",
        f"{field} must be text or null",
    )


def _line_cap(value: Any) -> list[int] | None:
    if value is None:
        return None
    result = _fixed_sequence(value, 3, "lineCap")
    caps: list[int] = []
    for index, item in enumerate(result):
        if isinstance(item, bool) or not isinstance(item, numbers.Integral):
            raise DrawingSignatureError(
                "DRAWING_FIELD_MALFORMED",
                f"lineCap[{index}] must be an integer",
            )
        caps.append(int(item))
    return caps


def canonical_path_operator(item: Any, operator_index: int) -> dict[str, Any]:
    if isinstance(item, (str, bytes)) or not isinstance(item, Sequence) or not item:
        raise DrawingSignatureError(
            "DRAWING_OPERATOR_MALFORMED",
            "Path operator must be a non-empty sequence",
            operator_index=operator_index,
        )
    operation = item[0]
    if not isinstance(operation, str):
        raise DrawingSignatureError(
            "DRAWING_OPERATOR_MALFORMED",
            "Path operator type must be text",
            operator_index=operator_index,
        )
    if operation not in SUPPORTED_PATH_OPERATORS:
        raise DrawingSignatureError(
            "DRAWING_OPERATOR_UNKNOWN",
            f"Unsupported path operator {operation!r}; refusing to omit it",
            operator_index=operator_index,
            operator=operation,
        )

    expected_lengths = {"l": 3, "re": 3, "qu": 2, "c": 5}
    if len(item) != expected_lengths[operation]:
        raise DrawingSignatureError(
            "DRAWING_OPERATOR_MALFORMED",
            f"Operator {operation!r} requires {expected_lengths[operation]} fields; got {len(item)}",
            operator_index=operator_index,
            operator=operation,
        )

    try:
        if operation == "l":
            return {
                "operator": "l",
                "points": [
                    _point(item[1], f"items[{operator_index}].start"),
                    _point(item[2], f"items[{operator_index}].end"),
                ],
            }
        if operation == "c":
            return {
                "operator": "c",
                "start": _point(item[1], f"items[{operator_index}].start"),
                "control_1": _point(item[2], f"items[{operator_index}].control_1"),
                "control_2": _point(item[3], f"items[{operator_index}].control_2"),
                "end": _point(item[4], f"items[{operator_index}].end"),
            }
        if operation == "re":
            orientation = item[2]
            if isinstance(orientation, bool) or not isinstance(orientation, numbers.Integral):
                raise DrawingSignatureError(
                    "DRAWING_OPERATOR_MALFORMED",
                    "Rectangle orientation must be an integer",
                    operator_index=operator_index,
                    operator=operation,
                )
            return {
                "operator": "re",
                "rect": _rect(item[1], f"items[{operator_index}].rect"),
                "orientation": int(orientation),
            }
        return {
            "operator": "qu",
            "points": _quad(item[1], f"items[{operator_index}].quad"),
        }
    except DrawingSignatureError as exc:
        if exc.operator_index is not None:
            raise
        raise DrawingSignatureError(
            exc.code,
            exc.message,
            operator_index=operator_index,
            operator=operation,
        ) from exc


def canonical_drawing_record(
    drawing: Any, *, drawing_index: int | None = None
) -> dict[str, Any]:
    if not isinstance(drawing, Mapping):
        raise DrawingSignatureError(
            "DRAWING_RECORD_MALFORMED",
            "Each drawing must be a mapping",
            drawing_index=drawing_index,
        )
    if "rect" not in drawing or "items" not in drawing or "type" not in drawing:
        missing = sorted(key for key in ("rect", "items", "type") if key not in drawing)
        raise DrawingSignatureError(
            "DRAWING_RECORD_MALFORMED",
            f"Drawing is missing required fields: {missing}",
            drawing_index=drawing_index,
        )
    drawing_type = drawing["type"]
    if not isinstance(drawing_type, str) or not drawing_type:
        raise DrawingSignatureError(
            "DRAWING_FIELD_MALFORMED",
            "Drawing type must be non-empty text",
            drawing_index=drawing_index,
        )
    items = drawing["items"]
    if isinstance(items, (str, bytes)) or not isinstance(items, Sequence) or not items:
        raise DrawingSignatureError(
            "DRAWING_RECORD_MALFORMED",
            "Drawing items must be a non-empty sequence",
            drawing_index=drawing_index,
        )
    try:
        operators = [
            canonical_path_operator(item, index) for index, item in enumerate(items)
        ]
        return {
            "rect": _rect(drawing["rect"], "rect"),
            "type": drawing_type,
            "fill": _optional_color(drawing.get("fill"), "fill"),
            "color": _optional_color(drawing.get("color"), "color"),
            "width": _optional_numeric(drawing.get("width"), "width"),
            "line_cap": _line_cap(drawing.get("lineCap")),
            "line_join": _optional_numeric(drawing.get("lineJoin"), "lineJoin"),
            "dashes": _optional_text(drawing.get("dashes"), "dashes"),
            "stroke_opacity": _optional_numeric(
                drawing.get("stroke_opacity"), "stroke_opacity"
            ),
            "fill_opacity": _optional_numeric(drawing.get("fill_opacity"), "fill_opacity"),
            "even_odd": _optional_bool(drawing.get("even_odd"), "even_odd"),
            "close_path": _optional_bool(drawing.get("closePath"), "closePath"),
            "layer": _optional_text(drawing.get("layer"), "layer"),
            "item_count": len(operators),
            "path_operators": operators,
        }
    except DrawingSignatureError as exc:
        if exc.drawing_index is not None or drawing_index is None:
            raise
        raise exc.with_drawing_index(drawing_index) from exc


def _intersection_area(first: Sequence[float], second: Sequence[float]) -> float:
    width = max(0.0, min(first[2], second[2]) - max(first[0], second[0]))
    height = max(0.0, min(first[3], second[3]) - max(first[1], second[1]))
    return width * height


def drawing_records(
    page: Any,
    bbox: Iterable[float] | None = None,
    *,
    minimum_intersection_area: float = 0.01,
) -> list[dict[str, Any]]:
    raw_drawings = page.get_drawings()
    if not isinstance(raw_drawings, Sequence):
        raise DrawingSignatureError(
            "DRAWING_COLLECTION_MALFORMED",
            "Page.get_drawings() must return a sequence",
        )
    normalized_bbox = _rect(bbox, "filter_bbox") if bbox is not None else None
    records: list[dict[str, Any]] = []
    for drawing_index, drawing in enumerate(raw_drawings):
        if not isinstance(drawing, Mapping) or "rect" not in drawing:
            raise DrawingSignatureError(
                "DRAWING_RECORD_MALFORMED",
                "Drawing is not a mapping with a rect field",
                drawing_index=drawing_index,
            )
        drawing_rect = _rect(drawing["rect"], f"drawings[{drawing_index}].rect")
        if (
            normalized_bbox is not None
            and _intersection_area(drawing_rect, normalized_bbox)
            <= minimum_intersection_area
        ):
            continue
        records.append(canonical_drawing_record(drawing, drawing_index=drawing_index))
    return records


def filter_drawing_records(
    records: Sequence[Mapping[str, Any]],
    bbox: Iterable[float],
    *,
    minimum_intersection_area: float = 0.01,
) -> list[dict[str, Any]]:
    """Filter already canonicalized page drawings without reparsing the page.

    This preserves original order and multiplicity. It is deliberately a pure
    selection step: every input record must already contain a valid canonical
    rect, and malformed cached evidence fails closed.
    """

    normalized_bbox = _rect(bbox, "filter_bbox")
    result: list[dict[str, Any]] = []
    for drawing_index, record in enumerate(records):
        if not isinstance(record, Mapping) or "rect" not in record:
            raise DrawingSignatureError(
                "DRAWING_RECORD_MALFORMED",
                "Cached drawing record is not a mapping with a rect field",
                drawing_index=drawing_index,
            )
        drawing_rect = _rect(record["rect"], f"records[{drawing_index}].rect")
        if (
            _intersection_area(drawing_rect, normalized_bbox)
            > minimum_intersection_area
        ):
            result.append(dict(record))
    return result


def _numbers_equal(left: Any, right: Any, tolerance: float) -> bool:
    if left is None or right is None:
        return left is right
    return abs(float(left) - float(right)) <= tolerance


def _numeric_sequences_equal(
    left: Any, right: Any, tolerance: float
) -> bool:
    if left is None or right is None:
        return left is right
    return len(left) == len(right) and all(
        _numbers_equal(a, b, tolerance) for a, b in zip(left, right, strict=True)
    )


def _point_sequences_equal(left: Any, right: Any) -> bool:
    return len(left) == len(right) and all(
        _numeric_sequences_equal(a, b, COORDINATE_TOLERANCE_PT)
        for a, b in zip(left, right, strict=True)
    )


def path_operators_equal(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    operation = left.get("operator")
    if operation != right.get("operator") or operation not in SUPPORTED_PATH_OPERATORS:
        return False
    if operation == "re":
        return (
            left.get("orientation") == right.get("orientation")
            and _numeric_sequences_equal(
                left.get("rect"), right.get("rect"), COORDINATE_TOLERANCE_PT
            )
        )
    if operation in {"l", "qu"}:
        return _point_sequences_equal(left.get("points") or [], right.get("points") or [])
    return all(
        _numeric_sequences_equal(
            left.get(field), right.get(field), COORDINATE_TOLERANCE_PT
        )
        for field in ("start", "control_1", "control_2", "end")
    )


def drawing_records_equal(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    exact_fields = (
        "type",
        "line_cap",
        "dashes",
        "even_odd",
        "close_path",
        "layer",
        "item_count",
    )
    if any(left.get(field) != right.get(field) for field in exact_fields):
        return False
    if not _numeric_sequences_equal(
        left.get("rect"), right.get("rect"), COORDINATE_TOLERANCE_PT
    ):
        return False
    for field in ("fill", "color"):
        if not _numeric_sequences_equal(left.get(field), right.get(field), STYLE_TOLERANCE):
            return False
    if not _numbers_equal(left.get("width"), right.get("width"), COORDINATE_TOLERANCE_PT):
        return False
    for field in ("line_join", "stroke_opacity", "fill_opacity"):
        if not _numbers_equal(left.get(field), right.get(field), STYLE_TOLERANCE):
            return False
    left_operators = left.get("path_operators") or []
    right_operators = right.get("path_operators") or []
    return len(left_operators) == len(right_operators) and all(
        path_operators_equal(a, b)
        for a, b in zip(left_operators, right_operators, strict=True)
    )


def drawing_record_sequences_equal(
    left: Sequence[Mapping[str, Any]], right: Sequence[Mapping[str, Any]]
) -> bool:
    """Compare drawing order as well as every complete drawing signature."""
    return len(left) == len(right) and all(
        drawing_records_equal(a, b) for a, b in zip(left, right, strict=True)
    )


def unmatched_drawing_records(
    source: Sequence[Mapping[str, Any]], candidate: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Return a multiplicity-preserving multiset difference under strict tolerance."""
    candidate_match: list[int | None] = [None] * len(candidate)

    def assign(source_index: int, seen: set[int]) -> bool:
        for candidate_index, candidate_record in enumerate(candidate):
            if candidate_index in seen or not drawing_records_equal(
                source[source_index], candidate_record
            ):
                continue
            seen.add(candidate_index)
            prior_source = candidate_match[candidate_index]
            if prior_source is None or assign(prior_source, seen):
                candidate_match[candidate_index] = source_index
                return True
        return False

    matched_source: set[int] = set()
    for source_index in range(len(source)):
        if assign(source_index, set()):
            matched_source.add(source_index)
    return [copy.deepcopy(source[index]) for index in range(len(source)) if index not in matched_source]


def operator_counts(records: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for record in records:
        for item in record.get("path_operators") or []:
            counts[str(item.get("operator"))] += 1
    return dict(sorted(counts.items()))


def same_rect(first: Any, second: Any) -> bool:
    try:
        left = _rect(first, "first_rect")
        right = _rect(second, "second_rect")
    except DrawingSignatureError:
        return False
    return _numeric_sequences_equal(left, right, COORDINATE_TOLERANCE_PT)


def record_is_single_rect(
    record: Mapping[str, Any], bbox: Any, expected_fill: Any
) -> bool:
    operators = record.get("path_operators") or []
    return (
        record.get("item_count") == 1
        and len(operators) == 1
        and operators[0].get("operator") == "re"
        and same_rect(record.get("rect"), bbox)
        and same_rect(operators[0].get("rect"), bbox)
        and _numeric_sequences_equal(
            record.get("fill"),
            _optional_color(expected_fill, "expected_fill"),
            STYLE_TOLERANCE,
        )
    )


def rewrite_single_rect_record(
    record: Mapping[str, Any], source_bbox: Any, target_bbox: Any
) -> dict[str, Any]:
    if not record_is_single_rect(record, source_bbox, record.get("fill")):
        raise DrawingSignatureError(
            "DRAWING_RECT_REWRITE_MALFORMED",
            "Declared background adjustment must bind one single-rectangle drawing",
        )
    result = copy.deepcopy(dict(record))
    normalized_target = _rect(target_bbox, "target_bbox")
    result["rect"] = normalized_target
    result["path_operators"][0]["rect"] = list(normalized_target)
    return result


def translate_drawing_record(
    record: Mapping[str, Any], delta_pt: Sequence[float]
) -> dict[str, Any]:
    """Translate every coordinate in one canonical drawing without changing shape/style."""
    delta = _fixed_sequence(delta_pt, 2, "delta_pt")
    dx = _finite_number(delta[0], "delta_pt[0]")
    dy = _finite_number(delta[1], "delta_pt[1]")
    result = copy.deepcopy(dict(record))

    def translated_point(point: Sequence[float]) -> list[float]:
        values = _fixed_sequence(point, 2, "drawing_point")
        return [
            round(_finite_number(values[0], "drawing_point.x") + dx, NUMERIC_REPRESENTATION_DECIMALS),
            round(_finite_number(values[1], "drawing_point.y") + dy, NUMERIC_REPRESENTATION_DECIMALS),
        ]

    def translated_rect(rect: Sequence[float]) -> list[float]:
        values = _rect(rect, "drawing_rect")
        return [
            round(values[0] + dx, NUMERIC_REPRESENTATION_DECIMALS),
            round(values[1] + dy, NUMERIC_REPRESENTATION_DECIMALS),
            round(values[2] + dx, NUMERIC_REPRESENTATION_DECIMALS),
            round(values[3] + dy, NUMERIC_REPRESENTATION_DECIMALS),
        ]

    result["rect"] = translated_rect(result["rect"])
    for operator in result.get("path_operators") or []:
        kind = operator.get("operator")
        if kind in {"l", "qu"}:
            operator["points"] = [translated_point(point) for point in operator["points"]]
        elif kind == "c":
            for field in ("start", "control_1", "control_2", "end"):
                operator[field] = translated_point(operator[field])
        elif kind == "re":
            operator["rect"] = translated_rect(operator["rect"])
        else:
            raise DrawingSignatureError(
                "DRAWING_OPERATOR_UNKNOWN",
                f"Unsupported path operator {kind!r}; refusing translated expectation",
                operator=kind,
            )
    return result
