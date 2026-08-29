#!/usr/bin/env python3
"""Verify the exact public-core reference runtime without changing it."""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import json
import sys
from typing import Any

CORE = {
    "PyMuPDF": ("1.27.2.2", "fitz"),
    "pypdf": ("6.10.0", "pypdf"),
    "Pillow": ("12.1.1", "PIL"),
}
DEV = {"PyYAML": ("6.0.3", "yaml")}
PYTHON_MIN = (3, 11)
PYTHON_MAX_EXCLUSIVE = (3, 15)


def check_package(name: str, expected: str, module: str) -> dict[str, Any]:
    try:
        actual = importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return {"package": name, "expected": expected, "status": "MISSING"}
    try:
        importlib.import_module(module)
    except Exception as exc:  # pragma: no cover - loader error is environment-specific
        return {
            "package": name,
            "expected": expected,
            "actual": actual,
            "status": "IMPORT_FAILED",
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "package": name,
        "expected": expected,
        "actual": actual,
        "status": "PASS" if actual == expected else "VERSION_MISMATCH",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dev", action="store_true", help="also require development/test dependencies")
    args = parser.parse_args()

    python_version = sys.version_info[:3]
    python_ok = PYTHON_MIN <= python_version[:2] < PYTHON_MAX_EXCLUSIVE
    packages = dict(CORE)
    if args.dev:
        packages.update(DEV)
    checks = [check_package(name, expected, module) for name, (expected, module) in packages.items()]
    ok = python_ok and all(item["status"] == "PASS" for item in checks)
    report = {
        "schema": "pdf-tw-localize/runtime-verification/v1",
        "status": "PASS" if ok else "BLOCKED",
        "python": {
            "version": ".".join(map(str, python_version)),
            "executable": sys.executable,
            "required": ">=3.11,<3.15",
            "status": "PASS" if python_ok else "VERSION_MISMATCH",
        },
        "development_dependencies_checked": args.dev,
        "packages": checks,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
