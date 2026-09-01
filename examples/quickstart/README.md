# De-identified end-to-end quickstart

This walkthrough uses only generic names and placeholder text. The repository intentionally ships no customer PDF or sample PDF binary. Bring a small English PDF that you are authorized to process, keep it out of Git, and call it `sample-guide-en.pdf` in the commands below.

The goal is a new candidate plus evidence. The goal is not to turn every status into `PASS` by adding exceptions.

## 1. Create the locked environment

The reference runtime is CPython 3.11–3.14. Repository lock files bind exact package versions and hashes.

Run from the repository root in Windows PowerShell:

```powershell
.\scripts\setup_env.ps1 -WithDev
.\.venv\Scripts\python.exe .\scripts\verify_runtime.py --dev

$Pdf = Resolve-Path ".\sample-guide-en.pdf"
$RunDir = ".\work\run-001"
New-Item -ItemType Directory -Path $RunDir | Out-Null
```

All output paths in this toolchain are write-once. Use another run directory if you repeat the walkthrough.

## 2. Preflight and inspect

```powershell
& .\.venv\Scripts\python.exe .\scripts\secure_preflight.py $Pdf `
  --output "$RunDir\preflight.json"
if ($LASTEXITCODE -ne 0) { throw "Preflight did not pass; stop and inspect the report." }

& .\.venv\Scripts\python.exe .\scripts\inspect_pdf.py $Pdf `
  --output "$RunDir\inspection.json"
```

Do not continue past `BLOCKED`. Investigate `NEEDS_REVIEW`, including scanned pages, embedded files, forms, JavaScript, encryption, unusual color handling, or pages that require a different route.

## 3. Extract a small page scope

Start with one representative page. The document ID below is synthetic and contains no source filename, customer, product, or model identity.

```powershell
& .\.venv\Scripts\python.exe .\scripts\extract_segments.py $Pdf `
  --pages "1" `
  --document-id "public-demo" `
  --output "$RunDir\extraction.json"

& .\.venv\Scripts\python.exe .\scripts\validate_segments.py `
  "$RunDir\extraction.json" `
  --stage extraction `
  --output "$RunDir\extraction-validation.json"
```

Open `extraction.json`. Check the reading order, source text, bounding boxes, protected tokens, render actions, and every `segment_id`. If the extraction validation is not `PASS`, repair the mapping or source evidence before translation.

## 4. Produce an exact-ID translation import

The scripts do not call a language model. Give the validated extraction and necessary page context to a human or explicitly selected translator. Never invent IDs and never translate from an old localized PDF.

Create `$RunDir\translations.json` with this shape:

```json
{
  "schema": "pdf-tw-localize/translation-import/v1",
  "source_manifest_sha256": "<SHA-256 OF extraction.json>",
  "translator": "human-or-explicitly-selected-translator",
  "translations": [
    {
      "segment_id": "<COPY EXACT ID FROM extraction.json>",
      "zh_TW": "<TAIWAN TRADITIONAL CHINESE TEXT>",
      "note": "",
      "translation_assertions": []
    }
  ]
}
```

Get the required manifest hash with:

```powershell
(Get-FileHash "$RunDir\extraction.json" -Algorithm SHA256).Hash
```

The `translations` array must cover every extracted segment ID exactly once. Preserve required model numbers, values, units, warning levels, and other protected tokens. Include every required semantic assertion from the extraction contract.

Import and validate:

```powershell
& .\.venv\Scripts\python.exe .\scripts\import_translations.py `
  "$RunDir\extraction.json" `
  "$RunDir\translations.json" `
  --output "$RunDir\translated-manifest.json"

& .\.venv\Scripts\python.exe .\scripts\validate_segments.py `
  "$RunDir\translated-manifest.json" `
  --stage translation `
  --output "$RunDir\translation-validation.json"
```

Missing, unexpected, duplicate, empty, stale, or semantically unsupported entries fail closed.

## 5. Rebuild from the English source

```powershell
& .\.venv\Scripts\python.exe .\scripts\rebuild_pdf.py $Pdf `
  --manifest "$RunDir\translated-manifest.json" `
  --font ".\assets\fonts\NotoSansTC-Regular.ttf" `
  --bold-font ".\assets\fonts\NotoSansTC-Bold.ttf" `
  --output "$RunDir\candidate-zh-TW.pdf" `
  --report "$RunDir\rebuild-report.json"
