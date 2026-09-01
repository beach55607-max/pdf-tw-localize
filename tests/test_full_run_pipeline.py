#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from _full_run import (  # noqa: E402
    BATCH_RESULT_SCHEMA,
    FullRunError,
    create_plan,
    merge_results,
    record_stage_timing,
    status_report,
    write_plan_bundle,
)
from _segment_common import SCHEMA as SEGMENT_MANIFEST_SCHEMA  # noqa: E402
from _segment_common import sha256_file  # noqa: E402


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


class FullRunPipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.manifest_path = self.root / "segments.json"
        self.glossary_path = self.root / "glossary.csv"
        self.glossary_path.write_text(
            "source,zh_TW\nMODE,模式\n",
            encoding="utf-8",
        )
        write_json(self.manifest_path, self._manifest())
        self.validation_path = self.root / "extraction-validation.json"
        write_json(
            self.validation_path,
            {
                "schema": "pdf-tw-localize/segment-validation/v1",
                "stage": "extraction",
                "manifest_sha256": sha256_file(self.manifest_path),
                "status": "PASS",
                "blocking_issue_count": 0,
                "needs_review_issue_count": 0,
                "user_acceptance": "NOT_CHECKED",
            },
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _manifest() -> dict[str, object]:
        page_contexts: list[dict[str, object]] = []
        for page in range(1, 5):
            page_contexts.append(
                {
                    "page": page,
                    "context_id": f"ctx-{page}",
                    "purpose": f"Fixture page {page}",
                    "heading_hierarchy": [],
                    "neighboring_context": [],
                    "table_context": [],
                    "condition_pairs": [],
                    "ui_state": [],
                    "image_text_inventory_status": "NOT_APPLICABLE",
                    "document_context_refs": (
                        [
                            {
                                "context_ref_id": "continued-procedure",
                                "page": 2,
                                "relationship": "continues_on",
                            }
                        ]
                        if page == 1
                        else []
                    ),
                }
            )
        return {
            "schema": SEGMENT_MANIFEST_SCHEMA,
            "status": "EXTRACTED",
            "document_id": "full-run-fixture",
            "source": {
                "path": "fixture-source.pdf",
                "sha256": "A" * 64,
            },
            "selected_pages": [1, 2, 3, 4],
            "translation_contract": {
                "source_locale": "en",
                "target_locale": "zh-TW",
                "source_fidelity_required": True,
            },
            "page_contexts": page_contexts,
            "segments": [
                {
                    "segment_id": "fixture.p0001.mode",
                    "page": 1,
                    "reading_order": 1,
                    "semantic_type": "paragraph",
                    "source_text": "Press MODE.",
                    "protected_tokens": [
                        {
                            "token": "MODE",
                            "target_token": "模式",
                            "kind": "explicit",
                            "source_count": 1,
                        }
                    ],
                    "relationships": {},
                    "semantic_bindings": [
                        {"binding_id": "warning-condition"}
                    ],
                    "render": {"action": "replace"},
                },
                {
                    "segment_id": "fixture.p0002.step",
                    "page": 2,
                    "reading_order": 1,
                    "semantic_type": "paragraph",
                    "source_text": "Continue the procedure.",
                    "protected_tokens": [],
                    "relationships": {},
                    "semantic_bindings": [],
                    "render": {"action": "replace"},
                },
                {
                    "segment_id": "fixture.p0003.warning",
                    "page": 3,
                    "reading_order": 1,
                    "semantic_type": "warning",
                    "source_text": "Do not disconnect power.",
                    "protected_tokens": [],
                    "relationships": {},
                    "semantic_bindings": [],
                    "render": {"action": "replace"},
                },
                {
                    "segment_id": "fixture.p0004.note",
                    "page": 4,
                    "reading_order": 1,
                    "semantic_type": "paragraph",
                    "source_text": "Operation is complete.",
                    "protected_tokens": [],
                    "relationships": {},
                    "semantic_bindings": [],
                    "render": {"action": "replace"},
                },
            ],
        }

    def _create_bundle(self) -> tuple[Path, Path, dict[str, object]]:
        plan, requests = create_plan(
            self.manifest_path,
            validation_report_path=self.validation_path,
            glossary_paths=[self.glossary_path],
            target_segments=1,
            max_batch_pages=2,
            context_pages=1,
            max_parallel_batches=2,
        )
        plan_path = self.root / "run-plan.json"
        requests_dir = self.root / "requests"
        write_plan_bundle(plan_path, requests_dir, plan, requests)
        return plan_path, requests_dir, plan

    @staticmethod
    def _result_for(
        plan_path: Path,
        requests_dir: Path,
        plan: dict[str, object],
        batch: dict[str, object],
        *,
        elapsed: float = 2.0,
    ) -> dict[str, object]:
        records = batch["segment_records"]
        translations: list[dict[str, object]] = []
        for segment_id in batch["owned_segment_ids"]:
            record = records[segment_id]
            tokens = [
                str(item["target_token"])
                for item in record["protected_token_requirements"]
                for _ in range(int(item["required_count"]))
            ]
            zh_tw = "已翻譯 " + str(segment_id)
            if tokens:
                zh_tw += " " + " ".join(str(token) for token in tokens)
            translations.append(
                {
                    "segment_id": segment_id,
                    "source_segment_sha256": record["source_segment_sha256"],
                    "zh_TW": zh_tw,
                    "note": "",
                    "translation_assertions": [
                        {"binding_id": binding_id}
                        for binding_id in record["semantic_binding_ids"]
                    ],
                }
            )
        return {
            "schema": BATCH_RESULT_SCHEMA,
            "status": "TRANSLATED",
            "plan_sha256": sha256_file(plan_path),
            "plan_digest": plan["plan_digest"],
            "source_manifest_sha256": plan["source_manifest"]["sha256"],
            "batch_id": batch["batch_id"],
            "batch_digest": batch["batch_digest"],
            "request_sha256": sha256_file(
                requests_dir / batch["request_filename"]
            ),
            "translator": "fixture translator",
            "translated_at_utc": "2026-09-01T00:00:02+00:00",
            "translations": translations,
            "timing": {
                "started_at_utc": "2026-09-01T00:00:00+00:00",
                "completed_at_utc": "2026-09-01T00:00:02+00:00",
                "elapsed_seconds": elapsed,
            },
        }

    def _write_results(
        self,
        plan_path: Path,
        requests_dir: Path,
        plan: dict[str, object],
        *,
        batch_count: int | None = None,
    ) -> Path:
        results_dir = self.root / "results"
        results_dir.mkdir()
        batches = plan["batches"]
        limit = len(batches) if batch_count is None else batch_count
        for index, batch in enumerate(batches[:limit], start=1):
            result = self._result_for(
                plan_path,
                requests_dir,
                plan,
                batch,
                elapsed=float(index),
            )
            write_json(results_dir / batch["result_filename"], result)
        return results_dir

    def test_plan_is_deterministic_and_never_splits_dependencies(self) -> None:
        first, first_requests = create_plan(
            self.manifest_path,
            validation_report_path=self.validation_path,
            glossary_paths=[self.glossary_path],
            target_segments=1,
            max_batch_pages=1,
            context_pages=1,
            max_parallel_batches=2,
        )
        second, second_requests = create_plan(
            self.manifest_path,
            validation_report_path=self.validation_path,
            glossary_paths=[self.glossary_path],
            target_segments=1,
            max_batch_pages=1,
            context_pages=1,
            max_parallel_batches=2,
        )
        self.assertEqual(first["plan_digest"], second["plan_digest"])
        self.assertEqual(first_requests, second_requests)

        owner_by_page: dict[int, str] = {}
        for batch in first["batches"]:
            for page in batch["owned_pages"]:
                owner_by_page[page] = batch["batch_id"]
        self.assertEqual(owner_by_page[1], owner_by_page[2])
        dependency_batch = next(
            batch for batch in first["batches"] if 1 in batch["owned_pages"]
        )
        self.assertTrue(dependency_batch["dependency_group_exceeds_soft_limit"])
        self.assertIn(3, dependency_batch["context_only_pages"])

        high_per_wave: dict[int, int] = {}
        for batch in first["batches"]:
            if batch["risk"] == "high_context":
                wave = int(batch["parallel_wave"])
                high_per_wave[wave] = high_per_wave.get(wave, 0) + 1
        self.assertTrue(all(count <= 1 for count in high_per_wave.values()))

        for request in first_requests.values():
            self.assertTrue(
                all(segment.get("source_segment_sha256") for segment in request["segments"])
            )

    def test_stale_extraction_validation_blocks_planning(self) -> None:
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        manifest["document_context"] = {"purpose": "changed after validation"}
        write_json(self.manifest_path, manifest)
        with self.assertRaisesRegex(FullRunError, "validation report is stale"):
            create_plan(
                self.manifest_path,
                validation_report_path=self.validation_path,
                glossary_paths=[self.glossary_path],
            )

    def test_status_resumes_only_hash_bound_complete_batches(self) -> None:
        plan_path, requests_dir, plan = self._create_bundle()
        results_dir = self._write_results(
            plan_path, requests_dir, plan, batch_count=1
        )
        report = status_report(plan_path, requests_dir, results_dir)
        self.assertEqual(report["status"], "IN_PROGRESS")
        self.assertEqual(report["completed_count"], 1)
        self.assertEqual(report["pending_count"], len(plan["batches"]) - 1)
        self.assertEqual(report["blocked_count"], 0)
        self.assertEqual(report["machine_qa"], "NOT_CHECKED")
        self.assertEqual(report["visual_review"], "NOT_CHECKED")
        self.assertEqual(report["user_acceptance"], "NOT_CHECKED")

    def test_stale_or_tampered_artifacts_are_blocked(self) -> None:
        plan_path, requests_dir, plan = self._create_bundle()
        results_dir = self._write_results(
            plan_path, requests_dir, plan, batch_count=1
        )
        result_path = results_dir / plan["batches"][0]["result_filename"]
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result["batch_digest"] = "0" * 64
        write_json(result_path.with_suffix(".replacement"), result)
        result_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        report = status_report(plan_path, requests_dir, results_dir)
        self.assertEqual(report["status"], "BLOCKED")
        self.assertEqual(report["blocked_count"], 1)

        tampered_plan = json.loads(plan_path.read_text(encoding="utf-8"))
        tampered_plan["configuration"]["max_parallel_batches"] = 99
        plan_path.write_text(
            json.dumps(tampered_plan, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(FullRunError, "plan digest mismatch"):
            status_report(plan_path, requests_dir, results_dir)

    def test_modified_batch_request_is_blocked_before_resume(self) -> None:
        plan_path, requests_dir, plan = self._create_bundle()
        results_dir = self._write_results(
            plan_path, requests_dir, plan, batch_count=1
        )
        batch = plan["batches"][0]
        request_path = requests_dir / batch["request_filename"]
        request = json.loads(request_path.read_text(encoding="utf-8"))
        request["document_context"] = {"purpose": "tampered request"}
        write_json(request_path, request)
        report = status_report(plan_path, requests_dir, results_dir)
        self.assertEqual(report["status"], "BLOCKED")
        self.assertIn("request content digest mismatch", report["blocked"][0]["error"])

    def test_missing_duplicate_and_protected_token_fail_closed(self) -> None:
        plan_path, requests_dir, plan = self._create_bundle()
        batch = plan["batches"][0]
        result = self._result_for(plan_path, requests_dir, plan, batch)

        missing = json.loads(json.dumps(result))
        missing["translations"] = missing["translations"][:-1]
        results_dir = self.root / "missing-results"
        results_dir.mkdir()
        write_json(results_dir / batch["result_filename"], missing)
        report = status_report(plan_path, requests_dir, results_dir)
        self.assertEqual(report["status"], "BLOCKED")
        self.assertIn("coverage mismatch", report["blocked"][0]["error"])

        duplicate = json.loads(json.dumps(result))
        duplicate["translations"].append(duplicate["translations"][0])
        duplicate_dir = self.root / "duplicate-results"
        duplicate_dir.mkdir()
        write_json(duplicate_dir / batch["result_filename"], duplicate)
        report = status_report(plan_path, requests_dir, duplicate_dir)
        self.assertIn("duplicate translation IDs", report["blocked"][0]["error"])

        protected = json.loads(json.dumps(result))
        protected["translations"][0]["zh_TW"] = "只保留翻譯文字"
        protected_dir = self.root / "protected-results"
        protected_dir.mkdir()
        write_json(protected_dir / batch["result_filename"], protected)
        report = status_report(plan_path, requests_dir, protected_dir)
        self.assertIn("protected token missing", report["blocked"][0]["error"])

        assertion = json.loads(json.dumps(result))
        assertion["translations"][0]["translation_assertions"] = []
        assertion_dir = self.root / "assertion-results"
        assertion_dir.mkdir()
        write_json(assertion_dir / batch["result_filename"], assertion)
        report = status_report(plan_path, requests_dir, assertion_dir)
        self.assertIn("assertion coverage mismatch", report["blocked"][0]["error"])

    def test_merge_preserves_manifest_order_and_never_caches_qa(self) -> None:
        plan_path, requests_dir, plan = self._create_bundle()
        results_dir = self._write_results(plan_path, requests_dir, plan)
        report = status_report(plan_path, requests_dir, results_dir)
        self.assertEqual(report["status"], "COMPLETE")
        self.assertEqual(report["pending_count"], 0)
        self.assertGreater(
            report["timing"]["translation_worker_seconds_observed"],
            report["timing"]["translation_parallel_wall_seconds_observed"],
        )

        merged = merge_results(plan_path, requests_dir, results_dir)
        self.assertEqual(
            [entry["segment_id"] for entry in merged["translations"]],
            plan["segment_order"],
        )
        self.assertEqual(merged["full_run"]["coverage_validation"], "PASS")
        self.assertFalse(merged["full_run"]["cached_qa_pass_used"])
        self.assertEqual(merged["full_run"]["user_acceptance"], "NOT_CHECKED")

    def test_stage_timing_is_plan_bound_and_rejects_duplicate_attempts(self) -> None:
        plan_path, _, _ = self._create_bundle()
        ledger_path = self.root / "timing.json"
        ledger = record_stage_timing(
            plan_path,
            ledger_path,
            stage="translate",
            attempt_id="translate-1",
            started_at_utc="2026-09-01T00:00:00+00:00",
            completed_at_utc="2026-09-01T00:01:30+00:00",
            status="COMPLETED",
            note="fixture",
        )
        self.assertEqual(ledger["total_recorded_seconds"], 90.0)
        self.assertEqual(ledger["user_acceptance"], "NOT_CHECKED")
        with self.assertRaisesRegex(FullRunError, "Duplicate timing attempt_id"):
            record_stage_timing(
                plan_path,
                ledger_path,
                stage="translate",
                attempt_id="translate-1",
                started_at_utc="2026-09-01T00:02:00+00:00",
                completed_at_utc="2026-09-01T00:03:00+00:00",
                status="COMPLETED",
            )

    def test_cli_plan_status_and_merge_round_trip(self) -> None:
        plan_path = self.root / "cli-plan.json"
        requests_dir = self.root / "cli-requests"
        env = {
            **os.environ,
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        command = [
            sys.executable,
            str(SCRIPTS / "full_run_pipeline.py"),
            "plan",
            str(self.manifest_path),
            "--output",
            str(plan_path),
            "--validation-report",
            str(self.validation_path),
            "--requests-dir",
            str(requests_dir),
            "--glossary",
            str(self.glossary_path),
            "--target-segments",
            "1",
            "--max-batch-pages",
            "2",
        ]
        created = subprocess.run(
            command,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
            env=env,
        )
        self.assertEqual(created.returncode, 0, created.stdout + created.stderr)
        created_report = json.loads(created.stdout)
        self.assertEqual(created_report["status"], "PLANNED")

        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        results_dir = self._write_results(plan_path, requests_dir, plan)
        merged_path = self.root / "translations.json"
        merged = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "full_run_pipeline.py"),
                "merge",
                str(plan_path),
                "--results-dir",
                str(results_dir),
                "--requests-dir",
                str(requests_dir),
                "--output",
                str(merged_path),
            ],
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
            env=env,
        )
        self.assertEqual(merged.returncode, 0, merged.stdout + merged.stderr)
        merged_report = json.loads(merged.stdout)
        self.assertEqual(merged_report["status"], "MERGED")
        self.assertEqual(merged_report["machine_qa"], "NOT_CHECKED")
        self.assertTrue(merged_path.exists())


if __name__ == "__main__":
    unittest.main()
