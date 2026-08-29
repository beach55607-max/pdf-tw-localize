#!/usr/bin/env python3
"""Security and integrity preflight for the pdf-tw-localize Skill."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from _console import emit_json, emit_text

SCHEMA = "pdf-tw-localize/security-report/v1"
PATCH_SCHEMA = "pdf-tw-localize/pdf2zh-patch-manifest/v1"
LOCK_SCHEMA = "pdf-tw-localize/runtime-lock/v1"
DIST_NAMES = {
    "babeldoc": "babeldoc",
    "pymupdf": "PyMuPDF",
    "pypdf": "pypdf",
    "opencc": "OpenCC",
    "llama-cpp-python": "llama-cpp-python",
    "pdf2zh-next": "pdf2zh-next",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def sha256_tree(root: Path) -> str:
    digest = hashlib.sha256()
    ignored_parts = {".git", "__pycache__", ".pytest_cache", ".mypy_cache"}
    files = [
        path
        for path in root.rglob("*")
        if path.is_file()
        and not any(part in ignored_parts for part in path.relative_to(root).parts)
        and path.suffix.lower() not in {".pyc", ".pyo"}
    ]
    for path in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
        rel = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(rel).to_bytes(4, "big"))
        digest.update(rel)
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest().upper()


def version_key(value: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", value.split("+", 1)[0])
    return tuple(int(part) for part in parts) or (0,)


def add_finding(
    findings: list[dict[str, Any]],
    severity: str,
    code: str,
    message: str,
    evidence: Any = None,
) -> None:
    item: dict[str, Any] = {
        "severity": severity,
        "code": code,
        "message": message,
    }
    if evidence is not None:
        item["evidence"] = evidence
    findings.append(item)


def installed_version(distribution: str) -> str | None:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return None


def canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def validate_runtime_lock(
    manifest_path: Path,
    lock_override: Path | None,
    patch_manifest: Path | None,
    findings: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not manifest_path.is_file():
        add_finding(
            findings,
            "BLOCKED",
            "RUNTIME_LOCK_MANIFEST_MISSING",
            "Runtime-lock manifest does not exist.",
            str(manifest_path),
        )
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        add_finding(
            findings,
            "BLOCKED",
            "RUNTIME_LOCK_MANIFEST_INVALID",
            f"Runtime-lock manifest cannot be read: {exc}",
        )
        return None
    if manifest.get("schema") != LOCK_SCHEMA or manifest.get("status") != "PASS":
        add_finding(
            findings,
            "BLOCKED",
            "RUNTIME_LOCK_MANIFEST_REJECTED",
            "Runtime-lock manifest schema or status is not approved.",
        )
        return None

    lock_record = manifest.get("lock", {})
    lock_path = lock_override or Path(str(lock_record.get("path", "")))
    if not lock_path.is_file():
        add_finding(
            findings,
            "BLOCKED",
            "RUNTIME_LOCK_MISSING",
            "Hashed runtime lock file does not exist.",
            str(lock_path),
        )
        return manifest
    actual_hash = sha256_file(lock_path)
    expected_hash = str(lock_record.get("sha256", "")).upper()
    if actual_hash != expected_hash:
        add_finding(
            findings,
            "BLOCKED",
            "RUNTIME_LOCK_HASH_MISMATCH",
            "Runtime lock differs from its manifest.",
            {"expected": expected_hash, "actual": actual_hash},
        )

    if not lock_record.get("all_exactly_pinned") or not lock_record.get("all_hashed"):
        add_finding(
            findings,
            "BLOCKED",
            "RUNTIME_LOCK_INCOMPLETE",
            "Runtime lock is not recorded as exact and fully SHA-256 hashed.",
        )

    locked_versions = {
        canonical_name(name): str(version)
        for name, version in lock_record.get("versions", {}).items()
    }
    installed_versions: dict[str, str] = {}
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get("Name")
        if name:
            installed_versions[canonical_name(name)] = distribution.version

    missing = {
        name: version
        for name, version in locked_versions.items()
        if name not in installed_versions
    }
    mismatched = {
        name: {"locked": version, "installed": installed_versions.get(name)}
        for name, version in locked_versions.items()
        if name in installed_versions and installed_versions[name] != version
    }
    if missing:
        add_finding(
            findings,
            "BLOCKED",
            "LOCKED_PACKAGE_MISSING",
            "Packages from the runtime lock are missing.",
            missing,
        )
    if mismatched:
        add_finding(
            findings,
            "BLOCKED",
            "LOCKED_PACKAGE_VERSION_MISMATCH",
            "Installed package versions differ from the runtime lock.",
            mismatched,
        )

    allowed_extra = {"pdf2zh-next"}
    extras = sorted(set(installed_versions) - set(locked_versions) - allowed_extra)
    if extras:
        add_finding(
            findings,
            "NEEDS_REVIEW",
            "UNLOCKED_PACKAGES_PRESENT",
            "The environment contains packages not covered by the runtime lock.",
            {name: installed_versions[name] for name in extras},
        )

    if patch_manifest and patch_manifest.is_file():
        patch_hash = sha256_file(patch_manifest)
        recorded_patch_hash = str(
            manifest.get("patch_manifest", {}).get("sha256", "")
        ).upper()
        if patch_hash != recorded_patch_hash:
            add_finding(
                findings,
                "BLOCKED",
                "LOCK_PATCH_MANIFEST_MISMATCH",
                "Runtime lock was generated for a different patch manifest.",
                {"expected": recorded_patch_hash, "actual": patch_hash},
            )
    return manifest


def check_runtime(
    policy: dict[str, Any],
    patch_manifest: Path | None,
    patched_source: Path | None,
    runtime_lock_manifest: Path | None,
    runtime_lock: Path | None,
    required_packages: set[str],
    findings: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    package_results: dict[str, Any] = {}
    rules = policy.get("packages", {})

    for policy_name, rule in rules.items():
        distribution = DIST_NAMES.get(policy_name, policy_name)
        value = installed_version(distribution)
        package_results[policy_name] = {"distribution": distribution, "version": value}

        if value is None:
            if policy_name in required_packages:
                add_finding(
                    findings,
                    "BLOCKED",
                    "PACKAGE_MISSING",
                    f"Required package is not installed: {distribution}",
                )
            continue

        minimum = rule.get("minimum")
        maximum = rule.get("maximum_exclusive")
        if minimum and version_key(value) < version_key(str(minimum)):
            add_finding(
                findings,
                "BLOCKED",
                "PACKAGE_TOO_OLD",
                f"{distribution} {value} is below approved minimum {minimum}.",
            )
        if maximum and version_key(value) >= version_key(str(maximum)):
            add_finding(
                findings,
                "NEEDS_REVIEW",
                "PACKAGE_OUTSIDE_TESTED_RANGE",
                f"{distribution} {value} is outside tested range below {maximum}.",
            )

    pdf2zh_version = package_results.get("pdf2zh-next", {}).get("version")
    babeldoc_version = package_results.get("babeldoc", {}).get("version")
    pymupdf_version = package_results.get("pymupdf", {}).get("version")

    if pdf2zh_version and not babeldoc_version:
        add_finding(
            findings,
            "BLOCKED",
            "PDF2ZH_WITHOUT_BABELDOC",
            "pdf2zh-next is installed but BabelDOC is missing.",
        )
    if pdf2zh_version and not pymupdf_version:
        add_finding(
            findings,
            "BLOCKED",
            "PDF2ZH_WITHOUT_PYMUPDF",
            "pdf2zh-next is installed but PyMuPDF is missing.",
        )

    if pdf2zh_version:
        if patch_manifest is None:
            add_finding(
                findings,
                "BLOCKED",
                "PATCH_MANIFEST_REQUIRED",
                "Installed pdf2zh-next requires a verified controlled-fork patch manifest.",
            )
        else:
            validate_patch_manifest(
                patch_manifest,
                patched_source,
                policy,
                pdf2zh_version,
                findings,
            )
        if runtime_lock_manifest is None:
            add_finding(
                findings,
                "BLOCKED",
                "RUNTIME_LOCK_MANIFEST_REQUIRED",
                "Installed pdf2zh-next requires an exact hashed runtime-lock manifest.",
            )
    elif patch_manifest is not None:
        validate_patch_manifest(
            patch_manifest,
            patched_source,
            policy,
            None,
            findings,
        )

    cmap_path = os.environ.get("CMAP_PATH")
    if cmap_path:
        add_finding(
            findings,
            "BLOCKED",
            "CMAP_PATH_SET",
            "CMAP_PATH is set. Clear it unless the path is independently trusted and explicitly approved.",
            cmap_path,
        )

    gradio_name = os.environ.get("GRADIO_SERVER_NAME", "").strip()
    if gradio_name in {"0.0.0.0", "::"}:
        add_finding(
            findings,
            "BLOCKED",
            "PUBLIC_BINDING",
            "GRADIO_SERVER_NAME exposes the UI beyond localhost.",
            gradio_name,
        )
    if os.environ.get("GRADIO_SHARE", "").strip().lower() in {"1", "true", "yes", "on"}:
        add_finding(
            findings,
            "BLOCKED",
            "GRADIO_SHARE_ENABLED",
            "GRADIO_SHARE is enabled.",
        )

    lock_record = None
    if runtime_lock_manifest is not None:
        lock_record = validate_runtime_lock(
            runtime_lock_manifest,
            runtime_lock,
            patch_manifest,
            findings,
        )
    return package_results, lock_record


def validate_patch_manifest(
    manifest_path: Path,
    patched_source: Path | None,
    policy: dict[str, Any],
    installed_pdf2zh_version: str | None,
    findings: list[dict[str, Any]],
) -> None:
    if not manifest_path.is_file():
        add_finding(
            findings,
            "BLOCKED",
            "PATCH_MANIFEST_MISSING",
            "Patch manifest does not exist.",
            str(manifest_path),
        )
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        add_finding(
            findings,
            "BLOCKED",
            "PATCH_MANIFEST_INVALID",
            f"Patch manifest cannot be read: {exc}",
        )
        return

    if manifest.get("schema") != PATCH_SCHEMA or manifest.get("status") != "PASS":
        add_finding(
            findings,
            "BLOCKED",
            "PATCH_MANIFEST_REJECTED",
            "Patch manifest schema or status is not approved.",
        )
        return

    rule = policy.get("packages", {}).get("pdf2zh-next", {})
    upstream = manifest.get("upstream", {})
    expected_version = rule.get("allowed_upstream_version")
    expected_commit = str(rule.get("allowed_upstream_commit", "")).lower()
    expected_tree = str(rule.get("allowed_pre_patch_tree_sha256", "")).upper()
    manifest_version = str(upstream.get("version", ""))
    manifest_commit = str(upstream.get("commit", "")).lower()
    manifest_tree = str(upstream.get("pre_patch_tree_sha256", "")).upper()

    if manifest_version != expected_version:
        add_finding(
            findings,
            "BLOCKED",
            "PATCH_UPSTREAM_VERSION_MISMATCH",
            f"Manifest upstream version {manifest_version!r} does not match {expected_version!r}.",
        )
    if expected_commit and manifest_commit != expected_commit:
        add_finding(
            findings,
            "BLOCKED",
            "PATCH_UPSTREAM_COMMIT_MISMATCH",
            "Manifest does not identify the approved upstream commit.",
            {"expected": expected_commit, "actual": manifest_commit},
        )
    if expected_tree and manifest_tree != expected_tree:
        add_finding(
            findings,
            "BLOCKED",
            "PATCH_UPSTREAM_TREE_MISMATCH",
            "Manifest does not prove the approved official upstream source tree.",
            {"expected": expected_tree, "actual": manifest_tree},
        )
    if installed_pdf2zh_version and manifest_version != installed_pdf2zh_version:
        add_finding(
            findings,
            "BLOCKED",
            "PATCH_INSTALLED_VERSION_MISMATCH",
            "Installed pdf2zh-next version does not match the patched upstream version.",
            {"installed": installed_pdf2zh_version, "manifest": manifest_version},
        )

    if patched_source is None:
        add_finding(
            findings,
            "NEEDS_REVIEW",
            "PATCHED_SOURCE_NOT_VERIFIED",
            "Manifest is present, but no patched source tree was supplied for hash verification.",
        )
        return
    if not patched_source.is_dir():
        add_finding(
            findings,
            "BLOCKED",
            "PATCHED_SOURCE_MISSING",
            "Patched source directory does not exist.",
            str(patched_source),
        )
        return

    actual_hash = sha256_tree(patched_source)
    expected_hash = str(manifest.get("post_patch_tree_sha256", "")).upper()
    if actual_hash != expected_hash:
        add_finding(
            findings,
            "BLOCKED",
            "PATCHED_SOURCE_HASH_MISMATCH",
            "Patched source tree differs from the recorded manifest.",
            {"expected": expected_hash, "actual": actual_hash},
        )


def scan_pdf(path: Path, findings: list[dict[str, Any]]) -> dict[str, Any]:
    record: dict[str, Any] = {
        "path": str(path.resolve()),
        "sha256": None,
        "size": None,
        "pages": None,
        "encrypted": None,
    }
    if not path.is_file():
        add_finding(findings, "BLOCKED", "INPUT_MISSING", "Input file does not exist.", str(path))
        return record
    if path.suffix.lower() != ".pdf":
        add_finding(
            findings,
            "BLOCKED",
            "INPUT_NOT_PDF",
            "Input does not have a .pdf extension.",
            str(path),
        )
        return record

    record["size"] = path.stat().st_size
    record["sha256"] = sha256_file(path)
    data = path.read_bytes()
    if b"%PDF-" not in data[:1024]:
        add_finding(
            findings,
            "BLOCKED",
            "PDF_MAGIC_MISSING",
            "PDF header is not present near the start of the file.",
            str(path),
        )
        return record

    high_risk_patterns = {
        "PDF_JAVASCRIPT": rb"/JavaScript\b|/JS\s*[\(<]",
        "PDF_LAUNCH_ACTION": rb"/Launch\b",
        "PDF_EMBEDDED_FILE": rb"/EmbeddedFile\b",
    }
    review_patterns = {
        "PDF_OPEN_ACTION": rb"/OpenAction\b|/AA\b",
        "PDF_EXTERNAL_REFERENCE": rb"/GoToR\b|/SubmitForm\b|/ImportData\b",
        "PDF_URI": rb"/URI\b",
    }
    for code, pattern in high_risk_patterns.items():
        if re.search(pattern, data, re.IGNORECASE):
            add_finding(
                findings,
                "BLOCKED",
                code,
                "PDF contains active or embedded content that is not allowed in the translation pipeline.",
                str(path),
            )
    for code, pattern in review_patterns.items():
        if re.search(pattern, data, re.IGNORECASE):
            add_finding(
                findings,
                "NEEDS_REVIEW",
                code,
                "PDF contains an action or external-reference indicator requiring review.",
                str(path),
            )

    cmap_risk = re.compile(
        rb"(?:/CMapName|/UseCMap|CMAP_PATH).{0,256}(?:\.\.[/\\]|[A-Za-z]:[/\\]|file:)",
        re.IGNORECASE | re.DOTALL,
    )
    if cmap_risk.search(data):
        add_finding(
            findings,
            "BLOCKED",
            "SUSPICIOUS_CMAP_PATH",
            "PDF contains a suspicious CMap path indicator.",
            str(path),
        )

    try:
        import fitz

        document = fitz.open(path)
        record["pages"] = document.page_count
        record["encrypted"] = bool(document.needs_pass)
        if document.needs_pass:
            add_finding(
                findings,
                "BLOCKED",
                "PDF_ENCRYPTED",
                "Encrypted PDF cannot be fully inspected without an explicitly supplied password.",
                str(path),
            )
        for index in range(document.page_count):
            try:
                page = document.load_page(index)
                _ = page.rect
                _ = page.get_text("text")
            except Exception as exc:
                add_finding(
                    findings,
                    "BLOCKED",
                    "PDF_PAGE_PARSE_FAILED",
                    f"Page {index + 1} could not be parsed: {exc}",
                    str(path),
                )
                break
        document.close()
    except ImportError:
        if re.search(rb"/Encrypt\b", data):
            record["encrypted"] = True
            add_finding(
                findings,
                "BLOCKED",
                "PDF_ENCRYPTED",
                "PDF encryption indicator was found.",
                str(path),
            )
        add_finding(
            findings,
            "NEEDS_REVIEW",
            "PYMUPDF_NOT_AVAILABLE",
            "PyMuPDF is unavailable, so page-level parsing was not checked.",
        )
    except Exception as exc:
        add_finding(
            findings,
            "BLOCKED",
            "PDF_OPEN_FAILED",
            f"PDF could not be opened: {exc}",
            str(path),
        )

    return record


def check_model(
    model: Path | None,
    expected_hash: str | None,
    model_id: str | None,
    policy: dict[str, Any],
    findings: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if model is None:
        return None
    record: dict[str, Any] = {"path": str(model.resolve()), "sha256": None, "size": None}
    if not model.is_file():
        add_finding(findings, "BLOCKED", "MODEL_MISSING", "Model file does not exist.", str(model))
        return record

    if model_id:
        approved = policy.get("approved_models", {}).get(model_id)
        if approved is None:
            add_finding(
                findings,
                "BLOCKED",
                "MODEL_ID_NOT_APPROVED",
                f"Model ID is not present in the approved policy: {model_id}",
            )
        elif expected_hash and expected_hash.upper() != str(approved.get("sha256", "")).upper():
            add_finding(
                findings,
                "BLOCKED",
                "MODEL_EXPECTED_HASH_CONFLICT",
                "Command-line model hash conflicts with the approved policy.",
            )
        else:
            expected_hash = str(approved.get("sha256", ""))

    record["size"] = model.stat().st_size
    record["sha256"] = sha256_file(model)
    if not expected_hash:
        add_finding(
            findings,
            "NEEDS_REVIEW",
            "MODEL_HASH_NOT_PINNED",
            "Model identity was calculated but no approved expected SHA-256 was supplied.",
            record["sha256"],
        )
    elif record["sha256"].upper() != expected_hash.upper():
        add_finding(
            findings,
            "BLOCKED",
            "MODEL_HASH_MISMATCH",
            "Model SHA-256 does not match the approved value.",
            {"expected": expected_hash.upper(), "actual": record["sha256"]},
        )
    return record


def derive_status(findings: list[dict[str, Any]]) -> str:
    severities = {item["severity"] for item in findings}
    if "BLOCKED" in severities:
        return "BLOCKED"
    if "NEEDS_REVIEW" in severities:
        return "NEEDS_REVIEW"
    return "PASS"


def build_parser() -> argparse.ArgumentParser:
    skill_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description="Check PDF inputs, package versions, patch provenance, environment, and model integrity."
    )
    parser.add_argument("inputs", nargs="*", type=Path, help="PDF input files to inspect.")
    parser.add_argument(
        "--policy",
        type=Path,
        default=skill_root / "references" / "approved-runtime.json",
        help="Approved runtime policy JSON.",
    )
    parser.add_argument("--model", type=Path, help="Optional local model file.")
    parser.add_argument("--model-sha256", help="Expected model SHA-256.")
    parser.add_argument("--model-id", help="Model ID in approved-runtime.json.")
    parser.add_argument("--patch-manifest", type=Path, help="Controlled-fork patch manifest.")
    parser.add_argument("--patched-source", type=Path, help="Patched pdf2zh source tree.")
    parser.add_argument("--runtime-lock-manifest", type=Path, help="Hashed runtime-lock manifest.")
    parser.add_argument("--runtime-lock", type=Path, help="Override the lock path recorded in its manifest.")
    parser.add_argument(
        "--require-package",
        action="append",
        default=[],
        choices=sorted(DIST_NAMES),
        help="Require a policy package to be installed; may be repeated.",
    )
    parser.add_argument("--output", type=Path, help="Write JSON report to this path.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        policy = json.loads(args.policy.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        emit_text(f"BLOCKED: cannot read policy: {exc}", stream=sys.stderr)
        return 2

    findings: list[dict[str, Any]] = []
    runtime, runtime_lock = check_runtime(
        policy,
        args.patch_manifest,
        args.patched_source,
        args.runtime_lock_manifest,
        args.runtime_lock,
        set(args.require_package),
        findings,
    )
    inputs = [scan_pdf(path, findings) for path in args.inputs]
    model = check_model(
        args.model,
        args.model_sha256,
        args.model_id,
        policy,
        findings,
    )
    report = {
        "schema": SCHEMA,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "policy_path": str(args.policy.resolve()),
        "policy_version": policy.get("policy_version"),
        "status": derive_status(findings),
        "runtime": runtime,
        "runtime_lock": runtime_lock,
        "inputs": inputs,
        "model": model,
        "findings": findings,
        "limitations": [
            "This is a conservative known-indicator scan, not full malware forensics.",
            "A matching model hash proves identity, not behavioral safety.",
        ],
    }

    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    emit_json(report)
    return {"PASS": 0, "BLOCKED": 2, "NEEDS_REVIEW": 3}[report["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
