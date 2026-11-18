# B3/B4 final design: guarded cross-TB specialization (`.sc` v6) and bounded state-traffic elimination

Status: **final synthesized design for milestones B3 and B4, revised after three reviews** (source facts, counterexamples, spec coverage and plan; the reviews are not in the repository); ready to implement (Student 2, the project specification (not in the repository) §6). Date 2026-11-17. Every review finding and its resolution is listed in §19; text changed by the revision carries a tag `[R n]` (major finding) or `[m-…]` (minor finding) that points to §19.
Pinned runtime: the integrated WP-QINT QEMU (branch `qint`, HEAD `1258c6a3f2`; QEMU 12f5e688 + FastDyn compat patch + sc-base/capture/profile patches), FastDyn 0e13b739, host x86-64, guest Cortex-M3 Thumb, CF_PCREL, one vCPU.

How it was built. Three candidates were reviewed (the soundness review chose C, the feasibility review chose A; the candidates and reviews are not in the repository). This design is **C's soundness model on A's runtime mechanics**:
- from C: the run-time guard covers the *whole* lookup key plus the breakpoint list; TLB-flush disarm and an arm-time fetch probe; B4 without any compensation code; the host-level validator `hostsym` (with its mutation suite and native self-test); the obligation table;
- from A: the 11-byte lookup tail becomes a same-length `jmp` to a stub appended after the pool (no splicer, no row re-encoding); continuation versions are relocated byte clones with same-length patches only (identical search rows); loader-only slots whose reset path is the original lookup; the 80-byte header kept with an extension table at file offset 0x100; the patch-plan join for provenance; the value probe first;
- from B: retire on plugin reset/one-insn-per-tb, fail-loud on a malformed v6 extension, `QEMU_SC_V6_STATS` counters, Tier-1 capture as the stable-representation cross-check, static off-page chaining as a first-class (separately reported) pass.

Every `must_fix` of both reviews is resolved; §17 maps each one to the section that resolves it. Every review disagreement is decided in §16.

Citations: `file:line` is relative to the WP-QINT QEMU tree for QEMU sources, to the maintainer's workspace otherwise (prefixed `fastdyn/` for `src/fastdyn`, i.e. the FastDyn repository root; `scopt/` for `tools/scopt/scopt`).
Marks: **V** = verified for this document (code read or check run); **V-j** = verified by a candidate-design review, not re-read here; **V-c** = verified by a review of the draft, not re-read here; **V-map** = verified by the author of a subsystem map (maintainer's notes), not re-read; **I** = inferred from verified facts; **U** = unknown, with the experiment that settles it (§15).

---

## 0. Ground truth this design relies on

| # | Fact | Evidence | Mark |
|---|---|---|---|
| G1 | `.sc` v5 header is 80 bytes; `reserved` (u32) is at byte **68**, `checksum` at 72 and covers [0,72). Records are 104 bytes: fields end at 100, record `checksum` (u32 at 92) covers [0,92); bytes 100..103 are padding. | supercache.c:41-78, 135-146 | V (A's "reserved at 0x4c" is wrong; 0x4c is inside `checksum`) |
| G2 | `valid_header` requires `version == 5` and `header_size == 80`. On failure the loader `ftruncate`s to `0x10000+code_size` and writes a fresh v5 header (destructive, silent). File bytes [80, 0x10000) are neither read nor written by any path. | supercache.c:389-401, 611-635, 293-305 | V |
| G3 | `load_records` folds `end_offset` into `max_end` only for records that pass the commit/checksum/order checks; `after_prologue` reads code only `[prologue_end, max_end)` and sets `code_gen_ptr = buffer + max_end`. | supercache.c:433-446, 674-693 | V |
| G4 | Activation is lazy, inside `lookup_entry`: key/invalid check, guest-hash check, struct rebuild, `tb_reset_jump` on both registered slots, rehydrate, `tcg_tb_insert` (region tree), `tb_supercache_link`; if another TB owns the key the record is dropped. | supercache.c:815-877 | V |
| G5 | `tb_flush` with a supercache keeps allocation state: `tcg_supercache_runtime_reset` clears `used/invalid`, the region tree is reset; records are re-activated lazily. | region.c:411-424; supercache.c:715-723 | V |
| G6 | Non-flat host lookup (`tcg_tb_lookup`) uses only the region tree (`tcg_supercache_host_lookup` returns NULL unless flat). | region.c:249-262; supercache.c:949-961 | V |
| G7 | `helper_lookup_tb_ptr`: sets `can_do_io = true`, key from `get_tb_cpu_state` with `curr_cflags`, `check_for_breakpoints` (only when the list is non-empty; may change cflags to a BP-page variant or `cpu_loop_exit`), `tb_lookup`, returns `tc.ptr` or the epilogue. | cpu-exec.c:364-418 | V |
| G8 | `tb_add_jump` refuses `CF_INVALID` targets, `cmpxchg(jmp_dest[n], NULL, next)`, patches the slot to `next->tc.ptr`, pushes `tb|n` on `next`'s list. The main loop chains `last_tb|tb_exit` to the looked-up TB unless it spans two pages or profile mode is on; it never checks that source and target share a page. | cpu-exec.c:635-670, 993-1009 | V |
| G9 | `tb_remove_from_jmp_list(orig,n)` sets the LSB of `jmp_dest[n]` and unlinks `orig|n` from the destination's list but **does not reset orig's own jump**; `tb_jmp_unlink(dest)` resets every incoming slot to `tc.ptr + jmp_reset_offset[n]`; `do_tb_phys_invalidate` sets `CF_INVALID` and returns before both for a TB not in the QHT. | tb-maint.c:805-851, 856-878, 907-954 (early return at the `qht_remove` check) | V |
| G10 | `curr_cflags` adds `CF_NO_GOTO_TB|CF_NO_GOTO_PTR|CF_SINGLE_STEP|1` under gdb single-step and `CF_NO_GOTO_TB|1` under one-insn-per-tb; `cpu_single_step` does **not** flush (TCG has no `update_guest_debug`); `tcg_set_one_insn_per_tb` only sets a global. | cpu-exec-common.c:38-58; cpu-target.c:37-51; tcg-all.c:256-262 | V |
| G11 | Breakpoint insertion only edits `cpu->breakpoints` (no flush). A gdb stop in `RUN_STATE_DEBUG` caused by a breakpoint or single-step does `tb_flush`; a watchpoint-hit stop jumps to `send_packet` before the flush, and a Ctrl-C/`interrupt` stop (`RUN_STATE_PAUSED`) does **not** flush. Breakpoints persist across `continue`. Watchpoint insert/remove call `tlb_flush_page`/`tlb_flush`. [m-SF6] | cpu-common.c:388-415; gdbstub/system.c:165-183 (watchpoint `goto send_packet` :171, flush :176-178, PAUSED :180-182); system/watchpoint.c:50-58, 87 | V |
| G12 | Every vCPU TLB flush funnels through three workers: full `tlb_flush_by_mmuidx_async_work` (jmp-cache flush at :395), page `tlb_flush_page_by_mmuidx_async_0` (:532-556), range `tlb_flush_range_by_mmuidx_async_0` (:722-758); `tlb_flush_range_by_mmuidx` and `tlb_flush_page_bits_by_mmuidx` devolve to these. Memory-map commits call `tlb_flush` (`tcg_commit_cpu`); NVIC MPU writes call `tlb_flush` at 7 sites. | cputlb.c:371-400, 520-616, 720-800; physmem.c:2757-2762; armv7m_nvic.c:1842-2023 | V |
| G13 | `can_do_io=false` is emitted before the first `insn_start` (TBs with >1 insn) and `can_do_io=true` before the last `insn_start`, **unconditionally**; `io_prepare` calls `cpu_io_recompile` whenever `can_do_io` is false, also unconditionally. MMIO can complete only in a TB's last guest instruction. | translator.c:209-221; cputlb.c:1274-1285 | V |
| G14 | TB entry: `ld icount_decr.u32`, (icount) `sub n`, `brcond LT 0 → exitreq` unless `CF_NOIRQ`, (icount) `st16`; the exit path `exit_tb(tb, TB_EXIT_REQUESTED)` is emitted at the end. | translator.c:43-102 | V |
| G15 | `cpu_tb_exec` sets `can_do_io = true` after every TB return; `cpu_loop_exit` too. | cpu-exec.c:451-452; cpu-exec-common.c:67-74 | V |
| G16 | Unwind: `icount_decr.low += insns_left` under `CF_USE_ICOUNT`, then `restore_state_to_opc` (PCREL page-relative PC, condexec, and `exception.syndrome` from `data[2]`, target/arm/cpu.c:116-121, V-c [m-SF14]). `cpu_io_recompile` needs `tcg_tb_lookup(retaddr)`, restores, sets `cflags_next_tb = curr | CF_MEMI_ONLY | CF_NOIRQ | n` and exits. | translate-all.c:180-195, 576-612 | V |
| G17 | Lookup key on M-profile: `pc = regs[15]`, `flags = hflags.flags`, `cs_base = hflags.flags2` with THUMB/CONDEXEC (from `env->thumb`, `env->condexec_bits`) and the per-lookup M bits LSPACT, NEW_FP_CTXT_NEEDED, FPCCR_S_WRONG, MVE_NO_PRED ORed into a **local copy** only; PSTATE__SS likewise. These positions are marked "Not cached". | hflags.c:550-631; target/arm/cpu.h:2954-3018 | V |
| G18 | hflags are rebuilt by guest NVIC/SCS writes (`armv7m_nvic.c:2459-2463`), NVIC reset (:2651-2655), flag-0 helpers (`v7m_msr`, …), exception entry/exit and lazy stacking (target/arm/tcg/m_helper.c:590, 652, 980, 1904, 1977, 2177), CPU reset (target/arm/cpu.c:578), migration load (target/arm/machine.c:1021), and the inline `TCG_CALL_NO_RWG` helpers `rebuild_hflags_m32[_newel]` that the translator emits after MSR/CPS, which then end the TB with `gen_lookup_tb` (target/arm/tcg/translate.c:600-613, 4934, 7383). All run on the vCPU thread (I). The rebuild helpers write `env->hflags` (not deletable, §7.2) and no TCG global. [m-SF7] | as listed; armv7m_nvic.c:2450-2465, 2645-2656; map-arm-m-returns §2.3 | V / V-map / V-c |
| G19 | PCREL `gen_goto_tb` stores `R15 = R15 + diff` (relative); `gen_bx` stores `R15 = var & ~1` and `thumb = var & 1`. | translate.c:2785-2790, 992-999 | V |
| G20 | Executable vaddr aliases exist on mps2 (ssram1 at 0x0 aliased at 0x400000); the fixtures' `cortexm` machine maps flash once at 0 (its `flash_alias` is never initialised). | hw/arm/mps2.c:209-212; hw/arm/cortexm_soc.c:101-108 | V |
| G21 | DWARF offsets (relative to env = `%rbp`): `regs[15]` +0x3c, `thumb` +0x14d, `hflags.flags` +0x150, `hflags.flags2` +0x158, `condexec_bits` +0x228, `mpu_ctrl[0]` +0xc40; `CPUState.breakpoints` −0x2980, `watchpoints` −0x2970, `singlestep_enabled` −0x2aec; `plugin_mem_cbs` −0x20, `plugin_mem_value_low` −0x18, **`plugin_mem_value_high` −0x10**, `icount_decr` −0x8, `can_do_io` −0x4. | the maintainer's evaluation data (not in the repository) (gdb -nx, CPUState 11200 bytes, CPUNegativeOffsetState 10432) | V (re-read) |
| G22 | `plugin_mem_value_low` (and `_high` for i128) stores are emitted for every guest load/store of every TB translated while a plugin instruments translation (`tcg_ctx->plugin_insn != NULL`; FastDyn always does, which is why D3 is 2.39 %), and `qemu_plugin_vcpu_mem_cb` also writes both (plugins/core.c:627-628, V-c) [m-SF11]; they are read only by `qemu_plugin_mem_get_value`; FastDyn never calls that function; FastDyn registers memory callbacks only in two virtuals. | tcg-op-ldst.c:182-224; plugins/api.c:635-660; grep over `src/fastdyn` (variable_watch.c:146, object_sanitizer.c:215) | V |
| G23 | Non-faulting fetch probe exists: `probe_access_full(env, addr, 1, MMU_INST_FETCH, mmu_idx, nonfault=true, …)`; the ARM `tlb_fill` returns false without delivering a fault when probing. `get_page_addr_code` itself is **not** non-faulting (nonfault=false). The closest non-faulting twin of the lookup's fetch is `probe_access_full_mmu` (nonfault=true, retaddr=0, check_mem_cbs=false; cputlb.c:1434-1452), used by §4.6 [m-SF8]. | cputlb.c:1415-1432, 1541-1563; target/arm/tcg/tlb_helper.c (probe branch before `arm_deliver_fault`) | V |
| G24 | FastDyn helper threads (hw_session poll, twintrace replay IRQ, coverage tracer) only raise IRQs (`qemu_plugin_raise_irq` → `armv7m_nvic_set_pending` under the BQL) or read their own buffers; register writes (`qemu_plugin_set_register` → `gdb_write_register`) are called from virtuals/core callbacks. `nvic_irq_update` does not rebuild hflags. Further thread creators (grep): `virtuals/fuzzer/fuzz_aflnet.c:143`, `virtuals/physics/flight_controllers/ardupilot/ardupilot.c:94`, `…/ardupilot/gcs_receiver.c:322-323`; the ardupilot virtuals also call `qemu_set_register` (ardupilot.c:162-164, 227-229, 261-264, 289-291, V-c). These virtuals are excluded from the admitted configuration by name (AC4) until X-ASYNC clears them [m-SF10]. | fastdyn/device_models/common/hw_session.c:65-95, 190; fastdyn/device_models/twintrace/twintrace.c:204-250; fastdyn/core/core.c:974-993; plugins/api.c:734-749; hw/arm/armv7m.c:595-623; armv7m_nvic.c:474-491 | V for the threads found by grep; completeness I (§15 X-ASYNC) |
| G25 | SysTick SYST_CSR read clears COUNTFLAG (read side effect usable as the device-effect fixture). | hw/timer/armv7m_systick.c:77-81 | V |
| G26 | Workload facts: compute steady, 826.1 M TB entries; 15.52 % are lookup hits (128.2 M); of those 34.0 % returns, 48.0 % static off-page, 18.0 % one `tbb`; hottest-successor guard covers 70.8 % of returns (two targets ≈ 80 %); 77.8 % of returns come from a TB that does not write r0; ≈7.6 % of returns are `movs r0,#k; bx lr` into an r0 test. All records of both FreeRTOS snapshots are `CF_USE_ICOUNT`. | map-return-edge-profile §0, §4, §7; map-loader-runtime-v6 §8 | V-map |
| G27 | B2 PGO layout gave no measurable speedup (0.9988 [0.993,1.005], n=90, held-out); A/A noise ≈1 %. | the maintainer's evaluation report `B2-gate.md` (not in the repository) | V |
| G28 | B4 model (instruction counts, not time): dead flag/register stores inside one TB with pseudo sync points relaxed (D1) 2.16 % (469 static, 83 % in TB 0x1d5a); dead `can_do_io` stores (D2) 2.77 %, of which 35.6 % lie in the TB prefix; `plugin_mem_value_low` stores (D3) 2.39 %. | map-state-sync-traffic §7.3; feasibility review script `cdi_split.py` | V-map / V-j |
| G30 | TB-start (`PLUGIN_GEN_FROM_TB`) callbacks are emitted after the entry check and before the first `insn_start`; the `can_do_io` stores are inserted at `insn_start` positions, i.e. after them. On a direct chain (stock `goto_tb` or a guarded slot) a TB-start callback therefore sees the `can_do_io` value its predecessor left. `icount_handle_interrupt` calls `cpu_abort` when the vCPU raises an interrupt with `can_do_io == false`; `icount_get_raw_locked` exits on an icount read with it false. FastDyn lets virtuals register TB-translation hooks and raise IRQs on the vCPU thread. [R5] | translator.c:150-154, 205-221; tcg-accel-ops-icount.c:150-158; icount-common.c:104-114; fastdyn/virtuals/runtime_sdk.c:211-214; fastdyn/virtuals/virtuals.c:114, 165 | V |
| G31 | `-d int` (CPU_LOG_INT) on v7M logs the exception number and name, the loaded vector PC, the EXC_RETURN magic and fault addresses; **no line carries icount or the interrupted (stacked) PC**. FastDyn passes `log_options` to `-d`. [R1] | target/arm/helper.c:9918-9919; target/arm/tcg/m_helper.c:712, 1403 (V), 2260, 2314, 2342 (V-c); fastdyn/targets/qemu_target.py:617-624 (V-c) | V / V-c |
| G32 | `tcg_set_one_insn_per_tb` is a QOM property setter that only stores globals; at run time it is called by HMP `one-insn-per-tb` → `object_property_set_bool` on the **monitor thread** (BQL held), not on the vCPU. Stock code crosses threads for code changes only via `async_safe_run_on_cpu` (`tb_flush`). [R3] | accel/tcg/tcg-all.c:256-262; system/runstate-hmp-cmds.c:43-64; tb-maint.c:790-801 (V-c) | V |
| G33 | gdb/plugin writes to M-profile system registers (CONTROL, PRIMASK, BASEPRI, …) are a no-op in this build (`arm_gdb_set_m_systemreg` returns 0); a write of xPSR goes through `xpsr_write`, which sets `thumb`, `condexec_bits` and NZCVQ and does **not** rebuild hflags. So `qemu_plugin_set_register` can change the uncached key bits THUMB/CONDEXEC but not the cached `hflags`. [R6] | target/arm/gdbstub.c:112, 413-416; target/arm/cpu.h `xpsr_write` (:1560-1585) | V |
| G29 | Real return TB shape (compute, `prvOpIs` 0x1ede, `movs r0,#0; bx lr`): prefix 0..0x16 (icount check, `can_do_io=0`), r0/NF/ZF stores, `can_do_io=1`, R15 and `thumb` stores, the 11-byte tail `mov rdi,rbp; call [rip+d]; jmp rax` at 0x4c, exit-request stub, pool. | design C disassembly `dis-r0pairs.txt` | V |

---

## 1. Invariants (B3/B4 additions to B1-design.md §1)

1. **General TBs stay general.** Key lookup, interrupt return, indirect jumps and every unarmed or failed path reach only ordinary records. A specialized continuation `C'` is never in the QHT, never on a page list, and is reachable only through one loader-armed slot.
2. **Every direct transfer that replaces a lookup is guarded at run time** by the full lookup key (pc, thumb, condexec, cached hflags flags/flags2) and the breakpoint list. The mapping part of the key is covered by an arm-time fetch probe plus disarm on every TLB flush. Nothing about the key is assumed from translation-time data.
3. **No edit changes TB shape.** Every edit inside an existing instruction range is same-length; growth happens only after the pool. Search rows, `icount`, `cflags`, the entry check and the icount immediate are byte-identical to the source TB.
4. **No compensation code and no recovery fixups.** B4 deletes a store only if the validator shows the observable env is identical at every real sync point.
5. **Guest effects are compared, never inferred.** Every guest access, helper call, plugin callback and non-env host store of a changed unit is an event that `hostsym` compares; profiles never certify a fact.
6. **Everything ships on one binary.** All arms (including warm and identity) run on the frozen gate binary `qint-v6@<sha>` (§5.9) [R13]; a v6 artifact is never interpreted by a v5 loader (it is rejected and the post-run proof marks the run INVALID).
7. **Fail closed offline, fail loud online.** An unsupported or undecided candidate keeps the baseline with a stable reason code; a malformed v6 extension makes QEMU exit before any guest instruction runs.

---

## 2. Mechanism overview

```
 R  (return TB "… ; [r0<-0] ; bx lr", ordinary record, grown)        C' (variant: relocated clone of C,
   prefix, body bytes unchanged (R15:=LR&~1, thumb:=LR&1 kept)          same-length patches only, own TB struct,
   T: jmp S ; int3 x6          <- was the 11-byte lookup tail           identical rows/icount/cflags; entered only
   … exit-request stub, LDST slow paths, pool (unchanged) …             through R's armed SPEC slot)
   S: cmp [bp-list],0 ; cmp thumb,1 ; cmp condexec,0 ;                +0 entry check (kept)  -> exit_tb(C'|3)
      cmp hflags.flags,Kf ; cmp hflags.flags2,Kf2   --fail--> F         … `cbz r0` folded: test/jcc/load nop'ed
      cmp R15,Vc1 -> L1 ; cmp R15,Vc2 -> L2 ; jmp F                     exits: C' own slots (stock-chained)
   L1: jmp rel32 [slot a, SPEC]      reset: jmp F        --> loader arms a -> C'.tc.ptr (on C's jmp list)
   L2: jmp rel32 [slot b, GENERAL]   reset: exit_tb(R|b) --> main loop links b -> general C2 after a lookup
   F:  mov rdi,rbp ; call [same pool slot] ; jmp rax        (the original tail: exactly baseline)

 C  (general continuation) <-- every lookup, interrupt return, alternate caller, unseen path, failed guard
```

| Primitive | Removes | Needs | File |
|---|---|---|---|
| GENERAL guarded slot (`chain-returns`, `chain-static`) | the lookup helper on a guarded edge to a general TB | grown unit; v6 loader for the TLB/flush hooks | v6 |
| SPEC guarded slot + variant (`specialize-returns`) | the lookup, plus the work folded in `C'` | v6 ext table, loader arming | v6 |
| CP / BF / DHO on variants | env loads, decided branches, dead flag computations in `C'` | certified return-edge facts | v6 |
| B4 same-length nops | dead env stores (D1/D2/D3) | `hostsym` equal-env proof | v5 (if no B3 edit) or v6 |

The v5 guarded-slot artifact (no loader change) exists **only** as the experimental value probe (WP16, `experimental_unsound_without_v6_tlb_hook`) and in slice 0b (WP27, together with an in-place patch of C labelled `experimental_unsound_in_place_specialization`); neither is ever a shipped artifact (§16 D-B3-3). Each such run needs an untimed diag twin showing 0 `tlb_flush*`/`tcg_commit_cpu` calls after `benchmark_start` [m-CE7].

---

## 3. B3 scope v1

### 3.1 Which edges qualify

An exit `E` of unit `R` is **admissible** for a guarded slot if all hold (reason code on failure):
1. `E` is a `GOTO_PTR` site preceded by `HELPER_CALL(lookup_tb_ptr)` and `mov rdi,rbp`, i.e. the 11-byte tail (G29) (`exit_not_goto_ptr`). No captured LABEL targets an offset inside `(T, T+11)` (`tail_is_branch_target`).
2. Exit class, from Thumb decode of R's guest bytes (`scopt/decode/thumb.py`) plus OpMarks:
   - **return**: last insn `bx lr`, `pop {…,pc}`, `ldm sp!,{…,pc}`, `ldr pc,[sp],#k` (pass `chain-returns`, `specialize-returns`); `ldr pc,[rX…]` / `ldm rX,{…,pc}` with a base other than SP is jump-table or vector dispatch and is `indirect_multiway` [m-CE8];
   - **static**: `bl`, `b`, `b<cond>` taken arm or page-end fallthrough whose destination is off the 1 KiB page (pass `chain-static`);
   - anything else: `indirect_multiway` (`tbb/tbh`, `bx rX`, `blx rX`) is out of v1 (§14).
3. `R.key.cs_base & HANDLER == 0` (`handler_mode_return`); R not a PC-writing FastDyn modifier exit (`pc_modifier_exit`).
4. `R.cflags & (CF_COUNT_MASK|CF_NOIRQ|CF_MEMI_ONLY) == 0` (`special_cflags_unit`); R `ok_relocatable`, not `ram_resident`, not `no_meta`, not pinned (`unit_not_relocatable`, `ram_resident`).
5. R has a free registered slot `n` (`jmp_insn_offset[n] == 0xffff`) (`no_free_slot`); return TBs and off-page `bl` TBs have both slots free (V-map, map-arm-m-returns §7.1).
6. Instructions that must return to the execution loop (MSR/CPS and every other `DISAS_EXIT` instruction end with `gen_lookup_tb` → `exit_tb(NULL,0)`; translate.c:1353-1357, 4932-4935, 7383-7384, 8316-8321, V-c) leave no lookup tail, so rule 1 (`exit_not_goto_ptr`) already excludes them; V13 also asserts that no admitted R ends in a DISAS_EXIT-class instruction (spec:64: "returning to the execution loop after state changes that can unmask interrupts") [m-SP7].

A **continuation candidate** `C` for `(R, E)`:
- an observed `lookup_hit` edge `R → C` in an exact steady-phase profile of the **base** snapshot (`target_not_profiled`);
- `C.flags == R.flags`, `C.cs_base` equals `R.cs_base` except THUMB (must be 1) and CONDEXEC (must be 0), `C.cflags == R.cflags` (`continuation_key_mismatch`);
- `C.page_addr1 == -1` (`two_page_target`); C relocatable, not `ram_resident`;
- `Vc` = the profiled destination vaddr (Thumb bit clear).
- **Variants are per (R unit, exit, C unit, fact set), and each has exactly one predecessor slot (V17)** [R4]. Suffix-TB duplicates of one return instruction are separate R units: icount budget suffixes and shared epilogues entered past the defining instruction (compute has 132 return units for 50 return addresses, map-return-edge-profile.md:52). Each R unit is certified on its own bytes by V14. A suffix that does not contain the defining instruction gets `fact_not_local_to_return_tb` and at most a GENERAL slot. The grouping by (return-insn vaddr, continuation key) of map-return-edge-profile §9.6 is used **only to aggregate train weight for ranking**, never to share a variant. (Sharing would let an IRQ taken at the budget boundary between `movs r0,#1` and `bx lr` rewrite stacked r0 and then enter a C' that assumes r0 == 1; F-B3-20.)

At most **two guarded targets per exit** (two slots); edges are ranked by train-profile weight; at most two variants per C, 64 per artifact (`slot_budget`, `context_budget`). The profile only selects and ranks.

### 3.2 Return-edge fact

**Definition.** `F = (R, E, field, value, def_site, source, cert)`: on every execution of `R` that reaches the guarded slot of exit `E`, `env[field] == value` (32-bit), where `field ∈ {r0..r12, NF, ZF, CF, VF}`. The fact attaches to the exit (all targets); the edge decides where it is used.

**Admissibility** (each checked by V14; rejection code in brackets):
1. The defining store is in `R` after its prefix: a FastDyn modifier op or a guest instruction of R (`fact_not_local_to_return_tb`). *Corrected rationale* (soundness review C-g4): crossing a link-entered entry check is sound, because an entry-check exit always returns to the main loop and then to general TBs (G14, G7). The real limit is that a fact established in an earlier TB `X` holds at R's exit only for executions that entered R from X, so it needs a version `R'` of R reachable only from X: chains of versions are v2 (§14).
2. On every host path from the definition to `E`'s slot, no store to `field` with a non-constant value and no call to a helper whose flags lack `TCG_CALL_NO_WRITE_GLOBALS`; FastDyn virtuals are flags-0 calls (`fact_killed_by_helper`, `fact_not_constant`).
3. Profiles never create facts; a fact with only profile evidence is `fact_from_profile_only` (spec:118, 128).

**Evidence and certification** (two independent derivations, both on R's captured bytes and Tier-0 metadata):
- *Proposer* `exit-facts@1`: forward must-constant dataflow over HostOps (§8.1) of R (A's algorithm, kills at non-`NO_WG` calls, plugin callbacks, unclassified stores).
- *Certifier* V14: `hostsym` symbolically executes R' and shows `env[field]` is the constant term on every feasible path ending in the guarded slot (C's rule). Disagreement between the two is `failed:fact_derivation_mismatch` (a tool bug, never a silent accept).
- *Provenance* `patch-join@1`: the defining store's insn index maps to a guest vaddr (`page_base(vaddr(R)) | words[0]`); a modifier `(vaddr, field <- k)` in the patch-plan content (hashed into `patch_plan_hash`) with equal value gives `source=patch`; a Thumb decode writing `field` with an immediate gives `source=guest_code`; disagreement is `patch_join_mismatch`, no explanation is `provenance_unexplained`. When Tier-1 PLUGIN_CBS is captured (WP23), the join is cross-checked against it (`ops_bytes_mismatch` on disagreement).

Example (V, G29): unit 0x1ede stores `r0 = 0` at host 0x2a with no call before the tail; the C prototype certified `r0 == 0` (design C prototype results, T4); source = guest_code.

### 3.3 CalleeSummary v1 (spec:116 "summary of the callee and its patches together")

`summaries/callee_summaries.jsonl`, schema `fastdyn.callee_summary` v1, one row per return unit, exported even when no edge is specialized:

| Field | Content |
|---|---|
| `callee` | optional ELF `STT_FUNC` symbol and range containing the return insn when a symbolized ELF is available (scopt resolves only host symbols today; WP20 adds a guest ELF symbol reader; stripped firmware gives `null`) [m-SP14]; `return_unit`, `exit_id` |
| `patches[]` | every patch-plan entry whose vaddr lies in the callee range (in R when the range is unknown), fact-defining or not (spec:116 "summary of the callee and its patches together") [m-SP14] |
| `facts[]` | `{field, value, def_host_off, def_insn, source: patch\|guest_code, plan_entry?, cert_hash}` |
| `effects[]` | ordered events of R from Tier-0 maps + HostOps: `guest_ld/st` (insn, MemOpIdx, faultable), `helper_call` (helper_id, flags), `plugin_cb`, `host_abs_store` (UPDATE_MEM/LOG_REG class), `exit` |
| `barriers[]` | the kills that limited facts (non-`NO_WG` calls, plugin callbacks, unclassified stores) |
| `deps` | `R.uid` (content hash, guest_hash, key), `patch_plan_hash`, `instrumentation_hash`, `contract_id`, helper ids+flags, `envlayout_id`, Tier-1 `ops_hash` if present |

Every FactRef carries the summary row hash; the manager rejects a stale one (`stale_fact`).

### 3.4 Continuation versions

A **variant** `C'[E, F]` is a clone of `C` that may assume the facts `F` of `R`'s exit `E` at `C'`'s first instruction.
- **Bytes:** `C`'s `[tc, end)` relocated by NativeLinker v2 to a fresh 64-aligned arena offset (own-struct LEAs rebased to `C'`'s struct, rel32 to the prologue/epilogue and helpers, RIP-relative pool references; the B2 fixup rules), then **same-length** patches from CP/BF/DHO. Search rows, `icount`, `cflags`, `guest_size`, key: copied from `C`'s record at load (never stored separately). V16 checks "identical modulo relocation" (soundness review A5 resolved).
- **Entry:** only through R's SPEC slot (§5.5). Interrupt return, alternate callers, unseen paths and failed guards reach the general `C` (spec:116).
- **Fact lifetime in `C'`:** facts hold at `C'.insn0_start`; the only code before is `C'`'s entry check, which writes `icount_decr` and may exit to the main loop (after which re-entry is by lookup, i.e. general `C`). `hconst@1` carries them forward until a kill: store to the field, call without `NO_WG`, plugin callback, unclassified store, any exit. `C'` has no interrupt opportunity after its entry check (G14).
- **Identity:** `variant_id = sha256(C.uid, R.uid, E, sorted(F), patch set)`.
- **Budgets** (policy, spec:112): `max_context_versions` = 2 per C (R units that share a return instruction compete for them by their own train weight) and 64 per artifact; `max_code_growth_bytes` = 10 % of the 99 %-coverage hot-set bytes (≈10.8 KB on compute, map-return-edge-profile §6.2); `max_region_members = 2` (R, C').
- A variant is created only if `hconst` predicts at least one decided branch or foldable load in `C` under `F` (`fact_unused`); after the simplification passes, a variant whose bytes equal the relocated `C` is downgraded by the manager to a GENERAL slot (`no_simplification`).

### 3.5 Analyses, passes and order

```
analyses: envlayout@1 hostops@1 hcfg@1 effects@1 obs-contract@1 return-edges@1 exit-facts@1 patch-join@1
          callee-summary@1 hconst@1 hlive@1 syncdead@1 [ops-xcheck@1 when Tier 1 is captured]
pipeline b3 (each pass: --only/--disable, own ledger rows and outcome):
   1 specialize-returns@1     CLONE C->C', REDIRECT_EXIT(R.slot, SPEC), ADD_ENTRY, ATTACH_FACT   (claims slots first)
   2 constant-propagation@1   PURE_REWRITE on variants: env load of a fact field -> xor/imm form
   3 branch-folding@1         PURE_REWRITE on variants: hconst-decided jcc -> jmp / nop
   4 dead-host-ops@1          PURE_REWRITE on variants: dead register-only ALU/test/cmp/setcc -> nop (never a memory operand)
   5 chain-returns@1          REDIRECT_EXIT(R.slot, GENERAL) on remaining slots of return exits
   6 chain-static@1           REDIRECT_EXIT(R.slot, GENERAL) on static off-page exits (separate arm, §10)
pipeline b4:
   7 eliminate-state-sync@1   PURE_REWRITE nops of dead env stores (D1/D2/D3), any unit incl. variants
then (combined arm only) B2 hotcold-partition, cluster-hot, order-cold, unchanged
then materialize (NativeLinker v2) -> ValidateAndPackage (V1-V10 of B1 §5.8 + V11-V22, §9.1)
```

- **Separation of CP/BF/DHO** (spec:116, 157): all three read the *analysis* `hconst@1` (constant lattice per host insn of the variant under the entry facts). CP rewrites `mov r32,[rbp+d]` of a fact field to `xor r32,r32` + nop when the value is 0 and EFLAGS are dead there, else `no_same_length_encoding`; BF rewrites a decided `jcc rel8/rel32` to `jmp rel8/rel32` (+nop) or a same-length nop; DHO nops straight-line host instructions with **no memory operand at all** (register-only ALU, `test`/`cmp`, `setcc`, `mov reg,reg|imm`) whose results, EFLAGS included, are dead by `hlive@1` (host register and flag liveness on the patched variant), never inside the prefix, an LDST block, an exit stub or the pool. An env load is removed only by CP (replaced by an immediate form), so the three passes have disjoint effects [m-SP13]. Disabling any one leaves the others effective, so each has its own byte delta and ledger row. Expected F-B3-1 deltas (I; WP27 slice 0a confirms): CP turns the r0 load `mov eax,[rbp+0]` into `xor eax,eax` + nop; BF turns the `jcc` after `test eax,eax` into `jmp` (or nop); DHO nops the then-dead `test` and `xor`. With CP off the load stays; with BF off the `jcc` and `test` stay; with DHO off the `test` stays.
- **Analysis dependencies** (spec:107): every analysis declares the analyses and identities it reads, hashed into its cache key: `hostops@1` ← snapshot uid, EnvLayout id; `hcfg@1` ← hostops; `hconst@1` ← hostops, hcfg, FactRefs; `hlive@1` ← hostops, hcfg; `syncdead@1` ← hostops, hcfg, obs-contract (+ RuntimeIdentity facts of V20) [m-SP10c].
- **Slot arbitration:** pipeline order (pass 1 before 5/6). Ablations are therefore not purely additive; the report says so (feasibility review).
- Manager changes: accept `unit_scope = "region"` (refused today at `scopt/manager.py:661-662`, V), allow later passes to edit units created by earlier passes (clones), downgrade rule above, enforce the new budgets.

### 3.6 Pass descriptors (`scopt/passes/base.py:59-74` type)

All `domain="HostCache"`, `requires_capture_level=1` (Tier 0); `ops-xcheck@1` is consumed when present, never required.

| name@ver | scope | requires | capabilities | budgets (defaults) | preserves | invalidates |
|---|---|---|---|---|---|---|
| `specialize-returns@1` | snapshot | `hostops, hcfg, return-edges, exit-facts, patch-join, hconst, envlayout, profile.counts` (steady, match ≥0.95) | CLONE, REDIRECT_EXIT, ADD_ENTRY, ATTACH_FACT | 2 variants/C, 64 total, growth 10 % hot set, `max_region_members=2`, `max_wall_s=120` | `profile.counts, callee-summary` | code, meta, layout |
| `constant-propagation@1` | region (variant) | `hconst, hostops` | PURE_REWRITE | 64 patches/variant | `exit-facts, profile.counts` | code (variant) |
| `branch-folding@1` | region | `hconst, hostops, hcfg` | PURE_REWRITE | same | same | code |
| `dead-host-ops@1` | region | `hostops, hcfg, hconst, hlive` | PURE_REWRITE | same | same | code |
| `chain-returns@1` | snapshot | `hostops, return-edges, envlayout, profile.counts` | REDIRECT_EXIT | ≤2 slots/unit, 256 units | `profile.counts, cfg.known_edges` | code, meta, layout |
| `chain-static@1` | snapshot | same | REDIRECT_EXIT | same | same | same |
| `eliminate-state-sync@1` | unit | `hostops, hcfg, effects, syncdead, obs-contract, envlayout` | PURE_REWRITE | 256 candidate stores/unit, 256 paths/unit, `max_wall_s=300` | `profile.counts`; `exit-facts` only if no fact-defining store is touched | code |

### 3.7 EditKind payloads (replace the stubs at `scopt/passes/base.py:26-38` and `scopt/proposal.py:355-387`)

| Kind | Payload (frozen) | Manager preconditions |
|---|---|---|
| `REDIRECT_EXIT` (11) | `GuardedSlot(uid, exit_id, slot_n, kind: GENERAL\|SPEC, exit_class, target_vaddr, key_C: TBKey, guard_template_id, variant_ref?)` | exit admissible (3.1); slot free; EnvLayout bound to the snapshot's RuntimeIdentity; SPEC requires a CLONE+ADD_ENTRY in the same proposal |
| `CLONE` (10) | `Clone(src_uid, variant_id, facts: [FactRef])` | src relocatable, `meta_status == ok`; budgets |
| `ADD_ENTRY` (12) | `SpecEntry(variant_id, pred=(uid, slot_n), precondition={guard atoms, facts})` | exactly one pred per variant |
| `ATTACH_FACT` (16) | `FactRef(fact_id, summary_row_hash, cert_hash)` | fact certified and current, else `stale_fact` |
| `PURE_REWRITE` (13) | `Patch(uid\|variant_id, host_off, old_bytes, new_bytes, template_id, justification)` | `len(old)==len(new)`; old matches; template allowed for the pass; placement rules of V15 |
| `ADD_REGION` (14) | manager-generated `multi_tb` CacheRegion row per (R, SPEC slot, C') link; field mapping in §3.9 (not used by passes) [R19] | — |
| `ADD_RECOVERY` (15) | not used in v1 | `capability_not_enabled` |

Every payload also carries `origins` (guest vaddrs and insn indices of the affected units, for attribution) and `deps_added` (FactRefs, summary row hashes, variant ids); the manager records in the ledger row the fixups and maps it derived (relocations, copied rows, slot offsets) [m-SP10a]. Candidate generation is the pass's `propose` step of B1 §5 and needs no edit kind.

### 3.8 B3 reason codes (stable; each gives `skipped_unsupported` or `skipped_unknown` and keeps the baseline)

| Code | Meaning |
|---|---|
| `exit_not_goto_ptr`, `tail_is_branch_target`, `handler_mode_return`, `pc_modifier_exit`, `indirect_multiway` | exit admission |
| `special_cflags_unit`, `two_page_target`, `ram_resident`, `no_meta`, `unit_not_relocatable`, `no_free_slot` | unit admission |
| `target_not_profiled`, `continuation_key_mismatch`, `continuation_not_in_snapshot` | edge admission |
| `fact_not_constant`, `fact_killed_by_helper`, `fact_not_local_to_return_tb`, `fact_from_profile_only`, `patch_join_mismatch`, `provenance_unexplained` | facts |
| `fact_unused`, `no_simplification`, `no_same_length_encoding`, `flags_live` | simplification |
| `slot_budget`, `context_budget`, `growth_budget`, `path_budget_exhausted` | policy (spec:112) |
| `edit_in_prefix`, `edit_inside_ldst_block`, `edit_in_exit_stub`, `edit_on_insn_boundary` | placement |
| `guard_layout_unbound`, `unsupported_guest` | runtime identity (EnvLayout or CPU model not admitted) |
| `validation_inconclusive` | `hostsym` met an unsupported instruction/path or a budget (skipped_unknown) |
| `validation_mismatch`, `fact_derivation_mismatch`, `guard_contract_violation` | **failed**: a candidate bug, never reported as unsupported (spec:47) |
| `not_profitable` | training profitability (§10.3) |
| `unsupported_contract_fact_source`, `no_patch_bindings` | the defining store comes from a B1 §9 `unsupported_contract` patch (detour, gated modifier); or patch-join on a Student 1 (optimized) image without `patch_bindings`/origin map [m-SP15] |
| `observer_contract_unknown` | the RuntimeIdentity lacks a fact the observation contract needs (`tb_start_cb_possible`, `qemu_plugin_mem_get_value` use) |

### 3.9 CacheRegion `multi_tb` row for one SPEC link [R19]

One row per (R, SPEC slot n, C') link, written by `scopt/formats/region_v1.py` (WP18) and validated against `cache_region.v1.json`, whose `kind` enum already contains `multi_tb`; WP18 completes the schema's multi_tb branch and types the arrays that are untyped today. GENERAL slots create no multi_tb row: R's own `tb_singleton` row marks the exit `guarded_slot` with its target. The general C keeps its own singleton row.

| Field (cache_region.v1) | Content for the (R, n, C') row |
|---|---|
| `kind` | `multi_tb` |
| `members[]` | `[0]` = grown R (unit uid after the edit, key, guest size/icount/hash/vaddrs, origin `{snapshot_id, slot, tb_offset}`); `[1]` = C' (`unit` = `variant_id`, key/guest fields copied from C, origin `{snapshot_id, slot: null, tb_offset: variant tb_offset}`, `x_variant_of: C.uid`) |
| `entries[]` | `{e0, general, member 0, host_offset 0, precondition {full_key: true, extra_guards: []}}`; `{e1, specialized, member 1, host_offset 0, precondition {full_key: false, extra_guards: [GuardAtom…], facts: [FactRef…]}, x_pred: {member: 0, slot: n}}` |
| `GuardAtom` (item schema) | `{atom: g_bp\|g_thumb\|g_cond\|g_flags\|g_flags2\|g_pc, env_field: <EnvLayout name>, width: 1\|4\|8, op: eq\|zero, value: hex}` |
| `FactRef` (item schema of `precondition.facts` and `dependencies.facts`) | `{fact_id, field, value, def_insn, source: patch\|guest_code, cert_hash, summary_row_hash}` |
| `exits[]` | R's exits with exit `n` of kind `guarded_slot` (`slot: n`, `slot_kind: SPEC`, `target_vaddr: Vc`, `guard_off`, `guard_len`, `reset: jmp_F`, `patchable: {insn_offset, reset_offset}`); C''s exits = C's (member 1), goto_tb slots patchable |
| `native` | `native.members[i]`: the tb_singleton `native` object per member (code sha256 of the member bytes, lengths, insn0_start, body_end, ldst_end, pool_start, search_len, struct offset); for R also `stub_start` |
| `relocations[]` | both members' relocations with `member`; C''s are C's rebased (`rule: variant_relocated`) |
| `maps.insn`, `maps.events` | member 0 = R's rows and events; member 1 = C's rows (byte-identical, same host offsets because patches are same-length) and C's events (DHO/CP/BF remove no event) |
| `recovery[]` | R's points (member 0) and C's points under member 1 at the same offsets; `state_fixups: []` always (v1, §6.5) |
| `bindings` | union of both members; C''s `unchecked_host_pointers` are C's, with `member: 1`; V16 checks each embedded pointer of C' equals C's binding [m-SP15] |
| `dependencies` | `guest_pages` of R and C; `units: [R.uid, R_orig.uid, C.uid]`; `facts: [FactRef…]`; `patch_plan_hash`; `instrumentation_hash`; `x_summary_row_hash`; `x_envlayout_id`; `x_runtime_exe_sha256` (gate binary) |
| `status`, `pin_reasons` | `ok_relocatable` (NativeLinker v2 rewrites the ext table when it moves either member; V11 rechecks) |
| `unknowns` | `profile:unseen_targets` (exit n has targets outside the guarded set), as for any exit |
| `provenance` | `passes`: `specialize-returns@1` plus each of CP/BF/DHO that changed C'; `source_units: [R_orig.uid, C.uid]` |

V19 checks that every armed SPEC link in the ext table has exactly one such row, with current dependency hashes, and that the graph round trip (T-B3-28) reproduces the `guarded_slot` and `variant_link` edges from these rows.

---

## 4. Guards and fallbacks

### 4.1 The guard (exact host form, emitted in stub `S`)

Placed after all of R's guest instructions; R's own stores of `regs[15]`, `thumb` (returns) or `regs[15]` (static) precede it on every path (V13 checks). Offsets from G21, bound to the RuntimeIdentity through `EnvLayout`.

```
S:  cmp  qword [rbp-0x2980], 0          ; g_bp:    cpu->breakpoints.tqh_first == NULL
    jne  F
    cmp  byte  [rbp+0x14d], 1           ; g_thumb: env->thumb == 1 (key THUMB of every admitted C)
    jne  F
    cmp  dword [rbp+0x228], 0           ; g_cond:  env->condexec_bits == 0 (key CONDEXEC of every admitted C)
    jne  F
    cmp  dword [rbp+0x150], Kf          ; g_flags: hflags.flags == K.flags & ~PSTATE__SS
    jne  F
    cmp  qword [rbp+0x158], Kf2         ; g_flags2: hflags.flags2 == K.cs_base & ~NOTCACHED2 (imm32 sign-extended;
    jne  F                              ;           mov rax,imm64 ; cmp qword [rbp+0x158],rax if Kf2 >= 2^31)
    cmp  dword [rbp+0x3c], Vc1          ; g_pc:    regs[15] == Vc1   (one compare per target, ≤2)
    je   L1
    cmp  dword [rbp+0x3c], Vc2
    je   L2
    jmp  F
L1: [nop pad: rel32 field 4-aligned] jmp rel32 0      ; slot a; jmp_insn_offset[a] = the rel32 field
    <reset path a>                                     ; jmp_reset_offset[a] = this instruction
L2: [nop pad] jmp rel32 0 ; <reset path b>
F:  mov rdi, rbp ; call [rip+d'] ; jmp rax             ; the original tail, same pool slot (d' re-resolved)
reset path GENERAL: lea rax,[rip→R_struct+n] ; jmp tb_ret_addr       (exit_tb(R|n): main loop may link it)
reset path SPEC:    jmp F                                              (the main loop never sees R|n)
```

- `NOTCACHED2` = THUMB(23), CONDEXEC(24-31), LSPACT(2), NEW_FP_CTXT_NEEDED(3), FPCCR_S_WRONG(4), MVE_NO_PRED(5) of `cs_base`; `PSTATE__SS` (bit 2) of `flags` (G17). Admission additionally requires `K.flags` SS_ACTIVE = 0 and K CONDEXEC = 0.
- The tail bytes `[T, T+11)` become `jmp rel32 S` + 6×`int3` (a stray jump into them traps loudly).
- Size ≈ 55 bytes of compares + ≈ 20 per target + 11 for F ≈ 90-120 bytes per redirected exit (I).
- Static exits use the same guard with `Vc` = the static destination; g_pc is still required (PCREL aliasing, G19/G20; soundness review must-fix 6).

### 4.2 Guard pass ⇒ the lookup would return the linked TB (component table)

| Lookup component (G7, G17) | Discharged by | Mark |
|---|---|---|
| `pc = regs[15]` | g_pc (absolute compare; aliases fail it) | V |
| THUMB, CONDEXEC (not cached) | g_thumb, g_cond | V |
| cached `flags`, `flags2` | g_flags, g_flags2 (read the same `env->hflags` the lookup reads) | V |
| per-lookup M bits LSPACT, NEW_FP_CTXT_NEEDED, FPCCR_S_WRONG, MVE_NO_PRED | constant 0 on cortex-m3 (no FPU/LOB/MVE/security); V20 admits only RuntimeIdentity CPU model `cortex-m3`, else `unsupported_guest` | V-j (C 4.1 A1) |
| `cflags = curr_cflags` | normal-cflags TBs are never entered from the main loop or the lookup while single-step / one-insn-per-tb are on (G10); the loader refuses to arm and disarms all when one-insn-per-tb is set; HMP `log nochain` at run time is outside the admitted configuration (AC2) — stock same-page chains have the same exposure | V / I |
| breakpoints (`check_for_breakpoints`, incl. the BP-page cflags variant) | g_bp: list empty ⇒ the check is a no-op (cpu-exec.c:364-369) | V |
| `page_addr0` / execute permission of `Vc` | arm-time non-faulting probe `probe_access_full_mmu` with the lookup's own MMU index (G23, §4.6) for SPEC links; for GENERAL slots the main loop's own lookup (with `get_page_addr_code`) chose the target; every TLB flush unlinks every guarded slot (§5.6) | V / I |
| target valid and current | SPEC: armed only when both records activated this flush epoch (hash-checked); GENERAL: stock `tb_add_jump` refuses `CF_INVALID` | V |
| `cflags_next_tb == -1` | always −1 when generated code reaches an exit: all writers exit the loop or run in the main loop (cpu-exec.c:713, 934, 966; translate-all.c:609; watchpoint.c:104,134; tb-maint.c:1100,1172) | V (writer list) / I |

### 4.3 Deliberately not checked

| Not checked | Reason |
|---|---|
| pending interrupt, icount budget | the transfer always enters `tc.ptr + 0` of C or C', whose entry check does what it does after a lookup (G14) |
| `singlestep_enabled` | G10: no normal-cflags TB runs while it is set; the arm step refuses when it is set (feasibility review: redundant) |
| watchpoints | the lookup does not check them; insert/remove flush the TLB (G11) which disarms every link; the arm step refuses while the list is non-empty |
| MPU enable bit (A's g2) | replaced by the TLB-flush hook: every MPU write flushes the TLB (G12) |

### 4.4 Guard failure and unarmed slots

- **Guard false:** jump to `F`, byte-equal to the original tail apart from the re-resolved pool displacement: baseline behaviour from that point. The guard reads env only and writes only EFLAGS (and `rax` in the imm64 form); nothing is live in host registers at an exit (all globals dead and synced; tcg.c liveness at exits, V-map).
- **GENERAL slot, not linked:** `exit_tb(R|n)` → main loop: interrupt delivery, key lookup (may translate), `tb_add_jump(R, n, found)` (G8). Sound because the slot's target key is fixed by the guard, exactly as a stock `goto_tb`'s key is fixed by translation.
- **SPEC slot, not armed or disarmed:** reset path `jmp F` = original lookup; stock code can never link it (it never sees `R|n`).

### 4.5 Why effects are never duplicated or skipped

- The guard follows every effect of R and precedes every instruction of the successor; it has no stores, calls or guest accesses (V13). Pass or fail, R's effects happened exactly once and exactly one TB is entered next at its `tc.ptr`.
- The only code bypassed on a pass is the lookup helper. Its effects: `can_do_io = true` (already true: set before R's last insn, G13, and rewritten by the successor's prefix); the breakpoint check (g_bp); the key (4.2); the fetch translation (probe + TLB hook); the profile hook (never active in production; profile runs never arm or chain); `-d exec` logging (lost exactly as on any chained edge).
- `C'` executes the same guest instructions as `C` (V12 compares every guest access, helper call and plugin callback). No region is ever restarted: a fault, MMIO recompile or interrupt inside `C'` leaves through stock paths with C's rows, and re-entry is always by lookup into general TBs (spec:120).

### 4.6 Arm-time conditions (SPEC links)

The loader refuses to arm (counted per reason in the stats) unless: v6 extension valid and not forbidden; not profile mode, not flat; `one_insn_per_tb == 0`, `CPU_LOG_TB_NOCHAIN` off, `singlestep_enabled == 0`; **breakpoint and watchpoint lists empty** (soundness review must-fix 1); R and C live, neither `CF_INVALID`; `tb_page_addr1(C) == -1`; the fetch probe `probe_access_full_mmu(env, Vc, 1, MMU_INST_FETCH, FIELD_EX32(G->flags, TBFLAG_ANY, MMUIDX), &host, &full)` (non-faulting, retaddr 0, no memory callbacks: the lookup's `get_page_addr_code_hostp` minus the fault, G23) returns a non-NULL `host` with `full->lg_page_size >= TARGET_PAGE_BITS` and `qemu_ram_addr_from_host_nofail(host) == tb_page_addr0(C)`. For CF_PCREL, `page_addr0` is the full ram_addr of C's pc (e.g. 0x901ede for vaddr 0x1ede), not a page base. The MMU index is the lookup's: `cpu_mmu_index(cs, true)` → `arm_cpu_mmu_index` → `EX_TBFLAG_ANY(env->hflags, MMUIDX)` (include/accel/tcg/cpu-mmu-index.h:29-40; target/arm/cpu.c:126-129; target/arm/internals.h:48-51; V-c), and g_flags pins it at run time; U-MMU is closed [m-SF4, m-SF8]. The inline g_bp check still guards every execution: a breakpoint inserted while a link is armed (at a Ctrl-C/PAUSED gdb stop, which does not flush, G11) is honoured; F-B3-13(d) tests exactly this [R6].

---

## 5. Runtime and loader: `.sc` v6

### 5.1 Compatibility and fallback matrix

| Situation | Behaviour | Mark |
|---|---|---|
| v6 loader, v5 file | unchanged v5 path (all baseline arms keep v5 files on the same binary) | I (code path untouched) |
| v5 (old) loader, v6 file | `valid_header` fails → truncate + fresh v5 header + cold run; the ext bytes at 0x100 survive but are ignored. Deploy hands QEMU a copy; the post-run proof (B1 §4.8) marks the run `INVALID_artifact_not_used` (spec:169 satisfied, destructively) | V (G2) |
| v6 loader, v6 header with unknown or missing feature bits, or ext table malformed (magic, sizes, checksum, artifact id, index ranges, disjointness, code/guard hashes) | `error_report("supercache: invalid v6 extension: <reason>")`, `exit(EXIT_FAILURE)` before guest execution. Rationale: the loader cannot tell which records carry guarded slots without the table, and running them without the TLB hook is unsound; V11 makes a published artifact unable to trip it | design (B graft) |
| v6 loader, a member's guest hash mismatches at activation | that record is never live → no link from or to it is armed; SPEC slots of R stay on `F`; a GENERAL slot of a live R is chained by the main loop to whatever fresh translation the lookup finds (sound, 4.4) | V (G4) / I |
| v6 loader, another TB owns R's or C's key (`linked != tb`) | not live → no arming | V (G4) |
| v6 + flat mode or split-wx | refused (fail loud); flat is already outside B1 artifacts | design |
| profile mode (`QEMU_SC_PROFILE`) | never arms; main loop never chains (cpu-exec.c:1005) | V |

### 5.2 Header (80 bytes, the v5 struct, G1)

| Off | Field | v6 rule |
|---|---|---|
| 0-7 | magic `QSCACHE1` | same |
| 8 | version | **6** (new files created by the v6 binary are still version 5) |
| 12 | header_size | 80 |
| 16-67 | code_size, code_address, prologue_end, record_count, runtime_signature, host_page_size, target_page_bits, tb_size | same meaning |
| 68 | `reserved` → **feature_flags** (u32, covered by the header checksum) | bit0 EXT_TABLE (must be 1), bit1 VARIANTS, bit2 GUARDED_SLOTS, bit3 REWRITTEN_RECORDS (informational); any other bit set, or EXT_TABLE clear → fail loud. The check runs in `sc6_load_ext`, **not** in `valid_header`, because a `valid_header` failure truncates the file silently (G2) [R2] |
| 72 | checksum | FNV-1a-64 over [0,72) as in v5 |

Records keep the v5 104-byte layout, commit word and checksum. Grown and rewritten units are ordinary records (their code differs; their guest hash, key and rows do not).

### 5.3 Extension table at file offset 0x100 (inside the untouched [80, 0x10000) gap, G2)

**ExtHeader (64 B)**

| Off | Type | Field |
|---|---|---|
| 0 | u8[8] | magic `"QSCX6EXT"` |
| 8 | u32 | ext_version = 1 |
| 12 | u32 | ext_header_size = 64 |
| 16 | u32 | n_variants |
| 20 | u32 | n_slots |
| 24 | u32 | n_links |
| 28 | u16 | variant_entry_size = 64 |
| 30 | u16 | slot_entry_size = 32 |
| 32 | u16 | link_entry_size = 16 |
| 34 | u16 | reserved (0) |
| 36 | u32 | reserved (0) |
| 40 | u64 | variants_max_end (max `end_offset` over variants; 0 if none) |
| 48 | u8[8] | artifact_id8 (first 8 bytes of the artifact sha256; equals the sidecar) |
| 56 | u64 | checksum: FNV-1a-64 over ext bytes [0,56) followed by all three arrays |

Arrays start at 0x140 in the order variants, slots, links; `0x140 + 64·nv + 32·ns + 16·nl ≤ 0x10000` (else the packager rejects `ext_table_too_large`; ≈ 580 variant+slot+link triples).

**VariantEntry (64 B)**

| Off | Type | Field | Load-time rule |
|---|---|---|---|
| 0 | u64 | general_tb_offset | names a valid record (C) |
| 8 | u64 | tb_offset | 64-aligned; `tc_offset − tb_offset == ROUND_UP(tb_size, 64)` (192) |
| 16 | u64 | tc_offset | |
| 24 | u64 | tc_size | `== C.tc_size` |
| 32 | u64 | end_offset | `== tc_offset + (C.end_offset − C.tc_offset)`; **<** code_size − host_page − 1024 (strict: `after_prologue` aborts when `max_end >= highwater`, supercache.c:676-683) [m-SF9] |
| 40 | u64 | code_fnv64 | FNV-1a-64 of `[tc_offset, end_offset)` in the loaded arena (checked after the code `pread`) |
| 48 | u32 | variant_id | dense 0..nv−1 |
| 52 | u32 | flags | bit0 facts_present |
| 56 | u64 | reserved | 0 |

`[tb_offset, end_offset)` of every variant must be disjoint from every valid record's range and every other variant, and ≥ `ROUND_UP(prologue_end, 64)`.

**GuardedSlotEntry (32 B)**

| Off | Type | Field | Load-time rule |
|---|---|---|---|
| 0 | u64 | src_tb_offset | names a valid record (R) |
| 8 | u8 | slot_n (0/1) | R's `jmp_insn_offset[n] != 0xffff` |
| 9 | u8 | kind: 1 GENERAL, 2 SPEC | |
| 10 | u8 | exit_class: 1 return, 2 static | |
| 11 | u8 | reserved | |
| 12 | u32 | target_vaddr (Vc) | |
| 16 | u32 | guard_off (offset of `S` from R's `tc`) | `< R.tc_size` |
| 20 | u32 | guard_len (S through F inclusive) | |
| 24 | u64 | guard_fnv64 over R's code `[guard_off, guard_off+guard_len)` | checked after the `pread` |

**LinkEntry (16 B)**: `u32 slot_index` (a SPEC slot), `u32 variant_index`, `u64 reserved`. Every SPEC slot has exactly one link; every variant exactly one link (V17).

Key, icount, cflags, guest size and pages of a variant are copied from C's record when the struct is built, so they cannot disagree. Records are referenced by `tb_offset` (unique among valid records), never by entry index, because `index_record` order is not file-slot order (V-j A G12).

### 5.4 Load algorithm (v6 additions, all in `tcg/supercache.c`)

```
valid_header(h):            (version == 5 || version == 6) && the existing v5 checks (header_size 80, signature, …)    # [R2]
                            # nothing v6-specific here: a valid_header failure truncates silently (G2)
  v6 build only, on the !reuse path: if the old file had magic QSCACHE1 and version 6,
      warn_report("supercache: discarding v6 file: <failed check>") before the stock truncate   # v5 path unchanged
alloc_code_buffer (589-657), reuse branch, after load_records() and BEFORE "loaded_max_end = max_end":
  if version == 6:
     if flat or splitwx: die("v6 needs normal mode")
     sc6_load_ext():  die("unknown feature bits") unless (h.reserved & ~KNOWN_FEATURES) == 0 && (h.reserved & EXT_TABLE)
                      pread 0x100; check magic/version/sizes/checksum; build tb_offset -> entry map;
                      check every rule of 5.3 except the two code hashes; die(reason) on any failure
                      # die(r) = error_report("supercache: invalid v6 extension: %s", r); exit(EXIT_FAILURE)
                      supercache.max_end = MAX(max_end, variants_max_end, max over variants)   # review must-fix 2
after_prologue (659-713), after the code pread:
  if sc6.loaded: verify every code_fnv64 and guard_fnv64 against the arena; die on mismatch
```

`max_end` folding happens before `after_prologue` computes `cached_size` and `code_gen_ptr`, so variant code is loaded and never overwritten by fresh translations (G3; review must-fix 2). `max_end ≥ highwater` still aborts (supercache.c:678-681).

### 5.5 Activation and arming

```
lookup_entry (815-877), at the end, only when linked == tb:           # Q5
  sc6_on_activate(entry, tb):
     entry.live = tb
     for each slot s with s.src == entry: s.src_tb = tb                # GENERAL slots need nothing else
     for each link l whose slot.src == entry or whose variant.general == entry: sc6_try_arm(l)

sc6_try_arm(l):
  S = slots[l.slot]; V = variants[l.variant]; R = S.src_tb; G = entry(V.general).live
  if l.armed || l.disarmed_epoch || !R || !G: return
  if !sc6_arm_conditions(cpu, R, G, S.target_vaddr): stats.refused[reason]++; return     # 4.6, incl. probe
  Cp = sc6_build_variant(V, G):  struct at buffer+V.tb_offset; copy pc/cs_base/flags/cflags/size/icount/pages
       from G; tc = buffer+V.tc_offset, tc.size = V.tc_size; jmp offsets = G's; spin_init; lists and jmp_dest
       zero; tb_reset_jump on Cp's registered slots (offsets copied from G; G's own slots are never touched) [m-CE6]; flush_idcache_range; tcg_tb_insert(Cp)   # region tree only
  qemu_thread_jit_write(); lock(G->jmp_lock)
  if !(G->cflags & CF_INVALID) && cmpxchg(&R->jmp_dest[S.n], 0, G) == 0:
       tb_set_jmp_target(R, S.n, Cp->tc.ptr)                         # exported, internal-common.h:141 (tb_reset_jump at :53)
       R->jmp_list_next[S.n] = G->jmp_list_head; G->jmp_list_head = R | S.n
       l.armed = 1; stats.armed++
  unlock(G->jmp_lock)
```

Why it is safe (V from G8, G9 unless marked):
- `jmp_dest[n] = G` (the general C), and `R|n` is on **G's** list. `G` invalidated → `tb_jmp_unlink(G)` resets R's slot to its reset path (`jmp F`) and clears `jmp_dest`; `C'` becomes unreachable. `R` invalidated → `tb_remove_from_jmp_list(R,n)` finds `R|n` on G's list.
- The `cmpxchg` makes arming and stock chaining mutually exclusive, exactly like `tb_add_jump`.
- `C'` exits with its own struct (`exit_tb(C'|k)`); the main loop reads `C'->icount/cflags` (= C's) and may `tb_add_jump(C', k, D)` on its own registered slots.
- `C'` is not in the QHT: `do_tb_phys_invalidate(C')` (reachable only via `tb_check_watchpoint`) sets `CF_INVALID` and returns early (G9); the code stays valid, R's slot may still jump to it (harmless), and watchpoints disarm links anyway.
- **Rehydration:** `C'` is not rehydrated; C was, at its own activation (a precondition of arming). `C'` executes the same baked plugin pointers as C. Whether FastDyn's rehydration side effects are per activation (as assumed) is **U** (X-REHY, §15); fallback: call `qemu_plugin_supercache_rehydrate(cpu, Cp, Vc)` in `sc6_build_variant`.
- MMU index for the probe: `FIELD_EX32(G->flags, TBFLAG_ANY, MMUIDX)`, which is exactly the index the lookup's fetch uses (§4.6); U-MMU is closed (V-c, review-source-facts F4) [m-SF4].

### 5.6 Disarm and invalidation

| Event | Mechanism | Why enough |
|---|---|---|
| C invalidated (SMC, debug write, watchpoint hit) | stock `tb_jmp_unlink(C)` resets R's slot (SPEC → `jmp F`; GENERAL → `exit_tb(R|n)`) | C' has no other entry (V17) |
| R invalidated | stock `tb_remove_from_jmp_list(R,n)` | R is unreachable afterwards (removed from QHT/jump cache; incoming links unlinked) |
| `tb_flush` (incl. a breakpoint/step gdb stop and plugin reset; in system mode plugins load only at start-up) | `tcg_supercache_runtime_reset` also clears `live`, `armed`, `disarmed_epoch`, built flags (Q6); structs are rebuilt and slots reset at re-activation (G4, G5) | flush destroys the region tree and all links |
| **any TLB flush** (MPU write, memory commit, watchpoint, reset; G12) | new `tcg_supercache_mapping_changed(cpu)` at the end of the three flush workers (Q7): for every slot with a live, valid `src_tb`: `tb_unlink_jump(src_tb, n)` (remove from the destination list, reset to the reset path, clear `jmp_dest[n]` to 0); the same for both registered slots of every built variant `Cp` (C' clones C's slots, including a cloned guarded stub when C is itself a grown unit, and the main loop may chain them cross-page) [m-CE6]; every armed link → `armed = 0, disarmed_epoch = 1`; `stats.disarm_tlb++`; after `SC6_MAX_DISARMS` (64) → `sc6.forbidden = true` | GENERAL slots may be re-linked by the main loop only after a fresh lookup under the new mapping; SPEC links stay off until the next flush epoch. Latency equals stock's: a flush requested from another thread runs as queued vCPU work, exactly as data-TLB changes do (I) |
| one-insn-per-tb set (tcg-all.c:256-262; at run time the setter runs on the **monitor thread**, G32) | Q9: the setter only does `qatomic_set(&sc6.forbidden, true)` (no list walk off the vCPU); if `first_cpu` exists it queues `async_safe_run_on_cpu(first_cpu, sc6_disarm_all_work, …)`, which runs in the exclusive context like `tb_flush` [R3] | once the global is set, `curr_cflags` adds `CF_NO_GOTO_TB|1` (G10), so no lookup or main-loop entry reaches a normal-cflags R; until the queued disarm runs, only TBs reached through existing chains of normal TBs can reach an armed slot, an exposure stock same-page chains share (AC2) |
| plugin reset / uninstall (`plugin_flush_destroy`, plugins/loader.c:381, exclusive context) and scoreboard growth (plugins/core.c:258); `plugins/core.c:711` is the linux-user exit path and is not hooked [m-SF5] | Q11: forbid further arming for the run (B graft) | the observer set changed; cached instrumentation is the baseline's exposure, but no new specialization is created on top of it |
| breakpoint inserted while armed | inline g_bp fails → `F` → lookup → `check_for_breakpoints` | V (G7) |

`tb_unlink_jump(tb, n)` is new in `accel/tcg/tb-maint.c` (Q8, ≈25 lines): `tb_remove_from_jmp_list(tb, n); tb_reset_jump(tb, n); qatomic_set(&tb->jmp_dest[n], 0);`. The three TLB-flush hooks run on the vCPU thread (`assert_cpu_is_self` in each worker, cputlb.c:377, 538, 728) and the Q9/Q11 disarms run in the exclusive context (`async_safe_run_on_cpu`, `plugin_flush_destroy`); with one vCPU the LSB-then-clear sequence therefore cannot race (I). No disarm runs directly on the monitor thread or a FastDyn helper thread [R3].

**Invariant (stated; asserted in WP22 debug builds):** after any TLB flush, no variant is reachable and no variant slot is linked until the next `tb_flush` epoch (R's SPEC slot is reset, `disarmed_epoch` blocks re-arming, the variant's own slots are unlinked, and C' has no other entry, V17) [m-CE6].

**SMC while a member runs.** ARM has no precise SMC (V-map); a store that invalidates C returns into the running code, as for baseline C. An invalidated R that is still executing keeps its own slot (G9: `tb_remove_from_jmp_list` does not reset it) and could reach C/C' once more; this only matters for RAM-resident code, which is excluded (`ram_resident`; review must-fix 7), and stock same-page `goto_tb` has the same exposure.

### 5.7 Per-dependency retirement (spec:120, 175, 185)

| Trigger | Granularity | Mechanism |
|---|---|---|
| constituent code page | per TB | page protection on R and C (both live) → invalidate → unlink (5.6) |
| callee summary | per R | the summary is a function of R's bytes (guest-hash checked at activation) and the patch plan; changed R ⇒ R not live ⇒ no link; F-B3-16(d) rebuilds the callee with one changed instruction [R17] |
| patch (plan) | whole artifact | FastDyn fingerprint deletes the cache; deploy refuses a `patch_plan_hash` mismatch with the existing codes `check_fingerprint_mismatch` / `bundle_mismatch` / `bundle_stale` (scopt/run/deploy.py:37-41; no new code) [R17]; F-B3-16(a) |
| helper binding | whole artifact | `runtime_signature` (QEMU/plugin r-x maps) rejects the file |
| observer registration | dynamic: breakpoints (g_bp), watchpoints (flush → disarm), single-step (G10); plugin reset (forbid); static: fingerprint + `instrumentation_hash` / `contract_id` (a changed FastDyn observation configuration, e.g. coverage on, is refused at deploy with `config_mismatch`, `bundle_mismatch` or `check_fingerprint_mismatch`; WP26 pins which in a unit test; F-B3-16(e)) [R17] | |

### 5.8 Run-time statistics (`QEMU_SC_V6_STATS=/abs/file`, opt-in, main-loop side only)

Counters written once at exit: links armed / refused (per reason) / disarmed (tlb, flush, forbid); variant entry-check exits (a `TB_EXIT_REQUESTED` return whose struct lies in the variant range, counted in `cpu_tb_exec`); faults and io-recompiles unwound in variants (`cpu_restore_state_from_tb` hits on a variant struct); ext load time. Nothing is added to generated code; the variable is scrubbed from timing arms like every `QEMU_SC_*` (B1 §4.8). C' *execution* counts come from diag runs only (gdb `-nx` ignore-count breakpoints on `C'.tc.ptr`, fixtures only).

### 5.9 QEMU patch list (new worktree, the gate QEMU tree, branch `qint-v6` from `qint`; needs explicit maintainer approval, as WP-QINT did)

| # | File : location | Change | Lines (I) |
|---|---|---|---|
| Q1 | tcg/supercache.c `valid_header` (389-401) and the `!reuse` branch (:620) | accept version 5 or 6 (feature bits are checked in Q2); `warn_report` when an existing version-6 file is discarded [R2] | 15 |
| Q2 | tcg/supercache.c new `sc6_load_ext` (called in `alloc_code_buffer`, reuse branch, before `loaded_max_end` is set, :643-648) | feature bits, parse, validate, index, die | 150 |
| Q3 | tcg/supercache.c `load_records` (403-447) | `tb_offset → entry` map | 20 |
| Q4 | tcg/supercache.c | fold variant extents into `max_end`; hash checks after the code pread in `after_prologue` (:684-693) | 25 |
| Q5 | tcg/supercache.c `lookup_entry` end (:871-876) | `sc6_on_activate`, `sc6_try_arm`, `sc6_arm_conditions` (probe), `sc6_build_variant` | 110 |
| Q6 | tcg/supercache.c `tcg_supercache_runtime_reset` (715-723) | clear link state | 10 |
| Q7 | tcg/supercache.c `tcg_supercache_mapping_changed`; calls in accel/tcg/cputlb.c after :395, after :555, at :745 (before `return`) and after the loop ending :757 | disarm all guarded slots and every built variant's registered slots | 40 + 4 |
| Q8 | accel/tcg/tb-maint.c after :860 (+ prototype in internal-common.h) | `tb_unlink_jump` | 25 |
| Q9 | accel/tcg/tcg-all.c `tcg_set_one_insn_per_tb` (:256-262) | set the atomic forbid flag; queue `async_safe_run_on_cpu(first_cpu, …)` disarm when `first_cpu` exists [R3] | 12 |
| Q10 | accel/tcg/cpu-exec.c `cpu_tb_exec` (:451-460), accel/tcg/translate-all.c `cpu_restore_state_from_tb` (:175-195) | `unlikely(sc6_stats)` counters | 60 + 4 |
| Q11 | plugins/loader.c:381 (`plugin_flush_destroy`), plugins/core.c:258 (scoreboard growth) | `tcg_supercache_v6_forbid()` [m-SF5] | 2 |
| Q12 (branch `qint-v6-t1`, WP23, not in the gate binary) | tcg/sc-capture.c (level `ops`, replace the die at :585-587), tcg/tcg.c after the `CPU_LOG_TB_OP_OPT` block (:6937), accel/tcg/plugin-gen.c `inject_cb` (:353) | Tier-1 OPS + PLUGIN_CBS chunks (observe-only, rules R1-R6 of B1 §2.2) | 335 |

Loader ≈ 480 lines in 6 files; capture ≈ 335. Every QEMU change changes `runtime_signature`, so every arm and baseline is re-collected on the `qint-v6` binary; B2 timings are not reused (spec:179; feasibility review M10).

**Gate binary [R13].** All dynamic gate evidence (B3 and B4) runs on one frozen binary, `qint-v6@<sha>` = the commit after WP22 (Q1-Q11). Its exe sha256 is recorded in the RuntimeIdentity of every gate artifact and in both gate reports. Q12 (Tier 1, WP23) is built on a separate branch `qint-v6-t1` from that commit and is used only for Tier-1 capture and `ops-xcheck`; it never runs a gate arm. Any later commit to `qint-v6` voids the dynamic gate evidence, which is then rerun in full.

---

## 6. Recovery

### 6.1 Faults and helper longjmps inside R (grown return TB)

R's bytes `[0, T)` are unchanged, its rows are byte-identical (they sit at the new `tc.ptr + tc.size`), and every faultable host return address lies before `T`. The stub `S` follows R's last guest instruction and contains no faultable operation; its only call (`lookup_tb_ptr` in F) never unwinds with its return address (its fetch probe passes `retaddr = 0`; a breakpoint hit does `cpu_loop_exit`, G7). A host pc in the stub would unwind to −1 rows and is never used. So the effectful-callee fault (spec:118) stacks exactly the baseline PC/xPSR/CFSR/BFAR (F-B3-8).

### 6.2 Faults and io-recompiles inside C' ("the second member", spec:44, 185)

- `C'` is in the region tree with its own disjoint range (Q5), so `tcg_tb_lookup(host_pc)` returns `C'` and `cpu_unwind_data_from_tb` walks `C'`'s rows (V-j, translate-all.c:137-169).
- Rows are byte-identical to C's and patches are same-length and never touch an `insn_start` boundary (V15/V16), so `unwind(C', h) == unwind(C, h)` for every recovery point `h` (each raddr and helper-call return).
- PCREL restore writes `regs[15] = (regs[15] & PAGE_MASK) | data[0]`; at C' entry `regs[15] == Vc` (g_pc), and C' updates R15 exactly as C does (V12 compares env including R15 at every sync point). `condexec` and `exception.syndrome` come from the row data (target/arm/cpu.c:116-121) [m-SF14].
- icount: `icount_decr.low += C.icount − i` is correct because C' has C's prefix (with its icount immediate) and `C'.icount == C.icount` (G16).
- `cpu_io_recompile` finds C', restores state, sets a 1-insn `CF_MEMI_ONLY|CF_NOIRQ` next TB from the main loop by lookup; the MMIO access had not happened (G13, `io_prepare` runs before the access), so nothing executes twice (spec:120).
- Helper longjmps (exceptions, `cpu_loop_exit`) lose host registers; V12 compares the observable env at every call, so a longjmping helper sees baseline env.
- A load on an arm that branch folding removed was not executed by baseline on that path either (the arm is infeasible under the fact); V12 checks per path. Loads are never deleted (DHO never touches an LDST block; V15).

### 6.3 Interrupts at member boundaries

Along `R → C'` there are exactly two opportunities: R's entry check (before any of R's effects: baseline) and C''s entry check at `tc.ptr + 0` (G14). If C''s fires: `exit_tb(C'|3)`; `cpu_tb_exec` reads `C'->icount/cflags` (= C's); PCREL `synchronize_from_tb` is a no-op, but env is complete because R left env exactly as before its lookup call (V13 (b)) and R stored `regs[15] = Vc`. The handler may clobber stacked r0; exception return goes through lookup to the **general** C. The fact is never used after an opportunity (spec:58, 118). No opportunity exists inside a TB body (V-j, translator.c:43-86).

### 6.4 icount accounting

Nothing in B3 or B4 changes a prefix (except deleting the dead `can_do_io` prefix store, which is not part of the icount sequence, §7.3), an icount immediate, `tb->icount`, a row, or the guest-instruction mapping; a guarded transfer adds no guest instruction and runs the target's own decrement; a folded branch is still accounted by C''s TB-level decrement. **Therefore `CF_USE_ICOUNT` TBs are supported** and the FreeRTOS icount fixtures are the primary qualification and measurement mode (deterministic interrupt points make the differential oracle exact, subject to E-DET). Count-limited, `CF_NOIRQ` and `CF_MEMI_ONLY` TBs are never sources, targets or variants (`special_cflags_unit`; they are entered only from the main loop, V-map). The B1 §4.9/D24 statement "icount regions unsupported in v1" is withdrawn (§16 D-B3-6).

### 6.5 No state fixups

The `state_fixups` hook in `cpu_restore_state_from_tb` promised by B1 §4.9 is **not** implemented: it could restore constants only, not register-held values (map-state-sync §6.2), and does not cover entry-check exits. `ADD_RECOVERY` stays `capability_not_enabled`.

---

## 7. B4: bounded executor-state synchronization elimination

### 7.1 Sync points

| Id | Point (host) | Observer | Mark |
|---|---|---|---|
| S1 | TB entry, before the entry check | `cpu_handle_interrupt` / v7m stacking reads r0-r3, r12, LR, PC, xPSR from env after an entry-check exit | V-map |
| S2 | every exit: goto_tb jmp, exit_tb jmp, guarded stub entry | successor's S1 or the main loop; on direct-chain exits (goto_tb, guarded slots) also the successor's TB-start plugin callback (G30) | V |
| S3 | `HELPER_CALL lookup_tb_ptr` | key fields; breakpoint `cpu_loop_exit` | V (G7) |
| S4 | every call not on the pure allowlist. The allowlist is the `TCG_CALL_NO_RWG` helpers that cannot raise (`sdiv`/`udiv` are S6). Every flags-0 or `NO_WG` call, plugin callback and noreturn exception helper is an S4 point [m-SP10f] | the helper (flags 0 reads/writes globals; NO_WG reads them) | V-map |
| S5 | each LDST block (fast path `jne` to the slow path) | fault (`cpu_restore_state` + env), MMIO (`io_prepare` reads `can_do_io`), plugin memory helpers | V (G13) |
| S6 | `udiv`/`sdiv` (NO_RWG but may raise DIV_0_TRP) | treated as S4 (baseline image is imprecise there) | V-map |
| P | `brcond`, `br`, `set_label` | none (pseudo) | V-j (tcg.c:4950-5038 asserts only) |

### 7.2 Observation contract `obs-contract@1` (fail closed: any field not listed is observable everywhere and not deletable)

| Field (env offset, G21) | Observable at | Deletable class | Justification |
|---|---|---|---|
| r0-r14 0x00-0x38; CF/VF/NF/ZF 0x210/0x214/0x218/0x21c | S1-S6 | **D1** | v7m stacking, helpers, faults, successors |
| `regs[15]` 0x3c | all | no (`pc_store_required`) | PCREL restore and successor PC updates |
| `thumb` 0x14d, `condexec_bits` 0x228, `hflags` 0x150-0x15f | all | no (`not_bookkeeping_class`) | key |
| `icount_decr` −0x8 | all | no | accounting (spec:64) |
| `can_do_io` −0x4 | S4, S5; **and S2 direct-chain exits (goto_tb, guarded slots) whenever `tb_start_cb_possible`** [R5] | **D2** | read by `io_prepare` and the icount readers (G30). Exits through the main loop (`exit_tb`, G15) or the lookup helper (G7) rewrite it before any reader. On a direct chain, though, the successor's TB-start plugin callback runs before the successor's own `can_do_io` store (G30), and it may raise an IRQ (`cpu_abort` if false, icount mode) or read icount. `tb_start_cb_possible` is a RuntimeIdentity fact (V20): false only if the FastDyn configuration registers no TB-translation hook and no core TB-exec callback (no twintrace), cross-checked against Tier 0 (no prefix plugin call in any captured unit); unknown ⇒ true (fail closed, `observer_contract_unknown` is not needed because the conservative rule still applies) |
| `plugin_mem_value_low/high` −0x18/−0x10 | S4 (any non-pure call; conservative) | **D3** | read only by `qemu_plugin_mem_get_value` from plugin callbacks; every reader is preceded by a fresh write (G22); RuntimeIdentity records that the loaded plugins do not call it outside memory callbacks |
| `plugin_mem_cbs` −0x20, exclusive_*, QF/GE, anything else | all | no | fail closed |
| any non-`rbp` memory operand | — | never | guest memory or observation events (§7.5) |

### 7.3 Algorithm (bounded, per unit, including variants)

```
for each unit U (general or variant), hot-first, until the work budget is spent:
  if not ok_relocatable or meta_status != ok: skip(unit_not_relocatable)
  if U.cflags & (CF_COUNT_MASK|CF_NOIRQ|CF_MEMI_ONLY): skip(special_cflags_unit)   # main-loop-only TBs; fail closed [m-SP8]
  G = hcfg(U)   # LDST(k) and calls atomic; forward edges plus slow-path returns to raddr
  if paths(G) > 256 or candidate stores > 256: skip(path_budget_exhausted)
  need = backward may-need dataflow per field f in {D1, D2, D3}:
      at sync event e:  need[f] |= observable(f, e)      # table 7.2, incl. can_do_io at direct-chain exits
      at ENV_LOAD(f') overlapping f: need[f] := true     # TCG reloads globals after brcond/set_label; LOG_REG reads env  [R7]
      at an unclassified rbp access: need := ALL         # HostOps pins such units anyway (defence in depth)
      at a store to f covering its width: need[f] := false before it; dead(s) := !need_after(s)
  candidates = { s | dead(s) on every path, s in a deletable class }
  placement: s not in an LDST block, exit stub, pool, guard stub, and not on an insn boundary;
             s not in the prefix [0, insn0_start) EXCEPT the single can_do_io store emitted before the
             first insn_start (G13) — the entry-check/icount bytes stay frozen         # feasibility review M3
  edits = same-length nop of each candidate (one multi-byte nop per store)
  submit PURE_REWRITE(edits); hostsym validates independently (V12); mismatch -> failed(validation_mismatch)
```

- **D1:** stores of r0-r14/NZCV dead because TCG syncs at pseudo points P (2.16 % model, 83 % in TB 0x1d5a; G28).
- **D2:** `can_do_io` stores with no S4/S5 before the next `can_do_io` store or an exit (and, when `tb_start_cb_possible`, no direct-chain exit: the last-insn `true` store before a goto_tb or guarded exit is then kept [R5]), including the prefix `false` store of multi-insn TBs whose non-last insns contain no LDST and no non-pure call, and the prefix `true` store of 1-insn TBs with no LDST/call (up to 2.77 % model; 1.78 % without the prefix exception; the share lost when `tb_start_cb_possible` holds is **U**, measured by WP25). The prefix `false` store follows the TB's own TB-start callback (G30), so deleting it never changes what that callback sees.
- **D3:** `plugin_mem_value_*` stores with no non-pure call before the next write or an exit (2.39 %).
- **Not in v1:** D4 slow-path compensation (`needs_slowpath_compensation`; soundness review A1 IT-block counterexample, must-fix 3), D5 cross-TB store elimination (`cross_tb_dse_needs_value_transport`; deferral argued in §7.6 [R21]), D6 register forwarding (`needs_regalloc`), D7 PC store removal (`pc_store_required`), true deletion (`needs_splicer`: v1 nop-fills; nops still cost decode bandwidth, feasibility review).

### 7.4 Required state at exits, callbacks, faults and interrupts (no compensation exists)

| Observer | Why its view is unchanged |
|---|---|
| normal exit (S2/S3) | a deleted store is dead before every exit; V12 compares the full observable env at every exit |
| callback (S4) | V12 compares env (per contract) at every call; after a flags-0 call TCG reloads globals from env, and the code after the call is unchanged |
| precise fault (S5, S6) | V12 compares the S5-observable env at every LDST block and raising helper; rows unchanged ⇒ PC/condexec/icount restore unchanged |
| MMIO / `cpu_io_recompile` (S5) | `can_do_io` is observable at S5, so a `false` store protecting a non-last-insn LDST is never dead |
| interrupt recovery (S1) | nothing is deleted across a TB boundary; env at every exit is equal, so every entry check (this unit's and its successors') sees baseline env |
| TB-start plugin callback of a direct-chain successor | `can_do_io` is observable at direct-chain exits when `tb_start_cb_possible` (§7.2); F-B4-9 [R5] |
| single-step, debug, one-insn-per-tb (spec:64) | debug-mode TBs (`CF_SINGLE_STEP`, `CF_NO_GOTO_TB|1`, BP-page variants) have other cflags and are translated fresh, never taken from B4-rewritten records (G10, G7); a gdb stop happens at a TB boundary where env is complete (nothing is deleted across S2); a watchpoint hit unwinds through S5 [m-SP8] |

### 7.5 Guest memory events are never deleted as bookkeeping

- Candidates are exactly `rbp`-based stores whose displacement is in D1/D2/D3 (V15).
- V12 compares, per path, every `GuestAccess` (LDST k, MemOpIdx, address term, value term, **plus the S5-observable env snapshot**; review must-fix 4), every non-env host store (`HostStore`: FastDyn UPDATE_MEM through `gpa2hva`, LOG_REG ring stores, STORE_IO, `return_from_runtime`; fastdyn translate paths, V-j translate.c:99-310) and every call.
- V12 asserts that every absolute host-pointer operand (`host_abs` binding) lies **outside** `[env − sizeof(CPUState), env + sizeof(CPUARMState))` of the vCPU (bounds from EnvLayout; review must-fix 4). FastDyn's absolute pointers target guest RAM and plugin buffers (V-j F16), so the assertion holds today and is checked per artifact.
- So no guest load/store, MMIO access, plugin memory callback, virtual or coverage/LOG_REG store can be removed or reordered.

**B4 reason codes:** `field_not_deletable_class`, `observed_at_sync_point`, `no_dead_store`, `unit_not_relocatable`, `path_budget_exhausted`, `edit_in_prefix`, `edit_inside_ldst_block`, `edit_in_exit_stub`, `host_abs_aliases_env`, `observer_contract_unknown` (D3 without the RuntimeIdentity plugin fact), `validation_inconclusive`, `validation_mismatch`, plus the out-of-scope codes of 7.3.

**Packaging:** B4-only edits produce rewritten singleton records and are written as a **v5** file (no guarded slot, no variant); they run on the current binary for the early measurement (WP25) and on `qint-v6` for the campaign. With any B3 edit the artifact is v6.

### 7.6 Cross-TB state traffic (spec:14): what v1 does and what it defers [R21]

- **Done, load side, on certified edges.** On an armed R→C' link, CP replaces C''s env loads of fact fields that R stored with immediates (§3.5). That is cross-TB load elimination on the one edge whose predecessor is known. The report counts it in both the B3 and the B4 categories (removed env loads per edge × edge weight).
- **Deferred, store side (D5).** A store in R whose field C' overwrites before any observer is still observable at C''s entry check (S1: an entry-check exit hands env to `cpu_handle_interrupt` / v7m stacking, G14) and on R's guard-fail path F (lookup → general C). Deleting it needs the value materialized on both paths, i.e. compensation code in the stub's F path and in C''s `exit_tb(C'|3)` path. Invariant 4 forbids compensation in v1, and the equal-env-at-every-sync-point rule of §7.3 cannot certify it. Reason code `cross_tb_dse_needs_value_transport`.
- **Evidence that would justify D5 (v2).** (1) WP16's D1 probe shows a measured paired speedup with the CI excluding 0 for nop-filling intra-TB dead stores (if it does not, a cross-TB store removal of similar size will not either); (2) the model count of R stores overwritten in C' before any S4/S5, weighted by armed-edge traffic, exceeds the D1 count; (3) a validator rule for "equal env at every sync point after compensation" with its own mutation suite. Until then the B4 report states that spec:14's cross-TB case is exercised only on the load side.

---

## 8. Representation (spec:84-86, review requirement T3)

### 8.1 HostOps (`hostops@1`): the stable operation view

The decoded body of a unit, partitioned and typed **only from Tier-0 emission-time metadata** (B1 §2.3-2.7; every byte explained or the unit is pinned):

| Block / op kind | Delimited by | Content |
|---|---|---|
| `prefix` | `[0, insn0_start)` | entry check, icount store, `can_do_io`, TB-start plugin callbacks |
| `insn(i)` | OpMark `insn_start` host offsets | ops typed `ENV_LOAD(field)`, `ENV_STORE_{IMM,REG,OTHER}(field)` (EnvLayout; an unknown `rbp` displacement pins: `unclassified_env_access`), `CONST`, `COPY`, `FLAGS`, `JCC/JMP` (LABEL sites), `CALL(helper_id, flags)`, `HOST_ABS_STORE(binding)`, `OPAQUE` (capstone regs_write/eflags) |
| `ldst(k)` | QEMU_LD/ST OpMark → raddr; slow path `[ldst_begin_k, ldst_end_k)` | one `GUEST_ACCESS` with `oi`, address/data registers, clobbers |
| `exit_stub(n)`, `guard_stub` | EXIT_TB/GOTO_TB/GOTO_PTR sites; ext slot entries | kind, slot, struct LEA |
| `pool` | `pool_start` | data |

Stable identifiers are the op kind, the EnvLayout field name, helper ids, MemOpIdx, OpMark insn indices; no TCG pointers, no temp ids (spec:86). HostOps, `effects@1` and the S1-S6 map (§7.1, the checkpoint map) are analyses over UnitView, not new UnitView members: they are derived from Tier-0 metadata that UnitView already exposes (B1 §5.3) and are versioned and cached like every other analysis (spec:106) [m-SP10b]. Host registers of different TBs are never connected: facts are env-only and cross a TB boundary only as a certified env-field fact at a guarded exit (spec:49, C8).

### 8.2 Tier-1 cross-check (`ops-xcheck@1`, WP23)

Q12 captures OPS (opc name, type, flags, `op->life`, insn index, host offset, args with per-TB renamed temps, globals by name+env offset, constants by value, helper ids) and PLUGIN_CBS (insn index, cb kind incl. FastDyn inline kinds, RW flags, reg, imm). `ops-xcheck@1` checks that every HostOps env store corresponds to a `SYNC_ARG` or explicit `st` op, that every certified fact's defining store matches a `mov_i32 global,$k` (modifier or guest op) and that PLUGIN_CBS agrees with the patch-plan join. Any mismatch pins the unit (`ops_bytes_mismatch`). The capture must leave the `.sc` byte-identical to level `reloc` (extends T-B1-1).

---

## 9. Validation

### 9.1 Static checks (rows in `validation.json`; any FAIL rejects the candidate and keeps the baseline)

| Code | Check |
|---|---|
| V1-V10 | B1 §5.8, with V2/V5 relaxed only for units carrying declared B3/B4 edits; V5 identity = every original unit present plus declared variants; V7 requires a v6-capable RuntimeIdentity (the `qint-v6` exe sha256) for v6 artifacts (today `scopt/validate.py:565` accepts only v5); **V9 byte identity** for the identity pipeline and for every pipeline whose passes all return `no_change` (§9.6) |
| V11 | ext table: every rule of 5.3/5.4 computed offline by `sc_v6.py` **and** by an independent parser `tools/scopt/independent/sc6parse.py`; `variants_max_end` and `max_end' < code_size − host_page − 1024`; artifact id |
| V12 | `hostsym` trace equivalence (9.2) for every changed unit: R' up to the slot exits (with the guarded-chain rule), C' vs C under `P = facts ∧ guard`, every B4 rewrite; `GuestAccess` carries the S5-observable env; host_abs operands outside env/CPUState |
| V13 | guarded-chain contract: no admitted R ends in a DISAS_EXIT-class instruction (§3.1 rule 6); stub decodes to the canonical template of 4.1 with every atom (g_bp, g_thumb, g_cond, g_flags, g_flags2, g_pc per target) and constants equal to the slot entry and key_C; R15/thumb stores precede the tail on every path; F byte-equal to the original tail; reset paths GENERAL = `lea [own+n]; jmp tb_ret`, SPEC = `jmp F`; `jmp_insn_offset[n]` 4-aligned rel32, 0 on disk; stub has no store, call or LDST; EnvLayout id equals the RuntimeIdentity's |
| V14 | fact certification for every SPEC link (hostsym on R'), equality with the proposer, provenance row present |
| V15 | edit placement: same-length; not in prefix except the D2 prefix store; not in `ldst(k)`, exit stubs, pool, guard stub; no `insn_start` boundary crossed; icount immediate unchanged; deletions only of deletable classes; re-decode tiles |
| V16 | recovery: rows byte-identical; `C'.icount == C.icount`, row count == icount; `unwind(C', μ(h)) == unwind(C, h)` for every recovery point; C' bytes == relocate(C) except declared patches ("identical modulo relocation"); every embedded host pointer of C' (plugin data, helper addresses) equals the corresponding binding of C [m-SP15] |
| V17 | reachability: no relocation, record, slot or link targets C' except its one LinkEntry; C' key never appears as a record |
| V18 | `hostsym` self-test passed for this scopt version: native `ctypes`+`mmap(PROT_EXEC)` differential test of every concretely modelled instruction form, and the mutation suite (9.3) rejected 100 % |
| V19 | summary/region consistency: CalleeSummary and CacheRegion (`multi_tb`) rows with current dependency hashes; derived `.scm` (TB chunk v2 with `stub_start`, Python-only chunk 9 `VARIANT`) re-imports to the output units; graph round trip includes the new edge kinds `guarded_slot` and `variant_link`, unknowns explicit (spec:43) |
| V20 | admission identity: CPU model `cortex-m3`; EnvLayout bound to the exe sha256; plugin set has no `qemu_plugin_mem_get_value` use outside memory callbacks (D3 only); `tb_start_cb_possible` derived from the FastDyn configuration and cross-checked against Tier-0 prefix plugin calls (D2 at direct-chain exits, §7.2); none of the AC4-excluded virtuals present; `exec_vaddr_aliases` recorded from the machine memory map (informational: g_pc covers aliases) |
| V21 | budgets and growth accounting: per-pass byte deltas, variant count, stub bytes vs hot-set bytes |
| V22 | ops-xcheck (when Tier 1 present): 0 `ops_bytes_mismatch` on changed units |
| V23 | observation contract unchanged: output `contract_id`, `instrumentation_hash` and obs-contract table version equal the input's; no pass adds, drops or moves a plugin callback or observation event (V12 compares them per path) [m-SP10d] |

### 9.2 `hostsym`: the validator kernel (from the design C prototype `hostsym_proto.py`)

- **State:** 64-bit register terms (`rbp = ENV`, `rsp = FRAME`); flags Z/S/C/O terms; env write log keyed by (offset, width), unwritten reads give `env0(off,w)` unless `P` fixes them, partial overlap gives an opaque term that fails closed if it matters; non-env, non-LDST memory: `HostStore(addr, w, value)` events and versioned `hmem` reads.
- **Abstractions:** `ldst(k)` atomic when its fast+slow bytes are identical in both versions under the offset map μ: event `GuestAccess(k, oi, addr, [value], env|obs(S5))`, fresh `ld_k(...)` result, clobbers havocked (otherwise `edit_inside_ldst_block`); `call` → `Call(helper_id, args, env|obs(class))`, havoc caller-saved registers, flags and env per helper flags; `jmp reg` (goto_ptr) → `Exit(goto_ptr, env|all)`; registered goto_tb field → `Exit(goto_tb, n, env|all)`; `jmp tb_ret`/epilogue → `Exit(exit_tb, rax ≅ struct|n, env|all)`.
- **Semantics:** concrete for mov, movzx, movsx, lea, add, sub, and, or, xor, not, neg, inc, dec, test, cmp, shl/shr/sar imm, setcc, cmovcc, jcc, jmp, nop, int3; every other mnemonic of the TCG vocabulary is an uninterpreted deterministic function of its inputs with fresh flag terms (sound for syntactically equal instructions); undecodable instructions or backward branches other than slow-path returns make the path UNSUPPORTED.
- **Equivalence (V12):** for every candidate path feasible under `P` there is an original path with the same sequence of symbolic branch outcomes and vice versa, and the traces are equal term-for-term after constant folding, modulo `C' ≅ C` struct pointers, μ, the per-event env restriction of `obs-contract`, and the guarded-chain rule: a candidate path ending in `Exit(goto_tb, n)` at a declared guarded slot matches an original path ending in `Call(lookup_tb_ptr); Exit(goto_ptr)` iff its path condition entails every guard atom, its env snapshot equals the original snapshot at the lookup call, where the lookup's `can_do_io = 1` is applied **to the original side only** (when `tb_start_cb_possible`, the candidate must itself show `can_do_io == 1` at the slot, so a deleted last-insn `true` store is a mismatch; otherwise `can_do_io` is unobservable there), and the stub has no call/store/LDST [R5].
- **Budgets:** 256 paths per unit; UNSUPPORTED or budget → `validation_inconclusive` (skipped_unknown); difference → `validation_mismatch` (failed).
- **Prototype evidence (V-j, design C prototype results):** T1 fold of the real `cbz r0` continuation 0x24f0 EQUIVALENT under `r0 == 0`; T2 same without the fact REJECT; T3 wrong direction REJECT; T4 fact certified on 0x1ede; T5 deletion of the prefix `can_do_io=0` store EQUIVALENT (allowed by the D2 prefix exception of 7.3, which resolves C's internal contradiction); T6 effectful 0x1f7c UNSUPPORTED without the LDST abstraction (as designed). Coverage target: ≥ 99 % of compute steady weight analysable (**U**, WP19 reports it).

### 9.3 Mutation suite (must be rejected 100 %; also used as dynamic negative controls)

Flip a fold direction; fold without the fact; delete a guest store; delete an UPDATE_MEM / LOG_REG store; delete a flag store before an LDST; delete a flag store before a flags-0 helper; **delete a `brcond` sync store whose global is reloaded in the next BB** (T-B4-2b, F-B4-1) [R7]; **delete the last-insn `can_do_io=1` before a direct-chain exit under `tb_start_cb_possible`** (F-B4-9) [R5]; **nop R's dead-result LDST** (F-B3-3b) [R16]; **IT-block D4 mutant** (move the conditional `moveq r1,#5` store into a slow-path stub, review A1 shape); drop each guard atom in turn; move the guard before an effect; change an icount immediate; corrupt a search row; point a link at a variant whose fact is falsified (`r0 == 1`); **share one variant between a full return unit and its suffix unit** (F-B3-20) [R4]; put a host_abs pointer inside env.

Every mutant must be rejected statically (V12-V17). Dynamic detection (O4) is required for every mutant except the **static-only** set below; each static-only entry states why no admitted execution can tell it from the real artifact [R6].

| Mutant | Dynamic detector | Static-only reason |
|---|---|---|
| drop g_bp | F-B3-13(d): breakpoint inserted at a PAUSED stop while the link is armed | — |
| drop g_thumb | F-B3-22 `invstate` | — |
| drop g_pc | F-B3-6 (`f5` and `bx r3` paths), F-B3-15 alias | — |
| drop g_cond | F-B3-22b, only if U-VIRT finds a non-RW_CFI virtual that writes xPSR (G33) | otherwise static-only: at a return-exit slot condexec is 0, because a return is last in any IT block and the block has ended (I; U-COND checks every compute and fixture return unit) |
| drop g_flags, g_flags2 | none | on cortex-m3 the cached hflags cannot change between R's key match and R's exit: MSR/CPS end the TB with `exit_tb(NULL,0)` (§3.1 rule 6); SCS writes are MMIO and run as the last insn of their own TB (G13); exceptions end the TB; gdb/plugin writes of M system registers are a no-op and an xPSR write does not rebuild hflags (G33); an R entered by a chain or a guarded slot inherits a key matched the same way (I). A privilege drop before the call re-keys R itself, so it does not reach the grown R (F-B3-21 checks correctness, not this mutant). The atoms stay as defence in depth (other CPU models, AC4 violations) |
| all others | the fixture named for the mutant in §9.6 | — |

### 9.4 Dynamic differential tests

- **Arms per fixture:** `warm` (v5, reference), `identity-v6` (v6 header, empty ext table), `chain` (GENERAL slots), `spec` (SPEC + CP/BF/DHO), `b4`, `combined`; each in a fresh run dir with the post-run proof extended for v6 (header version 6, ext bytes [0x100, 0x10000) unchanged after the run).
- **O1 outputs:** normalized semihosting/printreg/BENCH outputs, exit code and fault reports equal to warm on held-out inputs (V10).
- **O2a exception sequence:** with icount shift 0, `log_options="int"` lines byte-identical to warm. These lines carry the exception number, the vector PC, the EXC_RETURN magic and fault addresses, **not** icount or the interrupted PC (G31), so O2a shows only the same order of exception kinds and vectors [R1].
- **O2b interrupt position record (the instruction-count/PC oracle, spec:56, 64):** every fixture handler (SysTick and the HardFault/BusFault/UsageFault reporter) appends to a guest log, printed by semihosting at the end: stacked PC, stacked xPSR, the stacked registers the fixture names, SysTick CVR read as the handler's first load, and the guest's own iteration counter. Under icount shift 0 CVR is a function of the executed instruction count (I; E-DET confirms), so equal records mean interrupts taken at the same PC and the same instruction count. Compared exactly in icount mode and by the fixture's named invariants in noicount (§9.5). Requires E-DET (§15); if icount runs are not deterministic, O2b degrades to invariants and O1 remains [R1].
- **O3 non-vacuity (diag runs, never timed):** `QEMU_SC_V6_STATS` counters (armed > 0, variant entry-check exits > 0 where a fixture needs them, refusals by reason) and gdb `-nx -batch` ignore-count breakpoints on `C'.tc.ptr` and on each stub's `F`; a fixture whose specialized path never ran fails as `vacuous`.
- **O4 negative controls:** every mutant of 9.3 outside the static-only set is packaged by a test-only bypass of V12-V17 and must change O1, O2a or O2b on its named fixture; static-only mutants must be rejected statically [R6].
- **O5 fallback:** appended records per run (B1 proof), `disarm_*` and refusal counters.
- **O6 attribution (spec:185):** under a coverage / edge-coverage FastDyn configuration (snapshot, profile and artifact all prepared under that configuration), the coverage output equals warm's and C' hits > 0; this also settles X-REHY for variants [R20].

### 9.5 Fixtures (new `fixtures/b3/`, built like `fixtures/fault/`: Arm GNU 13.3.Rel1, `-mcpu=cortex-m3 -mthumb -O1`, generic `cortexm` machine, semihosting print, the fault fixture's HardFault reporter, phases selected by an input word; callee and continuation on different 1 KiB pages; `qualify.sh` over {icount, noicount})

**Per-mode oracles** [R14]: `expected.py` gives each phase an exact `.norm` in icount mode and named invariants in noicount mode (the COUNTFLAG log of F-B3-3/3b, the interrupt positions of F-B3-7/20 and the SysTick dumps of F-B4-3 depend on timing without icount). **Compiler independence** [R18]: HostOps and hostsym work on TCG output and match no guest idiom (facts come from host stores of constants, folds from `hconst` over host code); F-B3-1 and F-B3-2 are also built at `-O0` and `-Os` (F-B3-24).

| Fixture | Guest sketch | Patch / instrumentation | Expected observable | Spec sub-case |
|---|---|---|---|---|
| **F-B3-1** `two-tb` | `f: adds r0,r0,#1; bx lr`; loop N: `bl f; cbz r0, Lz; nz++; b nx; Lz: z++` | modifier `r0 <- 0` at `f`'s `bx lr` | `z=N nz=0` in every arm; static: C' byte diff shows the r0 load (CP), `test` (DHO) and `jcc` (BF) changed, each pass with its own ledger row and on/off delta; diag: link armed, C' hits ≈ N − warm-up | two-TB example (gate positive) |
| **F-B3-2** `effect-load` | `f` loads a volatile RAM counter, increments, stores it back, `bx lr` | same modifier | counter = N in every arm; continuation folded | effectful callee: guest load |
| **F-B3-1p** `two-tb-pop` | `f: push {r4,lr}; adds r0,#1; pop {r4,pc}`; the stack read (an LDST) lies between the fact definition and the slot | modifier `r0 <- 0` at `pop` | as F-B3-1; V14 certifies through the LDST abstraction | pop-return shape [m-CE5] |
| **F-B3-3** `effect-device` | `f: ldr r1,=SYST_CSR; ldr r2,[r1]; str r2,[log+i]; bx lr` (COUNTFLAG clears on read, G25); the read is io-recompiled into a 1-insn TB and the fact is carried by the suffix TB `[bx lr]` | same modifier | COUNTFLAG log identical (icount); read count = N | effectful callee: device |
| **F-B3-3b** `dead-result` | (i) `f: ldr r1,=SYST_CSR; ldr r0,[r1]; bx lr`, the loaded r0 dead because the patch overwrites it; the caller reads SYST_CSR after each call and logs COUNTFLAG; (ii) guest-load twin `ldr r0,[r2]` with r2 = 0xFFFFFFFF at iteration k. Arms `spec` and `combined` (B4 sees R) | modifier `r0 <- 0` at `bx lr` | (i) COUNTFLAG log equals warm (icount exact; noicount: read count = N); (ii) BusFault report equals warm; the mutant that nops the dead-result LDST changes O1 [R16] | spec:58 "keep an MMIO read … even if a later patch overwrites the result" |
| **F-B3-4** `effect-callback` | as F-B3-1 | modifier on `bx lr` + virtual `printreg r1` on `f`'s first insn | N printreg lines identical; link armed (dynamic hflags guard admits a flags-0 call *before* the definition) | effectful callee: callback |
| **F-B3-5** `helper-clobber` | as F-B3-1 | modifier on the insn before `bx lr` + a register-writing virtual on `bx lr` (**U**: WP24 picks an existing FastDyn virtual that writes r0; fallback `printreg`, which still kills statically) | static `fact_killed_by_helper`, no SPEC link; outputs identical; with a real writer the printed branch matches the written value. The writer must not be RW_CFI: an RW_CFI callback ends the TB with `exit_tb(NULL,0)` (accel/tcg/plugin-gen.c:121-131, V-c), which would give `exit_not_goto_ptr` instead; otherwise attach it to the insn before `bx lr` [m-SF12] | helper clobber |
| **F-B3-6** `alt-entry` | function pointer alternates `f0` (patched) / `f5` (`movs r0,#5; bx lr`, unpatched), both returning to the same continuation; plus `bx r3` to `Vc|1` with r0 = 9 | modifier in `f0` only | counts per path identical; static: SPEC only from `f0`'s R, `f5` gets `no_fact`; diag: `F` hits > 0 on `f5` and on `bx r3`. Sub-phases [m-CE5]: tail call `g: …; b f0`; recursion `r(n)` returning into its own epilogue (C is itself a grown unit; T-B3-26 adds an MPU write while `C'|n` is linked); handler-mode negative: patched `bx lr` in a function called from the SysTick ISR → `handler_mode_return`, outputs equal | alternate caller, arbitrary entry |
| **F-B3-7** `irq-clobber` | SysTick handler: if stacked PC == `Vc` then stacked r0 = 1 and `hits++`; reload swept over a co-prime set so the IRQ lands at C' entry | as F-B3-1 | icount: logs, O2a and O2b (stacked PC + CVR records) identical to warm; `hits > 0` and stats `variant_entry_check_exits > 0` (non-vacuity); no-icount invariant `nz == hits`, `z + nz == N` | interrupt clobber |
| **F-B3-8** `fault-callee` | `f(p)` loads `*p`; iteration k passes 0xFFFFFFFF (the only unmapped address on `cortexm`, fixtures/fault/README) | as F-B3-1 | BusFault report (stacked PC = `ldr` in `f`, xPSR, CFSR, BFAR) identical; effects before it happened once | fault in callee, fault prefix |
| **F-B3-8p** `fault-pop` | F-B3-1p with SP pointing at 0xFFFFFFF0 on iteration k, so `pop {r4,pc}` faults after the fact definition | as F-B3-1p | fault report equals warm; no link taken on that path | fault in R's last insn [m-CE5] |
| **F-B3-9** `fault-cont` | continuation's kept arm `ldr r1,[r3]`, r3 = 0xFFFFFFFF at iteration k | as F-B3-1 | report identical (stacked PC = the `ldr` inside C'); stats `faults_in_variants > 0` | fault in the second member |
| **F-B3-10** `unused-fault` | as F-B3-9 with r1 dead after the load | as F-B3-1 | fault still raised; DHO-deletes-LDST mutant rejected statically and changes O1 | faulting unused load |
| **F-B3-11** `unseen` | held-out input word selects a call site and an `nz` path never seen in train | as F-B3-1 | outputs equal; `F` hits > 0; appended records reported | unseen target, unobserved branch |
| **F-B3-12** `invalidate` | gdb stop at iteration K; debug write turns C's `cbz` into `cbnz`; continue | as F-B3-1 | output equals warm with the same write; C' hits stop after K; stats `armed` unchanged, slot reset observed | code-page invalidation |
| **F-B3-13** `debug` | (a) breakpoint at `Vc` set at a breakpoint stop (flushes), kept across `continue`; (b) watchpoint on `z`; (c) `stepi` across `R → C`; (d) armed link, Ctrl-C (`interrupt`) stop, which is PAUSED and does not flush (G11), then `break *Vc`, a register write `r0 := 7`, `continue` [R6] | as F-B3-1 | (a) arming refused while the breakpoint exists (stats `refused[bp] > 0`), hit count equals warm; (b) watch hits equal, `disarm_tlb > 0`; (c) stops at every instruction including `Vc`; (d) hit count equals warm on every later iteration and the output follows the written r0 as warm does; the g_bp-dropped mutant misses hits (O4) | observer registration, debug boundary |
| **F-B3-14** `mapping` | guest enables the MPU (PRIVDEFENA, background region) after K iterations | as F-B3-1 | outputs equal; `disarm_tlb > 0`; C' hits stop after K | mapping change |
| **F-B3-15** `alias` | mps2-an385 image calling `f` and returning through ssram1 at 0x0 and through its alias 0x400000; run directly on the integrated QEMU (`-M mps2-an385 -accel tcg,supercache=…`), test policy `allow_ram_resident` | modifier emulated by guest `movs r0,#0` (no FastDyn) | outputs equal; g_pc fails on the alias path (gdb diag `F` hits > 0) | PCREL alias (review must-fix 6, 9) |
| **F-B3-16** `stale` | F-B3-1 artifact deployed with (a) changed patch plan, (b) different plugin build, (c) different QEMU binary, (d) an image whose callee `f` was rebuilt with one changed instruction at the same addresses, (e) a coverage-enabled FastDyn configuration [R17] | — | (a) deploy `check_fingerprint_mismatch` or `bundle_mismatch` (existing codes) or fingerprint deletion; (b)/(c) post-run proof INVALID; (d) R's guest hash mismatches at activation, stats `armed == 0`, outputs equal the new image's warm; (e) a named existing deploy refusal (WP26 pins the code) or fingerprint deletion, post-run proof shows the artifact unused | callee summary, patch, helper binding, observation configuration |
| **F-B3-17** `smc-ram` | continuation copied to RAM and rewritten by the guest after K iterations; test policy `allow_ram_resident` | as F-B3-1 | output follows the new code as warm does | code invalidation (RAM) |
| **F-B3-18** `old-loader` | the F-B3-1 v6 artifact on the old `qint` binary | — | post-run proof `INVALID_artifact_not_used` | old loader rejects v6 |
| **F-B3-20** `suffix-clobber` | `f: movs r0,#1; <k filler insns>; bx lr`, continuation `cbnz r0` (natural fact, like compute 0x1eb8); icount shift 0, SysTick reload swept so the budget boundary and the IRQ fall between `movs` and `bx lr`; the handler sets stacked r0 = 0 when stacked PC ∈ (movs, bx lr] and counts | — | O1/O2b equal warm; the suffix unit R_s gets no SPEC link (`fact_not_local_to_return_tb`) whether or not it is in the snapshot; handler hits > 0; the shared-variant mutant changes O1 [R4] | interrupt clobber at a budget boundary |
| **F-B3-21** `priv-drop` | after K iterations `msr CONTROL,#1`; the continuation contains `cpsid i` and a PRIMASK readback | as F-B3-1 | outputs equal warm; C' hits stop after K (R and C are re-keyed by the new MMU index). Correctness of a mode boundary (spec:185), not a g_flags detector (§9.3) [R6] | mode boundary |
| **F-B3-22** `invstate` | on iteration K the callee clears LR bit0 (`biceq lr,lr,#1` under a compare) before `bx lr` | as F-B3-1 | INVSTATE UsageFault report equals warm (stacked PC = Vc); the g_thumb-dropped mutant runs C' with no fault (O4). F-B3-22b (only if U-VIRT finds a non-RW_CFI xPSR writer): the virtual sets IT bits before `bx lr`, which detects the g_cond-dropped mutant [R6] | interrupts/state that change relevant state |
| **F-B3-23** `coverage` | F-B3-1 under a coverage / edge-coverage FastDyn config | as F-B3-1 | O6: coverage output equals warm, C' hits > 0 [R20] | original guest-PC/coverage attribution |
| **F-B3-24** `opt-levels` | F-B3-1 and F-B3-2 built at `-O0` and `-Os` (at `-O0` r0 is tested through a stack spill or a `mov r3,r0`) | as F-B3-1 | per build: simplifies, or a stable skip reason; outputs equal warm [R18] | guest-compiler independence (spec:62) |
| **F-B3-19** `compute` | FreeRTOS compute held-out/final inputs (natural edges `prvOpIs`, `prvKvGet`) | existing config | V10 equal; coverage/report rows | real workload |
| **F-B4-1** `b4-loop` | CRC-like loop with flag-setting ops between branches (0x1d5a-like); `printreg r2,r3` every M iterations | — | printreg lines equal; D1/D2 deletions > 0 | normal exit, callback |
| **F-B4-2** `b4-fault` | as F-B4-1 with a load from 0xFFFFFFFF at iteration k | — | handler dump (stacked r0-r3, r12, LR, PC, xPSR, CFSR) equal | precise fault |
| **F-B4-3** `b4-irq` | SysTick handler prints stacked r0-r3/xPSR while the loop runs | — | dumps (O2b) and O2a equal | interrupt recovery |
| **F-B4-4** `b4-mmio` | SCB CPUID read as a non-last insn of the loop TB (fault fixture (d) shape) | — | io-recompile count equal; the `can_do_io=false` prefix store is kept (`observed_at_sync_point`) | MMIO / can_do_io |
| **F-B4-5** `b4-it` | `it eq; moveq r1,#5; ldr r2,[r3]; movs r1,#7` with r3 faulting at iteration k and an MMIO variant | — | real pipeline keeps the `r1=5` store; the IT-block D4 mutant changes the stacked r1 (O4) | review must-fix 9 (IT negative control) |
| **F-B4-6** `b4-div0` | fault fixture (b): `sdiv` by 0 with DIV_0_TRP | — | handler dump equal (S6 kept) | precise fault via helper |
| **F-B4-7** `b4-mem` | config with `variable_watch` (memory callbacks) and a FastDyn finline/UPDATE_MEM patch in the loop | — | callback output equal; D3 refuses near callbacks; mutants deleting a guest store/UPDATE_MEM/LOG_REG rejected and visible | guest memory events, observers |
| **F-B4-8** `b4-compute` | compute held-out | — | V10 equal; per-class deltas | real workload |
| **F-B4-9** `b4-tbcb` | icount config with a virtual whose TB hook registers a TB-exec callback that raises a spare IRQ every M executions on a chained successor of a D2-eligible TB (U-TBCB) | — | no `cpu_abort`; IRQ log and O2b equal warm; the last-insn `can_do_io=1` is kept; the deleting mutant aborts (O4). If U-TBCB finds no such virtual: static mutant control only, listed as a known gap in the B4 gate report [R5] | callback / interrupt via TB-start callbacks |
| **F-B4-10** `b4-coverage` | F-B4-1 under a coverage-enabled config | — | O6: coverage equal; D1 deletions > 0 [R20] | attribution |

### 9.6 Test IDs

| ID | Test | Kind | Covers |
|---|---|---|---|
| T-B3-1 | ext table round trip: `sc_v6.py` write → both parsers read; corruption cases give the loader's die reason | unit | V11 |
| T-B3-2 | NativeLinker v2: grow every relocatable compute return/static unit with a dummy guard; V1-V5, V13, V15, V16 pass; derived `.scm` re-imports | unit | stub-after-pool, rows |
| T-B3-3 | variant relocation: clone every relocatable compute unit; V16 "identical modulo relocation" | unit | clones |
| T-B3-4 | hostsym T1-T6 reproduced, T6 analysed with the LDST abstraction; coverage metric | unit | V12/V14 |
| T-B3-5 | V18 native self-test; mutation suite 100 % rejected | unit | validator |
| T-B3-6 | pass separation: F-B3-1 with each of CP/BF/DHO disabled gives a non-zero, distinct byte delta and ledger row | acceptance | spec:116, 157 |
| T-B3-7 | identity: identity pipeline and all-`no_change` B3/B4 pipelines give sha256-equal output (V9) | acceptance | identity |
| T-B3-8 | slice dynamic: F-B3-1 O1/O2a/O2b/O3/O4 | dynamic | gate positive |
| T-B3-9 … T-B3-25 | F-B3-2 … F-B3-18 dynamic oracles, one test each | dynamic | sub-cases |
| T-B3-26 | loader C-level stress on `qint-v6`: arm, then invalidate C (debug write), invalidate R, `tb_flush` (gdb stop), `tlb_flush` (MPU write), one-insn-per-tb, plugin reset; slot reset observed via gdb diag; stats as expected | dynamic | 5.5, 5.6 |
| T-B3-27 | v5 regression on `qint-v6`: B1 T-B1-* and B2 F-B2-1…6 byte/behaviour identical | dynamic | invariant 6 |
| T-B3-28 | graph/summary round trip with `guarded_slot`/`variant_link` edges; CalleeSummary export for every return unit | acceptance | spec:43, 116 |
| T-B3-29 | Tier-1 capture identity (`.sc` byte-identical to level `reloc`) and `ops-xcheck` mismatch count on fixtures and compute, on the `qint-v6-t1` capture binary | reported (B3+) | 8.2 [R12] |
| T-B3-30 | F-B3-19 compute held-out V10 + campaign rows (timing reported, not gating) | dynamic | real workload |
| T-B3-31 … T-B3-37 | F-B3-1p, F-B3-3b, F-B3-8p, F-B3-20, F-B3-21, F-B3-22 (+22b), F-B3-13(d) dynamic oracles | dynamic | R4, R6, R16, m-CE5 |
| T-B3-38 | F-B3-23 coverage (O6) | dynamic | R20 |
| T-B3-39 | F-B3-24 `-O0`/`-Os` builds: simplifies or stable skip reason | acceptance | R18 |
| T-B3-40 | F-B3-16(d)(e) refusals and guest-hash drop | dynamic | R17 |
| T-B3-41 | rejected fixture per pass (synthetic units): CP `no_same_length_encoding`, BF no decided branch, DHO `flags_live`, `chain-returns` `target_not_profiled`, `chain-static` `no_free_slot` | unit | spec:159 [m-SP10e] |
| T-B3-42 | U-COND: condexec == 0 at every return-exit slot of compute and the fixtures (decode check) | unit | g_cond static-only argument |
| T-B4-1 | syncdead reproduces M0 = 0 under TCG's own sync set and the D1/D2/D3 static counts of the model on compute | unit | 7.3 |
| T-B4-2 | placement: no edit in prefix except the D2 store; mutant edits rejected | unit | V15 |
| T-B4-2b | the brcond-sync-store-then-reload mutant is rejected statically and changes O1 on F-B4-1 | unit + dynamic | R7 |
| T-B4-3 … T-B4-10 | F-B4-1 … F-B4-8 dynamic oracles | dynamic | B4 sub-cases |
| T-B4-11 | per-class ablation (`--param classes=D1|D2|D3`) artifacts validated and measured | acceptance | spec:157 |
| T-B4-12 | F-B4-9 TB-start callback (or the static control if U-TBCB fails) | dynamic | R5 |
| T-B4-13 | F-B4-10 coverage (O6) | dynamic | R20 |

Identity: the v6 writer is used only if at least one variant or guarded slot exists; otherwise the v5 writer runs and V9 applies unchanged (the identity-v6 arm is a separate artifact with V9' "every record and code byte equal").

---

## 10. Evaluation hooks for B5

### 10.1 Arms (all on the `qint-v6` binary; baselines re-collected)

| Arm | Content |
|---|---|
| `nocache`, `empty`, `warm` (primary reference), `identity`, `identity-v6` | as B2; identity-v6 measures loader overhead |
| `layout` | B2 pipeline re-run on the new binary |
| `b3-chain` | `chain-returns` only |
| `b3-static` | `chain-static` only (beyond the spec's return scope; reported separately) |
| `b3-spec` | `specialize-returns` + CP + BF + DHO (semantic cross-TB) |
| `b4` | `eliminate-state-sync` only (v5 file) |
| `semantic` | `b3-chain + b3-static + b3-spec + b4` |
| `combined` | `semantic + layout` |

Ablations: `--disable` of each pass on top of `semantic`, and B4 per class. Slot arbitration makes B3 ablations non-additive (stated in the report).

### 10.2 Metrics

| Metric | Model (train profile, offline) | Measurement |
|---|---|---|
| removed lookups | Σ armed/linked edge weights; re-computed on held-out profiles of the **base** snapshot (profiles of optimized artifacts are not used for training, review M11) | paired BENCH speedup per arm, ≥30 interleaved pairs, held-out and final (spec:181) |
| guard pass rate per slot | profile edge share | diag gdb counts on fixtures |
| removed env stores per class | static count × profile entries | `b4` arm speedup and per-class ablations |
| folded branches / loads in variants | count × edge weight | `b3-spec` arm |
| code growth | stub + variant bytes vs hot-set bytes; file size, allocated size, populated bytes (spec:134) | offline |
| cold-path latency | — | QEMU start → `benchmark_start` (ext parse, arming, probes) warm vs v6 |
| fallback frequency | — | appended records, `disarm_*`, refusals |
| preparation cost | wall time per analysis/pass/validation (`hostsym` dominant) | report rows |
| memory | configured arena capacity, allocated and populated bytes, peak resident memory (spec:134) | `/proc` sampling in diag runs |
| full-harness throughput | — | BENCH plus harness wall time per arm, separate from vCPU throughput (spec:183) |
| region coverage | share of train TB entries inside an armed (R, C') pair and share of lookups on guarded edges | diag stats on held-out [m-SP12] |

### 10.3 Training profitability (spec:110)

`saving(E) = Σ n_i·c_lookup − N_E·c_guard − (N_E − Σ n_i)·c_fail` per redirected exit (train profile only). `c_lookup`, `c_guard`, `c_fail` are calibrated once by WP16 on train inputs (today **U**; the profile map's only scale is 10.7 ns per TB entry). Reject with `not_profitable` when `saving ≤ 0` or the guard pass share `Σ n_i / N_E` is below 50 %. CP, BF and DHO: saving = executed non-nop host instructions removed × C' entries (train); reject ≤ 0. B4: saving = removed stores × unit entries [m-SP11]. B4 has no calibrated cost until WP16/WP25 measure it; until then validated B4 edits are always applied and the artifact is flagged `predicted_unprofitable` if the model predicts < 0.5 % of train BENCH. Candidates and profiles are frozen before held-out and final timing (spec:157).

---

## 11. Gates

**Gate binary.** Every dynamic gate item runs on the frozen `qint-v6@<sha>` (§5.9); the sha256 is recorded in both reports [R13].

**B3 spec gate** (spec:145: "A two-TB example simplifies; an effectful callee, alternate entry, interrupt clobber, fault and unseen target remain correct"). All must hold:
1. Two-TB example: T-B3-6 and T-B3-8: F-B3-1 accepted with CP, BF and DHO each showing its own non-zero byte delta and ledger row; CalleeSummary row lists the patch provenance; O1, O2a, O2b equal; O3 C' hits > 0; O4 falsified-fact control detected.
2. Sub-cases, each passing O1 and O2a/O2b under its per-mode oracle with its non-vacuity counter > 0: effectful callee F-B3-2, F-B3-3, F-B3-3b, F-B3-4, F-B3-5; alternate entry F-B3-6 (incl. tail call, recursion, handler-mode negative); interrupt clobber F-B3-7, F-B3-20; return shape F-B3-1p; fault F-B3-8, F-B3-8p, F-B3-9, F-B3-10; unseen target F-B3-11; invalidation F-B3-12, F-B3-16(a)-(e).
3. Static V1-V21 and V23 pass for every accepted artifact (V22 when Tier 1 is present); mutation suite 100 % rejected statically (T-B3-5); every mutant outside the static-only set of §9.3 detected dynamically (O4) [R6].
4. Attribution and independence: F-B3-23 (O6) [R20]; F-B3-24 [R18].
5. T-B3-27 (v5 regression on the gate binary) and T-B3-28 (graph/summary round trip incl. multi_tb rows) pass.

**B3 hardening** (in the same report): F-B3-13 (a)-(d), F-B3-14, F-B3-15, F-B3-17, F-B3-18, F-B3-21, F-B3-22, T-B3-26 loader stress. A hardening item that runs and fails **blocks** the gate (it is a correctness bug). One that cannot run because of a named unknown (U-MPS2, U-VIRT) is reported as `not_run:<unknown>` and does not block [m-P11].

**B3+ (reported outcome, not gating)** [R12]: T-B3-29 Tier-1 capture identity and `ops-xcheck` mismatches on the `qint-v6-t1` capture binary.

**B3 performance (reported, not gating):** F-B3-19: compute B3 artifacts (`b3-chain`, `b3-static`, `b3-spec`) accepted and V10-equal on held-out inputs; paired timing measured and reported (spec §6 separates measured benefit from acceptance).

**B4 gate** (spec:146: "Normal exit, callback, precise fault and interrupt recovery retain required state. Guest memory events are not deleted as bookkeeping"):
1. T-B4-1, T-B4-2, T-B4-2b pass; every B4 mutant of 9.3 rejected statically and detected dynamically (O4).
2. T-B4-3…T-B4-10, T-B4-12: F-B4-1…F-B4-9 pass O1/O2a/O2b under their per-mode oracles, with D1 and D2 deletions > 0 where the fixture allows them (F-B4-9 may be replaced by its static control only when U-TBCB fails, stated as a known gap).
3. V12 event preservation and the host_abs assertion pass on every rewritten unit of the compute artifact; F-B4-8 V10-equal on held-out.
4. T-B4-13 (F-B4-10, O6) passes.
5. T-B4-11 per-class ablations validated and measured; timing reported, not gating. The report states the spec:14 cross-TB deferral of §7.6.

---

## 12. Work packages and the first vertical slice

Full WPs in `IMPLEMENTATION-PLAN-B3B4.md` (WP16-WP30). Summary (dependencies split into unit-level / integration-level [R9]):

| WP | Content | Depends (unit) | Depends (integration) |
|---|---|---|---|
| WP16 | value probe (all admissible top edges, n = 90, diag-twin TLB gate) + E-DET (incl. O2b CVR), E-MAP, E-BP, X-REHY, X-ASYNC | — | — |
| WP17 | EnvLayout, HostOps, hcfg, hlive, effects, obs-contract; V20; RuntimeIdentity extension (`bundle_v1.py`: exe sha, EnvLayout id, CPU model, `tb_start_cb_possible`, mem_get_value fact, AC4 exclusions) | — | — |
| WP18 | NativeLinker v2 (grow, variant, same-length patches, guard emitter), v6 writer, two parsers, `.scm` chunks, `region_v1.py` multi_tb writer + schema branch, `graph.py` edges `guarded_slot`/`variant_link`; V5/V7-v6/V9'/V11/V13 (structural)/V15/V16/V17/V19/V21 | WP17 | — |
| WP19 | `hostsym` + V12/V13 (entailment)/V14/V18 + mutation suite | WP17 | WP18 (guard-atom, guard-move and variant mutants) |
| WP20 | return edges, facts (per R unit), patch-join, CalleeSummary, U-COND decode check | WP17 | WP19 (certification), WP24 (F-B3 fixtures) |
| WP21 | B3 passes, hconst, manager changes, per-pass profitability, rejected fixtures per pass | WP18, WP19, WP20 | WP24 (F-B3-1), WP16 (constants; defaults until then) |
| WP22 | QEMU v6 loader Q1-Q11 (approval); freezes the gate binary | approval | WP18 (synthetic artifacts), WP24 (stress fixtures) |
| WP23 | Tier-1 capture Q12 on `qint-v6-t1` + ops-xcheck (approval; B3+, optional) | WP22 freeze, approval | WP20, WP24 |
| WP24 | fixtures/b3 (+ per-mode oracles, -O0/-Os builds, coverage configs, mps2 alias runner, gdb diag scripts); answers U-VIRT, U-TBCB, U-MPS2 first | — | — |
| WP25 | B4 pass (syncdead incl. loads, can_do_io direct-chain rule), v5 packaging, early timing | WP17, WP18, WP19 | WP24 |
| WP26 | harness: v6 proof, `run/diag.py` extension, O1-O6, negative-control packager, arms, `allow_ram_resident` test policy, non-FastDyn import path, refusal-code pinning | WP18 | WP24, WP22 (v6 runs) |
| WP27 | first vertical slice (0a static, 0b dynamic experimental, 1 on the gate binary) | WP17-WP21 (-min), WP24 (F-B3-1), WP26 (-min) | WP22 (slice 1) |
| WP28 | B3 gate (spec gate, hardening, B3+ reported) | WP21, WP22 (frozen), WP24, WP26, WP27 | — |
| WP29 | B4 gate | WP24, WP25, WP26, WP22 (frozen) | — |
| WP30 | B5 hooks: campaign definitions and report rows | WP28, WP29 | — |

**First vertical slice (two-TB example end to end)** [R15]. F-B3-1's R is `adds r0,r0,#1; bx lr` and C is the 1-insn TB `[cbz r0, Lz]`: neither has an LDST, so no LDST abstraction and no v6 writer are needed before slice 1.
- *Slice 0a (static, current binary):* F-B3-1 built; Tier-0 capture + profile; HostOps, hostsym (no LDST), exit-facts + patch-join, `specialize-returns` + CP + BF + DHO produce C' with all static validators; per-pass byte deltas (§3.5 expected bytes).
- *Slice 0b (dynamic, current binary, experimental, never published):* a v5 copy with R's GENERAL guarded slot **and the C' patches written in place into the general C** (`experimental_unsound_in_place_specialization`). This is behaviour-preserving for F-B3-1 only: C has one predecessor, the modifier makes r0 = 0 at every entry, and no handler touches r0. Run in icount: O1 equal warm, gdb diag shows the patched C executed ≈ N times, diag twin shows 0 TLB flushes. This executes the simplified bytes before any QEMU build.
- *Slice 1 (gate binary):* v6 writer emits one variant, one SPEC slot, one link; the loader arms; O1/O2a/O2b equal; O3 `armed = 1`, C' hits ≈ N; O4 falsified-fact control; X-REHY with the variant. F-B3-7 and F-B3-9 belong to WP28.

---

## 13. Risks and mitigations

| # | Risk | Mitigation |
|---|---|---|
| K1 | **No measurable speedup** (B2 repeat): a lookup hit may be cheap; env stores are L1-resident; nops still decode | WP16 measures an all-top-edges GENERAL-slot copy and a D1 copy on the current binary (n = 90, MDE ≈ 1 %) before WP25's timing, WP30 and B5 priorities are set; WP22 proceeds regardless because the B3 correctness gate needs the v6 loader (D-EV-1); static off-page chaining (48 % of lookups) is a first-class arm; the gates do not depend on speed; null results are reported (spec:157) [R11] |
| K2 | **Validator false acceptance** (wrong concrete semantics, missing observer, wrong offset) | uninterpreted-by-default semantics; V18 native differential test; mutation suite incl. the IT-block shape; DWARF EnvLayout bound to the exe sha256 (`plugin_mem_value_high` −0x10 fixed); observation contract fails closed; icount byte-identical dynamic runs |
| K3 | **Runtime-contract gaps for direct transfers** (mapping, breakpoints, aliases, async writers) | inline g_bp and g_pc; arm-time refusal with bp/wp lists non-empty; probe; TLB hook on all three flush workers; F-B3-13/14/15 negative controls; X-ASYNC audit (G24) and AC4 |
| K4 | **Loader lifetime bugs** (jmp lists, cmpxchg vs stock, flush epochs, CF_INVALID variants) | every link uses registered slots and stock unlink paths; fail-loud ext validation; T-B3-26 stress; v5 regression T-B3-27 |
| K5 | **Spec interpretation of "native bytes as IR"** | HostOps typed only from emission-time metadata; Tier-1 ops-xcheck (WP23) as an independent derivation; the report states the position |
| K6 | Coverage: SPEC links rarely fire on real workloads (77.8 % of returns do not write r0 in the return TB; ≈7.6 % candidates) | expected: SPEC links are the gate showcase; GENERAL/static chaining is the throughput mechanism; coverage reported per arm; version chains are v2 |
| K7 | `hostsym` coverage < 99 % or slow | fail-closed `validation_inconclusive`; coverage reported; LDST abstraction prioritized in WP19 |
| K8 | Fixture effort (device read side effect, RAM SMC, mps2 run, register-writing virtual) | G25 gives the device effect; WP24 starts first and reuses the fault scaffolding; fallbacks named in 9.5 |
| K9 | Variant rehydration assumption wrong (X-REHY) | coverage-enabled diff in WP16; fallback rehydrates C' at arm time |
| K10 | icount runs not deterministic (E-DET) | O2b degrades to invariants; O1 remains |
| K11 | D2 loses most of its weight once `tb_start_cb_possible` holds | measured by WP25; the prefix class (35.6 %) is unaffected; reported per class |
| K12 | No FastDyn virtual exists for F-B3-5/22b (U-VIRT) or F-B4-9 (U-TBCB), and `src/fastdyn` may not be modified | answered first in WP24; fallbacks are static controls listed as known gaps in the gate reports |

---

## 14. Explicitly out of scope (v1), with reason codes

| Item | Reason code | Why |
|---|---|---|
| facts established before the return TB (chains of versions X→R'→C') | `fact_not_local_to_return_tb` | needs versions of R reachable only from X and B's reachability invariant; sound by 6.3 but v2 |
| Handler-mode and exception returns | `handler_mode_return` | `exit_tb(NULL,0)`/EXC_RETURN shape; 0.17 % of transitions (V-map) |
| PC-writing FastDyn modifiers | `pc_modifier_exit` | mid-TB `exit_tb(NULL,0)`, no lookup tail |
| `tbb/tbh`, `bx rX`, `blx rX` | `indirect_multiway` | outside the spec's return scope; candidate `chain-indirect` (same mechanism, top-2 targets) after WP16 |
| more than two guarded targets per exit | `slot_budget` | two registered slots per TB struct |
| fused regions, callee inlining, non-PCREL pseudo-TBs | `fused_region_unsupported` | per-member icount recovery and SMC retirement (G16) |
| B4 D4 slow-path compensation, D5 cross-TB, D6 register forwarding, D7 PC, true deletion | `needs_slowpath_compensation`, `cross_tb_dse_needs_value_transport`, `needs_regalloc`, `pc_store_required`, `needs_splicer` | not decidable by the equal-env-at-every-sync-point rule; D4 needs a dominance proof per slow path |
| non-zero constants in CP when longer than the load | `no_same_length_encoding` | same-length rule; BF still applies |
| `state_fixups` recovery | `capability_not_enabled` | not needed; cannot restore register values |
| TCG re-translation, STOKE, LLVM producers | `validation_inconclusive` until hostsym gains non-syntactic equality (v2) | producer without patch locality |
| count-limited, `CF_NOIRQ`, `CF_MEMI_ONLY`, two-page TBs | `special_cflags_unit`, `two_page_target` | entered only from the main loop; stock never chains two-page TBs |
| RAM-resident code in production | `ram_resident` | X16 exposure (G9); allowed only under the test policy of F-B3-15/17 |
| icount `shift` ≠ 0 or `auto`, `sleep=on`, `align=on`, record/replay; `-d nochain` | `unsupported_timing_model` | not qualified; the fixtures and E-DET use icount shift 0 or no icount. Under `-d nochain`, `curr_cflags` adds `CF_NO_GOTO_TB` (G10), so normal-cflags records are never entered and the artifact would be vacuous [m-SP9] |
| flat mode, split-wx, MTTCG / >1 vCPU, non-x86-64 host, CPU model ≠ cortex-m3 | `unsupported_flat`, `unsupported_threading`, `unsupported_host`, `unsupported_guest` | loader and guard assumptions |
| **Admitted configuration** | — | AC1 no gdbstub/single-step in timed arms; AC2 no one-insn-per-tb or HMP `log nochain` toggled at run time (loader refuses/disarms one-insn); AC3 cortex-m3; AC4 no thread other than the vCPU writes CPU registers or hflags while a TB runs (G24; X-ASYNC completes the audit); the `fuzz_aflnet` and ardupilot virtuals are excluded by name until X-ASYNC clears them [m-SF10] |

---

## 15. Unknowns and the experiments that settle them (all light: single runs, `setarch x86_64 -R`, timeout, copies under the maintainer's evaluation data (not in the repository))

| Id | Unknown | Experiment | Owner |
|---|---|---|---|
| E-COST | `c_lookup`, `c_guard`, value of D1 removal | WP16: hand-built v5 copy of the compute snapshot with GENERAL guarded slots on **every admissible return and static exit of the train top set** (≤ 2 targets each; the modelled share of removed lookups is stated before timing), and separately a copy with the D1 dead stores of the hot units nop'ed. n = 90 interleaved train pairs per copy: B2's design gave a 95 % half-width of ≈ 0.6 % at n = 90, so the minimum detectable effect is ≈ 1 % (I). ≤ 4 cores. Each timed run has an untimed diag twin showing 0 `tlb_flush*`/`tcg_commit_cpu` calls after `benchmark_start`; a run without it is discarded [R11, m-CE7] | WP16 |
| E-DET | icount determinism of outputs, `-d int` traces and the O2b handler records (incl. SysTick CVR) | two warm runs of F-B3-1, F-B3-7 and compute train-1, diff | WP16 |
| E-MAP | TLB flushes / memory commits after `benchmark_start` (disarm cost) | `QEMU_SC_V6_STATS` on compute and blinky (on the current binary: gdb `-nx` counting of `tlb_flush*`) | WP16 |
| E-BP | breakpoint/watchpoint lists empty in production runs | gdb `-nx` read of `first_cpu->breakpoints.tqh_first` after `benchmark_start` | WP16 |
| X-REHY | per-activation vs per-execution rehydration side effects | coverage-enabled FastDyn config: compare coverage/log output of warm vs a slice-1 artifact | WP16/WP27 |
| X-ASYNC | completeness of G24 | full audit of `src/fastdyn` virtuals/device models used by fixtures for register/hflags writers off the vCPU thread, including `fuzz_aflnet.c:143`, `ardupilot.c:94`, `gcs_receiver.c:322-323` | WP16 |
| ~~U-MMU~~ | closed: the lookup's fetch uses `EX_TBFLAG_ANY(env->hflags, MMUIDX)` (§4.6, V-c) | WP22 keeps a debug assert | — |
| U-COND | condexec == 0 at every return-exit slot (g_cond static-only argument) | decode check over every compute return unit and the fixtures (T-B3-42) | WP20 |
| U-TBCB | an existing FastDyn virtual whose TB hook registers a TB-exec callback that raises an IRQ (F-B4-9) | survey `variable_watch`, `object_sanitizer`, `tests/plugins/mmio_count` and the virtual SDK for out-of-tree loading; `src/fastdyn` is not modified | WP24 (week 1) |
| U-TLBI | A-profile TLBI paths (`target/arm/tcg/tlb-insns.c`) unreachable on cortex-m3 (I: no cp15 on M) — covered anyway, they call the hooked workers | read registration guards | WP22 |
| U-COV | `hostsym` coverage of compute steady weight | WP19 metric | WP19 |
| U-VIRT | an existing non-RW_CFI FastDyn virtual that writes r0 (F-B3-5) or xPSR (F-B3-22b) | survey `fastdyn/virtuals` (`virtual_write_register` users), check the RW_CFI flag | WP24 (week 1, before the fixture is written) |
| U-MPS2 | integrated QEMU runs the mps2-an385 alias image with `-accel tcg,supercache=` without FastDyn | one run | WP24 |
| U-HELD | held-out stability of hot return/static edges | rerun the maintainer's evaluation data (not in the repository) on held-out profiles | WP21 |

---

## 16. Decisions and rejected alternatives (with the review concern behind each)

J-S = soundness review (winner C); J-F = feasibility review (winner A). Where they disagree, the decision column says which concern prevailed.

| # | Decision | Rejected alternative | Driver |
|---|---|---|---|
| D-B3-1 | Base = C's soundness model (full run-time key guard, no B4 compensation, host-level validator, TLB disarm) on A's mechanics (stub after pool, same-length clones, loader-only SPEC slots, minimal loader) | A as written (static S3 hflags, MPU guard, D4, no TLB hook); C as written (general splicer, 128-byte header, 8-cmp guard); B (TCG re-translation) | J-S: A's D4 unsound, A-MAP unproven; J-F: C's splicer is a second linker on the critical path; both: B's `max_end`, breakpoint-arming and alias defects plus a 2-2.7 k-line TCG-core trust base |
| D-B3-2 | Inline guard = g_bp, g_thumb, g_cond, g_flags, g_flags2, g_pc; no singlestep or watchpoint compare | C's 8-compare guard incl. singlestep/watchpoints; A's MPU bit + static hflags; B's PC-only guard with retire hooks | J-S must-fix 1 (breakpoint check on every direct transfer), A3 (static hflags makes the callback fixture vacuous); J-F: singlestep compare redundant (G10), guard cost; watchpoints are covered by the TLB hook (G11) |
| D-B3-3 | Guarded slots ship only in v6 artifacts with the TLB hook; the v5 GCE exists only as the WP16 experiment, labelled `experimental_unsound_without_v6_tlb_hook` | A's "GCE is sound on the stock runtime" in v5 | J-S A2, must-fix 5; J-F M4 |
| D-B3-4 | TLB hook in all three flush workers (full, page, range) | hooks only at cputlb.c:395, 745 (C, B) | J-S C-g3 / must-fix 5: page and range paths verified to bypass 395/745 (G12) |
| D-B3-5 | 80-byte header, feature flags in `reserved` (offset 68), ext table at 0x100 | C's 128-byte header + record-checksum change; B1 §4.9's table after `record_capacity*104` | J-S graft (no record-checksum change needed); the append path has no capacity check; A's offset 0x4c corrected to 68 (G1) |
| D-B3-6 | One own struct per variant with C's rows/icount; icount supported; primary measurement in icount | B1 §4.9/D24 non-PCREL pseudo-TB, "icount regions unsupported" | all three candidates and both reviews: stock rewind is per-TB exact only (G16); every fixture TB is icount (G26) |
| D-B3-7 | Variant code folded into `max_end` before the code pread; validated by V11 | B's "max_end unchanged" | J-S/J-F must-fix 2 (G3) |
| D-B3-8 | SPEC slot reset path = `jmp F` (loader-only); GENERAL slot reset = `exit_tb(R|n)` | C's SPEC reset `exit_tb(R|n)` (stock may claim it for an epoch) | J-S C-g6 non-vacuity graft from A |
| D-B3-9 | Arm-time refusal while bp/wp lists are non-empty **and** the inline g_bp | B's arm-only retire hooks | J-S B2 (gdb stop flushes, breakpoints persist, re-arm misses the breakpoint) |
| D-B3-10 | g_pc on every guarded slot, static exits included | B's unguarded `LinkCrossPage`/structural `R15 == V` | J-S B3 / must-fix 6 (G19, G20) |
| D-B3-11 | Facts local to the return TB in v1, with the corrected rationale; version chains v2 | C's rule with the "handler rewrites the frame" rationale; B's X→R'→C' in v1 | J-S C-g4 (rationale wrong, rule conservative); J-F: B's chains are not on the slice path |
| D-B3-12 | Two derivations of each fact (HostOps proposer + hostsym certifier) + patch-join provenance, Tier-1 as cross-check | Tier-1 as a prerequisite (B); A's interpreter-only V17 | J-F G1/G3; J-S: validator on the bytes that run |
| D-B3-13 | Malformed v6 extension → fail loud (exit before guest execution) | C's "ignore table, refuse guarded records" (needs per-record flags in pad bytes not covered by the record checksum, G1); A's "warn and run as v5" (guarded slots without hook) | J-S graft from B; G1 |
| D-B3-14 | Three separate simplification passes over one analysis (`hconst`) | B's report-only DeadHostOps | J-F M5 (spec:76, 116, 157) |
| D-B3-15 | `chain-static` first-class but separately reported; `tbb` out of v1 | A's `chain-indirect` in v1; C's optional static | J-F G4 (48 % of lookups); scope: spec names return edges |
| D-B3-16 | `QEMU_SC_V6_STATS` counters for non-vacuity; gdb only in diag fixture runs | A's gdb-only O3 | J-F G5, J-S graft |
| D-B3-17 | Retire on one-insn-per-tb set and on plugin reset (forbid) | none (A), full retire hook set (B) | J-S graft from B; G10 |
| D-B4-1 | No compensation code (no D4) in v1; validator proves equal env at every real sync point | A's D4 slow-path compensation | J-S A1 (IT-block counterexample), must-fix 3 |
| D-B4-2 | D2 may delete the single `can_do_io` store in the prefix; all other prefix bytes frozen | C's blanket prefix ban (contradicted by its own T5) | J-F M3 (35.6 % of D2 weight in the prefix) |
| D-B4-3 | `GuestAccess` carries the S5-observable env; host_abs operands asserted outside env/CPUState | C's event without env | J-S C-g1, C-g2 / must-fix 4 |
| D-B4-4 | Same-length nop-fill only (no splicer) | C's splice-delete | J-F: splicer cost; true deletion `needs_splicer` (v2) |
| D-B4-5 | B4-only artifacts are v5 files | requiring v6 for B4 | J-F: early measurement on the current binary; no guarded slot ⇒ no loader dependency |
| D-B4-6 | EnvLayout from DWARF with `plugin_mem_value_high = −0x10` | A's −0x14 | J-S F13 / must-fix 8, J-F M7 |
| D-EV-1 | WP16 value probe (all admissible top edges, n = 90) precedes WP25's timing, WP30 and the B5 priorities; it does **not** gate the WP19/WP22/WP25 builds, because the spec gates B3/B4 on correctness. Threshold: measured paired speedup ≥ 1.0 % with the 95 % CI excluding 0, separately for the guarded-slot copy and the D1 copy; below it the report records the null result and B5 priorities are re-planned [R11] | probe before the loader build (old text, contradicted by the plan); a 10-pair probe on two edges (underpowered: half-width ≈ 1.8 %) | J-F M1 after G27 (B2 null result); review P4 |
| D-EV-2 | All arms on `qint-v6`; profiles for training from the base snapshot only | reusing B2 timings; profiling optimized artifacts | J-F M10, M11 |
| D-B3-18 | Variants per (R unit, exit, C unit, fact set), one pred each; vaddr keying only for ranking | sharing a variant across suffix units (old §3.1) | review CE-F1 (budget-boundary IRQ counterexample) [R4] |
| D-B3-19 | Feature-bit check in `sc6_load_ext` (fail loud); `valid_header` accepts 5 and 6 | check inside `valid_header` | review SF-F2 (silent truncate) [R2] |
| D-B3-20 | Q9 sets an atomic flag and queues `async_safe_run_on_cpu` disarm | disarm in the setter | review SF-F3 (monitor thread) [R3] |
| D-B3-21 | Tier 1 optional (B3+ reported), on a separate capture-only branch | Tier 1 as a gate item | review P5/P6 [R12, R13] |
| D-B3-22 | Guard atoms g_flags/g_flags2 (and g_cond without U-VIRT) kept as defence in depth with static-only mutant controls | dropping them; claiming dynamic detection via privilege drop | review CE-F3; R itself is re-keyed by a privilege drop (§9.3) [R6] |
| D-B4-7 | `can_do_io` observable at direct-chain exits when `tb_start_cb_possible` (fail closed) | "every exit rewrites can_do_io"; never deleting the last-insn store | review CE-F2 (TB-start callbacks) [R5] |
| D-B4-8 | Cross-TB store elimination (D5) deferred with stated evidence; cross-TB load elimination only via CP on certified edges | adding D5 with compensation in v1 | invariant 4; review SP-F16 [R21] |

Departures from B1 §4.9 / D24 (spec requires stating them): per-variant own struct instead of a non-PCREL pseudo-TB (D-B3-6); no per-exit synthetic stub TBs (every link is a registered slot of R, and C' exits through its own slots); no region patch-site registry (no hard-coded cross-TB jump outside registered slots); inline breakpoint check instead of a `regions_enabled` byte (no hook site exists for it); `cflags_next_tb` guard omitted (always −1 at exits); ext table at 0x100 instead of after `record_capacity*104`; no `state_fixups`; lazy arming only when both ends were activated by the stock path (no eager member activation, which would need per-member vaddrs and can fault).

---

## 17. Review must-fix items and where each is resolved

| Must-fix (source) | Resolution | Section |
|---|---|---|
| No direct transfer bypasses `check_for_breakpoints` without a run-time bp check; arm-only paths refuse while bp non-empty (J-S 1) | inline g_bp on every guarded slot; arm refusal with bp/wp non-empty; F-B3-13(a) | 4.1, 4.6, 9.5 |
| Fold variant extents into `max_end` before pread/`code_gen_ptr`; validate offline (J-S 2, J-F M2) | Q2/Q4 in `alloc_code_buffer` before `loaded_max_end`; V11 | 5.4, 9.1 |
| No D4 in v1; later only with dominance + path enumeration (J-S 3) | D4 out (`needs_slowpath_compensation`); IT mutant in the suite; F-B4-5 | 7.3, 9.3 |
| `GuestAccess` carries S5 env; host_abs outside env (J-S 4) | V12 rules | 7.5, 9.1, 9.2 |
| Hook every TLB-flush path; keep arm-time probe; no cross-page links without a hook (J-S 5, J-F M4) | three workers hooked (G12); probe (G23); v5 GCE experimental only | 5.5, 5.6, 16 D-B3-3/4 |
| Absolute-R15 assumptions need a PC compare or an alias admission (J-S 6) | g_pc everywhere; V20 records aliases; F-B3-15 | 4.1, 9.5 |
| Keep `ram_resident` excluded or reset slots of an invalidated R (J-S 7) | excluded; test policy only | 5.6, 14 |
| `plugin_mem_value_high` = env−0x10 (J-S 8, J-F M7) | G21, EnvLayout | 0, 7.2 |
| Negative-control fixtures: IT-block, breakpoint across gdb stop, mps2 alias; interrupt/callback non-vacuity counters (J-S 9) | F-B4-5, F-B3-13, F-B3-15; stats counters in F-B3-4/F-B3-7 | 9.5, 5.8 |
| Settle async writers, icount determinism, lookup-vs-guard cost (J-S 10, J-F M8, M12) | G24 (partial V) + X-ASYNC; E-DET; E-COST in WP16 | 15 |
| Value probe first, ≥10 pairs, re-plan if < 1 % (J-F M1) | WP16, revised: all top edges, n = 90, CI threshold, does not gate builds (D-EV-1) | 12, 15, 16, plan |
| D2 prefix store class (J-F M3) | D-B4-2 | 7.3 |
| Separate CP/BF/DHO with on/off rows (J-F M5) | 3.5, T-B3-6 | 3.5 |
| Keep B3 performance case separate from the gate (J-F M6) | arms `b3-chain`, `b3-static`, `b3-spec`, `b4`; gates do not depend on speed | 10, 11 |
| X-REHY (J-F M9) | experiment + fallback | 5.5, 15 |
| Re-collect all baselines on the new binary (J-F M10) | invariant 6, D-EV-2 | 1, 10 |
| Training profiles from the base snapshot (J-F M11) | 10.2 | 10 |

---

## 18. Verification log

Re-read in the WP-QINT QEMU tree (HEAD 1258c6a3f2): tcg/supercache.c:25-80, 100-148, 268-330, 385-450, 585-730, 740-900, 949-975, 1090-1098; accel/tcg/cpu-exec.c:355-420, 440-456, 630-672, 925-937, 960-1015; accel/tcg/cpu-exec-common.c:36-76; accel/tcg/tb-maint.c:800-960; accel/tcg/cputlb.c:371-400, 520-616, 720-800, 1270-1292, 1361-1366, 1415-1463, 1541-1563; accel/tcg/translator.c:40-122, 195-236; accel/tcg/translate-all.c:180-195, 576-612; accel/tcg/tcg-all.c:250-263; target/arm/tcg/hflags.c:550-635; target/arm/cpu.h:2954-3018 (grep); target/arm/tcg/translate.c:990-1000, 2780-2792; target/arm/tcg/tlb_helper.c (`arm_cpu_tlb_fill` probe branch); tcg/region.c:240-262, 400-430; tcg/tcg-op-ldst.c:160-230; tcg/sc-capture.c:578-592; tcg/tcg.c:6937 (grep); accel/tcg/plugin-gen.c:353 (grep); plugins/api.c:630-662, 734-760; plugins/core.c:258, 711; plugins/loader.c:381 (grep); cpu-common.c:385-420; cpu-target.c:30-56; gdbstub/system.c:168-182; system/physmem.c:2750-2765; system/watchpoint.c:50-60; hw/arm/mps2.c:205-214; hw/arm/cortexm_soc.c:98-108; hw/arm/armv7m.c:590-625; hw/intc/armv7m_nvic.c:474-495, 525-545, 2450-2465, 2645-2656 and the `tlb_flush` call list; hw/timer/armv7m_systick.c:72-84.
FastDyn (grep + read): device_models/common/hw_session.c:65-95, 180-200; device_models/twintrace/twintrace.c:204-250, 465-480; core/core.c:514-523, 974-994, 1285-1300; virtuals/runtime_sdk.c:124-143; no use of `qemu_plugin_mem_get_value`; `qemu_plugin_register_vcpu_mem_cb` in variable_watch.c:146 and object_sanitizer.c:215.
scopt: passes/base.py:20-80; manager.py:661-662 (grep); run/arms.py:103-104 (grep).
Files read: the soundness review's `offsets.txt`; design C's `dis-r0pairs.txt` (both maintainer's evaluation data, not in the repository); `fixtures/fault/README.md`.
Not run: no QEMU run, no build, no timing. Everything marked V-j or V-map was taken from the reviews' or maps' verification and not re-read.

Revision re-reads (review resolution), in the WP-QINT QEMU tree: target/arm/helper.c:9912-9920; target/arm/tcg/m_helper.c:708-714, 1400-1405; tcg/supercache.c:605-655; accel/tcg/tcg-all.c:250-264; system/runstate-hmp-cmds.c:40-66; accel/tcg/translator.c:145-170, 205-222; accel/tcg/tcg-accel-ops-icount.c:145-160; accel/tcg/icount-common.c:104-114; gdbstub/system.c:160-185; target/arm/tcg/translate.c:7360-7372; accel/tcg/cpu-exec-common.c:45-56; target/arm/gdbstub.c:112, 398-441; target/arm/cpu.h `xpsr_write` (grep); accel/tcg/cputlb.c:1434-1452; plugins/core.c:255-260, 676-716; plugins/loader.c:375-383. FastDyn: virtuals/runtime_sdk.c:209-215; virtuals/virtuals.c:114, 165 (grep); `pthread_create` in virtuals (grep). scopt: run/deploy.py:30-45 (refusal codes; grep finds no `stale_dependency`); validate.py:558-570; formats/region_v1.py:195-253 and `schemas/cache_region.v1.json` (kind enum has `multi_tb`; `extra_guards`, `state_fixups`, `facts` untyped). Maintainer's notes and evaluation data (not in the repository): map-return-edge-profile.md:50-54, 518-523, 610-616; state-sync-traffic `sync_traffic.py`:284-292; fixtures/fault/qualify.sh; project specification:12-16, 56-64, 116-122, 143-147, 165-169, 183-186. Marks `V-c` are review verifications not repeated here. Still no QEMU run, no build, no timing.

---

## 19. Review resolution

Sources: the three reviews of the draft (not in the repository): source facts (SF), counterexamples (CE), spec coverage and plan (SP findings F*, plan findings P*). Each finding was checked against the cited source before it was resolved. **Result: 21 of 21 major findings valid and fixed (R6 with one sub-proposal rejected); 30 minor findings applied; 0 findings rejected outright.**

### 19.1 Major findings

| Id | Finding (source) | Verified | Resolution | Where |
|---|---|---|---|---|
| R1 | O2 claimed `-d int` shows icount/PCs (SF-F1) | V: helper.c:9918-9919, m_helper.c:712, 1403 carry neither | O2 split: O2a = exception sequence only; O2b = guest handler record (stacked PC, xPSR, named registers, SysTick CVR, iteration counter), exact in icount, invariants in noicount; E-DET also covers CVR | G31, §9.4, §9.5, §15 |
| R2 | Feature-bit check inside `valid_header` truncates silently (SF-F2) | V: supercache.c:611-635 | `valid_header` accepts 5 and 6; feature bits and EXT_TABLE checked in `sc6_load_ext` with `error_report`+`exit`; warning when a v6 file is discarded | §5.2, §5.4, §5.9 Q1/Q2, D-B3-19 |
| R3 | Q9 disarm runs on the monitor thread (SF-F3) | V: tcg-all.c:256-262, runstate-hmp-cmds.c:43-64 | setter sets an atomic flag only; disarm via `async_safe_run_on_cpu(first_cpu, …)`; "cannot race" restricted to TLB hooks and exclusive-context work | G32, §5.6, §5.9 Q9, D-B3-20 |
| R4 | Suffix-TB duplicates sharing a variant is unsound (CE-F1) | V: map-return-edge-profile.md:52 (132 units / 50 addresses), :614 (the keying recommendation); IRQ at budget boundary (I from cpu-exec.c:920-936, G14) | variants per (R unit, exit, C unit, facts), one pred each; vaddr keying only for ranking; per-unit V14; F-B3-20 `suffix-clobber`; shared-variant mutant | §3.1, §3.4, §9.3, §9.5, D-B3-18 |
| R5 | D2 "every exit rewrites can_do_io" false for TB-start callbacks (CE-F2) | V: translator.c:150-154 vs 211-220; tcg-accel-ops-icount.c:150-158; icount-common.c:107-113; runtime_sdk.c:211-214; virtuals.c:114, 165 | `can_do_io` observable at direct-chain exits when `tb_start_cb_possible` (RuntimeIdentity fact, fail closed, cross-checked with Tier 0); §9.2 forces 1 on the original side only; mutant + F-B4-9 (or static control if U-TBCB fails) | G30, §7.1-7.4, §9.1 V20, §9.2, §9.3, §9.5, D-B4-7 |
| R6 | Guard-atom mutants undetectable by listed fixtures (CE-F3) | V: gdbstub/system.c:176-183 (PAUSED no flush), translate.c:7367-7370; plus G33 (M sysreg gdb writes no-op, xPSR write no hflags rebuild) | F-B3-13(d) (PAUSED stop, breakpoint and r0 write while armed) detects g_bp; F-B3-22 `invstate` detects g_thumb; F-B3-22b detects g_cond if U-VIRT finds an xPSR writer; g_flags/g_flags2 (and g_cond otherwise) are static-only mutants with a written argument; O4 and gate item 3 aligned. **Rejected sub-proposal:** F-B3-21 `priv-drop` as a g_flags detector, because a privilege drop before the call re-keys R itself (the grown R is never entered after the drop); F-B3-21 is kept as a mode-boundary correctness fixture | §4.6, §9.3, §9.4, §9.5, §11, D-B3-22 |
| R7 | B4 dataflow ignores env loads (CE-F4) | V: sync_traffic.py:289-292 treats loads as uses; §7.3 did not | `ENV_LOAD` overlap ⇒ need; unclassified rbp ⇒ ALL; mutant T-B4-2b on F-B4-1 | §7.3, §9.3, §9.6, §11 |
| R8 | Gate components without an owner (SP-P1) | V: grep of the plan; validate.py:564-565 accepts only LOADER_V5 | V19, V5/V7-v6/V9', `region_v1.py` multi_tb writer, `graph.py` edges → WP18; V20 and RuntimeIdentity (`bundle_v1.py`) → WP17; test policy, non-FastDyn import, refusal codes → WP26; file-owner table in the plan | §12, plan |
| R9 | Missing dependencies (SP-P2) | V: plan lines cited by the review | unit vs integration dependencies per WP; edges WP20/21/25→WP24, WP23→WP24/WP20, WP22→WP24, WP19→WP18, WP26→WP24 | §12, plan graph |
| R10 | WP17 acceptance needs WP25's syncdead (SP-P3) | V: plan WP17/WP25 | M0 = 0 moved to WP25; WP17 acceptance: every HostOps `ENV_STORE` site matches a captured sync mark | plan WP17, WP25 |
| R11 | Value probe underpowered, ambiguous, contradicts design order (SP-P4) | V: plan WP16 (≥10 pairs), design D-EV-1/K1; G27 noise | all admissible top edges; n = 90 (MDE ≈ 1 %); threshold ≥ 1.0 % with CI excluding 0; the probe gates timing and B5 priorities, not builds (design text changed) | §13 K1, §15 E-COST, D-EV-1, plan WP16 |
| R12 | Tier 1 optional but gating (SP-P5) | V: design §3.6/V22 vs §11 item 4 | T-B3-29 is a reported B3+ outcome; WP28 no longer depends on WP23 | §11, §12, D-B3-21 |
| R13 | Binary not pinned across WP22→WP23 (SP-P6) | V: plan ground rules; §5.9 runtime_signature | frozen gate binary `qint-v6@<sha>` after WP22; Tier 1 on separate branch `qint-v6-t1`; any later commit voids dynamic evidence | invariant 6, §5.9, §11, plan |
| R14 | WP24 exact-norm rule fails in noicount (SP-P7) | V: fixtures/fault/qualify.sh:3-21 | per-mode oracles in `expected.py` | §9.5, plan WP24 |
| R15 | Slice not minimal; slice 0 never runs C' (SP-P8) | V: plan WP27 | exact "-min" function lists; slice 0b executes the simplified bytes via a labelled in-place v5 patch of C (F-B3-1 only); F-B3-7/9 moved to WP28 | §2, §12, plan WP27 |
| R16 | No dead-result effectful-callee fixture (SP-F1) | V: spec:58; F-B3-2/3 keep the value live | F-B3-3b (SYST_CSR read into r0 overwritten by the patch; faulting guest-load twin), arms spec and combined; dead-LDST mutant | §9.3, §9.5, §11 |
| R17 | Two invalidation triggers untested; `stale_dependency` does not exist (SP-F2) | V: run/deploy.py:37-41, grep | F-B3-16(d) callee rebuilt, (e) coverage-enabled config; existing refusal codes used; WP26 pins the exact code | §5.7, §9.5, §9.6 |
| R18 | Guest-compiler independence missing (SP-F3) | V: spec:62; fixtures `-O1` | independence argument stated; F-B3-24 builds F-B3-1/2 at `-O0` and `-Os` | §9.5, §11 |
| R19 | CacheRegion multi_tb underspecified, no owner (SP-F4) | V: region_v1.py emits only tb_singleton; schema arrays untyped | §3.9 field-by-field mapping with GuardAtom/FactRef item schemas; writer in WP18; V19 checks one row per armed link | §3.7, §3.9, plan WP18 |
| R20 | Coverage attribution not a gate item (SP-F5) | V: design X-REHY only in WP16/WP27 | O6; F-B3-23 and F-B4-10 under a coverage config are gate items | §9.4, §9.5, §11 |
| R21 | B4 skips spec:14's cross-TB case (SP-F16) | V: spec:14; design §7.3 | deferral of D5 stated with the reason (S1 entry check and guard-fail path observe env; compensation forbidden) and the evidence that would justify it; CP on certified edges counted as cross-TB load elimination | §7.6, D-B4-8 |

### 19.2 Minor findings (all applied)

| Id | Finding | Resolution |
|---|---|---|
| m-SF4 | U-MMU already answered | probe uses `FIELD_EX32(G->flags, TBFLAG_ANY, MMUIDX)`; U-MMU closed (§4.6, §5.5, §15) |
| m-SF5 | `plugins/core.c:711` is user-mode exit | Q11 hooks loader.c:381 and core.c:258 (§5.6, §5.9) |
| m-SF6 | not every gdb stop flushes | G11 reworded; F-B3-13(d) |
| m-SF7 | G18 list incomplete | full list incl. rebuild helpers (G18) |
| m-SF8 | probe function choice | `probe_access_full_mmu`, ram_addr from host pointer, PCREL page_addr0 note (G23, §4.6) |
| m-SF9 | end_offset bound must be strict | `<` (§5.3) |
| m-SF10 | thread list incomplete | ardupilot/fuzzer sites in G24, X-ASYNC, AC4 exclusion by name |
| m-SF11 | G22 wording | every guest access of plugin-translated TBs (G22) |
| m-SF12 | RW_CFI writer ends the TB | F-B3-5 requires a non-RW_CFI writer; U-VIRT updated |
| m-SF13 | `run/diag.py` exists | WP26 extends it (plan) |
| m-SF14 | restore also writes syndrome | G16, §6.2 |
| m-CE5 | `pop {pc}`, tail call, recursion, handler-mode fixtures | F-B3-1p, F-B3-8p, F-B3-6 sub-phases |
| m-CE6 | variants cloned from grown units carry unhooked slots; `tb_reset_jump on G's` | TLB hook also unlinks variant slots; invariant stated; "Cp's slots" (§5.5, §5.6, §5.9 Q7) |
| m-CE7 | WP16 probe has no per-run soundness gate | diag twin with 0 TLB flushes required (§2, §15, plan) |
| m-CE8 | `ldr pc,[rX]` is not a return | `indirect_multiway` unless base is SP (§3.1) |
| m-SP6 | helper clobber may be vacuous | U-VIRT answered in WP24 week 1; known-gap reporting (§15, K12) |
| m-SP7 | return-to-loop rule implicit | §3.1 rule 6 + V13 assertion |
| m-SP8 | B4 debug boundaries; special cflags units | §7.4 row; §7.3 `special_cflags_unit` skip |
| m-SP9 | excluded timing models incomplete | §14 row `unsupported_timing_model` |
| m-SP10 | interface obligations (a-f) | payload `origins`/`deps_added` (§3.7); UnitView rationale (§8.1); `hlive@1` and declared analysis deps (§3.5); V23 (§9.1); T-B3-41 rejected fixtures per pass; S4 wording (§7.1) |
| m-SP11 | profitability partial | per-pass and B4 savings; pass-share definition (§10.3) |
| m-SP12 | metrics incomplete | memory, full-harness throughput, region coverage (§10.2) |
| m-SP13 | DHO definition contradictory | DHO never touches memory operands; only CP removes env loads; expected F-B3-1 deltas (§3.5) |
| m-SP14 | CalleeSummary scope | `callee` optional; `patches[]` lists every patch in the callee (§3.3) |
| m-SP15 | consistency gaps (a-c) | reason codes `unsupported_contract_fact_source`, `no_patch_bindings` (§3.8); clone pointer binding in V16 and §3.9 |
| m-P9 | non-pass/fail criteria | labelled "reported outcome" in the plan |
| m-P10 | timing rules contradict | one timing rule with per-WP CPU budget (plan) |
| m-P11 | gate mixes spec and hardening | spec gate / hardening / B3+ split; hardening failures still block (§11) |
| m-P12 | shared files without owner | file-owner table (plan) |
| m-P13 | B4 measured in parallel with B3 | stated and justified in the plan (B4-only artifacts are v5; separate category) |

### 19.3 Scope changes caused by the review

- **Narrowed:** variant sharing across R units removed (R4); D2 loses the last-insn `can_do_io` store before direct-chain exits whenever TB-start callbacks are possible (R5); Tier 1 removed from the gate (R12); the probe no longer gates builds (R11); B4 skips special-cflags units (m-SP8); `ldr pc,[rX]` moved out of the return class (m-CE8).
- **Added:** CacheRegion multi_tb mapping (R19); O2b and O6 oracles (R1, R20); fixtures F-B3-1p, 3b, 8p, 13(d), 16(d)(e), 20, 21, 22(/22b), 23, 24 and F-B4-9, F-B4-10; tests T-B3-31…42, T-B4-2b, 12, 13; V23; reason codes `unsupported_contract_fact_source`, `no_patch_bindings`, `unsupported_timing_model`; static-only mutant class with written arguments (R6).
- **Stated, not built:** cross-TB store elimination D5 deferred with evidence criteria (R21).

---

## Implementation note (WP22, unit stage, 2026-11-17): the `.sc` v6 loader as built

Added by WP22; the sections above are unchanged. Commit `4671d5ceda` on `qint-v6` (the gate QEMU tree), patch `qemu-sc-v6-loader` (now QEMU commit `934890ed51`). Not frozen yet (no `qint-v6-gate-1` tag).

Where the code follows reality instead of the text above, or adds to it:

1. **MMU index of the arm-time probe (§4.6, §5.5).** `tcg/supercache.c` is built once for all targets (`system_ss`), so `FIELD_EX32(G->flags, TBFLAG_ANY, MMUIDX)` cannot be spelled there. The loader uses `extract32(G->flags, 4, 4)` (the same field: `FIELD(TBFLAG_ANY, MMUIDX, 4, 4)`, target/arm/cpu.h:2960) and `sc6_load_ext` refuses any target other than `arm` (`v6 needs the arm target`).
2. **Extra load-time rules (all fail loud, all hold for a correct packager):** `guard_off + guard_len <= R.tc_size` (not only `guard_off < tc_size`; the stub sits before R's rows); R's `jmp_insn_offset[n]` (+4) and `jmp_reset_offset[n]` lie inside the guard range; the rel32 field is 4-byte aligned; no two slots name the same `(R, n)`; VARIANTS set iff `n_variants > 0` and GUARDED_SLOTS set iff `n_slots > 0` (the "missing feature bits" rule of §5.1 made concrete); variant `flags` bits other than bit0 and every `reserved` field must be 0; a link's `target_vaddr` must have the page offset of C's `page_addr0`; duplicate `tb_offset` among valid records refuses a v6 file. **Artifact id:** the loader has no sidecar to compare `artifact_id8` with; it only refuses an all-zero id and reports the id in the stats file. The comparison with the sidecar belongs to deploy (WP26).
3. **Disarm budget and `disarmed_epoch` (§5.6).** A TLB flush with nothing live (e.g. CPU reset before any activation) does nothing. A flush that unlinks only GENERAL slots does not count towards `SC6_MAX_DISARMS`; only flushes that clear at least one armed SPEC link do. As written in §5.6, only links that were armed get `disarmed_epoch`; a link that was not armed at the flush can still be armed later in the same epoch by an activation, with a fresh §4.6 probe under the new mapping.
4. **§5.6 invariant.** Asserted (`g_assert`) only under `CONFIG_DEBUG_TCG`; the gate build has `debug_tcg=false` (same options as the WP-QINT QEMU tree). In that build the check runs after every disarm whenever `QEMU_SC_V6_STATS` is set and is reported as `invariant_checks` / `invariant_violations`; WP22 diag runs and the T-B3-26 stress test use those counters.
5. **Q11 declaration.** `plugins/loader.c` is built once for all targets and cannot include `tcg/supercache.h`; `tcg_supercache_v6_forbid()` is declared in the new target-independent header `include/tcg/supercache-v6.h`. There is no user-mode definition: `qint-v6` builds system mode only (as `qint` does). Both Q11 sites run right after `tb_flush`, so the forbid finds nothing live; it only blocks arming for the rest of the run.
6. **Stats (§5.8).** `QEMU_SC_V6_STATS` must be an absolute path (else exit). The JSON (`qemu-sc-v6-stats@1`) is written by an `atexit` handler, also when the loader exits on a malformed table. Counters beyond §5.8: `activations`, `tlb_hook_calls`, `variants_built`, `slots_unlinked`, `variant_slots_unlinked`, `variant_exits` (any return to the main loop whose `last_tb` is a variant; `variant_entry_exits` is the `TB_EXIT_REQUESTED` subset), per-link `armed` / `disarmed_epoch`, `forbid_reason`.
7. **Appends to a v6 file.** A v6 file that gets new records keeps version 6 and its feature flags (the header is rewritten from the loaded one; the extension table is not touched and its checksum does not cover the header). New files are still version 5.
8. **G21 offsets** were re-read with `gdb -nx` on the `qint-v6` binary: `regs[15]` +0x3c, `thumb` +0x14d (1 byte), `hflags.flags` +0x150 (u32), `hflags.flags2` +0x158 (u64), `condexec_bits` +0x228 (u32), `breakpoints` −0x2980, `watchpoints` −0x2970, `can_do_io` −0x4, `sizeof(TranslationBlock)` 152: unchanged.

## Implementation note (WP17, unit stage, 2026-11-17): EnvLayout, HostOps, hcfg/hlive/effects, obs-contract, RuntimeIdentity v2, V20 as built

Added by WP17; the sections above are unchanged. Branch `wp17` of `tools/scopt` (a development worktree); evidence in the maintainer's evaluation data (not in the repository) (`verdict.json`).

Where the code follows reality instead of the text above, or adds to it:

1. **EnvLayout (G21, §8.1).** `scopt.analysis.envlayout.produce_envlayout` flattens `ptype/o struct CPUArchState` and `ptype/o struct CPUState` (gdb `-nx -batch`, cwd a fresh temp dir) and expands typedef'd members (`CPUARMTBFlags`, `IcountDecr`, `CPUTLB`, …); 2,904 leaf fields for the `qint` binary, id `sha256:05db4e81…`. Names are DWARF paths: env fields as printed (`regs[15]`, `hflags.flags2`, `v7m.mpu_ctrl[0]` — G21's `mpu_ctrl[0]` is inside `v7m`), `neg.*` for `CPUState.neg` (`neg.can_do_io`, `neg.icount_decr.u16.low`, `neg.tlb.f[1].mask`), `cpu.*` for other CPUState fields. G21's `CPUState.breakpoints/watchpoints` are QTAILQ heads; the guard's non-empty test reads `cpu.breakpoints.tqh_first` / `cpu.watchpoints.tqh_first` (same offsets −0x2980/−0x2970). `scopt.formats.envlayout_v1.G21_NAMES` maps the design's short names to these. All 15 G21/offsets.txt offsets are reproduced. The layout file is packaged as `scopt/formats/envlayouts/<exe sha256>.json` (also searched on `$SCOPT_ENVLAYOUT_PATH`); RuntimeIdentity v2 carries only its `envlayout_id`. Binding rule: layout usable iff `layout.exe_sha256 == RuntimeIdentity.qemu_sha256` (and ids equal when the identity names one), else `guard_layout_unbound`.
2. **HostOps op kinds (§8.1).** The design's kinds are kept and extended so that every byte is typed: `ENV_ADDR` (`lea r,[rbp+d]`), `GOTO_PTR` (`jmp reg`), `EXIT_JMP` (`jmp tb_ret`/epilogue), `GUEST_ACCESS` (the access ending at `raddr` inside the LDST fast path), `TLB_READ` (other non-env memory reads of the fast path), `HOST_ABS_LOAD`, `POOL_LOAD`, `FRAME_LOAD/STORE`, `NOP`. `HOST_ABS_STORE.abs_addr` is the absolute address when the base register holds a tracked constant (movabs / RIP-LEA / pool load), the base for an indexed store, and a store with an untracked base pins (`unexplained_host_store`). A store or load through a register derived from `rbp` pins (`env_alias_access`). Constant tracking survives an LDST slow path for callee-saved registers the slow path does not write (TCG keeps `r12 = 1` across a guest store and then stores `can_do_io` from `r12b`).
3. **"Every ENV_STORE matches a captured sync mark or explicit store" (WP17 acceptance) is made concrete as an origin per store**, from Tier-0 positions only: `icount_entry`, `cdio_prefix`, `condexec_prefix` (the `condexec_bits = 0` store of TBs that start inside an IT block, translate.c:7946-7951; 14 TBs in compute, 1 in blinky — not in §8.1's prefix list), `global_sync` (whole-width store of a TCG global inside an insn), `cdio_insn`, `plugin_mem_value` (insn has a QEMU_LD/ST OpMark), `plugin_mem_cbs`, `explicit_st` (whole-element store of another CPUARMState field inside an insn); anything else is `unexplained` and pins (`unexplained_env_store`). Result on minimal/fault/blinky/compute: 0 unexplained of 1,440/494/11,565/28,533 stores; 0 unclassified `rbp` operands of 2,648/905/21,027/51,268; compute's counts equal map-state-sync-traffic §7.2 (16,495 global stores, 5,681 plugin_mem_value stores, 3,942 can_do_io stores, 164 thumb + 29 condexec stores).
4. **hcfg (§7.3).** Two views: *atomic* (LDST fast path is straight-line, no slow-path edge, slow paths absent; what B4/hostsym enumerate) and *full* (real machine graph incl. slow paths and their return to `raddr`; used by hlive). The `goto_tb` jmp is an exit node (chained or falling into its `lea; jmp tb_ret` stub). No cycle and no unit over 256 paths exist in the four snapshots.
5. **hlive.** A call is modelled as reading all six SysV argument registers (the helper typemask is in the `.scm` HELPER chunk but not in `TBUnit`); `cmov*`, `bsr`, `bsf`, `cmpxchg` also read their destination. On unmodified TCG output hlive finds no dead register-only op (all four snapshots), as expected from TCG's own liveness.
6. **effects.** Unnamed helpers (plugin callbacks) are S4 whatever their TCG flags (a `NO_RWG` plugin callback can still raise an IRQ); named `NO_RWG` helpers other than `udiv`/`sdiv` and not noreturn are the pure allowlist.
7. **RuntimeIdentity extension (R8, §7.2, V20).** Implemented as schema `scopt.runtime_identity/2` = v1 fields plus an `admission` block (`scopt.admission/1`: `exe_sha256`, `cpu_model`, `envlayout_id`, `tb_start_cb_possible` + reasons, `mem_get_value_outside_mem_cb` + per-library evidence, `ac4_excluded_virtuals_present` + inputs inspected, `exec_vaddr_aliases`). v1 documents stay valid with unchanged ids; a v1 identity means every admission fact is unknown (fail closed: `tb_start_cb_possible = true`, D3 not deletable, V20 fails with `observer_contract_unknown`). Derivations: `tb_start_cb_possible` is true when twintrace is not off, when the manifest has `preprocessing_artifact_*` files (a run preprocessor such as variable_watch/object_sanitizer, whose runtime calls `virtual_register_tb_translation_hook`, ran), when the TOML enables `[CPU.cpuN.plugins.variable_watch|object_sanitizer]`, when a virtual library imports a TB-hook symbol or cannot be read, when the plugin is not `libfastdyn.so`, or when the manifest is missing; minimal, fault, blinky and compute are all `false`. `mem_get_value_outside_mem_cb` is read from the ELF `.dynsym` imports of the plugin and virtual libraries (`libfastdyn.so` does not import `qemu_plugin_mem_get_value`, confirming G22). AC4 detection is by name (tokens `aflnet`, `ardupilot`, `gcs_receiver` in the TOML, the generated `virtuals.txt` or library paths; a non-zero `fuzzing` option adds `fuzz_aflnet`) — a heuristic, not a proof. `exec_vaddr_aliases` comes from a small machine table (`cortexm`: none; `mps2*`: ssram1 at 0x400000), `null` for other machines.
8. **V20.** The body is `scopt.validate.v20_rows(v, runtime, units=…, need_d3=…, need_d2=…)` (rows `admission`, `cpu_model`, `envlayout_bound`, `tb_start_cb_possible`, `mem_get_value`, `ac4_virtuals`, `exec_vaddr_aliases`); WP18's registry decides when it runs. New stable reason codes in addition to §3.8/§7.5: `tb_start_cb_contradicted` (a `false` claim and a plugin CALL OpMark in some unit's prefix `[0, insn0_start)`), `tb_start_cb_unchecked` (a `false` claim with no Tier-0 units to cross-check), `ac4_excluded_virtual`; identity-building errors `admission_schema`, `admission_exe_mismatch`. `guard_layout_unbound`, `unsupported_guest` and `observer_contract_unknown` are used as in §3.8.
9. **Analyses registered** (discovered from `scopt/analysis/`): `envlayout@1` (snapshot), `hostops@1`, `hcfg@1`, `hlive@1`, `effects@1` (unit), `obs-contract@1` (snapshot). CLI: `scopt envlayout {produce,show}`, `scopt hostops {census,dump}`, `scopt admission {identity,check}`.

## Implementation note (WP24, unit stage, 2026-11-17): the fixtures as built

Sections above are unchanged; this note records where `fixtures/b3/` (README there) follows the source instead of §9.5, and the week-1 answers.

1. **U-VIRT.** A virtual is RW_CFI only when its argument string starts with `*` (`src/fastdyn/core/core.c:881-888`). F-B3-5 uses the built-in `randstate "0"` (non-RW_CFI, writes r0 with a `/dev/urandom` word); the fixture stays deterministic by re-checking the taken branch against the real r0 in the join TB. **F-B3-22b is not built:** the only non-RW_CFI xPSR writer is `randstate "25"` (random T/IT bits); the python/fuzzer writers are compiled out (`ENABLE_LIBPY 0`, `ENABLE_LIBFUZZ 0`). The g_cond-dropped mutant is therefore static-only (the §9.3 "otherwise" branch).
2. **U-TBCB.** No in-tree virtual registers a TB-exec callback that raises an IRQ. F-B4-9 uses an out-of-tree plugin `fixtures/b3/plugins/b3_tbirq` (`FASTDYN_EXTERNAL_PLUGIN`, `[CPU.cpu0.plugins.b3_tbirq] source = …`; `src/fastdyn` unmodified): a TB-start callback on a chained successor raises exception 21 every 7 executions. `fastdyn build-model` cannot build it in this checkout (no `third_party/common/qemu` headers); `build.sh` uses the same command against `src/qemu/include`.
3. **U-MPS2.** The integrated QEMU runs the mps2-an385 alias image with `-accel tcg,supercache=` and no FastDyn; nocache/cold/warm agree in both modes. The cache has one PCREL record for the continuation shared by 0x42c and 0x40042c.
4. **F-B3-8p:** `SP = 0xFFFFFFF0` does not fault on `cortexm` (`generic_io` maps 0..0xFFFFFFFE). The fixture uses a 32-byte no-access MPU region (PRIVDEFENA) at 0x200F0000 instead: precise MemManage on `pop {r4,pc}`, stacking below the region.
5. **QEMU baseline imprecision at helper faults (relevant to §7.1 S6 and WP25):** `helper_sdiv`/`udiv` are `TCG_CALL_NO_RWG` (`target/arm/tcg/helper.h:11`), so when they raise DIVBYZERO a register written by an earlier insn of the same TB is not yet in env; the architectural state after the fault misses that write (F-B4-6: verified with gdb, r10 loses the preceding add). B4 oracles compare with warm, which shows the same behaviour.
6. **O6 has no coverage artifact in this build:** `cov_bbl.c` is compiled only with `ENABLE_LIBFUZZ`, so `coverage = true` writes no `bbl.txt`/`edges.txt`; it still adds an inline `LOG_REG` PC store at every TB start and the tracer thread. F-B3-23/F-B4-10/F-B3-16(e) configs exist and qualify; O6 attribution must use C' hits (`diag/o3_hits.py`).
7. **Shapes:** F-B3-9/10 load before the r0 test inside C (a TB ends at its conditional branch); F-B3-21's `cpsid i; mrs primask` run in the join TB after the test (CPS ends the TB). Output uses printreg virtuals as `fixtures/fault/` does, not semihosting.
8. **Per-mode oracles (R14):** named invariants are derived from source, ELF and architecture; the exact icount `.norm` files are frozen from the nocache reference run after the invariants held and all five icount runs agreed (`expected/FROZEN.json`). Timer phases (F-B3-3, 3b, 6h, 7, 20, F-B4-3) use invariants only in no-icount; there back-to-back SysTicks at one PC are possible (ptimer 10 µs floor), so F-B3-7/20 count interrupted iterations.
9. **Evidence:** 90/90 (45 config × input pairs × 2 modes, 5 runs each) and the alias image on the WP-QINT QEMU tree (the maintainer's evaluation data (not in the repository)); `-d int` traces of the timer phases identical across nocache ×2/cold/warm (the maintainer's evaluation data (not in the repository)); gdb scenarios F-B3-12, F-B3-13(a)-(d) and the O3 counter in the maintainer's evaluation data (not in the repository).

## Implementation note (WP24 rework, unit stage, 2026-11-17): review fixes to the fixtures

Appended after review; the sections above are unchanged.

1. **F-B3-13(d) register write (§9.5 table):** the design's `r0 := 7` is dead at the only PAUSED stop point (`b3_13_spin`, before `mov r0, r4; bl f`). The scenario writes **r6 := 7** instead (the phase's nz accumulator, live to the result). The "(d) output follows the written register" check is then real: the result must be nz = 7, z = 1000, nocache and warm alike. The g_bp half (breakpoint at Vc inserted at the PAUSED stop, vc_hits 700) is unchanged.
2. **O3 counter (§9.4):** `fixtures/b3/diag/o3_hits.py` reads `info breakpoints` one breakpoint block at a time. A breakpoint without an "already hit" line counts 0, and a target missing from the table is reported `null` and fails the run. The earlier regex gave an unhit breakpoint's count to the next breakpoint. The flash RAMBlock offset no longer assumes that record 0 is the reset TB: if record 0 is not RAMBlock-aligned (64 KiB) relative to Reset_Handler, the unique aligned candidate is used; otherwise the tool errors and needs `--flash-offset`.
3. **gdb diag pass/fail is automatic:** each scenario carries `# EXPECT` / `# EXPECT-RES` lines that `gdb_guest.py` checks. The scenarios end with `detach`, so gdb exits 0.
4. **F-B3-16(d) guest-hash route:** the FastDyn fingerprint covers the firmware path, size and mtime and the whole TOML, so a FastDyn deploy of the F-B3-1 cache under b3-16d is a fingerprint deletion and never reaches the activation-time guest-hash check. WP26/WP28 reach that check with a direct QEMU rerun: `diag/qemu_rerun.py --from-run <b3-1 warm run> --cache-from <same> --kernel b3_16d.elf [--qemu <v6 binary>]`.
5. **noicount invariants as data:** `expected/<input>.inv.json` (plan: `expected/*.inv.json`) is written by `expected.py export-inv` from the same checks that `expected.py check` runs. A unit test keeps the two in sync.
6. **Fault record without CVR (§9.4):** not retrofitted, because it would shift every address of the image. The resulting gap: an icount-accounting change confined to F-B3-3b part (ii) before its fault is not observed by the 3b oracle. It is listed in the fixture README's known gaps.

---

## Implementation note (WP16, 2026-11-17; appended, no section above was rewritten)

Measured on the current `qint` binary (sha256 `8864c726…`), FreeRTOS compute, train inputs, n = 90 pairs; details in the maintainer's evaluation report `WP16-probe.md` (not in the repository), constants in the maintainer's evaluation data (not in the repository).
1. **§10.3 constants (were U):** `c_lookup` = 7.6 ns [6.6, 8.2] measured directly (an extra lookup-hit call per execution); `c_guard` ≈ 1.5 ns [0.2, 2.4]; `c_fail` not separable from 0. The v5 GENERAL-slot probe (`experimental_unsound_without_v6_tlb_hook`, 74 % of lookups guarded) gave +6.9 % [6.3, 7.9]: the R11 threshold is met for the guarded-slot copy.
2. **§7.3/§7.6 (B4):** D1 nop-filling of the hot units (24 stores, ≈ 80 M removed stores per run) gave +0.3 % [−0.6, +1.0], and +0.05 % [−0.2, +1.0] on top of the icount-entry store-forwarding fix. The R11 threshold is **not** met; §7.6 evidence criterion (1) for D5 is therefore not satisfied.
3. **Baseline-TCG effect outside B3/B4:** rewriting the icount entry (`gen_tb_start`'s 16-bit store / 32-bit load pair) into a forwardable form gave +10.8 % [10.1, 11.6] (placement control −0.1 %), and with the same-length cmov if-conversion of TB 0x1d5a +16.9 %. Per §10.1 and spec §8 it must be its own arm and is never counted as caching, B3 or B4.
4. **§9.4 O6 / F-B3-23 / F-B4-10 (source fact):** in this plugin build `coverage = true` writes **no coverage file** (`fuzz_init` is compiled only with `ENABLE_LIBFUZZ || ENABLE_AFLNET`, `src/fastdyn/virtuals/virtuals.c:701-703`; `fuzz_add_observed_value` is an inline stub, `include/virtuals/virt_fuzz.h:39`). The observable O6 can compare is FastDyn's in-memory LOG_REG ring `core_cc_list.log_buf` (index + 64 KiB buffer = the last 16 384 executed TB start PCs), read by a gdb `-nx` diag run at `benchmark_end` (`experiments/diag_twin.py --cov`). WP16's X-REHY baseline (GENERAL slots, minimal) shows it identical to warm.
5. **E-MAP/E-BP:** 0 `tlb_flush*`/`tcg_commit_cpu` calls after `benchmark_start` on compute (900 replays) and blinky; breakpoint/watchpoint lists empty.

## Implementation note (WP20, unit stage, 2026-11-17): return edges, facts, provenance, CalleeSummary as built

Added by WP20; the sections above are unchanged. Branch `wp20` of `tools/scopt` (a development worktree); evidence in the maintainer's evaluation data (not in the repository) (`verdict.json`).

Where the code follows reality instead of the text above, or adds to it:

1. **Modules.** As planned: `scopt/analysis/{return_edges,exit_facts,patch_join,callee_summary}.py`, `scopt/decode/guest_symbols.py`, `scopt/formats/callee_summary_v1.py` + `schemas/callee_summary.v1.json`. Added: `scopt/decode/thumb_sem.py` (Thumb exit classes, the four immediate-move encodings, QEMU's IT-state machine), `scopt/analysis/b3_inputs.py` (resolves the guest ELF and the patch-plan content **by hash**: ELF sha256 = bundle `image_hash`; plan files = manifest content sha256 and manifest plan hash = bundle `patch_plan_hash`), CLI `scopt return-edges {analyze,facts,summaries,ucond,stability}`. Analyses registered: `exit-facts@1` (unit), `patch-join@1` (unit), `return-edges@1` (snapshot, reads `profile:steady`), `callee-summary@1` (snapshot). The callee-summary schema is loaded by its own module, not through `scopt/formats/schemas.py` (a shared file).
2. **Patch-plan content is not in a store snapshot** (only its hashes are). `modifiers.txt`/`virtuals.txt` come from the prepare dir (`--prep`, `work/virtuals/`) or explicit files and are refused on a sha256 mismatch (`patch_plan_content_mismatch`, `patch_plan_hash_mismatch`). A manifest whose modifiers sha256 is the empty-file hash gives a known empty plan without the file (compute). Unknown content fails closed: `pc_modifier_exit` (detail `patch_plan_unavailable`) on every exit and `provenance_unexplained` on every fact.
3. **Exit rows (§3.1)** are produced for every `goto_ptr` and `exit_null` exit (not for `goto_tb` and the entry-check `exit_requested`). Class `static` is decided by the capture's off-page StaticExit at the exit instruction (authoritative) and cross-checked with the Thumb decode; a disagreement, or no guest bytes, is `indirect_multiway` (fail closed). `handler_mode_return` is applied to every class (rule 3 as written). `pc_modifier_exit` = a modifier targeting r15 at any instruction vaddr of R. Rule 6 needs no row of its own (no lookup tail ⇒ `exit_not_goto_ptr`). Several reasons may hold at once; they are listed in a fixed order.
4. **Proposer lattice (§3.2).** Forward must-constant dataflow on the atomic `hcfg@1` view, values per env field `const(v, defs) | killed | nonconst | partial`. Only exact 32-bit stores define (a partial store is `fact_not_constant`); `mov [rbp+d], reg` is constant when the host register is tracked. Kills: any call with `flags & 3 == 0`, **every plugin callback whatever its flags** (unnamed helper; FastDyn virtuals such as `printreg` are these), a `HOST_ABS_STORE` with an unknown address, an unclassified `rbp` write; a kill marks all 17 fact fields. New use of an existing code: `partial` (stored on some paths of R only) is `fact_not_local_to_return_tb`. The facts are taken at the tail's `mov rdi, rbp`. Several defining stores with the same value are kept as `defs[]` (one fact). `fact_from_profile_only` is never emitted: no API accepts a fact from a profile.
5. **Suffix units (§3.1 [R4]).** Group the return exits by return-instruction vaddr; when one unit defines `field = v` at an instruction whose vaddr precedes another unit's start, and that unit stores no `field`, the latter gets `fact_not_local_to_return_tb` (compute: 32 rows, e.g. the 0x1eb8 suffix of `prvParseReg` and the 0x1ee0 suffix of `prvOpIs` for r0/NF/ZF).
6. **Provenance (§3.2 patch-join).** Last writer of the field, walking R's instructions backwards from the defining store's instruction; at each instruction the **guest instruction is checked before the modifiers at its vaddr** (modifiers are injected at the instruction start). `source = guest_code` only for the unconditional immediate moves `movs Rd,#imm8` (T1; NF/ZF only outside an IT block), `mov{s}.w`/`mvn{s}` with a modified immediate, `movw` — conditional (Tier-0 `words[1]` says the instruction is inside an IT block with a non-AL condition) or computed writers are `provenance_unexplained`; flag facts that TCG constant-folds from a compare (`cmp r3,#0` ⇒ CF = 1) are therefore unexplained (3 on compute). Whole-bundle fail-closed rules: `image_hash ≠ original_image_hash` ⇒ `no_patch_bindings` on every fact; a fuzzing schema in the plan (its gated modifiers are not in `modifiers.txt`) ⇒ `unsupported_contract_fact_source` on every fact; a `[rN] <- imm` modifier as the writer (FastDyn writes rN, not memory, map-fastdyn-patching §3) ⇒ `unsupported_contract_fact_source`. FastDyn detours cannot be configured from the frontend and target r15 (not a fact field). Real check: minimal's `0x800397e r0 <- 3` (before an off-page `bl`, a static exit) gives `r0 == 3, source = patch`.
7. **CalleeSummary v1 (§3.3).** Rows also carry `exit_kind`, `return_insn`, `admitted`, `exit_reasons`, `patches_scope` (`callee` | `unit` | `unknown`), `refused[]` (proposer refusals, provenance failures, suffix rows), per-fact `defs[]`, `writer_insn/writer_vaddr`, `status` (`proposed` | `certified`) and `x_facts_status` when HostOps is pinned. `fact_id` = sha256 of canonical `{return_unit, content_hash, exit_id, field, value, defs}`; `row_hash` = sha256 of the canonical row without `row_hash`; the FactRef is `{fact_id, field, value, def_insn, source, cert_hash, summary_row_hash}` and `check_factref` raises `StaleFact` (`stale_fact`) when its row is gone. Until WP19's certifier is plugged in (`certify=` callable: `False` ⇒ refused `fact_derivation_mismatch`, `None` ⇒ `validation_inconclusive`), every fact is `proposed` with `cert_hash = null`, so §3.7's ATTACH_FACT precondition refuses it. No `ops_hash` (WP23 not approved). `effects[]` lists the entry-check `exit_tb:3` exit and LDST slow-path calls as well.
8. **U-COND (T-B3-42).** Checked per return exit from the Tier-0 `insn_start` `words[1]` (the translator's condexec at each instruction start, translate.c `arm_tr_insn_start`): the IT state after the return instruction must be empty; the guest decode is replayed from the key's CONDEXEC through QEMU's IT machine and must reproduce every `words[1]`; HostOps must hold no `condexec_bits` store outside the prefix. compute 163, blinky 126, minimal 19, fault 8 return exits: 0 counterexamples, replay matches everywhere (25 compute return units contain IT blocks). The fixtures have no Tier-0 capture, so they are checked statically (`ucond_elf_scan`: linear decode of the `$t` mapping-symbol ranges): b3.elf 83, b3_O0 84, b3_Os 77, b3_16d 83 return instructions (compute ELF 117), none inside an IT block.
9. **U-HELD (reported outcome).** A held-out profile of the base compute snapshot (held-out inputs 1-3 × 1 rep, `scopt profile run`, not timed; the maintainer's evaluation data (not in the repository)): over admitted return exits, the train top-2 candidates take 80.9 % of the held-out return lookup traffic, the train top-1 is the held-out top-1 for 91.5 % of it, and 99.6 % goes to edges seen in train; static exits 100 % (one target each).

## Implementation note (WP19, unit stage, 2026-11-17): the `hostsym` kernel as built

Added by WP19. No section above was rewritten. Branch `wp19` of `tools/scopt` (a development worktree). Code is in `scopt/validate_sym/`; documentation is `docs/hostsym.md`; evidence is in the maintainer's evaluation data (not in the repository).

Where the code follows reality instead of the text above, or adds to it:

1. **Modules.** The plan names `state`, `semantics`, `paths`, `equiv`, `selftest` and `mutants`. WP19 adds three more:
   - `program` (the neutral input: bytes plus the Tier-0 facts the kernel needs);
   - `asm` (a small x86 encoder for synthetic units);
   - `coverage`.

   The CLI is `scopt hostsym {selftest,coverage,check,mutants}`.
2. **LDST abstraction (§9.2).** A QEMU_LD/ST OpMark starts the block at the op's first host byte, so the "fast path" also holds the register allocator's input loads (`mov r13d,[rbp+0xc]`) and constants.
   - **Execution.** The fast path runs with the normal semantics, except the TLB-miss `jcc`, which is not a branch. The guest address is the base/index register of the access that does not hold a TLB (host-memory) value.
   - **Clobbers.** Only registers that the fast path writes after the first slow-path `jcc`, or that the slow path writes, are clobbered at `raddr`. Values the allocator loaded before the `jcc` survive.
   - **Env.** After every LDST, env bytes other than the TCG globals are havocked, because a slow-path MMIO helper may write non-global env fields.
   - **Identity check.** The block's bytes must be identical in both versions (`edit_inside_ldst_block`). Bytes are compared modulo relocation: rel/RIP targets are normalised to own-relative or absolute addresses.
3. **Havoc is a transform, not a fresh value.** A havocked byte becomes `("hv", epoch, old_byte)`, so a store deleted before a call or an LDST stays visible after it. Env is byte-addressed: partial overlaps are exact, and there is no "envmix" term.

   Env havoc depends on the call class:

   | Call class | Env bytes havocked |
   |---|---|
   | flags-0, plugin (unnamed), unknown target, noreturn | all |
   | `NO_WG`, `lookup_tb_ptr` | all except the TCG globals |
   | `NO_RWG` (pure, and `udiv`/`sdiv`) | none |

   Env is compared at each event per `obs-contract@1`:

   | Event | Sync point |
   |---|---|
   | call `lookup_tb_ptr` | S3 |
   | call flags-0 / plugin / noreturn / unknown | S4 |
   | call `udiv`/`sdiv` | S6 |
   | call pure | P (only non-deletable fields) |
   | guest access | S5 |
   | exit | S2 with its exit kind |
4. **Barrier.** TCG's memory barrier `lock or dword [rsp],0` (DMB/DSB/ISB) is modelled as a `barrier` event. It was the only UNSUPPORTED form in the four snapshots before this. Every other locked or string op stays UNSUPPORTED.
5. **New failed code `recovery_rows_changed`.** V12 first checks that the search rows, `icount` and `cflags` are byte-identical (this overlaps V16), so the "corrupt a search row" mutant is rejected by the kernel itself.
6. **host_abs assertion (§7.5).** The process env address is not in the Tier-0 capture. When it is given (`env_abs`), the effective address is checked against `[env − sizeof(CPUState), env + sizeof(CPUARMState))`. Otherwise the base pointer of the access must be one the importer bound to `abs.guest_ram`, `abs.plugin_data`, `abs.qemu_data` or `ext.*`; anything else fails closed with `host_abs_aliases_env`. No captured unit of minimal/fault/blinky/compute has a HOST_ABS store or load. So the UPDATE_MEM, LOG_REG and host_abs mutants exist only on synthetic units.
7. **V18 is bound to `kernel_id`**, the sha256 of the `validate_sym` sources plus `effects.py` and `obs_contract.py`, and to the capstone version. `scopt.__version__` never changes between commits, so it cannot serve. New reason codes: `hostsym_selftest_missing`, `hostsym_selftest_stale`, `hostsym_selftest_failed`.

   The native test covers 330 forms, 48 random/edge trials each: 15,840 trials and 87,648 flag checks. A deliberately wrong flag model fails it.
8. **V-row bodies, not wired.** WP18 owns the registry (not merged), so the bodies are appended to `scopt/validate.py` as functions:
   - `v12_rows`;
   - `v13_entailment_rows` (WP19's path-condition half of V13; the structural V13 is WP18's);
   - `v14_rows` (rows `certified`, `proposer_equal`, `provenance`);
   - `v18_rows`.
9. **Guarded chain.**
   - **Path signature.** Branches inside the guard stub are not part of the path signature. Several R' paths (slot paths and the F path) match one R path.
   - **Atom entailment.** An atom is entailed when the env value at the slot exit is the constant, or when the path condition holds `ZF(env − K) = true` from a stub branch.
   - **can_do_io.** The lookup's `can_do_io = 1` is written into a copy of the original env at the lookup call only. The env comparison then uses S2 with exit kind `guarded_slot`.
   - **Fact certification.** `certify_fact(R, None, field)` certifies at the lookup call of an ungrown R (the T4 shape). `certify_fact(R', n, field)` certifies at slot n.
10. **Reference guard emitter.** `mutants.SynthUnit` emits the §4.1 template, with guard atoms declared per slot. It lets the guard mutants run at the unit stage (`needs_emitter: true`): drop each atom, guard before an effect, a store in the stub, a falsified-fact link, and a shared variant on a suffix unit. All are rejected. The same mutants built with WP18's emitter/placement are the WP19 integration acceptance, which was not run.
11. **Results (reported outcomes).**
    - **Coverage:** 100 %. All 2,222 compute units are analysable (complete exploration, and the identity check is equivalent), covering 826,110,048 of 826,110,048 steady entries. Minimal/fault/blinky: 163/163, 48/48 and 1,013/1,013 units. The largest unit has 9 paths.
    - **Prototype tests on compute** (store snapshot `53b4132f…`, the same id as the `arm-m-returns/snap-compute` copy): T1-T5 as in `proto-results.txt`. T6 (0x1f7c) is analysed: 2 paths, 3 guest events (ld, ld, st).
    - **Mutation suite:** all 27 synthetic mutants rejected as `mismatch`, plus 7 positive controls. Same-length real-unit mutants rejected: 150/150 minimal, 94/94 fault, 247/247 blinky, 252/252 compute.
    - **Informational, for WP25:** deleting each compute D1 global-sync store alone, hostsym accepts 474 of 13,587 (the G28 model counts 469 static D1 stores).

## Implementation note (WP18, unit stage, 2026-11-17): NativeLinker v2, `.sc` v6 writer/readers, regions, graph, V-rows as built

Added by WP18; the sections above are unchanged. Branch `wp18` of `tools/scopt` (a development worktree); evidence in the maintainer's evaluation data (not in the repository) (`acceptance.json`, `integ/integ.json`, `verdict.json`); user doc `tools/scopt/docs/b3-link-v6.md`.

Where the code follows reality instead of the text above, or adds to it:

1. **Guard template as built (§4.1; `scopt/guard.py`, id `guard-v1`).** Every branch uses the rel32 form (`0f 85`/`0f 84`/`e9`), so the stub length depends only on the number of targets, the `Kf2 >= 2^31` form and the slot padding (0-3 `nop`, so that the slot's rel32 field is 4-aligned relative to `tc`; `tc` is 64-aligned in absolute address). One stub per guarded exit; stubs are appended consecutively after the pool from `stub_start` (= the TB's original `tc_size`). The guard's displacements come from the EnvLayout (`GuardOffsets.from_layout`); `decode_guard` parses a stub strictly against the byte template (V13).
2. **Unregistered slots (v5 artifacts, plan "GENERAL slots not registered").** Their reset path is `jmp F`, never `exit_tb(R|n)`: stock `tb_add_jump` → `tb_set_jmp_target` patches `tc.ptr + jmp_insn_offset[n]` without checking for 0xffff (accel/tcg/cpu-exec.c:619-670), so a main-loop chain of `R|n` with an unregistered slot would corrupt code. A v5 grown unit therefore always ends in the original lookup (behaviour = baseline; only the guard runs). SPEC slots must be registered (v6).
3. **Grown-unit metadata (`grow_unit`).** The tail's HELPER_CALL and GOTO_PTR sites and its CALL/GOTO_PTR OpMarks move to `F`; the tail gets a LABEL_JMP site to `S`; stub sites: LABEL_JCC/LABEL_JMP, GOTO_TB (registered) or LABEL_JMP (unregistered) for the slot jump, MOVI_EXIT_TB_PTR + EXIT_TB_RET_JMP for a GENERAL reset; the pool row of the tail call names `F`'s call; the tail call's recovery point (return offset `T+9`, not `T+11`: the call ends 9 bytes into the tail) is dropped and stub HELPER_CALLs give no recovery point (§6.1: the lookup never unwinds); one new `rel_minus_delta` fixup per GENERAL reset (`jmp tb_ret`). Search bytes, including their alignment pad, are unchanged; `end_offset` grows by the stub bytes. Grown units always move: NativeLinker v2 places them from `ROUND_UP(max_end, 64)` in slot order and the variants after them; every other unit keeps its offset. `TBUnit` gained `stub_start` (default `None`).
4. **Derived `.scm`.** TB chunk **v2** has the v1 layout; the low u32 of `reserved` is `stub_start` (high u32 = 0); chunk type **9 `VARIANT`** (Python-only) carries the variant index, its arena offsets and `code_fnv64`, the output slots of C and of the predecessor R, `slot_n`, `exit_class`, the 32-byte variant id and the same-length patches. The importer sweeps `[0, pool_start)` and `[stub_start, tc_size)`, checks that the pool tiles `[pool_start, stub_start)`, and rebuilds each variant as `relocate(C) + patches` (`import_variants`). The existing contract test of the chunk-type numbering now includes `VARIANT = 9`; a version-6 `.sc` whose extension is refused is an import rejection `sc6_extension`.
5. **SnapshotId of a v6 file.** `sha256("scopt.snapshot_id.v6/1" ‖ <B1 SnapshotId of header + valid slots> ‖ ext bytes ‖ sha256 of each variant body)`; v5 ids are unchanged.
6. **`artifact_id8`.** The ArtifactId is the sha256 of `MANIFEST.sha256`, which covers `cache.sc`, so it cannot be embedded in the file. The ext table's id is the first 8 bytes of `sha256("scopt.sc6.artifact_id8/1" ‖ ext table with the id zeroed ‖ every record ‖ sha256 of every body and variant body)`; the sidecar carries it as `x_ext_artifact_id8` (deploy compares, WP26).
7. **V7 v6 clause.** "v6-capable RuntimeIdentity" = its exe sha256 is in `v6_capable_exes()`: explicit list, `$SCOPT_V6_EXES`, or the maintainer's evaluation data (not in the repository) (`exe_sha256`, written at the WP22 freeze). Failure reason `v6_runtime_required` (new). The sidecar `loader_requirement` is `qemu-sc-v6` for v6 outputs.
8. **CacheRegion schema (§3.9).** Exits gain kind `guarded_slot` with `slot_kind`, `target_vaddr`, `guard_off`, `guard_len`, `reset` (`jmp_F`|`exit_tb`); `$defs` `guard_atom`, `fact_ref`, `native_unit`; `native` is a `native_unit` (tb_singleton, optional `stub_start`) or `{members: [native_unit…]}` (multi_tb); `members[].origin.slot` may be null (C'); `state_fixups` has `maxItems 0`. Member 1's relocations keep C's move rule and carry `x_origin: variant_relocated` (the §3.9 text says `rule: variant_relocated`; a rule value that is not a move rule would not be actionable by a linker that moves the row). Member-1 exit and recovery ids are prefixed `m1.`; events and unchecked pointers carry `x_member`; the row has `x_slot`. `dependencies.units = [R.uid, R_orig.uid, C.uid]`: the UnitId depends only on key and guest bytes, so `R.uid == R_orig.uid` (the content hashes differ).
9. **Graph.** `guarded_slot` edges carry `slot`, `slot_kind`, `registered`, `dest_vaddr` and the candidate units at that vaddr with the source's flags and lookup cflags (no `dest_ram`: the guard's pc compare is absolute); `variant_link` edges (`resolution: variant`) name the variant id and the general C, never a node. The fallback lookup of a guarded **return** exit stays an explicit unknown (`goto_ptr dest`); a static exit's `F` keeps its static destination.
10. **V-rows.** `scopt.validate.V_ROWS` lists V1-V23 with owners; `validate_b3` implements V1-V9 (relaxed for declared edits), V7's v6 clause, V9' (identity-v6), V11, V13 (structural, incl. "R15/thumb stored on every path" by a must-store analysis over hcfg@1; rule 6 holds by construction: a `DISAS_EXIT` TB ends in `exit_tb(NULL,0)` and has no lookup tail), V15, V16, V17, V19 (cited FactRefs are checked against WP20's CalleeSummary rows with `check_factref` when rows are given, `stale_fact` otherwise; `skip` when nothing is cited), V21 (budgets from the plan; defaults 2 per C, 64 per artifact), V23; V20 calls WP17's `v20_rows`; V12/V14/V18 (WP19/WP20) and V22 (WP23, not approved) are `skip` rows. V16 is about variants; for grown units the unwind check is V6.
11. **Typed payloads (§3.7).** In `scopt/passes/base.py`: `GuardAtom`, `GuardedSlot`, `Clone`, `SpecEntry`, `FactRef`, `Patch` (validated at construction), `PATCH_TEMPLATES` per pass, `B3Reason` (all §3.8/§7.5 codes + WP17/WP18 additions, with `outcome()`). `scopt.proposal.FactRef` (an analysis citation) keeps its name; the B3 payload is `scopt.passes.base.FactRef`. `ProposalBuilder` admits a B3 kind only when the manager enables it (WP21); `ADD_RECOVERY` is never admitted; `ADD_REGION` is manager-only; `ADD_ENTRY` needs a CLONE of the same proposal; PURE_REWRITE checks the pass's template list.
12. **New stable codes:** linker `patch_length`, `patch_old_bytes`, `patch_overlap`, `edit_on_fixup`, `spec_slot_unregistered`, `link_missing`, `variant_pred_slot`, `unknown_unit`; packaging `ext_table_too_large`; V7 `v6_runtime_required`; import `sc6_extension`. Loader reasons are the loader's own messages (`ext_problem` codes such as `variant_extent`, `guard_hash`).
13. **Independent parser.** `tools/scopt/independent/sc6parse.py` was written from the C loader source and `sc_v6.py` from the design text, but **by the same developer**: the plan's "different engineer" condition is not met; independence is by source only. Both agree with each other on every T-B3-1 case.
14. **Exit class** comes from a Thumb decode of the captured guest bytes (`TBUnit` has none; tests read them from the capture join). Compute: 575 lookup tails, 156 return, 415 static, 4 indirect.
15. **EnvLayout of the qint-v6 build** (sha256 `56d8bfb3…`, not frozen) was produced with `gdb -nx` into the maintainer's evaluation data (not in the repository) (not packaged). The tests' v6 RuntimeIdentity is a stand-in (the compute v2 identity with that exe sha and layout id) that exercises the V7/V20 logic only.

## Implementation note (WP18 rework, unit stage, 2026-11-17): review fixes

Added by WP18 after review; the sections above are unchanged.

1. **SPEC key binding (invariant 2, §4.1, §9.1 V13).** The `.sc` v6 loader checks only that a SPEC slot's `target_vaddr` is at C's page offset, and the arm-time probe compares only `Vc`'s ram address under C's MMU index, so neither notices a guard whose `Kf`/`Kf2` (or `Vc`) is not C's key. Two layers now fail closed: (a) `NativeLinkerV2` refuses a variant link with `continuation_key_mismatch` unless `guard_key(spec.key) == guard_key(C.flags, C.cs_base)` and `Vc` is one of C's captured virtual addresses (`scopt.link.spec_binding_problem`); (b) V13 row `spec_key_binding` walks every ext link of the written file, decodes R's guard at the slot's `guard_off`, and requires the slot's decoded `Vc` to equal the entry's `target_vaddr` and to be a virtual address of C (`variant.general_tb_offset`'s record), and requires `(Kf, Kf2) == guard_key(C.flags, C.cs_base)` of C's record. The plan is not consulted. The pc is not in the record (CF_PCREL, record `pc = 0`), so C's virtual addresses come from the snapshot capture (`TBUnit.vaddrs`). A C with none is refused.
2. **SS_ACTIVE (§4.1 "Admission additionally requires K.flags SS_ACTIVE = 0").** `guard_key` now refuses both TBFLAG_ANY SS_ACTIVE (bit 1, target/arm/cpu.h:2957) and PSTATE__SS (bit 2), and each message names its own bit. On cortex-m3 neither bit is ever set, so this was latent.
3. **V13 GENERAL reset.** The `jmp tb_ret` of every registered GENERAL reset must target the snapshot's `tb_ret_addr` (RUN_INFO), not only be unchanged by the move (V4).
4. **Fail-closed packaging.** `package_b3` publishes an edited plan only when V12, V14 and V18 pass (`scopt.validate.CERTIFYING_ROWS`). V22 is excluded, because WP23 is not approved. Until WP19/WP20 wire those rows, structural tests and the integration script pass `allow_uncertified=True`, and the sidecar records `x_uncertified`. WP21's manager must not pass it. V20 fails with `guard_layout_unbound` when there are B3/B4 edits and no RuntimeIdentity (before, it was `skip`). V7 `loader_requirement` is `skip` when no sidecar is given. A table the loader would refuse (`ScExtRefused`) is rejected as `failed:<ext_problem code>`. `multi_tb_rows` raises if the guard key or EnvLayout is unknown, instead of writing zeros or default offsets. Packaging rejects the artifact as `multi_tb_region`.
5. **V16 with patches.** The embedded-pointer decode-compare now runs against C with the declared patches applied in place, and every finding counts. Before, findings were discarded whenever a variant had a patch.
6. **V13 must-store analysis** fails (`did not converge`) if the fixpoint is not reached within the round limit. Before, it returned the partial result.
7. New stable reason codes: `continuation_key_mismatch` is now also a linker failure (it already existed in §3.8), plus packaging `certification_not_run` and `multi_tb_region`.

## Implementation note (WP18 rework 2, unit stage, 2026-11-17): second review fixes

Added by WP18 after the second review; the sections and notes above are unchanged except where this note says it refines them.

1. **Import reason for a version-6 header (refines WP18 note item 4 and item 12).** A version-6 `.sc` whose extension is present but refused by the loader is import rejection `sc6_extension`. A version-6 header with no extension at all (feature EXT_TABLE clear, which is what a v5 file with its version field set to 6 looks like) keeps B1-9's stable reason `version`, so the B1 gate test `test_b1_9_rejections.py::test_version_6_rejects` is unchanged.
2. **Certifying rows wired (refines WP18 rework note item 4).** `validate_b3(..., hostsym=...)` now runs WP19's row bodies when the caller supplies their inputs: `equiv` (V12 verdicts), `entailment` (V13 path part), `facts` (V14) and `selftest` (V18 document). Without `hostsym` the three rows stay `not_run` stubs and `package_b3` rejects an edited plan as `certification_not_run`. `uncertified_rows(val, link)` accepts WP19's vacuous V14 skip ("no SPEC link") only when the link has no variant; V12's "no changed unit" skip never certifies an edited plan, and V18 has no skip path. The scopt kernels themselves are not invoked by `package_b3` (the caller, WP21's manager, runs them).
3. **`package_b3` refuses `skip_reimport`** (ValueError): V8/V19 and the regions need the re-import. A missing re-import result after an accepted validation is rejected `reimport_failed`.
4. **`multi_tb_region` is a failed outcome** (`B3Reason.FAILED`; it can only come from an internal inconsistency, spec:47 / §3.8), and packaging reports it as `failed:multi_tb_region`.
5. **`guard_key` refuses the M-profile FP/MVE bits** of C's cs_base (TBFLAG_M32 LSPACT bit 2, NEW_FP_CTXT_NEEDED bit 3, FPCCR_S_WRONG bit 4, MVE_NO_PRED bit 5; `scopt.guard.CS_M_FP_BITS`) with `continuation_key_mismatch`, instead of masking them out. The guard does not compare them (NOTCACHED2), so a key carrying them is not proven equal by a guard pass. cortex-m3 (the only core V20 admits) never sets them: all 2433 units of the minimal, fault and compute snapshots have them clear.
6. **Exact `Vc`.** The linker's `spec_binding_problem` and V13 `spec_key_binding` compare `Vc` exactly against C's virtual addresses (bit 0 cleared); a `Vc` with bit 0 set is refused.
7. **Standalone `validate_b3` on refused tables** emits rows and never raises: V13 `spec_key_binding` fails ("ext table refused by the loader") instead of indexing the table, out-of-range link indices are findings, and V5 reports an output record that is not an input unit (a duplicated record) as a finding.

## Implementation note (WP26, unit stage, 2026-11-17): run harness, oracles, negative controls, arms as built

Added by WP26; the sections above are unchanged. Branch `wp26` of `tools/scopt` (a development worktree); user doc `tools/scopt/docs/b3-run-harness.md`; evidence in the maintainer's evaluation data (not in the repository) (`verdict.json`).

Where the code follows reality instead of the text above, or adds to it:

1. **v6 post-run proof (§9.4).** For a deployed version-6 file with the EXT_TABLE feature bit (a version-6 header without it is a v5 file with its version field changed and keeps the B1 check list, as in B1-9) the proof records the sha256 of `[0x100, 0x10000)` and the ext `artifact_id8`, and adds checks `v6_header` (still version 6 with the deployed feature flags) and `ext` (bytes unchanged). F-B3-18 on the old loader therefore fails `header` and `v6_header` (the record bytes may survive the truncate). v5 proofs are unchanged (no new check rows).
2. **Artifact id comparison (WP22 note 2).** Deploy compares a v6 file's ext `artifact_id8` with the sidecar's `x_ext_artifact_id8` (`ext_artifact_id_mismatch`, `ext_artifact_id_missing`) and the header version with `loader_requirement` (`loader_requirement_mismatch`). These run before the FastDyn oracle. `deploy.json` keeps `artifact_header_problems` in the stock v5 loader's view (B1 tests rely on it) and adds `artifact_header_problems_v6`. A v6 artifact on a v5-only binary is **not** refused at deploy (F-B3-18 needs that run; the RuntimeIdentity check already refuses it for artifact directories unless identity enforcement is off); the proof decides.
3. **F-B3-16 refusal codes pinned (§5.7).** (a) changed patch plan and (e) coverage-enabled configuration both give `bundle_mismatch` (first, the code deploy raises) then `config_mismatch`. Fields: (a) `instrumentation_hash`, `patch_bindings_hash`, `patch_plan_hash`; (e) `contract_id` plus the same three, because every fixture config prints its own BENCH name through a virtual (the isolated coverage change differs in `contract_id` only). Pinned by `tests/unit/test_wp26_harness.py` with the WP24 manifests copied into `tests/data/wp26/stale/`.
4. **Test-only tags (§9.3, §9.5).** Negative controls carry sidecar `x_negative_control` and the marker file `NEGATIVE_CONTROL.json`; test-policy artifacts carry `x_test_only = {policies, waived}` with the single policy `allow_ram_resident`. `scopt.formats.artifact.publish` refuses both (`negative_control`, `test_only_artifact`), so every publisher, the new `scopt publish` command included, refuses them; deploy refuses them unless the caller admits them (`negative_control_artifact`, `test_only_artifact:<policy>`, `unknown_test_policy:<policy>`); campaign preflight refuses them as arm sources. The admission side of `allow_ram_resident` is not wired into WP20's `return_edges` (not a WP26 file): `scopt.run.policy.waive` drops the `ram_resident` reasons and `tag_sidecar` records it, for WP21's manager to call.
5. **Negative-control packager.** Bypasses exactly V12-V17; any other failing row refuses the mutant (`unbypassed:<code>`). A byte-level mutant is resealed (variant `code_fnv64`, slot `guard_fnv64`, `artifact_id8`) so the loader loads it, its derived captures are dropped and the re-import is skipped (V8/V19 rows absent): it is never re-imported. The directory is never written under a store's `artifacts/`.
6. **`scopt publish` did not exist** before WP26; it is a verified copy of an artifact directory into a store (refuses negative controls, test-policy artifacts, failing `validation.json` rows and sidecars with `x_uncertified`).
7. **O1 in noicount (R14).** With a fixture oracle, O1 in noicount compares only exit codes byte-wise and lets the fixture's per-mode oracle decide (it still requires the exact `.norm` for phases without a timer); without one, O1 is exact.
8. **O2b in noicount.** Generic invariants: records complete, counts equal to the end counters, fault positions (pc, cfsr, ipsr, bfar, mmfar, iteration) and spare-IRQ positions (pc, iteration; raised by a TB-execution count) equal to the reference, plus the fixture's named invariants when given. SysTick records are not compared there.
9. **O3 / O6 unknowns.** A counter or hit count that was not collected is `not_run`, never 0. With coverage off FastDyn allocates no LOG_REG ring (gdb shows a NULL buffer), so O6 is `not_run` on non-coverage configurations; on F-B3-23 (coverage on) the ring is dumped and compared.
10. **gdb.** Hit counters use hardware breakpoints (at most 4 per gdb run; more targets are split over several runs), so no `int3` is written into the shared file mapping of the cache. The diag twin's `commands … end` blocks need a script file (`-x`), not `-ex` (found in the smoke run). **Fixture fact for WP27/WP28:** every `fixtures/b3` phase writes `MPU_CTRL = 0` in its teardown after `benchmark_start` (b3.c), one TLB flush that the twin counts as 3 calls (`tlb_flush`, `tlb_flush_by_mmuidx`, `tlb_flush_by_mmuidx_async_work`); it happens after the measured loop. `diag_twin(allowed_calls=…)` records a tolerance.
11. **Arms (§10.1, §9.4).** `B3B4_ARMS` and `FIXTURE_ARMS` are data (name, kind, loader v5/v6, passes); ablations `semantic-no-<pass>` and `b4-<class>`. `semantic`/`combined` stay `unsupported` only while no artifact is given for them. A campaign with a v6 arm must name a v6-capable QEMU (`v6_arm_needs_qemu`, `v6_loader_required`, using `v6_capable_exes()`).
12. **Direct path (F-B3-15).** `deploy_direct`/`launch_direct` run a QEMU argv template (`@QEMU@`, `@SUPERCACHE@`, `@RUN@`) without FastDyn; the proof's manifest check passes with a note (`frontend = "none"`), `identity.checked = false`; an artifact directory whose RuntimeIdentity names another QEMU is refused `runtime_mismatch:qemu_sha256`. The import side: `prepare_direct` makes one cold capture-on direct run (`QEMU_SC_CAPTURE` the only `QEMU_SC_*` variable) and the importer accepts the cache and capture without a FastDyn manifest when no bundle is given (smoke on `qint`: the alias image imports with 17/17 units `ok_relocatable`, no bundle); a bare `.sc` without a capture imports as all `no_meta`.

## Implementation note (WP19, integration stage, 2026-11-17): hostsym on WP18's linked artifacts

Added by WP19 at the integration stage. No section above was rewritten. Branch `wp19-int` of `tools/scopt` (a development worktree, from `main` 00bfe21); evidence in the maintainer's evaluation data (not in the repository). There was no QEMU run and no timing.

1. **Glue from a link to the kernel (`scopt/validate_sym/link_check.py`).** `certify_link(snapshot, LinkResultV2, …)` runs hostsym on the **linked artifact** (placed bytes, records, ext table), not on the plan.
   - **Grown units.** Each grown R' gets `check_guarded_chain` (V12, plus the V13 entailment rows).
   - **Rewritten units.** Each rewritten unit gets `check_equiv`.
   - **Variants.** Each variant C' gets `check_equiv(C, C', facts ∧ guard atoms)`, once per ext link that enters it.
   - **Facts.** Each ext link gets `certify_fact(R', n, field)` for every fact of its variant (V14).

   A slot's **declared** atoms never come from the stub bytes. g_flags/g_flags2 come from the continuation key: C's record key for a linked SPEC slot (through `guard_key`), otherwise the plan's `GuardedExitSpec`. g_pc comes from the ext slot entry's `target_vaddr`. The displacements come from the EnvLayout. `hostsym_inputs(...)` is the `hostsym=` value of `validate_b3`/`package_b3`.
2. **`validate_b3(hostsym=callable)`.** This is a change to WP18's dispatch block in `scopt/validate.py`. A callable `(snapshot, link) -> inputs` is called on the link being validated. Before, `package_b3` could not be given hostsym inputs, because it links internally. Now `package_b3(..., hostsym=hostsym_inputs(...))` certifies and publishes. Without it, the plan is still rejected `certification_not_run`.
3. **Kernel change found by the integration: `can_do_io` across a returning access or helper (§9.2 havoc).** WP18-grown return units whose last guest instruction loads after the `can_do_io = 1` store failed their positive control under `tb_start_cb_possible` (compute: `pop {…,pc}` units such as 0x10ea). The error was `validation_mismatch: env at slot neg.can_do_io`, because the LDST havoc erased the 1 that the original side gets from the lookup.
   - **Source facts.** The only writers of `cpu->neg.can_do_io` are generated code, `cpu_tb_exec` (cpu-exec.c:452), `helper_lookup_tb_ptr` (:392), `cpu_loop_exit` (cpu-exec-common.c:70; it longjmps and never returns into the TB), `cpu_reset` (hw/core/cpu-common.c:132) and the vCPU thread start (tcg-accel-ops-rr.c:195,336; -mttcg.c:82). `io_prepare` (cputlb.c:1283) only reads it, and when it is false calls `cpu_io_recompile`, which does not return.
   - **New rule.** An LDST block, and a call to a known helper that returns, leave `neg.can_do_io` unchanged. An unknown call target and a noreturn call still havoc it, and the lookup call is unchanged.
   - **No regression.** T1-T6, coverage, the synthetic suite (27/27) and the real-unit mutants (150/94/247/252) are byte-identical to the unit stage (the maintainer's evaluation data (not in the repository)). The D1 probe still accepts 474 of 13,587.
   - **Tests.** A synthetic `pop {pc}` test shows the pre-fix kernel's false mismatch. Deleting that `can_do_io = 1` store is still rejected, at the guest access (S5), even without `tb_start_cb_possible`.
4. **Variants of a grown continuation.** NativeLinker v2 clones the *effective* C (`eff[src_uid]`). When C is itself grown in the same plan, C' carries C's guard stub and registered slots: 566 of the 2,207 SPEC-eligible compute units are also growable. hostsym compares such a C' with the grown C (`c_reference`), which is itself checked against the captured C. **Open question for WP18/WP22 (not checked here):** a clone of a grown C contains C's registered GENERAL reset `lea rax,[rip→C'struct+n]; jmp tb_ret`, so the main loop could see `exit_tb(C'|n)`. It is not known whether the v6 loader gives the variant's struct valid `jmp_insn_offset`s (cf. WP18 note item 2).
5. **`certify_fact(..., exit_pc=T+9)`** certifies a fact at one lookup exit. `summary_certifier(snapshot, layout)` is WP20's `Certifier` (`build_callee_summaries(certify=…)`), and `summary_provenance(rows)` gives V14 its provenance from the CalleeSummary fact rows. On compute, all 87 proposed facts are certified (the maintainer's evaluation data (not in the repository)). The `scopt return-edges summaries` CLI (WP20's file) does not pass a certifier yet.
6. **Integration acceptance: mutants built with WP18's emitter and placement** (`scopt/validate_sym/link_mutants.py`, `scopt hostsym link-mutants`). Every growable return/static unit of minimal, fault, blinky and compute (52/4/351/571) was used, with v6-registered GENERAL guards, v5-unregistered guards and the `Kf2 ≥ 2^31` imm64 form. Each mutant is a change to the linked artifact checked with `certify_link`.
   - **Result.** 20,056 of 20,056 counted mutants are rejected, all as `mismatch` (none only `inconclusive`). By class:

     | Class | Rejected |
     |---|---|
     | `drop_g_bp`, `drop_g_thumb`, `drop_g_cond`, `drop_g_flags`, `drop_g_flags2`, `guard_stub_store` | 2,078 each |
     | `drop_g_pc` (per target) | 4,022 |
     | `guard_before_effect` (tail jump hoisted above each `regs[15]`/`thumb` store) | 2,536 |
     | `falsified_fact_link` | 978 |
     | `shared_variant_suffix` | 26 |
     | `shared_variant_suffix_copy` | 26 |

   - **Controls.** All 3,084 positive controls pass (unmutated grown units, correct SPEC links with the certified facts, full+suffix units with their own variants).
   - **Shared variant.** The full/suffix pairs are real: e.g. compute 0x1ede `movs r0,#0; bx lr` and its suffix TB 0x1ee0 `bx lr`. 73 pairs were not built, because no fact differed, so sharing would be sound there.
   - **Equivalent mutants (listed, not counted).** Blinky unit `tb:100c64eb…` (0x13ee, static exit) stores `condexec_bits := 8` before its exit: the TB ends inside an IT block. So g_cond never passes, and its unmutated control already has both slots unreachable. 12 of its 18 mutants are not rejected, because they cannot change any execution. Admission (CONDEXEC = 0 in `key_C`; WP20/WP21) must refuse such exits. The harness's dummy plan does not apply admission.
7. **End to end (compute, the maintainer's evaluation data (not in the repository)).**
   - **Published.** `package_b3` with the WP18 stand-in qint-v6 identity publishes the F-B3-1-shaped plan: grow 0x1ede with a SPEC slot to a variant of 0x24f0 folded under the certified WP20 fact `r0 == 0` (`guest_code`). V12, V13, V14 (certified, proposer_equal, provenance), V18 and V19 all pass.
   - **Rejected.** The falsified link fails V14 `fact_derivation_mismatch` (and V19 `stale_fact`). The wrong fold fails V12. The suffix sharing a copy of the variant fails V14 `fact_not_constant`. Without `hostsym` the plan is rejected `certification_not_run`.
   - **Artifact-level mutants.** Stub byte mutants are refused already when the ext table is written (loader `guard_hash`). With `guard_fnv64`/`artifact_id8` recomputed they pass V11 and fail V12/V13 (hostsym) and WP18's structural V13/V3. A second LinkEntry to one variant is refused by the loader (`variant_two_links`).

## Implementation note (WP22, integration stage, 2026-11-17): stress test and the frozen gate binary

Added by WP22; the sections above are unchanged. Evidence: the maintainer's evaluation data (not in the repository).

1. **Gate binary (R13).** `qint-v6` commit `4671d5ceda` (the unit-stage commit; the loader was not changed in this stage) is tagged `qint-v6-gate-1`. The gate QEMU build has sha256 `56d8bfb32c91c970fe0a16237b962c02791ce0a7fcc52bf6246343fbef430d90`, recorded in the maintainer's evaluation data (not in the repository) (read by `scopt.validate.v6_capable_exes`), with a read-only copy in the maintainer's evaluation data (not in the repository). Its EnvLayout (`sha256:32440e25…`, re-produced with `gdb -nx`, byte-identical to WP18's) is packaged as `scopt/formats/envlayouts/56d8bfb3….json`. Do not rebuild the gate QEMU tree.
2. **T-B3-26 as built** (`tests/qemu/wp22_stress.py`). The artifacts come from WP18's writer (`package_b3`, `allow_uncertified=True`) on WP24 snapshots captured on the gate binary (icount). Each variant is an identity clone of C, so every run must equal the fixture's frozen icount oracle. The QEMU process runs under `gdb -nx`. Hardware breakpoints count the entries into each `C'.tc.ptr`, and the slot, variant and link state is read before and after every loader event. Some scenarios also drive a guest debugger on the gdbstub. Each event of §5.6 maps to a scenario as follows:
   - **TLB flush by guest MPU write:** F-B3-14, with `C'|n` linked at the flush. The b3-14 artifact also holds a variant of a grown C, whose cloned GENERAL slot the main loop links; the MPU disable after the phase unlinks it.
   - **TLB flush with `C'|n` linked:** F-B3-6r. Host `call tlb_flush(cpu)` is the call the NVIC MPU write makes.
   - **Invalidate C, invalidate R:** a gdbstub debug write at a PAUSED stop on F-B3-13 (`tests/qemu/wp22gdb/`). A PAUSED stop does not flush, so the link is still armed when the write lands.
   - **tb_flush at a breakpoint stop with the link armed:** F-B3-13(d), at the first breakpoint hit at Vc. WP24's F-B3-12 and F-B3-13(a)/(b) scripts run unchanged.
   - **One-insn-per-tb:** set through HMP.
   - **Plugin reset:** `qemu_plugin_reset(<FastDyn id>, NULL)` called from host gdb on the vCPU thread. No guest or FastDyn trigger exists. The scoreboard-growth site of Q11 (plugins/core.c:258) cannot be reached with one vCPU and was not exercised.
   - **The "debug assert":** the gate build has `debug_tcg=false`. The full suite was therefore also run on an out-of-tree `--enable-debug-tcg` build of the same commit (the maintainer's evaluation data (not in the repository), exe `c5e067ee…`, not a gate binary), where `g_assert(bad == 0)` is live. The gate binary checks the same invariant through its counters.

   Every stats counter of an unlink or reset path is compared with the count derived from the observed events. All 29 scenarios pass on both builds.
3. **Finding for WP28 (F-B3-12, F-B3-13(a)).** WP24's `diag/f-b3-12.gdb` and `f-b3-13a.gdb` insert their first breakpoint before the guest runs, on the 1 KiB page of Vc. C is then looked up with `CF_BP_PAGE` cflags, so it never matches its cached record and is never activated from the cache. As a result no link is armed in those runs (`links_armed == 0`, 0 entries into C'). The loader behaves soundly, but "C' hits stop after K" is vacuous with those scripts. To invalidate an armed C, use a PAUSED stop (`wp22-13-invc.gdb`: armed, then debug write, then R's slot back at `jmp F` with `jmp_dest` 0, and C' never entered again).
4. **Stats precision (no soundness effect).** A stock invalidation of C (`tb_jmp_unlink`) or of R resets the slot but leaves the loader's `links[i].armed` set until the next TLB flush or `tb_flush`. `disarm_tlb_links` then counts that link although its slot was already at the reset path. `try_arm` skips the link because `armed` is set. Each `tb_flush` clears the flags.
5. **Observed QEMU behaviour.**
   - In `f-b3-13b` every watchpoint hit was followed by a `tb_flush`: 702 flushes for 700 hits. This is most likely gdb stepping over the access as a debug stop. It is harmless for the loader, because a flush resets everything.
   - After the plugin reset (NULL callback) the guest output still equals the oracle: cached code keeps its baked callbacks. The forbid holds: `links_armed` 1, `refused.forbidden` 1, no C' entry afterwards.
   - A GDB note: `sc6_disarm_all_work` has two breakpoint locations (gcc split). The harness breaks at `*function`.
6. **WP18 model against the loader.** `tests/qemu/wp22_ext_xcheck.py` builds 32 one-field mutants of the WP18-written b3-14 file. For every mutant, WP18's `ext_problem` message equals the loader's `invalid v6 extension: <message>`. In every case the exit is non-zero, there is no guest output, and the file is unchanged.
7. **Disabled-path cost** (T-B1-0 method, v5 caches, gate exe against qint, 40 interleaved warm pairs per workload, randomized layout, timing lock held, `taskset -c 0-3`, load average 4.9-6.1):

   | Workload | Median overhead | 95% CI | Paired median | Flag |
   |---|---|---|---|---|
   | FreeRTOS blinky | -0.45% | [-1.79%, +1.08%] | +0.03% | not flagged |
   | compute | +0.64% | [-0.91%, +2.16%] | +0.30% | not flagged |

   The compute CI does not exclude an overhead of up to about 2%.

## Implementation note (WP24 review 3, unit stage, 2026-11-17): log race, noicount tick oracle, F-B3-7/13(d)/16(d)

Added by WP24; the sections above are unchanged. Fixture: `fixtures/b3/` (README "Log atomicity", "Why no tick count in noicount", findings 10 and 12). Evidence: the maintainer's evaluation data (not in the repository).

1. **Guest log race fixed.** `log_pair` is a non-atomic RMW of `g_log_n`, and the timer kernels report results (`b3_res`) while SysTick TICKINT is still on. A SysTick logged inside that window lost one O2b record (review 3: 1 in 1200 noicount runs). `b3_res` now runs `log_pair` with PRIMASK set (saved and restored). The handlers share one priority and never preempt each other. A unit test checks every `log_pair` call site on all four images.
2. **§9.5 R14 noicount oracle for timer phases (6h, 7, 20, b4-3): zero SysTicks is legal.** `SysTick records > 0` and `hits > 0` are icount-only invariants. Without icount, QEMU's ptimer 10 µs floor and timer-kick latency let a ~0.5 ms phase run with no tick (review 3: 9 of 1200 runs). A noicount gate (WP28/WP29) must not require ticks. Interrupt coverage of a B3/B4 arm is reviewed in icount. After the fix: 8000 noicount runs, 0 failures, 46 with no tick. Without icount F-B3-20's window is never interrupted, because interrupts land only at TB boundaries.
3. **F-B3-7 has its own reload sweep** {97…112}. In icount, 51 of 232 SysTicks land at Vc; before, it was 2 of 214.
4. **F-B3-16(d)'s changed instruction is `adds r6,#1`** (the caller's nz accumulator), replacing `adds r0,#2`, which the `r0 <- 0` patch overwrote. The output now discriminates: the correct image gives nz 1000, and stale b3.elf code gives nz 0.
5. **F-B3-13(d) writes `r0 := 7` as §9.5 says**, at the first breakpoint stop at Vc after the PAUSED stop. The `cbz r0` reads it next, so the expected result is z 999, nz 1. This replaces the rework's r6 write at the PAUSED stop. WP22's stress scenario `paused-bp:b3-13` takes its expectations from this script, so it must be re-run at integration.
6. The guest log capacity grew from 16384 to 65536 pairs, so a noicount tick storm on a slow host cannot overflow it. All `.norm` files were re-frozen. Only 3b (a scratch r3 in the fault frame), 7 and 16d changed.

## Implementation note (WP26 rework, unit stage, 2026-11-17): review fixes to the run harness

Added by WP26; the sections above and the earlier WP26 note are unchanged. Branch `wp26` (commits `47c7939`, `ded8083`, main merged in `e8bf19c`); evidence in the maintainer's evaluation data (not in the repository).

1. **`scopt publish` is a verified copy.** Nothing is copied unless the source's `MANIFEST.sha256` verifies, the sidecar's `validation_sha256` names `validation.json`, that file is `accepted` with no failing row and names the sidecar's input/output SnapshotIds, the output SnapshotId recomputed from `cache.sc` equals it, and the staged copy hashes to the source manifest (new codes `manifest_verify`, `validation_mismatch`, `validation_failed`, `output_snapshot_mismatch`, `copy_mismatch`). `scopt.formats.artifact.publish` itself also refuses a staging directory whose `validation.json` rejects the candidate or is not named by the sidecar (B1-design §4.6).
2. **Bare `.sc` of a tagged directory.** `artifact_tags(<dir>/cache.sc)` reads the directory's `NEGATIVE_CONTROL.json` and the file's own `<file>.scopt.json`, so deploying the bare file of a negative control no longer bypasses the tag.
3. **`launch_direct`** refuses (`runtime_mismatch`) when the QEMU's sha256 changed between `deploy_direct` and launch.
4. **O2b.** Both modes require the end counters `0x5f02`-`0x5f04` in both logs (a log cut short fails). In noicount the SysTick handler counters (`0x56xx`) are not compared (they count host-time-dependent SysTick entries), in addition to the SysTick positions (earlier note item 8).
5. **O3.** Hit counts from a gdb run whose QEMU did not exit 0 are unknown (`not_run`), never counts.
6. **`dynamic_oracles` verdict** is `fail` (an oracle fails or is vacuous, or a deploy is refused), else `incomplete` (a requested oracle is `not_run` or missing in some case), else `pass`; only `pass` exits 0 from `scopt validate --dynamic --oracles`. The smoke's "O6 pass on the coverage config" run in the earlier note would now be `incomplete` overall, because O3 had no v6 stats on `qint`.
7. **icount mode** is parsed from the TOML (`icount = false` and `icount = {enabled = false}` are noicount); a TOML that does not parse falls back to the text rule.
8. **F-B3-16 test data.** WP24 rebuilt `fixtures/b3/b3.elf` after the manifests in `tests/data/wp26/stale/` were recorded, so the pinned refusal-code tests had started to skip (`bundle_stale`). The ELF they were recorded with (the maintainer's evaluation data (not in the repository), sha256 `39a5f283…`) is stored as `tests/data/wp26/stale/b3.elf` and materialized with the recorded mtime; the tests now run regardless of the fixture's current build. The refusal codes are unchanged (`bundle_mismatch`, `config_mismatch`).

## Implementation note (WP26 rework 2, unit stage, 2026-11-17): O4 fails closed, run-harness fixes

Second review of the WP26 run harness (branch `wp26`, commit 60e5340, merged with main 41385f6 as e534c75). Evidence: the maintainer's evaluation data (not in the repository). This note does not change §9.3/§9.4; it records how §9.4's O4 rule is enforced in code.

1. **O4 fails closed (blocking).** `scopt.run.oracles.o4_detected(results, *, artifact_run, reference_run)` now takes both launch documents. It returns `not_run`, never `pass`, when the control's post-run proof is not `PASS` or its launch verdict is not `PASS`/`INVALID_exit_code`: `INVALID_artifact_not_used` (the loader refused or rebuilt the file, or a v6 control ran on the v5-only `qint` binary), stray capture files. It also returns `not_run` when the reference run is not `PASS`. A `fail` counts as a detection only when it records a concrete difference: O1 `data["differences"]` (exit code, normalized output, guest output, fixture oracle), O2a `first_difference`, or O2b record differences whose reference log has no structural problems (`data["reference_problems"]`). A QEMU abort while running a proved mutant (F-B4-9's deleting mutant: launch verdict `INVALID_exit_code`, proof `PASS`) still counts, through its exit code. Before this fix, an O1 `fail` caused only by an unproved artifact run counted as "detected".
2. **v6 artifact on a non-v6 QEMU.** `dynamic_oracles` refuses a `.sc` v6 artifact before any launch when the QEMU the runs would use is not in `scopt.validate.v6_capable_exes()`: `OracleRefused("v6_loader_required")`, and `scopt validate --dynamic --oracles` exits 2. Before, only campaign preflight checked this.
3. **O4 mode.** With `allow_negative_control=True` (`--negative-control`), `dynamic_oracles` adds O4 to every case and the verdict is O4's alone. `pass` means the mutant was detected in every case (exit 0). `fail` means it was not detected in some case, or a deploy was refused. `incomplete` means the control was not proved to run. The ledger reason starts with `negative control O4 (pass = mutant detected)`. Before, the driver never computed O4, and a control whose outputs equalled warm exited 0.
4. **Diag twin (m-CE7).** `clean` is now always the plan's rule: 0 counted calls after `benchmark_start`, and all 20 counter breakpoints resolved to an address. Breakpoints that are `<PENDING>` or missing are listed in `e_map.unresolved`, and their calls are unknown. The previous tolerance of 2 unresolved breakpoints (inherited from `experiments/diag_twin.py`) is gone. `allowed_calls` now only sets `within_allowance`, a diagnostic. No design note approves relaxing m-CE7 for the fixtures' `MPU_CTRL = 0` teardown, so a twin with those 3 calls is not clean and is not gate evidence. If that teardown makes F-B3 fixture twins unclean on the gate binary, the owner of that gate must either measure only up to the teardown or record an approved exception; this note does not grant one.
5. **Record parser.** `parse_handler_records` reports an error when a field index repeats inside one record (a record whose index-0 pair is missing). Before, such a record silently overwrote the previous one, and O2b now fails on it.
6. **`scopt publish`.** Validation rows that are not objects now give `validation_failed` instead of an `AttributeError`.
7. **Known limit, bare `.sc`.** Negative-control and test-only tags live next to the file (marker file, sidecars), not in the `.sc` bytes. A control's `cache.sc` copied to another directory deploys as an untagged bare `.sc` (`identity.checked = false`). Gate reports must name artifacts by directory.
8. **Test-data notes.** `test_o2b_equal_recorded_runs` compares two recorded logs that are byte-identical (7 icount nocache and warm), which is valid but trivial, and the b4-9 icount and noicount logs are identical files too. The F-B3-16 (a)/(e) pinning tests call the pure `identity_reasons()`, which `deploy()` raises its first reason from. Real deploys under those configs remain integration evidence.

## Implementation note (WP22, integration stage re-run, 2026-11-17): stress re-run on the WP24 review-3 fixture

Added by WP22; the sections above and the earlier WP22 notes are unchanged. Evidence: the maintainer's evaluation data (not in the repository) (the first integration verdict is kept as `verdict-r1-0853.json`). Branch `wp22-int` commit `21790e5` (main `3d5250f` merged).

1. **Gate binary unchanged.** No commit to `qint-v6`. `qint-v6` = `qint-v6-gate-1` = `4671d5ceda`, the gate QEMU tree is clean, and the exe sha256 is still `56d8bfb3…`. the maintainer's evaluation data (not in the repository) keeps its binary fields; only its `evidence` pointers were updated (the previous record is in the maintainer's evaluation data (not in the repository)). The disabled-path cost (item 7 of the WP22 integration note) was not re-timed: both measured exes and both workloads are unchanged. T-B3-27 does not use `fixtures/b3`.
2. **Why the re-run.** WP24 review 3 rebuilt `fixtures/b3` after the first integration run. `b3_res` now masks interrupts around `log_pair`, and `f-b3-13d.gdb` writes `r0 := 7` at the first Vc stop (z 999, nz 1).
3. **b3-14 artifact changed with the image.** `b3_res` is now `push; mrs PRIMASK; cpsid i` | `orr; bl log_pair` | `msr PRIMASK` | `pop {r4, pc}`. `log_pair` returns to the `msr` TB, which ends with `exit_tb`, so that TB has no lookup tail: it can be a C but cannot be grown. The `pop` is now a TB of its own, reached through the main loop. The earlier artifact (log_pair's return → SPEC → b3_res's pop TB, grown with two GENERAL slots) no longer exists. R and C are now taken from `b3.nm`/`b3.lst`, not from fixed offsets.
   - `b3-14`: f_b3_14 → SPEC → vc_b3_14; log_pair's return → SPEC → the `msr` TB (entered once, on the second `b3_res` call); b3_res's pop TB with two GENERAL slots to the phase's return sites.
   - `b3-14g` (new): the variant of a grown C moves to the phase's hot loop. The call TB `mov r0, r4; bl f_b3_14` (cross-page, so it has a lookup tail) → SPEC → `f_b3_14`, itself grown with a GENERAL slot to vc_b3_14. The guest MPU write at iteration 500 flushes the TLB while C'(f_b3_14)'s cloned GENERAL slot is linked by the main loop. The flush unlinks it. C' is entered 499 times before the flush and never after. This replaces the earlier "cloned GENERAL slot unlinked at the MPU disable after the phase" check with a stronger one at the in-loop MPU write.
   - Attempted first: a variant of the grown return site 0x980 (R = b3_res's pop). It was never entered. Each return site runs once, and a link arms only when both R and C are activated, so it arms only after C's single execution. This is loader behaviour as designed (§5.5), not a defect.
4. **Results on the gate binary:**
   - `stress-r6`: 32/32 scenarios pass. `paused-bp:b3-13` passes with the new f-b3-13d EXPECT lines (write at iteration 300, r0 = 7, vc_hits 700, z 999 / nz 1), C' entered 299 times, and the link is still armed at the first breakpoint flush.
   - `stress-dbg-r2`: 32/32 pass on the `--enable-debug-tcg` build `c5e067ee…`.
   - `wp22_ext_xcheck`: PASS on b3-14 (32 mutants) and on b3-14g (31 mutants).

## Implementation note (WP20, integration stage, 2026-11-17): facts on the WP24 fixtures

Added by WP20; the sections above are unchanged. Branch `wp20-int` of `tools/scopt` (a development worktree); evidence in the maintainer's evaluation data (not in the repository) (`verdict.json`).

1. **Fixture snapshots.** `tests/qemu/wp20_fixture_snapshots.py` captures every `fixtures/b3` config (44) in icount (shift 0) and noicount. It renders each template with `fixtures/lib/fdrun.py`, then runs `scopt prepare`, `bundle identity`, `bundle runtime-identity`, `import`, and an untimed steady-phase profile (`steady@b3_bench_start`). The binary is the WP-QINT build (sha 8864c726…): facts are taken on the base snapshot, which the v5 capture produces. The v6 gate binary is not needed for this. 88/88 preps PASS; nothing was timed.
2. **Criteria as checked** (`tests/acceptance/wp20_fixtures.py`, both modes):
   - F-B3-1/1p/2/3/3b/4/8: every return unit of the patched instruction has `r0 == 0`, `source = patch` (plan entry at that vaddr) and `certified`. F-B3-3 has 3 such units in icount and 2 in noicount: the full TB at 0x1001c, the TB at 0x10020 that follows the io-recompiled 1-insn `ldr r2,[r1]` at 0x1001e, and (icount only) a budget suffix at 0x10022. Each carries the fact, because the modifier sits on `bx lr` itself.
   - F-B3-5: `fact_killed_by_helper`; hostsym agrees that r0 is not constant.
   - F-B3-20: the full unit has `r0 == 1` `guest_code`, certified. In icount, all 7 suffix units (starts 0x100d6…0x100e2) are `fact_not_local_to_return_tb`. In noicount the snapshot holds no suffix unit, so that check is vacuous there.
3. **F-B3-6 `f5`: "no_fact" is read as "no `r0 == 0` and no `source = patch` fact".** `f5` (`f_b3_6b`: `movs r0,#5; bx lr`) has its own natural fact `r0 == 5` (`guest_code`, certified; NF/ZF too). That is correct: the immediate move defines r0 inside R. It can only yield an `r0 == 5` variant, never `f0`'s `r0 == 0` variant. `no_fact` is not a §3.8 code. `f0` has the certified patch fact. The `bx r3` arbitrary entry is `indirect_multiway`.
4. **Two-sided agreement (`scopt return-edges xcheck`, `scopt/analysis/fact_xcheck.py`).** `--certify` asks V14 only about proposed facts. `xcheck` also compares every refused or untracked field with hostsym's own derivation, at every admissible lookup tail of every unit and for all 17 fields. Result: 0 `fact_derivation_mismatch`, 0 `proposer_weaker` and 0 inconclusive on all 88 fixture snapshots (1394 agree_const, 39491 agree_nonconst) and on the 4 real snapshots (minimal, fault, blinky, compute).
5. **Fail-closed rows seen (not plan criteria):** under the coverage configs (F-B3-16e, F-B3-23), R's HostOps is pinned (`unexplained_host_store`), so there are no facts and the exits are `exit_not_goto_ptr`/`unit_not_relocatable`. F-B3-6h's handler-mode `bx lr` ends in `exit_tb(NULL)`, giving `exit_not_goto_ptr` + `handler_mode_return` and no facts. Every other patched config (6t, 6r, 7, 8p, 9-14, 16a/b/d, 17, 21, 22, 24 at -O0/-O1/-Os) has the certified patch fact (16a: `r0 == 2`).
6. Not run: the Tier-1 PLUGIN_CBS cross-check of patch-join (WP23 not approved).

## Implementation note (WP26, integration stage, 2026-11-17): run harness on the gate binary

Added by WP26. This note does not rewrite the sections above. Branch `wp26-int` of `tools/scopt`, driver `tests/qemu/wp26_integ.py`, evidence in the maintainer's evaluation data (not in the repository). Every run used the gate binary (sha256 `56d8bfb3…`, tag `qint-v6-gate-1` = HEAD 4671d5ce, clean tree) or `qint` (`8864c726…`). The fixture was the WP24 review-3 image (b3.elf `3b029618…`). Nothing was timed.

1. **F-B3-18 (§9.5).** WP18 wrote three v6 artifacts on an F-B3-1 base prepared on the gate binary: identity-v6, the F-B3-1 SPEC artifact (R = `f_b3_1`, SPEC slot to `vc_b3_1`) and the synthetic plan of WP18's unit tests.
   - **On the gate binary**, each artifact directory gives launch and proof `PASS`, with `v6_header` and `ext` true, appended 0, the fixture's icount oracle clean, and O1 equal to the v5 warm run.
   - **On `qint`**, the artifact directory is refused at deploy (`runtime_mismatch:qemu_sha256`, from its RuntimeIdentity). The bare `cache.sc` gives `INVALID_artifact_not_used`, with `header` and `v6_header` failed (`version 6 -> 5, feature_flags -> 0`). For the two artifacts with an ext table, `records` also fails.
2. **Why the extra control.** The `.sc` header's `runtime_signature` is bound to the exe. Because of that, `qint` also rejects a v5 cache made on the gate binary. So a gate-made v6 file proves nothing by being INVALID on `qint` on its own.
   - The control is a base prepared on `qint`. Its v5 cache passes on `qint`.
   - Its identity-v6, written with `v6_exes=[qint]`, is INVALID on `qint` with only `version`/`reserved` and the v6 flags changed. The `runtime_signature` is equal. That override of V7 `v6_capable_runtime` is test-only, and the directory was never published.
   - So the rejection comes from the v6 format.
3. **QEMU selection.** FastDyn takes the QEMU from the TOML's `qemu_path`, which wins over `FASTDYN_QEMU`. A run on another binary therefore needs the template rendered for it. The first attempt did not do this: all its `qint` runs ran on the gate binary (the maintainer's evaluation data (not in the repository)). The driver now compares every run's `qemu_sha256`.
4. **O2b on F-B4-3 (§9.4).** All runs were on the gate binary, compared exactly in icount mode against the v5 warm reference (33 SysTick records):
   - identity-v6: pass;
   - identity-v6 under a guest `arm-none-eabi-gdb -nx` that only watches `g_sweep`: pass;
   - the same with the SysTick sweep pointer advanced by one entry (every reload after the first is shifted): **fail**. It had 39 records, and of the 33 paired records, PC differed in 24, xPSR in 21 and the iteration in 31. The reference log was sound, and O4's fail-closed rule counts it as detected.
   - The gdb runs start QEMU from the deploy oracle's argv with `-S -gdb`. They take the §4.8 proof afterwards, and every one was `PASS`, although the watchpoint appended 4 or 12 records.
5. **Finding: SysTick CVR carries no information in these fixtures.** Every SysTick record's CVR is 0: in the frozen icount oracles `b4-3` (33), `7` (232), `20` (112) and `6h` (73), and in every gate run here. §9.4 assumes CVR is a function of the instruction count. On this QEMU/fixture it is constant. In icount, O2b's positions therefore rest on the stacked PC, xPSR and registers, and on the guest iteration counter. In a deterministic loop, (iteration, PC) names one dynamic instruction, so the interrupt position is still pinned. That is how the shifted reload above was detected. What is lost is what CVR would add on top: icount drift between the interrupt and the handler's first load, for example in exception-entry accounting. For WP24/WP28: if that is wanted, the CVR has to be read differently.
6. **Harness end to end.**
   - `scopt validate --dynamic --oracles` on the F-B3-1 SPEC artifact is refused on `qint` before any launch (exit 2, `v6_loader_required`).
   - On the gate binary it passes O1, O2a (2 exception lines identical), O2b (no handler records in F-B3-1) and O3 (`links_armed=1`, C' hits = 999), with O5 reported (appended 0).
   - An artifact packaged into its own store needs `--baseline <snapshot dir>`. Without it the reference deploy is `artifact_unreadable`, which was hit in a first try.

## Implementation note (WP25, unit stage, 2026-11-17): B4 `eliminate-state-sync` and the `icount-entry-sfb` baseline fix as built

Added by WP25; the sections above are unchanged. Branch `wp25` of `tools/scopt` (a development worktree); docs `docs/b4.md`; evidence in the maintainer's evaluation data (not in the repository) (`verdict.json`). No QEMU run, no timing.

Where the code follows reality instead of the text above, or adds to it:

1. **Modules.** `scopt/analysis/syncdead.py` (`syncdead@1`, unit analysis), `scopt/passes/eliminate_state_sync.py` (pass + driver), `scopt/b4_mutants.py`, `scopt/passes/icount_entry_sfb.py`, `scopt/commands/b4.py` (`scopt b4 {census,package,mutants}`, `scopt sfb package`), `pipelines/b4.toml`, `pipelines/tcg-entry-sfb.toml`. B4 reason codes live in `syncdead.B4Reason` (the §7.5 list plus `class_not_selected` for the per-class ablation and `special_cflags_unit`); `scopt/passes/base.py` (shared) is unchanged.
2. **Dataflow (§7.3).** Byte-level backward may-need over the atomic `hcfg@1` view for every byte of the D1/D2/D3 fields (EnvLayout offsets; `plugin_mem_value_high` is −0x10). A store is a candidate when it is a plain `mov [rbp+d], imm/reg` that writes only bytes of one deletable field. This is wider than "a store to f covering its width": TCG stores `plugin_mem_value_low` with 32 bits for an i32 access, and such a partial store is dead iff its own bytes are not needed.
3. **LLVM-survey B4 corrections (llvm-pass-applicability §3c B4-2, §4.3 item 5), reconciled with §7:**
   - *Every helper call reads `can_do_io`* (pure NO_RWG helpers and the lookup helper included): **adopted** in `syncdead` and in the validator. V12 runs under `StrictCallContract`, which makes `can_do_io` observable at hostsym's `P` (pure calls), `S3` and `S6`. This is the rule the D2 model used, and the static count then equals the model exactly.
   - *Every call reads `plugin_mem_value_*`*: **not adopted** as the default (`d3_all_calls` is an option). The only reader is `qemu_plugin_mem_get_value` (G22). V20's RuntimeIdentity fact restricts it to memory callbacks, which run only from a plugin call in TB code (S4) or after a fresh write by `qemu_plugin_vcpu_mem_cb`. The §7.2 rule (S4, plus S6 per §7.1) is therefore sound. On compute the survey rule gives 5,187 dead D3 stores instead of 5,602 (all units).
   - *D2a/D2b split with a successor dependency*: **adopted as §7.2's direct-chain rule**, keyed on `tb_start_cb_possible` (V20 cross-checks the claim against Tier-0 prefix plugin calls). A per-successor dependency cannot be expressed in a v5 file, because chaining is decided at run time (G8). The `instrumentation_hash` is pinned by V23.
   - *Prefix `can_do_io` store class, icount bytes frozen*: **adopted** (already in §7.3; V15 enforces it).
   - *`udiv`/`sdiv` keep the baseline env image*: **adopted**. S6 observes all three classes, in `syncdead` and in the strict contract.
   - *D3 needs no memory callback in the unit and no `mem_get_value` import*: **adopted**. A unit with a `plugin_mem_cbs` store refuses D3 (`observer_contract_unknown`); the import is the V20 fact.
     - *Retire D3 at run time when a memory callback is registered*: **not adopted**. A v5 artifact has no runtime hook. The admitted configuration is the guard, and F-B4-7 (integration) checks it dynamically.
   - *host_abs operands must not alias env "at load time"*: kept as V12's `host_abs_aliases_env` assertion at validation time. There is no loader change for v5. None of the four captured snapshots has a `HOST_ABS_*` op.
   - *Env offsets from DWARF*: **adopted** (EnvLayout).
4. **Unit admission.** `special_cflags_unit` (§7.3) skips 736 of compute's 2,222 units: the count-limited TBs `cflags & CF_COUNT_MASK ≠ 0` that the icount budget produces. The model analysed all units, so T-B4-1 compares on all units (`skip_special=False`, analysis only). The pass never edits a special unit.
   - All units (vs model): D1 **469** (469), D2 **1,665** (1,665; prefix 678 = 678), D3 **5,602** (5,681, −1.4 %). M0 (TCG's own sync set): **0** dead D1 stores.
   - Admitted units: D1 334, D2 1,207, D3 3,409.
   - Reported outcome (K11), D2 under `tb_start_cb_possible = true`, admitted units: 1,207 → 330 static, and 104.3 M → 29.4 M per run entry-weighted (an upper bound: unit entries × stores). Only prefix `false` stores survive.
5. **Manager.** The B2 `PassManager` still strips the B3/B4 edit kinds, so `pipelines/b4.toml` gives `capability_not_enabled` (fail closed; a unit test checks it). WP21 owns the manager change. Until then `propose_b4` runs the pass with a `ProposalBuilder` that enables PURE_REWRITE only. `package_b4` builds `B3Plan(rewrites=…, register_slots=False)` → a **v5** artifact (`qemu-sc-v5-unmodified`), calls `package_b3` with `hostsym_inputs(contract=StrictCallContract)` and `need_d2/need_d3`, and publishes only when V12/V18 certify the artifact.
   - The per-class ledger rows (`eliminate-state-sync@1/D1|D2|D3`) come from `class_ledger_rows` and from the sidecar pipeline row (`x_classes`). `package_b3` has no hook for extra artifact-ledger rows.
   - Hot-first ordering uses a context's `unit_weights` when one is offered. By default all units are edited.
6. **V15 (WP18-owned `scopt/validate.py`).** Added the missing clause "deletions only of deletable classes". A `b4_nop_store` patch must replace exactly one plain env store of a class the contract makes deletable (D3 only with the fact), and only with NOPs (`_b4_deletion_problems`). NOPs are single instructions of 1-11 bytes (66/2E-prefixed for 10/11), one per store as §7.3 says. WP19's `asm.nop` splits at 9 bytes.
7. **Mutants (§9.3, B4 part).** `scopt/b4_mutants.py` builds 16 mutant kinds on real units and packages each through `package_b4`.
   - 43/43 are rejected statically on fault, compute and blinky, by V12 `validation_mismatch` / `edit_inside_ldst_block` and V15 (`edit_in_prefix`, `edit_inside_ldst_block`, `edit_in_exit_stub`, `pc_store_required`, `observer_contract_unknown`), plus V20 for `d3_without_fact`.
   - T-B4-2b (`brcond_reload`) is rejected on compute and blinky.
   - UPDATE_MEM/LOG_REG deletion and host_abs-in-env exist only in WP19's synthetic suite (12/12 B4-relevant synthetic mutants rejected), because no captured unit has a FastDyn host store.
   - Static-only: `d2_before_lookup` (the lookup sets `can_do_io`, G7) and `d3_without_fact` (an identity defect, not a byte defect).
8. **`icount-entry-sfb@1` (survey §3c B4-1; WP16 measured +10.8 %).** Implemented as its **own** pass, pipeline (`tcg-entry-sfb`), packager (`package_sfb`) and rows (SFB1-SFB9). It is labelled `baseline_tcg_fix_icount_entry_sfb`, is never part of a B3/B4 pipeline, and is never counted as caching. It deliberately breaks invariant 3 (the entry check changes); that is why it is kept out of B4.
   - **Payload.** The frozen §3.7 payloads have no prefix-growth kind, so the proposal is an ANNOTATE edit (`x_icount_entry_sfb`).
   - **Rebuild.** The rebuild follows WP16's tested method:
     - the new entry, padded to old + 16 bytes;
     - every later byte shifted by +16;
     - refs to the own struct and to external targets re-encoded;
     - search rows +16, padded to a 16-aligned `end_offset` as the B1 importer requires;
     - goto_tb offsets +16;
     - the unit moved to the arena end when it outgrows its gap.

     The derived `.scm` is shifted too: the entry's `jl` LABEL_JCC site becomes the `jne` and `jb` sites. The artifact re-imports with full Tier-0 metadata, and the B1 §2.7 cross-check passes for every unit.
   - **Equivalence.** Equivalence is proven per entry form by native execution of both entries for all 65,536 `low` values × `high ∈ {0, 0xFFFF}` (exit decision, env bytes, register). It relies on the source fact that `high ∈ {0, 0xFFFF}`; hostsym cannot prove the different branch terms.
   - **Results.** Compute: 2,201 units (21 `cpu_noirq_shape`), 479 moved, 45 entry forms. Blinky: 985 units, 27 forms. The compute code bytes, search bytes and record fields except `end_offset` are identical to WP16's timed `sfb` copy.
   - **Reason codes:** `cpu_noirq_shape`, `not_icount`, `entry_shape`, `unit_not_relocatable`, `ref_into_prefix`, `unfixed_external_ref`, `rel8_out_of_region`, `abs_into_code`, `sfb_no_encoding`, `place_out_of_range`, `sfb_semantics_mismatch`, `sfb_body_mismatch`.
   - **Ablation.** Run without that pipeline. Per the survey, B4 timing should be sequenced after it; that is for WP29/WP30 to decide.
9. **Not run in this stage (integration, later):** F-B4-1…F-B4-7 O1/O2a/O2b, compute B4 V10 on held-out inputs, the early paired timing point, T-B4-2b dynamic, and the O4 negative controls. WP26's harness is not merged into `main`. Anything Tier-1 is `not_run: WP23 not approved`.

## Implementation note (WP21, unit stage, 2026-11-17): B3 passes and manager changes as built

Added by WP21; the sections above are unchanged. Branch `wp21` of `tools/scopt` (a development worktree); user doc `tools/scopt/docs/b3-passes.md`; evidence in the maintainer's evaluation data (not in the repository) (`verdict.json`). No QEMU run, no timing.

Where the code follows reality instead of the text above, or adds to it:

1. **Modules.** As planned: `scopt/passes/{specialize_returns,constant_propagation,branch_folding,dead_host_ops,chain_returns,chain_static}.py`, `scopt/analysis/hconst.py`, `scopt/analysis/profitability.py`, `scopt/manager.py`, `pipelines/b3.toml`, `docs/b3-passes.md`. Added: `scopt/manager_b3.py` (the B3 overlay, its checks, `materialize`, `optimize_b3`), `scopt/passes/_b3util.py` (encodings, refusal helper; not a pass module), `scopt optimize --b3` (implied by a pipeline that allows a B3 kind).
2. **Overlay instead of the WorkingSet.** The B2 `WorkingSet.apply` refuses B3 kinds, so B3/B4 edits live in an immutable `B3State` next to it: guarded exits `(R, tail) -> targets` (slot, Vc, GENERAL/SPEC, C, variant, claiming pass, train weight), variants (clone id, C, the one predecessor slot, facts, guard atoms, **current bytes**, patch history), unit rewrites, attached facts. `B3State.apply` checks §3.7's preconditions for a whole proposal; a refusal drops it and the pass row gets the code with `B3Reason.outcome(code)` (`edit_precondition`, `stale_fact`, `conflict` are `failed`). Analyses of the snapshot are not invalidated by B3 edits: passes read the captured units plus the overlay.
3. **Region scope.** A `unit_scope = "region"` pass runs once per variant on a `VariantView` (C's metadata, the variant's current bytes, its facts, the R->C train count as C' entries); with no variant it is `no_change` (`no_variants`). The passes' own analyses of patched bytes use `analysis_unit`, which moves a LABEL_JCC site to the `jmp` that BF wrote (and drops it for a nopped jcc), so hcfg keeps the branch edge. The validators never use it (V12/V15/V16 use C's metadata and the declared patches).
4. **`hconst@1`.** Forward must-constant dataflow on the atomic hcfg view: env values of the 17 fact fields, 64-bit host register constants, ZF/SF/CF/OF/PF. Exact semantics for mov/movzx/movsx/lea/xor/and/or/add/sub/cmp/test/inc/dec/neg/not/shifts by an immediate/setcc/cmovcc; anything else makes what it writes unknown. Kills of every fact field: a call whose flags lack `TCG_CALL_NO_WRITE_GLOBALS` or are unknown, every plugin callback (unnamed helper) whatever its flags, a HOST_ABS store, an OPAQUE memory write, an unclassified env write. A call or a guest access (LDST block) clobbers the caller-saved registers and the flags. Decided branches only in `insn` blocks (never the prefix entry check or an LDST fast-path `jne`); foldable loads only `mov r32, dword [rbp+d]` of a known fact field in an `insn` block. The registered analysis is the no-fact lattice; the passes call `build_hconst` with the variant's facts (the cache key "FactRefs" of §3.5 is the variant's FactRefs, cited in each edit's `dependency_additions`).
5. **Facts.** `callee-summary@1` in the AnalysisStore has no certifier, so `B3Ctx.summaries()` builds the CalleeSummary rows once per run with WP19's V14 certifier (`summary_certifier`, on the captured R); these certified rows are the FactRef targets (ATTACH_FACT `stale_fact` check), the V19 `summaries=` and the V14 provenance (`summary_provenance`). `specialize-returns` uses **all** certified facts of the exit as F (not a minimal subset).
6. **Variant identity.** CLONE carries `variant_key(C, R, E, F, ())` (the patch set is not known yet); `materialize` gives the final id `variant_key(C, R, E, F, composed patches)` (§3.4) and the sidecar maps clone id -> final id.
7. **Edits on clones compose.** Every PURE_REWRITE is checked against and applied to the variant's current bytes; `materialize` writes one patch per original host instruction that changed, relative to the **effective** C (C with its own unit rewrites: NativeLinker v2 clones `eff[C]`), with the template of the last pass that touched it. So DHO's nop of CP's `xor` becomes one patch `8b5d00 -> 669090`. A unit rewrite of a variant source (B4) is applied to its clones too; one that overlaps a variant patch is `conflict` (failed).
8. **Downgrade and ADD_REGION.** A variant whose bytes equal the effective C becomes a GENERAL slot to the general C (`no_simplification`; ledger row, sidecar `x_b3_manager.downgraded`). One manager-generated `ADD_REGION` record per remaining SPEC link (ledger row, sidecar); the multi_tb row itself is written by WP18's `multi_tb_rows` in `package_b3`.
9. **Variants only of un-grown units (fail closed).** WP19 integration note item 4 left open whether the loader registers the slots of a variant struct; a clone of a grown C would carry C's registered GENERAL reset (`exit_tb(C'|n)`). The manager therefore refuses CLONE of a grown unit and REDIRECT_EXIT on a variant source (`unit_not_relocatable`), and the chain passes skip variant sources. Cost on compute: the units that are variant sources keep their own return/static exits unguarded.
10. **Growth budget.** §3.4's `max_code_growth_bytes` (10 % of the 99 %-coverage hot-set bytes, `hotness@1`) budgets specialization: variant bytes plus the stubs of exits that carry a SPEC slot. GENERAL-only stubs have the §3.6 budget of chain-returns/chain-static (≤ 2 slots per unit, 256 units) and are reported, not budgeted. WP18's V21 counted every stub against the hot-set budget, which on F-B3-1 (hot set 4,480 bytes) refused every chain pass; V21 (WP21's body per the shared-files table) now checks `spec_stub_bytes + variant_bytes` and reports all stub bytes.
11. **Profitability (§10.3).** `scopt/analysis/profitability.py` with WP16's constants (7.6 / 1.5 / 0 ns; `load_calibration` reads `calibration.json`). For CP/BF/DHO the design's unit "executed non-nop host instructions removed" is 0 for CP (a load becomes an immediate form) and for a taken BF (jcc -> jmp), so the unit counted is one per folded env load (CP), decided branch (BF) and nopped instruction (DHO), × C' entries.
12. **Pass encodings.** Nops are register-free `66 90` pairs (+`90`), never `nop [mem]`, so patched code has no memory operand an analysis could mistake for an access. CP: `cp_xor_zero` needs EFLAGS dead after the load (`flags_live` otherwise); `cp_mov_imm` (5/6 bytes) fits only `[rbp+disp32]` loads (NF/ZF/CF/VF), so a non-zero r0..r12 fact gives `no_same_length_encoding`. BF: `7x rel8 -> eb rel8`, `0f 8x rel32 -> e9 rel32+1; 90`, or same-length nops. BF without a decided branch reports `fact_unused` (§3.8 has no "no decided branch" code). DHO: register-only allow-list, no memory operand at all, reachable under the facts, every written register and flag dead (`hlive@1` on the patched bytes), iterated to a fixpoint; register-dead but flag-live ops count as `flags_live`.
13. **Pass outcome when nothing is proposed:** the most frequent refusal code becomes the pass reason (`skipped_unsupported`/`skipped_unknown` by `B3Reason.outcome`), all counts are in the stats and the ledger `counts`.
14. **Linker/packaging rejections** are classified by `manager_b3.b3_reject_outcome`: §3.8 classes, plus `LINK_FAILED_CODES` (`link_missing`, `variant_pred_slot`, `unknown_unit`, `patch_*`, `edit_on_fixup`, `edit_in_tail`, `spec_slot_unregistered`, `slot_kind`, `rel32_range`, `multi_tb_region`, …) as `failed` (WP18 review minor). `B3Reason` (a shared contract) is unchanged.
15. **Packaging.** `optimize_b3` = PassManager (B3 mode) -> `materialize` -> WP18's `package_b3` with WP19's `hostsym_inputs` (EnvLayout of the target RuntimeIdentity, contract, provenance from the certified rows, the V18 document). Two backward-compatible additions to `package_b3`: `ledger_rows=` (the pass rows start the artifact ledger) and `sidecar_extra=` (`x_` keys; the manager's `x_b3_manager` report: per-pass byte delta, edits, origins, deps added, downgrades, regions, variant ids). A candidate without any B3/B4 edit goes through the B1/B2 `ValidateAndPackage` (v5, V9 byte identity). A pipeline with both B2 layout edits and B3 edits (the `combined` arm) is refused with a ValueError (v2).
16. **`tbb` and other indirect exits (LLVM survey §3b "promote-indirect-exit").** Not implemented: §3.1 rule 2 refuses `tbb/tbh`, `bx rX`, `blx rX` and `ldr pc,[rX]`/`ldm rX,{..pc}` with a non-SP base as `indirect_multiway`, and §14 defers `chain-indirect`. Deferred with reason code `indirect_multiway`; a unit test checks that no such exit is guarded.
17. **Results (unit stage, static, current `qint` captures).** Evidence in the maintainer's evaluation data (not in the repository) (`fb3_1.json`, `identity.json`, `compute.json`, `run_evidence.py`).
   - **F-B3-1** (WP20-int base snapshots, icount and noicount, steady profile): `specialize-returns` makes one variant of C (0x2f4, `cbz r0`) behind a SPEC slot on R (0x10000) under the certified fact `r0 == 0` (`source = patch`). The composed patches are exactly §3.5's expected bytes: `mov ebx,[rbp]` -> nops (CP's `xor ebx,ebx` then nopped by DHO), `test ebx,ebx` -> nop (DHO), `jne` -> nops (BF). Byte deltas CP 3, BF 6, DHO 4. Disabled in turn: CP off -> BF 6 + DHO 2 (the load stays); BF off -> CP 3 only (DHO `flags_live`: the test stays); DHO off -> CP 3 + BF 6; the three variants are distinct. All three off -> the variant is downgraded (`no_simplification`) and the slot becomes GENERAL. Every artifact (5 per mode) is published with V1-V8, V11-V21 and V23 pass, V9 skip (not an identity candidate), V22 skip (WP23); the all-off artifact has V14 skip (no SPEC link). The full pipeline also guards 22 more exits with GENERAL slots (chain-returns, chain-static): 23 grown units, 1 SPEC + 23 GENERAL slots.
   - **T-B3-7**: identity pipeline, b3 with all six passes disabled, and b3 `--only constant-propagation` (no variant: `no_change`) give a `cache.sc` sha256-equal to the input on F-B3-1 and on compute (6/6, V9 pass, v5 path).
   - **compute** (B2 base snapshot `53b4132f…`, steady profile; reported): 163 return exits; `specialize-returns` makes 4 variants (refusals: 91 `fact_not_constant`, 24 `target_not_profiled`, 17 `fact_unused`, 15 `fact_not_local_to_return_tb`, 11 `not_profitable`, 7 `exit_not_goto_ptr`, 5 `context_budget`, 2 `provenance_unexplained`); CP/BF/DHO change 9/18/12 bytes; 87 facts certified by V14. Full pipeline: 378 grown units, 4 SPEC + 408 GENERAL slots (chain-static capped at 256 units), published with every static row passing in 41.0 s; the `b3-spec` arm (chain passes disabled) in 28.9 s (tool wall time, not a measurement). Hot-set bytes (99 %, `hotness@1`): 180,192 on compute, 4,480 on F-B3-1.
18. **Not run in this stage:** anything dynamic (O1-O6, T-B3-8, the slice-1 runs; WP27/WP28), any timing, V22 (`not_run: WP23 not approved`). The static T-B3-6 acceptance was run on the WP20-int F-B3-1 snapshots, which were captured on the `qint` binary, with WP18's **stand-in** v6 RuntimeIdentity (the compute v2 identity with the qint-v6 exe sha and EnvLayout); the integration stage should repeat it on a base prepared on the gate binary.

## Implementation note (WP25, integration stage, 2026-11-17): B4 on the WP24 fixtures and on compute

Added by WP25; the sections above and the earlier WP25 note are unchanged. Branch `wp25-int` of `tools/scopt` (a development worktree); driver `tests/qemu/wp25_integ.py`, timing `tests/qemu/wp25_timing.sh`; evidence in the maintainer's evaluation data (not in the repository) (`verdict.json`). Every run used the current WP-QINT binary (sha256 `8864c726…`, checked per run), as §7.5 says for the early B4 measurement. The fixture was the WP24 review-3 image (b3.elf `3b029618…`). WP23 is not approved; nothing here depends on it.

1. **Bases and identities.** F-B4-1…F-B4-7 were captured on `qint` in icount (shift 0) and noicount with WP20's `wp20_fixture_snapshots.py` (14/14). Each snapshot's v1 RuntimeIdentity was extended with `admission_facts` of its own config. F-B4-7 (`variable_watch` and FastDyn preprocessing artifacts) gets `tb_start_cb_possible = true`; every other F-B4 config gets `false`. `mem_get_value_outside_mem_cb` is `false` everywhere: no loaded plugin imports `qemu_plugin_mem_get_value`.
2. **Artifacts.** All 14 B4 artifacts (D1|D2|D3) were published as v5 (`qemu-sc-v5-unmodified`), with V12 passing on every rewritten unit and no failing V-row. Deleted stores (icount; noicount differs only for b4-3):

   | Config | D1 | D2 | D3 |
   |---|---|---|---|
   | b4-1 | 17 | 45 | 129 |
   | b4-2 | 20 | 65 | 172 |
   | b4-3 | 20 | 65 | 189 |
   | b4-4 | 8 | 40 | 125 |
   | b4-5 | 10 | 65 | 171 |
   | b4-6 | 8 | 59 | 165 |
   | b4-7 | 8 | 9 | **0** |

   For b4-3 in noicount the counts are 17, 57 and 186. F-B4-7 deletes no D3 store: 13 are refused `observer_contract_unknown` (units with memory callbacks) and the rest `observed_at_sync_point`.
3. **Static fixture checks (§9.5).**
   - F-B4-4: in the loop TB (0xc68, CPUID load as insn 2 of 5), the prefix `can_do_io = false` store is kept as `observed_at_sync_point`, in both modes.
   - F-B4-5: the IT-block `regs[1]` store before the LDST is kept as `observed_at_sync_point`, and no `regs[1]` store is deleted in any unit that holds the faulting LDST.
   - F-B4-7: both FastDyn UPDATE_MEM host stores (`mov byte ptr [r15|r12], 5`) are untouched.
4. **Dynamic (O1/O2a/O2b/O5 vs warm, fixture per-mode oracle).** All 14 (mode, config) cases pass, with 2 reps in icount and 4 in noicount.
   - **Deviation:** O2a is gated only in icount, as §9.4 defines it ("with icount shift 0"). In noicount the timer phase b4-3 has a host-time-dependent exception sequence: warm against warm differs in 2 of 2 runs, just as B4 against warm does. So there O2a is reported next to that control, and O1 and O2b (noicount invariants) are gated.
   - **Non-vacuity:** gdb hardware breakpoints were set on the entries of up to 8 rewritten units nearest the fixture's kernel label, and a rewritten unit was entered in every case.
     - The unit holding the label was rewritten and entered in every icount case except b4-7, whose loop has no deletable store.
     - In noicount b4-3 the gdb run on the hottest units livelocked under SysTick, so those counts are unknown and not 0.
     - Deleted D1/D2 stores in entered units: D1 > 0 on b4-1/2/3/5/7 and D2 > 0 everywhere.
   - **F-B4-4:** `cpu_io_recompile` is called 265 times in the artifact run and 265 times in warm, in both modes.
   - **F-B4-7:** `events.tsv` is identical in every rep (sha256 `2e6cc52e…`, 20,813 bytes).
5. **Mutants.** All 168 B4 mutants of `scopt.b4_mutants` were rejected statically: the first instance of each kind in each icount fixture snapshot, the same kinds in the fixture's loop unit, and the real UPDATE_MEM deletion on F-B4-7, which V15 rejects. `exit_stub_edit` now fails V8 `reimport_status` first, with V15 `edit_in_exit_stub` also failing.

   Twelve were also run as negative controls (O4, bypassing V12-V17; this is WP29's item, run early here):

   **Detected (9):**
   - T-B4-2b `brcond_reload` on F-B4-1 (the guest hangs: `INVALID_exit_code`, O1 and O2b differ);
   - `d1_before_exit` on F-B4-1 (hang);
   - the IT-block D4 mutant `it_block_d4` on F-B4-5 (stacked r1 = 7 instead of 5, in O1 and O2b) and its slow-path variant;
   - `update_mem_delete` on F-B4-7 (result 5 instead of 1500);
   - `guest_store_delete` on F-B4-7 (O1 and `events.tsv` differ);
   - `d1_before_udiv` on F-B4-6;
   - `icount_immediate` and `prefix_icount_store` on F-B4-3 (O2b SysTick positions).

   **Not detected (2):**
   - `d3_before_plugin_call` on F-B4-7. Nothing in that configuration reads `plugin_mem_value_*`, so the mutant is behaviour-preserving there. It stays a static-only control until a fixture loads a plugin that calls `qemu_plugin_mem_get_value`; that is for WP29.
   - `flag_store_before_ldst` on F-B4-2. The only instance is in the entry unit (0xb9a, run once, whose LDST does not fault). The loop unit 0xbae, where the fault at iteration 300 happens, has no such shape.

   **Not run (1):** `flag_store_before_helper` was not found on F-B4-1.

   For WP29: a mutant must sit in the unit that executes the observed event. The first instance in the snapshot is usually in cold code.
6. **Compute (F-B4-8 shape).** V10 on the held-out inputs 1-3 in icount passes for the B4 artifact (all classes), the D1, D2 and D3 ablations, and the `icount-entry-sfb` artifact (the maintainer's evaluation data (not in the repository)). Appended records are equal in artifact and warm (198, 228 and 207: held-out inputs reach new TBs).
7. **Early paired timing (reported outcome; not a gate).**
   - Setup: compute train inputs 1-3, 30 interleaved reps, so 90 pairs per arm; seed 2501; all 540 runs valid with appended 0. The job held the workspace timing lock, ran under `taskset` on cores 8-11, and took 913 s. The 1-minute load average was 2.4-7.0 from other users' jobs on other cores.
   - Speedup = median of t_warm / t_arm − 1, with a 95 % bootstrap CI:

     | Arm | Speedup | 95 % CI | Verdict |
     |---|---|---|---|
     | b4 (D1\|D2\|D3) | +0.19 % | [−0.27, +0.66] % | no significant difference |
     | b4-D1 | +0.20 % | [−0.47, +0.92] % | no significant difference |
     | b4-D2 | +0.19 % | [−0.16, +0.70] % | no significant difference |
     | b4-D3 | +0.20 % | [−0.24, +0.69] % | no significant difference |
     | `icount-entry-sfb` (a baseline-TCG entry fix, **not B4, not caching**) | +11.06 % | [+10.13, +11.70] % | faster |

   - Reading: nop-filling the dead stores that B4 v1 can prove dead gives no measurable speedup on compute, below the ~1 % noise floor, which agrees with WP16's D1 copy. The entry fix reproduces WP16's +10.8 %, this time from scopt's own packager. Per the survey, B4 timing in WP29/WP30 should be layered on top of the entry fix, but that combined artifact was not built here.
8. **Main moved during the stage.** After the runs above, main gained WP21 (unit) and the WP18 follow-up "hostsym certification must cover every changed unit" (`84f9fe3`), and it was merged into `wp25-int`. All 14 fixture artifacts and the 4 compute B4 artifacts were re-packaged on the merged code (the maintainer's evaluation data (not in the repository)). Every one is published again, and its `cache.sc` is **byte-identical** to the one run and timed above. Only the artifact ids differ, because `validation.json` gains the new V12 coverage row (for example 55 → 56 V12 pass rows on F-B4-1). The dynamic and timing evidence therefore applies to the same bytes.
9. **Not run here:** F-B4-9 and F-B4-10 (WP29), anything on the gate binary (the B4 artifacts are v5; WP29 re-collects on the gate binary), and Tier 1 (`not_run: WP23 not approved`).

## Implementation note (WP21, integration stage, 2026-11-17): B3 passes on gate-binary bases

Added by WP21. The sections above are unchanged. Branch `wp21-int` of `tools/scopt` (a development worktree); driver `tests/qemu/wp21_integ.py`; evidence in the maintainer's evaluation data (not in the repository) (`wp21-integ.json`, `verdict.json`). Nothing was timed and the timing lock was not taken. QEMU runs used the frozen gate binary (sha256 `56d8bfb3…`, `qint-v6-gate-1`) and the WP24 review-3 fixture (b3.elf `3b029618…`).

1. **Bases on the gate binary.** WP24 phase F-B3-1 (and F-B3-6, item 5) was prepared, imported and profiled (`steady@b3_bench_start`, untimed) on the gate binary in icount (shift 0) and noicount with WP20's `wp20_fixture_snapshots.py --qemu <gate>`. `scopt admission identity` then wrote each base's real RuntimeIdentity v2 (gate exe sha, EnvLayout `sha256:32440e25…`). This replaces the unit stage's `qint` captures and WP18's stand-in identity (WP21 unit note item 18).
2. **T-B3-6, static part, through the CLI.** `scopt optimize --pipeline b3 --b3 --runtime-identity <v2>` ran the full pipeline, CP, BF and DHO disabled in turn, and all three disabled, in both modes. All 10 artifacts were published. In every artifact V1-V8, V11-V21 and V23 pass, V9 is skip (not an identity candidate) and V22 is skip (`not_run: WP23 not approved`). The all-off artifact also has V14 skip (no SPEC link). V10 is dynamic, see item 4. The variant bytes were checked **independently of the manager**: the variant body in the output `cache.sc` was diffed against the general C record of the same file, and each differing C instruction was decoded with capstone. A relocation is the same instruction with the same absolute target, or the same target relative to its own TB struct. Anything else counts as a patch. Each variant has 3 relocations, and its only patches are §3.5's three sites:

   | case | `mov ebx,[rbp]` (8b5d00) | `test ebx,ebx` (85db) | `jne` (0f85 rel32) | byte deltas |
   |---|---|---|---|---|
   | full | nops (669090) | nop (6690) | nops (669066906690) | CP 3, BF 6, DHO 4 |
   | CP off | unchanged | nop | nops | BF 6, DHO 2 |
   | BF off | `xor ebx,ebx; nop` (31db90) | unchanged | unchanged | CP 3 |
   | DHO off | `xor ebx,ebx; nop` | unchanged | nops | CP 3, BF 6 |
   | all three off | no variant: downgraded `no_simplification`; the R -> 0x2f4 slot is GENERAL (SPEC 0, GENERAL 24) | | | — |

   The jcc is `jne` and r0 == 0 makes ZF = 1, so it is not taken, and the nops are the correct fold. The three single-pass ablations give three distinct variants. The full pipeline guards 24 exits: 1 SPEC (R `f_b3_1` -> C 0x2f4, fact `r0 == 0`, `source = patch`) and 23 GENERAL.
3. **T-B3-7 on the gate bases.** The identity pipeline, b3 with all six passes disabled, and b3 `--only constant-propagation` each give a `cache.sc` that is sha256-equal to the base, with V9 pass (6 of 6). `scopt publish` (a verified copy that includes WP18's hostsym coverage check) accepted all 10 B3 artifacts.
4. **Dynamic (supplementary, reported; WP27/WP28 own the dynamic gate).** `scopt validate --dynamic --oracles O1,O2a,O2b,O3,O5` ran on the gate binary for each of the 10 artifacts. All passed. For the four SPEC artifacts, O3 gave `links_armed = 1` and C' hits = 999 in both modes, so the CP/BF/DHO-patched variant bytes execute on every iteration with O1-equal output.
5. **Finding and fix: the growth budget lost every variant.** The manager checks §3.4's `max_code_growth_bytes` against a pass's whole proposal. On F-B3-6 (two return units `f_b3_6a`/`f_b3_6b` into one continuation), `specialize-returns` proposed two variants totalling 790 bytes against a 619-byte budget (10 % of the hot set). The manager refused the proposal, so the artifact had no variant at all. `specialize-returns` now applies the same budget itself, in rank order (`_fit_growth`): a variant that does not fit is dropped and counted as `growth_budget`, and the rest are proposed. The manager check is unchanged and stays as the backstop. Tests are in `tests/unit/test_wp21_growth.py`; the two real-snapshot tests fail on the old code. After the fix, F-B3-6 keeps one variant per mode. In icount it is `f_b3_6b`'s `r0 == 5` (`guest_code`; CP reports `no_same_length_encoding`, while BF and DHO apply). In noicount it is `f_b3_6a`'s `r0 == 0` (`patch`). The two units have equal train weight, so the tie is broken by uid order. With `growth_fraction = 1.0` both variants are kept. Using params is how WP28 can get both variants on this fixture.
6. **`tbb`/indirect promotion stays deferred (`indirect_multiway`).** The LLVM survey's "promote-indirect-exit" was not built, because §3.1 rule 2 refuses these exits. On F-B3-6 on the gate binary, the unit that ends in `bx r3` (0x588, holding `b3_6_bxr3` at 0x590) is not grown in either mode. The artifact publishes and passes O1/O2a/O2b, and O3 reports `links_armed = 1` with C' hits 333 (icount) and 332 (noicount). The arbitrary-entry iterations reach the general C.
7. **Known gap (not WP21's code): `scopt validate ART` cannot re-validate a v6 artifact.** `validate_artifact` reads `cache.sc` with the v5 reader (`artifact_link`), so on every B3 artifact it exits with a traceback (`not loadable: ('version',)`). `scopt publish` does the verified re-check for v6 artifacts. Static re-validation of v6 belongs to `scopt/validate.py` (WP18) and is left for WP18/WP28.
8. **Not run:** timing (WP28/WP30), compute on a gate-binary base (WP28 item 4; the unit stage's compute run on the `qint` B2 base stands), and Tier 1 / V22 (`not_run: WP23 not approved`).

## Implementation note (WP27, unit stage, 2026-11-17): first vertical slice, slices 0a and 0b on the current binary

Added by WP27. This note does not rewrite the sections above. Branch `wp27` of `tools/scopt` (a development worktree, commit 92e15cc); driver `tests/qemu/wp27_slice0.py`; copy builder `experiments/wp27_inplace.py`; user doc `docs/b3-vertical-slice.md`; evidence in the maintainer's evaluation data (not in the repository) (`verdict.json`, `wp27-slice0.json`). Every QEMU run used `qint` (sha256 `8864c726…`) and the WP24 review-3 fixture (b3.elf `3b029618…`). Nothing was timed and the timing lock was not taken. Slice 1 (plan steps 6-7, T-B3-8) belongs to the integration stage.

1. **Slice 0a and V7.** A B3 artifact is a `.sc` v6 file. For a RuntimeIdentity of `qint` V7 (`v6_capable_runtime`) rejects it (`V7:v6_runtime_required`), and in both modes this is the only failing row. To get the bytes, the static cases therefore ran with the test-only `scopt optimize --v6-exe <qint sha>` override, into an evidence store that is never deployed. The results equal WP21's gate-binary table (WP21 integration note item 2) in both modes. Full pipeline: CP 3, BF 6, DHO 4 bytes. CP off: BF 6, DHO 2. BF off: CP 3, with DHO `flags_live`. DHO off: CP 3, BF 6. All three off: downgraded `no_simplification`, and the slot becomes GENERAL. Each enabled pass that changed bytes has its own ledger row with the same `bytes_changed`. The fact `r0 == 0` (`source = patch`, plan entry `0x10002 r0 <- 0`) is certified by V14. The multi_tb FactRef carries the `row_hash` of the exported CalleeSummary row.
2. **Slice 0b needs a registered GENERAL slot in a v5 file.** WP18's documented v5 mode (`B3Plan(register_slots=False)`) leaves the slot unregistered, and its reset path is then `jmp F`. Such a slot never links on the stock loader, so F would be taken on every execution, and §12's "R's stub F not taken after warm-up" could not hold. The copy is therefore built with `register_slots=True`, which registers `jmp_insn_offset[n]` in R's record and resets it to `exit_tb(R|n)`, as WP16's probe did. The records and bodies are written with the v5 writer, and the ext table is dropped. C keeps its record and offsets; only its code changes, by the 11 bytes of the three slice-0a patches. The other 64 units are byte-identical. The directory has no sidecar or manifest, so no publisher accepts it. Its label file is `EXPERIMENTAL_UNSOUND.json`.
3. **Slice 0b results (icount).** O1 equals warm, including the fixture's exact icount oracle and z = 1000, nz = 0. O2a: 2 exception lines identical. O2b: no handler records in this phase. Both proofs PASS with 0 appended. gdb `-nx` hardware-breakpoint counts on the copy: C `tc.ptr` 1000 (all 1000 iterations run the patched bytes), R's stub S 1000, F 0, and the slot's `exit_tb(R|n)` reset 1. The one reset is the warm-up exit after which the main loop linked the slot; the other 999 entries took the linked jump.
4. **m-CE7 twin on `fixtures/b3`.** The literal rule (0 calls after `benchmark_start`) fails on every run of these fixtures, and this includes the plain warm reference. The cause is `main`'s teardown `MPU_CTRL = 0` after the measured loop, which is one flush counted as `tlb_flush`, `tlb_flush_by_mmuidx` and `tlb_flush_by_mmuidx_async_work` (WP26 note 10). WP27 adds an **ordering twin** that keeps the same counters but stops at each flush and reads the hit counts of hardware breakpoints on C and R's stub. All 3 flushes came after the last execution of C and of R's stub, with counts 1000/1000/0 at every flush, equal to the final counts. So no TLB flush happened while the unsound link or the in-place C could still run, which is the hazard m-CE7 guards against. `verdict.json` reports the literal row as `fail` and uses the ordering row. For WP28: a gate run on `fixtures/b3` needs either this rule or a phase-end marker; otherwise no fixture run can have a clean twin.
5. **"≈ N"** was taken as N − 5 … N for the C and stub hits, and 1 … 5 for the reset hits.

## Implementation note (WP27, integration stage, 2026-11-17): slice 1 on the gate binary

Added by WP27. This note does not rewrite the sections above. Branch `wp27-int` of `tools/scopt` (a development worktree); driver `tests/qemu/wp27_slice1.py`; offline tests `tests/unit/test_wp27_slice1.py`; user doc `docs/b3-vertical-slice.md` ("Slice 1"); evidence in the maintainer's evaluation data (not in the repository) (`wp27-slice1.json`, `verdict.json`). Every QEMU run used the frozen gate binary (`qint-v6-gate-1` = `qint-v6` HEAD `4671d5ceda`, clean tree, exe sha256 `56d8bfb3…`; the driver checks all three before it starts) and the WP24 review-3 fixture (b3.elf `3b029618…`). Nothing was timed and the timing lock was not taken.

1. **Step 6 as built.** F-B3-1 bases (icount shift 0 and noicount) were prepared, imported and profiled on the gate binary, with their own RuntimeIdentity v2 and CalleeSummary rows (`r0 == 0`, `source = patch`, certified). "One variant, one SPEC slot, one link" is the `b3-spec` arm: `scopt optimize --pipeline b3 --b3 --disable chain-returns --disable chain-static`. The full b3 pipeline also adds 23 GENERAL slots (WP21 integration note item 2). Checked from the output `cache.sc` with `read_sc6`: version 6, 1 variant, 1 slot (SPEC, `f_b3_1` → `vc_b3_1`), 1 link (slot 0 → variant 0). The variant bytes are §3.5's full form (CP 3, BF 6, DHO 4, capstone diff against the general C). V1-V8, V11-V21 and V23 pass; V9 and V22 are skip. Each of CP/BF/DHO has its own ledger row, and the FactRef cites the CalleeSummary row. `scopt publish` makes the verified copy, and that copy is what was deployed.
2. **Step 7, O1/O2a/O2b/O3 (both modes).** `scopt validate --dynamic --oracles O1,O2a,O2b,O3,O5` passes against the base's warm cache. O1 includes the fixture's per-mode oracle (z 1000, nz 0 on both sides). O2a has 2 exception lines, identical. O2b has no handler records in this phase. Both post-run proofs are PASS with 0 appended. Loader stats: `links_armed = 1`, `invariant_violations = 0`. gdb `-nx` hardware-breakpoint counts: R's stub S 1000, its fallback F 1, C' 999 (= S − F). The one F is the first iteration. R's first execution happens before C has been activated, and a link arms only when both are live (§5.5). So "C' hits ≈ N − warm-up" is exact here: N − 1. After the phase the stats show one `disarm_tlb` event, which disarms the link (`disarmed_epoch`). This is the fixture's teardown `MPU_CTRL = 0` (WP26 note 10), and it comes after the measured loop. The m-CE7 diag twin is a rule for v5 guarded-slot copies (plan, Runtime rules). It is not needed for a v6 artifact, where the loader's TLB hook does the disarm, and it was not run.
3. **Step 7, O4: the falsified-fact control.**
   - **How the mutants were built.** No pass proposes a false fact, so the controls were built by hand with `scopt.link.B3Plan`, using the same R, slot, C and guard key as the manager. They were packaged with WP26's `package_negative`.
   - **Positive control.** The same hand-built plan with the manager's patches and FactRef publishes through `package_b3` with hostsym, and its variant body and every record body are byte-equal to the manager's artifact.
   - **`falsified_fact_link`.** The variant cites `r0 == 1` and is folded for it: the `jne` becomes `e9 rel32+1; 90`, so `cbz r0` is never taken. **Deviation:** this mutant is rejected statically by V14 (`fact_derivation_mismatch`, certifier 0 / proposer 1) and also by V19 `callee_summaries` (`stale_fact`), because no CalleeSummary row holds that fact (as the WP19 integration note item 7 found). WP26's packager bypasses only V12-V17, so this control was packaged with V19 also bypassed. The driver checks that V19's only failing row is `callee_summaries`/`stale_fact`.
   - **`fold_flip`.** The same bytes, citing the true certified FactRef. It is rejected by V12 and packaged with the default V12-V17 bypass.
   - **Result.** Both controls arm the link (O3: C' 999 hits) and change O1 in both modes (z 1, nz 999 against z 1000, nz 0). In noicount O2b also differs (end counters), and O2a is equal. `--negative-control` gives O4 pass, meaning detected.
4. **X-REHY with the variant (coverage config F-B3-23): finding plus a test-only experiment.**
   - **Finding (for WP28's F-B3-23 / O6).** With `coverage = true` every TB starts with FastDyn's inline LOG_REG ring store, which includes a spin loop (a backward `je`). The B3 toolchain fails closed on it in both modes:
     - the CalleeSummary row of `f_b3_1` has no facts;
     - `specialize-returns` refuses all 7 return exits with `exit_not_goto_ptr`, so the real `b3-spec` artifact has no variant;
     - a hand-built SPEC link is rejected by V13 `guard_template` (R's HostOps are pinned by `unexplained_host_store`), by V12/V13/V14 `validation_inconclusive` (hostsym stops at the backward branch), and by V19 `stale_fact`.
     - `allow_uncertified` does not help, because V13 `guard_template` is structural.

     So today no accepted B3 artifact has a variant under a coverage configuration. F-B3-23's "O6 equal and C' hits > 0" cannot be met by a validated artifact. WP28 must either extend HostOps/hostsym to the LOG_REG store, or report F-B3-23 with this reason.
   - **The experiment.** X-REHY asks a runtime question: does C' need its own rehydration? To answer it, the slice-1 variant was placed into the coverage C. It uses the same three §3.5 patches, located by capstone at 0x56/0x59/0x5b (icount) and 0x50/0x53/0x55 (noicount). It was packaged by WP26's test-only packager with exactly the failing rows bypassed (V12, V13, V14, V19); it is tagged `NEGATIVE_CONTROL.json` and is never publishable. Only O4 mode can deploy such a directory, so the driver reads each oracle separately (O4 = `fail`, meaning no difference, is the expected outcome here).
   - **Result (both modes).** O1, O2a and O2b are equal; `links_armed = 1`; C' 999 hits. O6 passes: FastDyn's LOG_REG ring at `benchmark_end` has the same index (27656) and the same buffer sha256 as warm's. If C' had skipped the store, the index would be 999 lower. C' executes C's baked plugin pointers and logs every execution, so the assumption of §5.5 holds and the `qemu_plugin_supercache_rehydrate` fallback is not needed. Caveat: this was measured with an artifact the validators do not accept (see the finding above).
5. **Not run / not applicable:** timing (not allowed for WP27); V22 and Tier 1 (`not_run: WP23 not approved`); the m-CE7 twin (v5-only rule, item 2); F-B3-7/F-B3-9 (WP28 items per the plan).

## Implementation note (WP29, B4 gate, 2026-11-17): the B4 gate on the frozen gate binary

Added by WP29; the sections above are unchanged. Branch `wp29` of `tools/scopt` (a development worktree); driver `tests/qemu/wp29_gate.py`, verdict `tests/qemu/verdict_wp29.py`, timing `tests/qemu/wp29_timing.sh`, report generator `tests/qemu/wp29_report.py`; evidence in the maintainer's evaluation data (not in the repository) (`verdict.json`); report the maintainer's evaluation report `B4-gate.md` (not in the repository). Every dynamic run used the gate binary `qint-v6-gate-1` (exe sha256 `56d8bfb3…`, re-checked, and recorded per run). WP23 is not approved; nothing depends on it.

1. **Bases on the gate binary.** 18 fixture bases (F-B4-1…7, 9, 10 × icount/noicount), 11 B3 fixture bases (icount; used only as extra O4 detectors), and a compute base whose TOML is re-rendered with `qemu_path` = gate. The B4 deletion counts on F-B4-1…7 and the compute census (all units D1 469 / D2 1665, prefix 678 / D3 5602; M0 D1 = 0; admitted 334/1207/3409) equal WP25's `qint` numbers.
2. **Verdict: the B4 gate does not pass.**
   - Items 2 and 3 pass:
     - F-B4-1…F-B4-9 pass O1/O2a/O2b per mode, 16/16 rows. Every rewritten unit was entered, and D1 > 0 and D2 > 0 executed in every fixture.
     - F-B4-9 ran with its real plugin. Its admission is `tb_start_cb_possible = true`, the last-insn `can_do_io = 1` of TB A is kept, and there is no `cpu_abort`.
     - Compute: V12 passes on 1391/1391 rewritten units, the compute units contain 0 HOST_ABS operands, and V10 on held-out passes.
   - Item 1 fails on dynamic detection; the static part passes:
     - T-B4-1 and T-B4-2 pass. All 636 real and 12 synthetic mutants are rejected. T-B4-2b is detected.
     - 9 of the 11 §9.3 B4 kinds are detected. *(Corrected by the WP29 review-fix note below: 10 of 11.)*
     - `flag_store_before_helper` has no detector in the fixture set. *(Corrected below: the 19 controls covered 16 of the 18 executed instances, not all; the two missing ones were run afterwards; that the flags are recomputed before any reader was inferred, not shown.)*
     - `host_abs_in_env` is synthetic only. *(Wrong; corrected below: F-B4-7 has the operand, and a real instance is rejected statically and detected by O4.)*
   - Item 4 fails as vacuous (item 4 below).
3. **O4 detectors differ from §9.5.**
   - `flag_store_before_ldst` is detected on F-B3-8, not F-B4-2. F-B4-2's faulting load has no live flag store before it.
   - The LOG_REG deletion is detected by O6 (the coverage ring), which is F-B4-10's §9.5 oracle. `o4_detected` reads only O1/O2a/O2b, so the driver reads the O6 row itself.
   - Instance choice matters. The hottest unit in every fixture is Reset_Handler's `.bss` clear, where several mutants change nothing. The driver therefore tries several instances per kind (phase-specific units first) and records every attempt.
4. **F-B4-10: B4 v1 is vacuous under coverage.** `coverage = true` prefixes each TB with a LOG_REG block.
   - The block stores the PC through `index + const`, which is built with `add r64, r64`. HostOps does not classify that pointer, so it pins the unit as `unexplained_host_store`.
   - The block also spins on a full ring through a backward `je`. That makes the host graph cyclic, and hostsym treats any backward branch other than a slow-path return as UNSUPPORTED.
   - Result: 62 of 71 units are skipped and 0 stores are deleted, so T-B4-13's "D1 deletions > 0" cannot hold.
   - A local ring-base classification in HostOps removed the pin. The units then failed as `path_budget_exhausted` (cyclic), so the change was reverted, and WP17 code is unchanged.
   - Fix (WP17/WP19, a shared-contract edit): classify the ring store with its constant base, and treat the LOG_REG block as one atomic host event in hcfg, syncdead and hostsym.
5. **Timing (T-B4-11, reported outcome).** Setup: gate binary, compute train inputs 1-3, 30 interleaved reps = 90 pairs per arm, `flock the workspace timing lock`, cores 24-27, 630/630 runs valid with appended 0. Arm `sfb-b4` is B4 run on the re-imported `icount-entry-sfb` artifact. Results:

   | Arm | Speedup | 95 % CI |
   |---|---|---|
   | b4 vs warm | +0.14 % | [−0.05, +0.56] % |
   | D1 | −0.12 % | CI includes 0 |
   | D2 | −0.08 % | CI includes 0 |
   | D3 | −0.17 % | CI includes 0 |
   | sfb vs warm (baseline-TCG fix, not B4) | +11.27 % | [+10.70, +11.53] % |
   | sfb-b4 vs sfb | +0.37 % | [−0.02, +0.91] % (seed 0; borderline, see below) |

   B4 v1 has no measurable benefit. This is a valid null result. As §7.6's first condition for D5 requires, this also means cross-TB store elimination stays deferred.
6. **Not run:** Tier 1 / V22 (`not_run: WP23 not approved`).

## Implementation note (WP29 review fix, 2026-11-17): host_abs_in_env on F-B4-7, O4 coverage, provenance

Added by WP29 after review; it corrects the WP29 note above, whose wrong bullets are marked there. No other section is changed. Evidence in the maintainer's evaluation data (not in the repository) (`host-abs.json`, `mutants.json`, `verdict.json`); report the maintainer's evaluation report `B4-gate.md` (not in the repository) regenerated.

1. **`host_abs_in_env` has a real instance; the earlier "synthetic only" was wrong.** F-B4-7's FastDyn UPDATE_MEM store is `movabs reg, imm64; mov byte [reg], 5` (imm64 = g_mod7's host address in guest RAM) in 2 units; HostOps classifies both as `HOST_ABS_STORE` (F-B4-10 has 248 more). This is exactly the operand WP19's synthetic mutant patches, and `b4_mutants.DETECTORS` names F-B4-7 for it. §7.5's "no captured unit has a HOST_ABS store" (implementation note item 6 of WP19) holds for minimal/fault/blinky/compute only, not for the WP24 fixtures. New driver step `host-abs` (`tests/qemu/wp29_gate.py`):
   - env address: a gdb `-nx` diag re-run of the gate warm run, hardware breakpoint at the UPDATE_MEM store, `rbp` (TCG_AREG0) = env `0x5555594951c0`; the pointer register equals the captured imm64, so the re-run's layout matches the capture.
   - mutant: imm64 := env + 0x1cf0 (middle of `vfp.zregs`, guest-dead on a Cortex-M3 without FPU). WP19's synthetic uses env + 0x40; on the real process the env base moved 0xc0 between processes with different path lengths (a first run with + 0x40 hit `neg.tlb.f[10].mask`), so the displacement sits deep in an 8 KiB dead region.
   - static: both instances rejected through `package_b4`, with V12 `host_abs_aliases_env` both without `env_abs` (unbound pointer, fail closed) and with `env_abs` = the process env bounds ("inside env"); V8/V3 (re-import, unexplained reference) and V15 fail too.
   - O4: packaged as a byte mutant (`scopt.run.negative`: no re-import), O1 detects both (result 5 resp. 1495 instead of 1500: the 5 goes into env, not g_mod7). A gdb re-run of each control's own artifact run shows the pointer inside env (env + 0x1c30, `vfp.zregs[15]`).
2. **Gate item 1 now fails on `flag_store_before_helper` only** (10 of 11 §9.3 kinds detected). New step `o4-cover` enumerated the kind in all 20 gate fixture snapshots: 18 instances, all in entered units; the 2 untried ones (main's start-up `v7m_mrs` ZF and VF stores) were run; 18/18 tried, none detected. Shown: the image has no `mrs`/`msr` of APSR/xPSR, so those helpers do not read NZCV. Why the stale flags are never observed later is not analysed. F-B4-1 (§9.5's detector) has no instance of the kind on the gate base. The gate verdict is unchanged (fail; items 1 and 4).
3. **Timing:** `sfb-b4` vs `sfb` is **borderline**, not a plain null: the 95 % CI's lower bound changes sign with the bootstrap seed (seeds 0-5: −0.02 % … +0.02 %). `wp29_timing_analyze.py` now records `ci95_by_seed` and marks such arms `borderline`; the other rows are seed-stable. No timing was re-run.
4. **Provenance:** `compute/host_abs_ops.json` is now written by step `compute-host-abs` (2222 units, 0 HOST_ABS ops; 1391 rewritten, 0). The hand-filled `sfb` row of `compute/package.json` is rebuilt by step `sfb-row`, and `recheck-compute` re-ran `package-compute` into the maintainer's evaluation data (not in the repository): every row has the same status, classes, rewritten units and `cache.sc` sha256.
5. **Verdict hardening:** item 2 now checks each row's `runs_qemu_sha256` against the gate sha and, for F-B4-9, a resolved gdb `cpu_abort` count of 0; item 1 counts an O4 detection only if its run was on the gate binary.

## Implementation note (WP28, B3 gate, 2026-11-17): the B3 gate on the frozen gate binary

Added by WP28; the sections above are unchanged. Branch `wp28` of `tools/scopt` (a development worktree); driver `tests/qemu/wp28_gate.py` + `tests/qemu/wp28_steps.py`, timing `tests/qemu/wp28_timing.sh` + `wp28_timing_analyze.py`, report generator `tests/qemu/wp28_report.py`, acceptance `tests/acceptance/test_b3_gate.py`, user doc `docs/b3-gate.md`; evidence in the maintainer's evaluation data (not in the repository) (`verdict.json`); report the maintainer's evaluation report `B3-gate.md` (not in the repository). Every dynamic item ran on `qint-v6-gate-1` (exe sha256 `56d8bfb3…`, re-checked; tag = HEAD, clean tree) and the WP24 review-3 fixture (b3.elf `3b029618…`). WP23 is not approved: V22 and T-B3-29 are `not_run: WP23 not approved`.

1. **Verdict: the B3 gate does not pass; the only blocker is F-B3-23 (O6).** All other spec-gate items (two-TB example, every sub-case in both modes, static rows on 62 fixture + 4 compute artifacts, the mutation suite, O4 for every dynamic-set mutant, F-B3-24, T-B3-27, T-B3-28, T-B3-42) and every hardening item (F-B3-13 a-d, 14, 15, 17, 18, 21, 22, T-B3-26) pass. T-B3-26, T-B3-27 and F-B3-18 are cited from WP22/WP26 (same exe sha); the rest ran in WP28.
2. **F-B3-23.** Under `coverage = true` every unit is `unit_not_relocatable` / `exit_not_goto_ptr` because FastDyn's LOG_REG ring store pins HostOps (`unexplained_host_store`), and its spin loop is a backward branch hostsym does not model, so the real b3 pipeline produces a v5 identity artifact. O6 on it passes (ring equal) but `C' hits > 0` cannot hold. The runtime is not the problem (WP27's test-only X-REHY variant logs every execution). Fix belongs to WP17/WP19 as a shared-contract edit (classify the ring store by its constant base; one atomic LOG_REG host event in hcfg/hostsym), the same root cause as WP29's F-B4-10.
3. **Artifacts as built.** Full `b3` pipeline on every fixture base with `specialize-returns.growth_fraction = 1.0` (tiny fixture hot sets; WP21 note 5); compute uses default params. A fixture whose passes make no B3 edit (F-B3-23) is packaged by the v5 writer (V1-V9), as §9.6 "Identity" says.
4. **Reality vs §9.5, recorded (none of these is a code change):** F-B3-6h's R is refused with `exit_not_goto_ptr` first (TCG's handler-mode `bx lr` EXC_RETURN check ends the TB with `exit_tb`), `handler_mode_return` is its second reason; F-B3-6's f5 R gets its own `guest_code` variant (r0 == 5) instead of `no_fact`, and the arbitrary entry shows as general-C hits (334); F-B3-6r specializes `rec_b3_cont` from both returns; F-B3-17's RAM continuation is refused `target_not_profiled` (the `allow_ram_resident` admission side is still not wired into the manager); F-B3-12/13(a) arm no link at all (CF_BP_PAGE, WP22 finding), F-B3-13(b) refuses arming 700 times; F-B3-16(b) is refused at deploy (`bundle_mismatch`) rather than INVALID after the run; F-B3-16(d) needs the direct rerun (WP24 finding 12): `links_armed = 0`, output = `16d.norm`.
5. **O2a is not an oracle for noicount timer phases.** Warm-vs-warm of the same base in noicount differs in `-d int` for 6h, 7 and 20 (the maintainer's evaluation data (not in the repository)); those rows are assessed by O2b and the fixture's noicount invariants (R14). Every icount O2a passed.
6. **O4 controls are built from the manager's own plan** (in-process `PassManager` + `materialize`, linked byte-identical to the published artifact), packaged by `package_negative`. Detectors that differ from §9.3: drop_g_pc on F-B3-6r and the F-B3-11 held-out input (on F-B3-6 every R returns to a single Vc); guard_stub_store on F-B3-9 (equivalent on F-B3-1: r1 dead); guard_before_effect must hoist above the `regs[15]` store (the thumb store is an equivalent mutant except on F-B3-22); icount_immediate on F-B3-7 drops C's `dec ebx` (1-insn C, icount only); nop_dead_result_ldst is a declared rewrite (V12 + V15 `edit_inside_ldst_block`) detected in icount only. Byte mutants on placed units also bypass V2/V3, the declared LDST rewrite V3/V8, fact mutants V19.
7. **Static mutation suite.** 27 synthetic, 3034 real same-length and 16881 WP19 link mutants on the gate fixture bases, plus 5818 guard mutants on the manager's own links: all rejected, except `drop_g_cond` on one F-B3-3b unit (0x452) that reads `condexec_bits` itself, so hostsym proves the atom on every reaching path (equivalent for hostsym); the full validator rejects it (V13 `guard_template`). T-B3-42: 678 return units (fixtures + compute), 0 U-COND counterexamples.
8. **Harness findings.** `dynamic_oracles(..., quiet=True)` sets FastDyn `log_level=30`, which is part of `contract_id`, so every deploy is refused `bundle_mismatch`. WP22's host-gdb loader-event harness did not finish F-B3-13(d) on a manager-built artifact within 600 s (1401 `tb_flush` events); 13(d) runs through `direct_guest_scenario` (direct rerun, `QEMU_SC_V6_STATS`, host gdb counting C' only): armed 1, C' 299, z 999 / nz 1. `scopt.run.diag.gdb_hits` needs a WP8 run dir; direct runs use their own counter.
9. **F-B3-15 uses a test-only artifact.** The direct mps2 path has no ExecutionBundle/RuntimeIdentity, so V7/V8/V20 cannot pass; a hand-built SPEC plan (`alias_f` → `alias_vc`, §3.5 patches) was packaged with exactly those rows bypassed (never publishable). Output = warm = `expected.norm`, link armed, C' 999, F 1001 (g_pc fails on every return through the 0x400000 alias).
10. **Performance (reported, not gating).** Compute base on the gate binary (train inputs, `steady@0x2990`), arms on held-out inputs 1-3, 30 interleaved reps (90 pairs per arm), `flock the workspace timing lock`, cores 24-27, 450/450 valid, appended records identical across arms per input. Median paired speedup vs warm [95 % CI]: `b3-chain` +1.91 % [+1.67, +2.20], `b3-static` +4.71 % [+4.44, +5.12], `b3-spec` −0.02 % [−0.45, +0.34] (no measurable effect), `b3` (all six passes) +6.22 % [+5.96, +6.64]. The gain comes from the GENERAL guarded slots (replacing `lookup_tb_ptr`), consistent with WP16's guarded-slot probe; the SPEC variants and their CP/BF/DHO bytes (4 variants, 39 bytes) give no measurable benefit on compute.

## Implementation note (WP28 review fix, 2026-11-17): F-B3-12 non-vacuous, F-B3-13(a) criterion, F-B3-17 test-only link

Added by WP28 after review; the sections above (including the WP28 note just above) are unchanged; where they differ, this note supersedes them.

1. **F-B3-12 (code-page invalidation) was vacuous in the first WP28 run and is now tested on an armed link.** WP24's `diag/f-b3-12.gdb` sets its first breakpoint on Vc's 1 KiB page, so C is looked up with CF_BP_PAGE, never activated from the cache, and no link is ever armed (`links_armed 0`, C' 0); the earlier "pass" (and the C' 999 non-vacuity, which came from the plain run without a debugger) did not test §9.5's "C' hits stop after K". The F-B3-12 kernel has no PAUSED hook, so the scenario now runs on the F-B3-13 kernel (same SIMPLE_CALLEE shape, `vc_b3_13: cbz r0`) with its diag input (spins at K = 300), as WP22's integration note recommended: Ctrl-C = RUN_STATE_PAUSED stop (no flush, G11) with the link armed, debug write `cbz` -> `cbnz` on `vc_b3_13`, release, continue (`wp22-13-invc.gdb`), on WP28's published b3-pipeline F-B3-13 artifact through WP22's host-gdb harness. Both modes: links_armed 1 (unchanged), C' 299 hits before the write and 0 after, R's SPEC slot before the write `dest` = C' and after it `at_reset`, `dest` 0, C invalid (slot reset observed), z 300 / nz 700 = warm with the same write (the same scenario on the warm v5 cache). WP24's breakpoint script is kept as a supplementary, explicitly vacuous row (`F-B3-12-wp24bp`). Evidence in the maintainer's evaluation data (not in the repository) rows `F-B3-12/<mode>`.
2. **F-B3-13(a): §9.5's `refused[bp] > 0` is unreachable as worded on this loader.** With the breakpoint on Vc's page C is never activated from the cache, so no arming attempt is made and nothing is refused (observed 0 in both modes). WP28 assesses 13(a) on the substituted criterion "never armed, C' never entered, hit count and results equal warm" and reports it as a pass *on a substituted criterion* (`verdict.json` hardening `F-B3-13a.criterion`). The arming refusal while a breakpoint exists is exercised by F-B3-13(b) (`refused.breakpoints = 700`; breakpoint not on C's page).
3. **F-B3-17 now has a non-vacuous SMC check.** The production pipeline links nothing into the RAM continuation (`target_not_profiled`; `allow_ram_resident` is still not wired into the manager), so the production artifact only shows equal outputs. A test-only plan (WP27 slice-1 shape: f_b3_17's return grown with one SPEC slot to `smc_cont`'s RAM copy at g_smc + 0x16, the snapshot's `cbz` record chosen by guest hash, one variant with §3.5's three patches and the certified r0 == 0 fact) passes every static row with nothing bypassed (it is NEGATIVE_CONTROL-tagged only because it is built outside the manager). Dynamic, both modes: link armed, C' 299, F 701, the guest's own `strh` at K = 300 invalidates C and C' is never entered again, O1/O2a/O2b equal warm. F-B3-17 now requires both. Evidence in the maintainer's evaluation data (not in the repository).
4. **Other review fixes.** F-B3-23 is computed from `coverage.json` (O6 per mode plus C' hits on the published artifact's variants), so it flips by itself once WP17/WP19 allow a variant under coverage; it still fails today. T-B3-27 is assessed from WP22's evidence files (gate JSON verdicts and per-test checks, pytest summary lines; an earlier failure counts only when a later passing re-run supersedes it), not from verdict text. The timing CPU budget (cores 24-27, 4 cores, lock, load averages) is recorded in `timing-analysis.json` and `verdict.json`. Diagnostic re-runs (never timed) show every SPEC variant of `b3-spec` entered on every held-out input (0.19-0.23 M C' entries per held-out run), so its null timing result is not a coverage gap. The O4 table distinguishes divergence from crash detections (drop_g_pc on F-B3-11, guard_before_effect and search_row are crash detections) and names the one-mode mutants. `scopt.run.diag.gdb_hits` accepts direct run directories (F-B3-15 now counts through it); `dynamic_oracles(quiet=True)`'s `bundle_mismatch` (log_level is part of `contract_id`) is documented and named in the refusal detail. The 450 per-run timing cache copies and the 450 per-run `work/io.log` files (7.4 GB) were deleted after hashing (the maintainer's evaluation data (not in the repository), `deleted-iologs.jsonl`); the analysis reads only `runs.jsonl`.

## Implementation note (WP30, unit stage, 2026-11-17): the B5 campaign as defined and frozen

Added by WP30; the sections above are unchanged. Branch `wp30` of `tools/scopt` (a development worktree); doc `tools/scopt/docs/b5-campaign.md`; report the maintainer's evaluation report `B5-plan.md` (not in the repository); evidence in the maintainer's evaluation data (not in the repository). Nothing was timed.

1. **Files.** The definition is `campaigns/b5-freertos.def.toml` and the frozen campaign `campaigns/b5-freertos.toml` (sha256 `9b937039…87eca`, generated by `tests/qemu/b5_campaign.py freeze`; `check` is the freeze checker). The plan's name `b3b4-freertos.toml` was not used (task naming `b5-*.toml`).
2. **§10.1 arms that cannot be built: `semantic`, `combined` and the `semantic-no-<pass>` ablations are `skipped_unsupported` (`capability_not_enabled`).** `optimize_b3` certifies B3 edits with `build_contract` and has no `need_d2`/`need_d3`; B4 deletions are certified only by `package_b4` (`StrictCallContract`, V15 deletable class), and B2 layout edits plus B3 edits are refused (WP21 note 15). Substitutes timed instead: `b3` (all six B3 passes, the B3 part of `semantic`) and `b4`; ablations `b3-no-<pass>` on top of `b3` and `b4-D1/D2/D3` on top of `b4`. The report shows the unsupported arms as rows in every split.
3. **Arms added to §10.1:** `b3` (above), `sfb` (baseline-TCG fix, category `baseline_tcg_fix`, never counted as caching/B3/B4) and `sfb-b4` (parent `sfb`), per the WP16 decision and the WP25/WP29 notes. Excluded: `layout-no-hotcold-partition` (degenerate), WP16's experimental copies (incl. `cmov`, V15 refusal), blinky (no gate-binary base). `identity` is byte-identical to `warm` and kept as the A/A control.
4. **Sets:** train (calibration/model check only: the WP16 constants are qint's, D-EV-2), held-out and final, each split as a core set and an ablation set; 30 interleaved paired reps × 3 inputs per arm; final last; `run` holds the timing lock, ≤ 4 CPUs.
5. **Held-out inputs were seen before the freeze** by WP28's reported B3 rows on the gate binary; recorded in the frozen file. No candidate was re-tuned; the final split has no gate-binary timing.
6. **§10.2 metrics as implemented** (`scopt/report/b5.py`): removed lookups = v6 ext slot × profile `lookup_hit` edges (reproduces the passes' `train_lookups_guarded` exactly); removed stores = NOP-filled `mov [rbp+d]` per unit × entries (reproduces WP29's `x_classes` exactly); region coverage MEASURED counts SPEC links and C' entries only (GENERAL slots are not links in `qemu-sc-v6-stats`); cold-path latency is DERIVED as `qemu.wait − BENCH` (start-up + teardown; no timestamp at `benchmark_start` exists).

## Implementation note (B3/B4 audit fix, 2026-11-17): T-B1-9 v6-stamped copy under T-B3-27

Appended after an independent audit of the B3 gate; the sections above are unchanged. Evidence: the maintainer's evaluation data (not in the repository), scopt `b3b4-auditfix` commit 2932762 (merged into main as 08a6834).

1. **Conflict between T-B3-27 and §5.1 for one input.** T-B3-27 (§9.6, plan WP22) asks for "identical verdicts" / "byte/behaviour identical" B1 T-B1-* results on `qint-v6`. B1's T-B1-9 case "post-run proof on a v6-stamped copy gives `INVALID_artifact_not_used`" (B1-design §4.8, T-B1-9) describes the v5 loader (§5.1 row 2: `valid_header` fails, the copy is truncated, the proof's `header` check fails). A v6-stamped copy is a v5 file with its version field set to 6, i.e. a version-6 header with feature EXT_TABLE clear, so on the v6 loader §5.1 row 3 and §5.2 [R2] apply instead: `supercache: invalid v6 extension: feature EXT_TABLE clear`, exit 1 before guest execution, file untouched. The two requirements cannot both hold for this one case; §5.1/§5.2 win, and T-B3-27 is read as "identical except where §5.1 prescribes a different v6-loader behaviour" (this is the only such case in T-B1-0…9 and F-B2-1…6).
2. **What happened in WP22.** The first T-B3-27 run of T-B1-9 on the gate binary failed this case (the maintainer's evaluation data (not in the repository): `assert 'PASS' == 'INVALID_artifact_not_used'`, proof PASS because nothing was rewritten). The test was then made loader-aware (scopt-wp22 commit 8c53614) and the suite re-run (`t-b1-9-rerun`, 24 passed). WP22's the maintainer's evaluation data (not in the repository) recorded this, but no design note or gate report did; the B3 gate report presented the re-run as superseding the failure. That is corrected in the maintainer's evaluation report `B3-gate.md` (not in the repository) §6.
3. **Amended criterion (as built after the audit).** On the v6 loader the B1 intent of the case, "a run that could not use the artifact is never a valid sample", is carried by the run verdict, not by the proof: the test asserts the fail-loud message, exit != 0, run verdict `INVALID_exit_code`, proof `PASS` (file byte-identical) and no guest output. On the v5 loader it keeps proof `INVALID_artifact_not_used` with `failed == ["header"]` and now also asserts the run verdict and the absence of the v6 message. The branch is chosen by the exe sha256 (`scopt.validate.v6_capable_exes`), not by the QEMU log under test (8c53614 chose it from the log, so a v6 loader that silently truncated would have passed through the v5 branch).
4. **Re-run (not timed, no lock):** the full T-B1-9 suite against WP22's minimal gate dir on the gate exe `56d8bfb3…` and against the maintainer's evaluation data (not in the repository) on qint `8864c726…`: 24 passed each; the case took the v6 branch (exit 1, `INVALID_exit_code`, proof PASS) and the v5 branch (exit 0, `INVALID_artifact_not_used`, `failed ['header']`) respectively.

## Implementation note (close-out, 2026-11-18): LOG_REG / HostLog, F-B4-11, re-gate drivers

Added after the gate close-out (design `closeout-logreg.md`); the sections above are unchanged, and where they differ this note supersedes them. Code: scopt main 36a28a9 (close-out A/B), 494023c (closeout2: ring base never `abs.heap`, B5 re-freeze), and the CO-6 driver commits abc06c4 and 1671ae9 (main). Evidence: the maintainer's evaluation data (not in the repository) (`b3/`, `b4/`, `fixture-b4-11/`, `manifests/`). Reports: the maintainer's evaluation report `B3-gate.md` (not in the repository), the maintainer's evaluation report `B4-gate.md` (not in the repository) (section "Close-out (2026-11-18)").

1. **Model extension (closeout-logreg.md A.2-A.6).** FastDyn's `coverage = true` LOG_REG block (65 bytes before guest insn 0 of every TB: index load, ring-full spin `je` back to the block start, ring store through an `add r64,r64` pointer, index store) is one atomic host event `HostLog(pc)`. `scopt/decode/logreg.py` is the single, pure matcher shared by HostOps and hostsym: exact bytes outside the three rel32 holes and the `regs[15]` displacement, MOVI_CONST / LABEL_JCC metadata agreeing with the bytes, no other site, OpMark or entry inside the block, IDX/CONS bound `abs.plugin_data`, BUF bound to a nameless anonymous mapping only (`abs.heap` and every other class refused, closeout2 rule 5), one (IDX, CONS, BUF) triple per snapshot (V15 rule 7). Any deviation is `NoMatch` and the unit is treated exactly as before (pinned / cyclic / unsupported: fail closed).
   - HostOps types the ring store `HOST_ABS_STORE` at the ring base and marks the block ops (`log`, `spin`); every non-block unit's HostOps digest is unchanged.
   - hcfg adds the spin edge in the full view only (`spin_edges`); any other back edge stays cyclic. hconst kills no env fact at a block store. syncdead, V15 and manager placement refuse every edit inside the block with the new FAILED reason code **`edit_inside_log_block`**; edits after the block follow the existing rules.
   - hostsym re-matches the block on the program's own bytes, executes the loop head once (spin not forked, assumption recorded), turns the ring store into one `host_log` event whose pointer class is the new **`abs.logreg_ring`** (only the block's own ring store; extent checked against `env_abs`), requires the candidate block to be byte-identical to the original, and exempts only the matched spin from the backward-branch rule. V18's synthetic suite gained L1-L8 (all rejected); `b4_mutants` gained the real kinds `log_reg_ring_store` (L1), `log_reg_index_store` (L2) and `log_reg_value_field` (L7), detected by O6, reported and not gating.
2. **Fixture F-B4-11 (closeout-logreg.md B.2).** `fixtures/b3/b4_11.s` in the variant image `b3_b411.elf` (`Makefile.b411`, `phases_nob41.o` + `b4_11.o`; no existing image or object rebuilt) runs three sub-phases (A: `cmp; mrs APSR`, B: `cmp; mrs PRIMASK; IT readers`, C: `cmp; printreg callback; IT readers`) after a poison TB (`msr APSR_nzcvq` of 0 / 0xF8000000), over 8 compare pairs. Config `b4-11` (phase 0x201). The correct output is independent of the poison (`expected.b411_words`); a deleted flag store makes exactly the predicted words differ (`expected.b411_divergence`). Qualified on the gate binary in both modes (the maintainer's evaluation data (not in the repository)).
3. **Disclosed detector change (§9.5).** §9.5 named F-B4-1 (printreg) as the dynamic detector of `flag_store_before_helper`. F-B4-1 has no instance of the kind on the gate base (its printreg TB starts after the `bne`), and the 18 earlier instances (F-B3-21, F-B3-5, main's start-up `v7m_mrs`) store guest-dead flags (closeout-logreg.md B.1). The detector is F-B4-11 (`b4_mutants.DETECTORS`, `wp29_gate.O4_EXTRA` first fixture, `O4_SPECIFIC`, `verdict_wp29.SPECIFIC_DETECTOR`). The B4 gate criterion "every §9.3 mutant rejected statically and detected dynamically" is unchanged; for this kind the driver now requires every F-B4-11 icount instance to be detected by O1 with exactly its predicted divergence (an O1 difference anywhere else is an `anomaly` and does not count); noicount is reported.
4. **Re-gate drivers (CO-6).**
   - `tests/qemu/wp29_gate.py`: `b4-11` in `CONFIGS` (kernel label `b4_11_a`, symbols of `b3_b411.elf` resolved by a fallback in `sym`), the real-pass static check `b4_11_static` (every instance kept, `observed_at_sync_point`), `o4-extra` every-instance mode with `b411_predicted`, O6 detection for every LOG_REG kind in rounds 1 and 2 (`o6_detects`), the `b4-10` diagnosis now counts matched / unmatched blocks, HostOps pins, cyclic units, syncdead status and rewritten units, `deletions --only`, and the re-gate steps `census`, `pytest-static`, `compare-ref`, `carry-timing` (`--ref <WP29 evaluation dir>`).
   - `tests/qemu/verdict_wp29.py`: item 1 assesses `flag_store_before_helper` on the F-B4-11 every-instance record (gate binary, predicted divergence); item 4 additionally requires the F-B4-10 artifact to be published with D1 > 0 in both modes, executed D1 > 0 in icount (gdb entry counts, `deletions.json`; the item-2 non-vacuity rule) and its dynamic rows on the gate binary with O6 gated; `reported_f_b4_11_real_pass` (B.3 step 5) is reported, not gating.
   - `tests/qemu/wp28_steps.py`: new step `coverage-neg` (every C' of the F-B3-23 artifact with its LOG_REG ring store nopped, test-only, V12-V17 bypassed; O6 must detect it while C' runs), item 4a = `coverage` **and** `coverage-neg` in both modes (`item_4a`; a stricter, added check, disclosed), and the re-gate steps `compare-ref`, `carry` (byte-identical artifacts only; `carried.json`) and `compute_static` (static re-packaging; V10, C' hits and timing carried only when byte-identical) with `--ref <WP28 evaluation dir>`.
   - Report generators (`wp28_report.py`, `wp29_report.py`): `--append-section FILE` appends hand-written sections verbatim and checks that they appear byte-for-byte; the maintainer's evaluation report `B3-gate.audit6.md` (not in the repository) holds B3 §6 "Audit corrections" (extracted verbatim from the WP28 report), whose inline item-5a notes the generator now emits; narratives for item 1 / item 4 / 4a follow the verdict; F-B4-11 is named as the `flag_store_before_helper` detector; Known gaps add the unmatched LOG_REG variants (logger form, FP `arm_log_fp`, `movabs`, other allocations: `NoMatch`, fail closed, not exercised).
   - Unit tests: `tests/unit/test_wp28_gate.py`, `tests/unit/test_wp29_gate.py` (synthetic evidence for the new rules).
5. **Re-gate results (closeout-logreg.md C.2, gate binary `56d8bfb3…0d90`).**
   - **B4 FAIL → PASS** (the maintainer's evaluation data (not in the repository)). Item 1 passes: 11/11 kinds; `flag_store_before_helper` is detected on 12/12 F-B4-11 icount instances with exactly the predicted divergence (noicount 12/12, reported); static rejection is 820/820 real and 21/21 synthetic. Item 4 passes: on F-B4-10, 62/62 blocks match with 0 pinned and 0 cyclic; D1/D2/D3 17/45/129 are deleted per mode and executed in icount; O1/O2a/O2b/O5 + O6 pass in both modes. Items 2, 3 and 5 are unchanged: every F-B4-1…9 and compute artifact is byte-identical to WP29's.
   - **B3 FAIL → PASS** (the maintainer's evaluation data (not in the repository)). 4a passes: F-B3-23 is v6 with a SPEC link `f_b3_1` → `vc_b3_1` in both modes, O6 equal, C' 999 hits, and `coverage-neg` is detected by O6. The control's ring index equals warm's (27656), and the buffer differs. The design had expected the index to be lower by the C' hits; that does not hold, because nopping only the ring store keeps the index store. 60 of 62 fixture artifacts are byte-identical to WP28's; only F-B3-23 changed. The static mutation suites grow (real 3133, link 17425, manager 6009, synthetic 36) and are still 100 % rejected, apart from the known `drop_g_cond` on F-B3-3b, which the full validator rejects.
   - **Timing is not re-run.** Every timed compute arm of WP28 and WP29 is byte-identical (sha256 in both reports). The base manifests are identical before and after. The frozen B5 campaign `check` still passes at main 1671ae9 (`campaigns/b5-freertos.toml` sha256 `e095bf1f…`, the maintainer's evaluation data (not in the repository)). The non-qemu suite passes: 1391 passed, 1 skipped, 20 deselected, 3 xfailed. The 3 xfailed are the WP28/WP29 acceptance tests, which read the archived failing evidence.
