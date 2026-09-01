# Full-mode acceleration without weaker QA

Use this workflow when the user requests a full-document translation from an English source and the document is large enough that one serial translation pass would be slow. The acceleration boundary is translation planning and draft generation only. Source inspection, source-coordinate rebuild, machine QA, semantic QA, individual visual review, and user acceptance remain full-strength gates.

## Quality-preserving order

1. Complete secure preflight, page inspection, route selection, and stable-ID extraction for the declared full scope.
2. Perform one document-wide terminology and dependency prepass before creating batches. Resolve glossary precedence, repeated controls and labels, headings, warnings, table continuations, cross-page procedures, paired values, and any verified document-context references.
3. Record unresolved ambiguity as `NEEDS_REVIEW`. Do not send an ambiguous dependency into separate translation batches.
4. Require an extraction-stage `segment-validation/v1` report with `PASS`, zero blocking issues, zero needs-review issues, and the exact current manifest SHA-256. Create a deterministic, hash-bound plan and request bundle with `scripts/full_run_pipeline.py plan`.
5. Translate only the owned stable IDs in each request. Context-only pages and segments are read-only context and must not be returned as translations.
6. Validate existing result checkpoints with `status`. Reuse only batches whose plan, source manifest, request digest, stable IDs, protected tokens, and source-segment digests still match exactly.
7. Merge only when every batch is complete and valid. Import the merged `translation-import/v1` into the original source-bound segment manifest.
8. Rebuild once from the English source, then run the complete final QA and page-by-page visual review. Never reuse a previous QA PASS or user-acceptance state.

## Dependency-safe batching

The planner treats the following as indivisible semantic dependencies:

- explicit `document_context_refs` between pages;
- shared relationship IDs, including table, UI-state, condition-pair, and component relationships;
- semantic bindings that reference another page context;
- reviewer-supplied dependency groups.

An indivisible group may exceed the configured page or segment target. The limit is deliberately soft: preserving meaning takes precedence over batch size.

Warnings, tables, UI, image text, protected regions, semantic bindings, preserved visuals, and compound or repeated components are `high_context`. A parallel wave contains at most one high-context batch. Standard-context batches may share a wave up to the configured limit.

Adjacent pages outside the owned group are supplied as context-only neighbors. Every batch also receives the document context, translation contract, and the same digest-bound glossary payloads. This keeps terminology global while ownership remains disjoint.

## Commands

Create a plan and immutable request directory:

```text
python scripts/full_run_pipeline.py plan segments.json \
  --validation-report extraction-validation.json \
  --output run-plan.json \
  --requests-dir batch-requests \
  --glossary current-run-glossary.csv \
  --target-segments 80 \
  --max-batch-pages 8 \
  --context-pages 1 \
  --max-parallel-batches 4
```

Use `--dependency-spec dependencies.json` when inspection found additional cross-page dependencies. Its schema is `pdf-tw-localize/full-run-dependencies/v1`:

```json
{
  "schema": "pdf-tw-localize/full-run-dependencies/v1",
  "groups": [
    {
      "group_id": "controller-procedure",
      "pages": [10, 11],
      "reason": "one operating procedure continues across both pages",
      "risk": "high"
    }
  ]
}
```

After one or more result files exist, inspect resumable state:

```text
python scripts/full_run_pipeline.py status run-plan.json \
  --requests-dir batch-requests \
  --results-dir batch-results
```

Merge only a `COMPLETE` run:

```text
python scripts/full_run_pipeline.py merge run-plan.json \
  --requests-dir batch-requests \
  --results-dir batch-results \
  --output translations.json
```

All output paths above must be new except the timing ledger, which is updated atomically. Result files use the filename declared in each plan batch and schema `pdf-tw-localize/full-run-batch-result/v1`. They bind:

- `plan_sha256` and `plan_digest`;
- `source_manifest_sha256`;
- `batch_id` and `batch_digest`;
- the exact request file as `request_sha256`;
- every owned `segment_id` and its declared `source_segment_sha256`, which binds the source text, page, semantic type, protected tokens, relationships, semantic bindings, and render action;
- exact `zh_TW` text and `translation_assertions`.

The status command treats a missing result as pending and an invalid or stale result as blocked. It never silently reuses a near match.

## Parallel execution boundary

The local scripts plan, validate, resume, and merge; they do not call or impersonate a language model. When the active environment and user authority permit independent workers, batches in the same `parallel_wave` may be translated concurrently. Each worker receives exactly one request and writes exactly one new result. Do not split a batch further, edit the plan after dispatch, or let workers modify the source manifest or PDF.

If parallel workers are unavailable, process batches serially in plan order. Resume still avoids repeating completed exact batches.

## Timing evidence

Use `record-stage` to append a plan-bound timing record for secure preflight, inspection, extraction, planning, translation, import, rebuild, machine QA, rendering, visual review, and delivery. Attempt IDs are unique, timestamps include time zones, and negative or duplicate records are rejected.

Batch results may include elapsed seconds. `status` reports both observed worker-seconds and a wave-based parallel wall-time estimate. These measurements are operational evidence only; they do not grant a QA or acceptance state.

The timing ledger cannot record `user_acceptance`. `USER_ACCEPTANCE` remains `NOT_CHECKED` until the user explicitly accepts the actual delivered candidate.
