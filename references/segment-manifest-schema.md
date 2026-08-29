# Stable-ID segment manifest

Read this reference before extracting, translating, importing, rebuilding, or validating semantic PDF segments.

## Contract

The executable schema identifier is `pdf-tw-localize/segment-manifest/v1`. A manifest is source-bound by path, SHA-256, page count, selected pages, and page dimensions. It is always an internal work artifact; the delivered PDF remains monolingual zh-TW.

Every segment contains:

- `page` and a stable `segment_id` in `<document-id>.p####.<semantic-key>` form;
- `semantic_type`: `heading`, `paragraph`, `list`, `table-cell`, `UI`, `image-text`, `caption`, `warning`, `footer`, or `protected`;
- source `bbox`, span-level `source_lines` and `source_spans`, font/style summary, rotation, and page-local `reading_order`;
- exact `protected_tokens`, `source_text`, `zh_TW`, and `status`;
- `relationships` that bind the segment to its page context, row/column headers, table, group, condition pair, UI state, or neighbors;
- optional `component_contract` for compound or vector components, including `group_id`, `segment_role`, `mask_policy`, and a complete `members[]` inventory;
- `render` instructions with separate source mask and target bboxes, font limits, alignment, spacing, colors, and any table container.

Segments whose meaning depends on paired values, thresholds, default/fallback behavior, ranges, table roles, or another technical relationship may also contain `semantic_bindings[]`. Each binding uses a globally unique `binding_id`, a `parameter`, a `role`, exact protected `source_tokens[]`, optional `mode` and `condition`, `required_target_cues[]`, and any `context_ref_ids[]`. Set `context_required: true` when the value cannot be interpreted safely from the segment alone.

`required_target_cues[]` may be plain strings or objects with `text`, `kind`, and `scope`. Kinds are `parameter`, `role`, `mode`, `condition`, `comparison`, `consequence`, or `general`. Scope is `binding_phrase` when the cue must occur in the assertion's narrow value phrase, otherwise `segment`.

When a value needs explanatory wording to be understandable, a binding may additionally declare:

- `clarification_mode`: `none` (default), `source_derived_inline`, or `source_derived_note`;
- `comparison`: the source-supported comparator, such as `room_temperature_above_setpoint`;
- `consequence`: the source-supported result, such as `cooling_performed`.

Source-derived clarification requires `context_required: true`, exact verified `context_ref_ids[]`, and `required_target_cues[]` of kinds `comparison` and `consequence`. Those two additional cue kinds must occur in the asserted target phrase. The translation assertion mirrors `clarification_mode`, `comparison`, and `consequence` exactly. There is deliberately no generic hypothetical-example mode: examples are not a default translation operation.

Stable IDs come from a document ID, source page, and durable semantic key supplied in a layout spec. Do not make an ID depend on translation text or extraction sequence. Auto-generated block IDs are only a starting point for simple pages.

## Context packet

Each `page_contexts[]` entry records the whole-page purpose and geometry plus the heading hierarchy, neighboring semantic context, table headers and conditions, UI state, protected terms, and image-text inventory status. Translation receives this complete packet and returns the same IDs.

When another source page defines or disambiguates the current technical claim, add `document_context_refs[]` to the page context. Each entry records a stable `context_ref_id`, source `page`, exact `source_excerpt`, the reason it is relevant, and `review_status`. Only `VERIFIED_SOURCE_CONTEXT` satisfies a binding with `context_required: true`; missing or unverified references remain `NEEDS_REVIEW`.

When a screenshot, diagram, or path-outlined component uses `preserve_source_visual_with_textual_guidance`, add `preserved_visuals[]` to that page context. Every entry records a stable `visual_id`, source `bbox`, `policy`, and `visual_kind` (`raster_image` or `vector_component`). Raster entries also record the primary decoded image SHA-256, pixel width and height, and all same-bbox image layers. Vector entries instead record `component_group_id` and all `component_roles`. Use `allowed_ui_english[]` for raster UI text and `allowed_visual_english[]` for complete vector labels. Every allowlist entry records exact `source_text`, its `zh_TW` mapping, whether adjacent guidance is required, and the permitted guidance segment IDs. These are scoped visual exceptions, not page-wide English allowlists.

For a table cell, populate relationship fields such as `table_id`, `row_header_ids`, `column_header_ids`, and `condition_pair_ids`. For controls, use `group_id`, `ui_state`, and relevant neighbor IDs. Bare labels such as `Above`, `Under`, `Back`, `Off`, or `OK` are invalid without these relationships.

