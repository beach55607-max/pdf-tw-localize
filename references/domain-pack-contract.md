# Explicit data-only domain-pack contract

Domain packs extend terminology and regression data without changing public-core code. They are optional. The public core remains usable without one.

## Identity and loading

Use `scripts/domain_pack.py::load_domain_pack` with all four caller-supplied values:

- exact pack directory path;
- expected `pack_id`;
- expected `version`;
- expected `pack_digest`.

The loader has no discovery fallback. Do not scan sibling folders, user profiles, registries, environment paths, or network shares. The manifest must declare `explicit_path_only`, `auto_discovery: false`, `require_expected_identity: true`, and `data_only: true`.

`pack_digest` is SHA-256 over canonical UTF-8 JSON of the complete manifest with only the `pack_digest` field omitted. Canonical JSON uses sorted keys, no extra whitespace, and unescaped Unicode. Every listed content file also carries its own SHA-256.

## Allowed content

The v1 loader accepts only schema-whitelisted roles and types:

- `glossary`: UTF-8 CSV with exact columns `id,source,target,notes`;
- `protected_names`: UTF-8 JSON;
- `english_allowlist_policy`: UTF-8 JSON;
- `regression_index`: UTF-8 JSON;
- `domain_policy`: UTF-8 JSON or safe YAML. YAML is lazy and requires PyYAML; absence is a fail-closed `YAML_RUNTIME_MISSING`, not a fallback parser.

The pack manifest is `pack.json`. Every other file must be listed exactly once. Executables, PDFs, archives, images, bytecode, caches, unknown extensions, unknown fields, and unlisted files are rejected.

## Fail-closed checks

Reject the whole pack on:

- path traversal, absolute/content-controlled paths, backslash aliases, or colon-bearing paths;
- a symlink, junction, or other reparse point at the root or within the tree;
- missing or extra files;
- unknown manifest, descriptor, or payload fields;
- unknown roles, schemas, media types, or role/type combinations;
- duplicate content IDs, payload IDs, content paths, or glossary terms within one layer;
- content hash, canonical pack digest, expected identity, or core compatibility mismatch;
- malformed source/candidate identity records or user-history references.

The executable contract is authoritative. [domain-pack.schema.json](../schemas/domain-pack.schema.json) documents the manifest shape for independent review.

## Terminology precedence and missing-pack behavior

`resolve_glossary` applies layers in this order, with later layers overriding earlier ones:

1. public general Taiwan glossary;
2. explicitly loaded domain pack;
3. current-run user glossary.

Therefore the effective priority is current user > explicit domain pack > public general glossary.

With no pack, public terminology and general PDF localization continue. `domain_validation_state(None)` returns `NOT_CHECKED`. A caller must not retain, infer, or import a prior domain PASS.
