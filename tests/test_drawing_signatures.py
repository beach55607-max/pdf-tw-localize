#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any

import fitz


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import qa_preserved_visuals  # noqa: E402
import qa_rebuilt_pdf  # noqa: E402
from _drawing_signature import (  # noqa: E402
    COORDINATE_TOLERANCE_PT,
    DrawingSignatureError,
    canonical_drawing_record,
    drawing_record_sequences_equal,
    drawing_records,
    drawing_records_equal,
    filter_drawing_records,
    operator_counts,
    unmatched_drawing_records,
)
from _compound_components import candidate_drawing_match_count  # noqa: E402


class FakePage:
    def __init__(self, drawings: list[dict[str, Any]]) -> None:
        self._drawings = drawings

    def get_drawings(self) -> list[dict[str, Any]]:
        return self._drawings


def p(x: float, y: float) -> fitz.Point:
    return fitz.Point(x, y)


def cubic_drawing(
    control_1_y: float = 25.0,
    control_2_y: float = 75.0,
    *,
    items: list[tuple[Any, ...]] | None = None,
) -> dict[str, Any]:
    path_items = items or [
        ("l", p(0, 0), p(100, 0)),
        (
            "c",
            p(100, 0),
            p(100, control_1_y),
            p(100, control_2_y),
            p(100, 100),
        ),
        ("l", p(100, 100), p(0, 100)),
    ]
    return {
        "rect": fitz.Rect(0, 0, 100, 100),
        "type": "f",
        "fill": (0.82, 0.826, 0.832),
        "color": None,
        "width": None,
        "lineCap": None,
        "lineJoin": None,
        "dashes": None,
        "stroke_opacity": None,
        "fill_opacity": 1.0,
        "even_odd": True,
        "closePath": False,
        "layer": "",
        "items": path_items,
    }


