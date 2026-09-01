#!/usr/bin/env python3
"""Build and verify a deterministic, single-commit public release mirror."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import tempfile
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence


ROOT = Path(__file__).resolve().parents[1]
PROJECT_NAME = "pdf-tw-localize"
DEFAULT_VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
RELEASE_AUTHOR_NAME = "beach55607-max"
RELEASE_AUTHOR_EMAIL = "254055579+beach55607-max@users.noreply.github.com"
RELEASE_DATE = "2026-08-30T00:00:00+08:00"
ZIP_TIMESTAMP = (2026, 8, 30, 0, 0, 0)
TEXT_SUFFIXES = {"", ".csv", ".json", ".lock", ".md", ".ps1", ".py", ".toml", ".txt", ".yaml", ".yml"}


class ReleaseBuildError(RuntimeError):
    """Raised when a public mirror cannot be proven exact and clean."""


@dataclass(frozen=True)
class SourceEntry:
    path: str
    mode: str
    size: int
    sha256: str
    data: bytes


@dataclass(frozen=True)
class ReleaseResult:
    status: str
    version: str
    file_count: int
    tree_sha256: str
    source_git_tree: str | None
    public_git_tree: str
    root_commit: str
    zip_path: str
    zip_sha256: str
    checksums_path: str
    checksums_sha256: str
    manifest_path: str
    mirror_path: str


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def git_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    environment = dict(os.environ)
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    environment.update(extra or {})
    return environment


def run_git(
    repository: Path,
    *arguments: str,
    environment: dict[str, str] | None = None,
    text: bool = True,
) -> str | bytes:
    process = subprocess.run(
        [
            "git",
            "-c",
            "core.autocrlf=false",
            "-c",
            "core.safecrlf=true",
            "-c",
            "commit.gpgsign=false",
            "-C",
            str(repository),
            *arguments,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
        env=git_environment(environment),
        check=False,
    )
    if process.returncode:
        stderr = process.stderr if text else process.stderr.decode("utf-8", "replace")
        raise ReleaseBuildError(f"git {' '.join(arguments)} failed: {stderr.strip()}")
    return process.stdout.strip() if text else process.stdout


def ensure_git_root(source_root: Path) -> Path:
    source_root = source_root.resolve()
    reported = Path(str(run_git(source_root, "rev-parse", "--show-toplevel"))).resolve()
    if reported != source_root:
        raise ReleaseBuildError(f"source root must be the exact Git top-level: {reported}")
    return source_root


def require_clean_candidate(source_root: Path) -> str:
    """Return the accepted source tree only when HEAD and the worktree are exact."""
    tree, _is_draft = candidate_tree_identity(source_root, allow_draft_snapshot=False)
    return tree


def is_isolated_candidate_snapshot(source_root: Path) -> bool:
    """Recognize a disposable, detached verification clone without vendor coupling."""
    source_root = ensure_git_root(source_root)
    temporary_root = Path(tempfile.gettempdir()).resolve()
    try:
        source_root.relative_to(temporary_root)
    except ValueError:
        return False
    if source_root.name != "repository" or "candidate-snapshot-" not in source_root.parent.name:
        return False
    if str(run_git(source_root, "rev-parse", "--abbrev-ref", "HEAD")) != "HEAD":
        return False
    try:
        origin = Path(str(run_git(source_root, "config", "--get", "remote.origin.url"))).resolve()
    except ReleaseBuildError:
        return False
    return origin.is_dir() and origin != source_root


def projected_tracked_candidate_tree(source_root: Path) -> str:
    """Hash tracked draft bytes through an independent temporary Git index."""
    untracked = bytes(
        run_git(
            source_root,
            "ls-files",
            "-z",
            "--others",
            "--exclude-standard",
            text=False,
        )
    )
    if untracked:
        raise ReleaseBuildError("isolated Candidate snapshot contains untracked release paths")

    modes = index_modes(source_root)
    descriptor, temporary_index_name = tempfile.mkstemp(prefix="pdf-tw-localize-index-")
    os.close(descriptor)
    temporary_index = Path(temporary_index_name)
    temporary_index.unlink()
    environment = {"GIT_INDEX_FILE": str(temporary_index)}
    try:
        run_git(source_root, "read-tree", "HEAD", environment=environment)
        run_git(source_root, "add", "-A", "--", ".", environment=environment)
        for mode, chmod in (("100644", "-x"), ("100755", "+x")):
            paths = [path for path, entry_mode in modes.items() if entry_mode == mode and (source_root / path).is_file()]
            if paths:
                run_git(source_root, "update-index", f"--chmod={chmod}", "--", *paths, environment=environment)
        return str(run_git(source_root, "write-tree", environment=environment))
    finally:
        temporary_index.unlink(missing_ok=True)
        temporary_index.with_name(temporary_index.name + ".lock").unlink(missing_ok=True)


def clean_candidate_tree(source_root: Path) -> str | None:
    """Return HEAD's tree when tracked bytes differ only by checkout EOLs."""

    head_tree = str(run_git(source_root, "rev-parse", "HEAD^{tree}"))
    try:
        index_tree = str(run_git(source_root, "write-tree"))
    except ReleaseBuildError:
        return None
    if index_tree != head_tree:
        return None
    untracked = bytes(
        run_git(
            source_root,
            "ls-files",
            "-z",
            "--others",
            "--exclude-standard",
            text=False,
        )
    )
    if untracked:
        return None
    raw_paths = bytes(run_git(source_root, "ls-files", "-z", "--cached", text=False))
    for encoded_path in (value for value in raw_paths.split(b"\0") if value):
        relative = os.fsdecode(encoded_path).replace("\\", "/")
        path = source_root / Path(*PurePosixPath(relative).parts)
        try:
            path.resolve(strict=True).relative_to(source_root)
        except (FileNotFoundError, ValueError):
            return None
        if not path.is_file() or path.is_symlink() or os.path.islink(path):
            return None
        worktree_data = path.read_bytes()
        blob_data = bytes(run_git(source_root, "cat-file", "blob", f"HEAD:{relative}", text=False))
        if worktree_data == blob_data:
            continue
        if (
            PurePosixPath(relative).suffix.lower() in TEXT_SUFFIXES
            and worktree_data.replace(b"\r\n", b"\n") == blob_data.replace(b"\r\n", b"\n")
        ):
            continue
        return None
    return head_tree


