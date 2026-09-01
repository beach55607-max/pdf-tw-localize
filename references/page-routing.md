# Page and region routing

Route every page after inspecting the rendered page and extracted structure. The inspector suggests a route; it never decides the route or review result. A mixed page may use several region routes under one primary page route.

Before translation, give every meaningful region a stable source ID and record its role, bounding box, reading order, neighbors, protected tokens, and intended translation region. Never use extraction order as a substitute for this mapping.

## prose

Use for ordinary low-image, single-flow paragraphs.

1. Build a page context packet with headings, preceding and following paragraphs, captions, warnings, and protected terms.
2. Translate coherent paragraphs with the primary context-capable language model.
3. Optionally compare a BabelDOC or PDFMathTranslate candidate on low-risk prose only.
4. Reflow mapped regions and inspect the full rendered page.

Do not route a page as prose merely because it has extractable text.

## structured

Use for tables, forms, callout boxes, warnings, multi-column pages, dense lists, paired conditions, repeated labels, or vector-rule layouts.

1. Capture the whole table or structured section before translating cells.
2. Include row headers, column headers, units, footnotes, and adjacent conditions in the context packet.
3. Translate by semantic row, column, or callout; keep the one-to-one source and translation region map.
4. Reconstruct deliberately, then inspect every cell, rule, heading, and reading-order boundary.

Bare labels such as `Under`, `On`, `Off`, or `Direct` require their row, column, and product context. Global word replacement is unsafe.

When one table-cell phrase is split across source lines or extraction blocks, route the fragments as one ordered `table_cell_phrase` group before translation. Preserve the full cell meaning and terminology; line-by-line dictionary translation is blocking when the joined target no longer expresses the joined source phrase.

Inspect source anchoring and contrast at region level. A running header or label aligned to the source right edge remains right-aligned even when the target is shorter. Text painted white or another non-black source color on a dark plate retains that foreground color. Record these properties in the manifest and verify the reopened candidate's actual spans rather than inferring success from target boxes.

## image-overlay

Use when English is baked into screenshots, controllers, diagrams, labels, callouts, or illustrations and some image regions may require localization.

1. Open the full page and identify the image's purpose and UI state.
2. Classify each image-text region as required, user-permitted English, or protected; protect symbols, values, model codes, and physical button shapes.
3. Choose one route for each visual:
   - `preserve_source_visual_with_textual_guidance` when the source visual is clear, the UI English is not safety-critical, and adjacent localized text can explain the needed operation. Preserve the original pixels and the registered placement of that complete screenshot, diagram, or controller visual; put only exact `中文（Source UI）` navigation terms in nearby body text or a caption.
   - `redraw_required_image_text` only when the user requests a redraw, or when safety or core operation cannot be explained adequately outside the visual.
4. A user instruction to preserve the source visual overrides a default redraw preference. Never force a redraw merely to eliminate otherwise permitted UI English.
5. For a preserved visual, declare its bbox, decoded image identity, pixel dimensions, visual ID, exact UI-English-to-zh-TW map, and guidance segment IDs. No mask, replacement text, or new drawing may intersect that visual bbox.
6. For a required redraw, remove or cover only source text and redraw zh-TW with comparable visual height, alignment, contrast, and reading order. Do not harm the artwork.
7. Inspect the candidate at 300 dpi or higher. For a preserved route, verify source/candidate decoded images and rendered visual regions match exactly, adjacent guidance is complete, and the exact allowlist did not leak into unrelated body text. For a redraw, confirm no required English, duplicated label, or damaged artwork remains.

### Vector outlined text and compound components

Path-outlined words are graphics, not safely maskable text. Inventory the whole visual component and split it into stable members before routing. Use the canonical roles `translatable_live_text`, `translatable_vector_outlined_text`, `icon`, `frame`, `vector_rule`, `background`, and `neighbor_container`. A partial rectangular mask is forbidden.

An inspected manifest may explicitly identify which paths form outlined text. Bind each selected path to its source page object, object xrefs, decoded content-stream xref and SHA-256, CTM, graphics state, ordered path constructors (`m`, `l`, `c`, `v`, `y`, `h`, `re`), clipping disposition, and paint operator. `replace_vector_outlined_text` removes only the uniquely matched contiguous operators, verifies zero signature residue, and inserts embedded zh-TW text. It does not claim to recognize arbitrary vector shapes as language.

If selection is missing, duplicated, non-contiguous, clipped, interwoven with a protected frame/icon/rule, or contains an unknown operator, stop as `BLOCKED`. Do not retain required English as a whole-group workaround and do not cover the group with a white patch. `preserve_complete_visual` is available only when all members are declared non-translatable or explicitly user-preserved.

For every preserved vector member, drawing identity includes the complete path, not only its bbox or paint summary. Retain the emitted order and multiplicity of `l`, `re`, `qu`, and cubic `c` operators and every coordinate. A cubic record includes the start point, both control points, and endpoint. Unknown or incomplete operators require `manual-review` or `BLOCKED`; they may not be skipped while the component is reported preserved.

When a label line mixes live text with a Dingbats marker, symbol, vector rule, frame, or separate background, create a component group. Record every member bbox, role, and policy. Map a preserved marker span separately, select only the live-text source span for translation, and use `mask_policy: source_text_spans_only`, `mask_mode: remove_text_only`, and `mask_padding_pt: 0`. The rule, marker, frame, and background remain independent source components and must pass member-level QA.

Record `contains`, `adjacent`, and `avoid` relations between members. `adjacent` and `avoid` carry a page-specific `minimum_clearance_pt` when visible separation matters. QA requires both zero intersection and clearance at or above the declared value; report the equivalent at 300 dpi.