If a single table-cell phrase is split across segments, every member declares an identical `relationships.table_cell_phrase` object with `schema: pdf-tw-localize/table-cell-phrase/v1`, stable `group_id` and `table_id`, the exact `cell_bbox`, ordered `segment_ids`, full `source_phrase` and `target_phrase`, and explicit source/target separators. All members must be same-page `table-cell` segments whose `render.container_bbox` equals the declared cell. Validation joins the ordered fragments and fails on a missing member, conflicting contract, wrong container, or phrase mismatch.

## Extraction layout spec

`extract_segments.py --layout-spec` accepts `pdf-tw-localize/layout-spec/v1` JSON. Each selected page contains `context`, `segments`, and optional `ignored_source_refs`.

Each segment spec uses:

- `key`: durable semantic key used in the stable ID;
- `source_refs`: one or more `{ "block": n, "line": n, "spans": [n, ...] }` selectors. Omit `spans` to select the full line;
- `semantic_type`, `reading_order`, `relationships`, and `render`;
- optional `component_contract` for a compound label or complete vector visual;
- optional `source_text` and `bbox` for visually annotated image text that has no extractable span;
- `extraction_method: visual_annotation` for image/OCR/UI content identified while viewing the page;
- optional exact `protected_tokens` and `source_style` for non-extractable labels.

Every extractable span must be mapped once or be listed in `ignored_source_refs` with a concrete reason. Unmapped or duplicate source refs block validation.

### Component contract

`component_contract.group_id` is a stable visual-group identifier and must equal `relationships.component_group_id`. `relationships.component_relation` is `member_of`, `visual_annotation_of`, or `guidance_for`. Every segment in the same group declares an identical `members[]` inventory.

New compound routes use `component_contract.schema: pdf-tw-localize/compound-component/v2`. Canonical member roles are `translatable_live_text`, `translatable_vector_outlined_text`, `icon`, `frame`, `vector_rule`, `background`, and `neighbor_container`. Every member requires `component_id`, `source_page`, `bbox`, `role`, `translatability` (`required`, `not_translatable`, or `user_preserved`), `policy`, `source_evidence`, and `relations[]`. A user-preserved member also requires `preservation_basis`.

`source_evidence` records `page_object_xref`, all relevant `object_xrefs`, each decoded `content_streams[]` xref and SHA-256, and an `ordered_path_signatures` object using `pdf-tw-localize/ordered-path-signature-set/v1`. Applicable vector members carry complete PyMuPDF drawing signatures and, when content-stream selection is required, `pdf-tw-localize/content-path-signature/v1` entries with source stream xref, CTM, graphics state, ordered operators and operands, clip operator, and paint operator. Live text records an explicit not-applicable path disposition rather than omitting evidence.

`translatable_live_text` requires `required + replace_live_text`. `translatable_vector_outlined_text` requires `required + replace_vector_outlined_text`, `segment_role: translatable_vector_outlined_text`, `render.action: replace_vector_outlined_text`, `mask_policy: none`, `extraction_method: visual_annotation`, and one exact `vector_member_id`; no mask fields are permitted. `preserve_complete_visual` is rejected when any group member still requires translation.

Each member relation has `type: contains | adjacent | avoid | align_center_y | align_optical_offset_y` and `target_member_id`. `adjacent` and `avoid` require non-negative `minimum_clearance_pt`. `align_center_y` requires non-negative `maximum_delta_pt` and compares geometric bbox centerlines. `align_optical_offset_y` applies only from a translated text member to a `vector_rule` target and requires a signed `expected_target_minus_member_center_pt`, non-negative `maximum_delta_pt`, and `measurement_basis: actual_candidate_text_span_bbox`. Candidate QA reopens the PDF, uniquely matches the inserted text span, and compares its actual bbox center to the target rule; planned render boxes are not alignment evidence. Intersection must be zero, clearance must meet the manifest value, and declared alignment errors must remain within tolerance.

A page context may include `repeated_component_layouts[]` when multiple compound groups are instances of one layout template. Each contract requires a unique `contract_id`, `normalization: anchor_top_left`, non-negative `maximum_delta_pt`, at least two `instances[]`, and a `compare` map. Every instance requires a unique `instance_id`, an `anchor_member_id`, and exactly the same semantic `member_ids` keys as `compare`. Supported metrics are `x0`, `y0`, `x1`, `y1`, `width`, `height`, `center_x`, and `center_y`, all evaluated in anchor-local coordinates from reopened candidate bboxes. To permit translation-length differences, omit width-dependent metrics for the label and record that choice in the manifest; undeclared or malformed exceptions are not inferred.

