from __future__ import annotations

import csv
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

from domain_pack import (  # noqa: E402
    PackValidationError,
    canonical_pack_digest,
    domain_validation_state,
    load_domain_pack,
    resolve_glossary,
    sha256_file,
)


class DomainPackTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.pack_root = self.root / "example-domain"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_glossary(self, path: Path, rows: list[list[str]] | None = None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream, lineterminator="\n")
            writer.writerow(["id", "source", "target", "notes"])
            writer.writerows(rows or [["domain-term-1", "Indicator", "指示器", "synthetic domain term"]])

    def _manifest(self) -> dict:
        glossary = self.pack_root / "data" / "glossary.csv"
        self._write_glossary(glossary)
        manifest = {
            "schema": "pdf-tw-localize/domain-pack/v1",
            "pack_id": "example-domain",
            "version": "1.2.3",
            "schema_version": "1.0.0",
            "public_core_compatibility": {"minimum": "1.0.0", "maximum_exclusive": "2.0.0"},
            "contents": [
                {
                    "id": "content-glossary",
                    "path": "data/glossary.csv",
                    "media_type": "text/csv",
                    "role": "glossary",
                    "schema": "pdf-tw-localize/glossary/v1",
                    "sha256": sha256_file(glossary),
                }
            ],
            "pack_digest": "0" * 64,
            "provenance": {
                "owner": "Synthetic Test Owner",
                "created_at": "2026-01-01T00:00:00Z",
                "source_basis": "Synthetic fixtures only",
                "confidentiality": "TEST_DATA",
            },
            "scope": {
                "locale": "zh-TW",
                "domain": "synthetic-controls",
                "purpose": "Test explicit data-only loading",
            },
            "load_policy": {
                "mode": "explicit_path_only",
                "auto_discovery": False,
                "require_expected_identity": True,
                "data_only": True,
            },
        }
        return manifest

    def _write_manifest(self, manifest: dict, *, recalculate: bool = True) -> str:
        self.pack_root.mkdir(parents=True, exist_ok=True)
        if recalculate:
            manifest["pack_digest"] = canonical_pack_digest(manifest)
        (self.pack_root / "pack.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return manifest["pack_digest"]

    def _load(self, digest: str, **overrides):
        arguments = {
            "expected_pack_id": "example-domain",
            "expected_version": "1.2.3",
            "expected_digest": digest,
            "core_version": "1.0.0",
        }
        arguments.update(overrides)
        return load_domain_pack(self.pack_root, **arguments)

    def test_explicit_identity_and_digest_bound_load_succeeds(self) -> None:
        manifest = self._manifest()
        digest = self._write_manifest(manifest)
        loaded = self._load(digest)
        self.assertEqual(loaded.identity, ("example-domain", "1.2.3", digest))
        self.assertEqual(loaded.glossary_entries[0]["target"], "指示器")
        self.assertEqual(domain_validation_state(loaded), "READY")

    def test_missing_pack_keeps_domain_validation_not_checked(self) -> None:
        self.assertEqual(domain_validation_state(None), "NOT_CHECKED")
        with self.assertRaisesRegex(PackValidationError, "PACK_MISSING"):
            load_domain_pack(
                self.root / "absent",
                expected_pack_id="example-domain",
                expected_version="1.2.3",
                expected_digest="0" * 64,
            )

    def test_content_hash_mismatch_is_blocked(self) -> None:
        manifest = self._manifest()
        digest = self._write_manifest(manifest)
        with (self.pack_root / "data" / "glossary.csv").open("a", encoding="utf-8") as stream:
            stream.write("tamper,Switch,開關,tampered after manifest\n")
        with self.assertRaisesRegex(PackValidationError, "CONTENT_HASH_MISMATCH"):
            self._load(digest)

    def test_expected_identity_and_pack_digest_mismatch_are_blocked(self) -> None:
        manifest = self._manifest()
        digest = self._write_manifest(manifest)
        with self.assertRaisesRegex(PackValidationError, "IDENTITY_MISMATCH"):
            self._load(digest, expected_version="1.2.4")
        with self.assertRaisesRegex(PackValidationError, "PACK_DIGEST_MISMATCH"):
            self._load("f" * 64)

    def test_core_version_incompatibility_is_blocked(self) -> None:
        manifest = self._manifest()
        manifest["public_core_compatibility"] = {"minimum": "2.0.0", "maximum_exclusive": "3.0.0"}
        digest = self._write_manifest(manifest)
        with self.assertRaisesRegex(PackValidationError, "CORE_VERSION_INCOMPATIBLE"):
            self._load(digest)

    def test_schema_whitelisted_safe_yaml_payload_loads(self) -> None:
        manifest = self._manifest()
        policy = self.pack_root / "data" / "policy.yaml"
        policy.write_text(
            json.dumps(
                {
                    "schema": "pdf-tw-localize/domain-policy/v1",
                    "entries": [
                        {
                            "id": "domain-policy-1",
                            "key": "review_boundary",
                            "value": "User acceptance stays not checked",
                            "scope": "Synthetic test",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        manifest["contents"].append(
            {
                "id": "content-policy",
                "path": "data/policy.yaml",
                "media_type": "application/yaml",
                "role": "domain_policy",
                "schema": "pdf-tw-localize/domain-policy/v1",
                "sha256": sha256_file(policy),
            }
        )
        digest = self._write_manifest(manifest)
        loaded = self._load(digest)
        self.assertEqual(loaded.contents["content-policy"]["entries"][0]["id"], "domain-policy-1")

    def test_duplicate_payload_id_is_blocked(self) -> None:
        manifest = self._manifest()
        self._write_glossary(
            self.pack_root / "data" / "glossary.csv",
            [
                ["same-id", "Indicator", "指示器", "first"],
                ["same-id", "Switch", "開關", "second"],
            ],
        )
        manifest["contents"][0]["sha256"] = sha256_file(self.pack_root / "data" / "glossary.csv")
        digest = self._write_manifest(manifest)
        with self.assertRaisesRegex(PackValidationError, "DUPLICATE_ID"):
            self._load(digest)

    def test_unknown_manifest_field_and_unknown_type_are_blocked(self) -> None:
        manifest = self._manifest()
        manifest["unexpected"] = True
        digest = self._write_manifest(manifest)
        with self.assertRaisesRegex(PackValidationError, "UNKNOWN_FIELD"):
            self._load(digest)

        self.pack_root = self.root / "unknown-type"
        manifest = self._manifest()
        manifest["contents"][0]["media_type"] = "application/octet-stream"
        digest = self._write_manifest(manifest)
        with self.assertRaisesRegex(PackValidationError, "TYPE_ROLE_MISMATCH"):
            load_domain_pack(
                self.pack_root,
                expected_pack_id="example-domain",
                expected_version="1.2.3",
                expected_digest=digest,
            )

    def test_unlisted_executable_file_is_blocked(self) -> None:
        manifest = self._manifest()
        (self.pack_root / "data" / "run.py").write_text("raise SystemExit('must never execute')\n", encoding="utf-8")
        digest = self._write_manifest(manifest)
        with self.assertRaisesRegex(PackValidationError, "INVENTORY_MISMATCH"):
            self._load(digest)

    def test_path_traversal_is_blocked_before_content_parse(self) -> None:
        manifest = self._manifest()
        outside = self.root / "outside.csv"
        self._write_glossary(outside)
        manifest["contents"][0]["path"] = "../outside.csv"
        manifest["contents"][0]["sha256"] = sha256_file(outside)
        digest = self._write_manifest(manifest)
        with self.assertRaisesRegex(PackValidationError, "PATH_TRAVERSAL"):
            self._load(digest)

    def test_symlink_content_is_blocked(self) -> None:
        manifest = self._manifest()
        external_directory = self.root / "external-data"
        external = external_directory / "glossary.csv"
        self._write_glossary(external)
        link = self.pack_root / "linked-data"
        try:
            os.symlink(external_directory, link, target_is_directory=True)
        except OSError as exc:
            if os.name != "nt":
                self.skipTest(f"symbolic-link creation unavailable: {exc}")
            result = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(external_directory)],
                text=True,
                encoding="utf-8",
                capture_output=True,
                check=False,
            )
            if result.returncode != 0:
                self.fail(f"junction creation failed: {result.stdout} {result.stderr}")
        manifest["contents"][0]["path"] = "linked-data/glossary.csv"
        manifest["contents"][0]["sha256"] = sha256_file(external)
        digest = self._write_manifest(manifest)
        with self.assertRaisesRegex(PackValidationError, "SYMLINK_ESCAPE"):
            self._load(digest)

    def test_fixed_glossary_precedence(self) -> None:
        manifest = self._manifest()
        digest = self._write_manifest(manifest)
        loaded = self._load(digest)
        public = [{"source": "Indicator", "target": "一般指示"}, {"source": "Button", "target": "按鈕"}]
        user = [{"source": "Indicator", "target": "本次指定"}]
        resolved = resolve_glossary(public, domain_pack=loaded, user_entries=user)
        self.assertEqual(resolved["indicator"]["target"], "本次指定")
        self.assertEqual(resolved["indicator"]["source_layer"], "current_user")
        without_user = resolve_glossary(public, domain_pack=loaded)
        self.assertEqual(without_user["indicator"]["target"], "指示器")
        without_pack = resolve_glossary(public)
        self.assertEqual(without_pack["indicator"]["target"], "一般指示")


if __name__ == "__main__":
    unittest.main()
