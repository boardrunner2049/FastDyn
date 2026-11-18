# B1 final design: capture, Profile v1, artifact formats and the `scopt` optimizer (B2 layout on the unmodified v5 loader)

Status: **final synthesized design, ready to implement.** It is pinned to QEMU 12f5e688 with the FastDyn compat patch (the uncommitted working-tree changes in `src/qemu`, from `patches/qemu-fastdyn-plugin-icount.patch` (QEMU commit `00c19f7ee0`)) and to FastDyn 0e13b739. Host is x86-64 Linux; guest is Cortex-M Thumb with CF_PCREL.

How it was built: Design 1 (layout-first) is the base. It got the best feasibility score, and its hook placement is the most precise. The design then takes Design 2's identity, edge, recovery and qualification ideas and Design 3's validation and run-proof ideas. Every factual error the two reviews found is fixed; section 11 lists each fix.

Citations: `file:line` refers to `src/qemu` (the QEMU tree, `third_party/common/qemu` in this repository) unless prefixed with `fastdyn/` (meaning `src/fastdyn`, the FastDyn repository root). Every citation used for a hook location was re-read against the tree for this document. Facts marked **[B0]** were measured on the maintainer's evaluation data (not in the repository) (B0 review findings A1-A3).

---

## 0. Ground truth this design relies on (verified)

| # | Fact | Evidence |
|---|---|---|
| G1 | `.sc` v5: 80-byte header, arena image at file offset `0x10000+off`, 104-byte records at `0x10000+code_size`. FNV-1a-64 checksums. | supercache.c:29-72, 131-142 |
| G2 | Under CF_PCREL the lookup key uses `pc=0`, so one TB serves every vaddr alias. `record.pc==0` in all 163 records [B0]. | cpu-exec.c:208-209; supercache.c:198-206, 835-841; translate-all.c:307-309 |
| G3 | The loader rebuilds the TB struct at `buffer+tb_offset`, resets the goto_tb jumps and rehydrates plugins. Host lookup goes through the region tree (`tcg_tb_insert`) in normal mode. | supercache.c:768-830 |
| G4 | One-shot TBs (`page_addr0==-1`) return at translate-all.c:518-522, **before** `tcg_supercache_record` (539). An `existing_tb` returns at 531-538, also before the record. The guard at supercache.c:707-710 is therefore never reached for one-shot TBs. | translate-all.c:505-541 |
| G5 | `tcg_gen_code` resets its buffer at tcg.c:6926-6928 on every attempt, including the -1/-2 retries. `pc_start` is the guest vaddr `s.pc`. | tcg.c:6841, 6926-6928; translate-all.c:258, 326-384 |
| G6 | `tcg_out_jxx` emits branches to labels that already have a value **without** `tcg_out_reloc`. Only the forward paths call `tcg_out_reloc`. | tcg-target.c.inc:1506-1546 |
| G7 | `new_pool_alloc` (tcg.c:674-685) does not see the data; callers fill `n->data` after it returns. `tcg_out_pool_finalize` (tcg.c:795-833) is the one place where each user site, its deduplicated slot and its value are all known. It also runs once for the **prologue** (tcg.c:1906-1910). | tcg.c:674-760, 795-833, 1906-1910 |
| G8 | `tcg_out_movi_int` encodings: xor (0 and `!carry_live`), `mov r32,imm32` (fits u32 **or** type I32), `mov r/m64,simm32`, 7-byte RIP LEA, movabs. | tcg-target.c.inc:1091-1122 |
| G9 | `tcg_out_branch` emits `E8/E9 rel32`, or `FF 15/25 [rip+d32]` plus a pool label when out of range. Only `tcg_out_call`, `tcg_out_jmp` (exit_tb, slow-path return) and slow-path helper calls reach it. | tcg-target.c.inc:1938-1982, 2090-2114, 2629-2638 |
| G10 | The slow paths are emitted by the generic `tcg_out_ldst_finalize` loop (tcg.c:770-792). Each one patches the fast-path `jne` (`label_ptr[0..1]`), loads `raddr` via `ldst_ra_gen` → `tcg_out_movi`, calls the helper and `jmp raddr`. | tcg.c:770-792; tcg-target.c.inc:2036-2116 |
| G11 | `tb_ret_addr` is a `static` in tcg-target.c.inc:181, which is included into tcg.c. `tcg_code_gen_epilogue` is global. | tcg-target.c.inc:181, 4740-4744 |
| G12 | `runtime_signature()` is computed in supercache init at accel setup, before the plugin loads (vl.c:3783 vs 3840). It hashes only r-x mappings and file stats, and it allocates (glib). Plugin placement is **not** part of it. | supercache.c:149-180, 586; review A4 |
| G13 | FastDyn's fingerprint hashes the whole TOML content, generated work-dir files (by content), native-file stats, scalar plugin options (including `log_level`) and `FASTDYN_QEMU_ICOUNT`. It does **not** hash the environment otherwise. It deletes a `.sc` whose manifest fingerprint differs or is missing. QEMU inherits `os.environ`. | fastdyn/src/fastdyn/targets/qemu_target.py:133-216, 456-463, 614, 886-943 |
| G14 | `setup_qemu` (qemu_target.py:1000-1012) is separable from `start_execution`. It writes the manifest via `build_qemu_cmd → _configure_supercache` and resets file RAM backends without launching QEMU. `Fastdyn.run` calls `qemu_target.start_execution` through the module attribute (fastdyn.py:66-75). | as cited |
| G15 | Without chaining, the only TB entries are `cpu_tb_exec` from the main loop and `helper_lookup_tb_ptr` hits. `tb_add_jump` is called only at cpu-exec.c:983-986. | cpu-exec.c:379-410, 432-460, 960-990 |
| G16 | goto_tb is used only when dest is on the same target page as `pc_first`. | translator.c:106-120; target/arm/tcg/translate.c:2765-2790 |
| G17 | [B0] reference tallies: 394 pool-indirect helper calls (11 helpers); 3 plugin rel32 calls; 3 external RIP LEAs; 316 jmps to `tb_ret_addr`; 3 jmps to the epilogue; 316 own-struct LEAs (+0:105, +1:50, +3:161); 336 raddr LEAs; 52 goto_ptr; 0 movabs; 0 host-pointer imm32. tc−tb = 192, `tb_offset%64==0`, records ascending, no duplicates, 161/163 with the irq check. | review A1-A3 |
| G18 | The build has `buildtype=debug` but `optimization=2`, `debug=true` (`meson-info/intro-buildoptions.json`), i.e. `-O2 -g`. Review A6's "unoptimized" inference is wrong. The working-tree compat patch is part of the binary. | build introspection |

---

## 1. Invariants

1. **One QEMU binary and one plugin for every arm.** Capture and profile code is compiled in and stays inert unless its environment variable is set. That keeps `runtime_signature` (exe stat plus r-x maps) and the FastDyn fingerprint identical across prepare, profile, validate and production.
2. **Capture only observes.** It never emits bytes, never changes a translation decision, and never allocates (no heap, no `tcg_malloc`, no mmap, no stdio). The capture run's `.sc` *is* the production snapshot. This is enforced by construction (2.2) and by tests (T-B1-1).
3. **Identity is the translation key plus guest bytes, never a host PC or a vaddr.** `TBKey = (page_addr0, page_addr1, flags, cs_base, cflags)`; `tb_uid` adds `guest_size`, `icount` and `guest_hash`. The guest vaddr is an *observed attribute* (G2).
4. **Fail closed on references.** Any decoded native reference not explained by capture, and any captured reference not found in the bytes, pins the TB at its original offset. Pinned is a supported state; it is never a silent mis-relocation.
5. **Unknown never collapses to zero.** This holds for counts, edges and reference coverage.
6. **Nothing writes a live cache or a published artifact.** Snapshots and artifacts are immutable and content-addressed. Every QEMU run gets a fresh per-run directory holding a sparse copy, and every run is followed by a proof that the artifact was actually used (5.4).
7. **B2 ships on the unmodified v5 loader.** Whole-TB moves keep the original slot order, and code changes only at declared fixup fields. The loader changes only in B3 (v6).

---

## 2. QEMU preparation capture (`scc`, Tier 0), file `.scm` v1

### 2.1 Enabling it

- **Switch:** `QEMU_SC_CAPTURE=/abs/path/run.scm`. Optional `QEMU_SC_CAPTURE_LEVEL=reloc` (Tier 0, the default) or `ops` (Tier 1; reserved, see 2.8).
- It is read with `getenv()` at hook **H1** in `tcg_supercache_configure` (supercache.c:316-334, after `supercache.flat = flat;`).
- QEMU prints `supercache-capture: level=reloc path=...` on stderr. It exits with an error in three cases:
  - the variable is set but no supercache path is configured;
  - `supercache-flat` is on;
  - `QEMU_SC_PROFILE` is also set (capture and profile are mutually exclusive).
- **Why an environment variable:** it is neither in the FastDyn fingerprint nor in `-accel` (G13), and FastDyn passes the environment through. A TOML key or `-accel` property would change the fingerprint and delete the cache.
- **Opening the file:** `open(path, O_WRONLY|O_CREAT|O_EXCL|O_CLOEXEC, 0644)`, then `fcntl(fd, F_DUPFD_CLOEXEC, 1000)`, then `close(fd)`. That moves the fd to number ≥1000, so the rest of QEMU's fd numbering is unchanged with capture on or off. If `F_DUPFD` fails, QEMU exits with an error. The file header is written immediately.
- **Scrubbing:** `scopt run` removes every `QEMU_SC_*` variable from timing and validation arms. After the run it asserts that no `*.scm` or `*.qprof` file appeared in the run directory.

### 2.2 Construction rules (code-review checklist; any violation blocks merge)

| Rule | Requirement |
|---|---|
| R1 observe-only | Hooks take `const` pointers and scalars captured *after* the emission decision. They never write `s->code_ptr`, `code_buf`, labels, pool or ldst lists, or `carry_live`. They never call `tcg_out_*`, `tcg_gen_*`, `tcg_malloc` or `new_pool_*`, and never change cflags, max_insns, log masks or control flow. |
| R2 no allocation | All state lives in static `.bss` (section 2.6 sizes). Output uses `write(2)` only. Strings come from `.rodata` or `TCGHelperInfo.name`. `/proc/self/maps` is read with `open`/`read` into a static buffer. FNV-1a only; no sha256 in QEMU. |
| R3 same binary | The arrays exist in every run, so `.bss`, brk and the mmap layout are identical with capture on or off. |
| R4 overflow degrades | A full staging array sets `TB.flags.capture_overflow`. The TB is then imported as pinned; code generation is never affected. |
| R5 gate | Every hook is `if (unlikely(scc_in_tb)) scc_xxx(...)` (per-TB hooks) or `if (unlikely(scc_active))` (per-run hooks). `scc_in_tb` is true only between H3 and H15. This excludes the prologue's own movi, branch and pool emission (tcg.c:1898-1910). |
| R6 separate file | The `.sc` file and its header are never touched. The `.scm` takes no flock. |

Capture slows translation. That can change *which* TBs a timing-dependent guest translates, but never the bytes of any TB. T-B1-1 tests this.

### 2.3 Hook table (Tier 0)

New files: `include/tcg/sc-capture.h` and `tcg/sc-capture.c` (≈700 lines), added to `tcg/meson.build` next to `supercache.c`. Two small accessors are also added:
- `const void *tcg_target_tb_ret_addr(void)` in tcg.c, which returns the static `tb_ret_addr` (G11);
- `void *tcg_supercache_buffer(void)` plus `uint64_t tcg_supercache_loaded_max_end(void)` in supercache.c.