### Translation-dependent related-object geometry

Any member whose target geometry depends on translated text uses `dependent_geometry.schema: pdf-tw-localize/translation-dependent-geometry/v1`. It requires `measurement_basis: actual_candidate_text_span_bbox`, `bounds_policy: within_source_bbox | within_page`, non-negative `maximum_delta_pt`, optional non-negative minimum width/height, and an `edge_bindings` map containing exactly `x0`, `y0`, `x1`, and `y1`.

Each edge binding names `basis`, `edge`, and finite `offset_pt`. `basis: source_bbox` reads an edge from the dependent member's source bbox and has no member ID. `basis: candidate_member_bbox` also requires `member_id` naming one translated live-text or outlined-text member; QA binds that driver's actual reopened candidate span. At least one edge must use a candidate text driver. The resolved bbox must be nonempty, meet its declared minimum size, remain within its bounds policy, and match the actual adjusted candidate member within tolerance.

Currently safe executable dependency routes are an exact untransformed rectangle rewrite through `adjust_background`, or an exact non-resizing path translation through `adjust_vector_rule`. Other roles remain in the member inventory, but a declared dependency on an unsupported policy is blocking rather than silently preserved. A manifest may not provide an arbitrary static `target_bbox` as a substitute for the actual candidate driver when `dependent_geometry` is present; the rebuild report records the resolved target and driver bboxes.

### Composited-visible layout

A page context uses `composited_visible_layouts[]` when opaque related members can cover a horizontal rule. Each entry has `schema: pdf-tw-localize/composited-visible-layout/v1`, unique `contract_id`, `normalization: anchor_top_left`, `axis: horizontal`, non-negative `maximum_delta_pt`, a nonempty `compare` list, and at least two instances. Supported visible metrics are `start`, `end`, `length`, `thickness`, and `center_cross`.

Every instance names one `anchor_member_id`, one `subject_member_id`, and a nonempty unique `opaque_occluder_member_ids` list. QA uses actual candidate member bboxes, requires every occluder to cover the subject's full thickness and intersect it, subtracts the occluded X intervals, and requires exactly one remaining visible interval. Missing bboxes, non-intersecting or partial occluders, a completely covered subject, a split visible interval, cross-page members, duplicate fields, or any compared-metric delta above tolerance is blocking.

Legacy unversioned component contracts remain accepted only for historical regression compatibility. Do not use their old whole-vector preservation rule to author a new required-translation route.

A separately identified `background` member may instead use `policy: adjust_background` only with `adjustment_method: rewrite_untransformed_rect`. It declares its source `bbox`, a `target_bbox` wholly inside that source bbox, an exact three-channel `expected_fill`, and one or more `avoid_regions` containing stable `region_id` and page bbox values. Each source background must intersect an avoid region while the target has zero intersection. The renderer must bind exactly one source rectangle operator, rewrite that operator without painting a patch, verify one target rectangle remains, and report the before/after stream hashes. This exception cannot apply to text, markers, rules, frames, icons, images, outlined text, transformed paths, rotated pages, or arbitrary shapes.

A separately identified `vector_rule` may use `policy: adjust_vector_rule` only with `adjustment_method: translate_exact_stroked_path`. It declares its source `bbox`, an exactly translated `target_bbox`, a finite non-zero two-value `translation_delta_pt`, one complete source drawing signature, and one complete source content-path signature. The content path must use an axis-aligned unit CTM, no clip, and a stroked paint operator. The renderer rewrites only the uniquely matched path construction operands, preserves graphics state and operator order, and proves the source signature is absent while the exact translated drawing and content signatures each remain once. Duplicate selection, transformation, interweaving, unknown operators, source residue, or missing target evidence is blocking.

`segment_role: translatable_live_text` requires source-span extraction, `render.action: replace`, `mask_policy: source_text_spans_only`, `mask_mode: remove_text_only`, and `mask_padding_pt: 0`. Its `mask_bbox` cannot escape the one declared live-text member bbox. `segment_role: preserved_component` requires `render.action: preserve`, `mask_policy: none`, and no mask. A rectangular mask over outlined text, its frame, or its icon is blocking.

The manifest binds vector members by stable component ID, role, policy, page, and bbox; bbox equality is not itself preservation evidence. Machine QA derives a complete ordered drawing signature from each bound source/candidate region. It retains every `l`, `re`, `qu`, and cubic `c` operator, all coordinates, operator order, and duplicate count. The signature parser fails closed when a PyMuPDF operator is unknown or structurally incomplete.

