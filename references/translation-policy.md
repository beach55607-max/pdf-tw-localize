# Translation policy for Taiwan Traditional Chinese

## Output language

- Use Traditional Chinese as used in Taiwan.
- Use full-width Chinese punctuation in prose.
- Insert a half-width space between Chinese and Latin model codes or numeric units when it improves readability.
- Keep UI labels concise enough for their original controls.
- Avoid Mainland China vocabulary when a standard Taiwan term exists.

## Monolingual replacement

- Replace source English directly in its semantic region.
- Do not append Chinese after English.
- Do not retain English headings above or below translated headings.
- English may remain when it is a protected proper name, model/part number, required standard identifier, trademark, URL, email address, code token, user-approved label, or user-permitted text baked into an image. This permission does not cover ordinary extractable body text or the language-edition label.
- When a user-approved screenshot is preserved, adjacent zh-TW guidance may retain only the exact source UI terms needed for navigation in `中文（Source UI）` form. Bind each phrase to a visual ID and guidance segment; do not turn this exception into bilingual headings or general body text.
- A language-edition label is output metadata, not a protected token. Replace `ENGLISH` with `繁體中文`; never translate it as `英語`.

## Image text boundary

- Do not force every English word baked into an illustration, photograph, screenshot, or decorative graphic into Chinese.
- Translate or redraw image text only when the user requests it or when it materially affects operation, safety, or core understanding.
- Functional UI labels and state changes require a contextual decision; decorative labels and nonessential packaging text may remain English when permitted.
- If a redraw would reduce fidelity or legibility, preserve the original image text and record the exception in the page review.
- If the user explicitly requests preservation, keep the original pixels and explain necessary operation in adjacent localized text unless safety or core understanding remains unresolved. Do not put a translation mask or overlay inside that visual.
- Preserving pixels or vector content does not by itself freeze the visual object's page coordinates. When target-language grammar needs an inline icon to move, relocate the complete source-bound object without cropping, scaling, redrawing, or editing its interior. Keep placement fixed only when the user explicitly requires it or when the visual is registered to a protected frame, screenshot, diagram, table cell, or other positional contract.
- Do not turn translation difficulty into an English exception. A retained English brand, model, standard identifier, source UI, or protected proper name requires an exact `english-allowlist/v3` type, substantive reason, page-and-segment scope, and user or policy basis. Ordinary headings, warnings, and safety labels remain required translation unless the user explicitly changes that requirement.
- A permitted image-English exception does not waive full-page visual review.

## Context packet and region mapping

Translate pages as related semantic regions, not as a sequence of extracted lines. Before translating a region, include:

- page purpose and heading hierarchy;
- preceding, following, and neighboring regions;
- table row and column headers, units, footnotes, and condition pairs;
- screenshot, controller, or diagram state and related labels;
- protected tokens and selected glossary terms;
- stable source-region IDs and intended target roles.

Create a one-to-one source-to-translation region map before layout. Do not refill translated strings by extraction order. A bare word such as `Under`, `Direct`, `Off`, or `Back` must be resolved from its table, control, or sentence context.

## Protected content

Preserve exactly unless the source is demonstrably wrong and the user authorizes correction:

- model and part numbers
- electrical ratings, temperatures, pressures, dimensions, capacities, percentages, dates, and tolerances
- units and symbols
- warning levels and numbered steps
- connector names, error codes, terminal labels, and regulatory references
- URLs, email addresses, filenames, and QR destinations

Do not translate a protected token into look-alike full-width characters.

## Technical proposition and value-role binding

Protected-token preservation alone does not prove semantic preservation. For specifications, paired values, thresholds, ranges, defaults, fallback behavior, and condition-dependent settings, preserve the complete proposition: subject, condition, operating mode, parameter, value, unit, comparator or role, and consequence/default status.

- Bind each protected value to its source parameter and role before translation.
- When a value is paired, counterintuitive, or defined elsewhere, inspect and cite verified context from the same source document before translating it.
- Return a narrow target phrase for each semantic binding so deterministic validation can prove that the assigned value and translated parameter cue remain together.
- Do not invent a universal ordering rule such as cooling always being numerically lower than heating. Preserve the source relationship and explain its documented mode.
- If the relevant mode, condition, or same-document definition is not verified, keep the segment `NEEDS_REVIEW` rather than producing a fluent guess.

## Source-derived clarification without overtranslation

A literal technical label can preserve the words and still hide the operating logic. When the user or page context shows that a value is difficult to interpret, add only the minimum source-derived comparator and consequence needed to understand it.

- Prefer `冷房控制門檻：30 °C（室溫高於此值時才進行冷房）` over a bare `冷房設定值 30 °C` only when verified source context explicitly supports both the comparison and the consequence.
- Use `clarification_mode: source_derived_inline` or `source_derived_note`; require exact same-document context refs and matching comparison/consequence assertions.
- Keep the explanation declarative. Do not invent a household scenario, comfort claim, deadband, delay, hysteresis, energy-saving outcome, or product capability.
- Examples are optional and are never generated merely because a sentence is counterintuitive. If the user separately requests an example, keep it outside the translated source proposition, label its status, and use no new technical claim that lacks source support.
- If the source supports only “cooling is not performed below the cooling setpoint,” do not silently strengthen it to “the whole unit turns on at this exact temperature.”

## Terminology precedence

1. Current-run user glossary or explicit user choice.
2. One domain pack loaded from an explicit path with the expected pack ID, version, and digest.
3. Public general Taiwan glossary.
4. Contextual translation.

Apply longest source phrase first and case-insensitively where safe. Log conflicts instead of silently choosing.

Never scan for or auto-load a domain pack. When no pack is loaded, general translation continues and every pack-dependent terminology or regression claim remains `NOT_CHECKED` or `BLOCKED`. A prior run's domain result cannot be reused as a public-core PASS.

Treat a phrase split by visual line breaks or extraction blocks as one term when its table cell, heading, or label context makes it semantically whole. Bind the ordered fragments and translate the complete phrase before distributing the target fragments. Do not translate isolated components literally when an established Taiwan domain term exists.

## Style

- Translate meaning, not English word order.
- Keep warnings direct and unambiguous.
- Prefer established Taiwan technical terms over literal calques.
- Keep headings compact.
- In numbered procedures, begin with the action and preserve the step boundary.
- For two inline visual choices, write one Chinese sentence in semantic order—prefix + complete visual A + `或` + complete visual B + suffix. Do not imitate an English detached-icon row or create leading whitespace merely to retain source word order.
- Do not invent product capabilities, safety claims, specifications, or operating conditions.
- Keep a controller or diagram's related labels mutually consistent across all states on the page.

## Back-translation spot check

For safety warnings, specifications, installation constraints, and maintenance procedures, independently compare the Chinese meaning against the source after layout. Check negation, thresholds, conditional language, subjects, operating modes, value roles, and consequences.