| # | File : function (anchor, verified lines) | Hook call | Records |
|---|---|---|---|
| H1 | tcg/supercache.c `tcg_supercache_configure` (316-334), at the end | `scc_configure_from_env()` | Opens the fd and writes the FileHeader. Registers `atexit(scc_finish)`. This happens before supercache init registers `atexit(sync_cache)` (627), so `scc_finish` runs *after* `sync_cache`. |
| H2 | tcg/supercache.c `tcg_supercache_after_prologue` (632-671), at the end | `scc_run_info(s, tcg_target_tb_ret_addr())` | The RUN_INFO chunk (2.5.3). |
| H3 | tcg/tcg.c `tcg_gen_code`, right after `s->data_gen_ptr = NULL;` (6928) | `scc_tb_begin(s, tb, pc_start)` | Resets staging; `attempt++` (it resets to 1 when `tb` differs from the staged one); sets `scc_in_tb`. Reached again on every retry (G5). |
| H4 | tcg/tcg.c `tcg_gen_code` op loop, first statement inside `QTAILQ_FOREACH(op, &s->ops, link)` (≈6943) | `scc_op(s, op->opc, num_insns)` | For `insn_start`: a new insn index (`num_insns+1`), its host start offset (`tcg_current_code_size`) and its 3 words, taken from the op params the same way as tcg.c:6975-6978. For `call` and `qemu_ld*`/`qemu_st*`/`exit_tb`/`goto_tb`/`goto_ptr`: an OpMark `{host_off, kind, insn_idx}`. All other ops are ignored in Tier 0. |
| H5 | tcg/tcg.c `tcg_reg_alloc_call`, immediately before `tcg_out_call(...)` (6185) | `scc_helper(info, tcg_call_func(op))` | Interns the helper (HELPER chunk on first sight: name or NULL, flags, typemask, func). Sets the pending **branch tag** `HELPER_CALL(helper_id)` and stores the helper_id in the last call OpMark. |
| H6 | tcg/i386/tcg-target.c.inc `tcg_out_jxx` (1506-1546), all **four** emit paths (has_value short/long, forward short/long) | `scc_label_jump(s, insn_start, field, width, opc, l)` | Field offset, rel8/rel32, JMP vs JCC, and the `TCGLabel*` (in staging only). Its target is resolved at H15 from `l->u.value_ptr`. This fixes Design 2's gap (G6). |
| H7 | tcg-target.c.inc `tcg_out_branch` (1938-1955), both forms | `scc_branch(s, insn_start, field, call, enc, dest)` | `enc` = REL32 or POOL_INDIRECT; `dest` absolute. Consumes the pending branch tag. With no tag it uses the `LDST_*` bracket (H13); with neither it records `OTHER_BRANCH`. |
| H8 | tcg-target.c.inc `tcg_out_movi_int` (1091-1122), each of the 5 returns | `scc_movi(s, insn_start, field, enc, arg)` | Consumes the pending **movi tag**. A site is recorded when `enc ∈ {RIPREL32, IMM64}`, or a tag is pending, or `arg` lies in `[code_address, code_address+code_size)`. Untagged xor and imm32 constants are not recorded (2.7 step 5 range-checks them anyway). |
| H9 | tcg-target.c.inc `tcg_out_exit_tb` (2629-2638) | `a0==0`: branch tag `EXIT_TB_EPI_JMP`. Otherwise: movi tag `MOVI_EXIT_TB_PTR(idx=a0&3)`, then branch tag `EXIT_TB_RET_JMP(idx)` | Own-struct LEA and the prologue jumps. |
| H10 | tcg-target.c.inc `ldst_ra_gen` (2036-2043), before `tcg_out_movi` | movi tag `MOVI_LDST_RA` | The raddr LEA. |
| H11 | tcg-target.c.inc `tcg_out_goto_tb` (2640-2654), after the `E9` byte | `scc_goto_tb(s, which, pad_start, field)` | Pad start and the rel32 field. Cross-checked against the record's `jmp_insn_offset` / `jmp_reset_offset`. |
| H12 | tcg-target.c.inc `tcg_out_goto_ptr` (2656-2660) | `scc_goto_ptr(s, insn_start, reg)` | Site and register. |
| H13 | tcg/tcg.c `tcg_out_ldst_finalize` loop (770-792): before the `is_ld ? ... : ...` call and after it; then after the loop | `scc_ldst_begin(s, lb->is_ld, lb->oi, lb->label_ptr[0], lb->label_ptr[1], lb->raddr)`; `scc_ldst_end(s)`; `scc_ldst_done(s)` | Slow-path start and end, `oi`, `raddr`, the two jne fields (`LDST_FAST_JCC`), and the ordinal that joins this slow path to the k-th qemu_ld/st OpMark (ldst labels are created in op order). While the bracket is open, a call branch is `LDST_HELPER_CALL` and a jmp is `LDST_RET_JMP` (its target must equal raddr). `scc_ldst_done` records `ldst_end`. The generic location replaces Design 1's two backend hooks. |
| H14 | tcg/tcg.c `tcg_out_pool_finalize` (795-833): after `s->data_gen_ptr = a;` (812); and after each successful `patch_reloc(...)` (827) | `scc_pool_start(s, a)`; `scc_pool_entry(s, p->label, p->rtype, p->addend, p->nlong, p->data, value)` | Code/pool boundary. Per pool user: the field site, the deduplicated slot offset (`value − tc`) and the data. This replaces Design 1's wrong `new_pool_alloc` anchor (G7). Gated by R5, so the prologue's pool is ignored. |
| H15 | tcg/tcg.c `tcg_gen_code`, after `tcg_resolve_relocs` succeeds and before `return` (7046-7057) | `scc_tb_end(s, tb, tcg_current_code_size(s))` | Copies `gen_insn_end_off[0..icount-1]`; `body_end = gen_insn_end_off[icount-1]` (7035); resolves label targets. If a tag is still pending, sets `capture_error`. Re-reads the final bytes of every recorded field from `code_buf` and stores them as `final_field`, so value checks see post-relocation bytes. Clears `scc_in_tb` and marks staging `READY`. |
| H16a | tcg/tcg.c `tcg_func_start` (called at translate-all.c:247 by `setjmp_gen_code` before every translation attempt) | `scc_translate_begin()` | Clears the pre-staging area (static exits). Gate: `scc_active`. |
| H16 | target/arm/tcg/translate.c `gen_goto_tb` (2765-2790), both branches | `scc_static_exit(n, s->pc_curr + diff, direct)` | Static guest destination vaddr per goto_tb slot (`direct=1`). Also covers the off-page `gen_goto_ptr` fallback (`direct=0`, slot 0xFF). Runs during translation, before H3, so it writes to the pre-staging area that H16a cleared. H3 adopts that area into the staged TB. |
| H17 | tcg/supercache.c `tcg_supercache_record` (702-754), after the record `pwrite` succeeds (744-749), before `record_count++` | `scc_tb_commit(supercache.header.record_count, &record, supercache.buffer)` | Requires staging `READY` for this `tb`. Joins it to the file slot, copies the 104 record bytes, computes `code_fnv64` over `buffer[tc_offset, end_offset)` (the bytes just pwritten), copies the guest bytes via `qemu_map_ram_ptr` as `guest_hash` does (supercache.c:683-700), and writes the TB chunk. On the first commit it also writes MAPS(`first_commit`) and RAMBLOCKS. |
| H18 | `atexit(scc_finish)` (registered in H1) | `scc_finish()` | MAPS(`exit`), then END with counters. |

**Tag protocol** (from Design 1):
- There are two single-slot pending registers, `branch_tag` and `movi_tag`. Each is consumed by the next emitter of its kind. Callers set the tag immediately before the emitter call. H5 is placed after all argument setup in `tcg_reg_alloc_call`, so argument moves cannot steal it.
- A tag still pending at H15 sets `capture_error`, and the importer pins that TB.
- An `OTHER_BRANCH` site is never expected; it also pins.

**Abort semantics.**
- Only H17 writes a TB chunk.
- One-shot TBs (G4), `existing_tb` rewinds, `encode_search` overflow retries and longjmps out of translation leave staging non-committed. The next H16a or H3 discards it and increments `discarded` in END.
- No hook is needed in translate-all.c. This fixes the dead discard hook that Design 1 and Design 2 placed at supercache.c:707-710.

### 2.4 Enumerations (stable numbering, shared by C and Python)

```
site kind (u16):
 0x01 LABEL_JMP        0x02 LABEL_JCC        0x03 LDST_FAST_JCC    0x04 LDST_RET_JMP
 0x05 LDST_HELPER_CALL 0x06 HELPER_CALL      0x07 EXIT_TB_RET_JMP  0x08 EXIT_TB_EPI_JMP
 0x09 MOVI_EXIT_TB_PTR 0x0A MOVI_LDST_RA     0x0B MOVI_CONST       0x0C POOL_REF
 0x0D GOTO_TB          0x0E GOTO_PTR         0x0F OTHER_BRANCH (never expected -> pin)
encoding (u8):
 0 NONE 1 REL8 2 REL32 3 RIPREL32 4 IMM32_ZX 5 IMM32_SX 6 IMM64 7 XOR0 8 POOL_INDIRECT 9 GOTO_TB_REL32
opmark kind (u16): 1 CALL 2 QEMU_LD 3 QEMU_ST 4 EXIT_TB 5 GOTO_TB 6 GOTO_PTR
TB flags (u32): bit0 irq_check (insn0_start > 0 and !CF_NOIRQ), bit1 capture_overflow, bit2 capture_error,
                bit3 ops_present (Tier 1), bit4 plugin_cbs_present (Tier 1), bit5 guest_bytes_truncated
```

### 2.5 `.scm` v1 file format (binary, little-endian, chunked)

The format is binary because QEMU writes it without allocating. All derived artifacts are canonical JSON (section 4).

#### 2.5.1 FileHeader (64 bytes)

| Off | Type | Field |
|---|---|---|
| 0 | u8[8] | magic `"QSCMETA1"` |
| 8 | u32 | version = 1 |
| 12 | u32 | header_size = 64 |
| 16 | u32 | capture_level (1 = reloc, 2 = ops) |
| 20 | u32 | pid |
| 24 | u64 | created_realtime_ns |
| 32 | u64[3] | reserved = 0 |
| 56 | u64 | checksum = FNV-1a-64 over bytes [0, 56) |

#### 2.5.2 Chunk framing

| Off | Type | Field |
|---|---|---|
| 0 | u32 | type |
| 4 | u32 | payload_len (excluding padding) |
| 8 | u64 | seq (0, 1, 2, … per file) |
| 16 | u8[payload_len] | payload |
| … | u8[pad] | zero padding to an 8-byte boundary |
| … | u64 | trailer = FNV-1a-64 over [type .. end of padding] |

Chunk types: 1 RUN_INFO, 2 (reserved), 3 HELPER, 4 TB, 5 OPS (Tier 1), 6 PLUGIN_CBS (Tier 1), 7 MAPS, 8 RAMBLOCKS, 10 END.

Reader rules:
- A wrong file header rejects the whole file.
- A bad trailer, or a `seq` that is not one greater than the previous, rejects that chunk and every chunk after it (prefix semantics, like a torn `.sc` tail).
- An unknown type with a valid trailer is skipped.
- A missing END marks the capture `incomplete`. Every complete TB chunk is still usable.

#### 2.5.3 RUN_INFO payload (version 1)

| Off | Type | Field |
|---|---|---|
| 0 | u32 | info_version = 1 |
| 4 | u32 | capture_level |
| 8 | u64 | code_address |
| 16 | u64 | code_size |
| 24 | u64 | prologue_end |
| 32 | u64 | tb_ret_off (`tb_ret_addr − buffer`) |
| 40 | u64 | epilogue_off (`tcg_code_gen_epilogue − buffer`) |
| 48 | u64 | runtime_signature (from the header) |
| 56 | u64 | header_record_count_at_load (= first slot this run writes) |
| 64 | u64 | loaded_max_end |
| 72 | u64 | highwater_off |
| 80 | u32 | icache_linesize |
| 84 | u32 | tb_struct_size |
| 88 | u32 | host_page_size |
| 92 | u32 | target_page_bits |
| 96 | u32 | insn_start_words (= 3) |
| 100 | u32 | host cpuinfo bits (`cpuinfo`) |
| 104 | u64 | exe st_dev |
| 112 | u64 | exe st_ino |
| 120 | u64 | exe st_size |
| 128 | u64 | exe st_mtim (ns) |
| 136 | u32 | flags: bit0 icount_enabled, bit1 flat (always 0) |
| 140 | u32 | prologue_len (= prologue_end) |
| 144 | u8[prologue_len] | prologue bytes `[0, prologue_end)` |

#### 2.5.4 HELPER payload

| Type | Field |
|---|---|
| u32 | helper_id (dense, starting at 0) |
| u32 | tcg_flags (`info->flags`) |
| u32 | typemask |
| u32 | name_len (0 when `info->name == NULL`, e.g. plugin udata callbacks, plugins/core.c:384-405) |
| u64 | func |
| u8[name_len] | name bytes |

#### 2.5.5 TB payload (`tb_chunk_version` 1): fixed header (200 bytes) followed by arrays

| Off | Type | Field |
|---|---|---|
| 0 | u32 | tb_chunk_version = 1 |
| 4 | u32 | slot (= `header.record_count` at commit) |
| 8 | u64 | commit_seq |
| 16 | u8[104] | record (byte-exact, as pwritten) |
| 120 | u64 | vaddr (`pc_start`; observed attribute) |
| 128 | u64 | code_fnv64 over `buffer[tc_offset, end_offset)` |
| 136 | u32 | attempts |
| 140 | u32 | flags (2.4) |
| 144 | u32 | insn0_start (host offset of the first insn_start; `[0, insn0_start)` is the TB-start check) |
| 148 | u32 | body_end |
| 152 | u32 | ldst_end |
| 156 | u32 | pool_start (`data_gen_ptr − code_buf`, or 0xFFFFFFFF) |
| 160 | u32 | code_size (must equal record.tc_size) |
| 164 | u16 | icount |
| 166 | u16 | insn_start_words (3) |
| 168 | u32 | n_sites |
| 172 | u32 | n_pool |
| 176 | u32 | n_ldst |
| 180 | u32 | n_opmarks |
| 184 | u32 | n_static_exits |
| 188 | u32 | n_guest_bytes (= record.guest_size) |
| 192 | u64 | reserved = 0 |

The arrays follow in this order. Each is 8-aligned.

| Array | Element (bytes) | Layout |
|---|---|---|
| `Insn[icount]` | 32 | `u64 word[3]; u32 host_start; u32 host_end` |
| `Site[n_sites]` | 32 | `u32 field_off; u32 insn_off; u8 width; u8 enc; u16 kind; u32 aux; u64 value; u32 insn_idx (0xFFFFFFFF = TB-start/none); u32 final_field (raw field bytes, little-endian, low 4 bytes)` |
| `Pool[n_pool]` | 24 | `u32 user_field_off; u32 slot_off; u16 rtype; u16 nlong; i32 addend; u64 data0` (x86-64 uses only nlong=1, map-native-refs c14; nlong>1 sets capture_error) |
| `Ldst[n_ldst]` | 32 | `u32 jcc_field[2] (0xFFFFFFFF unused); u32 slow_start; u32 slow_end; u32 raddr_off; u32 oi; u32 op_ordinal; u8 is_ld; u8 pad[3]` |
| `OpMark[n_opmarks]` | 16 | `u32 host_off; u16 kind; u16 insn_idx; u32 helper_id (0xFFFFFFFF); u32 reserved` |
| `StaticExit[n]` | 16 | `u8 slot (0, 1, 0xFF = off-page goto_ptr); u8 direct; u16 insn_idx; u32 reserved; u64 dest_vaddr` |
| `guest_bytes[n_guest_bytes]` | padded to 8 | FNV-1a-64 must equal record.guest_hash |

