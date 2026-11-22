# Pye guarded-path execution counts

The original release MicroPython guest completed the queued `update-package-version` edit on each of nine separately deployed training replays. Every launch returned the expected 166-byte editor buffer and passed the sealed-cache checks. Each appended two to six translations. The following counts cover the complete guest run with the debugger attached. Timed trials ran separately.

## Guarded UDIV

Each row counts one physical splice in its own run. The debugger observed the guarded entry, the nonzero inline arm and the retained zero-divisor helper CALL. The entries equaled inline visits plus fallback calls for every site. Guest addresses below identify the start of each translation block.

| TB start | Guarded entries | Nonzero inline | Zero-divisor helper CALL |
| ---: | ---: | ---: | ---: |
| 0xe8fa | 8,525 | 8,525 | 0 |
| 0xe73c | 4,186 | 4,186 | 0 |
| 0xe88e | 1,725 | 1,725 | 0 |
| 0x11f3a | 80 | 80 | 0 |
| 0x11e44 | 80 | 80 | 0 |
| 0xe890 | 55 | 55 | 0 |
| 0x11f42 | 29 | 29 | 0 |

These are 14,680 observed inline divisions across seven independent site replays. The retained CALL bytes remain present in every generation. No zero divisor reached those CALLs in these runs. [Audited counts](hot-sites.json) · [Source-site bounds and splice certificates](../pye-b4-native-training/README.md)

## Single-target B3 guards

For each guard kind, the initial diagnostic selected the eligible source TB with the highest ordinary training entry count. Hardware breakpoints counted entry to the emitted guard, its matched slot instruction, the reset path immediately after that instruction and the original lookup fallback. A conditional predicate miss and a matched slot awaiting installation have different consequences. The registered GENERAL reset exits to the dispatcher, while a SPEC reset reaches the original lookup helper.

| Kind | Source TB → expected PC | Guard entries | Predicate matches | Predicate misses | Matched slot linked | Matched slot reset | Lookup fallbacks |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| GENERAL | 0x20ed8 → 0x20f24 | 423 | 423 | 0 | 422 | 1 | 0 |
| SPEC | 0x21080 → 0x3632 | 1,062 | 1,062 | 0 | 1,061 | 1 | 1 |

The GENERAL source TB had 20,527 ordinary training entries. Only 423 entries reached this physical guard in its instrumented transformed run. Assigning the TB total to this guard would significantly overstate its traffic. Both sampled guards matched on every visit. The single SPEC lookup fallback occurred on a matching, unlinked slot. These counts apply to the two measured guards. [Raw path counts and plans](hot-sites.tgz)

## Polymorphic B3 guards

A follow-up selected one single-target and one two-target GENERAL guard using only a fresh ordinary training profile. Each source contained a single physical guard. Every selected target and at least one other destination had exact, unflagged lookup events. Within each target count the selector preferred the source with the most observed lookups to other destinations, then the highest total for admitted targets. The same release edit passed on both diagnostic runs. The caches appended four and eight translations respectively.

| Source TB | Matched PCs and actual visits | Guard visits | Lookup fallback | Ordinary-profile visits to the same targets and other destinations |
| --- | --- | ---: | ---: | --- |
| 0x1238 | 0x13f26 × 33 | 36 | 3 | 33 matched, 3 other |
| 0x39fa | 0x42a0 × 2,139 and 0x4296 × 721 | 3,901 | 1,041 | 2,140 first, 721 second, 1,041 other |

At 0x1238, 32 matched jumps used a linked slot and one encountered a reset. At 0x39fa, the second comparison accounted for 721 matches, or 18.48% of all visits. The remaining 1,041 visits (26.69%) reached the original lookup path. Four simultaneous hardware breakpoints counted guard entries, both target matches and fallback. Those four counters give no per-slot reset count or elapsed marginal benefit. [Audited counts](polymorphic.json) · [Frozen plans, complete observations and training profile](polymorphic.tgz)

## Reproduction

Run `scopt-env/bin/python tools/trebuchet/applications/pye/path_counts.py "$work" "$out"` after preparing the matching release product, runtime, plugin and scopt bridge. The driver creates each fresh checked deployment and executes FastDyn’s generated QEMU command in batch GDB. Up to four host instruction hardware breakpoints count the decoded sites without rewriting guest or cached host instructions. Each observed site’s first live opcode prefix must match the archived plan. A positive source count and conserved path counts are required. Debugger wall times in the archive are diagnostic costs.

The archive SHA-256 is `02b950df9782d9cdb6ef0b3727e05e55979bbe650f2473e67fcfe7ed41c1a6d7` from hosted execution 44995090950. After extraction, run `scopt-env/bin/python tools/trebuchet/applications/pye/audit_path_counts.py diagnostics` to verify the counts, byte checks, deployment proof, input, full UART transcript and returned document against the frozen training job. [Counting driver](../../../applications/pye/path_counts.py) · [Independent archive audit](../../../applications/pye/audit_path_counts.py)

Use `--polymorphic-only` to reproduce the two mixed-target observations. The follow-up archive from job 44996094997 has SHA-256 `1b054f6b334071bd421ed6725bac0b424e3ad6bac83c1d3fed1de8775bc94bb4` and uses the same audit command. [Hosted source and archive identities](hosted.json)
