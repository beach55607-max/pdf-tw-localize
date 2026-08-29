from __future__ import annotations

import hashlib
import os
import sys
import unittest
from pathlib import Path
from unittest import mock


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from font_discovery import BOLD_ENV, REGULAR_ENV, discover_font_pair  # noqa: E402


class FontPortabilityTest(unittest.TestCase):
    def test_bundled_ofl_font_pair_is_discovered_and_hash_bound(self) -> None:
        with mock.patch.dict(os.environ, {REGULAR_ENV: "", BOLD_ENV: ""}, clear=False):
            regular, bold = discover_font_pair(SKILL_ROOT)
        self.assertEqual(regular.name, "NotoSansTC-Regular.ttf")
        self.assertEqual(bold.name, "NotoSansTC-Bold.ttf")
        self.assertEqual(hashlib.sha256(regular.read_bytes()).hexdigest(), "82559d4a2ab69224de5cb6191ddf40b0027c4b519ff891e9342d354045285014")
        self.assertEqual(hashlib.sha256(bold.read_bytes()).hexdigest(), "f298e7332e462777aecc30adddc7330c5a884b9c246567fc9f4afa44c7753775")
        self.assertTrue((regular.parent / "OFL.txt").is_file())

    def test_explicit_font_paths_have_highest_precedence(self) -> None:
        bundled = SKILL_ROOT / "assets" / "fonts"
        with mock.patch.dict(
            os.environ,
            {
                REGULAR_ENV: str(bundled / "NotoSansTC-Regular.ttf"),
                BOLD_ENV: str(bundled / "NotoSansTC-Bold.ttf"),
            },
            clear=False,
        ):
            regular, bold = discover_font_pair(self.id())
        self.assertEqual(regular, (bundled / "NotoSansTC-Regular.ttf").resolve())
        self.assertEqual(bold, (bundled / "NotoSansTC-Bold.ttf").resolve())

    def test_discovery_source_contains_no_fixed_windows_font_root(self) -> None:
        source = (SCRIPTS / "font_discovery.py").read_text(encoding="utf-8")
        self.assertNotIn(":\\Windows\\Fonts", source)
        self.assertNotIn(":/Windows/Fonts", source)


if __name__ == "__main__":
    unittest.main()
