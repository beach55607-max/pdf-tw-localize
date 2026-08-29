#!/usr/bin/env python3
"""Explicit, digest-bound loader for data-only localization domain packs."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping


PACK_SCHEMA = "pdf-tw-localize/domain-pack/v1"
CORE_API_VERSION = "1.0.0"
PACK_MANIFEST_NAME = "pack.json"
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
SEMVER_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")

MANIFEST_KEYS = {
    "schema",
    "pack_id",
    "version",
    "schema_version",
    "public_core_compatibility",
    "contents",
    "pack_digest",
    "provenance",
    "scope",
    "load_policy",
}
CONTENT_KEYS = {"id", "path", "media_type", "role", "schema", "sha256"}
ROLE_RULES = {
    "glossary": {
        "media_types": {"text/csv"},
        "schemas": {"pdf-tw-localize/glossary/v1"},
    },
    "protected_names": {
        "media_types": {"application/json"},
        "schemas": {"pdf-tw-localize/protected-names/v1"},
    },
    "english_allowlist_policy": {
        "media_types": {"application/json"},
        "schemas": {"pdf-tw-localize/english-allowlist-policy/v1"},
    },
    "regression_index": {
        "media_types": {"application/json"},
        "schemas": {"pdf-tw-localize/regression-index/v1"},
    },
    "domain_policy": {
        "media_types": {"application/json", "application/yaml"},
        "schemas": {"pdf-tw-localize/domain-policy/v1"},
    },
}
MEDIA_EXTENSIONS = {
    "application/json": {".json"},
    "application/yaml": {".yaml", ".yml"},
    "text/csv": {".csv"},
}


class PackValidationError(ValueError):
    """Fail-closed pack validation error with a stable machine code."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class LoadedDomainPack:
    root: Path
    manifest: Mapping[str, Any]
    contents: Mapping[str, Any]
    glossary_entries: tuple[Mapping[str, str], ...]
    digest: str

    @property
    def identity(self) -> tuple[str, str, str]:
        return (
            str(self.manifest["pack_id"]),
            str(self.manifest["version"]),
            self.digest,
        )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_pack_digest(manifest: Mapping[str, Any]) -> str:
    payload = {key: value for key, value in manifest.items() if key != "pack_digest"}
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _ensure_mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PackValidationError("TYPE_MISMATCH", f"{context} must be an object")
    return value


def _ensure_exact_keys(
    value: Mapping[str, Any], required: set[str], context: str
) -> None:
    keys = set(value)
    missing = sorted(required - keys)
    unknown = sorted(keys - required)
    if missing:
        raise PackValidationError("MISSING_FIELD", f"{context} missing fields: {missing}")
    if unknown:
        raise PackValidationError("UNKNOWN_FIELD", f"{context} unknown fields: {unknown}")