`aux` holds the helper_id (HELPER_CALL), the idx (EXIT_TB_*, MOVI_EXIT_TB_PTR), the goto_tb slot (GOTO_TB), the register (GOTO_PTR), the pool index (POOL_REF) or the ldst index (LDST_*).

`value` holds the absolute destination (branches), the movi argument, the label target as an absolute address (LABEL_*, resolved at H15), or the pool `data0` (POOL_REF).

#### 2.5.6 MAPS, RAMBLOCKS, END

- **MAPS:** `u32 tag (1 = first_commit, 2 = exit); u32 truncated; u32 len; u32 pad;` followed by the raw `/proc/self/maps` (static 256 KiB buffer).
- **RAMBLOCKS:** `u32 n; u32 pad;` followed by rows `{char idstr[64]; u64 ram_offset; u64 used_length; u64 max_length; u64 host}`, filled through `qemu_ram_foreach_block` and the `qemu_ram_get_*` accessors. This chunk is for diagnostics and the address witness only. It is **never** part of a bundle identity.
- **END:** `u64 committed, discarded, overflow, capture_error, write_errors, helpers;` plus `u32 clean (1);`.

#### 2.5.7 Static staging sizes (`.bss`, ≈2.2 MiB total)

| Area | Capacity |
|---|---|
| sites | 16384 × 32 B |
| insns | 512 (TCG_MAX_INSNS) |
| pool | 4096 |
| ldst | 4096 |
| opmarks | 8192 |
| static exits | 4 |
| guest bytes | 4 KiB (Thumb TB ≤ 512×4 B) |
| chunk output buffer | 1 MiB |
| maps buffer | 256 KiB |
| helper intern table | 2048 entries |

#### 2.5.8 Binding to the `.sc` and unioning several captures

- A TB chunk binds to slot `i` only if all of these hold:
  - `chunk.slot == i`;
  - `chunk.record == sc_record_bytes[i]`, all 104 bytes;
  - `chunk.code_fnv64 == FNV64(sc_bytes[tc,end))`;
  - RUN_INFO `code_address`, `code_size`, `prologue_end` and `runtime_signature` equal the `.sc` header, and `tb_struct_size == header.tb_size`.
- A leftover `.scm` from a deleted cache therefore cannot bind.
- Several prep runs may each add records to one `.sc`, each with its own `.scm`. The importer takes the union by slot. Overlapping slots must be byte-equal chunks. A slot with no binding chunk is `no_meta` and is pinned.

### 2.6 Byte-identity evidence

- **T-B1-1a (determinism control):** three capture-off cold runs in the **same** run directory, recreated each time, with RAM files reset.
- **T-B1-1b:** two capture-on cold runs in the same directory.
- **Gate:**
  - If 1a gives one sha256 for the whole sparse `.sc`, the 1b files must have that sha256.
  - Otherwise (guest timing nondeterminism, documented), every TBKey+guest_hash common to an on run and an off run must have an identical `[tc,end)` byte range and identical records modulo offsets after normalizing **only** self-relative fields (MOVI_EXIT_TB_PTR, MOVI_LDST_RA, prologue rel32). Every absolute host value (external rel32 target, RIP-LEA target, pool data) must be equal. The weaker mode is reported as such.
- RUN_INFO `code_address` is equal in all runs. The MAPS(`first_commit`) diff on and off is empty apart from the `.scm` itself, which is not mapped.

### 2.7 Importer cross-check (per valid slot; implemented in `scopt.importer.crosscheck`)

Record validity mirrors `load_records` (supercache.c:378-423): `n = min(record_count, (filesize − record_offset(0)) / 104)`; committed; checksum; bounds. Then:

1. **Bind** (2.5.8). If nothing binds, the TB is `no_meta` (pinned).
   - `FNV64(guest_bytes) == guest_hash`.
   - `Insn[0].word[0] == vaddr & (2^target_page_bits − 1)` under PCREL.
2. **Layout.**
   - `tc_offset == tb_offset + ROUND_UP(tb_size, icache_linesize)`.
   - `insn0_start ≤ body_end ≤ ldst_end ≤ pool_start ≤ code_size`.
   - `[ldst_end, pool_start)` is all `0x90`.
   - Pool slots tile `[pool_start, code_size)`, 8-aligned in absolute address, with bytes equal to the captured data.
3. **Search data.**
   - Decode `icount` rows of 3+1 sleb128 deltas at `tc+tc_size`, seeded `{0,0,0}` under PCREL (translate-all.c:101-135). They must equal the `Insn` words and `host_end`.
   - The last `host_end == body_end`.
   - `tc+tc_size+search_len ≤ end_offset < tc+tc_size+search_len+16`.
4. **Linear sweep** of `[0, pool_start)` with capstone 5.0.3 (CS_ARCH_X86, CS_MODE_64, detail on) at address `code_address + tc_offset`.
   - It must tile exactly, with no `(bad)`.
   - Every captured `insn_off` must be an instruction boundary, and `field_off` must lie inside that instruction.
   - Failure: pin, reason `decode_failed@off`.
