# Public synthetic regression policy

Read this file before changing translation routing, component preservation, geometry, fonts, data-pack loading, or QA behavior.

## Evidence boundary

- Public tests contain only invented words, paths, coordinates, colors, images, hashes, and IDs.
- Real document identities, locators, candidate hashes, protected names, and user feedback belong only in an explicitly loaded private data pack.
- Generate each candidate directly from its synthetic source. A prior localized output is comparison evidence only and never a generation base.
- A previous PASS never survives changed source, manifest, core code, pack identity, or user rejection.

## Required invariant groups

1. **Stable IDs and semantic spans**: exact source coverage, explicit mapping, protected tokens, paired-value bindings, table-cell phrases, and unresolved context fail closed.
2. **Compound components**: independently inventory live text, outlined text, icon, frame, rule, background, and neighboring container. Remove only the exact selected text or path.
3. **Ordered path signatures**: preserve operator type, order, repetition, and every coordinate for lines, rectangles, quadratics, and cubics. A cubic includes start, both control points, and endpoint. Any changed control point, operator, stream, count, missing field, duplicate match, clip, transform, or unknown operator is blocking.
4. **Layout semantics**: verify left/right/center anchoring from reopened text spans, exact source-bound foreground color including white on dark artwork, ordered table-cell phrases, dependent objects, minimum clearance, repeated local layout, optical offsets, and composited visible extents.
5. **Preserved visuals**: compare decoded raster identity separately from 300 dpi rendered-region identity, enforce page mapping before intersections, prohibit overlays inside preserved visuals, and verify OutputIntent plus decoded ICC identity.
6. **Data packs**: cover exact successful loading, absence, identity/hash/digest/version incompatibility, duplicate IDs, unknown fields/types, traversal, reparse-point escape, inventory mismatch, and glossary precedence.
7. **Hygiene**: reject private paths or identifiers, document-specific hashes/evidence, bytecode, cache folders, temporary outputs, reparse points, and forbidden file types from the public package.

## End-to-end fixtures

Maintain at least two fully synthetic PDF workflows:

- one structured page with right-aligned text, white text on a dark bar, a table-cell phrase, preserved artwork, and source-bound colors;
- one vector-heavy page with outlined text, icon/frame/rule/background separation, a dependent related object, a visible-rule interval, and declared minimum clearance.

Render every page at 300 dpi, open each rendered page individually, bind observations to image hashes, and report `MACHINE_QA` separately from `VISUAL_REVIEW`. `USER_ACCEPTANCE` remains `NOT_CHECKED`.