```

The candidate is rebuilt from the English source. The original remains unchanged. A successful write only proves that files were produced.

## 6. Run machine QA

Create `$RunDir\english-allowlist.json`. For a document with no approved remaining English, the v3 structure is:

```json
{
  "schema": "pdf-tw-localize/english-allowlist/v3",
  "scope": {
    "document_id": "public-demo",
    "pages": [1]
  },
  "allowed": [],
  "allowed_visual_english": [],
  "allowed_ui_english": []
}
```

Then run:

```powershell
& .\.venv\Scripts\python.exe .\scripts\qa_rebuilt_pdf.py `
  --source $Pdf `
  --candidate "$RunDir\candidate-zh-TW.pdf" `
  --manifest "$RunDir\translated-manifest.json" `
  --rebuild-report "$RunDir\rebuild-report.json" `
  --allowlist "$RunDir\english-allowlist.json" `
  --output "$RunDir\machine-qa.json"
```

An allowlist is evidence, not a suppression switch. Do not add broad English exceptions merely to obtain `MACHINE_QA_PASS`. Scope any legitimate exception to exact pages and stable IDs with a source-supported basis.

## 7. Render and review every page

```powershell
& .\.venv\Scripts\python.exe .\scripts\render_review.py `
  $Pdf `
  "$RunDir\candidate-zh-TW.pdf" `
  --pages "1" `
  --dpi 300 `
  --out-dir "$RunDir\review"
```

The generated contact sheet is for navigation only. Open every comparison image individually at readable zoom, record page-specific observations in the generated v2 review manifest, and then validate the evidence:

```powershell
& .\.venv\Scripts\python.exe .\scripts\validate_visual_review.py `
  "$RunDir\review\review_manifest.json" `
  --pages "1" `
  --output "$RunDir\visual-review-validation.json"
```

Do not bulk-stamp identical observations. A render, a contact sheet, or a valid manifest structure does not prove that a human looked at the pages.

## 8. Interpret the result honestly

A candidate can be internally complete only when the declared scope has machine QA, applicable semantic QA, and valid page-by-page visual review with no blocking finding. `USER_ACCEPTANCE` remains `NOT_CHECKED` until the user explicitly accepts that exact candidate.

For the detailed gate definitions, read [`../../references/qa-contract.md`](../../references/qa-contract.md). For security behavior, read [`../../references/security.md`](../../references/security.md).

## Large documents and resume

After extraction validation passes, create hash-bound translation batches:

```powershell
& .\.venv\Scripts\python.exe .\scripts\full_run_pipeline.py plan `
  "$RunDir\extraction.json" `
  --validation-report "$RunDir\extraction-validation.json" `
  --output "$RunDir\full-run-plan.json" `
  --requests-dir "$RunDir\requests"

New-Item -ItemType Directory -Path "$RunDir\results" | Out-Null

& .\.venv\Scripts\python.exe .\scripts\full_run_pipeline.py status `
  "$RunDir\full-run-plan.json" `
  --requests-dir "$RunDir\requests" `
  --results-dir "$RunDir\results"
```

Each result must satisfy its generated request contract. When status is `COMPLETE`, merge exact results into a translation import:

```powershell
& .\.venv\Scripts\python.exe .\scripts\full_run_pipeline.py merge `
  "$RunDir\full-run-plan.json" `
  --requests-dir "$RunDir\requests" `
  --results-dir "$RunDir\results" `
  --output "$RunDir\translations.json"
```

Resume validates checkpoints; it does not restore machine QA, visual review, or user acceptance. See [`../../references/full-mode-acceleration.md`](../../references/full-mode-acceleration.md).

## Public issue hygiene

A useful public report contains a synthetic reproducer generated in test code, the exact command, the status or error JSON, and expected versus actual behavior. Remove names, account identifiers, serial numbers, source filenames, private paths, document hashes, internal glossary entries, credentials, screenshots, and model metadata before posting.

Do not commit the input PDF, candidate PDF, translation cache, review images, or runtime output directory.
