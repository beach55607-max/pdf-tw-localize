# Test-font fixture provenance

The candidate bundles two static test instances derived from the official Google Fonts Noto Sans TC variable font. This avoids platform-specific font paths and makes synthetic tests reproducible.

## Locked source

- Upstream: `google/fonts`
- Family path: `ofl/notosanstc`
- Source revision: `3be1884c48c3e45b52ecc725676a08f87776373e`
- Revision page: `https://github.com/google/fonts/commit/3be1884c48c3e45b52ecc725676a08f87776373e`
- Variable-font URL: `https://raw.githubusercontent.com/google/fonts/3be1884c48c3e45b52ecc725676a08f87776373e/ofl/notosanstc/NotoSansTC%5Bwght%5D.ttf`
- Variable-font SHA-256: `864727D210D54F2537BBE23B3A839436C3992AF72DE9322AF5270897246BD44F`
- Upstream OFL URL: `https://raw.githubusercontent.com/google/fonts/3be1884c48c3e45b52ecc725676a08f87776373e/ofl/notosanstc/OFL.txt`
- Upstream OFL SHA-256: `1C05C68C34F9708415AADA51F17E1B0092D2CEA709BF4A94CD38114F9E73D7D9`
- License: SIL Open Font License 1.1; the complete notice is bundled as `assets/fonts/OFL.txt`.

## Deterministic instances

FontTools 4.63.0 instantiated the `wght` axis at 400 and 700 with `--static`, `--update-name-table`, and `--no-recalc-timestamp`.

| Candidate asset | Style name | SHA-256 |
|---|---|---|
| `assets/fonts/NotoSansTC-Regular.ttf` | Noto Sans TC Regular | `82559D4A2AB69224DE5CB6191DDF40B0027C4B519FF891E9342D354045285014` |
| `assets/fonts/NotoSansTC-Bold.ttf` | Noto Sans TC Bold | `F298E7332E462777AECC30ADDDC7330C5A884B9C246567FC9F4AFA44C7753775` |

Both outputs have no remaining variation axis. Tests independently verify the regular/bold role in the font program and in reopened PDF spans.

## Runtime discovery

`scripts/font_discovery.py` checks, in order:

1. explicit `PDF_TW_LOCALIZE_FONT_REGULAR` and `PDF_TW_LOCALIZE_FONT_BOLD` paths;
2. these bundled OFL fixtures;
3. `fc-match` when available;
4. dynamically discovered platform font directories derived from environment or operating-system conventions.

There is no fixed Windows drive, user profile, or font filename path.
