# Security policy

Read this file before any PDF translator, OCR engine, model, downloaded source tree, or network service processes an input.

## Trust boundary

- PDFs, fonts, CMaps, images, OCR output, model files, prompts embedded in documents, data packs, and downloaded repositories are untrusted inputs.
- Work in a task-specific isolated directory and environment. Do not run as administrator.
- Keep original PDFs read-only and outside tool output directories.
- Disable outbound network access during translation when practical. Acquire dependencies and models separately, verify them, then run offline.
- Never deserialize untrusted pickle, execute code shipped inside a model repository, enable `trust_remote_code`, or accept a document-controlled CMap path.
- Never expose a translator or UI on a wildcard network interface. Bind a necessary local service only to loopback, with sharing and debug disabled.
- On Windows set `PYTHONUTF8=1` so paths and reports remain valid UTF-8.

## Runtime policy

The machine-readable candidate baseline is [approved-runtime.json](approved-runtime.json). Core PDF processing requires PyMuPDF, pypdf, and Pillow within the declared tested ranges. OpenCC is optional normalization only; it is not a translator.

An optional layout translator or model is not approved merely because its package name or version appears in a manifest. Before use:

1. record the official source, resolved revision, exact installed artifacts, dependency lock, platform, Python version, and SHA-256 values;
2. review current security advisories and licenses;
3. verify any controlled source patch against both preimage and result digests;
4. install from the exact lock in an isolated environment;
5. run `scripts/secure_preflight.py` with the task-specific evidence;
6. keep its output a candidate and rerun the applicable synthetic regressions.

The public core intentionally contains no approved private model identity or document-specific source hash. Pass an expected model SHA-256 explicitly. A matching hash proves identity, not behavioral safety.

## Domain-pack boundary

- Load a domain pack only through `scripts/domain_pack.py` with an explicit path plus expected pack ID, version, and digest.
- Do not scan common folders, environment locations, parent directories, registries, or network shares for packs.
- Packs are data-only. Reject unlisted files, executable content, traversal, reparse points, unknown fields or media types, duplicate IDs, hash mismatch, digest mismatch, and incompatible core versions.
- Pack absence does not block general localization, but all pack-dependent assertions remain `NOT_CHECKED` or `BLOCKED`.

## PDF risk checks

The preflight scans conservatively for encryption, malformed input, suspicious CMap indicators, JavaScript/actions, embedded files, launch actions, and external references. These checks are not a forensic guarantee.

- `BLOCKED` stops the run.
- `NEEDS_REVIEW` requires deliberate inspection or sanitization.
- Never describe a PDF as safe merely because no known indicator was found.

## Licensing boundary

Dependency roles, official sources, and license evidence are recorded in [dependency-manifest.json](dependency-manifest.json) and [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md). Copyleft optional backends are not copied into this candidate. Distribution or network-service use requires an independent obligation review. This policy is engineering guidance, not legal advice.
