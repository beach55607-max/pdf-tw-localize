#!/usr/bin/env python3
from __future__ import annotations

import json
import hashlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path

import fitz
from PIL import Image, ImageDraw


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from _console import emit_json  # noqa: E402
from font_discovery import discover_font_pair  # noqa: E402
from _pdf_catalog import catalog_color_evidence  # noqa: E402
from _segment_common import (  # noqa: E402
    page_scoped_intersection_area,
    validate_manifest,
    validate_table_cell_phrase_groups,
    validate_text_alignment_contract,
    validate_text_color_contract,
)
from rebuild_pdf import (  # noqa: E402
    image_signature,
    insert_lines,
    match_source_image_placements,
    resolve_text_color,
    restore_missing_source_images,
)
from qa_rebuilt_pdf import (  # noqa: E402
    alignment_measurement,
    matching_rendered_spans,
    scoped_allowlist_tokens,
    text_color_measurement,
)


REGULAR_FONT, BOLD_FONT = discover_font_pair(SKILL_ROOT)
FONT = REGULAR_FONT


def run_script(name: str, *args: object, expected: int = 0) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / name), *(str(arg) for arg in args)],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
    )
    if result.returncode != expected:
        raise AssertionError(
            f"{name} returned {result.returncode}, expected {expected}\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
    return result


class SegmentPipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source.pdf"
        doc = fitz.open()
        page = doc.new_page(width=300, height=200)
        page.insert_text((40, 60), "WARNING 40 °C", fontsize=10, fontname="helv")
        doc.save(self.source)
        doc.close()
        self.spec = self.root / "layout.json"
        self.spec.write_text(
            json.dumps(
                {
                    "schema": "pdf-tw-localize/layout-spec/v1",
                    "document_id": "fixture",
                    "pages": {
                        "1": {
                            "context": {
                                "purpose": "Test warning",
                                "heading_hierarchy": ["WARNING"],
                                "neighboring_context": [],
                                "table_context": [],
                                "condition_pairs": [],
                                "ui_state": [],
                                "image_text_inventory_status": "NOT_APPLICABLE",
                            },
                            "segments": [
                                {
                                    "key": "warning",
                                    "semantic_type": "warning",
                                    "source_refs": [{"block": 0, "line": 0}],
                                    "reading_order": 1,
                                    "render": {
                                        "action": "replace",
                                        "target_bbox": [38, 48, 220, 72],
                                        "font_size_pt": 10,
                                        "min_font_size_pt": 7.5,
                                    },
                                }
                            ],
                            "ignored_source_refs": [],
                        }
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_large_top_aligned_heading_stays_inside_target_bbox(self) -> None:
        doc = fitz.open()
        page = doc.new_page(width=300, height=120)
        page.insert_font(fontname="twbold", fontfile=str(BOLD_FONT))
        font = fitz.Font(fontfile=str(BOLD_FONT))
        segment = {
            "segment_id": "fixture.p0001.large-heading",
            "page": 1,
            "semantic_type": "heading",
            "bbox": [20.0, 20.0, 280.0, 70.0],
            "font_style": {"source_font_size_pt": 30.0},
            "zh_TW": "大型繁體中文標題",
            "render": {
                "action": "replace",
                "target_bbox": [20.0, 20.0, 280.0, 70.0],
                "font_size_pt": 30.0,
                "min_font_size_pt": 22.5,
                "line_spacing": 1.08,
                "align": "left",
                "valign": "top",
            },
        }
        result = insert_lines(
            page,
            segment,
            font,
            "twbold",
            {"role": "bold", "font_path": str(BOLD_FONT)},
        )
        self.assertEqual(result["fit_status"], "FIT")
        self.assertGreaterEqual(result["rendered_lines"][0]["bbox"][1], 20.0)
        self.assertLessEqual(result["rendered_lines"][0]["bbox"][3], 70.0)
        doc.close()

    def test_missing_source_image_placement_is_restored_by_digest_and_bbox(self) -> None:
        source = self.root / "image-source.pdf"
        candidate = self.root / "image-candidate.pdf"
        restored = self.root / "image-restored.pdf"
        image = Image.new("L", (3, 30), color=0)
        payload = io.BytesIO()
        image.save(payload, format="PNG")

        source_doc = fitz.open()
        source_page = source_doc.new_page(width=200, height=120)
        source_page.insert_image(
            fitz.Rect(80, 30, 83, 60),
            stream=payload.getvalue(),
            keep_proportion=False,
        )
        source_doc.save(source)
        source_doc.close()

        candidate_doc = fitz.open()
        candidate_doc.new_page(width=200, height=120)
        candidate_doc.save(candidate)
        candidate_doc.close()

        evidence = restore_missing_source_images(source, candidate, restored, [1])
        self.assertEqual(evidence["status"], "PASS")
        self.assertEqual(evidence["restoration_count"], 1)

        source_doc = fitz.open(source)
        restored_doc = fitz.open(restored)
        source_images = Counter(
            image_signature(item)
            for item in source_doc[0].get_image_info(hashes=True, xrefs=True)
        )
        restored_images = Counter(
            image_signature(item)
            for item in restored_doc[0].get_image_info(hashes=True, xrefs=True)
        )
        self.assertEqual(source_images - restored_images, Counter())
        restored_doc.close()
        source_doc.close()

    def test_source_image_match_accepts_only_exact_digest_and_submillipoint_drift(self) -> None:
        digest = bytes.fromhex("00112233445566778899AABBCCDDEEFF")
        source = [{"digest": digest, "bbox": (10.0, 20.0, 30.0, 40.0)}]
        within = [
            {
                "digest": digest,
                "bbox": (10.0004, 19.9996, 30.0004, 39.9996),
            }
        ]
        matches, missing = match_source_image_placements(source, within)
        self.assertEqual(len(matches), 1)
        self.assertFalse(missing)

        wrong_digest = [
            {
                "digest": bytes.fromhex("10112233445566778899AABBCCDDEEFF"),
                "bbox": source[0]["bbox"],
            }
        ]
        matches, missing = match_source_image_placements(source, wrong_digest)
        self.assertFalse(matches)
        self.assertEqual(missing, source)

        outside = [{"digest": digest, "bbox": (10.002, 20.0, 30.0, 40.0)}]
        matches, missing = match_source_image_placements(source, outside)
        self.assertFalse(matches)
        self.assertEqual(missing, source)

    def test_ordinary_english_allowlist_tokens_do_not_leak_across_pages(self) -> None:
        tokens_by_page, exact = scoped_allowlist_tokens(
            [
                {
                    "token": "ExampleBrand",
                    "scope": {
                        "pages": [2],
                        "segment_ids": ["segment-page-2"],
                        "exact": True,
                    },
                }
            ]
        )
        self.assertNotIn("ExampleBrand", tokens_by_page.get(1, set()))
        self.assertIn("ExampleBrand", tokens_by_page[2])
        self.assertIn((2, "segment-page-2", "ExampleBrand"), exact)

    def extract_and_translate(self) -> Path:
        extraction = self.root / "extraction.json"
        run_script(
            "extract_segments.py",
            self.source,
            "--pages",
            "1",
            "--document-id",
            "fixture",
            "--layout-spec",
            self.spec,
            "--output",
            extraction,
        )
        run_script("validate_segments.py", extraction, "--stage", "extraction")
        manifest = json.loads(extraction.read_text(encoding="utf-8"))
        manifest["segments"][0]["zh_TW"] = "警告：保持 40 °C"
        manifest["segments"][0]["status"] = "TRANSLATED"
        manifest["status"] = "TRANSLATED"
        translation = self.root / "translation.json"
        translation.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return translation

    def build_semantic_pair(self) -> tuple[Path, Path]:
        source = self.root / "semantic-source.pdf"
        doc = fitz.open()
        page = doc.new_page(width=800, height=200)
        page.insert_text(
            (40, 60),
            (
                "DUAL SETPOINT COOL 30 °C HEAT 16 °C; "
                "COOL IF ROOM ABOVE COOL SETPOINT; "
                "HEAT IF ROOM BELOW HEAT SETPOINT"
            ),
            fontsize=10,
            fontname="helv",
        )
        doc.save(source)
        doc.close()
        context_ref_id = "fixture.context.dual-setpoint"
        common_cues = [
            {"text": "雙設定點", "kind": "mode", "scope": "segment"},
            {"text": "無排程事件", "kind": "condition", "scope": "segment"},
        ]
        spec = self.root / "semantic-layout.json"
        spec.write_text(
            json.dumps(
                {
                    "schema": "pdf-tw-localize/layout-spec/v1",
                    "document_id": "fixture",
                    "pages": {
                        "1": {
                            "context": {
                                "purpose": "Test paired technical value-role bindings.",
                                "heading_hierarchy": [],
                                "neighboring_context": [],
                                "table_context": [],
                                "condition_pairs": [["COOL", "HEAT"]],
                                "ui_state": [],
                                "image_text_inventory_status": "NOT_APPLICABLE",
                                "document_context_refs": [
                                    {
                                        "context_ref_id": context_ref_id,
                                        "page": 1,
                                        "source_excerpt": (
                                            "DUAL SETPOINT COOL 30 °C HEAT 16 °C; "
                                            "COOL IF ROOM ABOVE COOL SETPOINT; "
                                            "HEAT IF ROOM BELOW HEAT SETPOINT"
                                        ),
                                        "reason": (
                                            "Defines the paired-value mode and the "
                                            "source-supported control comparisons."
                                        ),
                                        "review_status": "VERIFIED_SOURCE_CONTEXT",
                                    }
                                ],
                            },
                            "segments": [
                                {
                                    "key": "paired-values",
                                    "semantic_type": "paragraph",
                                    "source_refs": [{"block": 0, "line": 0}],
                                    "reading_order": 1,
                                    "semantic_bindings": [
                                        {
                                            "binding_id": "fixture.cooling-setpoint",
                                            "parameter": "cooling_setpoint",
                                            "role": "upper_setpoint",
                                            "mode": "dual_setpoint_auto",
                                            "condition": "no_schedule_event",
                                            "comparison": "room_temperature_above_setpoint",
                                            "consequence": "cooling_performed",
                                            "clarification_mode": "source_derived_inline",
                                            "source_tokens": ["30 °C"],
                                            "required_target_cues": [
                                                {
                                                    "text": "冷房控制門檻",
                                                    "kind": "parameter",
                                                    "scope": "binding_phrase",
                                                },
                                                {
                                                    "text": "室溫高於此值",
                                                    "kind": "comparison",
                                                    "scope": "binding_phrase",
                                                },
                                                {
                                                    "text": "才進行冷房",
                                                    "kind": "consequence",
                                                    "scope": "binding_phrase",
                                                },
                                                *common_cues,
                                            ],
                                            "context_required": True,
                                            "context_ref_ids": [context_ref_id],
                                        },
                                        {
                                            "binding_id": "fixture.heating-setpoint",
                                            "parameter": "heating_setpoint",
                                            "role": "lower_setpoint",
                                            "mode": "dual_setpoint_auto",
                                            "condition": "no_schedule_event",
                                            "comparison": "room_temperature_below_setpoint",
                                            "consequence": "heating_performed",
                                            "clarification_mode": "source_derived_inline",
                                            "source_tokens": ["16 °C"],
                                            "required_target_cues": [
                                                {
                                                    "text": "暖房控制門檻",
                                                    "kind": "parameter",
                                                    "scope": "binding_phrase",
                                                },
                                                {
                                                    "text": "室溫低於此值",
                                                    "kind": "comparison",
                                                    "scope": "binding_phrase",
                                                },
                                                {
                                                    "text": "才進行暖房",
                                                    "kind": "consequence",
                                                    "scope": "binding_phrase",
                                                },
                                                *common_cues,
                                            ],
                                            "context_required": True,
                                            "context_ref_ids": [context_ref_id],
                                        },
                                    ],
                                    "render": {
                                        "action": "replace",
                                        "target_bbox": [38, 48, 780, 95],
                                        "font_size_pt": 10,
                                        "min_font_size_pt": 7.5,
                                    },
                                }
                            ],
                            "ignored_source_refs": [],
                        }
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        extraction = self.root / "semantic-extraction.json"
        run_script(
            "extract_segments.py",
            source,
            "--pages",
            "1",
            "--document-id",
            "fixture",
            "--layout-spec",
            spec,
            "--output",
            extraction,
        )
        manifest = json.loads(extraction.read_text(encoding="utf-8"))
        segment = manifest["segments"][0]
        segment["zh_TW"] = (
            "無排程事件時採用雙設定點："
            "冷房控制門檻 30 °C（室溫高於此值時才進行冷房）；"
            "暖房控制門檻 16 °C（室溫低於此值時才進行暖房）"
        )
        segment["status"] = "TRANSLATED"
        segment["translation_assertions"] = [
            {
                "binding_id": "fixture.cooling-setpoint",
                "parameter": "cooling_setpoint",
                "role": "upper_setpoint",
                "mode": "dual_setpoint_auto",
                "condition": "no_schedule_event",
                "comparison": "room_temperature_above_setpoint",
                "consequence": "cooling_performed",
                "clarification_mode": "source_derived_inline",
                "target_phrase": "冷房控制門檻 30 °C（室溫高於此值時才進行冷房）",
            },
            {
                "binding_id": "fixture.heating-setpoint",
                "parameter": "heating_setpoint",
                "role": "lower_setpoint",
                "mode": "dual_setpoint_auto",
                "condition": "no_schedule_event",
                "comparison": "room_temperature_below_setpoint",
                "consequence": "heating_performed",
                "clarification_mode": "source_derived_inline",
                "target_phrase": "暖房控制門檻 16 °C（室溫低於此值時才進行暖房）",
            },
        ]
        manifest["status"] = "TRANSLATED"
        translation = self.root / "semantic-translation.json"
        translation.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return extraction, translation

    def test_end_to_end_cli(self) -> None:
        self.assertTrue(FONT.is_file())
        translation = self.extract_and_translate()
        validation = self.root / "translation-validation.json"
        run_script(
            "validate_segments.py",
            translation,
            "--stage",
            "render",
            "--output",
            validation,
        )
        proof = self.root / "proof.pdf"
        rebuild = self.root / "rebuild.json"
        run_script(
            "rebuild_pdf.py",
            self.source,
            "--manifest",
            translation,
            "--font",
            FONT,
            "--output",
            proof,
            "--report",
            rebuild,
        )
        allowlist = self.root / "allowlist.json"
        allowlist.write_text(
            json.dumps({"schema": "pdf-tw-localize/english-allowlist/v1", "allowed": []}),
            encoding="utf-8",
        )
        qa = self.root / "qa.json"
        run_script(
            "qa_rebuilt_pdf.py",
            "--source",
            self.source,
            "--candidate",
            proof,
            "--manifest",
            translation,
            "--rebuild-report",
            rebuild,
            "--allowlist",
            allowlist,
            "--output",
            qa,
        )
        qa_payload = json.loads(qa.read_text(encoding="utf-8"))
        self.assertEqual(qa_payload["machine_qa"], "MACHINE_QA_PASS")
        self.assertEqual(qa_payload["user_acceptance"], "NOT_CHECKED")

    def test_duplicate_id_is_blocked(self) -> None:
        translation = self.extract_and_translate()
        manifest = json.loads(translation.read_text(encoding="utf-8"))
        manifest["segments"].append(dict(manifest["segments"][0]))
        duplicate = self.root / "duplicate.json"
        duplicate.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        result = run_script("validate_segments.py", duplicate, "--stage", "translation", expected=2)
        self.assertIn("DUPLICATE_ID", result.stdout)

    def test_protected_token_loss_is_blocked(self) -> None:
        translation = self.extract_and_translate()
        manifest = json.loads(translation.read_text(encoding="utf-8"))
        manifest["segments"][0]["zh_TW"] = "警告：保持適當溫度"
        lost = self.root / "lost-token.json"
        lost.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        result = run_script("validate_segments.py", lost, "--stage", "translation", expected=2)
        self.assertIn("PROTECTED_TOKEN_LOST", result.stdout)

    def test_semantic_pair_validates_value_roles(self) -> None:
        _, translation = self.build_semantic_pair()
        report = self.root / "semantic-validation.json"
        run_script(
            "validate_segments.py",
            translation,
            "--stage",
            "translation",
            "--output",
            report,
        )
        payload = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(payload["semantic_qa"], "SEMANTIC_QA_PASS")
        self.assertEqual(payload["semantic_binding_count"], 2)

    def test_semantic_pair_blocks_swapped_value_roles(self) -> None:
        _, translation = self.build_semantic_pair()
        manifest = json.loads(translation.read_text(encoding="utf-8"))
        segment = manifest["segments"][0]
        segment["zh_TW"] = (
            "無排程事件時採用雙設定點："
            "冷房控制門檻 16 °C（室溫高於此值時才進行冷房）；"
            "暖房控制門檻 30 °C（室溫低於此值時才進行暖房）"
        )
        segment["translation_assertions"][0]["target_phrase"] = (
            "冷房控制門檻 16 °C（室溫高於此值時才進行冷房）"
        )
        segment["translation_assertions"][1]["target_phrase"] = (
            "暖房控制門檻 30 °C（室溫低於此值時才進行暖房）"
        )
        swapped = self.root / "semantic-swapped.json"
        swapped.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        result = run_script("validate_segments.py", swapped, "--stage", "translation", expected=2)
        self.assertIn("VALUE_ROLE_SWAPPED", result.stdout)

    def test_semantic_mode_and_condition_cues_are_required(self) -> None:
        _, translation = self.build_semantic_pair()
        base = json.loads(translation.read_text(encoding="utf-8"))
        cases = (
            (
                "mode",
                (
                    "無排程事件時："
                    "冷房控制門檻 30 °C（室溫高於此值時才進行冷房）；"
                    "暖房控制門檻 16 °C（室溫低於此值時才進行暖房）"
                ),
                "MODE_CONTEXT_DROPPED",
            ),
            (
                "condition",
                (
                    "採用雙設定點："
                    "冷房控制門檻 30 °C（室溫高於此值時才進行冷房）；"
                    "暖房控制門檻 16 °C（室溫低於此值時才進行暖房）"
                ),
                "CONDITION_SCOPE_DROPPED",
            ),
        )
        for name, zh_text, expected_code in cases:
            with self.subTest(name=name):
                manifest = json.loads(json.dumps(base, ensure_ascii=False))
                manifest["segments"][0]["zh_TW"] = zh_text
                path = self.root / f"semantic-{name}-dropped.json"
                path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
                result = run_script("validate_segments.py", path, "--stage", "translation", expected=2)
                self.assertIn(expected_code, result.stdout)

    def test_source_derived_clarification_requires_comparator_cue(self) -> None:
        _, translation = self.build_semantic_pair()
        manifest = json.loads(translation.read_text(encoding="utf-8"))
        segment = manifest["segments"][0]
        segment["zh_TW"] = segment["zh_TW"].replace("室溫高於此值時", "")
        segment["translation_assertions"][0]["target_phrase"] = (
            "冷房控制門檻 30 °C（才進行冷房）"
        )
        path = self.root / "semantic-comparator-dropped.json"
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        result = run_script("validate_segments.py", path, "--stage", "translation", expected=2)
        self.assertIn("COMPARISON_LOGIC_DROPPED", result.stdout)

    def test_source_derived_clarification_requires_verified_context(self) -> None:
        extraction, _ = self.build_semantic_pair()
        manifest = json.loads(extraction.read_text(encoding="utf-8"))
        binding = manifest["segments"][0]["semantic_bindings"][0]
        binding["context_required"] = False
        binding["context_ref_ids"] = []
        path = self.root / "semantic-clarification-no-context.json"
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        result = run_script("validate_segments.py", path, "--stage", "extraction", expected=2)
        self.assertIn("CLARIFICATION_SOURCE_NOT_VERIFIED", result.stdout)

    def test_source_derived_clarification_rejects_changed_consequence(self) -> None:
        _, translation = self.build_semantic_pair()
        manifest = json.loads(translation.read_text(encoding="utf-8"))
        manifest["segments"][0]["translation_assertions"][0]["consequence"] = (
            "cooling_disabled"
        )
        path = self.root / "semantic-consequence-changed.json"
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        result = run_script("validate_segments.py", path, "--stage", "translation", expected=2)
        self.assertIn("CONSEQUENCE_DROPPED", result.stdout)

    def test_hypothetical_example_is_not_a_clarification_mode(self) -> None:
        extraction, _ = self.build_semantic_pair()
        manifest = json.loads(extraction.read_text(encoding="utf-8"))
        manifest["segments"][0]["semantic_bindings"][0]["clarification_mode"] = (
            "hypothetical_example"
        )
        path = self.root / "semantic-hypothetical-example.json"
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        result = run_script("validate_segments.py", path, "--stage", "extraction", expected=2)
        self.assertIn("CLARIFICATION_POLICY_INVALID", result.stdout)

    def test_semantic_context_not_checked_needs_review(self) -> None:
        extraction, _ = self.build_semantic_pair()
        manifest = json.loads(extraction.read_text(encoding="utf-8"))
        manifest["page_contexts"][0]["document_context_refs"][0]["review_status"] = "NOT_CHECKED"
        unchecked = self.root / "semantic-context-unchecked.json"
        unchecked.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        result = run_script("validate_segments.py", unchecked, "--stage", "extraction", expected=2)
        self.assertIn('"status": "NEEDS_REVIEW"', result.stdout)
        self.assertIn("CROSS_PAGE_CONTEXT_NOT_CHECKED", result.stdout)

    def test_semantic_context_excerpt_must_match_source_page(self) -> None:
        extraction, _ = self.build_semantic_pair()
        manifest = json.loads(extraction.read_text(encoding="utf-8"))
        manifest["page_contexts"][0]["document_context_refs"][0]["source_excerpt"] = (
            "INVENTED SOURCE CONTEXT"
        )
        mismatch = self.root / "semantic-context-mismatch.json"
        mismatch.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        result = run_script("validate_segments.py", mismatch, "--stage", "extraction", expected=2)
        self.assertIn("DOCUMENT_CONTEXT_REF_MISMATCH", result.stdout)

    def test_import_translations_carries_semantic_assertions(self) -> None:
        extraction, translation = self.build_semantic_pair()
        translated = json.loads(translation.read_text(encoding="utf-8"))
        segment = translated["segments"][0]
        payload = self.root / "semantic-import.json"
        payload.write_text(
            json.dumps(
                {
                    "schema": "pdf-tw-localize/translation-import/v1",
                    "source_manifest_sha256": hashlib.sha256(extraction.read_bytes()).hexdigest().upper(),
                    "translator": "test fixture",
                    "translations": [
                        {
                            "segment_id": segment["segment_id"],
                            "zh_TW": segment["zh_TW"],
                            "translation_assertions": segment["translation_assertions"],
                        }
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        imported = self.root / "semantic-imported-manifest.json"
        run_script("import_translations.py", extraction, payload, "--output", imported)
        imported_payload = json.loads(imported.read_text(encoding="utf-8"))
        self.assertEqual(
            imported_payload["segments"][0]["translation_assertions"],
            segment["translation_assertions"],
        )

    def test_page_scoped_intersection_rejects_cross_page_coordinate_match(self) -> None:
        bbox = [228.41, 97.364, 372.308, 179.864]
        self.assertGreater(page_scoped_intersection_area(27, bbox, 27, bbox), 0.0)
        self.assertEqual(page_scoped_intersection_area(5, bbox, 27, bbox), 0.0)

    def test_fragmented_live_text_keeps_discontiguous_columns_independent(self) -> None:
        doc = fitz.open()
        page = doc.new_page(width=300, height=120)
        page.insert_font(fontname="zh_regular", fontfile=str(REGULAR_FONT))
        font = fitz.Font(fontfile=str(REGULAR_FONT))
        segment = {
            "page": 1,
            "segment_id": "fixture.discontiguous-live-row",
            "semantic_type": "table-cell",
            "bbox": [10.0, 20.0, 290.0, 34.0],
            "zh_TW": "CH1 | CH01 | 運轉停止",
            "font_style": {"source_font_size_pt": 8.0},
            "render": {
                "action": "replace",
                "target_bbox": [10.0, 16.0, 290.0, 44.0],
                "font_size_pt": 8.0,
                "min_font_size_pt": 6.0,
                "line_spacing": 1.0,
                "valign": "middle",
                "fragments": [
                    {
                        "fragment_id": "error-code",
                        "source_text": "CH1",
                        "zh_TW": "CH1",
                        "target_bbox": [12.0, 18.0, 62.0, 42.0],
                        "align": "center",
                    },
                    {
                        "fragment_id": "controller-code",
                        "source_text": "CH01",
                        "zh_TW": "CH01",
                        "target_bbox": [126.0, 18.0, 176.0, 42.0],
                        "align": "center",
                    },
                    {
                        "fragment_id": "operation-state",
                        "source_text": "Operation off",
                        "zh_TW": "運轉停止",
                        "target_bbox": [216.0, 18.0, 288.0, 42.0],
                        "align": "center",
                    },
                ],
            },
        }
        evidence = insert_lines(
            page,
            segment,
            font,
            "zh_regular",
            {"role": "regular", "path": str(REGULAR_FONT)},
        )
        self.assertEqual(evidence["fit_status"], "FIT_FRAGMENTED_LIVE_TEXT")
        self.assertEqual(len(evidence["fragment_results"]), 3)
        boxes = [item["bbox"] for item in evidence["rendered_lines"]]
        self.assertLessEqual(boxes[0][2], boxes[1][0])
        self.assertLessEqual(boxes[1][2], boxes[2][0])
        doc.close()

    def test_source_alignment_contract_uses_actual_candidate_text_bbox(self) -> None:
        contract_base = {
            "schema": "pdf-tw-localize/text-alignment/v1",
            "source_reference_bbox": [20.0, 20.0, 280.0, 60.0],
            "target_reference_bbox": [20.0, 20.0, 280.0, 60.0],
            "source_text_bbox": [220.0, 24.0, 278.0, 44.0],
            "maximum_delta_pt": 1.0,
            "measurement_basis": "actual_candidate_text_span_bbox",
        }
        right = alignment_measurement(
            {**contract_base, "alignment": "right"},
            [232.0, 24.0, 278.0, 44.0],
        )
        left = alignment_measurement(
            {
                **contract_base,
                "alignment": "left",
                "source_text_bbox": [22.0, 24.0, 80.0, 44.0],
            },
            [22.0, 24.0, 68.0, 44.0],
        )
        center = alignment_measurement(
            {
                **contract_base,
                "alignment": "center",
                "source_text_bbox": [111.0, 24.0, 191.0, 44.0],
            },
            [121.0, 24.0, 181.0, 44.0],
        )
        wrong_right = alignment_measurement(
            {**contract_base, "alignment": "right"},
            [222.0, 24.0, 268.0, 44.0],
        )
        self.assertEqual(right["status"], "PASS")
        self.assertEqual(left["status"], "PASS")
        self.assertEqual(center["status"], "PASS")
        self.assertEqual(wrong_right["status"], "FAIL")

    def test_alignment_contract_rejects_missing_fields_and_render_mismatch(self) -> None:
        segment = {
            "segment_id": "fixture.p0001.edge-label",
            "render": {
                "align": "left",
                "target_bbox": [20.0, 20.0, 280.0, 60.0],
                "alignment_contract": {
                    "schema": "pdf-tw-localize/text-alignment/v1",
                    "alignment": "right",
                },
            },
        }
        codes = {
            item["code"]
            for item in validate_text_alignment_contract(
                segment, [0.0, 0.0, 300.0, 100.0]
            )
        }
        self.assertIn("TEXT_ALIGNMENT_FIELDS", codes)

        segment["render"]["alignment_contract"].update(
            {
                "source_reference_bbox": [20.0, 20.0, 280.0, 60.0],
                "target_reference_bbox": [20.0, 20.0, 280.0, 60.0],
                "source_text_bbox": [220.0, 24.0, 278.0, 44.0],
                "maximum_delta_pt": 1.0,
                "measurement_basis": "actual_candidate_text_span_bbox",
            }
        )
        codes = {
            item["code"]
            for item in validate_text_alignment_contract(
                segment, [0.0, 0.0, 300.0, 100.0]
            )
        }
        self.assertIn("TEXT_ALIGNMENT_RENDER_MISMATCH", codes)

    def test_single_source_foreground_color_is_preserved_in_actual_pdf_span(self) -> None:
        proof = self.root / "white-on-dark.pdf"
        doc = fitz.open()
        page = doc.new_page(width=300, height=100)
        page.draw_rect(
            fitz.Rect(20.0, 20.0, 280.0, 60.0),
            color=None,
            fill=(0.1, 0.1, 0.1),
        )
        page.insert_font(fontname="zh_regular", fontfile=str(REGULAR_FONT))
        font = fitz.Font(fontfile=str(REGULAR_FONT))
        segment = {
            "page": 1,
            "segment_id": "fixture.p0001.inverse-heading",
            "semantic_type": "heading",
            "bbox": [24.0, 24.0, 276.0, 54.0],
            "zh_TW": "反白標題",
            "font_style": {
                "source_font_size_pt": 14.0,
                "source_colors": [0xFFFFFF],
            },
            "render": {
                "action": "replace",
                "target_bbox": [24.0, 24.0, 276.0, 54.0],
                "font_size_pt": 14.0,
                "min_font_size_pt": 10.5,
                "text_color_policy": "preserve_source_exact",
            },
        }
        evidence = insert_lines(
            page,
            segment,
            font,
            "zh_regular",
            {"role": "regular", "path": str(REGULAR_FONT)},
        )
        doc.save(proof)
        doc.close()
        reopened = fitz.open(proof)
        actual = matching_rendered_spans(
            reopened[0],
            evidence["rendered_lines"][0]["text"],
            evidence["rendered_lines"][0]["bbox"],
        )
        measured = text_color_measurement(0xFFFFFF, actual)
        self.assertEqual(evidence["text_color_srgb"], 0xFFFFFF)
        self.assertEqual(measured["status"], "PASS")
        self.assertEqual(text_color_measurement(0x000000, actual)["status"], "FAIL")
        reopened.close()

    def test_ambiguous_source_colors_fail_closed_without_inspected_routing(self) -> None:
        segment = {
            "segment_id": "fixture.p0001.multicolor-heading",
            "font_style": {"source_colors": [0x111111, 0xEEEEEE]},
            "render": {"action": "replace"},
        }
        with self.assertRaisesRegex(ValueError, "multiple source text colors"):
            resolve_text_color(segment, segment["render"])
        codes = {item["code"] for item in validate_text_color_contract(segment)}
        self.assertIn("AMBIGUOUS_SOURCE_TEXT_COLOR", codes)

    def test_ordered_table_cell_phrase_contract_is_fail_closed(self) -> None:
        contract = {
            "schema": "pdf-tw-localize/table-cell-phrase/v1",
            "group_id": "fixture.pipe-size-cell",
            "table_id": "fixture.specification-table",
            "cell_bbox": [100.0, 20.0, 160.0, 80.0],
            "segment_ids": ["fixture.pipe-part-a", "fixture.pipe-part-b"],
            "source_phrase": "Rated Width",
            "target_phrase": "額定寬度",
            "source_separator": " ",
            "target_separator": "",
        }
        segments = [
            {
                "segment_id": "fixture.pipe-part-a",
                "page": 1,
                "semantic_type": "table-cell",
                "source_text": "Rated",
                "zh_TW": "額定",
                "relationships": {"table_cell_phrase": contract},
                "render": {"container_bbox": contract["cell_bbox"]},
            },
            {
                "segment_id": "fixture.pipe-part-b",
                "page": 1,
                "semantic_type": "table-cell",
                "source_text": "Width",
                "zh_TW": "寬度",
                "relationships": {"table_cell_phrase": contract},
                "render": {"container_bbox": contract["cell_bbox"]},
            },
        ]
        self.assertEqual(validate_table_cell_phrase_groups(segments), [])

        wrong_target = json.loads(json.dumps(segments))
        wrong_target[1]["zh_TW"] = "高度"
        self.assertIn(
            "TABLE_CELL_TARGET_PHRASE_MISMATCH",
            {item["code"] for item in validate_table_cell_phrase_groups(wrong_target)},
        )
        missing_member = segments[:1]
        self.assertIn(
            "TABLE_CELL_PHRASE_MEMBER_MISSING",
            {item["code"] for item in validate_table_cell_phrase_groups(missing_member)},
        )
        wrong_container = json.loads(json.dumps(segments))
        wrong_container[1]["render"]["container_bbox"] = [100.0, 20.0, 159.0, 80.0]
        self.assertIn(
            "TABLE_CELL_PHRASE_CONTAINER",
            {item["code"] for item in validate_table_cell_phrase_groups(wrong_container)},
        )

    def test_cp950_json_output_round_trips_copyright_character(self) -> None:
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="cp950", errors="strict", newline="")
        emit_json({"status": "PASS", "symbol": "©"}, stream=stream)
        stream.flush()
        encoded = raw.getvalue().decode("cp950")
        self.assertIn("\\u00a9", encoded.lower())
        self.assertEqual(json.loads(encoded)["symbol"], "©")

    def test_live_text_component_requires_text_only_zero_padding_mask(self) -> None:
        translation = self.extract_and_translate()
        manifest = json.loads(translation.read_text(encoding="utf-8"))
        segment = manifest["segments"][0]
        segment["relationships"].update(
            {
                "component_group_id": "fixture.warning-label",
                "component_relation": "member_of",
            }
        )
        segment["component_contract"] = {
            "group_id": "fixture.warning-label",
            "segment_role": "live_text",
            "mask_policy": "source_text_spans_only",
            "members": [
                {
                    "component_id": "fixture.warning-label.text",
                    "role": "live_text",
                    "policy": "replace_live_text",
                    "bbox": segment["bbox"],
                },
                {
                    "component_id": "fixture.warning-label.rule",
                    "role": "vector_rule",
                    "policy": "preserve",
                    "bbox": [38, 47, 220, 48],
                },
            ],
        }
        segment["render"].update(
            {
                "mask_mode": "remove_text_only",
                "mask_padding_pt": 0,
                "preserve_background_bbox": [38, 47, 220, 72],
            }
        )
        valid_codes = {
            item["code"]
            for item in validate_manifest(
                manifest, require_translation=True, require_render=True
            )
        }
        self.assertFalse(any(code.startswith("COMPONENT_") for code in valid_codes))
        self.assertNotIn("LIVE_TEXT_MASK_POLICY", valid_codes)

        segment["render"]["mask_mode"] = "fill"
        blocked_codes = {
            item["code"]
            for item in validate_manifest(
                manifest, require_translation=True, require_render=True
            )
        }
        self.assertIn("LIVE_TEXT_MASK_POLICY", blocked_codes)

    def test_bold_role_rejects_a_thin_variable_font_default(self) -> None:
        translation = self.extract_and_translate()
        manifest = json.loads(translation.read_text(encoding="utf-8"))
        manifest["segments"][0]["font_style"]["bold"] = True
        translation.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        result = run_script(
            "rebuild_pdf.py",
            self.source,
            "--manifest",
            translation,
            "--font",
            FONT,
            "--bold-font",
            FONT,
            "--output",
            self.root / "must-not-exist.pdf",
            "--report",
            self.root / "must-not-exist.json",
            expected=1,
        )
        self.assertIn("BOLD_FONT_ROLE_MISMATCH", result.stderr)
        self.assertFalse((self.root / "must-not-exist.pdf").exists())

    def test_background_rect_adjustment_and_real_bold_face_are_verified(self) -> None:
        self.assertTrue(REGULAR_FONT.is_file())
        self.assertTrue(BOLD_FONT.is_file())
        source = self.root / "background-source.pdf"
        gray_bbox = [35.0, 80.0, 230.0, 130.0]
        source_plate_bbox = [35.0, 48.0, 170.0, 82.0]
        doc = fitz.open()
        page = doc.new_page(width=300, height=200)
        page.draw_rect(
            fitz.Rect(gray_bbox), color=None, fill=(0.82, 0.826, 0.832), overlay=True
        )
        page.draw_rect(
            fitz.Rect(source_plate_bbox), color=None, fill=(1, 1, 1), overlay=True
        )
        page.draw_line((100, 64), (220, 64), color=(0.1, 0.1, 0.1), width=1)
        page.insert_text((45, 70), "INSTALL", fontsize=12, fontname="hebo")
        doc.save(source)
        doc.close()

        spec = self.root / "background-layout.json"
        spec.write_text(
            json.dumps(
                {
                    "schema": "pdf-tw-localize/layout-spec/v1",
                    "document_id": "fixture-background",
                    "pages": {
                        "1": {
                            "context": {
                                "purpose": "Test a live heading over a separate background plate.",
                                "heading_hierarchy": ["INSTALL"],
                                "neighboring_context": ["A gray instruction container begins below."],
                                "table_context": [],
                                "condition_pairs": [],
                                "ui_state": [],
                                "image_text_inventory_status": "NOT_APPLICABLE",
                            },
                            "segments": [
                                {
                                    "key": "heading",
                                    "semantic_type": "heading",
                                    "source_refs": [{"block": 0, "line": 0}],
                                    "reading_order": 1,
                                    "relationships": {
                                        "component_group_id": "fixture-background.p0001.heading",
                                        "component_relation": "member_of",
                                    },
                                    "component_contract": {
                                        "group_id": "fixture-background.p0001.heading",
                                        "segment_role": "live_text",
                                        "mask_policy": "source_text_spans_only",
                                        "members": [
                                            {
                                                "component_id": "fixture-background.p0001.heading.text",
                                                "role": "live_text",
                                                "policy": "replace_live_text",
                                                "bbox": [45.0, 57.16, 95.664, 73.684],
                                            },
                                            {
                                                "component_id": "fixture-background.p0001.heading.plate",
                                                "role": "background",
                                                "policy": "adjust_background",
                                                "bbox": source_plate_bbox,
                                                "adjustment_method": "rewrite_untransformed_rect",
                                                "dependent_geometry": {
                                                    "schema": "pdf-tw-localize/translation-dependent-geometry/v1",
                                                    "measurement_basis": "actual_candidate_text_span_bbox",
                                                    "bounds_policy": "within_source_bbox",
                                                    "maximum_delta_pt": 0.05,
                                                    "minimum_width_pt": 20.0,
                                                    "minimum_height_pt": 10.0,
                                                    "edge_bindings": {
                                                        "x0": {"basis": "source_bbox", "edge": "x0", "offset_pt": 0.0},
                                                        "y0": {"basis": "source_bbox", "edge": "y0", "offset_pt": 0.0},
                                                        "x1": {
                                                            "basis": "candidate_member_bbox",
                                                            "member_id": "fixture-background.p0001.heading.text",
                                                            "edge": "x1",
                                                            "offset_pt": 5.0,
                                                        },
                                                        "y1": {"basis": "source_bbox", "edge": "y1", "offset_pt": -2.5},
                                                    },
                                                },
                                                "expected_fill": [1.0, 1.0, 1.0],
                                                "avoid_regions": [
                                                    {
                                                        "region_id": "fixture-background.p0001.gray-card",
                                                        "bbox": gray_bbox,
                                                    }
                                                ],
                                            },
                                            {
                                                "component_id": "fixture-background.p0001.heading.rule",
                                                "role": "vector_rule",
                                                "policy": "preserve",
                                                "bbox": [100.0, 63.5, 220.0, 64.5],
                                            },
                                        ],
                                    },
                                    "render": {
                                        "action": "replace",
                                        "mask_mode": "remove_text_only",
                                        "mask_padding_pt": 0,
                                        "target_bbox": [45.0, 54.0, 165.0, 76.0],
                                        "font_size_pt": 12,
                                        "min_font_size_pt": 10,
                                        "preserve_background_bbox": [35.0, 48.0, 230.0, 130.0],
                                    },
                                }
                            ],
                            "ignored_source_refs": [],
                        }
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        extraction = self.root / "background-extraction.json"
        run_script(
            "extract_segments.py",
            source,
            "--pages",
            "1",
            "--document-id",
            "fixture-background",
            "--layout-spec",
            spec,
            "--output",
            extraction,
        )
        manifest = json.loads(extraction.read_text(encoding="utf-8"))
        segment = manifest["segments"][0]
        segment["zh_TW"] = "合成標題"
        segment["status"] = "TRANSLATED"
        manifest["status"] = "TRANSLATED"
        translation = self.root / "background-translation.json"
        translation.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        run_script("validate_segments.py", translation, "--stage", "render")

        proof = self.root / "background-proof.pdf"
        rebuild = self.root / "background-rebuild.json"
        run_script(
            "rebuild_pdf.py",
            source,
            "--manifest",
            translation,
            "--font",
            REGULAR_FONT,
            "--bold-font",
            BOLD_FONT,
            "--output",
            proof,
            "--report",
            rebuild,
        )
        rebuild_payload = json.loads(rebuild.read_text(encoding="utf-8"))
        self.assertEqual(rebuild_payload["font_role_validation"]["status"], "PASS")
        self.assertTrue(
            rebuild_payload["font_role_validation"]["roles"]["bold"]["is_bold"]
        )
        adjustment = rebuild_payload["background_adjustments"][0]
        self.assertEqual(adjustment["status"], "APPLIED_VERIFIED")
        self.assertEqual(adjustment["source_rect_count_after"], 0)
        self.assertEqual(adjustment["target_rect_count_after"], 1)
        self.assertEqual(
            adjustment["avoid_regions"][0]["target_intersection_area"], 0.0
        )
        rendered_text_x1 = rebuild_payload["segments"][0]["rendered_lines"][0]["bbox"][2]
        self.assertAlmostEqual(
            adjustment["target_bbox"][2], rendered_text_x1 + 5.0, places=5
        )
        self.assertEqual(
            adjustment["dependent_geometry_resolution"]["measurement_basis"],
            "actual_candidate_text_span_bbox",
        )

        allowlist = self.root / "background-allowlist.json"
        allowlist.write_text(
            json.dumps(
                {
                    "schema": "pdf-tw-localize/english-allowlist/v1",
                    "allowed": [],
                }
            ),
            encoding="utf-8",
        )
        rebuilt_qa = self.root / "background-rebuilt-qa.json"
        run_script(
            "qa_rebuilt_pdf.py",
            "--source",
            source,
            "--candidate",
            proof,
            "--manifest",
            translation,
            "--rebuild-report",
            rebuild,
            "--allowlist",
            allowlist,
            "--output",
            rebuilt_qa,
        )
        rebuilt_qa_payload = json.loads(rebuilt_qa.read_text(encoding="utf-8"))
        self.assertEqual(rebuilt_qa_payload["machine_qa"], "MACHINE_QA_PASS")
        bold_spans = [
            span
            for role in rebuilt_qa_payload["rendered_font_roles"]
            for span in role["actual_spans"]
        ]
        self.assertTrue(bold_spans)
        self.assertTrue(all(span["is_bold"] for span in bold_spans))

        visual_qa = self.root / "background-visual-qa.json"
        run_script(
            "qa_preserved_visuals.py",
            "--source",
            source,
            "--candidate",
            proof,
            "--manifest",
            translation,
            "--rebuild-report",
            rebuild,
            "--allowlist",
            allowlist,
            "--dpi",
            "300",
            "--output",
            visual_qa,
        )
        visual_payload = json.loads(visual_qa.read_text(encoding="utf-8"))
        self.assertEqual(visual_payload["machine_qa"], "MACHINE_QA_PASS")
        adjusted_component = next(
            item
            for item in visual_payload["component_preservation"]
            if item["component_id"] == "fixture-background.p0001.heading.plate"
        )
        self.assertTrue(adjusted_component["match"])
        rule_component = next(
            item
            for item in visual_payload["component_preservation"]
            if item["component_id"] == "fixture-background.p0001.heading.rule"
        )
        self.assertEqual(
            rule_component["identity_method"], "vector_rule_stroke_signatures"
        )
        self.assertTrue(rule_component["match"])

    def test_vector_outlined_text_cannot_use_partial_replacement_policy(self) -> None:
        translation = self.extract_and_translate()
        manifest = json.loads(translation.read_text(encoding="utf-8"))
        segment = manifest["segments"][0]
        segment["relationships"].update(
            {
                "component_group_id": "fixture.vector-badge",
                "component_relation": "member_of",
            }
        )
        segment["component_contract"] = {
            "group_id": "fixture.vector-badge",
            "segment_role": "live_text",
            "mask_policy": "source_text_spans_only",
            "members": [
                {
                    "component_id": "fixture.vector-badge.outlined-word",
                    "role": "vector_outlined_text",
                    "policy": "replace_live_text",
                    "bbox": segment["bbox"],
                }
            ],
        }
        segment["render"].update(
            {"mask_mode": "remove_text_only", "mask_padding_pt": 0}
        )
        codes = {
            item["code"]
            for item in validate_manifest(
                manifest, require_translation=True, require_render=True
            )
        }
        self.assertIn("VECTOR_COMPONENT_PARTIAL_MASK", codes)
        self.assertIn("NON_TEXT_COMPONENT_REPLACEMENT", codes)

    def test_rebuild_preserves_catalog_output_intent_and_decoded_icc(self) -> None:
        from pypdf import PdfReader, PdfWriter
        from pypdf.generic import (
            ArrayObject,
            DecodedStreamObject,
            DictionaryObject,
            NameObject,
            NumberObject,
            TextStringObject,
        )

        source_reader = PdfReader(str(self.source))
        writer = PdfWriter()
        writer.clone_document_from_reader(source_reader)
        profile = DecodedStreamObject()
        profile.set_data(b"fixture-icc-profile-copyright-\xa9")
        profile[NameObject("/N")] = NumberObject(3)
        profile_ref = writer._add_object(profile)
        intent = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/OutputIntent"),
                NameObject("/S"): NameObject("/GTS_PDFX"),
                NameObject("/OutputConditionIdentifier"): TextStringObject(
                    "Fixture RGB"
                ),
                NameObject("/Info"): TextStringObject("Fixture © profile"),
                NameObject("/DestOutputProfile"): profile_ref,
            }
        )
        intent_ref = writer._add_object(intent)
        writer.root_object[NameObject("/OutputIntents")] = ArrayObject([intent_ref])
        rewritten = self.root / "source-with-output-intent.pdf"
        with rewritten.open("xb") as stream:
            writer.write(stream)
        self.source = rewritten

        translation = self.extract_and_translate()
        proof = self.root / "output-intent-proof.pdf"
        rebuild = self.root / "output-intent-rebuild.json"
        run_script(
            "rebuild_pdf.py",
            self.source,
            "--manifest",
            translation,
            "--font",
            FONT,
            "--output",
            proof,
            "--report",
            rebuild,
        )
        source_evidence = catalog_color_evidence(self.source)
        candidate_evidence = catalog_color_evidence(proof)
        self.assertEqual(source_evidence, candidate_evidence)
        self.assertTrue(source_evidence["catalog_output_intents_present"])
        self.assertEqual(source_evidence["output_intent_count"], 1)
        rebuild_payload = json.loads(rebuild.read_text(encoding="utf-8"))
        self.assertTrue(
            rebuild_payload["catalog_color_management"]["output_intents_exact"]
        )

    def test_preserved_visual_identity_and_no_overlay(self) -> None:
        image_path = self.root / "controller.png"
        image = Image.new("RGB", (120, 60), "white")
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((1, 1, 118, 58), radius=7, outline="black", width=2)
        draw.text((12, 21), "Menu", fill="black")
        image.save(image_path)

        visual_source = self.root / "visual-source.pdf"
        doc = fitz.open()
        page = doc.new_page(width=300, height=200)
        page.insert_text((40, 52), "GUIDE", fontsize=10, fontname="helv")
        visual_bbox = [40.0, 80.0, 160.0, 140.0]
        page.insert_image(fitz.Rect(visual_bbox), filename=str(image_path))
        doc.save(visual_source)
        doc.close()

        doc = fitz.open(visual_source)
        page = doc[0]
        info = next(
            item
            for item in page.get_image_info(hashes=True, xrefs=True)
            if all(abs(float(left) - float(right)) <= 0.01 for left, right in zip(item["bbox"], visual_bbox))
        )
        pix = fitz.Pixmap(doc, int(info["xref"]))
        decoded_sha256 = hashlib.sha256(pix.samples).hexdigest().upper()
        layer = {
            "role": "base",
            "source_xref": int(info["xref"]),
            "pixel_width": pix.width,
            "pixel_height": pix.height,
            "channels": pix.n,
            "alpha": bool(pix.alpha),
            "decoded_samples_sha256": decoded_sha256,
        }
        doc.close()

        visual_spec = self.root / "visual-layout.json"
        visual_spec.write_text(
            json.dumps(
                {
                    "schema": "pdf-tw-localize/layout-spec/v1",
                    "document_id": "fixture",
                    "pages": {
                        "1": {
                            "context": {
                                "purpose": "Preserve a clear non-safety-critical source UI screenshot.",
                                "heading_hierarchy": [],
                                "neighboring_context": ["The adjacent guidance names the source UI label."],
                                "table_context": [],
                                "condition_pairs": [],
                                "ui_state": ["Menu"],
                                "image_text_inventory_status": "NOT_APPLICABLE_USER_PERMITTED_SOURCE_UI_WITH_TEXTUAL_GUIDANCE",
                                "preserved_visuals": [
                                    {
                                        "visual_id": "fixture.p0001.controller",
                                        "policy": "preserve_source_visual_with_textual_guidance",
                                        "bbox": visual_bbox,
                                        "pixel_width": pix.width,
                                        "pixel_height": pix.height,
                                        "decoded_image_sha256": decoded_sha256,
                                        "image_layers": [layer],
                                        "allowed_ui_english": [
                                            {
                                                "source_text": "Menu",
                                                "zh_TW": "選單",
                                                "guidance_required": True,
                                                "guidance_segment_ids": ["fixture.p0001.guidance"],
                                            }
                                        ],
                                    }
                                ],
                            },
                            "segments": [
                                {
                                    "key": "guidance",
                                    "semantic_type": "paragraph",
                                    "source_refs": [{"block": 0, "line": 0}],
                                    "reading_order": 1,
                                    "render": {
                                        "action": "replace",
                                        "target_bbox": [40, 38, 240, 65],
                                        "font_size_pt": 10,
                                        "min_font_size_pt": 8,
                                    },
                                },
                                {
                                    "key": "ui-menu",
                                    "semantic_type": "image-text",
                                    "source_text": "Menu",
                                    "bbox": [52, 101, 80, 119],
                                    "reading_order": 2,
                                    "extraction_method": "visual_annotation",
                                    "source_style": {
                                        "primary_font": "RASTER_UI",
                                        "source_font_size_pt": 9,
                                        "bold": False,
                                        "italic": False,
                                    },
                                    "relationships": {"visual_id": "fixture.p0001.controller"},
                                    "render": {
                                        "action": "preserve_source_visual_with_textual_guidance",
                                        "target_bbox": [52, 101, 80, 119],
                                        "guidance_segment_ids": ["fixture.p0001.guidance"],
                                    },
                                },
                            ],
                            "ignored_source_refs": [],
                        }
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        extraction = self.root / "visual-extraction.json"
        run_script(
            "extract_segments.py",
            visual_source,
            "--pages",
            "1",
            "--document-id",
            "fixture",
            "--layout-spec",
            visual_spec,
            "--output",
            extraction,
        )
        manifest = json.loads(extraction.read_text(encoding="utf-8"))
        translations = {
            "fixture.p0001.guidance": "選單（Menu）",
            "fixture.p0001.ui-menu": "選單",
        }
        for segment in manifest["segments"]:
            segment["zh_TW"] = translations[segment["segment_id"]]
            segment["status"] = "TRANSLATED"
        manifest["status"] = "TRANSLATED"
        translation = self.root / "visual-translation.json"
        translation.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        run_script("validate_segments.py", translation, "--stage", "render")

        proof = self.root / "visual-proof.pdf"
        rebuild = self.root / "visual-rebuild.json"
        run_script(
            "rebuild_pdf.py",
            visual_source,
            "--manifest",
            translation,
            "--font",
            FONT,
            "--output",
            proof,
            "--report",
            rebuild,
        )
        rebuild_payload = json.loads(rebuild.read_text(encoding="utf-8"))
        preserved = next(
            item for item in rebuild_payload["segments"] if item["segment_id"] == "fixture.p0001.ui-menu"
        )
        self.assertEqual(preserved["fit_status"], "PRESERVED_SOURCE_VISUAL")
        self.assertNotIn("mask_bbox", preserved)
        self.assertNotIn("rendered_lines", preserved)

        allowlist = self.root / "visual-allowlist.json"
        allowlist.write_text(
            json.dumps(
                {
                    "schema": "pdf-tw-localize/english-allowlist/v2",
                    "allowed": [],
                    "allowed_ui_english": [
                        {
                            "source_text": "Menu",
                            "zh_TW": "選單",
                            "visual_ids": ["fixture.p0001.controller"],
                            "guidance_required": True,
                            "guidance_segment_ids": ["fixture.p0001.guidance"],
                        }
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        qa = self.root / "visual-qa.json"
        run_script(
            "qa_preserved_visuals.py",
            "--source",
            visual_source,
            "--candidate",
            proof,
            "--manifest",
            translation,
            "--rebuild-report",
            rebuild,
            "--allowlist",
            allowlist,
            "--dpi",
            "300",
            "--output",
            qa,
        )
        qa_payload = json.loads(qa.read_text(encoding="utf-8"))
        self.assertEqual(qa_payload["machine_qa"], "MACHINE_QA_PASS")
        self.assertTrue(qa_payload["visuals"][0]["decoded_layers_match"])
        self.assertTrue(qa_payload["visuals"][0]["rendered_region_match"])
        self.assertEqual(qa_payload["visuals"][0]["rebuild_intersections"], [])


if __name__ == "__main__":
    unittest.main()