def candidate_tree_identity(source_root: Path, *, allow_draft_snapshot: bool) -> tuple[str, bool]:
    clean_tree = clean_candidate_tree(source_root)
    if clean_tree is not None:
        return clean_tree, False
    if not allow_draft_snapshot or not is_isolated_candidate_snapshot(source_root):
        raise ReleaseBuildError("source Candidate working tree must be clean")
    return projected_tracked_candidate_tree(source_root), True


def validate_relative_path(raw_path: str) -> str:
    normalized = raw_path.replace("\\", "/")
    candidate = PurePosixPath(normalized)
    if (
        not normalized
        or candidate.is_absolute()
        or ".." in candidate.parts
        or ".git" in candidate.parts
        or ":" in normalized
    ):
        raise ReleaseBuildError(f"unsafe release path: {raw_path!r}")
    return candidate.as_posix()


def index_modes(source_root: Path) -> dict[str, str]:
    raw = bytes(run_git(source_root, "ls-files", "--stage", "-z", text=False))
    modes: dict[str, str] = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        header, encoded_path = record.split(b"\t", 1)
        mode, _object_id, stage = header.decode("ascii").split()
        if stage != "0":
            raise ReleaseBuildError(f"unmerged index entry is forbidden: {os.fsdecode(encoded_path)}")
        path = validate_relative_path(os.fsdecode(encoded_path))
        if mode not in {"100644", "100755"}:
            raise ReleaseBuildError(f"unsupported Git mode {mode} for {path}")
        modes[path] = mode
    return modes


def is_reparse_point(path: Path) -> bool:
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(flag and attributes & flag)


