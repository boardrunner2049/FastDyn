# `.scm` v1 as written by the WP3 capture (`qemu-sc-capture.patch`)

Design §2 is authoritative; `docs/contracts.md` holds the frozen layouts. This
note records how the C writer fills in the points §2.3-2.5 leave open, so that
readers (WP4) and the importer cross-check (WP7) can rely on them.

## Chunk order

`RUN_INFO` (seq 0) → `HELPER` chunks interleaved with `TB` chunks (a helper's
chunk always precedes the first TB that references it) → `MAPS(first_commit)`
and `RAMBLOCKS` right after the **first** TB chunk → … → `MAPS(exit)` → `END`.
A run that commits no TB (e.g. a warm run) has no `MAPS(first_commit)` and no
`RAMBLOCKS`.

## Fields

| Field | Value |
|---|---|
| FileHeader `pid`, `created_realtime_ns` | `getpid()`, `CLOCK_REALTIME` at H1 |
| TB `commit_seq` | 0-based count of TB chunks before this one in the file (not the chunk `seq`) |
| TB `attempts` | `tcg_gen_code` calls for this TB (restarts with -2 reuse the same `tb` and count up) |
| TB `slot` | `.sc` `header.record_count` at commit; dense from RUN_INFO `header_record_count_at_load` |
| TB `pool_start` | `data_gen_ptr − code_buf`, or `0xFFFFFFFF` without a pool |
| TB `ldst_end` | code offset after the last slow path (= `body_end` when there is none) |
| `Insn.host_start` | code offset at the `insn_start` op; `Insn[0].host_start == insn0_start` |
| `Site.insn_off` | first byte of the instruction holding the field, prefixes included. GOTO_TB: the `E9` byte (the alignment nops before it are separate instructions). LDST_FAST_JCC: `field − 2` (`0F 85`). XOR0 / GOTO_PTR: `field_off == insn_off`, `width = 0`. POOL_REF: `0xFFFFFFFF` (see below) |
| `Site.insn_idx` | the current insn when emitted; inside a slow path (LDST_* sites and the MOVI_LDST_RA LEA) the insn of the issuing qemu_ld/st op; `0xFFFFFFFF` before the first insn_start (TB-start check) |
| `Site.value` | branches: absolute destination; LABEL_*: label address resolved at H15; GOTO_TB: absolute target encoded in the final bytes (the reset target `field+4` when `final_field == 0`); MOVI_*: the movi argument; POOL_REF: `data0`; GOTO_PTR: 0 |
| `Site.aux` | HELPER_CALL: helper_id; EXIT_TB_*, MOVI_EXIT_TB_PTR: `a0 & 3` (0 for EPI); GOTO_TB: slot; GOTO_PTR: register; LDST_*, MOVI_LDST_RA: ldst index; POOL_REF: pool index; others 0 |
| `Site.final_field` | low `min(width, 4)` bytes of the field after `tcg_resolve_relocs`, zero-extended |
| `OpMark.insn_idx` | `0xFFFF` before the first insn_start |
| `OpMark.helper_id` | CALL op marks only, set by H5 |
| `StaticExit.insn_idx` | `DisasContext.base.num_insns − 1` at `gen_goto_tb` |
| HELPER intern key | `(TCGHelperInfo *, func)`; qemu_ld/st slow-path helpers are not interned (their sites carry the ldst index) |

## Which pool users get a POOL_REF site

A pool-indirect call/jmp (`FF 15/25 [rip+d32]`) is recorded once, by H7, as its
HELPER_CALL / LDST_* / EXIT_TB_* site with `enc = POOL_INDIRECT` and
`value = destination`; its `Pool` row joins by `user_field_off == field_off`.
Every other pool user (vector constants, not seen on the Cortex-M fixtures) gets
a separate POOL_REF site at H14. H14 does not know that instruction's start,
so `insn_off = insn_idx = 0xFFFFFFFF`; the importer finds the instruction that
contains `field_off`.

## Counters in END

* `discarded`: staged TBs dropped without a commit: codegen restarts (-1/-2),
  one-shot TBs, `existing_tb` rewinds, `encode_search` overflow, and an
  uncommitted TB at exit.
* `overflow` / `capture_error`: number of committed TB chunks with that flag.
* `write_errors`: failed or suppressed writes. After the first failed write
  nothing more is written; the file keeps its valid prefix.

A TB whose commit finds no matching READY staging is still written, with
`flags = capture_error` and empty arrays (record, code FNV and guest bytes
only), so the importer pins it rather than calling it `no_meta`.

## Error exits (H1)

QEMU exits 1 when `QEMU_SC_CAPTURE` is set and: no supercache is configured;
`supercache-flat=on`; `QEMU_SC_PROFILE` is also set; the path is relative;
the file already exists (`O_EXCL`); `QEMU_SC_CAPTURE_LEVEL` is anything but
`reloc` (`ops` is reserved for Tier 1 and not implemented).

## Files and descriptors

The `.scm` fd and a `/proc/self/maps` fd are both moved to ≥1000
(`F_DUPFD_CLOEXEC`), so QEMU's other fd numbers are the same with capture on
or off. MAPS is read with `pread` from that fd into a static 256 KiB buffer.
