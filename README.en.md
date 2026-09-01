# Taiwan Traditional Chinese PDF Localization (`pdf-tw-localize`)

[繁體中文](README.md) · [End-to-end quickstart](examples/quickstart/README.md) · [Skill contract](SKILL.md)

A source-bound Codex Skill and deterministic Python toolchain for rebuilding layout-sensitive English PDFs as traceable Taiwan Traditional Chinese candidates.

This is not a one-click “translate and ship” wrapper. It binds source pages, semantic segments, stable IDs, translations, rebuild evidence, and QA states so that omissions, misplaced text, damaged visuals, or stale results can fail visibly.

## Who it is for

Use this project when you need to preserve diagrams, icons, tables, page geometry, and source traceability. It is especially useful for technical documents where automated checks, individual page review, and final user acceptance must remain separate.

It is not intended for plain-text-only translation, unauthorized source documents, or workflows that treat a successful PDF write as approval.

## Pipeline

```text
English source PDF
  -> security preflight and page routing
  -> semantic segments with stable IDs
  -> zh-TW text from a human or explicitly selected translator
  -> exact-ID import and validation
  -> source-coordinate candidate rebuild
  -> machine QA -> semantic QA -> page-by-page visual review -> user acceptance
```

The local scripts do not pretend to call a language model. A primary LLM, a human translator, or another explicitly selected translator produces the text. The scripts handle source binding, deterministic planning, exact-ID import, rebuild, and validation.

## Inspectable claims

| Claim | Implementation and regression evidence |
| --- | --- |
| Stable source-bound segments | [`scripts/extract_segments.py`](scripts/extract_segments.py), [`tests/test_segment_pipeline.py`](tests/test_segment_pipeline.py) |
| Exact translation coverage | [`scripts/import_translations.py`](scripts/import_translations.py), [`scripts/validate_segments.py`](scripts/validate_segments.py) |
| Hash-bound resumable full runs | [`scripts/full_run_pipeline.py`](scripts/full_run_pipeline.py), [`tests/test_full_run_pipeline.py`](tests/test_full_run_pipeline.py) |
| Source-coordinate rebuild | [`scripts/rebuild_pdf.py`](scripts/rebuild_pdf.py), [`tests/test_drawing_signatures.py`](tests/test_drawing_signatures.py) |
| Separate QA and acceptance states | [`references/qa-contract.md`](references/qa-contract.md), [`scripts/render_review.py`](scripts/render_review.py) |
| Public-package hygiene and synthetic-only tests | [`scripts/validate_public_package.py`](scripts/validate_public_package.py), [`references/synthetic-regression-policy.md`](references/synthetic-regression-policy.md) |

## Safe first run

The reference runtime is CPython 3.11–3.14. Repository lock files bind exact package versions and hashes.

On Windows PowerShell:

```powershell
.\scripts\setup_env.ps1
.\.venv\Scripts\python.exe .\scripts\verify_runtime.py

$Pdf = Resolve-Path ".\sample-guide-en.pdf"
$RunDir = ".\work\run-001"
New-Item -ItemType Directory -Path $RunDir | Out-Null

& .\.venv\Scripts\python.exe .\scripts\secure_preflight.py $Pdf `
  --output "$RunDir\preflight.json"
if ($LASTEXITCODE -ne 0) { throw "Preflight did not pass; stop and inspect the report." }

& .\.venv\Scripts\python.exe .\scripts\inspect_pdf.py $Pdf `
  --output "$RunDir\inspection.json"
```

This first run is read-only with respect to the source PDF. Stop on `BLOCKED`; investigate `NEEDS_REVIEW`. Use a fresh run directory because outputs intentionally refuse overwrite.

Continue with the de-identified [end-to-end quickstart](examples/quickstart/README.md).

## Install as a Codex Skill

Place this repository in Codex's `skills/pdf-tw-localize` directory, restart Codex, and invoke `$pdf-tw-localize`. Ask it to preserve the original, create a new candidate, stop on `BLOCKED`, review every page, and leave `USER_ACCEPTANCE` unset until you explicitly accept the result.

Read [`SKILL.md`](SKILL.md) and [`references/security.md`](references/security.md) before using the workflow on real material.

## Privacy and public issue boundary

The public core contains no customer PDFs, real manual identifiers, private glossary, historical candidate, translation log, private path, model weight, credential, or document-specific evidence. Tests generate synthetic PDF objects in code rather than committing real binary fixtures.

For a public issue, provide a synthetic reproducer, exact command, status JSON, expected result, and actual result. Do not upload customer files, identifying screenshots, credentials, private directories, model files, or translation caches.

## Validation

```powershell
.\scripts\setup_env.ps1 -WithDev
.\.venv\Scripts\python.exe .\scripts\validate_public_package.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
```

A successful write or render is not acceptance. The exact QA state contract is documented in [`references/qa-contract.md`](references/qa-contract.md).

## License

This project is released under `AGPL-3.0-only`; see [`LICENSE`](LICENSE). Dependency and bundled-font notices are in [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md). Evaluate the license obligations for your own distribution, modification, and network-service scenario.
