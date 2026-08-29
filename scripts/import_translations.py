#!/usr/bin/env python3
"""Import model-authored zh-TW text by exact stable segment ID."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from _console import emit_json
from _segment_common import read_json, sha256_file, validate_manifest, write_json


def main() -> int:
    parser = argparse.ArgumentParser(description="Import zh-TW translations by stable segment ID.")
    parser.add_argument("extraction", type=Path)
    parser.add_argument("translations", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    extraction_path = args.extraction.resolve()
    translations_path = args.translations.resolve()
    output_path = args.output.resolve()
    if output_path.exists():
        raise FileExistsError(f"Refusing to overwrite existing output: {output_path}")
    manifest = read_json(extraction_path)
    payload = read_json(translations_path)
    if payload.get("schema") != "pdf-tw-localize/translation-import/v1":
        raise ValueError("Unsupported translation import schema")
    if payload.get("source_manifest_sha256") != sha256_file(extraction_path):
        raise ValueError("Translation import is bound to a different extraction manifest")
    entries = payload.get("translations") or []
    entry_ids = [str(entry.get("segment_id", "")) for entry in entries]
    duplicates = sorted(identifier for identifier, count in Counter(entry_ids).items() if count > 1)
    if duplicates:
        raise ValueError(f"Duplicate translation IDs: {duplicates}")
    expected_ids = {segment["segment_id"] for segment in manifest["segments"]}
    actual_ids = set(entry_ids)
    if expected_ids != actual_ids:
        raise ValueError(
            f"Translation ID coverage mismatch: missing={sorted(expected_ids - actual_ids)} "
            f"unexpected={sorted(actual_ids - expected_ids)}"
        )
    by_id = {entry["segment_id"]: entry for entry in entries}
    for segment in manifest["segments"]:
        entry = by_id[segment["segment_id"]]
        zh_text = str(entry.get("zh_TW", ""))
        action = (segment.get("render") or {}).get("action", "replace")
        if action != "preserve" and not zh_text.strip():
            raise ValueError(f"Empty translation: {segment['segment_id']}")
        if action == "preserve" and not zh_text:
            zh_text = segment["source_text"]
        segment["zh_TW"] = zh_text
        segment["status"] = "TRANSLATED"
        segment["translation_note"] = entry.get("note", "")
        segment["translation_assertions"] = list(entry.get("translation_assertions") or [])
    manifest["translation_import"] = {
        "path": str(translations_path),
        "sha256": sha256_file(translations_path),
        "source_manifest_path": str(extraction_path),
        "source_manifest_sha256": sha256_file(extraction_path),
        "translator": payload.get("translator", "context-capable language model"),
        "translated_at_utc": payload.get("translated_at_utc") or datetime.now(timezone.utc).isoformat(),
        "script_called_language_model": False,
    }
    manifest["status"] = "TRANSLATED"
    manifest["machine_qa"] = "NOT_CHECKED"
    manifest["semantic_qa"] = "NOT_CHECKED"
    manifest["visual_review"] = "NOT_CHECKED"
    manifest["user_acceptance"] = "NOT_CHECKED"
    issues = validate_manifest(manifest, require_translation=True, require_render=False)
    unresolved = [
        item for item in issues if item.get("severity") in {"BLOCKING", "NEEDS_REVIEW"}
    ]
    if unresolved:
        raise ValueError(
            f"Imported translations are blocked or need review: {json.dumps(unresolved, ensure_ascii=False)}"
        )
    write_json(output_path, manifest)
    emit_json(
        {
            "status": "TRANSLATED",
            "segment_count": len(entries),
            "unmapped_id_count": 0,
            "duplicate_id_count": 0,
            "output": str(output_path),
            "output_sha256": sha256_file(output_path),
            "user_acceptance": "NOT_CHECKED",
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
