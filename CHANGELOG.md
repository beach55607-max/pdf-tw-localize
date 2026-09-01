# Changelog

All notable changes to this project are documented here.

## 0.2.0 - 2026-09-01

### Added

- Added `scripts/full_run_pipeline.py` and its deterministic planning core for dependency-safe semantic batches, bounded parallel waves, exact request/result checkpoints, stable-ID merge output, and plan-bound stage timing.
- Added `pdf-tw-localize/inline-visual-relocation/v1`, complete-object source-clip relocation, document- or selected-scope homologous sweeps, and `scripts/qa_inline_visual_sequences.py` for natural zh-TW inline visual order.
- Added regression coverage for deterministic planning, context propagation, interrupted-run resume, stale-result rejection, protected-token evidence, inline-visual geometry, connector placement, and legacy two-stage overlay evidence.

### Changed

- Full-document translation now requires an extraction-stage validation `PASS`, one document-wide terminology and dependency prepass, and semantic grouping that never splits a declared cross-page dependency merely to meet a batch-size target.
- Every translation batch now receives the same hash-bound document contract and glossary payloads plus context-only neighbors. High-context batches are isolated to at most one per parallel wave.
- Rebuild and QA now distinguish immutable visual content from fixed placement: a complete icon or image may move when zh-TW grammar requires it, while cropping, scaling, redrawing, or editing internal marks remains blocking.
- Public-package validation now requires the full-mode planner, acceleration reference, inline-visual QA implementation, and their regression suites.
- Clean public-mirror verification now reads accepted Git blob bytes and permits only checkout-only CRLF/LF differences, so Windows clones reproduce the exact Candidate tree without packaging transformed working-tree bytes.

### Safety and compatibility

- Resume and merge fail closed on stale plans, changed request files, missing or duplicate segment IDs, incomplete protected-token evidence, conflicting translations, and missing semantic assertions.
- Acceleration affects translation planning and draft generation only. Final source-coordinate rebuild, machine QA, semantic QA, individual visual review, and user acceptance remain fresh, independent gates and are never restored from checkpoints.
- Legacy unversioned inline-visual evidence remains explicitly classified as legacy and cannot be promoted to a v1 machine pass without the required source, manifest, candidate, segment, and overlay bindings.
- No private domain pack, customer document, document-specific identifier, model weight, or secret is added. The domain-pack API remains `1.0.0`; runtime dependency versions, `AGPL-3.0-only` licensing, and third-party notice boundaries are unchanged.

## 0.1.0 - 2026-08-30

- Initial public release of the source-bound Taiwan Traditional Chinese PDF localization Skill.
- Added deterministic segment extraction, exact translation import, source-coordinate rebuilding, security preflight, and PDF QA tooling.
- Added optional, explicitly loaded data-only domain packs while keeping private terminology and regression identities outside the public core.
- Added hash-locked runtime manifests, synthetic regression tests, portable Noto Sans TC fixtures, and AGPL-3.0-only licensing.
- Added a deterministic clean-mirror builder for producing a single human-facing public Git history and reproducible release assets.
