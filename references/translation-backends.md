# Translation backend roles

The primary semantic translation is performed by a context-capable language model using the complete page and semantic-region context packet. No extraction, translation, OCR, conversion, or normalization tool owns the final PDF.

For full-document work, perform one document-wide terminology and dependency prepass before translation. Then follow [full-mode-acceleration.md](full-mode-acceleration.md): each batch owns disjoint stable IDs, receives the same glossary and document contract, and includes read-only neighboring context. Exact completed request/result pairs may resume after hash validation; a stale or partial batch must be regenerated.

## Role hierarchy

1. **Primary language model:** translate coherent semantic blocks with page purpose, hierarchy, neighbors, table relationships, UI state, protected tokens, and glossary choices.
2. **Deterministic reconstruction:** use PyMuPDF or another controlled layout method to place mapped text, preserve undeclared visuals, and apply only explicitly routed visual edits.
3. **Optional candidate or second opinion:** use PDFMathTranslate, BabelDOC, or Hy-MT2 only where the page route and risk justify it.
4. **OCR:** transcribe image text and verify the final render; never infer page meaning from OCR alone.
5. **OpenCC:** normalize an already translated result with protected spans; it is not a translator.
6. **Human visual review:** decide alignment, hierarchy, readability, image-text completion, and whether the candidate is no worse than an applicable baseline.

## Optional PDFMathTranslate and BabelDOC use

They may generate a candidate only for low-risk prose regions after source-region mapping exists. Do not use either as the full-document default for tables, controller screens, image-heavy pages, dense technical labels, or user-reported defects.

This public core does not bundle an adapter for either backend. Before use, bind an exact reviewed upstream revision and dependency lock through a task-specific fail-closed wrapper. Direct CLI exit code 0 is never success evidence, and the output remains a candidate even when the wrapper succeeds.

## Optional Hy-MT2 use

Hy-MT2 may provide a draft or second opinion, not the primary semantic decision. If used:

- pass the exact official checkpoint revision and expected file SHA-256 to `scripts/secure_preflight.py`;
- use the exact hashed runtime lock;
- bind only to `127.0.0.1`, disable sharing and remote loading, and keep inference offline;
- use bounded context, output, temperature, and timeout;
- record every region, retry, fallback, and selected final backend;
- stop the service after the run.

The public core intentionally contains no approved model identity. Every selected checkpoint and runtime is a new candidate and must repeat the applicable synthetic regression suite.

## Prompt contract for any translation candidate

Provide the full semantic context, then require output only for the identified region IDs. The candidate must use Taiwan Traditional Chinese, preserve protected tokens and structural boundaries, and omit commentary or Markdown.

Reject or reroute output that:

- repeats source English or silently passes it through;
- loses a subject, negation, condition pair, operating mode, value-role association, unit, warning level, or step boundary;
- assigns a translation to the wrong region;
- adds commentary, placeholders, invalid characters, or unsupported claims;
- is empty or truncated.

Retry once only when a narrower semantic block preserves enough context. Otherwise route to the primary language model or manual translation and keep the page unresolved.

Parallel workers are optional execution capacity, not independent authorities. Dispatch only batches assigned to the same parallel wave, allow at most one high-context batch per wave, and merge only exact schema-bound results. A worker must not edit the source manifest, rebuild the PDF, or assert machine QA, visual review, or user acceptance.
