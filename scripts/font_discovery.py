#!/usr/bin/env python3
"""Cross-platform, evidence-first font discovery without fixed Windows paths."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Iterable


REGULAR_ENV = "PDF_TW_LOCALIZE_FONT_REGULAR"
BOLD_ENV = "PDF_TW_LOCALIZE_FONT_BOLD"
SUPPORTED_EXTENSIONS = {".otf", ".ttf", ".ttc"}
FAMILY_HINTS = (
    "notosanstc",
    "notosanscjktc",
    "sourcehansanstc",
    "sourcehansans",
)


class FontDiscoveryError(RuntimeError):
    pass


def _existing_font(value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value).expanduser().resolve()
    if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS:
        return path
    return None


def _candidate_directories(skill_root: Path | None) -> list[Path]:
    directories: list[Path] = []
    if skill_root is not None:
        directories.append(skill_root / "assets" / "fonts")
    system = platform.system()
    if system == "Windows":
        root = os.environ.get("SystemRoot")
        if root:
            directories.append(Path(root) / "Fonts")
        local = os.environ.get("LOCALAPPDATA")
        if local:
            directories.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
    elif system == "Darwin":
        directories.extend(
            [Path("/System/Library/Fonts"), Path("/Library/Fonts"), Path.home() / "Library" / "Fonts"]
        )
    else:
        directories.extend(
            [Path("/usr/share/fonts"), Path("/usr/local/share/fonts"), Path.home() / ".local" / "share" / "fonts"]
        )
    return directories


def _font_files(directories: Iterable[Path]) -> list[Path]:
    results: list[Path] = []
    for directory in directories:
        if not directory.is_dir():
            continue
        for path in directory.rglob("*"):
            if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS:
                results.append(path.resolve())
    return sorted(set(results), key=lambda path: str(path).casefold())


def _score(path: Path, *, bold: bool) -> tuple[int, str]:
    compact = "".join(character for character in path.stem.casefold() if character.isalnum())
    family = max((100 - index for index, hint in enumerate(FAMILY_HINTS) if hint in compact), default=0)
    bold_signal = any(token in compact for token in ("bold", "semibold", "demibold", "700", "800", "900"))
    regular_signal = any(token in compact for token in ("regular", "book", "400"))
    role = (30 if bold_signal else -20 if regular_signal else 0) if bold else (30 if regular_signal else -20 if bold_signal else 0)
    return family + role, str(path).casefold()


def _fc_match(pattern: str) -> Path | None:
    executable = shutil.which("fc-match")
    if executable is None:
        return None
    result = subprocess.run(
        [executable, "-f", "%{file}", pattern],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
        timeout=10,
    )
    if result.returncode != 0:
        return None
    return _existing_font(result.stdout.strip())


def discover_font_pair(skill_root: str | os.PathLike[str] | None = None) -> tuple[Path, Path]:
    """Return regular and bold fonts, preferring explicit and bundled evidence."""

    regular = _existing_font(os.environ.get(REGULAR_ENV))
    bold = _existing_font(os.environ.get(BOLD_ENV))
    if regular is not None and bold is not None:
        return regular, bold

    root = Path(skill_root).resolve() if skill_root is not None else None
    bundled_regular = _existing_font(str(root / "assets" / "fonts" / "NotoSansTC-Regular.ttf")) if root else None
    bundled_bold = _existing_font(str(root / "assets" / "fonts" / "NotoSansTC-Bold.ttf")) if root else None
    regular = regular or bundled_regular
    bold = bold or bundled_bold
    if regular is not None and bold is not None:
        return regular, bold

    regular = regular or _fc_match("Noto Sans CJK TC:style=Regular")
    bold = bold or _fc_match("Noto Sans CJK TC:style=Bold")
    if regular is not None and bold is not None:
        return regular, bold

    fonts = _font_files(_candidate_directories(root))
    if regular is None:
        candidates = sorted(fonts, key=lambda path: _score(path, bold=False), reverse=True)
        regular = candidates[0] if candidates and _score(candidates[0], bold=False)[0] > 0 else None
    if bold is None:
        candidates = sorted(fonts, key=lambda path: _score(path, bold=True), reverse=True)
        bold = candidates[0] if candidates and _score(candidates[0], bold=True)[0] > 0 else None
    if regular is None or bold is None:
        raise FontDiscoveryError(
            f"No verified Traditional Chinese regular/bold pair found. Set {REGULAR_ENV} and {BOLD_ENV}, "
            "or fetch the locked OFL test fixture described in references/font-fixture-provenance.md."
        )
    return regular, bold


if __name__ == "__main__":
    regular_font, bold_font = discover_font_pair(Path(__file__).resolve().parents[1])
    print(f"regular={regular_font}")
    print(f"bold={bold_font}")
