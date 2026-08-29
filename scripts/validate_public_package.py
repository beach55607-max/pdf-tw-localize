#!/usr/bin/env python3
"""Fail-closed hygiene and release-structure validation for the public core."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = {
    ".github/workflows/ci.yml",
    "SKILL.md",
    "README.md",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "VERSION",
    "pyproject.toml",
    "uv.lock",
    "requirements-core.lock",
    "requirements-dev.lock",
    "agents/openai.yaml",
    "references/dependency-manifest.json",
    "references/approved-runtime.json",
    "assets/fonts/OFL.txt",
}
IGNORED_DIRS = {".git", ".venv"}
FORBIDDEN_DIRS = {"__pycache__", ".pytest_cache", "artifacts", "outputs", "reports"}
FORBIDDEN_SUFFIXES = {
    ".pdf",
    ".docx",
    ".xlsx",
    ".pptx",
    ".zip",
    ".pkl",
    ".pickle",
    ".gguf",
    ".safetensors",
    ".pyc",
}
TEXT_SUFFIXES = {".md", ".py", ".json", ".yaml", ".yml", ".toml", ".csv", ".txt", ".ps1", ".lock", ""}
ABSOLUTE_PRIVATE_PATH = re.compile(r"(?i)(?:[A-Z]:[\\/]Users[\\/]|/(?:home|Users)/[^/]+/)")
SECRET_ASSIGNMENT = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|client[_-]?secret|password)\s*[:=]\s*[\"'][^\"']{8,}[\"']"
)
MARKDOWN_LINK = re.compile(r"\[[^\]]+\]\((?!https?://|mailto:|#)([^)]+)\)")


def ignored(path: Path) -> bool:
    return any(part in IGNORED_DIRS for part in path.relative_to(ROOT).parts)


def issue(code: str, path: str, detail: str) -> dict[str, str]:
    return {"code": code, "path": path, "detail": detail}


def main() -> int:
    issues: list[dict[str, str]] = []
    paths = [path for path in ROOT.rglob("*") if not ignored(path)]
    files = [path for path in paths if path.is_file()]
    relative_files = {path.relative_to(ROOT).as_posix() for path in files}

    for required in sorted(REQUIRED - relative_files):
        issues.append(issue("REQUIRED_FILE_MISSING", required, "required release file is absent"))

    for path in paths:
        relative = path.relative_to(ROOT).as_posix()
        if path.is_symlink() or os.path.islink(path):
            issues.append(issue("SYMLINK_FORBIDDEN", relative, "public package must contain regular files only"))
        if path.is_dir() and path.name in FORBIDDEN_DIRS:
            issues.append(issue("FORBIDDEN_DIRECTORY", relative, "runtime or private output directory is present"))
        if path.is_file() and path.suffix.lower() in FORBIDDEN_SUFFIXES:
            issues.append(issue("FORBIDDEN_FILE_TYPE", relative, path.suffix.lower()))

    for path in files:
        relative = path.relative_to(ROOT).as_posix()
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            issues.append(issue("TEXT_NOT_UTF8", relative, "declared text file does not decode as UTF-8"))
            continue
        if ABSOLUTE_PRIVATE_PATH.search(text):
            issues.append(issue("PRIVATE_ABSOLUTE_PATH", relative, "user-specific absolute path found"))
        if SECRET_ASSIGNMENT.search(text):
            issues.append(issue("POSSIBLE_SECRET", relative, "credential-like literal assignment found"))
        if path.suffix.lower() == ".md":
            for target in MARKDOWN_LINK.findall(text):
                clean = target.split("#", 1)[0].strip().strip("<>")
                if clean and not (path.parent / clean).resolve().exists():
                    issues.append(issue("BROKEN_MARKDOWN_LINK", relative, target))

    try:
        manifest = json.loads((ROOT / "references/dependency-manifest.json").read_text(encoding="utf-8"))
        project_license = manifest["project_license"]
        if project_license.get("spdx") != "AGPL-3.0-only":
            issues.append(issue("PROJECT_LICENSE_MISMATCH", "references/dependency-manifest.json", "SPDX must be AGPL-3.0-only"))
        if project_license.get("license_file_present") is not True or project_license.get("release_allowed") is not True:
            issues.append(issue("RELEASE_NOT_ALLOWED", "references/dependency-manifest.json", "release flags are not enabled"))
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        issues.append(issue("DEPENDENCY_MANIFEST_INVALID", "references/dependency-manifest.json", str(exc)))

    skill_path = ROOT / "SKILL.md"
    skill_text = skill_path.read_text(encoding="utf-8") if skill_path.exists() else ""
    if not skill_text.startswith("---\n") or "name: pdf-tw-localize" not in skill_text[:500]:
        issues.append(issue("SKILL_FRONTMATTER_INVALID", "SKILL.md", "required YAML frontmatter or exact name is missing"))

    status = "PASS" if not issues else "BLOCKED"
    report = {
        "schema": "pdf-tw-localize/public-package-validation/v1",
        "status": status,
        "root": str(ROOT),
        "file_count": len(files),
        "issues": issues,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if status == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
