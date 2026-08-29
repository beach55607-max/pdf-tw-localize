# QA contract

A successful write or render is not acceptance. Keep automated evidence, real page viewing, and user acceptance as separate states.

## MACHINE_QA

Automated checks must report scope and evidence. They may flag visual suspicions but cannot decide that a page looks correct.

### Input and output integrity

- Source SHA-256 is recorded and unchanged.
- Source, optional baseline, and candidate open; requested pages render.
- Page counts, sizes, orientations, and encryption state match the declared scope.

### Content and mapping

- Model numbers, part numbers, values, units, symbols, warning levels, steps, and approved protected terms remain intact.
- No placeholders, replacement glyphs, control characters, obvious omissions, duplications, or wrong-region assignments remain.
- No unapproved extractable body English remains. User-permitted English baked into images is allowed; automation must not mistake it for a defect or a completed visual review.
- A scoped UI-English allowlist must name the visual, exact source phrase, zh-TW mapping, and permitted guidance segments. Automation must reject the same English outside those segments instead of treating the phrase as globally allowed.

### Geometry and legibility evidence

- Report overlap, out-of-bounds text, suspiciously small inserted spans, and source-to-candidate font ratios with page, text, and bounding boxes.
- Verify requested font roles against both the supplied font program and the actual candidate text span. A requested bold segment rendered as a thin, regular, or unresolved variable-font face is blocking.
- Verify every declared `text-alignment/v1` contract against the reopened candidate's actual text-span union. Compare source and target left inset, right inset, or center offset as declared; exceeding `maximum_delta_pt`, missing actual spans, or substituting planned boxes is blocking.
- Verify every rendered line's actual 24-bit sRGB foreground color against its source-bound or inspected declaration. White-on-dark and other non-black source treatments must remain exact. Multiple source colors without per-fragment or inspected effective-color evidence are blocking.
- Validate every `table-cell-phrase/v1` group before rebuild: ordered members, same table cell, complete joined source phrase, and complete joined target phrase must all match.
- Ordinary translated text below 6 pt or 75% of its mapped source size is blocking.
- Source-small image or UI labels require page-specific 300 dpi evidence when they are reviewed or redrawn; automation must not grant a general exception.
- For `preserve_source_visual_with_textual_guidance`, compare source and candidate decoded image samples and pixel dimensions, require an exact 300 dpi rendered-region match, and prove that no rebuild mask or inserted text intersects the visual bbox. A matching embedded image alone is insufficient because an overlay could still cover it.
- Before any bbox-intersection decision, verify that both objects refer to the same source page and the declared source-to-candidate page mapping. Identical coordinates on different pages have intersection area zero. Report page-mismatch rejections and keep cross-page false intersections at zero.
- For raster visuals, report decoded-image identity separately from 300 dpi rendered-region identity. For vector components, decoded-image identity is `NOT_APPLICABLE_VECTOR_COMPONENT`; compare the complete rendered component and its member roles instead.
- Compare source and candidate Catalog `/OutputIntents` entries and decoded `/DestOutputProfile` ICC stream SHA-256. A missing or changed OutputIntent is blocking. Never use a DeviceRGB render hash as evidence that an embedded CMYK image changed when its decoded samples match; label decoded-image and color-managed-render findings separately.
- For compound components, bind component group, member roles, relations, and mask policy to the rebuild report. Verify Dingbats markers and preserved drawings independently from the replaced live text.
- For `pdf-tw-localize/compound-component/v2`, run `qa_compound_components.py`. Re-read every declared source object and decoded stream, require exact stream hashes, uniquely match all ordered path signatures, prove outlined replacement signatures are absent, prove preserved member signatures remain exactly once, and prove adjusted rules have zero source-signature residue plus exactly one translated target signature. A missing, duplicate, changed, clipped, interwoven, transformed, or unknown path is blocking.
- Validate `contains`, `adjacent`, `avoid`, `align_center_y`, and `align_optical_offset_y` relations against candidate member bboxes. `adjacent` and `avoid` pass only when intersection is zero and Euclidean clearance is at least `minimum_clearance_pt`; record the equivalent distance at 300 dpi. `align_center_y` compares geometric centers. `align_optical_offset_y` must use the reopened candidate's uniquely matched actual text-span bbox and passes only when the error from signed `expected_target_minus_member_center_pt` is no greater than manifest `maximum_delta_pt`; record actual offset, error, and 300 dpi equivalents. Planned render boxes cannot satisfy this gate. Zero intersection alone is not a visual-spacing or alignment PASS.
- For declared same-page repeated-component templates, normalize every instance to its declared anchor and compare every listed local bbox metric for corresponding semantic members. Text width may vary only when width-dependent metrics are explicitly omitted for that member. Missing anchors or members, duplicate instance IDs, different semantic keys, cross-page references, unsupported metrics, and any delta above `maximum_delta_pt` are blocking.
- Resolve every `dependent_geometry` member from the actual candidate bbox of its named translated-text driver. Compare all four expected edges with the adjusted member reported by the rebuild, enforce bounds and minimum size, and require the role's exact adjustment evidence. A missing/ambiguous driver, preserved-but-dependent member, malformed edge formula, circular/conflicting declaration, out-of-bounds target, or unsupported policy is blocking.
- For `composited_visible_layouts`, compare what remains visible after declared opaque members cover a horizontal subject. Require full-thickness intersection for every occluder and exactly one remaining interval, then compare anchor-local start, end, length, thickness, and cross-axis center as declared. Equal underlying path bboxes cannot pass when the effective visible extents differ. A 300 dpi crop must still be opened because this analytic gate is machine evidence, not visual acceptance.
- Validate `english-allowlist/v3` structure and exact page/segment scope before residue scanning. A missing reason or basis, a tool-difficulty rationale, or an exception outside its declared scope is blocking.
- For every vector drawing comparison, use the complete ordered operator stream. Preserve duplicates and all coordinates for `l`, `re`, `qu`, and cubic `c`; a cubic contains its start, both control points, and endpoint. Do not sort or deduplicate operators. Normalize only binary-float representation noise, compare coordinates with an absolute tolerance of `0.001 pt` (about `0.00035 mm`, roughly 240 times smaller than one 300 dpi pixel), and report that tolerance with the evidence. Bbox, fill, line endpoints, or item count alone cannot establish identity. An unknown, missing-field, non-finite, or malformed operator is blocking and must be reported as fail-closed evidence.
- When live text is removed from a separate colored or rounded background, verify the background drawing remains and inspect the 300 dpi crop for a rectangular fill seam. `remove_text_only` evidence is not a visual PASS by itself.
- When `adjust_background` is declared, treat only its exact source-rectangle-to-target-rectangle change as intentional. Require one source operator, one verified target drawing, zero target intersection with every avoid region, preservation of the underlying neighboring container, and a 300 dpi seam/corner review. Do not waive unrelated missing line art.
- When `adjust_vector_rule` is declared, treat only the exact signature-bound path translation as intentional. Require the declared non-zero delta and target bbox, one uniquely matched source path, zero source-signature residue, one exact translated drawing signature, one exact translated content-path signature, unchanged graphics state/operator order, and a 300 dpi optical review. Do not accept an overpainted duplicate line or waive unrelated line-art changes.

