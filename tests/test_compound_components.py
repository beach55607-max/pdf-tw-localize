from __future__ import annotations

import copy
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

import fitz


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from _compound_components import (  # noqa: E402
    COMPOSITED_VISIBLE_LAYOUT_SCHEMA,
    COMPOUND_COMPONENT_SCHEMA,
    ENGLISH_ALLOWLIST_SCHEMA,
    ORDERED_PATH_SET_SCHEMA,
    TRANSLATION_DEPENDENT_GEOMETRY_SCHEMA,
    CompoundComponentError,
    candidate_member_bboxes,
    content_path_construction_signatures_equal,
    content_path_reserialization_equivalent,
    content_path_signatures_equal,
    evaluate_member_relations,
    evaluate_composited_visible_layouts,
    evaluate_repeated_component_layouts,
    evaluate_translation_dependent_geometry,
    parse_content_paths,
    remove_selected_paths_from_stream,
    restore_reserialized_path_from_source,
    restore_reserialized_paths_from_source_batch,
    resolve_translation_dependent_bbox,
    translate_selected_paths_in_stream,
    translated_content_path_signature,
    validate_compound_component_contract,
    validate_english_allowlist,
    verify_member_source_evidence,
)


class CompoundComponentRoutingTest(unittest.TestCase):
    @staticmethod
    def _synthetic_path_entry(data: bytes, xref: int = 9) -> dict:
        record = parse_content_paths(data, stream_xref=xref)[0]
        return {
            "content_stream_xref": xref,
            "signature": record["signature"],
            "signature_sha256": record["signature_sha256"],
        }

    def _member(
        self,
        component_id: str,
        role: str,
        policy: str,
        bbox: list[float],
        *,
        translatability: str = "not_translatable",
        content_entries: list[dict] | None = None,
        relations: list[dict] | None = None,
    ) -> dict:
        applicable = role not in {"translatable_live_text"}
        return {
            "component_id": component_id,
            "source_page": 1,
            "bbox": bbox,
            "role": role,
            "translatability": translatability,
            "policy": policy,
            "source_evidence": {
                "page_object_xref": 5,
                "object_xrefs": [5, 9],
                "content_streams": [{"xref": 9, "sha256": "A" * 64}],
                "ordered_path_signatures": {
                    "schema": ORDERED_PATH_SET_SCHEMA,
                    "status": (
                        "APPLICABLE" if applicable else "NOT_APPLICABLE_LIVE_TEXT"
                    ),
                    "drawing_signatures": (
                        [{"synthetic_component": component_id}] if applicable else []
                    ),
                    "content_path_signatures": content_entries or [],
                },
                **(
                    {
                        "text_spans": [
                            {
                                "ref": f"fixture.{component_id}",
                                "text": "Sample",
                                "bbox": bbox,
                                "font": "FixtureSans",
                                "size_pt": 8.0,
                            }
                        ]
                    }
                    if role == "translatable_live_text"
                    else {}
                ),
            },
            "relations": relations or [],
        }

    def _all_role_segment(self) -> dict:
        path_data = b"q 0 0 0 rg 0 0 m 4 0 l 4 5 l h f Q"
        vector_entry = self._synthetic_path_entry(path_data)
        members = [
            self._member(
                "member-live",
                "translatable_live_text",
                "replace_live_text",
                [1, 1, 9, 4],
                translatability="required",
            ),
            self._member(
                "member-outline",
                "translatable_vector_outlined_text",
                "replace_vector_outlined_text",
                [10, 1, 18, 6],
                translatability="required",
                content_entries=[vector_entry],
                relations=[
                    {
                        "type": "avoid",
                        "target_member_id": "member-icon",
                        "minimum_clearance_pt": 1.0,
                    }
                ],
            ),
            self._member("member-icon", "icon", "preserve", [20, 1, 24, 5]),
            self._member("member-frame", "frame", "preserve", [0, 0, 26, 8]),
            self._member("member-rule", "vector_rule", "preserve", [0, 9, 26, 10]),
            self._member("member-bg", "background", "preserve", [0, 0, 26, 12]),
            self._member(
                "member-neighbor", "neighbor_container", "preserve", [0, 14, 26, 22]
            ),
        ]
        return {
            "page": 1,
            "segment_id": "synthetic-vector-segment",
            "bbox": [10, 1, 18, 6],
            "extraction_method": "visual_annotation",
            "render": {
                "action": "replace_vector_outlined_text",
                "target_bbox": [10, 1, 18, 6],
                "vector_member_id": "member-outline",
            },
            "component_contract": {
                "schema": COMPOUND_COMPONENT_SCHEMA,
                "group_id": "synthetic-compound-group",
                "segment_role": "translatable_vector_outlined_text",
                "mask_policy": "none",
                "members": members,
            },
        }

    def test_all_canonical_members_are_independently_declared(self) -> None:
        segment = self._all_role_segment()
        roles = {
            member["role"] for member in segment["component_contract"]["members"]
        }
        self.assertEqual(
            roles,
            {
                "translatable_live_text",
                "translatable_vector_outlined_text",
                "icon",
                "frame",
                "vector_rule",
                "background",
                "neighbor_container",
            },
        )
        self.assertEqual(validate_compound_component_contract(segment, [0, 0, 30, 30]), [])

    def test_live_text_requires_text_only_zero_padding_route(self) -> None:
        segment = self._all_role_segment()
        contract = segment["component_contract"]
        contract["segment_role"] = "translatable_live_text"
        contract["mask_policy"] = "source_text_spans_only"
        segment["segment_id"] = "synthetic-live-segment"
        segment["bbox"] = [1, 1, 9, 4]
        segment["extraction_method"] = "source_spans"
        segment["render"] = {
            "action": "replace",
            "target_bbox": [1, 1, 9, 4],
            "mask_bbox": [1, 1, 9, 4],
            "mask_mode": "remove_text_only",
            "mask_padding_pt": 0.0,
        }
        self.assertEqual(validate_compound_component_contract(segment, [0, 0, 30, 30]), [])
        segment["render"]["mask_mode"] = "fill"
        codes = {
            item["code"]
            for item in validate_compound_component_contract(segment, [0, 0, 30, 30])
        }
        self.assertIn("LIVE_TEXT_MASK_POLICY", codes)

    def test_exact_unique_path_is_removed_without_touching_neighbor_path(self) -> None:
        data = (
            b"q 0 0 0 rg 0 0 m 4 0 l 4 5 l h f Q "
            b"q 1 0 0 RG 10 10 m 20 10 l S Q"
        )
        records = parse_content_paths(data, stream_xref=12)
        declaration = {
            "content_stream_xref": 12,
            "signature": records[0]["signature"],
        }
        updated, evidence = remove_selected_paths_from_stream(
            data, [declaration], stream_xref=12
        )
        remaining = parse_content_paths(updated, stream_xref=12)
        self.assertEqual(evidence["selected_path_count"], 1)
        self.assertEqual(evidence["residue_count"], 0)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["signature"], records[1]["signature"])

    def test_duplicate_signature_match_is_blocked(self) -> None:
        one = b"0 0 m 3 0 l 3 3 l h f "
        data = one + one
        declaration = {"signature": parse_content_paths(one)[0]["signature"]}
        with self.assertRaisesRegex(CompoundComponentError, "matched more than one"):
            remove_selected_paths_from_stream(data, [declaration])

    def test_control_point_operator_order_and_count_changes_are_blocked(self) -> None:
        source = b"0 0 m 1 2 l 3 4 5 6 7 8 c 9 10 l S"
        declaration = {"signature": parse_content_paths(source)[0]["signature"]}
        changed_streams = {
            "control_point": b"0 0 m 1 2 l 3.01 4 5 6 7 8 c 9 10 l S",
            "operator_order": b"0 0 m 3 4 5 6 7 8 c 1 2 l 9 10 l S",
            "operator_count": b"0 0 m 3 4 5 6 7 8 c 9 10 l S",
        }
        for label, changed in changed_streams.items():
            with self.subTest(label=label):
                with self.assertRaisesRegex(CompoundComponentError, "did not match"):
                    remove_selected_paths_from_stream(changed, [declaration])

    def test_interwoven_unknown_operator_is_blocked(self) -> None:
        with self.assertRaisesRegex(CompoundComponentError, "interwoven"):
            parse_content_paths(b"0 0 m ZZ 2 2 l S")

    def test_interwoven_protected_member_assignment_is_blocked(self) -> None:
        segment = self._all_role_segment()
        members = segment["component_contract"]["members"]
        outline_entry = copy.deepcopy(
            next(item for item in members if item["component_id"] == "member-outline")
            ["source_evidence"]["ordered_path_signatures"]["content_path_signatures"][0]
        )
        frame = next(item for item in members if item["component_id"] == "member-frame")
        frame["source_evidence"]["ordered_path_signatures"][
            "content_path_signatures"
        ] = [outline_entry]
        codes = {
            item["code"]
            for item in validate_compound_component_contract(segment, [0, 0, 30, 30])
        }
        self.assertIn("PATH_MEMBER_INTERWOVEN", codes)

    def test_missing_required_member_field_is_blocked(self) -> None:
        segment = self._all_role_segment()
        del segment["component_contract"]["members"][1]["source_evidence"]
        codes = {
            item["code"]
            for item in validate_compound_component_contract(segment, [0, 0, 30, 30])
        }
        self.assertIn("COMPONENT_MEMBER_FIELDS", codes)
        self.assertIn("COMPONENT_SOURCE_EVIDENCE", codes)

    def test_source_stream_sha_mismatch_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.pdf"
            doc = fitz.open()
            page = doc.new_page(width=100, height=100)
            page.draw_rect(fitz.Rect(10, 10, 30, 30), color=(0, 0, 0))
            doc.save(path)
            doc.close()
            doc = fitz.open(path)
            page = doc[0]
            stream_xref = int(page.get_contents()[0])
            member = self._member("fixture-frame", "frame", "preserve", [10, 10, 30, 30])
            member["source_evidence"] = {
                "page_object_xref": int(doc.page_xref(0)),
                "object_xrefs": [int(doc.page_xref(0)), stream_xref],
                "content_streams": [{"xref": stream_xref, "sha256": "0" * 64}],
                "ordered_path_signatures": {
                    "schema": ORDERED_PATH_SET_SCHEMA,
                    "status": "APPLICABLE",
                    "drawing_signatures": [],
                    "content_path_signatures": [],
                },
            }
            with self.assertRaisesRegex(CompoundComponentError, "content stream changed"):
                verify_member_source_evidence(doc, page, member)
            doc.close()

    def test_intersection_and_minimum_clearance_both_gate_relations(self) -> None:
        contract = {
            "members": [
                {
                    "component_id": "replacement",
                    "relations": [
                        {
                            "type": "avoid",
                            "target_member_id": "protected",
                            "minimum_clearance_pt": 2.0,
                        }
                    ],
                },
                {"component_id": "protected", "relations": []},
            ]
        }
        _, overlap_issues = evaluate_member_relations(
            contract, {"replacement": [0, 0, 5, 5], "protected": [4, 0, 9, 5]}
        )
        low_results, low_issues = evaluate_member_relations(
            contract, {"replacement": [0, 0, 5, 5], "protected": [6, 0, 9, 5]}
        )
        pass_results, pass_issues = evaluate_member_relations(
            contract, {"replacement": [0, 0, 5, 5], "protected": [7, 0, 9, 5]}
        )
        self.assertTrue(overlap_issues)
        self.assertEqual(low_results[0]["intersection_area_pt2"], 0.0)
        self.assertEqual(low_results[0]["clearance_pt"], 1.0)
        self.assertTrue(low_issues)
        self.assertEqual(pass_results[0]["clearance_pt"], 2.0)
        self.assertFalse(pass_issues)

    def test_vertical_center_alignment_relation_fails_and_passes_by_manifest_tolerance(self) -> None:
        contract = {
            "members": [
                {
                    "component_id": "localized_heading",
                    "relations": [
                        {
                            "type": "align_center_y",
                            "target_member_id": "protected_rule",
                            "maximum_delta_pt": 0.25,
                        }
                    ],
                },
                {"component_id": "protected_rule", "relations": []},
            ]
        }
        failed, failed_issues = evaluate_member_relations(
            contract,
            {
                "localized_heading": [10.0, 10.0, 30.0, 20.0],
                "protected_rule": [31.0, 16.0, 60.0, 16.0],
            },
        )
        passed, passed_issues = evaluate_member_relations(
            contract,
            {
                "localized_heading": [10.0, 11.0, 30.0, 21.0],
                "protected_rule": [31.0, 15.8, 60.0, 15.8],
            },
        )
        self.assertEqual(failed[0]["status"], "FAIL")
        self.assertEqual(failed_issues[0]["code"], "COMPONENT_ALIGNMENT_FAILED")
        self.assertEqual(passed[0]["status"], "PASS")
        self.assertFalse(passed_issues)

    def test_exact_vector_rule_path_translation_changes_only_selected_path(self) -> None:
        data = (
            b"q 1 0 0 1 5 20 cm 0 0 m 30 0 l S Q "
            b"q 1 0 0 1 5 40 cm 0 0 m 30 0 l S Q"
        )
        records = parse_content_paths(data, stream_xref=17)
        declaration = {
            "content_stream_xref": 17,
            "signature": records[0]["signature"],
        }
        expected = translated_content_path_signature(records[0]["signature"], [0.0, 2.0])
        updated, evidence = translate_selected_paths_in_stream(
            data,
            [
                {
                    "component_id": "synthetic-rule-a",
                    "declaration": declaration,
                    "translation_delta_pt": [0.0, 2.0],
                }
            ],
            stream_xref=17,
        )
        after = parse_content_paths(updated, stream_xref=17)
        self.assertEqual(evidence["adjusted_path_count"], 1)
        self.assertEqual(after[0]["signature"], expected)
        self.assertEqual(after[1]["signature"], records[1]["signature"])
        self.assertEqual(after[0]["signature"]["path_operators"][0]["operands"], [0.0, -2.0])

    def test_vector_rule_translation_duplicate_and_unsupported_ctm_fail_closed(self) -> None:
        one = b"0 0 m 20 0 l S "
        declaration = {"signature": parse_content_paths(one)[0]["signature"]}
        with self.assertRaisesRegex(CompoundComponentError, "matched more than one"):
            translate_selected_paths_in_stream(
                one + one,
                [
                    {
                        "component_id": "synthetic-duplicate-rule",
                        "declaration": declaration,
                        "translation_delta_pt": [0.0, 1.0],
                    }
                ],
            )
        skewed = parse_content_paths(b"1 0.2 0 1 0 0 cm 0 0 m 20 0 l S")[0][
            "signature"
        ]
        with self.assertRaisesRegex(CompoundComponentError, "axis-aligned"):
            translated_content_path_signature(skewed, [0.0, 1.0])

    def test_vector_rule_adjustment_contract_requires_exact_translated_geometry(self) -> None:
        segment = self._all_role_segment()
        rule = next(
            member
            for member in segment["component_contract"]["members"]
            if member["component_id"] == "member-rule"
        )
        rule_entry = self._synthetic_path_entry(b"0 0 m 26 0 l S")
        rule.update(
            {
                "policy": "adjust_vector_rule",
                "target_bbox": [0.0, 10.0, 26.0, 11.0],
                "translation_delta_pt": [0.0, 1.0],
                "adjustment_method": "translate_exact_stroked_path",
            }
        )
        rule["source_evidence"]["ordered_path_signatures"] = {
            "schema": ORDERED_PATH_SET_SCHEMA,
            "status": "APPLICABLE",
            "drawing_signatures": [
                {
                    "rect": [0.0, 9.5, 26.0, 9.5],
                    "type": "s",
                    "fill": None,
                    "color": [0.0, 0.0, 0.0],
                    "width": 1.0,
                    "line_cap": [0, 0, 0],
                    "line_join": 0.0,
                    "dashes": "[] 0",
                    "stroke_opacity": 1.0,
                    "fill_opacity": None,
                    "even_odd": None,
                    "close_path": False,
                    "layer": "",
                    "item_count": 1,
                    "path_operators": [
                        {
                            "operator": "l",
                            "points": [[0.0, 9.5], [26.0, 9.5]],
                        }
                    ],
                }
            ],
            "content_path_signatures": [rule_entry],
        }
        self.assertEqual(validate_compound_component_contract(segment, [0, 0, 30, 30]), [])
        rule["target_bbox"] = [0.0, 10.5, 26.0, 11.5]
        codes = {
            item["code"]
            for item in validate_compound_component_contract(segment, [0, 0, 30, 30])
        }
        self.assertIn("VECTOR_RULE_TARGET_GEOMETRY", codes)

    def test_final_rule_construction_survives_graphics_state_reserialization(self) -> None:
        source = parse_content_paths(b"0 0 m 26 0 l S")[0]["signature"]
        target = translated_content_path_signature(source, [0.0, 1.0])
        reserialized = copy.deepcopy(target)
        reserialized["graphics_state"] = {"K": [0.0, 0.0, 0.0, 1.0], "w": [1.0]}
        self.assertFalse(content_path_signatures_equal(target, reserialized))
        self.assertTrue(
            content_path_construction_signatures_equal(target, reserialized)
        )
        changed_operator = copy.deepcopy(reserialized)
        changed_operator["path_operators"][1]["operands"][0] = 25.0
        self.assertFalse(
            content_path_construction_signatures_equal(target, changed_operator)
        )

    def test_optical_offset_relation_uses_signed_reference_and_tolerance(self) -> None:
        contract = {
            "members": [
                {
                    "component_id": "localized-label",
                    "relations": [
                        {
                            "type": "align_optical_offset_y",
                            "target_member_id": "adjusted-rule",
                            "expected_target_minus_member_center_pt": 2.4,
                            "maximum_delta_pt": 0.05,
                            "measurement_basis": "actual_candidate_text_span_bbox",
                        }
                    ],
                },
                {"component_id": "adjusted-rule", "relations": []},
            ]
        }
        passed, pass_issues = evaluate_member_relations(
            contract,
            {
                "localized-label": [0.0, 5.0, 20.0, 15.0],
                "adjusted-rule": [21.0, 12.4, 50.0, 12.4],
            },
        )
        failed, fail_issues = evaluate_member_relations(
            contract,
            {
                "localized-label": [0.0, 5.0, 20.0, 15.0],
                "adjusted-rule": [21.0, 11.0, 50.0, 11.0],
            },
        )
        self.assertEqual(passed[0]["target_minus_member_center_pt"], 2.4)
        self.assertFalse(pass_issues)
        self.assertEqual(failed[0]["status"], "FAIL")
        self.assertEqual(fail_issues[0]["code"], "COMPONENT_ALIGNMENT_FAILED")

    def test_candidate_member_bbox_comes_from_reopened_actual_text_span(self) -> None:
        doc = fitz.open()
        page = doc.new_page(width=120, height=80)
        page.insert_text((10, 30), "SyntheticLabel", fontsize=10)
        actual = next(
            span["bbox"]
            for block in page.get_text("dict")["blocks"]
            for line in block.get("lines", [])
            for span in line.get("spans", [])
            if span.get("text") == "SyntheticLabel"
        )
        contract = {
            "group_id": "synthetic-actual-bbox-group",
            "members": [
                {
                    "component_id": "synthetic-live-label",
                    "policy": "replace_live_text",
                    "bbox": [10, 20, 80, 32],
                }
            ],
        }
        reports = [
            {
                "action": "replace",
                "component_contract": {
                    "group_id": "synthetic-actual-bbox-group"
                },
                "target_bbox": [8, 18, 90, 34],
                "used_font_size_pt": 10.0,
                "rendered_lines": [
                    {"text": "SyntheticLabel", "bbox": [10, 20, 75, 31]}
                ],
            }
        ]
        bboxes = candidate_member_bboxes(contract, reports, candidate_page=page)
        self.assertEqual(
            [round(value, 6) for value in bboxes["synthetic-live-label"]],
            [round(float(value), 6) for value in actual],
        )
        doc.close()

    def test_repeated_component_layout_allows_declared_text_width_variation(self) -> None:
        contract = {
            "contract_id": "synthetic-repeated-heading",
            "normalization": "anchor_top_left",
            "maximum_delta_pt": 0.05,
            "compare": {
                "marker": ["x0", "y0", "width", "height"],
                "label": ["x0", "y0", "height"],
                "rule": ["x0", "y0", "width", "height"],
            },
            "instances": [
                {
                    "instance_id": "first",
                    "anchor_member_id": "first-marker",
                    "member_ids": {
                        "marker": "first-marker",
                        "label": "first-label",
                        "rule": "first-rule",
                    },
                },
                {
                    "instance_id": "second",
                    "anchor_member_id": "second-marker",
                    "member_ids": {
                        "marker": "second-marker",
                        "label": "second-label",
                        "rule": "second-rule",
                    },
                },
            ],
        }
        bboxes = {
            "first-marker": [10, 10, 12, 14],
            "first-label": [13, 9, 21, 15],
            "first-rule": [22, 12, 50, 13],
            "second-marker": [10, 40, 12, 44],
            "second-label": [13, 39, 29, 45],
            "second-rule": [22, 42, 50, 43],
        }
        checks, issues = evaluate_repeated_component_layouts([contract], bboxes)
        self.assertFalse(issues)
        self.assertTrue(checks)
        self.assertTrue(all(item["status"] == "PASS" for item in checks))
        label = next(item for item in checks if item["semantic_member"] == "label")
        self.assertNotIn("width", label["compared_metrics"])

    def test_repeated_component_layout_blocks_local_rule_mismatch(self) -> None:
        contract = {
            "contract_id": "synthetic-repeated-rule",
            "normalization": "anchor_top_left",
            "maximum_delta_pt": 0.05,
            "compare": {"rule": ["x0", "y0", "width", "height"]},
            "instances": [
                {
                    "instance_id": "first",
                    "anchor_member_id": "first-marker",
                    "member_ids": {"rule": "first-rule"},
                },
                {
                    "instance_id": "second",
                    "anchor_member_id": "second-marker",
                    "member_ids": {"rule": "second-rule"},
                },
            ],
        }
        bboxes = {
            "first-marker": [10, 10, 12, 14],
            "first-rule": [22, 12, 50, 13],
            "second-marker": [10, 40, 12, 44],
            "second-rule": [22, 42.3, 50, 43.3],
        }
        checks, issues = evaluate_repeated_component_layouts([contract], bboxes)
        self.assertEqual(checks[0]["status"], "FAIL")
        self.assertEqual(issues[0]["code"], "REPEATED_COMPONENT_LAYOUT_MISMATCH")

    def test_repeated_component_layout_missing_fields_fail_closed(self) -> None:
        checks, issues = evaluate_repeated_component_layouts(
            [
                {
                    "contract_id": "missing-normalization",
                    "maximum_delta_pt": 0.05,
                    "compare": {"rule": ["y0"]},
                    "instances": [{}, {}],
                }
            ],
            {},
        )
        self.assertFalse(checks)
        self.assertEqual(issues[0]["code"], "REPEATED_LAYOUT_CONTRACT_FIELDS")

    def test_translation_dependent_background_resolves_from_actual_text_bbox(self) -> None:
        segment = self._all_role_segment()
        background = next(
            item
            for item in segment["component_contract"]["members"]
            if item["component_id"] == "member-bg"
        )
        background["policy"] = "adjust_background"
        background["adjustment_method"] = "rewrite_untransformed_rect"
        background["dependent_geometry"] = {
            "schema": TRANSLATION_DEPENDENT_GEOMETRY_SCHEMA,
            "measurement_basis": "actual_candidate_text_span_bbox",
            "bounds_policy": "within_source_bbox",
            "maximum_delta_pt": 0.05,
            "minimum_width_pt": 1.0,
            "minimum_height_pt": 1.0,
            "edge_bindings": {
                "x0": {"basis": "source_bbox", "edge": "x0", "offset_pt": 0.0},
                "y0": {"basis": "source_bbox", "edge": "y0", "offset_pt": 0.0},
                "x1": {
                    "basis": "candidate_member_bbox",
                    "member_id": "member-live",
                    "edge": "x1",
                    "offset_pt": 2.0,
                },
                "y1": {"basis": "source_bbox", "edge": "y1", "offset_pt": 0.0},
            },
        }
        self.assertEqual(validate_compound_component_contract(segment, [0, 0, 30, 30]), [])
        bboxes = {"member-live": [1.0, 1.0, 14.0, 4.0]}
        target, evidence = resolve_translation_dependent_bbox(
            background, bboxes, page_rect=[0, 0, 30, 30]
        )
        self.assertEqual(target, [0.0, 0.0, 16.0, 12.0])
        self.assertEqual(evidence["driver_candidate_bboxes"]["member-live"], bboxes["member-live"])
        checks, issues = evaluate_translation_dependent_geometry(
            segment["component_contract"],
            {**bboxes, "member-bg": target},
            page_rect=[0, 0, 30, 30],
        )
        self.assertFalse(issues)
        self.assertEqual(checks[0]["status"], "PASS")

    def test_translation_dependency_missing_driver_and_wrong_target_fail_closed(self) -> None:
        segment = self._all_role_segment()
        background = next(
            item
            for item in segment["component_contract"]["members"]
            if item["component_id"] == "member-bg"
        )
        background.update(
            {
                "policy": "adjust_background",
                "adjustment_method": "rewrite_untransformed_rect",
                "dependent_geometry": {
                    "schema": TRANSLATION_DEPENDENT_GEOMETRY_SCHEMA,
                    "measurement_basis": "actual_candidate_text_span_bbox",
                    "bounds_policy": "within_source_bbox",
                    "maximum_delta_pt": 0.05,
                    "edge_bindings": {
                        "x0": {"basis": "source_bbox", "edge": "x0", "offset_pt": 0.0},
                        "y0": {"basis": "source_bbox", "edge": "y0", "offset_pt": 0.0},
                        "x1": {
                            "basis": "candidate_member_bbox",
                            "member_id": "member-live",
                            "edge": "x1",
                            "offset_pt": 2.0,
                        },
                        "y1": {"basis": "source_bbox", "edge": "y1", "offset_pt": 0.0},
                    },
                },
            }
        )
        with self.assertRaisesRegex(CompoundComponentError, "driver bbox"):
            resolve_translation_dependent_bbox(background, {}, page_rect=[0, 0, 30, 30])
        checks, issues = evaluate_translation_dependent_geometry(
            segment["component_contract"],
            {
                "member-live": [1.0, 1.0, 14.0, 4.0],
                "member-bg": [0.0, 0.0, 17.0, 12.0],
            },
            page_rect=[0, 0, 30, 30],
        )
        self.assertEqual(checks[0]["status"], "FAIL")
        self.assertEqual(issues[0]["code"], "TRANSLATION_DEPENDENT_GEOMETRY_MISMATCH")

    def test_translation_dependency_on_preserved_related_member_is_rejected(self) -> None:
        for component_id in (
            "member-icon",
            "member-frame",
            "member-rule",
            "member-bg",
            "member-neighbor",
        ):
            with self.subTest(component_id=component_id):
                segment = self._all_role_segment()
                member = next(
                    item
                    for item in segment["component_contract"]["members"]
                    if item["component_id"] == component_id
                )
                member["dependent_geometry"] = {
                    "schema": TRANSLATION_DEPENDENT_GEOMETRY_SCHEMA,
                    "measurement_basis": "actual_candidate_text_span_bbox",
                    "bounds_policy": "within_source_bbox",
                    "maximum_delta_pt": 0.05,
                    "edge_bindings": {
                        "x0": {
                            "basis": "source_bbox",
                            "edge": "x0",
                            "offset_pt": 0.0,
                        },
                        "y0": {
                            "basis": "source_bbox",
                            "edge": "y0",
                            "offset_pt": 0.0,
                        },
                        "x1": {
                            "basis": "candidate_member_bbox",
                            "member_id": "member-live",
                            "edge": "x1",
                            "offset_pt": 2.0,
                        },
                        "y1": {
                            "basis": "source_bbox",
                            "edge": "y1",
                            "offset_pt": 0.0,
                        },
                    },
                }
                codes = {
                    item["code"]
                    for item in validate_compound_component_contract(
                        segment, [0, 0, 30, 30]
                    )
                }
                self.assertIn("TRANSLATION_DEPENDENCY_POLICY_UNSUPPORTED", codes)

    @staticmethod
    def _visible_layout_contract() -> dict:
        return {
            "schema": COMPOSITED_VISIBLE_LAYOUT_SCHEMA,
            "contract_id": "synthetic-visible-rule-template",
            "normalization": "anchor_top_left",
            "axis": "horizontal",
            "maximum_delta_pt": 0.05,
            "compare": ["start", "end", "length", "thickness", "center_cross"],
            "instances": [
                {
                    "instance_id": "first",
                    "anchor_member_id": "first-marker",
                    "subject_member_id": "first-rule",
                    "opaque_occluder_member_ids": ["first-plate"],
                },
                {
                    "instance_id": "second",
                    "anchor_member_id": "second-marker",
                    "subject_member_id": "second-rule",
                    "opaque_occluder_member_ids": ["second-plate"],
                },
            ],
        }

    def test_composited_visible_rule_length_detects_opaque_plate_difference(self) -> None:
        bboxes = {
            "first-marker": [10.0, 10.0, 12.0, 14.0],
            "first-rule": [22.0, 12.0, 60.0, 13.0],
            "first-plate": [8.0, 8.0, 35.0, 16.0],
            "second-marker": [10.0, 40.0, 12.0, 44.0],
            "second-rule": [22.0, 42.0, 60.0, 43.0],
            "second-plate": [8.0, 38.0, 25.0, 46.0],
        }
        checks, issues = evaluate_composited_visible_layouts(
            [self._visible_layout_contract()], bboxes
        )
        self.assertEqual(checks[0]["status"], "FAIL")
        self.assertEqual(issues[0]["code"], "COMPOSITED_VISIBLE_LAYOUT_MISMATCH")
        self.assertEqual(checks[0]["absolute_deltas_pt"]["length"], 10.0)

    def test_composited_visible_rule_passes_after_dependent_plate_alignment(self) -> None:
        bboxes = {
            "first-marker": [10.0, 10.0, 12.0, 14.0],
            "first-rule": [22.0, 12.0, 60.0, 13.0],
            "first-plate": [8.0, 8.0, 25.0, 16.0],
            "second-marker": [10.0, 40.0, 12.0, 44.0],
            "second-rule": [22.0, 42.0, 60.0, 43.0],
            "second-plate": [8.0, 38.0, 25.0, 46.0],
        }
        checks, issues = evaluate_composited_visible_layouts(
            [self._visible_layout_contract()], bboxes
        )
        self.assertFalse(issues)
        self.assertEqual(checks[0]["status"], "PASS")
        self.assertEqual(checks[0]["absolute_deltas_pt"]["length"], 0.0)

    def test_composited_visible_layout_non_unique_or_missing_fields_fail_closed(self) -> None:
        contract = self._visible_layout_contract()
        contract["instances"][0]["opaque_occluder_member_ids"] = [
            "first-left-plate",
            "first-middle-plate",
        ]
        bboxes = {
            "first-marker": [10.0, 10.0, 12.0, 14.0],
            "first-rule": [22.0, 12.0, 60.0, 13.0],
            "first-left-plate": [8.0, 8.0, 25.0, 16.0],
            "first-middle-plate": [35.0, 8.0, 40.0, 16.0],
            "second-marker": [10.0, 40.0, 12.0, 44.0],
            "second-rule": [22.0, 42.0, 60.0, 43.0],
            "second-plate": [8.0, 38.0, 25.0, 46.0],
        }
        checks, issues = evaluate_composited_visible_layouts([contract], bboxes)
        self.assertFalse(checks)
        self.assertEqual(issues[0]["code"], "VISIBLE_LAYOUT_INTERVAL_NOT_UNIQUE")
        malformed = self._visible_layout_contract()
        del malformed["schema"]
        checks, issues = evaluate_composited_visible_layouts([malformed], {})
        self.assertFalse(checks)
        self.assertEqual(issues[0]["code"], "VISIBLE_LAYOUT_CONTRACT_FIELDS")

    def test_preserve_complete_visual_cannot_hide_required_translation(self) -> None:
        segment = self._all_role_segment()
        frame = next(
            item
            for item in segment["component_contract"]["members"]
            if item["component_id"] == "member-frame"
        )
        frame["policy"] = "preserve_complete_visual"
        codes = {
            item["code"]
            for item in validate_compound_component_contract(segment, [0, 0, 30, 30])
        }
        self.assertIn("PRESERVE_TRANSLATABLE_COMPONENT", codes)

    def test_allowlist_requires_reason_scope_and_policy_basis(self) -> None:
        invalid = {
            "schema": ENGLISH_ALLOWLIST_SCHEMA,
            "scope": {"document_id": "fixture", "pages": [1]},
            "allowed": [
                {
                    "token": "ExampleBrand",
                    "type": "brand",
                    "reason": "tool difficulty",
                    "scope": {"pages": [1], "segment_ids": ["segment-a"], "exact": True},
                }
            ],
        }
        invalid_codes = {item["code"] for item in validate_english_allowlist(invalid)}
        self.assertIn("ALLOWLIST_ENTRY_REASON", invalid_codes)
        self.assertIn("ALLOWLIST_ENTRY_BASIS", invalid_codes)
        valid = copy.deepcopy(invalid)
        valid["allowed"][0]["reason"] = "Registered brand name is protected content"
        valid["allowed"][0]["basis"] = {
            "type": "protected_content_policy",
            "reference": "translation-policy protected brand rule",
        }
        self.assertEqual(validate_english_allowlist(valid), [])

        outside = copy.deepcopy(valid)
        outside["allowed"][0]["scope"]["pages"] = [2]
        outside_codes = {
            item["code"] for item in validate_english_allowlist(outside)
        }
        self.assertIn("ALLOWLIST_ENTRY_SCOPE_OUTSIDE_DOCUMENT", outside_codes)

        duplicate = copy.deepcopy(valid)
        duplicate["allowed"].append(copy.deepcopy(duplicate["allowed"][0]))
        duplicate_codes = {
            item["code"] for item in validate_english_allowlist(duplicate)
        }
        self.assertIn("ALLOWLIST_ENTRY_DUPLICATE", duplicate_codes)

    def test_unique_safe_y_to_l_reserialization_restores_source_operator(self) -> None:
        source = b"q 1 0 0 1 10 20 cm 0 0 m 4 5 4 5 y f Q"
        candidate = b"q 1 0 0 1 10 20 cm 0 0 m 4 5 l f Q"
        source_record = parse_content_paths(source, stream_xref=11)[0]
        candidate_record = parse_content_paths(candidate, stream_xref=21)[0]
        self.assertTrue(
            content_path_reserialization_equivalent(
                source_record["signature"], candidate_record["signature"]
            )
        )
        updated, evidence = restore_reserialized_path_from_source(
            source,
            candidate,
            source_record["signature"],
            candidate_record["signature"],
            source_stream_xref=11,
            candidate_stream_xref=21,
        )
        updated_record = parse_content_paths(updated, stream_xref=21)[0]
        self.assertTrue(
            content_path_construction_signatures_equal(
                source_record["signature"], updated_record["signature"]
            )
        )
        self.assertEqual(
            evidence["status"], "RESTORED_SOURCE_OPERATOR_SEQUENCE_VERIFIED"
        )

    def test_changed_curve_or_duplicate_match_blocks_reserialization_repair(self) -> None:
        source = b"q 0 0 m 4 5 4 5 y f Q"
        changed = b"q 0 0 m 4 6 l f Q"
        source_record = parse_content_paths(source)[0]
        changed_record = parse_content_paths(changed)[0]
        self.assertFalse(
            content_path_reserialization_equivalent(
                source_record["signature"], changed_record["signature"]
            )
        )
        duplicate = b"q 0 0 m 4 5 l f 0 0 m 4 5 l f Q"
        duplicate_record = parse_content_paths(duplicate)[0]
        with self.assertRaises(CompoundComponentError) as captured:
            restore_reserialized_path_from_source(
                source,
                duplicate,
                source_record["signature"],
                duplicate_record["signature"],
            )
        self.assertEqual(
            captured.exception.code, "CANDIDATE_RESERIALIZED_PATH_MATCH_COUNT"
        )

    def test_batch_reserialization_restores_multiple_unique_source_paths(self) -> None:
        source_first = b"q 1 0 0 1 10 20 cm 0 0 m 4 5 4 5 y f Q"
        source_second = b"q 1 0 0 1 30 40 cm 1 2 m 7 8 7 8 y S Q"
        candidate = (
            b"q 1 0 0 1 10 20 cm 0 0 m 4 5 l f Q "
            b"q 1 0 0 1 30 40 cm 1 2 m 7 8 l S Q"
        )
        source_first_record = parse_content_paths(source_first, stream_xref=11)[0]
        source_second_record = parse_content_paths(source_second, stream_xref=12)[0]
        candidate_records = parse_content_paths(candidate, stream_xref=21)
        updated, evidence = restore_reserialized_paths_from_source_batch(
            {11: source_first, 12: source_second},
            candidate,
            [
                {
                    "source_stream_xref": 11,
                    "source_signature": source_first_record["signature"],
                    "candidate_signature": candidate_records[0]["signature"],
                },
                {
                    "source_stream_xref": 12,
                    "source_signature": source_second_record["signature"],
                    "candidate_signature": candidate_records[1]["signature"],
                },
            ],
            candidate_stream_xref=21,
        )
        updated_records = parse_content_paths(updated, stream_xref=21)
        self.assertEqual(len(evidence), 2)
        self.assertTrue(
            content_path_construction_signatures_equal(
                source_first_record["signature"], updated_records[0]["signature"]
            )
        )
        self.assertTrue(
            content_path_construction_signatures_equal(
                source_second_record["signature"], updated_records[1]["signature"]
            )
        )

    def test_batch_reserialization_rejects_duplicate_candidate_declaration(self) -> None:
        source = b"q 0 0 m 4 5 4 5 y f Q"
        candidate = b"q 0 0 m 4 5 l f Q"
        source_record = parse_content_paths(source, stream_xref=11)[0]
        candidate_record = parse_content_paths(candidate, stream_xref=21)[0]
        declaration = {
            "source_stream_xref": 11,
            "source_signature": source_record["signature"],
            "candidate_signature": candidate_record["signature"],
        }
        with self.assertRaises(CompoundComponentError) as captured:
            restore_reserialized_paths_from_source_batch(
                {11: source},
                candidate,
                [declaration, copy.deepcopy(declaration)],
                candidate_stream_xref=21,
            )
        self.assertEqual(
            captured.exception.code, "RESERIALIZED_PATH_OPERATION_RANGE_OVERLAP"
        )


if __name__ == "__main__":
    unittest.main()