def source_entries(source_root: Path, *, allow_draft_snapshot: bool = False) -> list[SourceEntry]:
    source_root = ensure_git_root(source_root)
    _candidate_tree, is_draft = candidate_tree_identity(
        source_root,
        allow_draft_snapshot=allow_draft_snapshot,
    )
    raw = bytes(
        run_git(
            source_root,
            "ls-files",
            "-z",
            "--cached",
            text=False,
        )
    )
    paths = sorted(validate_relative_path(os.fsdecode(value)) for value in raw.split(b"\0") if value)
    if not paths:
        raise ReleaseBuildError("public source contains no release files")
    if len(paths) != len(set(paths)):
        raise ReleaseBuildError("duplicate release paths are forbidden")
    folded = [path.casefold() for path in paths]
    if len(folded) != len(set(folded)):
        raise ReleaseBuildError("case-colliding release paths are forbidden")

    modes = index_modes(source_root)
    entries: list[SourceEntry] = []
    for relative in paths:
        path = source_root / Path(*PurePosixPath(relative).parts)
        try:
            path.resolve(strict=True).relative_to(source_root)
        except (FileNotFoundError, ValueError) as error:
            raise ReleaseBuildError(f"release path is missing or escapes the source root: {relative}") from error
        if path.is_symlink() or os.path.islink(path) or is_reparse_point(path):
            raise ReleaseBuildError(f"links and reparse points are forbidden: {relative}")
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ReleaseBuildError(f"release entry must be a regular file: {relative}")
        data = (
            path.read_bytes()
            if is_draft
            else bytes(run_git(source_root, "cat-file", "blob", f"HEAD:{relative}", text=False))
        )
        entries.append(
            SourceEntry(
                path=relative,
                mode=modes.get(relative, "100644"),
                size=len(data),
                sha256=sha256_bytes(data),
                data=data,
            )
        )
    return entries


