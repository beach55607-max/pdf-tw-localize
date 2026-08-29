# Third-party notices

This public core contains original Skill code and two Noto Sans TC test-font instances. It does not vendor PDFMathTranslate, BabelDOC, Hy-MT2, OpenCC, PyMuPDF, pypdf, Pillow, PyYAML, or FontTools code.

## Bundled font assets

`assets/fonts/NotoSansTC-Regular.ttf` and `assets/fonts/NotoSansTC-Bold.ttf` are static instances derived from the official Google Fonts Noto Sans TC variable font. They are distributed under the SIL Open Font License 1.1. The complete license text is included as `assets/fonts/OFL.txt`; source revision and digests are recorded in `references/font-fixture-provenance.md`.

## External runtime and optional tools

The implementation references external packages with the following upstream-declared licenses:

- PyMuPDF: GNU AGPL v3 or a commercial license.
- pypdf: BSD 3-Clause.
- Pillow: MIT-CMU.
- OpenCC: Apache 2.0.
- PyYAML: MIT.
- PDFMathTranslate: GNU AGPL v3.
- BabelDOC: GNU AGPL v3.
- Hy-MT2: the cited official model card declares Apache 2.0; verify the exact checkpoint.
- FontTools: MIT.

Official source and license URLs, roles, version strategies, and engineering boundaries are recorded in `references/dependency-manifest.json`.

## Release boundary

The public core is released under `AGPL-3.0-only`; the complete project license is in `LICENSE`. Runtime dependencies remain external and are resolved from the hash-bearing release lock files. Optional models and translator backends are neither bundled nor approved merely by being named here. Modified distribution or network-service use must satisfy the applicable AGPL source and notice obligations. This file records engineering provenance and is not legal advice.