5. **Two-sided reference matching.** Extract every reference-bearing operand:
   - rel8 and rel32 branches and calls;
   - every `[rip+disp32]` operand (memory or LEA);
   - every movabs imm64;
   - every immediate or displacement (imm32 zero- or sign-extended) whose value lies in the arena or in any MAPS mapping.

   Each extracted reference must match exactly one captured site: same field offset, an encoding consistent with the site's kind, and a value or target equal to `value`. Conversely, every captured site with `width>0` must be found.
   - Unmatched decode: reason `unexplained_reference@off`.
   - Unmatched capture: reason `missing_reference@off` (a contradiction; with `--strict`, the whole import fails).
   - Either way the TB is pinned.
   - There is **no** structural exception for label-less intra branches (Design 3's INTRA_STRUCTURAL is dropped): H6 captures every label branch.
6. **Target classification** (exhaustive; an unknown class pins the TB):

| Class | Rule | B2 fixup under a whole-TB move by `d` (d % 64 == 0) |
|---|---|---|
| `intra` | target in `[tc, tc+code_size)`, on an instruction boundary or a pool slot | none |
| `own_struct+idx` | MOVI_EXIT_TB_PTR target `== code_address + tb_offset + idx`, idx ∈ {0, 1, 3}, encoding RIPREL32 | none (the struct moves with the code) |
| `prologue.tb_ret` / `prologue.epilogue` | EXIT_TB_RET_JMP / EXIT_TB_EPI_JMP target equals `code_address + tb_ret_off` / `+ epilogue_off` | rel32 −= d |
| `ext.helper` / `ext.plugin` / `ext.lib` | target outside the arena, equal to the captured func, symbolized via MAPS → ELF symtab (pyelftools) | rel32 −= d, exact int32 check |
| `ext.ptr_riprel` | MOVI_CONST RIPREL32 whose target is outside the arena (the 3 plugin LEAs [B0]) | disp32 −= d, exact int32 check |
| `pool` | POOL_REF; the value (absolute) is classified as `ext.*` or `abs.*` for bindings only | none |
| `arena.abs` | absolute imm32/imm64/pool value inside the arena | only MOVI_EXIT_TB_PTR or MOVI_LDST_RA with IMM forms: `value += d`. Since `code_address ≥ 4 GiB` is required (section 9), this class is "pin" in practice |
| `arena.other_tb` | rel or riprel into another TB | pin |
| `goto_tb` | GOTO_TB at `jmp_insn_offset[n]`, field 4-aligned, `final_field == 0` | none (reset on load, supercache.c:809-814) |
| `goto_ptr` | `FF /4 reg` matching GOTO_PTR | none |
| `abs.heap` / `abs.guest_ram` / `abs.plugin_data` / `abs.qemu_data` / `abs.other` | absolute host values (movabs, RIP-LEA targets, pool data) classified with MAPS and RAMBLOCKS | none; recorded under `bindings.unchecked_host_pointers` |

7. **Recovery points** (from Design 2; this is the B3 recovery oracle). The issuing insn comes from emission-time OpMarks, independently of the search data:
   - For each `Ldst` with ordinal k: the k-th QEMU_LD/ST OpMark's `insn_idx` must equal `unwind(raddr_off)`.
   - For each HELPER_CALL / LDST_HELPER_CALL site: the return offset `insn_off + 5` (rel32) or `+6` (pool) must satisfy `unwind(ret_off) == insn_idx` of its OpMark. For LDST_HELPER_CALL, the helper receives `ra = raddr`, so the raddr check is what applies.
   - `unwind(h)` reimplements `cpu_unwind_data_from_tb` (translate-all.c:137-169): `h − GETPC_ADJ(2)`, then the first row whose cumulative `host_end > h`.
   - A mismatch pins the TB, reason `recovery_mismatch`.
8. **Exits.**
   - The number of valid `jmp_insn_offset` entries equals the number of GOTO_TB sites, at matching offsets.
   - Each EXIT_TB_RET_JMP(idx) is preceded by its MOVI_EXIT_TB_PTR(idx).
   - `flags.irq_check` holds iff `!(cflags & CF_NOIRQ)`. [B0]: 161/163.
9. **Guest.**
   - `vaddr` maps through the firmware ELF PT_LOADs. If it is file-backed, the guest bytes must equal the ELF bytes (origin `elf_verified`); otherwise the origin is `ram_resident`.
   - A capstone Thumb decode (CS_ARCH_ARM, CS_MODE_THUMB|CS_MODE_MCLASS) must give instruction lengths equal to consecutive `Insn` vaddr deltas, where `vaddr_i = page_base(vaddr) | word0_i`.
10. **Golden totals on the re-captured minimal snapshot** (T-B1-3): exactly the G17 tallies, with ±10% tolerance only if T-B1-1a shows the TB set drifts.

Result per TB: `RelocInfo{relocatable: bool, reasons: [str], fixups: [Fixup], env_deps: [...]}`. Status is one of `ok_relocatable`, `pinned(reasons)` or `no_meta`.

### 2.8 Tier 1 extension points (semantic route; same file; off by default; not in B1/B2)

- **OPS (chunk 5, level `ops`).** At tcg.c after the `CPU_LOG_TB_OP_OPT` block (6902-6911), stage op indices; H4 records the host offset of every op; serialization happens at H15.
  - Opcode **names** (`tcg_op_defs[opc].name`), TCGOP_TYPE, TCGOP_FLAGS, `op->life`.
  - Args: temps renamed per TB (`t0..` for TB/EBB temps, `L0..` for labels); globals and fixed temps by name plus env offset; constants by type and value.
  - Calls as helper_id; `insn_start` words; qemu_ld/st with MemOpIdx.
  - Plain ld/st classified by base as `env` or `host_abs`. `host_abs` covers FastDyn UPDATE_MEM via `gpa2hva`, plugin-gen.c:325-341: a guest-RAM write that bypasses the softmmu, so it is a guest effect.
  - No raw pointers and no global temp ids.
- **PLUGIN_CBS (chunk 6).** In `accel/tcg/plugin-gen.c:inject_cb` (353-387): insn index, cb type including the FastDyn inline kinds, RW flags including `RW_CFI`, func, userp, and inline entry data.
- **Env layout** is built offline: `gdb -nx -batch -ex 'ptype/o CPUARMState' build/qemu-system-arm`, run **outside** `src/qemu` because of its `.gdbinit`. Review A3 offsets are the golden values.

---

## 3. Profile v1: exact, software-only, on an unchained warm copy

There is no perf (`perf_event_paranoid=4`). `-d exec,nochain` changes cflags (CF_NO_GOTO_TB) and the fingerprint. Design 3's in-code entry helper changes the generated code, so profiled TBs differ from snapshot TBs and need a heuristic join. Rejected; see section 11.

### 3.1 Mechanism

- Profile mode (`accel/tcg/sc-profile.{c,h}`, ≈450 lines) runs on a **sparse warm copy** of the snapshot (or artifact) in a fresh run directory.
- It **disables chaining at run time only**: `tb_add_jump` is skipped. Code bytes and cflags are unchanged, and the persisted goto_tb fields are in reset state anyway (supercache.c:809-814).
- With chaining off, every TB entry is observed (G15).
- Snapshot TBs live at `buffer + tb_offset` (G3), so the TB pointer gives the slot directly. TBs translated during the run are appended to the copy only.

### 3.2 Enabling it

- `QEMU_SC_PROFILE=/abs/run.qprof`
- `QEMU_SC_PROFILE_PHASES=name@0xVADDR[,name@0xVADDR...]` (in order; phase 0 is always `boot`)
- Both are read in H1's configure, open the fd the same way as 2.1, and are mutually exclusive with capture.
- Tables are static `.bss` open-addressing tables: 65536 TB rows and 262144 edge rows. Overflow sets a flag and never allocates.

### 3.3 Hooks

| # | Site (verified) | Action |
|---|---|---|
| P1 | cpu-exec.c `cpu_exec_loop`, 983-986 | `if (last_tb && likely(!scp_active)) tb_add_jump(last_tb, tb_exit, tb);`, then `if (unlikely(scp_active)) scp_dispatch(cpu, tb, s.pc);` before `cpu_loop_exec_tb`. `scp_dispatch`: `entries[tb][phase]++`; edge `(cur → tb, pending_kind, pending_flags, phase)`; `cur = tb`; checks the phase marker. |
| P2 | cpu-exec.c `cpu_tb_exec`, after `*tb_exit = ret & TB_EXIT_MASK;` (455-456) | `scp_return(last_tb, *tb_exit, ret)`. idx 0/1 → pending `CHAIN0/CHAIN1` (the edges production would chain). idx 3 → `aborted[last_tb]++`, pending `EXIT_REQUESTED`. `ret==0` → pending `EXIT_NULL`, or `LOOKUP_MISS` if P3 flagged a miss. |
| P3 | cpu-exec.c `helper_lookup_tb_ptr` (379-410). Hit: before `return tb->tc.ptr;`. Miss: before `return tcg_code_gen_epilogue;` | Hit: `entries[tb]++`, edge `(cur → tb, LOOKUP_HIT)`, `cur = tb`, phase check. The transfer stays direct, as in production. Miss: set `miss_flag`. A debug build asserts `tcg_tb_lookup(GETPC()) == cur`. |
| P4 | cpu-exec.c `cpu_handle_interrupt` (768-871), inside the `cpu_exec_interrupt()` success branch (≈827) | pending flag `IRQ` |
| P5 | cpu-exec.c `cpu_handle_exception` (692-755), at the `do_interrupt` call (733) | pending flag `EXCEPTION` |
| P6 | cpu-exec.c `cpu_exec_longjmp_cleanup` (517-) | `longjmp_exits[cur][phase]++`, pending flag `LONGJMP` |
| P7 | atexit (registered in configure) | Writes `.qprof`: header, phase table, TB rows (`tb_offset = tb − tcg_supercache_buffer()`, key from the TB struct, first vaddr, per-phase counters), edge rows. |

### 3.4 Counting semantics

- `executions = entries − aborted`. `longjmp_exits` counts entries left mid-TB by a fault, `cpu_io_recompile` or `cpu_loop_exit`.
- Every entry produces exactly one in-edge (the first has src NONE, kind FIRST). The **corrected** flow-conservation oracle (Design 3's version was wrong) is:
  - `Σ in-edges(t) == entries(t)` for every t and phase;
  - `Σ out-edges(t) == entries(t) − [t is the last TB executed]`.
- **Phases** (fixed per Design 2 and the reviews):
  - At each dispatch (P1, P3) the next pending marker `m` is tested with `s.pc ≤ m < s.pc + tb->size`.
  - On a hit the phase advances, and the dispatching execution counts in the new phase. Granularity is the TB, and that is documented.
  - Each phase records `reached`, `first_dispatch_index` and `match = exact_start | contained`.
  - Markers should be function entries. A BL ends a TB, so entries are TB starts.

### 3.5 `.qprof` raw format v1 (little-endian)

**Header (128 bytes):**

| Off | Type | Field |
|---|---|---|
| 0 | u8[8] | magic `"QSCPROF1"` |
| 8 | u32 | version = 1 |
| 12 | u32 | header_size = 128 |
| 16 | u64 | code_address |
| 24 | u64 | runtime_signature |
| 32 | u64 | loaded_record_count |
| 40 | u64 | loaded_max_end |
| 48 | u64 | loaded_header_checksum |
| 56 | u32 | n_phases |
| 60 | u32 | flags: bit0 clean_exit, bit1 tb_overflow, bit2 edge_overflow |
| 64 | u64 | n_tb_rows |
| 72 | u64 | n_edge_rows |
| 80 | u64 | total_dispatches |
| 88 | u64 | created_realtime_ns |
| 96 | u64[3] | reserved |
| 120 | u64 | checksum = FNV-1a-64 over [0, 120) |

**Phase rows (40 bytes):** `u32 id; u32 reached; u64 marker_vaddr; u64 first_dispatch_index; char name[16]` (marker 0 for `boot`).

**TB rows:** a 72-byte fixed part plus `n_phases × 32` bytes.
- Fixed part: `u64 tb_offset; u64 first_vaddr; u64 page_addr0; u64 page_addr1; u64 cs_base; u32 flags; u32 cflags; u16 size; u16 icount; u32 pad; u64 reserved`.
- Per phase: `u64 entries; u64 aborted; u64 longjmp; u64 lookup_hits`.

**Edge rows (32 bytes):** `u64 src_tb_offset (UINT64_MAX = NONE); u64 dst_tb_offset; u16 kind; u16 flags; u32 phase; u64 count`.
- kind: 1 CHAIN0, 2 CHAIN1, 3 LOOKUP_HIT, 4 LOOKUP_MISS, 5 EXIT_NULL, 6 EXIT_REQUESTED, 7 FIRST
- flags: bit0 IRQ, bit1 EXCEPTION, bit2 LONGJMP

The trailer is a u64 FNV-1a over all bytes after the header.

### 3.6 Ingest and identity join (`scopt profile ingest`)

- The ingest reads the **staged copy's** `.sc` after the run.
- A row with `tb_offset` equal to a loaded record (slot `< loaded_record_count`) whose key, size and icount match is origin `SNAPSHOT`. It maps to that slot's `UnitId` (`tb_uid#k`).
- A row whose `tb_offset ≥ loaded_max_end` and which matches an appended record is `PROFILE_ONLY`. It is kept by key and guest_hash and never merged into snapshot identities.
- A row with `page_addr0 == −1` is `TRANSIENT` (one-shot MMIO TB). It is counted separately and **does not** affect completeness. Design 2 marked the whole profile incomplete in this case, which review 1 called over-conservative.
- Host PCs are never identities.

### 3.7 Status (unknown is not zero)

Each count is `{value, status, reason?}` with status ∈ `exact | lower_bound | unknown`. `sampled` is reserved and has no producer in v1.

- **exact** requires all of:
  - clean exit with a valid trailer;
  - no overflow;
  - method `exact_dispatch_unchained_v1`;
  - phase `reached`;
  - the unit is in the profile domain (snapshot units of the profiled snapshot, or PROFILE_ONLY rows).

  An exact 0 is a real zero. The extra page-coverage condition from Design 3 is dropped.
- **lower_bound:** crash or overflow. Observed values are kept.
- **unknown:**
  - `unknown/not_in_domain` (the unit is absent from the profiled snapshot);
  - `unknown/phase_not_reached`;
  - `unknown/not_collected` (no profile attached).
- **Edges:** an absent edge is `exact` 0 only between two exact units. Indirect sites (GOTO_PTR, EXIT_NULL) carry `dest_set_complete=false`: an unobserved destination means "not observed", never "unreachable".

### 3.8 Profile v1 file (`profiles/<profile_id>/profile.json`, canonical JSON, gzip optional)

```json
{"schema":"fastdyn.profile","version":1,"profile_id":"sha256:…",
 "snapshot_id":"sha256:…","bundle_id":"sha256:…","contract_id":"sha256:…",
 "runtime_identity_id":"sha256:…",
 "collection":{"method":"exact_dispatch_unchained_v1","complete":true,"overflow":false,
   "chain_disabled":true,"time_dilation":7.9,"repetitions":5,
   "inputs":[{"name":"train-000","sha256":"…","split":"train"}],
   "runs":[{"run_id":"…","clean_exit":true,"total_dispatches":20148,"transient_entries":0}]},
 "phases":[{"id":0,"name":"boot","marker":null,"reached":true},
           {"id":1,"name":"steady","marker":"0x08003972","match":"contained","reached":true}],
 "domain":{"kind":"snapshot_units","count":163},
 "units":[{"unit":"tb:5f…#0","vaddrs":["0x08018766"],"status":"exact",
           "phase":{"0":{"entries":19394,"aborted":0,"longjmp":0},"1":{"entries":0,"aborted":0,"longjmp":0}}}],
 "edges":[{"src":"tb:…#0","dst":"tb:…#0","kind":"chain1","flags":[],"phase":0,"count":750,"status":"exact"}],
 "profile_only":[{"key":{…},"guest_hash":"0x…","phase":{…}}],
 "transient":{"entries":0},
 "static_cost":{"note":"model, not measured","per_unit":"code bytes, host insns (capstone)"}}
```

`profile_id` is the sha256 of canonical JSON with `profile_id` removed.

**Compatibility** (`ProfileView.open(profile, snapshot)`):
- **Hard reject:**
  - different `bundle_id` or `contract_id`;
  - different `runtime_identity` fields `qemu_sha256`, `plugin_sha256`, `runtime_signature`, `prologue_sha256` or `cpuinfo`.
- **Same snapshot_id:** the profile applies directly.
- **Different snapshot or an artifact:** join by `UnitId`. An artifact's provenance lists its input snapshot_id, and B2 preserves UnitIds. Unmatched units become `unknown/not_in_domain`.
- **Coverage threshold:** reject (`profile_incompatible:coverage`) when matched steady-phase entries fall below the pass's `min_profile_match` (default 0.95).
- **Merging** requires an identical method, phase list, snapshot_id and bundle_id. Counts add; status is the minimum; inputs are concatenated.

**Known bias (always reported):** unchained execution is slower (`time_dilation`). Without icount, host-timer interrupts arrive at different guest points. Profiles set priority and placement only; they never authorize semantic change (spec §5).

**Isolation:**
- The profile run uses its own per-run directory and TOML, a deployed copy (5.3) and reset RAM files.
- Before and after the run, `scopt` compares the sha256 of the source snapshot and of every published artifact; any change is a hard failure.

---

## 4. Identities and artifact formats

### 4.1 Identities (lowercase hex sha256, prefixed)

| ID | Definition |
|---|---|
| `TBKey` | `(page_addr0, page_addr1, flags, cs_base, cflags)`. The pc is implicitly 0 under CF_PCREL; non-PCREL guests are unsupported. |
| `tb_uid` | `"tb:" + hex(sha256(b"tb1" ‖ le64 page_addr0 ‖ le64 page_addr1 ‖ le32 flags ‖ le64 cs_base ‖ le32 cflags ‖ le16 guest_size ‖ le16 icount ‖ le64 guest_hash)[:16])` |
| `UnitId` | `tb_uid + "#" + k`, where k is the occurrence index among equal `tb_uid`s in slot order. That preserves LIFO "newest wins" (supercache.c:271-287). |
| `vaddrs` | observed attribute set (capture `vaddr`, profile `first_vaddr`); never part of an identity |
| `SnapshotId` | `"sha256:" + sha256(header[0:72] ‖ for each valid slot in slot order: record[0:92] ‖ sha256(bytes[tc_offset:end_offset]))` |
| `ArtifactId` | sha256 of the artifact's `MANIFEST.sha256` file bytes |
| `BundleId`, `ProfileId`, `RegionId`, `RuntimeIdentityId` | sha256 of canonical JSON (sorted keys, no whitespace, ints as JSON numbers, addresses as `"0x…"` strings) with the id field (and for regions `provenance`) removed |

### 4.2 ExecutionBundle v1 reader and identity bundle (`scopt.formats.bundle_v1`)

- **Reader:** validates the spec §7 field set against `schemas/execution_bundle.v1.json`, recomputes `bundle_id`, and rejects an unknown `schema_version`. Errors: `BundleSchemaError`, `BundleIdMismatch`, `BundleStale`.
- **Identity bundle:** `scopt bundle identity -c BASE.toml --manifest <cache>.fastdyn.json`. Path-valued keys (`supercache`, `memory_file`, `qmp_socket`, `log_file`) are normalized away, so every per-run TOML of one configuration shares one `bundle_id`.

| Field | Construction |
|---|---|
| `original_image_hash`, `image_hash` | sha256 of the firmware ELF bytes (`native_files.firmware.path`). Size and mtime must equal the manifest. |
| `patch_plan_hash` | sha256(canonical `{virtuals, modifiers, finline, fuzzing_schema, preprocessing_artifact_*}` content sha256s from manifest `inputs.content_files`) |
| `patch_bindings_hash` | sha256(`{"kind":"identity","plan":patch_plan_hash}`) |
| `origin_map_hash` | sha256(`"identity-origin/1:" + image_hash`) |
| `target` | `{isa:"armv7-m", mode:"thumb", endianness:"little", cpu:settings.cpus[0].cpu, features:[from the QEMU cpu model]}` |
| `entry_points` | ELF `e_entry`; reset vector and initial SP from the vector table at `init_nsvtor` |
| `memory_map_hash` | sha256(canonical `{platform, machine, banks:[(id, base, size, type, backend, share)], dev_config_sha256}`). **No host pointers, no paths** (fixes review 1's Design 1 objection). |
| `contract_id` | sha256(canonical `{scalar plugin_options incl. log_level, log_options, icount, coverage, edge_coverage, fuzzing, twintrace, supercache_flat:false}`) |
| `instrumentation_hash` | sha256 over the plugin `.so` **content**, the virtual libraries' content, and the modifiers/virtuals/finline content |
| `device_model_hash` | sha256 over `dev_config.json` content, platform, machine, cpu, fmu |

**RuntimeIdentity** is kept outside the bundle, in `runtime_identity.json`:
- `qemu_sha256`, `qemu_exe_stat`, `plugin_sha256`;
- `build_options` (`buildtype`, `optimization`, `debug` from meson introspection);
- `runtime_signature`, `code_address`, `code_size`, `prologue_end`, `prologue_sha256`, `tb_size`, `host_page_size`, `target_page_bits`, `icache_linesize`, `cpuinfo`, `base_cflags`;
- `supercache_flat:false`, `fastdyn_commit`, `capture_format:1`;
- `normalized_config_sha256`: canonical TOML with the path keys removed.

A snapshot or artifact is compatible with a run only if both the bundle_id **and** the RuntimeIdentity match.

### 4.3 CacheSnapshot import (`scopt import`)

1. Take `flock(LOCK_SH|LOCK_NB)` on the `.sc`; fail if QEMU holds it (supercache.c:581).
   - Copy the `.sc` sparsely to `store/snapshots/<snapshot_id>/cache.sc` with `SEEK_DATA`/`SEEK_HOLE`, then mark it read-only.
   - Copy the `.scm` files and the manifest alongside.
2. Parse the header: magic, version 5, header_size 80, checksum, `code_size` equal to the configured tb-size. Parse records as in 2.7.
   - Reject the snapshot if RUN_INFO `runtime_signature`, `code_address` or `prologue_end` differ from the header, or if the bundle mismatches.
3. Run the 2.7 cross-check. Build the immutable model.
4. Write `snapshot.json` (ids, header, bundle_id, runtime_identity_id, status histogram, capture completeness).
   - Write `units.jsonl`: one CacheRegion v1 (`kind:"tb_singleton"`) per valid slot, in slot order.
   - Write `code/<sha256>.bin` and `evidence.jsonl` (one row per check per unit).
   - `analyses.sqlite` is a cache only, never a source of truth.
   - Re-import of the same inputs is byte-deterministic.

### 4.4 Graph export (`graph.json`, B1 gate C2)

- **Nodes:** UnitIds with `vaddrs`.
- **Edges:** `{src, exit:{kind, slot|site_off}, dest_vaddr?, dest_ram?, candidates:[UnitId], observed:[{dst, count, status, profile_id}], resolution}`.
- **Static goto_tb edges:**
  - `dest_ram = (page_addr0 & ~(2^target_page_bits − 1)) | (dest_vaddr & (2^target_page_bits − 1))`. This is valid because goto_tb is same-page only (G16).
  - `candidates` = snapshot units with `page_addr0 == dest_ram` and the same cflags.
  - `resolution ∈ {candidates_unique, candidates_multiple, not_in_snapshot}`.
  - A static edge **never** asserts `dst`, because flags and cs_base (condexec) at the destination are runtime state.
- **goto_ptr, EXIT_NULL, exit_requested:** `resolution: dynamic`.
- **Profile-observed edges** carry exact `dst` UnitIds with evidence `profile:<id>`.
- **No edge without a captured exit site or a profile observation.**
- **Round trip:** canonical JSON. `export → import → export` must be byte-identical.

### 4.5 CacheRegion v1 (`schemas/cache_region.v1.json`, `scopt.formats.region_v1`)

```json
{"schema":"fastdyn.cache_region","version":1,"region_id":"sha256:…","kind":"tb_singleton|multi_tb",
 "bundle_id":"…","contract_id":"…","runtime_identity_id":"…",
 "members":[{"unit":"tb:…#0","key":{"page_addr0":"0x28098766","page_addr1":null,"flags":"0x110",
             "cs_base":"0x800400","cflags":"0xff020000"},
             "guest":{"size":12,"icount":4,"hash":"0x…","origin":"elf_verified","vaddrs":["0x08018766"]},
             "origin":{"snapshot_id":"…","slot":57,"tb_offset":"0x…"}}],
 "entries":[{"id":"e0","kind":"general","member":0,"host_offset":0,
             "precondition":{"full_key":true,"extra_guards":[]}}],
 "exits":[{"id":"x0","member":0,"kind":"goto_tb|goto_ptr|exit_null|exit_requested",
           "slot":1,"insn":3,"static_dest_vaddr":"0x…","patchable":{"insn_offset":212,"reset_offset":216}}],
 "native":{"code_sha256":"…","code_len":340,"insn0_start":12,"body_end":260,"ldst_end":300,
           "pool_start":312,"search_len":21,"end_offset_rel":384,
           "struct":{"offset_from_code":-192,"size":152},"alignment":64},
 "relocations":[{"member":0,"field_offset":215,"width":4,"encoding":"rel32","site_kind":"EXIT_TB_RET_JMP",
                 "target":{"class":"prologue.tb_ret"},"rule":"rel_minus_delta"},
                {"member":0,"field_offset":64,"width":4,"encoding":"rel32","site_kind":"HELPER_CALL",
                 "target":{"class":"ext.plugin","module":"libfastdyn.so","symbol":"printreg","addr":"0x7fff1f0257…"},
                 "rule":"rel_minus_delta"}],
 "maps":{"insn":[{"member":0,"index":0,"words":["0x366","0x0","0x0"],"host_start":12,"host_end":40}],
         "events":[{"host_offset":55,"kind":"guest_ld|guest_st|helper_call|plugin_cb","insn":1,
                    "faultable":true,"recovery":"r0"}]},
 "recovery":[{"id":"r0","host_return_offset":120,"member":0,"insn":1,
              "restore":{"pc_mode":"pcrel_page_offset","words":["0x36a","0x0","0x0"]},"state_fixups":[]}],
 "bindings":{"helpers":[{"id":3,"name":"lookup_tb_ptr","module":"qemu-system-arm","symbol":"helper_lookup_tb_ptr","flags":"0x…"}],
             "unchecked_host_pointers":[{"class":"abs.plugin_data","anchor":"libfastdyn.so+0x3a120","site":7}]},
 "dependencies":{"guest_pages":[{"ram_addr":"0x28098400","len":1024}],"units":["tb:…#0"],"facts":[],
                 "patch_plan_hash":"…","instrumentation_hash":"…"},
 "status":"ok_relocatable","pin_reasons":[],
 "unknowns":["goto_ptr dest","exit_null dest"],
 "provenance":{"passes":[{"name":"identity","version":1}],"source_units":["tb:…#0"]}}
```

Rules:
- B1 and B2 produce only `tb_singleton` regions with general entries. `specialized` entries, non-empty `state_fixups`/`facts`, and `multi_tb` are defined now and **rejected** by the B2 packager (`capability_not_enabled`).
- Process pointers appear only as **anchored** bindings (module+offset, `heap`, `ramblock:<id>+off`), never as identities.
- Recovery rows: one per faultable return offset (each raddr, each helper-call return).

### 4.6 Output artifact (published atomically)

```
store/artifacts/<artifact_id>/
  cache.sc                 # v5 (identity, B2); v6 from B3 on; sparse
  cache.sc.scopt.json      # provenance sidecar (NOT named *.fastdyn.json; ignored by QEMU and FastDyn)
  cache.scm                # DERIVED capture describing the new layout (same binding rules) -> re-import closure
  regions.jsonl            # CacheRegion v1 per unit, final offsets, slot order
  bundle.json  runtime_identity.json  profiles.json (ids + verdicts)  validation.json  ledger.jsonl
  MANIFEST.sha256          # "sha256  relative/path" per file above, sorted
```

- **`cache.sc.scopt.json`** holds:
  - `input_snapshot_id`, `output_snapshot_id`;
  - `bundle_id`, `runtime_identity_id`, `profile_ids` and their verdicts;
  - the pipeline `[{pass, version, params_sha256, outcome, reason, affected, edit_digest, duration_s}]`;
  - the move map `{UnitId: {slot, old_tb_offset, new_tb_offset, delta}}`;
  - the pinned units with reasons;
  - the sha256 of `validation.json`;
  - tool versions (scopt, capstone 5.0.3, pyelftools);
  - `loader_requirement: "qemu-sc-v5-unmodified"` or `"qemu-sc-v6"`.
- **Derived `cache.scm`:** Python writes the same v1 format, with RUN_INFO copied and per-TB records and sites rewritten for the new offsets (`final_field` and `value` updated, `code_fnv64` recomputed). `scopt import art/cache.sc` then reproduces the same units, and `output_snapshot_id` equals the re-import's SnapshotId.
- **Publishing:** build in `store/artifacts/.tmp-<pid>-<rand>/`, write the `.sc` sparsely (`truncate` to `0x10000 + code_size + n*104`, then `pwrite` the header, each unit's `[tc,end)`, and the record array), `fsync` each file and the directory, `rename()`, then `chmod a-w`. On any validation failure nothing is published; `store/rejections/<candidate_id>.json` is written instead.

### 4.7 Decision: B2 is an ordinary v5 `.sc` for the **unmodified** loader

| Loader requirement | B2 rule |
|---|---|
| Header: magic, v5, size 80, checksum, code_size, runtime_signature, host page, page bits, tb_size (supercache.c:364-376) | Copy every field. Recompute only `checksum`. `record_count` = n (unchanged). `reserved` = 0. `runtime_signature` cannot be computed offline, so it is copied. |
| `prologue_end` equality is fatal (632-645) | Copied. No TB is placed below `ROUND_UP(prologue_end, 64)`. |
| Record validity (378-423) | Offsets rewritten; checksum recomputed (137-142); committed `0x53435231`; pad zero. |
| `max_end < highwater` and a single pread of `[prologue_end, max_end)` (649-666) | The placer keeps `max_end < code_size − host_page − 1024` and compact. Holes and struct slots are zero. |
| Struct rebuilt at `buffer+tb_offset`, code at `tc_offset` (790-807) | Struct and code move by the same `d`, `d % 64 == 0`. That keeps `tc−tb == 192`, own-struct LEAs, 8-aligned pool, 4-aligned goto_tb fields and line alignment. |
| Duplicate keys are LIFO by slot (271-287, 880-897) | **The original slot order is kept exactly** (Design 1). Both reviews preferred this over sorting. |
| Host lookup via region tree in normal mode (819-829) | Non-overlapping `[tb, end)` intervals. Flat mode is unsupported (binary searches at 916-924 and 950-959, plus the stale-QHT hazard). |
| Search data at `tc+tc_size` (translate-all.c:137-169; plugin-gen.c:680-690) | Moves with the code, byte-identical. |
| goto_tb reset on load (809-814) | Field stays 0 and 4-aligned. |

**Fixups per moved unit** (`NativeLinker`, from RelocInfo):
- `prologue.*` rel32: `−= d`.
- `ext.*` rel32 and `ext.ptr_riprel` disp32: `−= d`, with an exact int32 check. Overflow rejects that placement. [B0]: the plugin `.so` is about 1.29 GB below the arena.
- `arena.abs` with MOVI_EXIT_TB_PTR or MOVI_LDST_RA in an IMM form: `+= d`. Unreachable while `code_address ≥ 4 GiB` is enforced.
- Everything else is byte-identical.

A v5 file cannot express intra-TB splitting (u16 offsets, a single tc range), extra entries, cross-TB hard-coded jumps or rewritten code. Those need v6 (4.9).

### 4.8 Deploy, run directories and the "artifact used" proof

**Per-run directory** (always fresh): the maintainer's evaluation data (not in the repository) with `run.toml`, `cache/`, `work/`, `ram/`, `qmp.sock`, `run.json`.
- `run.toml` is the base TOML with exactly these keys rewritten by line-level substitution: `[Machine].supercache`, every `[Memory.*].memory_file`, `qmp_socket`.
- After substitution, `tomllib` verifies that only those keys changed.
- `normalized_config_sha256` must equal the artifact's RuntimeIdentity value, and the `bundle_id` must match; otherwise deploy refuses.

**Manifest oracle (`scopt deploy`, fixes Design 3's verbatim-manifest error; uses G14):**
1. Run `src/fastdyn/fastdyn-env/bin/python -m scopt_fdshim -q run -c run.toml -o work/` with `cwd=src/fastdyn`. `scopt_fdshim` is a tiny module shipped by scopt and placed on `PYTHONPATH`.
   - It replaces `fastdyn.targets.qemu_target.start_execution` with a stub that records `qemu_cmd` to `work/qemu_argv.json` and returns 0.
   - It then calls `fastdyn.main.cli()`, catching `SystemExit`.
   - The real CLI path therefore runs: `prepare_work_dir`, preprocessing, `setup_qemu`, `build_qemu_cmd` and `_configure_supercache`. The latter writes `cache/<name>.sc.fastdyn.json` with exactly the fingerprint the real run will compute, including the CLI's `-q` `log_level`.
   - No `.sc` exists yet, so nothing is deleted.
2. Sparse-copy `artifact/cache.sc` to `cache/<name>.sc`.
3. `scopt deploy --check` runs the shim twice in two scratch dirs and asserts equal fingerprints. That catches nondeterministic generated files.
4. **Fallback** without the shim: a seed cold run with the same `run.toml` writes the manifest; then the `.sc` is replaced. This costs one run.

**Launch:** `setarch x86_64 -R src/fastdyn/fastdyn-env/bin/fastdyn -q run -c run.toml -o work/`, with `cwd=src/fastdyn` (the plugin path is cwd-relative). The environment is scrubbed of `QEMU_SC_*`. `FASTDYN_QEMU_ICOUNT` and the timer env must be unset or fixed per campaign. RAM files are deleted and recreated before each run (review B5).

**Post-run "artifact used" proof (from Design 3; every run):**
1. The header after the run equals the deployed header except `record_count` (≥ n) and `checksum`. `code_address`, `runtime_signature` and `prologue_end` are unchanged; otherwise QEMU truncated and rebuilt the file (supercache.c:586-610).
2. Record bytes `[0, n)` are byte-identical.
3. The manifest fingerprint is unchanged.
4. `appended = record_count − n` is reported as the fallback-translation count.

A run failing (1)-(3) is `INVALID_artifact_not_used` and is never a timing sample.

### 4.9 What the loader must do for B3 multi-TB regions (`.sc` v6; requirements only)

- **Rejection.** `SUPERCACHE_VERSION=6`. The header grows to 128 bytes: `record_capacity`, `region_table_offset/size/checksum`, `feature_flags`, `artifact_id[16]`. The region table sits after `record_capacity*104`, so appends never overwrite it.
  - A v5 loader rejects the file in `valid_header` but then **truncates and rebuilds** it. That is destructive, so deploy only ever hands it a copy, and the post-run proof (4.8) turns that into a detected INVALID run.
  - A `supercache-strict` option would change `-accel` and therefore the fingerprint. It is deferred, because it needs a FastDyn frontend change.
- **Members** stay ordinary records, so general entries always work.
- **Activation** is lazy:
  - verify the guest_hash of every member and every dependency page;
  - activate and link each member's general TB so that page protection covers every member page;
  - rehydrate plugins once per member;
  - verify helper bindings against the current maps.
- **Specialized entries:**
  - They are never in the QHT under the general key; they are reachable only from certified predecessors through registered patch sites.
  - Each starts with a guard on the **full key before any effect**: `regs[15]`, `thumb`, `condexec_bits`, hflags-derived `flags`/`cs_base` including the per-lookup M bits (hflags.c:550-631), `cflags == curr_cflags`, `cflags_next_tb == −1`, and a loader-maintained `regions_enabled` byte that is cleared while gdbstub, breakpoints or single-step are active. The breakpoint check that `helper_lookup_tb_ptr` performs (cpu-exec.c:396) is skipped by direct transfers, hence that byte.
  - A failed guard takes the general path (store PC, `exit_tb(NULL,0)`) and never re-executes effects.
- **Exits to non-members** go through a per-exit synthetic stub TB struct (Design 3), so the existing `tb_add_jump`/jmp-list machinery unlinks them. **Intra-region hard-coded jumps** are listed in a region patch-site registry.
- **Invalidation:**
  - A hook in `do_tb_phys_invalidate` (tb-maint.c:907-954), a page→region index and `tb_flush` call `sc_region_invalidate`.
  - It atomically resets every registered patch site to its fallback, unlinks the stubs and retires the region.
  - Callee-summary, patch-plan, helper-binding and observer changes are caught by bundle, contract and dependency checks at load.
- **Recovery** (Design 1 mechanism, Design 2 fail-closed proof):
  - The region's host range is a pseudo-TB in the region tree only, never in the QHT.
  - It has **non-PCREL cflags and absolute-PC search rows**, so the unchanged `cpu_unwind_data_from_tb` and `arm_restore_state_to_opc` (cpu.c:101-124) write the full `regs[15]`.
  - It has one row per faultable host return address in every member.
  - The validator proves coverage (V6). B4 adds `state_fixups` through a hook in `cpu_restore_state_from_tb` (translate-all.c:175-195).
  - icount regions are unsupported in v1.
- **Interrupt checks:** every member keeps its entry check with `exit_tb(member_struct, 3)`, and R15 is stored before each member transfer.

---

## 5. Python optimizer: package `scopt`

### 5.1 Layout

The package lives at `tools/scopt` (its own git repo) and is installed editable (`pip install -e`) into a Python 3.12 virtual environment. Dependencies: Python 3.12, capstone 5.0.3 (present), pyelftools, jsonschema, click. `zstandard` is optional.

```
scopt/
  canon.py            canonical JSON, sha256 ids, fnv1a64, record checksum fold
  ids.py              TBKey, tb_uid(), UnitId, SnapshotId, ...
  formats/
    sc_v5.py          header/record structs '<8sIIQQQQQIIIIQ' / '<9QII2H2H2HII4x'; reader (loader rules), sparse writer
    scm_v1.py         .scm reader + writer (writer used for derived scm and synthetic tests)
    qprof_v1.py       .qprof reader
    profile_v1.py     Profile v1 schema, ProfileView, merge, compat
    bundle_v1.py      ExecutionBundle reader + identity builder; RuntimeIdentity
    region_v1.py      CacheRegion v1 dataclasses + JSON Schema
    artifact.py       artifact dir writer/reader, MANIFEST, atomic publish
    schemas/*.json
  decode/
    x86.py            capstone sweep -> DecodedRef list
    thumb.py          guest Thumb/MCLASS length check
    search.py         sleb128 search-data codec + unwind() (GETPC_ADJ=2)
    symbols.py        MAPS parse + pyelftools symtabs -> (module, symbol, offset)
  importer/
    join.py  crosscheck.py  classify.py  recovery.py  import_cache.py (ImportCache service)
  model/
    unit.py           TBUnit, Site, Insn, Ldst, OpMark, StaticExit, Fixup, RelocInfo
    snapshot.py       CacheSnapshot (immutable), WorkingSet (copy-on-write overlay)
    view.py           UnitView, SnapshotView
  analysis/
    store.py registry.py refs.py layout.py cfg.py profile.py hotness.py locality.py
    effects.py (Tier-1 stub)
  passes/
    base.py  identity.py  hotcold.py  cluster.py  order_cold.py  move_units.py (test-only)
  proposal.py  manager.py  link.py (NativeLinker)  validate.py  ledger.py
  graph.py            graph.json export/import
  run/
    toml_rewrite.py  deploy.py  fdshim/ (runs in fastdyn-env)  launcher.py  proof.py  arms.py  diag.py
  report/
    collect.py  render.py
  cli.py
tests/
  unit/  synth/ (builders for .sc/.scm/.qprof and corruptions)  acceptance/  data/
pipelines/  identity.toml  b2-layout.toml
```

### 5.2 Core types (all frozen; byte buffers are `bytes`, mappings are `MappingProxyType`)

```python
@dataclass(frozen=True, slots=True)
class TBKey: page_addr0: int; page_addr1: int | None; flags: int; cs_base: int; cflags: int

@dataclass(frozen=True, slots=True)
class Fixup:
    field_off: int; width: int; rule: Literal["rel_minus_delta", "abs_plus_delta"]
    site_kind: int; target_class: str; target_abs: int | None

@dataclass(frozen=True, slots=True)
class RelocInfo:
    relocatable: bool; reasons: tuple[str, ...]; fixups: tuple[Fixup, ...]

@dataclass(frozen=True, slots=True)
class TBUnit:
    uid: str                      # UnitId "tb:<hex>#k"
    key: TBKey; slot: int; guest_size: int; icount: int; guest_hash: int
    vaddrs: tuple[int, ...]; origin: Literal["elf_verified", "ram_resident", "unknown"]
    tb_offset: int; tc_offset: int; tc_size: int; end_offset: int
    code: bytes; search: bytes; record: bytes                  # record = 104 raw bytes
    insn0_start: int; body_end: int; ldst_end: int; pool_start: int | None
    insns: tuple[Insn, ...]; sites: tuple[Site, ...]; ldst: tuple[Ldst, ...]
    opmarks: tuple[OpMark, ...]; static_exits: tuple[StaticExit, ...]
    reloc: RelocInfo; recovery: tuple[RecoveryPoint, ...]
    bindings: Bindings; meta_status: Literal["ok", "no_meta", "incomplete"]
    content_hash: str             # sha256(record ‖ code ‖ search ‖ canonical meta)

class UnitView(Protocol):         # what passes see
    unit_id: str; snapshot_id: str
    members: tuple[TBUnit, ...]
    def entries(self) -> tuple[Entry, ...]: ...
    def exits(self) -> tuple[Exit, ...]: ...
    def origins(self) -> tuple[GuestOrigin, ...]: ...
    def effects(self) -> "EffectMap | None": ...   # None until Tier 1

class SnapshotView(Protocol):
    snapshot_id: str; header: ScHeader; runtime: RuntimeIdentity; bundle: Bundle
    def units(self) -> tuple[TBUnit, ...]: ...      # slot order
    def unit(self, uid: str) -> TBUnit: ...
    def by_key(self, key: TBKey) -> tuple[TBUnit, ...]: ...
    def layout(self) -> Mapping[str, int]: ...      # uid -> current tb_offset (WorkingSet-aware)
```

### 5.3 AnalysisStore

```python
@dataclass(frozen=True)
class AnalysisSpec:
    name: str; version: int; scope: Literal["unit", "snapshot", "region"]
    requires: tuple[tuple[str, int], ...]            # (analysis, min version)
    inputs: frozenset[str]                           # "code","meta","layout","profile:<phase>","bundle"
    compute: Callable[["AnalysisCtx", ScopeRef], Any]; result_schema: type

@dataclass(frozen=True)
class AnalysisResult:
    name: str; version: int; scope_id: str; value: Any
    dep_units: frozenset[tuple[str, str]]            # (unit_id, content_hash or layout_hash)
    dep_results: frozenset[tuple[str, int, str]]     # (name, version, result_hash)
    dep_inputs: frozenset[str]                       # snapshot_id / profile_id / bundle_id
    certified: bool; result_hash: str

class AnalysisStore:
    def get(self, name: str, scope: ScopeRef, min_version: int = 1) -> AnalysisResult: ...
    def invalidate(self, changed: Mapping[str, frozenset[str]], preserved: frozenset[str]) -> int: ...
    def check_fact(self, r: AnalysisResult) -> None: ...   # raises StaleFact
```

- The cache key is `(name, version, scope_id, digest(dep content hashes, dep result hashes, inputs))`.
- `invalidate` drops results whose `dep_units` or facets intersect `changed`, unless they are in `preserved`, and recurses through `dep_results`.
- Persisted in `analyses.sqlite`; a schema-version mismatch triggers recompute.
- **B1 analyses:**

| Analysis | What it holds |
|---|---|
| `refs.v1` | crosscheck + classify per unit |
| `layout.v1` | intervals, pinned set, max_end, highwater slack |
| `cfg.known_edges.v1` | static and profile edges with evidence |
| `recovery.v1` | |
| `profile.counts.v1` | per phase, with status |
| `hotness.v1` | classes `hot`, `warm`, `boot_only`, `cold_zero`, `unknown` |
| `locality.v1` | hot bytes, 64 B lines and 4 KiB pages spanned, weighted hot-edge distance |

### 5.4 Pass API

```python
class EditKind(Enum):
    ANNOTATE = 1; PIN = 2; LAYOUT_ORDER = 3; PLACE = 4                 # B2
    CLONE = 10; REDIRECT_EXIT = 11; ADD_ENTRY = 12; PURE_REWRITE = 13  # B3/B4 (defined, rejected in B2)
    ADD_REGION = 14; ADD_RECOVERY = 15; ATTACH_FACT = 16

@dataclass(frozen=True)
class Budgets:
    max_wall_s: float = 60.0; max_code_growth_bytes: int = 0
    max_region_members: int = 1; max_context_versions: int = 0; max_units: int | None = None

@dataclass(frozen=True)
class PassDescriptor:
    name: str; version: int; domain: Literal["HostCache"] = "HostCache"
    unit_scope: Literal["unit", "snapshot", "region"] = "snapshot"
    requires: tuple[tuple[str, int], ...] = ()
    requires_profile_phase: str | None = None; min_profile_match: float | None = None
    requires_capture_level: int = 1
    capabilities: frozenset[EditKind] = frozenset()
    budgets: Budgets = Budgets()
    preserves: frozenset[str] = frozenset()         # analysis names, or {"*"}
    invalidates: frozenset[str] = frozenset()       # facets: "layout","code","meta"
    params_schema: Mapping[str, type] = MappingProxyType({})

class Outcome(Enum):
    NO_CHANGE = "no_change"; CANDIDATE = "candidate"; SKIPPED_UNSUPPORTED = "skipped_unsupported"
    SKIPPED_UNKNOWN = "skipped_unknown"; BUDGET_EXHAUSTED = "budget_exhausted"; FAILED = "failed"

@dataclass(frozen=True)
class PassResult:
    outcome: Outcome; reason: str                    # stable code, e.g. "unexplained_reference"
    detail: str = ""; affected: tuple[str, ...] = ()  # UnitIds / RegionIds
    guest_origins: tuple[str, ...] = ()               # vaddr ranges
    stats: Mapping[str, float] = MappingProxyType({})

class Pass(Protocol):
    descriptor: ClassVar[PassDescriptor]
    def run(self, unit: UnitView | SnapshotView, ctx: "PassContext",
            edits: "ProposalBuilder") -> PassResult: ...
```

`PassContext` exposes:
- `analysis(name, scope, min_version=1)` and `fact(...)`;
- `profile(phase)`, which returns a ProfileView or None;
- `params`, `bundle`, `runtime`, `snapshot_id`;
- `budget`, a monotonic deadline that raises `BudgetExceeded`;
- `log`.

### 5.5 ProposalBuilder

| Method | Kind | Preconditions / effect |
|---|---|---|
| `annotate(uid, key, value)` | ANNOTATE | Metadata only; visible to later passes via the `annotations` analysis. |
| `pin(uid, reason)` | PIN | Keep the unit at its original offset. |
| `layout_order(section, uids)` | LAYOUT_ORDER | section ∈ {`hot`, `warm`, `unknown`, `boot`, `cold`}. Every uid must be `ok_relocatable` and not pinned. |
| `place(uid, tb_offset)` | PLACE | 64-aligned, explicit. Used only by `move_units` (test pass) and the placer. |
| `clone / redirect_exit / add_entry / pure_rewrite / add_region / add_recovery / attach_fact` | B3/B4 | Defined with CacheRegion v1 types. The B2 manager rejects them with `capability_not_enabled`. |

- Each edit records:
  - `reads`: the `(uid, content_hash)` and `(analysis, result_hash)` pairs it depends on;
  - `affected_origins`;
  - `dependency_additions`;
  - its descriptor.
- An edit kind not in `descriptor.capabilities` raises `EditCapabilityError`, and the pass result becomes FAILED.
- Layout edits never carry bytes; `NativeLinker` derives the fixups.

### 5.6 PassManager

`PassManager(snapshot, pipeline, profiles, params, store).execute() -> Candidate`. Steps:

1. **Load the pipeline TOML** (`[[pass]] name, version, params`, plus `allowed_capabilities`).
2. **Admission** per pass:
   - required analyses and their versions;
   - profile phase and match rate;
   - capture level;
   - capabilities ⊆ allowed.

   On failure the pass is recorded as SKIPPED_UNKNOWN or SKIPPED_UNSUPPORTED with a reason, and the pipeline continues.
3. **Run** on a fresh view of the current `WorkingSet`, under the budget:
   - an exception gives FAILED, with the sha256 of the traceback in `detail`;
   - `BudgetExceeded` gives BUDGET_EXHAUSTED;
   - in either case the pass's edits are discarded.
4. **Edit check:**
   - capability membership;
   - `reads` still current (otherwise `stale_edit`);
   - cited facts valid (otherwise `stale_fact`);
   - budgets;
   - layout only on relocatable units;
   - conflicts: two orderings of the same uid are resolved by "later pass wins" only if the later pass declares `overrides=("layout",)`, otherwise `conflict`.
5. **Apply** the edits to `WorkingSet n+1`, call `store.invalidate(...)`, and append provenance.
6. **After the pipeline:** `NativeLinker.link()` → `ValidateAndPackage` → atomic publish.
   - Per-unit link failures (e.g. `rel32_range`) roll that unit back to its original offset when the slot is still free and re-run the placer once. Otherwise the candidate is rejected; no artifact is published, and the input snapshot stays the reference.
7. **Ablation:** `--disable <pass>` and `--only <pass>` produce separate artifacts, so every pass has its own enabled/disabled comparison.

### 5.7 NativeLinker (B2 placer and fixups)

- **Section order:** pinned units first, as fixed obstacles. Then first-fit from `ROUND_UP(prologue_end, 64)` in section order `hot` (cluster order) → `warm` → `unknown` → `boot` → `cold` (original relative order).
- **Per unit:** `tb' = ROUND_UP(cursor, 64)` skipping obstacles, `tc' = tb' + (tc − tb)`, `end' = tc' + (end − tc)`, `d = tb' − tb`.
- Copy `[tc, end)` and apply the Fixups: `rel_minus_delta` gives `field' = field − d` with an exact int32 check; `abs_plus_delta` gives `+ d`.
- Records keep the **original slot order**.
- Constraint: `max_end' < code_size − host_page − 1024`.
- The linker also emits the derived `.scm` (4.6).

### 5.8 ValidateAndPackage (every check is a row in `validation.json`; any FAIL rejects)

| Code | Check |
|---|---|
| V1 structural | Header equals the input header except checksum. Every record passes the replicated `load_records`. `tb%64==0`; `tc−tb == ROUND_UP(tb_size,64)`. `[tb,end)` intervals are pairwise disjoint and ≥ `ROUND_UP(prologue_end,64)`. max_end bound holds. Slot order is preserved. All bytes outside records are zero. File size = `0x10000+code_size+n*104`. Re-parsed by `sc_v5` **and** by the independent `tools/scopt/independent/scparse.py`. |
| V2 byte diff | Per unit, `new[tc':end'] XOR old[tc:end]` is non-zero only inside declared fixup fields. |
| V3 re-decode | A full capstone sweep at the new address gives the same tiling and mnemonics. Each reference resolves to the same semantic target: intra offset, prologue symbol, external absolute address, own struct = `tb'+idx`, pool slot value, host value. Two-sided equality with the (relocated) sites. 0 unexplained. |
| V4 range | Every rel32 and disp32 recomputed from the decode fits int32. goto_tb fields are 4-aligned and 0. Pool start is 8-aligned in absolute address. |
| V5 identity | The multiset of `(UnitId, key, guest_hash, guest_size, icount, jmp_*)` is equal to the input, with no add or drop. Search-data bytes are identical. |
| V6 recovery | For every recovery point, `unwind()` at the new placement gives the same `(insn, words)` as the original, and equals the OpMark insn (2.7 step 7). |
| V7 compat | `runtime_signature`, `code_address`, `code_size`, `prologue_end`, `tb_size`, `target_page_bits` and `host_page_size` are copied. `bundle_id` and `runtime_identity_id` equal the import's. `supercache_flat=false`. A v6 artifact requires a v6-capable RuntimeIdentity. |
| V8 provenance | The sidecar is complete. MANIFEST hashes verify. The derived `.scm` re-imports to `output_snapshot_id`. |
| V9 identity | For the identity pipeline: `sha256(out cache.sc) == sha256(in cache.sc)` when the input is canonical (all records valid, file size exact). Otherwise `identity_canonicalized`, with the dropped-record list. |
| V10 dynamic | A separate ledger stage (`scopt validate --dynamic`): held-out correctness inputs, normalized outputs equal to the warm baseline, the "artifact used" proof (4.8), the appended count reported, and exit code and fault reports equal. |

### 5.9 Outcome ledger

- `ledger.jsonl` rows: `{subject: unit|pass|artifact|run, id, stage, verdict, reason, evidence_ref, ts}`.
- Stages: `inspected` (imported and classified), `compiled` (candidate bytes emitted), `accepted` (V1-V9 passed), `validated_dynamic` (V10 passed), `measured` (paired timing collected).
- The report shows each stage as its own column and never infers one stage from another (spec §6).

### 5.10 Identity pass and a pass-writer example

```python
class Identity:
    descriptor = PassDescriptor(name="identity", version=1, preserves=frozenset({"*"}))
    def run(self, unit, ctx, edits) -> PassResult:
        return PassResult(Outcome.NO_CHANGE, "identity")

class HotColdPartition:
    """B2 classification. Annotates only; cold code is never removed."""
    descriptor = PassDescriptor(
        name="hotcold-partition", version=1, unit_scope="snapshot",
        requires=(("profile.counts", 1), ("refs", 1)), requires_profile_phase="steady",
        min_profile_match=0.95, capabilities=frozenset({EditKind.ANNOTATE}),
        budgets=Budgets(max_wall_s=10),
        preserves=frozenset({"refs", "layout", "cfg.known_edges", "profile.counts", "recovery"}),
        params_schema={"phase": str, "coverage": float})

    def run(self, snap, ctx, edits):
        phase = ctx.params.get("phase", "steady"); cov = ctx.params.get("coverage", 0.99)
        steady = ctx.analysis("profile.counts", snap).value.phase(phase)   # {uid: Count(value,status)}
        boot = ctx.analysis("profile.counts", snap).value.phase("boot")
        exact = {u: c.value for u, c in steady.items() if c.status == "exact"}
        if not exact:
            return PassResult(Outcome.SKIPPED_UNKNOWN, "no_exact_counts")
        total, acc, hot = sum(exact.values()), 0, set()
        for u, v in sorted(exact.items(), key=lambda kv: (-kv[1], kv[0])):   # deterministic
            if total and acc / total >= cov: break
            if v > 0: hot.add(u); acc += v
        for u in snap.units():
            c = steady.get(u.uid)
            if u.uid in hot: cls = "hot"
            elif c is None or c.status != "exact": cls = "unknown"
            elif c.value > 0: cls = "warm"
            elif boot.get(u.uid) and boot[u.uid].status == "exact" and boot[u.uid].value > 0: cls = "boot_only"
            else: cls = "cold_zero"
            edits.annotate(u.uid, "hotness", cls)
        return PassResult(Outcome.CANDIDATE, "classified", affected=tuple(sorted(hot)),
                          stats={"hot": len(hot), "coverage": acc / max(total, 1)})
```

The other B2 passes:
- **`cluster-hot@1` (LAYOUT_ORDER):** Pettis-Hansen greedy chain merge over the exact `chain0`/`chain1`/`lookup_hit` edge weights between hot units, favouring the hottest chain and then fallthrough-like CHAIN edges. Ties break by UnitId.
- **`order-cold@1` (LAYOUT_ORDER):** `warm`, `unknown`, `boot_only` and `cold_zero` sections, each in original slot order.
- **`move-units@1`:** test-only, PLACE.

The placement itself belongs to `NativeLinker`. Struct segregation (all structs in a block before the code) is a possible B2.1 option and is **not** in B2.

### 5.11 CLI

```
scopt qualify-capture -c BASE.toml --runs 3                 # T-B1-1a/1b; writes qualification.json per (qemu,plugin) build
scopt prepare -c BASE.toml --out W/prep [--inputs train/*]  # fresh run dir, QEMU_SC_CAPTURE set, cold run(s)
scopt bundle identity -c BASE.toml --manifest W/prep/cache/x.sc.fastdyn.json --out W/bundle.json
scopt import W/prep --bundle W/bundle.json --store STORE [--strict]   # prints coverage + pin reasons; exit 2 on reject
scopt graph export STORE/snapshots/S --out W/graph.json ; scopt graph import W/graph.json --check S
scopt profile -c BASE.toml --snapshot S --phase steady@0x08003972 --inputs train/* --reps 5 --out W/prof
scopt optimize S [--profile P...] --pipeline identity|b2-layout [--disable PASS] [--only PASS] --store STORE
scopt validate A [--dynamic -c BASE.toml --inputs heldout/*]
scopt deploy A -c BASE.toml --run-dir R [--check]
scopt run -c BASE.toml --arms nocache,empty,warm,identity,layout --artifacts A... --set train|heldout|final \
          --reps 30 --interleave --out W/timing
scopt diag exec --arm X --run-dir R      # direct QEMU with recorded argv, own cache copy, -d exec,int -D (no nochain)
scopt inspect unit|ref|edges|diff ...  ; scopt meta dump --jsonl X.scm
scopt report W --out W/report.md         # always succeeds; unsupported/rejected stages are rows
```

All QEMU runs use `setarch x86_64 -R`, `supercache_flat=false`, and a fresh per-run directory. Any gdb use inside scopt is `gdb -nx`, run with `cwd` outside `src/qemu`.

---

## 6. B1 test plan and acceptance gate

The **B1 completion gate** is: T-B1-0 … T-B1-9 all pass on the minimal fixture, and T-B1-0/1/3/5 also pass on FreeRTOS MPS2 CM3 once that workload is qualified.

| ID | Test | Pass criterion |
|---|---|---|
| T-B1-0 | Re-qualify B0 with the integrated capture+profile binary, with capture and profile off: nocache, cold, warm×2 on `b0-*` configs through per-run dirs. Also measure the disabled-hook cost: ≥30 interleaved warm BENCH samples, new binary vs the B0 binary (each with its own cache). | Normalized outputs equal (`*.norm`, `Reg 0:0x00000003`, BENCH present). Warm runs append 0 records. The post-run proof passes. Overhead is reported with a CI; it is not gated, but a median >1% is flagged in the report. |
| T-B1-1 | Byte identity (2.6): 1a three capture-off cold runs; 1b two capture-on cold runs, all in the same run dir | Whole-file sha256 equality if 1a is deterministic; otherwise the per-key normalized equality with absolute host values equal, reported as the weaker mode. |
| T-B1-2 | `sc_v5` golden test plus independent parser | Fresh minimal `.sc`: header and records valid. v5, tb_size 152, `tb%64==0`, `tc−tb==192`, page bits 10. Our parser and `scparse.py` agree on every field. Read→write round trip is byte-identical. |
| T-B1-3 | Import cross-check | 100% of slots bind, 0 `unexplained_reference`, 0 `missing_reference`, 0 `recovery_mismatch`. Class totals equal G17 exactly: 394 / 3 / 3 / 316 / 3 / 316 (+0:105, +1:50, +3:161) / 336 / 52 / 0 movabs. The CF_NOIRQ io-recompile TBs are `ok_relocatable`. Helper symbols equal `HELPER` names where non-NULL. |
| T-B1-4 | Graph round trip **without guessing** | Full snapshot and a two-TB goto_tb pair (oracle: a `scopt diag exec` "Linking TBs" line): export→import→export is byte-identical. Every edge has a captured exit site or profile evidence. Static edges carry candidates, never `dst`. goto_ptr/exit_null are `dynamic`. A seeded destination absent from the snapshot gives `not_in_snapshot`. |
| T-B1-5 | Identity artifact, static and dynamic | `optimize --pipeline identity`: V1-V9 pass, including the V9 sha256 equality, and the derived `.scm` re-imports to the same SnapshotId. Then deploy and run warm 5× vs warm-baseline 5× interleaved: equal normalized outputs, post-run proof passes, appended = 0 in both. `scopt diag exec`: every `Trace` `tc.ptr` equals `code_address+tc_offset` of a snapshot record. |
| T-B1-6 | Profile exactness | Flow conservation (3.4) holds for every unit and phase. Σ entries per `(vaddr, flags, cs_base)` equals the `-d exec,nochain` Trace count from a direct QEMU run (cflags `& ~CF_NO_GOTO_TB`): exactly if T-B1-1a was deterministic, else within the documented tolerance. The `0x08018766` TB is ≈19.4k entries and all in `boot`. Phase `steady` is `reached` at the `0x08003972` marker. |
| T-B1-7 | Profile isolation | sha256 of the source snapshot, of published artifacts and of the base manifest is unchanged after profiling. Appended slots are reported as PROFILE_ONLY. |
| T-B1-8 | Recovery oracle | For every unit, every Ldst raddr and helper return unwinds to its OpMark insn (2.7 step 7). A synthetic unit with a shifted `host_end` row is rejected with `recovery_mismatch`. |
| T-B1-9 | Rejection suite (pytest, synthetic builders plus real files); each case asserts its reason code | `record_checksum` (flipped record byte: skipped exactly like QEMU); `header_checksum`; `version` (v6 header); truncated record array (`count=min(...)`); `record_mismatch` (capture from another run); `chunk_checksum` (bad chunk FNV: the chunk and its suffix are dropped); `missing_reference` (a site removed from bytes); `unexplained_reference` (a site removed from the capture: pinned); `code_fnv_mismatch` (code byte flip: `no_meta`); RUN_INFO `runtime_signature` ≠ header; `bundle_mismatch` (modifiers.txt edited); `profile_incompatible` (other bundle, or coverage < threshold); a profile with `clean_exit=0` gives `lower_bound`, never `exact`; `unsupported_flat`; `unsupported_arena_below_4g`; MANIFEST that fails to verify; post-run proof on a v6-stamped copy gives `INVALID_artifact_not_used`. |

## 7. B2 fixtures and gate

The **B2 gate** is: F-B2-1 … F-B2-6 pass on minimal and on the fault fixture, and F-B2-7 is measured and reported, regressions included, on FreeRTOS MPS2 CM3 with frozen artifacts and held-out inputs.

| ID | Fixture | Pass criterion |
|---|---|---|
| F-B2-1 | `move-units`: relocate the hottest steady-phase unit (on minimal: the hottest relocatable unit) to `ROUND_UP(max_end,64)`, then by +4 MiB | V1-V9 pass. Capstone diff only at its prologue/ext fields. Dynamic: equal outputs, appended = 0, post-run proof passes. A profile on the artifact attributes executions to the new `tb_offset`. `diag exec` shows the new `tc.ptr`. |
| F-B2-2 | Reverse every relocatable unit's placement | Exercises every fixup class (316 tb_ret, 3 epilogue, 3 plugin calls, 3 plugin LEAs). Equal outputs. Pinned units untouched. A synthetic duplicate-key unit test shows LIFO order preserved. |
| F-B2-3 | **Fault attribution**: new `fixtures/fault/fault_fixture.elf` (Cortex-M3, built with Arm GNU Toolchain 13.3.Rel1) | (a) `CCR.UNALIGN_TRP` set, then an unaligned `LDR` as the 3rd instruction of a hot loop TB: UsageFault via the softmmu slow path (raddr route). (b) `CCR.DIV_0_TRP` and `SDIV` by 0: helper GETPC route (op_helper.c:134-169). (c) a load from an unmapped address: BusFault. (d) an MMIO access in a non-final instruction: `cpu_io_recompile`. The fault handler copies the stacked PC (`SP+24`), xPSR and CFSR/BFAR into r0..r3 and hits `printreg` virtuals. Printed values are identical across nocache, warm, identity, and the artifact with the faulting units moved ±64 MiB, and the PC equals the ELF address of the faulting instruction. In (d), the one-instruction CF_NOIRQ TB appears with the same key in both arms. Static V6 agrees. |
| F-B2-4 | Negative: unexplained reference (a tampered capture with one site removed) | The unit is pinned. Layout reports it as `skipped_unsupported:unexplained_reference` in `affected`. The artifact is still valid and runs. |
| F-B2-5 | Negative: rel32 range. Force a unit that calls a plugin function to `tb_offset ≈ 0x60000000` | V4 or the linker rejects that placement with `rel32_range`. The rollback re-places it, or the candidate is rejected with provenance `failed:rel32_range`. The baseline is retained. |
| F-B2-6 | `no_meta` unit: append a record via a capture-off warm run on a held-out input, then optimize | The unit stays fixed and layout plans around it. Equal outputs. |
| F-B2-7 | First real workload: FreeRTOS MPS2 CM3 (built via `fastdyn/tests/integration/run_freertos_introspection_smoke.sh`, `machine=cortexm`; qualified first by B0 rules). Arms: warm, identity, layout, plus nocache and empty. Separate train, held-out and final inputs. ≥30 interleaved paired repetitions. | Report: median paired speedup with bootstrap CI, raw times, BENCH vCPU time, fallback (appended) counts, configured arena capacity, logical file size, allocated blocks, populated native bytes, static locality metrics (hot bytes, 64 B lines and 4 KiB pages covering 90% of steady entries; labelled as models), and every regression. No retuning after looking at final inputs. |

---

## 8. Workflow summary (prepare → optimize → validate → run → report)

`qualify-capture` → `prepare` (capture) → `bundle identity` → `import` → `graph export` → `profile` → `optimize identity` → `validate` → `optimize b2-layout` → `validate --dynamic` → `run` (arms) → `report`. Each stage writes ledger rows. `report` renders whatever exists and marks absent stages `not_run`/`unsupported`.

---

## 9. Explicitly unsupported in B1/B2 (rejected with a reason code; baseline kept)

| Case | Reason code |
|---|---|
| `supercache_flat=true` | `unsupported_flat` |
| split-wx, MTTCG or more than one vCPU | `unsupported_threading` (QEMU already refuses, supercache.c:322-329) |
| non-x86-64 host | `unsupported_host` |
| `code_address < 4 GiB` (imm32 arena pointers possible) | `unsupported_arena_below_4g` |
| changed `code_address`/`code_size` (no arena rebasing) | `unsupported_rebase` |
| Non-M-profile or non-PCREL guests | `unsupported_guest` |
| icount mismatch between snapshot, profile and run | `icount_mismatch` |
| CF_USE_ICOUNT units: imported and moved, but excluded from F-B2 gates until qualified | — |
| Adding, dropping, splitting, cloning or merging TBs; hot/cold splitting inside a TB (u16 end offsets, single host range); multi-TB regions; specialized entries; any semantic rewrite | `capability_not_enabled` (B3/B4, needs v6) |
| Merging snapshots from different capture sessions unless overlapping slots are byte-equal and bundle and runtime match | `snapshot_merge_conflict` |
| `no_meta`, `incomplete` or pinned units: kept but never moved | — |
| RAM-executed or self-modifying code: B2 moves such units (the guest-hash check still guards them); they are marked `ram_resident` and excluded from future regions | — |
| Profiles that are not exact: `lower_bound` priority only; `unknown` is never treated as cold | — |
| Detour configurations (uninitialized `entry.data`, map-fastdyn C19) and gated modifiers (C9) | `unsupported_contract` |
| `-d nochain`, gdbstub, single-step, one-insn-per-tb in production arms (keys differ) | refused by `scopt run` |
| AVX-512/EVEX code not decodable by capstone 5.0.3 | pinned (`decode_failed`) |

---

## 10. Risks and mitigations

1. **Unchecked host pointers.** The cached code embeds heap, guest-RAM and plugin-data pointers that runtime_signature does not cover. Rehydrate may malloc a new logger buffer (map-fastdyn C18), and the mem-cbs GArray is not recreated (C16).
   - This is a baseline hazard; B2 preserves the absolute values.
   - Mitigations: capture never allocates (R2); T-B1-1 compares absolute values; validation records the bindings and can run an optional address-witness comparison of MAPS/RAMBLOCKS between a capture run and a later capture-on run.
   - Configurations whose instrumentation mallocs at translation time (logger, variable_watch, object_sanitizer mem callbacks) are flagged `identity_risk` in the report.
2. **Disabled-hook cost on production hits.** Hooks P1 and P3 sit on the hot dispatch path as `unlikely` branches; T-B1-0 measures them. If the median exceeds 1%, move the two P-hooks behind a `static` function-pointer indirection that is patched only in profile mode. The binary stays the same.
3. **Profile bias** from unchained execution (3.8): placement only, `time_dilation` reported.
4. **External rel32 overflow:** compact placement plus V4.
5. **Frontend coupling:** the shim relies on `fastdyn.main.cli` and `qemu_target.start_execution` at 0e13b739. There is a seed-run fallback, and a `fastdyn supercache-manifest` upstream command is recommended.
6. **Hook drift** (a new emission path): two-sided matching turns it into `unexplained_reference` or `missing_reference` pins, never a silent mis-relocation.
7. **Prologue differences across hosts** (vzeroupper/AVX): `prologue_sha256` and `cpuinfo` are in RuntimeIdentity; artifacts are host-specific.
8. **Timing-dependent TB sets:** units are compared by UnitId, never by slot. T-B1-1a decides the evidence mode.
9. **Shared RAM files:** deleted and recreated per run.
10. **Uncommitted compat patch:** every QEMU worktree must apply `patches/qemu-fastdyn-plugin-icount.patch` (QEMU commit `00c19f7ee0`) before the capture and profile patches. RuntimeIdentity records the qemu sha256.

---

## 11. Decisions and rejected alternatives (with the review concern that drove each)

J1 = soundness review (winner Design 2); J2 = feasibility review (winner Design 1).

| # | Decision | Rejected alternative | Driver |
|---|---|---|---|
| D1 | The base is Design 1's Tier-0 capture: ≈18 one-line hooks, env-var switch, binary chunked `.scm`, commit inside `tcg_supercache_record`, pending-tag protocol | Design 2's 23-hook level-1 capture with plugin-gen and FastDyn-generator hooks (deferred to Tier 1); Design 3's H11-H14 exit-reason, pointer-registry and register-allocator hooks | J2: smallest diff, fastest to B2; J1 and J2: pending tag with capture_error is fail-closed |
| D2 | Identity excludes the vaddr: `TBKey=(page0, page1, flags, cs_base, cflags)`, `tb_uid` adds size, icount and guest_hash; vaddr is an attribute | Design 1's "full key" with vaddr inside TBId | J1 and J2 factual error (cpu-exec.c:208-209; supercache.c:198-206, 836) |
| D3 | Static goto_tb edges store `dest_vaddr`, `dest_ram` and **candidates**; they never assert `dst` | Design 1's `resolved_unique` by vaddr+flags of the source | J1: condexec and flags at dest can differ, so that is a guess |
| D4 | Hook all 4 `tcg_out_jxx` paths; no INTRA_STRUCTURAL exception | Design 2's `tcg_out_reloc`-only capture; Design 3's structural exception | J1 and J2 factual error (tcg-target.c.inc:1509-1526); J1: the exception weakens drift detection |
| D5 | Pool capture at `tcg_out_pool_finalize` per entry, gated to TB staging | Design 1's `new_pool_alloc` hook | J2 factual error: data is not yet written at alloc (tcg.c:674-716); J1 minor line error |
| D6 | Slow-path capture in the generic `tcg_out_ldst_finalize` loop, plus one backend tag in `ldst_ra_gen` | Design 1's two backend slow-path hooks | J2 graft from Design 2 |
| D7 | Importer range-checks every imm32/disp value against the arena and MAPS; `code_address < 4 GiB` is unsupported | Design 1's "pointers only via movi, so other immediates are constants by construction" | J1 factual error (`tcg_out_sib_offset` absolute path, arithmetic immediates) |
| D8 | No discard hook; staging is reset at the next begin | Discard hooks at supercache.c:707-710 (Design 1, Design 2) | J2 factual error: one-shot TBs return at translate-all.c:518-522 before the record |
| D9 | The capture fd is moved to ≥1000; `QEMU_SC_*` is scrubbed from production with a post-run assert | Opening at a low fd (Design 1) | J1 and J2 graft from Design 3 |
| D10 | FNV-64 only inside QEMU (`code_fnv64`); sha256 only in Python | Design 2's in-QEMU sha256 code hash | J2: hashing under the no-heap rule would need a self-contained sha256 that Design 2 never specified |
| D11 | Byte identity is shown by an off/off control plus on runs, with whole-file sha256 or the documented per-key fallback | Design 1's single on-vs-off comparison of common keys | J1: no control separating nondeterminism from capture effects; J2 graft from Design 3 |
| D12 | Profile by unchained dispatch observation on a warm copy (no code change, exact slot attribution); `transient` bucket for one-shot TBs | Design 3's entry helper in generated code; Design 2's `complete=false` on unrecorded TBs | J2: riskier translator diff and a heuristic join; J1: over-conservative completeness |
| D13 | Phases by containment, with `reached` status and `unknown/phase_not_reached` | Design 1's exact TB-start trigger with no not-reached status | J1 flaw; graft of Design 3's `unknown_incomplete` |
| D14 | Corrected flow conservation: `in == entries`, `out == entries − last` | Design 3's `in == exec + bailed` | J1 factual error |
| D15 | Exact zero needs only completeness and domain membership | Design 3's page-coverage requirement | J1 and J2: real zeros were marked unknown |
| D16 | The manifest for each fresh run dir is produced by FastDyn's own CLI with `start_execution` stubbed (G14); seed-run fallback | Design 1's monkeypatch of `_configure_supercache`; Design 2's blessed reusable slots; Design 3's verbatim manifest copy | J2: setup_qemu is separable and simpler; J1 and J2 factual error on the verbatim copy; the user decision requires separate cache and work dirs per run |
| D17 | Post-run "artifact used" proof on every run | Trusting the loader to accept | J1 and J2 graft from Design 3 (supercache.c:586-610 truncates silently) |
| D18 | B2 keeps the **original slot order** | Sorting records while preserving only duplicate order (Designs 2 and 3) | J1 and J2: trivially preserves LIFO and used fallthrough (supercache.c:880-897) |
| D19 | Recovery attribution is checked at import from emission-time OpMarks, and again in V6 | Recovery checked only after a move | J1 and J2 graft from Design 2 (B3 oracle) |
| D20 | V9 identity sha256 plus a second independent parser (`scparse.py`) | Structural equality only | J1 and J2 graft from Design 3 |
| D21 | `memory_map_hash` from config banks; RAMBLOCKS are diagnostics only | Design 1's capture RAMBLOCKS (host pointers) in a bundle identity | J1 |
| D22 | Derived `.scm` in artifacts (re-import closure); outcome ledger | No closure; collapsed outcomes | J2 grafts from Designs 2 and 3; spec §6 |
| D23 | Runtime-signature statement: plugin placement is not covered (G12) | Design 2's claim that an mmap shift of the plugin changes the signature | J1 factual error |
| D24 | B3 v6: Design 1 pseudo-TB non-PCREL recovery, Design 2 fail-closed coverage proof, Design 3 stub TBs for exits, no `supercache-strict` in v1 | `supercache-strict` as a new `-accel` property | J1: it changes the FastDyn `-accel` string and fingerprint |
| D25 | The Python framework is kept, but B1 ships the thin path first (formats → meta-less decode → importer → identity) before AnalysisStore persistence | Building the full framework before the first evidence | J2: framework over-built for B1/B2; IMPLEMENTATION-PLAN orders the work accordingly |
| D26 | Build type statement corrected: `optimization=2`, `debug=true` despite `buildtype=debug` | Design 1's and review A6's "maybe not -O2" | Verified in `build/meson-info/intro-buildoptions.json` |
| D27 | Design 3's class 20 `R_386_32` arena pool references are dropped | — | J2 factual error: they occur only on 32-bit hosts (tcg-target.c.inc:1047-1088) |
