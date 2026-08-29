from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from qa_pdf import (  # noqa: E402
    english_residue,
    load_allowlist,
    load_stable_qa_report,
)


class ScopedAllowlistQaTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write(self, payload: dict) -> Path:
        path = self.root / "allowlist.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    @staticmethod
    def _payload() -> dict:
        return {
            "schema": "pdf-tw-localize/english-allowlist/v3",
            "scope": {"document_id": "fixture", "pages": [1, 2, 27]},
            "allowed": [
                {
                    "token": "Example Electronics Inc.",
                    "type": "protected_proper_name",
                    "reason": "Exact registered company name is retained in copyright text.",
                    "scope": {
                        "pages": [1],
                        "segment_ids": ["fixture.p0001.copyright"],
                        "exact": True,
                    },
                    "basis": {
                        "type": "protected_content_policy",
                        "reference": "registered company-name policy",
                    },
                }
            ],
            "allowed_visual_english": [],
            "allowed_ui_english": [
                {
                    "source_text": "Additional Info",
                    "zh_TW": "其他資訊",
                    "type": "source_ui_user_preserved",
                    "reason": "Exact source UI remains beside scoped Traditional Chinese guidance.",
                    "scope": {
                        "pages": [27],
                        "segment_ids": ["fixture.p0027.screen-info"],
                        "exact": True,
                    },
                    "basis": {
                        "type": "user_instruction",
                        "reference": "explicit user-approved source UI preservation",
                    },
                    "visual_ids": ["fixture.p0027.screen"],
                    "guidance_segment_ids": ["fixture.p0027.guidance"],
                    "guidance_required": True,
                }
            ],
        }

    @staticmethod
    def _residue(text: str, page: int, loaded: tuple) -> dict[str, int]:
        exact, regex_strings, scoped_exact, scoped_regex, _metadata = loaded
        return dict(
            english_residue(
                text,
                exact,
                [re.compile(item) for item in regex_strings],
                page_exact_allowlist=scoped_exact.get(page),
                page_regex_allowlist=[
                    re.compile(item) for item in scoped_regex.get(page, set())
                ],
            )
        )

    def test_scoped_company_phrase_is_allowed_only_on_declared_page(self) -> None:
        loaded = load_allowlist(self._write(self._payload()))
        self.assertEqual(
            self._residue("Example Electronics Inc.", 1, loaded),
            {},
        )
        self.assertEqual(
            self._residue("Example Electronics Inc.", 2, loaded),
            {"Example": 1, "Electronics": 1, "Inc": 1},
        )

    def test_scoped_source_ui_phrase_is_allowed_only_on_declared_page(self) -> None:
        loaded = load_allowlist(self._write(self._payload()))
        self.assertEqual(self._residue("Additional Info", 27, loaded), {})
        self.assertEqual(
            self._residue("Additional Info", 2, loaded),
            {"Additional": 1, "Info": 1},
        )

    def test_invalid_v3_allowlist_fails_closed(self) -> None:
        payload = self._payload()
        payload["allowed"][0]["reason"] = "tool issue"
        with self.assertRaisesRegex(ValueError, "Invalid scoped English allowlist"):
            load_allowlist(self._write(payload))

    def test_legacy_line_allowlist_remains_global(self) -> None:
        path = self.root / "allowlist.txt"
        path.write_text("Example\nre:Widget[A-Z]+\n", encoding="utf-8")
        loaded = load_allowlist(path)
        self.assertEqual(self._residue("Example WidgetABC", 2, loaded), {})
        self.assertEqual(loaded[4]["format"], "LEGACY_LINES")

    def test_hash_bound_stable_qa_report_can_resolve_extraction_only_difference(self) -> None:
        source_hash = "A" * 64
        candidate_hash = "B" * 64
        path = self.root / "stable-qa.json"
        path.write_text(
            json.dumps(
                {
                    "schema": "pdf-tw-localize/rebuilt-machine-qa/v1",
                    "source": {"sha256": source_hash},
                    "candidate": {"sha256": candidate_hash},
                    "machine_qa": "MACHINE_QA_PASS",
                    "blocking_issue_count": 0,
                    "blocking_issues": [],
                    "protected_token_evidence": [
                        {
                            "segment_id": "fixture.p0001.value",
                            "source_token": "50 mm",
                            "target_token": "50 mm",
                            "status": "PRESERVED",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        evidence = load_stable_qa_report(
            path,
            source_hash=source_hash,
            candidate_hash=candidate_hash,
        )
        self.assertEqual(evidence["machine_qa"], "MACHINE_QA_PASS")
        self.assertEqual(evidence["protected_token_evidence_count"], 1)

    def test_stable_qa_report_with_wrong_candidate_hash_fails_closed(self) -> None:
        source_hash = "A" * 64
        path = self.root / "stable-qa.json"
        path.write_text(
            json.dumps(
                {
                    "schema": "pdf-tw-localize/rebuilt-machine-qa/v1",
                    "source": {"sha256": source_hash},
                    "candidate": {"sha256": "C" * 64},
                    "machine_qa": "MACHINE_QA_PASS",
                    "blocking_issue_count": 0,
                    "blocking_issues": [],
                    "protected_token_evidence": [
                        {
                            "status": "PRESERVED",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "candidate SHA-256"):
            load_stable_qa_report(
                path,
                source_hash=source_hash,
                candidate_hash="B" * 64,
            )


if __name__ == "__main__":
    unittest.main()