def canonical_tree_sha256(entries: Sequence[SourceEntry]) -> str:
    digest = hashlib.sha256()
    for entry in entries:
        digest.update(entry.path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(entry.mode.encode("ascii"))
        digest.update(b"\0")
        digest.update(str(entry.size).encode("ascii"))
        digest.update(b"\0")
        digest.update(entry.sha256.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def copy_entries(source_root: Path, destination: Path, entries: Sequence[SourceEntry]) -> None:
    for entry in entries:
        target = destination / Path(*PurePosixPath(entry.path).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(entry.data)
        if sha256_file(target) != entry.sha256:
            raise ReleaseBuildError(f"copy digest mismatch: {entry.path}")


def ensure_new_output(source_root: Path, target: Path, label: str) -> Path:
    target = target.resolve()
    try:
        target.relative_to(source_root.resolve())
    except ValueError:
        pass
    else:
        raise ReleaseBuildError(f"{label} must be outside the source repository")
    if target.exists():
        raise ReleaseBuildError(f"{label} already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.mkdir()
    return target


def initialize_public_history(mirror_root: Path, entries: Sequence[SourceEntry], version: str) -> str:
    run_git(mirror_root, "init", "--quiet", "--initial-branch=main")
    run_git(mirror_root, "add", "--all")
    executable_paths = [entry.path for entry in entries if entry.mode == "100755"]
    if executable_paths:
        run_git(mirror_root, "update-index", "--chmod=+x", "--", *executable_paths)
    subject = f"chore: publish {PROJECT_NAME} v{version}"
    identity = {
        "GIT_AUTHOR_NAME": RELEASE_AUTHOR_NAME,
        "GIT_AUTHOR_EMAIL": RELEASE_AUTHOR_EMAIL,
        "GIT_COMMITTER_NAME": RELEASE_AUTHOR_NAME,
        "GIT_COMMITTER_EMAIL": RELEASE_AUTHOR_EMAIL,
        "GIT_AUTHOR_DATE": RELEASE_DATE,
        "GIT_COMMITTER_DATE": RELEASE_DATE,
    }
    run_git(mirror_root, "commit", "--quiet", "-m", subject, environment=identity)
    return str(run_git(mirror_root, "rev-parse", "HEAD"))


def working_files(root: Path) -> list[str]:
    return sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(root).parts
    )


def verify_mirror(
    source_root: Path,
    mirror_root: Path,
    entries: Sequence[SourceEntry],
    version: str,
    *,
    expected_source_tree: str | None = None,
    allow_draft_snapshot: bool = False,
) -> tuple[str, str, str | None]:
    expected_paths = [entry.path for entry in entries]
    if working_files(mirror_root) != expected_paths:
        raise ReleaseBuildError("public mirror has extra or missing working-tree files")
    for entry in entries:
        path = mirror_root / Path(*PurePosixPath(entry.path).parts)
        if sha256_file(path) != entry.sha256:
            raise ReleaseBuildError(f"public mirror byte mismatch: {entry.path}")

    if str(run_git(mirror_root, "status", "--porcelain", "--untracked-files=all")):
        raise ReleaseBuildError("public mirror working tree is not clean")
    if str(run_git(mirror_root, "rev-list", "--all", "--count")) != "1":
        raise ReleaseBuildError("public mirror must expose exactly one reachable commit")
    parents = str(run_git(mirror_root, "show", "-s", "--format=%P", "HEAD"))
    if parents:
        raise ReleaseBuildError("public release commit must be a root commit")
    expected_subject = f"chore: publish {PROJECT_NAME} v{version}"
    metadata = str(run_git(mirror_root, "show", "-s", "--format=%an%n%ae%n%s", "HEAD")).splitlines()
    if metadata != [RELEASE_AUTHOR_NAME, RELEASE_AUTHOR_EMAIL, expected_subject]:
        raise ReleaseBuildError("public release commit identity or subject is not exact")
    refs = str(run_git(mirror_root, "for-each-ref", "--format=%(refname)")).splitlines()
    if refs != ["refs/heads/main"]:
        raise ReleaseBuildError(f"public mirror exposes unexpected refs: {refs}")

    public_tree = str(run_git(mirror_root, "rev-parse", "HEAD^{tree}"))
    source_tree, _is_draft = candidate_tree_identity(
        source_root,
        allow_draft_snapshot=allow_draft_snapshot,
    )
    if expected_source_tree is not None and source_tree != expected_source_tree:
        raise ReleaseBuildError("source Candidate changed during mirror construction")
    if source_tree != public_tree:
        raise ReleaseBuildError("source Candidate tree and public mirror tree differ")
    root_commit = str(run_git(mirror_root, "rev-parse", "HEAD"))
    return root_commit, public_tree, source_tree


def write_release_assets(
    source_root: Path,
    artifacts_root: Path,
    entries: Sequence[SourceEntry],
    version: str,
    tree_sha256: str,
    root_commit: str,
    public_git_tree: str,
) -> tuple[Path, Path, Path]:
    prefix = f"{PROJECT_NAME}-v{version}"
    zip_path = artifacts_root / f"{prefix}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for entry in entries:
            info = zipfile.ZipInfo(f"{prefix}/{entry.path}", ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (0o100755 if entry.mode == "100755" else 0o100644) << 16
            archive.writestr(info, entry.data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)

    checksums_path = artifacts_root / f"{prefix}-SHA256SUMS.txt"
    checksum_text = "".join(f"{entry.sha256}  {entry.path}\n" for entry in entries)
    checksums_path.write_text(checksum_text, encoding="utf-8", newline="\n")

    manifest_path = artifacts_root / f"{prefix}-RELEASE.json"
    manifest = {
        "schema": f"{PROJECT_NAME}/clean-public-release/v1",
        "version": version,
        "file_count": len(entries),
        "tree_sha256": tree_sha256,
        "public_git_tree": public_git_tree,
        "root_commit": root_commit,
        "zip": {"name": zip_path.name, "sha256": sha256_file(zip_path), "bytes": zip_path.stat().st_size},
        "checksums": {
            "name": checksums_path.name,
            "sha256": sha256_file(checksums_path),
            "bytes": checksums_path.stat().st_size,
        },
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return zip_path, checksums_path, manifest_path


def verify_release_assets(
    source_root: Path,
    entries: Sequence[SourceEntry],
    version: str,
    zip_path: Path,
    checksums_path: Path,
    manifest_path: Path,
) -> None:
    prefix = f"{PROJECT_NAME}-v{version}"
    expected_names = [f"{prefix}/{entry.path}" for entry in entries]
    with zipfile.ZipFile(zip_path) as archive:
        infos = archive.infolist()
        if [info.filename for info in infos] != expected_names:
            raise ReleaseBuildError("release ZIP has extra, missing, or reordered entries")
        for info, entry in zip(infos, entries, strict=True):
            if info.date_time != ZIP_TIMESTAMP:
                raise ReleaseBuildError(f"release ZIP timestamp is not deterministic: {entry.path}")
            if archive.read(info) != entry.data:
                raise ReleaseBuildError(f"release ZIP byte mismatch: {entry.path}")

    expected_checksums = "".join(f"{entry.sha256}  {entry.path}\n" for entry in entries)
    if checksums_path.read_text(encoding="utf-8") != expected_checksums:
        raise ReleaseBuildError("release SHA256SUMS content mismatch")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest["zip"]["sha256"] != sha256_file(zip_path):
        raise ReleaseBuildError("release ZIP digest mismatch")
    if manifest["checksums"]["sha256"] != sha256_file(checksums_path):
        raise ReleaseBuildError("release checksum-file digest mismatch")


def build_release(
    source_root: Path,
    mirror_root: Path,
    artifacts_root: Path,
    version: str,
    *,
    allow_draft_snapshot: bool = False,
) -> ReleaseResult:
    source_root = ensure_git_root(source_root)
    if not version or version != (source_root / "VERSION").read_text(encoding="utf-8").strip():
        raise ReleaseBuildError("requested release version must exactly match VERSION")
    expected_source_tree, is_draft = candidate_tree_identity(
        source_root,
        allow_draft_snapshot=allow_draft_snapshot,
    )
    entries = source_entries(source_root, allow_draft_snapshot=allow_draft_snapshot)
    mirror_root = ensure_new_output(source_root, mirror_root, "mirror output")
    artifacts_root = ensure_new_output(source_root, artifacts_root, "artifact output")
    tree_sha256 = canonical_tree_sha256(entries)
    copy_entries(source_root, mirror_root, entries)
    initialize_public_history(mirror_root, entries, version)
    root_commit, public_git_tree, source_git_tree = verify_mirror(
        source_root,
        mirror_root,
        entries,
        version,
        expected_source_tree=expected_source_tree,
        allow_draft_snapshot=allow_draft_snapshot,
    )
    zip_path, checksums_path, manifest_path = write_release_assets(
        mirror_root,
        artifacts_root,
        entries,
        version,
        tree_sha256,
        root_commit,
        public_git_tree,
    )
    verify_release_assets(
        mirror_root,
        entries,
        version,
        zip_path,
        checksums_path,
        manifest_path,
    )
    return ReleaseResult(
        status="DRAFT_VERIFIED" if is_draft else "PASS",
        version=version,
        file_count=len(entries),
        tree_sha256=tree_sha256,
        source_git_tree=source_git_tree,
        public_git_tree=public_git_tree,
        root_commit=root_commit,
        zip_path=str(zip_path),
        zip_sha256=sha256_file(zip_path),
        checksums_path=str(checksums_path),
        checksums_sha256=sha256_file(checksums_path),
        manifest_path=str(manifest_path),
        mirror_path=str(mirror_root),
    )


def verify_only(source_root: Path, version: str) -> ReleaseResult:
    allow_draft_snapshot = is_isolated_candidate_snapshot(source_root)
    with tempfile.TemporaryDirectory(prefix="pdf-tw-localize-public-release-") as temporary:
        root = Path(temporary)
        return build_release(
            source_root,
            root / "mirror",
            root / "artifacts",
            version,
            allow_draft_snapshot=allow_draft_snapshot,
        )


def parse_args(arguments: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--version", default=DEFAULT_VERSION)
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--artifacts-dir", type=Path)
    return parser.parse_args(arguments)


def main(arguments: Iterable[str] | None = None) -> int:
    args = parse_args(arguments)
    try:
        if args.verify_only:
            if args.output_root or args.artifacts_dir:
                raise ReleaseBuildError("--verify-only cannot be combined with output paths")
            result = verify_only(args.source_root, args.version)
        else:
            if not args.output_root or not args.artifacts_dir:
                raise ReleaseBuildError("build mode requires --output-root and --artifacts-dir")
            result = build_release(args.source_root, args.output_root, args.artifacts_dir, args.version)
    except (OSError, ReleaseBuildError, subprocess.SubprocessError, zipfile.BadZipFile, json.JSONDecodeError) as error:
        print(
            json.dumps(
                {
                    "schema": f"{PROJECT_NAME}/clean-public-release-result/v1",
                    "status": "BLOCKED",
                    "error": str(error),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2
    print(
        json.dumps(
            {
                "schema": f"{PROJECT_NAME}/clean-public-release-result/v1",
                **asdict(result),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