def _semver(value: Any, context: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or not SEMVER_RE.fullmatch(value):
        raise PackValidationError("INVALID_VERSION", f"{context} is not strict SemVer")
    return tuple(int(part) for part in value.split("."))


def _require_text(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PackValidationError("INVALID_TEXT", f"{context} must be nonempty text")
    return value


def _require_sha256(value: Any, context: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise PackValidationError("INVALID_SHA256", f"{context} must be a SHA-256 hex digest")
    return value.lower()


def _is_reparse_point(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(os.path, "isjunction", None)
    if is_junction is not None and is_junction(path):
        return True
    try:
        attributes = int(getattr(path.lstat(), "st_file_attributes", 0))
    except OSError:
        return False
    marker = int(getattr(__import__("stat"), "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(attributes & marker)


def _validate_relative_path(value: Any, context: str) -> tuple[str, ...]:
    text = _require_text(value, context)
    if "\\" in text or ":" in text:
        raise PackValidationError("PATH_TRAVERSAL", f"{context} is not a portable relative path")
    pure = PurePosixPath(text)
    parts = pure.parts
    if pure.is_absolute() or not parts or any(part in {"", ".", ".."} for part in parts):
        raise PackValidationError("PATH_TRAVERSAL", f"{context} escapes or aliases the pack root")
    return parts


def _resolve_content_path(root: Path, relative: Any, context: str) -> Path:
    parts = _validate_relative_path(relative, context)
    current = root
    for part in parts:
        current = current / part
        if not current.exists():
            raise PackValidationError("CONTENT_MISSING", f"{context} does not exist")
        if _is_reparse_point(current):
            raise PackValidationError("SYMLINK_ESCAPE", f"{context} crosses a reparse point")
    resolved_root = root.resolve(strict=True)
    resolved = current.resolve(strict=True)
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise PackValidationError("PATH_TRAVERSAL", f"{context} resolves outside pack root") from exc
    if not resolved.is_file():
        raise PackValidationError("CONTENT_NOT_FILE", f"{context} must resolve to a regular file")
    return resolved


def _read_json(path: Path, context: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PackValidationError("INVALID_JSON", f"{context} is not valid UTF-8 JSON") from exc


def _read_yaml(path: Path, context: str) -> Any:
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError as exc:
        raise PackValidationError(
            "YAML_RUNTIME_MISSING",
            "PyYAML is required only when an explicitly declared YAML payload is present",
        ) from exc
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise PackValidationError("INVALID_YAML", f"{context} is not safe UTF-8 YAML") from exc


def _register_id(identifier: Any, seen: set[str], context: str) -> str:
    text = _require_text(identifier, f"{context}.id")
    if text in seen:
        raise PackValidationError("DUPLICATE_ID", f"duplicate ID {text!r}")
    seen.add(text)
    return text


def _validate_glossary(path: Path, seen: set[str]) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames != ["id", "source", "target", "notes"]:
                raise PackValidationError(
                    "UNKNOWN_FIELD",
                    "glossary header must be exactly id,source,target,notes",
                )
            rows = list(reader)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise PackValidationError("INVALID_CSV", "glossary is not valid UTF-8 CSV") from exc
    if not rows:
        raise PackValidationError("EMPTY_CONTENT", "glossary must contain at least one row")
    normalized_sources: set[str] = set()
    result: list[dict[str, str]] = []
    for index, row in enumerate(rows, start=2):
        context = f"glossary row {index}"
        _register_id(row.get("id"), seen, context)
        source = _require_text(row.get("source"), f"{context}.source")
        target = _require_text(row.get("target"), f"{context}.target")
        notes = row.get("notes")
        if not isinstance(notes, str):
            raise PackValidationError("TYPE_MISMATCH", f"{context}.notes must be text")
        key = source.casefold()
        if key in normalized_sources:
            raise PackValidationError("DUPLICATE_TERM", f"duplicate glossary source {source!r}")
        normalized_sources.add(key)
        result.append({"id": row["id"], "source": source, "target": target, "notes": notes})
    return result


def _validate_protected_names(data: Any, seen: set[str]) -> None:
    root = _ensure_mapping(data, "protected_names")
    _ensure_exact_keys(root, {"schema", "entries"}, "protected_names")
    if root["schema"] != "pdf-tw-localize/protected-names/v1":
        raise PackValidationError("SCHEMA_MISMATCH", "protected_names schema mismatch")
    if not isinstance(root["entries"], list):
        raise PackValidationError("TYPE_MISMATCH", "protected_names.entries must be a list")
    for index, raw in enumerate(root["entries"]):
        entry = _ensure_mapping(raw, f"protected_names.entries[{index}]")
        _ensure_exact_keys(entry, {"id", "text", "type", "reason", "scope"}, f"protected_names.entries[{index}]")
        _register_id(entry["id"], seen, f"protected_names.entries[{index}]")
        for key in ("text", "type", "reason", "scope"):
            _require_text(entry[key], f"protected_names.entries[{index}].{key}")


def _validate_allowlist_policy(data: Any, seen: set[str]) -> None:
    root = _ensure_mapping(data, "english_allowlist_policy")
    _ensure_exact_keys(root, {"schema", "allowed_types", "entries"}, "english_allowlist_policy")
    if root["schema"] != "pdf-tw-localize/english-allowlist-policy/v1":
        raise PackValidationError("SCHEMA_MISMATCH", "english_allowlist_policy schema mismatch")
    if not isinstance(root["allowed_types"], list) or not root["allowed_types"]:
        raise PackValidationError("TYPE_MISMATCH", "allowed_types must be a nonempty list")
    allowed_types = {_require_text(value, "allowed_types[]") for value in root["allowed_types"]}
    if not isinstance(root["entries"], list):
        raise PackValidationError("TYPE_MISMATCH", "english_allowlist_policy.entries must be a list")
    for index, raw in enumerate(root["entries"]):
        entry = _ensure_mapping(raw, f"english_allowlist_policy.entries[{index}]")
        _ensure_exact_keys(entry, {"id", "text", "type", "rule", "scope", "basis"}, f"english_allowlist_policy.entries[{index}]")
        _register_id(entry["id"], seen, f"english_allowlist_policy.entries[{index}]")
        for key in ("text", "type", "rule", "scope", "basis"):
            _require_text(entry[key], f"english_allowlist_policy.entries[{index}].{key}")
        if entry["type"] not in allowed_types:
            raise PackValidationError("UNKNOWN_ALLOWLIST_TYPE", f"entry type {entry['type']!r} is not declared")


def _validate_regression_index(data: Any, seen: set[str]) -> None:
    root = _ensure_mapping(data, "regression_index")
    _ensure_exact_keys(root, {"schema", "entries"}, "regression_index")
    if root["schema"] != "pdf-tw-localize/regression-index/v1":
        raise PackValidationError("SCHEMA_MISMATCH", "regression_index schema mismatch")
    if not isinstance(root["entries"], list):
        raise PackValidationError("TYPE_MISMATCH", "regression_index.entries must be a list")
    for index, raw in enumerate(root["entries"]):
        context = f"regression_index.entries[{index}]"
        entry = _ensure_mapping(raw, context)
        _ensure_exact_keys(entry, {"id", "source", "candidates", "expected_contracts", "user_history"}, context)
        _register_id(entry["id"], seen, context)
        source = _ensure_mapping(entry["source"], f"{context}.source")
        _ensure_exact_keys(source, {"document_id", "sha256", "locator", "pages"}, f"{context}.source")
        _require_text(source["document_id"], f"{context}.source.document_id")
        _require_sha256(source["sha256"], f"{context}.source.sha256")
        _require_text(source["locator"], f"{context}.source.locator")
        if not isinstance(source["pages"], list) or not all(isinstance(page, int) and page > 0 for page in source["pages"]):
            raise PackValidationError("TYPE_MISMATCH", f"{context}.source.pages must contain positive integers")
        if not isinstance(entry["expected_contracts"], list) or not entry["expected_contracts"]:
            raise PackValidationError("TYPE_MISMATCH", f"{context}.expected_contracts must be nonempty")
        for contract in entry["expected_contracts"]:
            _require_text(contract, f"{context}.expected_contracts[]")
        if not isinstance(entry["candidates"], list) or not isinstance(entry["user_history"], list):
            raise PackValidationError("TYPE_MISMATCH", f"{context} candidate/history fields must be lists")
        candidate_ids: set[str] = set()
        for candidate_index, raw_candidate in enumerate(entry["candidates"]):
            candidate_context = f"{context}.candidates[{candidate_index}]"
            candidate = _ensure_mapping(raw_candidate, candidate_context)
            _ensure_exact_keys(candidate, {"id", "sha256", "locator", "status"}, candidate_context)
            candidate_id = _register_id(candidate["id"], seen, candidate_context)
            candidate_ids.add(candidate_id)
            _require_sha256(candidate["sha256"], f"{candidate_context}.sha256")
            _require_text(candidate["locator"], f"{candidate_context}.locator")
            _require_text(candidate["status"], f"{candidate_context}.status")
        for history_index, raw_history in enumerate(entry["user_history"]):
            history_context = f"{context}.user_history[{history_index}]"
            history = _ensure_mapping(raw_history, history_context)
            _ensure_exact_keys(history, {"id", "candidate_id", "status", "recorded_at", "note"}, history_context)
            _register_id(history["id"], seen, history_context)
            if _require_text(history["candidate_id"], f"{history_context}.candidate_id") not in candidate_ids:
                raise PackValidationError("UNKNOWN_REFERENCE", f"{history_context} references an unknown candidate")
            for key in ("status", "recorded_at", "note"):
                _require_text(history[key], f"{history_context}.{key}")


def _validate_domain_policy(data: Any, seen: set[str]) -> None:
    root = _ensure_mapping(data, "domain_policy")
    _ensure_exact_keys(root, {"schema", "entries"}, "domain_policy")
    if root["schema"] != "pdf-tw-localize/domain-policy/v1":
        raise PackValidationError("SCHEMA_MISMATCH", "domain_policy schema mismatch")
    if not isinstance(root["entries"], list):
        raise PackValidationError("TYPE_MISMATCH", "domain_policy.entries must be a list")
    for index, raw in enumerate(root["entries"]):
        context = f"domain_policy.entries[{index}]"
        entry = _ensure_mapping(raw, context)
        _ensure_exact_keys(entry, {"id", "key", "value", "scope"}, context)
        _register_id(entry["id"], seen, context)
        for key in ("key", "value", "scope"):
            _require_text(entry[key], f"{context}.{key}")


def _parse_content(path: Path, descriptor: Mapping[str, Any], seen: set[str]) -> Any:
    media_type = descriptor["media_type"]
    role = descriptor["role"]
    if media_type == "text/csv":
        return _validate_glossary(path, seen)
    if media_type == "application/json":
        data = _read_json(path, descriptor["id"])
    elif media_type == "application/yaml":
        data = _read_yaml(path, descriptor["id"])
    else:
        raise PackValidationError("UNKNOWN_MEDIA_TYPE", f"unsupported media type {media_type!r}")
    if role == "protected_names":
        _validate_protected_names(data, seen)
    elif role == "english_allowlist_policy":
        _validate_allowlist_policy(data, seen)
    elif role == "regression_index":
        _validate_regression_index(data, seen)
    elif role == "domain_policy":
        _validate_domain_policy(data, seen)
    else:
        raise PackValidationError("UNKNOWN_ROLE", f"unsupported role {role!r}")
    return data


def load_domain_pack(
    pack_path: str | os.PathLike[str],
    *,
    expected_pack_id: str,
    expected_version: str,
    expected_digest: str,
    core_version: str = CORE_API_VERSION,
) -> LoadedDomainPack:
    """Load one exact pack path; there is intentionally no discovery fallback."""

    root = Path(pack_path)
    if not root.exists() or not root.is_dir():
        raise PackValidationError("PACK_MISSING", "explicit pack path is not a directory")
    if _is_reparse_point(root):
        raise PackValidationError("SYMLINK_ESCAPE", "pack root must not be a reparse point")
    root = root.resolve(strict=True)
    manifest_path = root / PACK_MANIFEST_NAME
    if not manifest_path.is_file() or _is_reparse_point(manifest_path):
        raise PackValidationError("MANIFEST_MISSING", f"{PACK_MANIFEST_NAME} is missing or unsafe")
    manifest = _ensure_mapping(_read_json(manifest_path, "pack manifest"), "pack manifest")
    _ensure_exact_keys(manifest, MANIFEST_KEYS, "pack manifest")
    if manifest["schema"] != PACK_SCHEMA or manifest["schema_version"] != "1.0.0":
        raise PackValidationError("SCHEMA_MISMATCH", "pack schema is incompatible")
    pack_id = _require_text(manifest["pack_id"], "pack_id")
    version = _require_text(manifest["version"], "version")
    _semver(version, "version")
    if pack_id != expected_pack_id or version != expected_version:
        raise PackValidationError("IDENTITY_MISMATCH", "pack ID or version differs from the expected identity")
    expected_digest_normalized = _require_sha256(expected_digest, "expected_digest")
    declared_digest = _require_sha256(manifest["pack_digest"], "pack_digest")
    computed_digest = canonical_pack_digest(manifest)
    if declared_digest != computed_digest or declared_digest != expected_digest_normalized:
        raise PackValidationError("PACK_DIGEST_MISMATCH", "declared, computed, or expected digest differs")

    compatibility = _ensure_mapping(manifest["public_core_compatibility"], "public_core_compatibility")
    _ensure_exact_keys(compatibility, {"minimum", "maximum_exclusive"}, "public_core_compatibility")
    core = _semver(core_version, "core_version")
    minimum = _semver(compatibility["minimum"], "public_core_compatibility.minimum")
    maximum = _semver(compatibility["maximum_exclusive"], "public_core_compatibility.maximum_exclusive")
    if not (minimum <= core < maximum):
        raise PackValidationError("CORE_VERSION_INCOMPATIBLE", "public core version is outside the pack range")

    scope = _ensure_mapping(manifest["scope"], "scope")
    _ensure_exact_keys(scope, {"locale", "domain", "purpose"}, "scope")
    for key in ("locale", "domain", "purpose"):
        _require_text(scope[key], f"scope.{key}")
    if scope["locale"] != "zh-TW":
        raise PackValidationError("SCOPE_MISMATCH", "pack locale must be zh-TW")

    provenance = _ensure_mapping(manifest["provenance"], "provenance")
    _ensure_exact_keys(provenance, {"owner", "created_at", "source_basis", "confidentiality"}, "provenance")
    for key in ("owner", "created_at", "source_basis", "confidentiality"):
        _require_text(provenance[key], f"provenance.{key}")

    load_policy = _ensure_mapping(manifest["load_policy"], "load_policy")
    _ensure_exact_keys(load_policy, {"mode", "auto_discovery", "require_expected_identity", "data_only"}, "load_policy")
    if load_policy != {
        "mode": "explicit_path_only",
        "auto_discovery": False,
        "require_expected_identity": True,
        "data_only": True,
    }:
        raise PackValidationError("LOAD_POLICY_MISMATCH", "pack must require explicit, identity-bound, data-only loading")

    descriptors = manifest["contents"]
    if not isinstance(descriptors, list) or not descriptors:
        raise PackValidationError("EMPTY_CONTENT", "contents must be a nonempty list")
    descriptor_ids: set[str] = set()
    content_paths: set[str] = set()
    global_ids: set[str] = set()
    parsed: dict[str, Any] = {}
    glossary_entries: list[Mapping[str, str]] = []
    for index, raw_descriptor in enumerate(descriptors):
        context = f"contents[{index}]"
        descriptor = _ensure_mapping(raw_descriptor, context)
        _ensure_exact_keys(descriptor, CONTENT_KEYS, context)
        descriptor_id = _require_text(descriptor["id"], f"{context}.id")
        if descriptor_id in descriptor_ids:
            raise PackValidationError("DUPLICATE_ID", f"duplicate content ID {descriptor_id!r}")
        descriptor_ids.add(descriptor_id)
        role = _require_text(descriptor["role"], f"{context}.role")
        media_type = _require_text(descriptor["media_type"], f"{context}.media_type")
        schema = _require_text(descriptor["schema"], f"{context}.schema")
        rules = ROLE_RULES.get(role)
        if rules is None:
            raise PackValidationError("UNKNOWN_ROLE", f"unknown content role {role!r}")
        if media_type not in rules["media_types"] or schema not in rules["schemas"]:
            raise PackValidationError("TYPE_ROLE_MISMATCH", f"{context} media type or schema is not allowed for role")
        path_text = _require_text(descriptor["path"], f"{context}.path")
        if path_text in content_paths:
            raise PackValidationError("DUPLICATE_PATH", f"duplicate content path {path_text!r}")
        content_paths.add(path_text)
        path = _resolve_content_path(root, path_text, f"{context}.path")
        if path.suffix.lower() not in MEDIA_EXTENSIONS[media_type]:
            raise PackValidationError("UNKNOWN_FILE_TYPE", f"{context} extension is not allowed for media type")
        declared_hash = _require_sha256(descriptor["sha256"], f"{context}.sha256")
        actual_hash = sha256_file(path)
        if declared_hash != actual_hash:
            raise PackValidationError("CONTENT_HASH_MISMATCH", f"{context} SHA-256 mismatch")
        value = _parse_content(path, descriptor, global_ids)
        parsed[descriptor_id] = value
        if role == "glossary":
            glossary_entries.extend(value)

    actual_files: set[str] = set()
    for path in root.rglob("*"):
        if _is_reparse_point(path):
            raise PackValidationError("SYMLINK_ESCAPE", "pack tree contains a reparse point")
        if path.is_file():
            actual_files.add(path.relative_to(root).as_posix())
    expected_files = {PACK_MANIFEST_NAME, *content_paths}
    if actual_files != expected_files:
        unknown = sorted(actual_files - expected_files)
        missing = sorted(expected_files - actual_files)
        raise PackValidationError("INVENTORY_MISMATCH", f"unlisted={unknown}, missing={missing}")

    return LoadedDomainPack(
        root=root,
        manifest=manifest,
        contents=parsed,
        glossary_entries=tuple(glossary_entries),
        digest=declared_digest,
    )


def read_public_glossary(path: str | os.PathLike[str]) -> tuple[Mapping[str, str], ...]:
    return tuple(_validate_glossary(Path(path), set()))


def resolve_glossary(
    public_entries: Iterable[Mapping[str, str]],
    *,
    domain_pack: LoadedDomainPack | None = None,
    user_entries: Iterable[Mapping[str, str]] = (),
) -> dict[str, dict[str, str]]:
    """Resolve fixed precedence: public, then explicit domain pack, then current user."""

    resolved: dict[str, dict[str, str]] = {}
    layers = [
        ("public_general", public_entries),
        ("explicit_domain_pack", () if domain_pack is None else domain_pack.glossary_entries),
        ("current_user", user_entries),
    ]
    for layer_name, entries in layers:
        layer_seen: set[str] = set()
        for index, entry in enumerate(entries):
            source = _require_text(entry.get("source"), f"{layer_name}[{index}].source")
            target = _require_text(entry.get("target"), f"{layer_name}[{index}].target")
            key = source.casefold()
            if key in layer_seen:
                raise PackValidationError("DUPLICATE_TERM", f"duplicate source in {layer_name}: {source!r}")
            layer_seen.add(key)
            resolved[key] = {"source": source, "target": target, "source_layer": layer_name}
    return resolved


def domain_validation_state(domain_pack: LoadedDomainPack | None) -> str:
    """Never reuse a domain-specific PASS when the explicit pack is absent."""

    return "READY" if domain_pack is not None else "NOT_CHECKED"