`MACHINE_QA_PASS` applies only to the reported page scope. A selected-page regression is not a full-document pass.

## SEMANTIC_QA

For every declared semantic binding, automated validation must prove:

- the binding ID and translation assertion are unique and mapped one-to-one;
- each exact protected source value remains in the target phrase assigned to the same parameter and role;
- values from paired bindings are not mixed into the same asserted phrase;
- declared mode and condition fields match and their required target cues remain in `zh_TW`;
- source-derived clarification mirrors the declared comparator and consequence, keeps both cues in the same narrow value phrase, and uses only verified context refs;
- every required same-document context reference exists and is `VERIFIED_SOURCE_CONTEXT`.

Missing or unverified source context is `NEEDS_REVIEW` and blocks rebuild. Missing, duplicated, unknown, swapped, text-detached, comparator-dropped, consequence-dropped, or source-unsupported clarification assertions are blocking. A helpful-sounding hypothetical example cannot satisfy this gate. `SEMANTIC_QA_PASS` is distinct from both protected-token counting and visual review: a visually polished page can still be technically misleading.

## VISUAL_REVIEW

Run `render_review.py`, then open every page's comparison image individually at readable zoom. For risky image, UI, dense-table, or small-label pages, use 300 dpi or higher.

Contact sheets are navigation only. `RENDERED` means files exist; it never means reviewed.

Each reviewed page must record:

- `visual_status`, `image_text_status`, `geometry_status`, and `legibility_status`;
- reviewer and a timezone-aware, page-unique review time;
- page reference and review DPI;
- the exact comparison-image SHA-256 viewed;
- a concrete, page-specific observation that is neither generic nor duplicated.

For `image_text_status`, use `PASS` when required image text was checked and is correct, `NOT_APPLICABLE` when the page contains only user-permitted or nonessential image English, and `FAIL` when required image/UI text is wrong or unreadable. This field never exempts the page from overall visual, geometry, and legibility review.

`validate_visual_review.py` rejects stale or missing image hashes, generic or repeated notes, repeated review times, missing page references, incomplete status sets, and comparison evidence that does not match the current files. These checks make bulk stamping harder; they do not prove honesty. The reviewer must still inspect each page.

Visual states:

- `NOT_CHECKED`: no page has a valid completed review.
- `PARTIAL`: some but not all requested pages have valid completed reviews.
- `VISUAL_REVIEWED`: every requested page is hash-bound and individually reviewed with no failed status.
- `BLOCKED`: at least one requested page has a confirmed visual, image-text, geometry, or legibility failure.

## USER_ACCEPTANCE

Only the user may set `USER_ACCEPTED`. Until explicit confirmation, report `USER_ACCEPTANCE: NOT_CHECKED` even when internal machine QA and visual review are complete.

## Candidate decision

- `BLOCKED`: any security block, corrupt input, confirmed content loss, unreadable ordinary text, overlap, clipping, duplicated heading, untranslated required region, wrong edition label, or failed page review remains. User-permitted English baked into an image is not by itself blocking.
- `INTERNAL_QA_COMPLETE`: the declared scope has machine QA, applicable semantic QA, and valid visual review with no blocking finding. This is still a candidate when user acceptance is `NOT_CHECKED`.
- `NEEDS_REVIEW`: required evidence is uncertain, incomplete, stale, or not checked.

Never rename a candidate to bypass a gate.

## Required evidence

Keep the secure-preflight report, page inspection and route map, region mapping, glossary and backend record, machine QA report, comparison images, review manifest, review-validation report, and all bound hashes.
