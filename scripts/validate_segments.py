#!/usr/bin/env python3
"""Fail-closed validation for extraction and translation manifests."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from _console import emit_json
from _segment_common import read_json, sha256_file, validate_manifest, write_json


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a stable-ID segment manifest.")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--stage", choices=("extraction", "translation", "render"), default="extraction")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    manifest_path = args.manifest.resolve()
    manifest = read_json(manifest_path)
    issues = validate_manifest(
        manifest,
        require_translation=args.stage in {"translation", "render"},
        require_render=args.stage == "render",
    )

    source_path = Path((manifest.get("source") or {}).get("path", ""))
    if not source_path.is_file():
        issues.append(
            {
                "severity": "BLOCKING",
                "code": "SOURCE_MISSING",
                "message": f"Source file does not exist: {source_path}",
            }
        )
    else:
        actual_hash = sha256_file(source_path)
        expected_hash = (manifest.get("source") or {}).get("sha256")
        if actual_hash != expected_hash:
            issues.append(
                {
                    "severity": "BLOCKING",
                    "code": "SOURCE_HASH_MISMATCH",
                    "message": "Source SHA-256 no longer matches the manifest",
                    "evidence": {"expected": expected_hash, "actual": actual_hash},
                }
            )

    blocking = [item for item in issues if item.get("severity") == "BLOCKING"]
    needs_review = [item for item in issues if item.get("severity") == "NEEDS_REVIEW"]
    semantic_codes = {
        "SEMANTIC_BINDINGS_TYPE",
        "SEMANTIC_BINDING_TYPE",
        "SEMANTIC_BINDING_ID",
        "DUPLICATE_SEMANTIC_BINDING",
        "SEMANTIC_BINDING_FIELDS",
        "SEMANTIC_SOURCE_TOKENS",
        "SEMANTIC_SOURCE_TOKEN_MISSING",
        "SEMANTIC_SOURCE_TOKEN_NOT_PROTECTED",
        "SEMANTIC_TARGET_CUES",
        "SEMANTIC_TARGET_CUE",
        "CLARIFICATION_POLICY_INVALID",
        "CLARIFICATION_BINDING_FIELDS",
        "CLARIFICATION_TARGET_CUES",
        "CLARIFICATION_SOURCE_NOT_VERIFIED",
        "SEMANTIC_CONTEXT_REF_IDS",
        "DOCUMENT_CONTEXT_SOURCE_NOT_CHECKED",
        "DOCUMENT_CONTEXT_REF_MISMATCH",
        "CROSS_PAGE_CONTEXT_NOT_CHECKED",
        "TRANSLATION_ASSERTIONS_TYPE",
        "DUPLICATE_TRANSLATION_ASSERTION",
        "SEMANTIC_BINDING_MISSING",
        "SEMANTIC_ASSERTION_UNKNOWN_BINDING",
        "VALUE_ROLE_SWAPPED",
        "MODE_CONTEXT_DROPPED",
        "CONDITION_SCOPE_DROPPED",
        "COMPARISON_LOGIC_DROPPED",
        "CONSEQUENCE_DROPPED",
        "CLARIFICATION_POLICY_DROPPED",
        "SEMANTIC_TARGET_PHRASE_NOT_FOUND",
        "SEMANTIC_TARGET_CUE_DROPPED",
    }
    semantic_issues = [item for item in issues if item.get("code") in semantic_codes]
    semantic_binding_count = sum(
        len(segment.get("semantic_bindings") or []) for segment in manifest.get("segments") or []
    )
    if not semantic_binding_count:
        semantic_qa = "NOT_APPLICABLE"
    elif any(item.get("severity") == "BLOCKING" for item in semantic_issues):
        semantic_qa = "BLOCKED"
    elif semantic_issues:
        semantic_qa = "NEEDS_REVIEW"
    elif args.stage == "extraction":
        semantic_qa = "SOURCE_BINDINGS_VERIFIED"
    else:
        semantic_qa = "SEMANTIC_QA_PASS"
    if blocking:
        status = "BLOCKED"
    elif needs_review:
        status = "NEEDS_REVIEW"
    else:
        status = "PASS"
    report = {
        "schema": "pdf-tw-localize/segment-validation/v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "manifest_path": str(manifest_path),
        "manifest_sha256": sha256_file(manifest_path),
        "stage": args.stage,
        "segment_count": len(manifest.get("segments") or []),
        "mapping_count": len(manifest.get("mappings") or []),
        "coverage": manifest.get("coverage"),
        "issue_count": len(issues),
        "blocking_issue_count": len(blocking),
        "needs_review_issue_count": len(needs_review),
        "issues": issues,
        "status": status,
        "semantic_binding_count": semantic_binding_count,
        "semantic_assertion_count": sum(
            len(segment.get("translation_assertions") or [])
            for segment in manifest.get("segments") or []
        ),
        "semantic_qa": semantic_qa,
        "user_acceptance": "NOT_CHECKED",
    }
    if args.output:
        write_json(args.output, report)
    emit_json(report)
    return 0 if status == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