If inspection shows that a rule itself must move to restore optical alignment after translation, route that one independently identified member through `adjust_vector_rule`. Bind exactly one unclipped stroked source path by its complete drawing and content-stream signatures, declare `adjustment_method: translate_exact_stroked_path`, a non-zero page-space `translation_delta_pt`, and the exact translated `target_bbox`. Move only its path construction operators; do not paint another line over the source. Source residue, a missing or duplicate target signature, a transformed path, or any unrelated path change is blocking.

Use `align_center_y` only when geometric centers are intended to coincide. For a manifest-specific optical offset, use `align_optical_offset_y` with signed `expected_target_minus_member_center_pt`, `maximum_delta_pt`, and `measurement_basis: actual_candidate_text_span_bbox`. Candidate QA must reopen the PDF and locate the actual inserted text span before measuring the offset to the rule; planned line boxes or target rectangles are not sufficient evidence.

When two or more same-page headings, badges, or callouts are instances of one visual template, declare a repeated-component layout contract instead of tuning each instance independently. Normalize each instance to an explicit anchor member and compare only manifest-listed local bbox metrics for corresponding semantic members. A translated label may omit width-dependent metrics when character count legitimately differs, but its local vertical position and the rule's local position, length, and thickness remain comparable when declared. Candidate QA fails closed on missing members, mismatched instance fields, or any metric outside tolerance.

Translation can also change a related object's required geometry without changing the object's semantic role. Inventory every plate, frame, icon, rule, and neighboring container whose edge, position, clearance, or occlusion is derived from the translated label. Put `dependent_geometry` on each affected member, bind its edges to either the source member bbox or a specifically named translated-text member's actual candidate bbox, and declare all offsets and bounds in the manifest. The renderer may use only the member's supported exact path or rectangle rewrite. If an affected object has no safe exact route, stop instead of leaving it unchanged or adding an overlay.

Do not judge the rule only by its hidden source path. If an opaque plate or other related member overlaps the rule, declare a composited-visible contract. QA subtracts the declared opaque intervals from the final candidate rule bbox, requires one uniquely measurable visible interval, and compares visible start, end, length, thickness, and cross-axis placement across repeated instances. An occluder that misses the subject, covers only part of its thickness, splits it into several intervals, or is omitted from the contract is not a PASS. Open the 300 dpi crops afterward because analytic compositing does not replace human visual review.

If the visual defect comes from the source background member itself rather than the live-text removal bbox, do not enlarge or repaint the text mask. A simple, independently identified, untransformed rectangular background may use `adjust_background` to shrink within its own source bbox. Declare the target bbox, expected fill, and neighboring `avoid_regions`; the source background must intersect an avoid region and the target must clear it. Preserve rounded containers underneath so their original geometry is revealed instead of patched.

OCR is evidence and transcription assistance; it is not a translation or layout decision.

### Inline complete-object relocation

A standalone button or status icon embedded in an instruction is not automatically a fixed-placement screenshot. Distinguish two independent properties:

- internal visual content is immutable: retain the complete source clip at identical width and height, with no crop, redraw, substituted glyph, or internal pixel/vector edit;
- object placement may change: move the whole visual horizontally or vertically when zh-TW grammar requires it, unless the user or another manifest contract explicitly fixes its registration.

For a two-choice instruction, use `pdf-tw-localize/inline-visual-relocation/v1`. Declare `natural_inline_choice_sequence`, semantic labels for both visuals, `show_pdf_page_source_clip`, exact source and target clips, both translation deltas, an inspected cover color, and live-text fragments in the order prefix + visual A + `或` + visual B + suffix. The target clips must retain source dimensions. Declare a finite `maximum_gap_pt`; source-order whitespace is not a valid layout requirement.

Before authoring the relocation, scan every page in the selected scope for the same source visual pattern and declare one `pdf-tw-localize/inline-visual-sweep/v1`. The expected ID list must exactly equal the matching segments. Full-document runs use `document_wide_source_visual_scan` and `DOCUMENT_WIDE_COMPLETE`; explicitly narrower proofs use the corresponding declared-scope values. A user report identifies a defect family, not permission to leave homologous instances unchecked.

Stage 1 removes all prior live text in each declared cover with `remove_text_only`. Stage 2 applies the inspected opaque cover, copies the complete source clips, and inserts the zh-TW fragments. Report both stages truthfully. Do not change `mask_mode` after the fact to make a QA tool pass; legacy external overlays require exact hash-bound evidence, zero residual prior text, and source-drawing preservation.

## scanned-rebuild

Use for raster-dominant pages with little trustworthy extractable text.

1. Render at high resolution and verify page orientation.
2. OCR or manually transcribe logical regions while viewing the whole page.
3. Rebuild text overlays or page composition with explicit region mapping.
4. Compare warnings, numbers, labels, hierarchy, and reading order at readable zoom.

## manual-review

Use when corruption, encryption, unsupported fonts, unusually dense structure, uncertain reading order, or meaning loss makes automatic reconstruction unsafe.

Do not force a result. Record the exact uncertainty and the decision needed.

## Conservative signals

- High image area or no extractable text: `image-overlay` or `scanned-rebuild`.
- Repeated x positions, rules, many small regions, or multiple text bands: `structured`.
- Visual text materially exceeds extracted text: `image-overlay` or `manual-review`.
- Ordinary single-column text with low image coverage: `prose`.

Always inspect title, safety, table, diagram, controller, maintenance, screenshot, and user-reported pages individually.