## Translation handoff

Scripts export untranslated manifests; they do not call or impersonate a language model. The translating model receives the complete page context and all related segments, fills `zh_TW` by exact `segment_id`, and changes only the segment status to `TRANSLATED`. Preserve the mapping list.

For every declared semantic binding, the translation entry also returns one `translation_assertions[]` item with the same `binding_id`, `parameter`, `role`, `mode`, and `condition`, plus a narrow `target_phrase` copied exactly from `zh_TW`. When declared, it also mirrors `clarification_mode`, `comparison`, and `consequence`. That phrase must contain only the protected value tokens assigned to that binding, not values belonging to a paired binding. The importer and validator reject missing, duplicate, unknown, swapped, text-detached, or source-unsupported clarification assertions.

One-to-one mappings use one source and target ID. If a semantic block must split or merge, set `operation` to `split` or `merge`, list all source and target IDs, and record the reason. Implicit sequence-based mapping is forbidden.

Bind an import payload to the exact extraction-manifest SHA-256, include every segment ID exactly once, and run:

```text
extract_segments.py SOURCE --pages PAGES --document-id ID --layout-spec SPEC --output EXTRACTION.json
validate_segments.py EXTRACTION.json --stage extraction --output EXTRACTION_VALIDATION.json
import_translations.py EXTRACTION.json MODEL_TRANSLATIONS.json --output TRANSLATION.json
validate_segments.py TRANSLATION.json --stage render --output TRANSLATION_VALIDATION.json
```

Validation requires unique IDs and reading order, full source-ref coverage, explicit mappings, in-page bboxes, allowed states, nonempty translations, and exact protected-token preservation.

## Render boundary

`render.action` is normally `replace`; use `replace_vector_outlined_text` only with a v2 source-bound outlined member. Use `preserve` only for an exact protected token or separately mapped preserved component. Use `preserve_source_visual_with_textual_guidance` for a user-permitted `image-text`, UI, or protected visual annotation bound to a declared `relationships.visual_id`; it is not a tool-difficulty fallback for required headings or warnings. `mask_bbox` covers only source live-text spans. `mask_mode: remove_text_only` removes live text without painting a replacement rectangle and is required when a separate marker, background, frame, or line-art object must remain. `target_bbox` may reasonably expand within the same semantic area. `container_bbox` is mandatory for `table-cell` segments.

`render.text_color_policy` is `preserve_source_exact`, `fragment_source_exact`, or `explicit_inspected`. `preserve_source_exact` requires exactly one 24-bit sRGB value in `font_style.source_colors`; the renderer uses it and candidate QA reads back the actual span color. More than one source color is ambiguous unless every render fragment declares `text_color` plus `text_color_basis`, or an inspected effective `render.text_color` plus `text_color_basis` is declared. Missing or inconsistent evidence is blocking.

Layout-significant source alignment uses `render.alignment_contract` with `schema: pdf-tw-localize/text-alignment/v1`, `alignment: left | right | center`, `source_reference_bbox`, `target_reference_bbox`, `source_text_bbox`, non-negative `maximum_delta_pt`, and `measurement_basis: actual_candidate_text_span_bbox`. `render.align` must match. Candidate QA compares the reopened target text's actual left inset, right inset, or center offset with the source value; a planned target bbox cannot satisfy this contract.

New English exceptions use `pdf-tw-localize/english-allowlist/v3`. The document scope has `document_id` and pages. Each entry has exact text, an approved type, a substantive reason unrelated to tool difficulty, exact page and segment scope, and a `user_instruction`, `translation_policy`, or `protected_content_policy` basis with a concrete reference.

The renderer copies selected English source pages into a new PDF, applies mapped redactions while preserving source images and line art, embeds the declared zh-TW font, clones Catalog OutputIntents and decoded ICC streams, and records component contracts, mask mode, fitted size, and exact line bboxes. It refuses an existing output path and fails when content cannot fit above the declared minimum. `qa_preserved_visuals.py` separately proves page-aware zero-intersection behavior, decoded-image identity when applicable, Catalog/ICC identity, exact 300 dpi region identity, and compound-member preservation.

`MACHINE_QA`, `VISUAL_REVIEW`, and `USER_ACCEPTANCE` remain independent. Rasterized English inside images cannot be cleared by extractable-text scanning alone; it requires declared image-text segments plus an individually opened 300 dpi comparison.
