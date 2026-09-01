#!/usr/bin/env python3
from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import fitz


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
REGULAR_FONT = Path(r"C:\Windows\Fonts\msjh.ttc")
sys.path.insert(0, str(SCRIPTS))

from _inline_visual_sequences import (  # noqa: E402
    COPY_METHOD,
    INLINE_RELOCATION_SCHEMA,
    INLINE_SWEEP_SCHEMA,
    INTERNAL_CONTENT_POLICY,
    LEGACY_TWO_STAGE_MASK_MODE,
    OBJECT_POLICY,
    OPAQUE_COVER_BASIS,
    POSITION_POLICY,
    inline_contract,
    validate_inline_superseded_segments,
    validate_inline_visual_sequence,
    validate_inline_visual_sweeps,
    validate_legacy_two_stage_overlay_evidence,
    versioned_inline_contract,
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def run_script(name: str, *args: object, expected: int = 0) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / name), *(str(arg) for arg in args)],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
        env={
            **os.environ,
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )
    if result.returncode != expected:
        raise AssertionError(
            f"{name} returned {result.returncode}, expected {expected}\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
    return result


def make_sequence_segment(
    segment_id: str,
    page: int,
    *,
    source_y: float = 100.0,
    target_y: float = 40.0,
    labels: tuple[str, str] = ("上鍵", "下鍵"),
) -> dict:
    source_boxes = [
        [200.0, source_y, 210.0, source_y + 10.0],
        [212.0, source_y, 222.0, source_y + 10.0],
    ]
    target_boxes = [
        [60.5, target_y, 70.5, target_y + 10.0],
        [76.5, target_y, 86.5, target_y + 10.0],
    ]
    fragments = [
        {
            "fragment_id": "prefix",
            "source_text": "Press",
            "zh_TW": "按下",
            "target_bbox": [50.0, target_y + 2.0, 60.0, target_y + 9.0],
            "layout_role": "left_of_first_choice_visual",
        },
        {
            "fragment_id": "connector",
            "source_text": "or",
            "zh_TW": "或",
            "target_bbox": [71.0, target_y + 2.0, 76.0, target_y + 9.0],
            "layout_role": "between_choice_visuals",
        },
        {
            "fragment_id": "suffix",
            "source_text": "button",
            "zh_TW": "按鈕設定。",
            "target_bbox": [87.0, target_y + 2.0, 140.0, target_y + 9.0],
            "layout_role": "right_of_second_choice_visual_first_line",
        },
    ]
    return {
        "segment_id": segment_id,
        "page": page,
        "bbox": [45.0, 35.0, 230.0, source_y + 12.0],
        "render": {
            "action": "replace",
            "target_bbox": [45.0, target_y - 5.0, 145.0, target_y + 15.0],
            "mask_mode": "remove_text_only",
            "mask_padding_pt": 0,
            "fragments": fragments,
            "display_text_with_visual_semantics": (
                f"按下〔{labels[0]}圖示〕或〔{labels[1]}圖示〕按鈕設定。"
            ),
            "inline_visual_relocation": {
                "schema": INLINE_RELOCATION_SCHEMA,
                "policy": "natural_inline_choice_sequence",
                "homologous_set_id": "fixture.up-down-choice",
                "connector": "或",
                "maximum_gap_pt": 1.0,
                "object_position_policy": POSITION_POLICY,
                "internal_content_policy": INTERNAL_CONTENT_POLICY,
                "cover_bboxes": [[45.0, 35.0, 230.0, source_y + 12.0]],
                "cover_fill": [1.0, 1.0, 1.0],
                "cover_fill_basis": OPAQUE_COVER_BASIS,
                "relocations": [
                    {
                        "source_clip_bbox": source_box,
                        "target_clip_bbox": target_box,
                        "horizontal_shift_pt": round(target_box[0] - source_box[0], 3),
                        "vertical_shift_pt": round(target_box[1] - source_box[1], 3),
                        "semantic_label": label,
                        "object_policy": OBJECT_POLICY,
                        "copy_method": COPY_METHOD,
                    }
                    for source_box, target_box, label in zip(
                        source_boxes, target_boxes, labels, strict=True
                    )
                ],
            },
        },
    }


def make_full_scope_manifest(segments: list[dict]) -> dict:
    segment_ids = [segment["segment_id"] for segment in segments]
    return {
        "source": {"page_count": 19},
        "selected_pages": list(range(1, 20)),
        "segments": segments,
        "inline_visual_sweeps": [
            {
                "schema": INLINE_SWEEP_SCHEMA,
                "sweep_id": "fixture.up-down-choice",
                "pattern": "choice_between_two_complete_visuals",
                "connector": "或",
                "scope_pages": list(range(1, 20)),
                "expected_instance_count": len(segment_ids),
                "expected_segment_ids": segment_ids,
                "detection_basis": "document_wide_source_visual_scan",
                "discovery_status": "DOCUMENT_WIDE_COMPLETE",
            }
        ],
    }


class InlineVisualSequenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_eight_homologous_instances_allow_whole_object_vertical_moves(self) -> None:
        pages = [6, 7, 8, 14, 16, 16, 16, 16]
        segments = [
            make_sequence_segment(
                f"fixture.p{page:04d}.choice-{index}",
                page,
                source_y=100.0 if page in {6, 8} else 40.0,
                target_y=40.0,
                labels=("下鍵", "上鍵") if page == 16 else ("上鍵", "下鍵"),
            )
            for index, page in enumerate(pages, start=1)
        ]
        for segment in segments:
            self.assertEqual(
                validate_inline_visual_sequence(segment, [0.0, 0.0, 300.0, 200.0]),
                [],
            )
        manifest = make_full_scope_manifest(segments)
        self.assertEqual(validate_inline_visual_sweeps(manifest), [])
        page6 = segments[0]["render"]["inline_visual_relocation"]["relocations"]
        self.assertTrue(all(item["vertical_shift_pt"] < 0 for item in page6))
        page7 = segments[1]["render"]["inline_visual_relocation"]["relocations"]
        self.assertTrue(all(item["vertical_shift_pt"] == 0 for item in page7))

    def test_historical_contract_is_not_executed_as_v1(self) -> None:
        segment = make_sequence_segment("fixture.p0001.legacy", 1)
        del segment["render"]["inline_visual_relocation"]["schema"]
        self.assertIsNotNone(inline_contract(segment))
        self.assertIsNone(versioned_inline_contract(segment))
        issues = validate_inline_visual_sequence(
            segment, [0.0, 0.0, 300.0, 200.0]
        )
        self.assertEqual(issues[0]["code"], "INLINE_VISUAL_LEGACY_UNVERSIONED")
        self.assertEqual(issues[0]["severity"], "NEEDS_REVIEW")

    def test_missing_connector_and_unnatural_gap_fail_closed(self) -> None:
        segment = make_sequence_segment("fixture.p0001.choice", 1)
        segment["render"]["fragments"][1]["zh_TW"] = ""
        segment["render"]["fragments"][2]["target_bbox"][0] = 100.0
        codes = {
            issue["code"]
            for issue in validate_inline_visual_sequence(
                segment, [0.0, 0.0, 300.0, 200.0]
            )
        }
        self.assertIn("INLINE_VISUAL_RENDERED_CONNECTOR", codes)
        self.assertIn("INLINE_VISUAL_UNNATURAL_GAP", codes)

    def test_scaling_or_internal_edit_policy_is_blocked(self) -> None:
        segment = make_sequence_segment("fixture.p0001.choice", 1)
        first = segment["render"]["inline_visual_relocation"]["relocations"][0]
        first["target_clip_bbox"][2] += 2.0
        first["object_policy"] = "redraw_visual"
        first["copy_method"] = "raster_recreation"
        codes = {
            issue["code"]
            for issue in validate_inline_visual_sequence(
                segment, [0.0, 0.0, 300.0, 200.0]
            )
        }
        self.assertIn("INLINE_VISUAL_INTERNAL_SCALING", codes)
        self.assertIn("INLINE_VISUAL_OBJECT_POLICY", codes)
        self.assertIn("INLINE_VISUAL_COPY_METHOD", codes)

    def test_homologous_sweep_rejects_an_unlisted_instance(self) -> None:
        segments = [
            make_sequence_segment("fixture.p0006.choice", 6),
            make_sequence_segment("fixture.p0008.choice", 8),
        ]
        manifest = make_full_scope_manifest(segments)
        manifest["inline_visual_sweeps"][0]["expected_segment_ids"].pop()
        manifest["inline_visual_sweeps"][0]["expected_instance_count"] = 1
        codes = {issue["code"] for issue in validate_inline_visual_sweeps(manifest)}
        self.assertIn("INLINE_VISUAL_SWEEP_COVERAGE", codes)
        self.assertIn("INLINE_VISUAL_SWEEP_GLOBAL_COVERAGE", codes)

    def test_superseded_fragment_must_be_inside_owner_cover(self) -> None:
        owner = make_sequence_segment("fixture.p0006.owner", 6)
        old = {"segment_id": "fixture.p0006.old-button", "page": 6, "bbox": [80, 80, 90, 90]}
        manifest = {
            "segments": [owner, old],
            "post_rebuild_superseded_segments": {
                old["segment_id"]: owner["segment_id"]
            },
        }
        self.assertEqual(validate_inline_superseded_segments(manifest), [])
        old["bbox"] = [250, 150, 260, 160]
        codes = {
            issue["code"] for issue in validate_inline_superseded_segments(manifest)
        }
        self.assertIn("INLINE_SUPERSEDED_NOT_COVERED", codes)

    def test_legacy_two_stage_overlay_requires_hashes_zero_residue_and_drawings(self) -> None:
        source = self.root / "source.bin"
        candidate = self.root / "candidate.bin"
        manifest = self.root / "manifest.json"
        rebuild_path = self.root / "rebuild.json"
        source.write_bytes(b"source")
        candidate.write_bytes(b"candidate")
        manifest.write_text("{}", encoding="utf-8")
        overlay_path = self.root / "overlay.json"
        segment_id = "fixture.p0001.choice"
        overlay = {
            "source": {"sha256": sha256(source)},
            "manifest": {"sha256": sha256(manifest)},
            "output": {"sha256": sha256(candidate)},
            "stage1": {
                "status": "PASS",
                "residual_text_span_count": 0,
                "source_drawing_verification": {"status": "PASS"},
            },
            "stage2": {
                "status": "PASS",
                "operations": [{"segment_id": segment_id, "status": "PASS"}],
            },
            "source_drawing_verification": {
                "status": "PASS",
                "missing_source_record_count": 0,
            },
            "status": "PASS",
        }
        overlay_path.write_text(json.dumps(overlay), encoding="utf-8")
        rebuild = {
            "post_rebuild_final_inline_visual_overlay": {
                "evidence_path": str(overlay_path),
                "evidence_sha256": sha256(overlay_path),
            }
        }
        report_item = {
            "mask_mode": LEGACY_TWO_STAGE_MASK_MODE,
            "mask_fill": [1.0, 1.0, 1.0],
            "post_rebuild_inline_visual_relocation": {
                "policy": "natural_inline_choice_sequence",
                "copied_visuals": [{"source_clip_bbox": [0, 0, 1, 1]}],
                "prior_live_text_removed_before_final_overlay": True,
            },
        }
        valid, evidence, problems = validate_legacy_two_stage_overlay_evidence(
            rebuild=rebuild,
            rebuild_path=rebuild_path,
            report_item=report_item,
            segment_id=segment_id,
            source_path=source,
            candidate_path=candidate,
            manifest_path=manifest,
        )
        self.assertTrue(valid)
        self.assertEqual(problems, [])
        self.assertEqual(evidence["status"], "PASS")

        overlay["stage1"]["residual_text_span_count"] = 1
        overlay_path.write_text(json.dumps(overlay), encoding="utf-8")
        rebuild["post_rebuild_final_inline_visual_overlay"]["evidence_sha256"] = sha256(overlay_path)
        valid, _, problems = validate_legacy_two_stage_overlay_evidence(
            rebuild=rebuild,
            rebuild_path=rebuild_path,
            report_item=report_item,
            segment_id=segment_id,
            source_path=source,
            candidate_path=candidate,
            manifest_path=manifest,
        )
        self.assertFalse(valid)
        self.assertIn("INLINE_OVERLAY_STAGE1_INCOMPLETE", {item["code"] for item in problems})

    def test_rebuild_and_inline_qa_copy_two_complete_visuals(self) -> None:
        source = self.root / "source.pdf"
        source_doc = fitz.open()
        page = source_doc.new_page(width=300, height=200)
        page.insert_text((20, 60), "Press up or down button.", fontsize=10, fontname="helv")
        page.draw_circle((106, 46), 5, color=(0, 0, 0), width=1)
        page.draw_line((103, 48), (106, 43), color=(0, 0, 0), width=1)
        page.draw_line((106, 43), (109, 48), color=(0, 0, 0), width=1)
        page.draw_circle((120, 46), 5, color=(0, 0, 0), width=1)
        page.draw_line((117, 44), (120, 49), color=(0, 0, 0), width=1)
        page.draw_line((120, 49), (123, 44), color=(0, 0, 0), width=1)
        source_doc.save(source)
        source_doc.close()

        segment_id = "fixture-inline.p0001.choice"
        source_boxes = [[100.0, 40.0, 112.0, 52.0], [114.0, 40.0, 126.0, 52.0]]
        target_boxes = [[40.0, 95.0, 52.0, 107.0], [60.5, 95.0, 72.5, 107.0]]
        layout = {
            "schema": "pdf-tw-localize/layout-spec/v1",
            "document_id": "fixture-inline",
            "inline_visual_sweeps": [
                {
                    "schema": INLINE_SWEEP_SCHEMA,
                    "sweep_id": "fixture-inline.up-down-choice",
                    "pattern": "choice_between_two_complete_visuals",
                    "connector": "或",
                    "scope_pages": [1],
                    "expected_instance_count": 1,
                    "expected_segment_ids": [segment_id],
                    "detection_basis": "document_wide_source_visual_scan",
                    "discovery_status": "DOCUMENT_WIDE_COMPLETE",
                }
            ],
            "pages": {
                "1": {
                    "context": {
                        "purpose": "Inline control choice fixture",
                        "heading_hierarchy": [],
                        "neighboring_context": [],
                        "table_context": [],
                        "condition_pairs": [],
                        "ui_state": ["up/down choice"],
                        "image_text_inventory_status": "NOT_APPLICABLE",
                    },
                    "segments": [
                        {
                            "key": "choice",
                            "semantic_type": "paragraph",
                            "source_refs": [{"block": 0, "line": 0}],
                            "reading_order": 1,
                            "render": {
                                "action": "replace",
                                "target_bbox": [20.0, 93.0, 145.0, 112.0],
                                "mask_bbox": [20.0, 50.0, 145.0, 62.0],
                                "mask_mode": "remove_text_only",
                                "mask_padding_pt": 0,
                                "font_size_pt": 8.0,
                                "min_font_size_pt": 7.5,
                                "fragments": [
                                    {
                                        "fragment_id": "prefix",
                                        "source_text": "Press",
                                        "zh_TW": "按下",
                                        "target_bbox": [20.0, 97.0, 39.5, 106.0],
                                        "layout_role": "left_of_first_choice_visual",
                                    },
                                    {
                                        "fragment_id": "connector",
                                        "source_text": "or",
                                        "zh_TW": "或",
                                        "target_bbox": [52.5, 97.0, 60.0, 106.0],
                                        "layout_role": "between_choice_visuals",
                                    },
                                    {
                                        "fragment_id": "suffix",
                                        "source_text": "button",
                                        "zh_TW": "按鈕設定。",
                                        "target_bbox": [73.0, 97.0, 140.0, 106.0],
                                        "layout_role": "right_of_second_choice_visual_first_line",
                                    },
                                ],
                                "display_text_with_visual_semantics": "按下〔上鍵圖示〕或〔下鍵圖示〕按鈕設定。",
                                "inline_visual_relocation": {
                                    "schema": INLINE_RELOCATION_SCHEMA,
                                    "policy": "natural_inline_choice_sequence",
                                    "homologous_set_id": "fixture-inline.up-down-choice",
                                    "connector": "或",
                                    "maximum_gap_pt": 1.0,
                                    "object_position_policy": POSITION_POLICY,
                                    "internal_content_policy": INTERNAL_CONTENT_POLICY,
                                    "cover_bboxes": [[15.0, 35.0, 150.0, 112.0]],
                                    "cover_fill": [1.0, 1.0, 1.0],
                                    "cover_fill_basis": OPAQUE_COVER_BASIS,
                                    "relocations": [
                                        {
                                            "source_clip_bbox": source_box,
                                            "target_clip_bbox": target_box,
                                            "horizontal_shift_pt": target_box[0] - source_box[0],
                                            "vertical_shift_pt": target_box[1] - source_box[1],
                                            "semantic_label": label,
                                            "object_policy": OBJECT_POLICY,
                                            "copy_method": COPY_METHOD,
                                        }
                                        for source_box, target_box, label in zip(
                                            source_boxes,
                                            target_boxes,
                                            ("上鍵", "下鍵"),
                                            strict=True,
                                        )
                                    ],
                                },
                            },
                        }
                    ],
                    "ignored_source_refs": [],
                }
            },
        }
        layout_path = self.root / "layout.json"
        layout_path.write_text(json.dumps(layout, ensure_ascii=False, indent=2), encoding="utf-8")
        extraction = self.root / "extraction.json"
        run_script(
            "extract_segments.py",
            source,
            "--pages",
            "1",
            "--document-id",
            "fixture-inline",
            "--layout-spec",
            layout_path,
            "--output",
            extraction,
        )
        manifest = json.loads(extraction.read_text(encoding="utf-8"))
        manifest["segments"][0]["zh_TW"] = "按下上鍵或下鍵按鈕設定。"
        manifest["segments"][0]["status"] = "TRANSLATED"
        manifest["status"] = "TRANSLATED"
        translation = self.root / "translation.json"
        translation.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        run_script("validate_segments.py", translation, "--stage", "render")

        candidate = self.root / "candidate.pdf"
        rebuild = self.root / "rebuild.json"
        run_script(
            "rebuild_pdf.py",
            source,
            "--manifest",
            translation,
            "--font",
            REGULAR_FONT,
            "--output",
            candidate,
            "--report",
            rebuild,
        )
        rebuild_payload = json.loads(rebuild.read_text(encoding="utf-8"))
        segment_report = rebuild_payload["segments"][0]
        self.assertEqual(segment_report["mask_mode"], "remove_text_only")
        self.assertIsNone(segment_report["mask_fill"])
        relocation_report = segment_report["post_rebuild_inline_visual_relocation"]
        self.assertEqual(relocation_report["status"], "APPLIED_SOURCE_CLIP_COPY")
        self.assertEqual(len(relocation_report["copied_visuals"]), 2)
        self.assertTrue(
            all(
                item["copy_method"] == COPY_METHOD
                and item["internal_content_edited"] is False
                for item in relocation_report["copied_visuals"]
            )
        )
        self.assertIn("或", [line["text"] for line in segment_report["rendered_lines"]])

        inline_qa = self.root / "inline-qa.json"
        run_script(
            "qa_inline_visual_sequences.py",
            "--source",
            source,
            "--candidate",
            candidate,
            "--manifest",
            translation,
            "--rebuild-report",
            rebuild,
            "--dpi",
            "300",
            "--output",
            inline_qa,
        )
        inline_payload = json.loads(inline_qa.read_text(encoding="utf-8"))
        self.assertEqual(inline_payload["machine_qa"], "MACHINE_QA_PASS")
        self.assertEqual(inline_payload["scope"]["inline_sequence_count"], 1)
        self.assertEqual(inline_payload["checks"][0]["status"], "PASS")


if __name__ == "__main__":
    unittest.main()
