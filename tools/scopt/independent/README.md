# Independent `.sc` parsers

These two scripts are deliberately separate implementations of the supercache
file format. scopt runs them on every artifact it writes and compares their
output field by field with its own reader. A disagreement fails validation.

| script | format | written from | used by |
|---|---|---|---|
| `scparse.py` | `.sc` v5 (header, records, checksums) | the v5 file layout of the QEMU loader (`tcg/supercache.c`) | V1 (`scopt.validate.scparse_compare`) |
| `sc6parse.py` | `.sc` v6 (v5 plus the extension table: variants, slots, links) | the v6 loader in `tcg/supercache.c` (QEMU branch `scopt`) | V11 (`scopt.formats.sc_v6.sc6parse_compare`) |

Rules that keep them useful as a cross-check:

- Standard library only. Never import `scopt`.
- Change them only from the QEMU loader source, never by copying from
  `scopt/formats/`. If scopt and a parser disagree, find out which one
  matches the loader before changing either.

scopt finds them here by default. `SCOPT_SCPARSE` and `SCOPT_SC6PARSE`
override the paths.
