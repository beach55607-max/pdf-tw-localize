from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from build_clean_public_mirror import (  # noqa: E402
    RELEASE_AUTHOR_EMAIL,
    RELEASE_AUTHOR_NAME,
    ReleaseBuildError,
    build_release,
    sha256_file,
    source_entries,
    verify_mirror,
    verify_only,
)


def git(repository: Path, *arguments: str) -> str:
    environment = dict(os.environ)
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
            "GIT_AUTHOR_DATE": "2026-08-30T00:00:00+08:00",
            "GIT_COMMITTER_DATE": "2026-08-30T00:00:00+08:00",
        }
    )
    completed = subprocess.run(
        ["git", "-c", "core.autocrlf=false", "-C", str(repository), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
        check=False,
    )
    if completed.returncode:
        raise AssertionError(completed.stderr)
    return completed.stdout.strip()


def make_source(root: Path) -> Path:
    root.mkdir()
    git(root, "init", "--quiet", "--initial-branch=main")
    (root / "VERSION").write_text("0.1.0\n", encoding="utf-8", newline="\n")
    (root / "README.md").write_text("# Fixture\n", encoding="utf-8", newline="\n")
    scripts = root / "scripts"
    scripts.mkdir()
    (scripts / "tool.py").write_text("print('fixture')\n", encoding="utf-8", newline="\n")
    git(root, "add", "--all")
    git(root, "commit", "--quiet", "-m", "fixture source")
    return root


class CleanPublicMirrorTest(unittest.TestCase):
    def test_build_is_deterministic_single_root_and_tree_exact(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            source = make_source(base / "source")
            first = build_release(source, base / "mirror-1", base / "artifacts-1", "0.1.0")
            second = build_release(source, base / "mirror-2", base / "artifacts-2", "0.1.0")

            self.assertEqual(first.root_commit, second.root_commit)
            self.assertEqual(first.public_git_tree, first.source_git_tree)
            self.assertEqual(first.public_git_tree, second.public_git_tree)
            self.assertEqual(first.tree_sha256, second.tree_sha256)
            self.assertEqual(first.zip_sha256, second.zip_sha256)
            self.assertEqual(first.checksums_sha256, second.checksums_sha256)
            self.assertEqual(git(Path(first.mirror_path), "rev-list", "--all", "--count"), "1")
            self.assertEqual(git(Path(first.mirror_path), "show", "-s", "--format=%P", "HEAD"), "")
            metadata = git(
                Path(first.mirror_path),
                "show",
                "-s",
                "--format=%an%n%ae%n%s",
                "HEAD",
            ).splitlines()
            self.assertEqual(
                metadata,
                [
                    RELEASE_AUTHOR_NAME,
                    RELEASE_AUTHOR_EMAIL,
                    "chore: publish pdf-tw-localize v0.1.0",
                ],
            )

    def test_existing_output_is_rejected_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            source = make_source(base / "source")
            existing = base / "existing"
            existing.mkdir()
            marker = existing / "keep.txt"
            marker.write_text("keep\n", encoding="utf-8")
            with self.assertRaisesRegex(ReleaseBuildError, "already exists"):
                build_release(source, existing, base / "artifacts", "0.1.0")
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep\n")

    def test_deleted_tracked_file_makes_source_dirty_and_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            source = make_source(Path(raw) / "source")
            (source / "README.md").unlink()
            with self.assertRaisesRegex(ReleaseBuildError, "working tree must be clean"):
                source_entries(source)

    def test_modified_tracked_source_is_rejected_without_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            source = make_source(base / "source")
            (source / "README.md").write_text("modified\n", encoding="utf-8", newline="\n")
            mirror = base / "mirror"
            artifacts = base / "artifacts"
            with self.assertRaisesRegex(ReleaseBuildError, "working tree must be clean"):
                build_release(source, mirror, artifacts, "0.1.0")
            self.assertFalse(mirror.exists())
            self.assertFalse(artifacts.exists())

    def test_untracked_source_file_is_rejected_without_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            source = make_source(base / "source")
            (source / "private-note.txt").write_text("must not ship\n", encoding="utf-8", newline="\n")
            mirror = base / "mirror"
            artifacts = base / "artifacts"
            with self.assertRaisesRegex(ReleaseBuildError, "working tree must be clean"):
                build_release(source, mirror, artifacts, "0.1.0")
            self.assertFalse(mirror.exists())
            self.assertFalse(artifacts.exists())

    def test_dirty_isolated_candidate_snapshot_is_projected_but_not_accepted(self) -> None:
        with tempfile.TemporaryDirectory(prefix="candidate-snapshot-fixture-") as raw:
            base = Path(raw)
            origin = make_source(base / "origin")
            snapshot = base / "repository"
            git(base, "clone", "--quiet", "--no-hardlinks", str(origin), str(snapshot))
            git(snapshot, "checkout", "--quiet", "--detach", "HEAD")
            (snapshot / "README.md").write_text("draft\n", encoding="utf-8", newline="\n")

            result = verify_only(snapshot, "0.1.0")

            self.assertEqual(result.status, "DRAFT_VERIFIED")
            self.assertEqual(result.source_git_tree, result.public_git_tree)
            with self.assertRaisesRegex(ReleaseBuildError, "working tree must be clean"):
                build_release(snapshot, base / "mirror", base / "artifacts", "0.1.0")

    def test_untracked_isolated_candidate_snapshot_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="candidate-snapshot-fixture-") as raw:
            base = Path(raw)
            origin = make_source(base / "origin")
            snapshot = base / "repository"
            git(base, "clone", "--quiet", "--no-hardlinks", str(origin), str(snapshot))
            git(snapshot, "checkout", "--quiet", "--detach", "HEAD")
            (snapshot / "private-note.txt").write_text("must not ship\n", encoding="utf-8", newline="\n")

            with self.assertRaisesRegex(ReleaseBuildError, "untracked release paths"):
                verify_only(snapshot, "0.1.0")

    def test_tampered_mirror_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            source = make_source(base / "source")
            result = build_release(source, base / "mirror", base / "artifacts", "0.1.0")
            mirror = Path(result.mirror_path)
            (mirror / "README.md").write_text("tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(ReleaseBuildError, "byte mismatch"):
                verify_mirror(source, mirror, source_entries(source), "0.1.0")

    def test_extra_reachable_commit_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            source = make_source(base / "source")
            result = build_release(source, base / "mirror", base / "artifacts", "0.1.0")
            mirror = Path(result.mirror_path)
            git(mirror, "commit", "--quiet", "--allow-empty", "-m", "second commit")
            with self.assertRaisesRegex(ReleaseBuildError, "exactly one reachable commit"):
                verify_mirror(source, mirror, source_entries(source), "0.1.0")

    def test_release_assets_are_nonempty_and_hash_bound(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            source = make_source(base / "source")
            result = build_release(source, base / "mirror", base / "artifacts", "0.1.0")
            zip_path = Path(result.zip_path)
            checksums_path = Path(result.checksums_path)
            self.assertGreater(zip_path.stat().st_size, 0)
            self.assertEqual(sha256_file(zip_path), result.zip_sha256)
            self.assertEqual(sha256_file(checksums_path), result.checksums_sha256)


if __name__ == "__main__":
    unittest.main()