class DrawingSignatureTest(unittest.TestCase):
    def test_identical_cubic_signatures_pass_in_both_qa_implementations(self) -> None:
        source = FakePage([cubic_drawing()])
        candidate = FakePage([cubic_drawing()])
        rebuilt_source = qa_rebuilt_pdf.drawing_signatures(source)
        rebuilt_candidate = qa_rebuilt_pdf.drawing_signatures(candidate)
        preserved_source = qa_preserved_visuals.drawing_signatures(
            source, [0, 0, 100, 100]
        )
        preserved_candidate = qa_preserved_visuals.drawing_signatures(
            candidate, [0, 0, 100, 100]
        )
        self.assertEqual(unmatched_drawing_records(rebuilt_source, rebuilt_candidate), [])
        self.assertTrue(
            drawing_record_sequences_equal(preserved_source, preserved_candidate)
        )

    def test_qa_rebuilt_blocks_same_bbox_fill_count_with_changed_control_points(self) -> None:
        source = qa_rebuilt_pdf.drawing_signatures(FakePage([cubic_drawing(25, 75)]))
        changed = qa_rebuilt_pdf.drawing_signatures(FakePage([cubic_drawing(10, 90)]))
        self.assertEqual(len(source), 1)
        self.assertEqual(len(changed), 1)
        self.assertEqual(source[0]["rect"], changed[0]["rect"])
        self.assertEqual(source[0]["fill"], changed[0]["fill"])
        self.assertEqual(source[0]["item_count"], changed[0]["item_count"])
        self.assertEqual(len(unmatched_drawing_records(source, changed)), 1)

    def test_qa_preserved_blocks_same_bbox_fill_count_with_changed_control_points(self) -> None:
        bbox = [0, 0, 100, 100]
        source = qa_preserved_visuals.drawing_signatures(
            FakePage([cubic_drawing(25, 75)]), bbox
        )
        changed = qa_preserved_visuals.drawing_signatures(
            FakePage([cubic_drawing(10, 90)]), bbox
        )
        self.assertEqual(source[0]["rect"], changed[0]["rect"])
        self.assertEqual(source[0]["fill"], changed[0]["fill"])
        self.assertEqual(source[0]["item_count"], changed[0]["item_count"])
        self.assertFalse(drawing_record_sequences_equal(source, changed))

    def test_operator_order_change_is_blocked(self) -> None:
        source_items = cubic_drawing()["items"]
        reordered_items = [source_items[1], source_items[0], source_items[2]]
        source = canonical_drawing_record(cubic_drawing(items=source_items))
        reordered = canonical_drawing_record(cubic_drawing(items=reordered_items))
        self.assertFalse(drawing_records_equal(source, reordered))

    def test_repeated_operator_count_change_is_blocked(self) -> None:
        source_items = cubic_drawing()["items"]
        duplicate_items = [*source_items, source_items[-1]]
        source = canonical_drawing_record(cubic_drawing(items=source_items))
        duplicated = canonical_drawing_record(cubic_drawing(items=duplicate_items))
        self.assertEqual(source["path_operators"][-1], duplicated["path_operators"][-1])
        self.assertNotEqual(source["item_count"], duplicated["item_count"])
        self.assertFalse(drawing_records_equal(source, duplicated))

    def test_unknown_operator_fails_closed_in_both_qa_implementations(self) -> None:
        drawing = cubic_drawing(items=[("mystery", p(0, 0), p(1, 1))])
        page = FakePage([drawing])
        with self.assertRaisesRegex(DrawingSignatureError, "Unsupported path operator"):
            qa_rebuilt_pdf.drawing_signatures(page)
        with self.assertRaisesRegex(DrawingSignatureError, "Unsupported path operator"):
            qa_preserved_visuals.drawing_signatures(page, [0, 0, 100, 100])

    def test_incomplete_cubic_operator_fails_closed_in_both_qa_implementations(self) -> None:
        incomplete = ("c", p(0, 0), p(10, 10), p(90, 90))
        page = FakePage([cubic_drawing(items=[incomplete])])
        with self.assertRaisesRegex(DrawingSignatureError, "requires 5 fields"):
            qa_rebuilt_pdf.drawing_signatures(page)
        with self.assertRaisesRegex(DrawingSignatureError, "requires 5 fields"):
            qa_preserved_visuals.drawing_signatures(page, [0, 0, 100, 100])

    def test_all_supported_operators_retain_type_order_and_all_coordinates(self) -> None:
        quad = fitz.Quad(p(0, 0), p(10, 0), p(0, 10), p(10, 10))
        items = [
            ("l", p(0, 0), p(10, 0)),
            ("re", fitz.Rect(0, 0, 10, 10), 1),
            ("qu", quad),
            ("c", p(0, 0), p(2, 1), p(8, 9), p(10, 10)),
        ]
        record = canonical_drawing_record(cubic_drawing(items=items))
        self.assertEqual([item["operator"] for item in record["path_operators"]], ["l", "re", "qu", "c"])
        cubic = record["path_operators"][-1]
        self.assertEqual(cubic["start"], [0.0, 0.0])
        self.assertEqual(cubic["control_1"], [2.0, 1.0])
        self.assertEqual(cubic["control_2"], [8.0, 9.0])
        self.assertEqual(cubic["end"], [10.0, 10.0])
        self.assertEqual(record["path_operators"][1]["orientation"], 1)
        self.assertEqual(operator_counts([record]), {"c": 1, "l": 1, "qu": 1, "re": 1})

    def test_coordinate_tolerance_is_explicit_and_strict(self) -> None:
        source = canonical_drawing_record(cubic_drawing(25, 75))
        harmless = canonical_drawing_record(
            cubic_drawing(25 + COORDINATE_TOLERANCE_PT / 2, 75)
        )
        material = canonical_drawing_record(
            cubic_drawing(25 + COORDINATE_TOLERANCE_PT * 2, 75)
        )
        self.assertTrue(drawing_records_equal(source, harmless))
        self.assertFalse(drawing_records_equal(source, material))

    def test_cached_filter_preserves_order_and_multiplicity(self) -> None:
        outside = cubic_drawing()
        outside["rect"] = fitz.Rect(200, 0, 300, 100)
        cached = drawing_records(FakePage([cubic_drawing(), outside, cubic_drawing()]))

        filtered = filter_drawing_records(cached, [0, 0, 100, 100])

        self.assertEqual(filtered, [cached[0], cached[2]])

    def test_cached_filter_fails_closed_on_malformed_record(self) -> None:
        with self.assertRaisesRegex(DrawingSignatureError, "rect field"):
            filter_drawing_records([{"bbox": [0, 0, 100, 100]}], [0, 0, 100, 100])

    def test_cached_and_uncached_drawing_match_counts_are_identical(self) -> None:
        page = FakePage([cubic_drawing(), cubic_drawing()])
        cached = drawing_records(page)
        declaration = cached[0]

        self.assertEqual(candidate_drawing_match_count(page, declaration), 2)
        self.assertEqual(
            candidate_drawing_match_count(page, declaration, records=cached),
            2,
        )

    def test_background_proof_is_required_only_for_declared_background_member(self) -> None:
        plain_text = {
            "render": {"mask_mode": "remove_text_only", "mask_padding_pt": 0}
        }
        compound_text = {
            "render": {"mask_mode": "remove_text_only", "mask_padding_pt": 0},
            "component_contract": {
                "members": [
                    {
                        "component_id": "fixture.background",
                        "role": "background",
                        "policy": "preserve",
                    }
                ]
            },
        }

        self.assertFalse(
            qa_preserved_visuals.declares_protected_background_member(plain_text)
        )
        self.assertTrue(
            qa_preserved_visuals.declares_protected_background_member(compound_text)
        )


if __name__ == "__main__":
    unittest.main()
