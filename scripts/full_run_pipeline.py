#!/usr/bin/env python3
"""Plan and resume full-document translation without weakening final QA."""

from __future__ import annotations

import argparse
from pathlib import Path

from _console import emit_json
from _full_run import (
    FullRunError,
    create_plan,
    merge_results,
    record_stage_timing,
    status_report,
    write_json_new,
    write_plan_bundle,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create context-safe translation batches, validate resumable batch results, "
            "merge an exact translation import, and record stage timing."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan", help="Create a hash-bound full-run plan and batch requests.")
    plan.add_argument("manifest", type=Path)
    plan.add_argument("--validation-report", required=True, type=Path)
    plan.add_argument("--output", required=True, type=Path)
    plan.add_argument("--requests-dir", required=True, type=Path)
    plan.add_argument("--glossary", action="append", default=[], type=Path)
    plan.add_argument("--dependency-spec", type=Path)
    plan.add_argument("--target-segments", type=int, default=80)
    plan.add_argument("--max-batch-pages", type=int, default=8)
    plan.add_argument("--context-pages", type=int, default=1)
    plan.add_argument("--max-parallel-batches", type=int, default=4)

    status = subparsers.add_parser("status", help="Validate result checkpoints and report resumable work.")
    status.add_argument("plan", type=Path)
    status.add_argument("--requests-dir", required=True, type=Path)
    status.add_argument("--results-dir", required=True, type=Path)

    merge = subparsers.add_parser("merge", help="Merge complete valid batch results into translation-import/v1.")
    merge.add_argument("plan", type=Path)
    merge.add_argument("--requests-dir", required=True, type=Path)
    merge.add_argument("--results-dir", required=True, type=Path)
    merge.add_argument("--output", required=True, type=Path)

    timing = subparsers.add_parser("record-stage", help="Append one plan-bound stage timing record atomically.")
    timing.add_argument("plan", type=Path)
    timing.add_argument("--ledger", required=True, type=Path)
    timing.add_argument("--stage", required=True)
    timing.add_argument("--attempt-id", required=True)
    timing.add_argument("--started-at", required=True)
    timing.add_argument("--completed-at", required=True)
    timing.add_argument("--status", required=True)
    timing.add_argument("--note", default="")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "plan":
            plan, requests = create_plan(
                args.manifest,
                validation_report_path=args.validation_report,
                glossary_paths=args.glossary,
                dependency_spec_path=args.dependency_spec,
                target_segments=args.target_segments,
                max_batch_pages=args.max_batch_pages,
                context_pages=args.context_pages,
                max_parallel_batches=args.max_parallel_batches,
            )
            report = write_plan_bundle(args.output, args.requests_dir, plan, requests)
        elif args.command == "status":
            report = status_report(args.plan, args.requests_dir, args.results_dir)
        elif args.command == "merge":
            payload = merge_results(args.plan, args.requests_dir, args.results_dir)
            write_json_new(args.output.resolve(), payload)
            report = {
                "schema": "pdf-tw-localize/full-run-merge-write/v1",
                "status": "MERGED",
                "output": str(args.output.resolve()),
                "segment_count": len(payload["translations"]),
                "machine_qa": "NOT_CHECKED",
                "visual_review": "NOT_CHECKED",
                "user_acceptance": "NOT_CHECKED",
            }
        elif args.command == "record-stage":
            report = record_stage_timing(
                args.plan,
                args.ledger,
                stage=args.stage,
                attempt_id=args.attempt_id,
                started_at_utc=args.started_at,
                completed_at_utc=args.completed_at,
                status=args.status,
                note=args.note,
            )
        else:  # pragma: no cover - argparse enforces the command set.
            raise FullRunError(f"Unsupported command: {args.command}")
    except (FullRunError, FileExistsError, OSError, ValueError) as exc:
        emit_json(
            {
                "schema": "pdf-tw-localize/full-run-error/v1",
                "status": "BLOCKED",
                "error": str(exc),
                "machine_qa": "NOT_CHECKED",
                "visual_review": "NOT_CHECKED",
                "user_acceptance": "NOT_CHECKED",
            }
        )
        return 2
    emit_json(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
