# Helper-call specialization for `.sc` code: design for the `hs-*` pass family

**Status.** Design proposal, to be implemented by one graduate student. Written 2026-11-18; revised the same day after a third review, see §12. Nothing in any source tree, scopt `main`, a QEMU worktree or a campaign was changed to produce it. No timed run was made, and the workspace timing lock was not taken.

**Where this comes from.**
- Three scout reports: a hot-helper census, a QEMU/TCG mechanics study and a scopt integration study.
- Three competing designs:
  - A: hand-written fast-path templates;
  - B: importing helper machine code from the binary;
  - C: fact-driven specialization.
- Two independent reviews, one on soundness and one on payoff and student fit. Both recommended C as the base, with parts grafted from A and B, and listed must-fix items. Every must-fix is applied here; §11 shows where.
- A third review of the first draft. All its issues were verified and applied; §12 shows where, and where this revision differs.
- Spot checks made while writing this document: offline disassembly of the hot units, and re-reading the cited QEMU and scopt source.

The supporting material (scripts, per-site JSON, gdb logs) was kept in a temporary working directory (called `H/` below) that is not part of the repository and was not kept. Every fact this design depends on is restated here with its label and source, and phase P0 re-derives the numbers.

**Number labels.**

| label | meaning |
|---|---|
| **MEASURED** | Observed in an artifact, the binary, a gdb diag run, or a cited report |
| **MODEL** | Profile TB executions × static sites, or a stated cost model |
| **DERIVED** | Arithmetic on labelled numbers, or a deduction from cited source |
| **ESTIMATE** | Judgement, with a range |
| **[I]** | An inference that still needs a test or a reviewer |

**Paths.**

| short | path |
|---|---|
| `Q` | the gate QEMU tree, tag `qint-v6-gate-1`. This is the gate tree; every QEMU `file:line` refers to it. The exe is `Q/build/qemu-system-arm`, sha256 `56d8bfb3…0d90`; it is called "the gate binary" below |
| `S` | `tools/scopt/scopt` (scopt `main`, read only for this design) |
| `T` | `tools/scopt` (tests, experiments, pipelines) |
| `D`, `R`, `N` | `D`: this directory (`docs/design/`); `R`: the maintainer's evaluation reports, `N`: the maintainer's notes (both not in the repository) |

**Workload.** Everything is measured on the B5 compute base:
- snapshot: the maintainer's evaluation data (not in the repository) (v5, 2222 units);
- train profile: the maintainer's evaluation data (not in the repository), `sha256:39e2e7c7…` (3 runs);
- train warm BENCH median **934 ms** (MEASURED, `R/B5-evaluation.md:130`).

---

## 1. Summary and recommendation

### 1.1 What to build first

**Recommendation.** Build two **in-place, same-length** rewrites of the FreeRTOS `MSR BASEPRI` call pair. They are needed in every critical-section enter and exit. **Ship them in this order** (§7.1):
1. **First ship: F13 + C2 alone.** C2 carries ≈ 81 % of the modelled value (§1.2). It needs only the rebuild identity axiom and a *write-set* row for `v7m_msr(0x811)`. It needs no key facts, no CPU-model facts beyond the `cortex-m3` admission that V20 already checks, and no closure of the `secure == 0` question.
2. **Second ship: C1** on top of C2, once its key/CPU facts and D3 certificates are in place.

| pass | what it does | code change |
|---|---|---|
| `hs-msr-fold@1` (called C1 below) | Replaces `call helper_v7m_msr(env, 0x811, val)` by the helper's known effect, `env->v7m.basepri[0] = val & 0xff` | The 14-byte tail `mov esi,0x811; mov rdi,rbp; call [rip+d]` becomes `movzx edx,dl; mov [rbp+0xc04],edx; nop5` |
| `hs-hflags-elide@1` (C2) | Deletes the `call helper_rebuild_hflags_m32_newel` that follows. A BASEPRI write changes no input of the rebuild, so the rebuild writes back the value already in `env->hflags` | The 9-byte `mov rdi,rbp; call [rip+d]` becomes `nop9` |

**Properties.**
- No stub, no code growth, no linker placement, no change to search rows or icount.
- The trailing `exit_tb(0)` stays.
- The artifact stays **v5** and keeps B3 invariant 3 (`D/B3-B4-design.md:62`).
- Both rewrites are proven by `hostsym`, extended with an exe-bound **helper-axiom table**.
- The facts C1's proof uses are decoded from the TB key the unit was entered with, **after removing the bits the lookup adds that are never stored in env** (§4.2 item 2), plus the CPU model. The same TB key is what TCG already trusts when it picks `mmu_idx`.
- AX-H, and so C2, is claimed only for runs without `-loadvm`/`-incoming` (§4.2, §4.10).

**Prerequisite.** Fix a soundness gap in `hostsym` first (F13, §4.6). Today a `NO_RWG` helper call havocs no env memory, yet `rebuild_hflags_m32_newel` writes `env->hflags`.

**Why these two and not "real inlining".**
- After `helper_lookup_tb_ptr`, which belongs to B3, the only helper work large enough to matter is the MSR pair:
  - **1.08 M pairs per run** (MODEL; 1,076,941 `v7m_msr` hits MEASURED on train-1);
  - **49 + 204 = 253 host instructions per pair** (MEASURED, gdb `stepi`, 6/6 samples).
- Every other helper is either too cheap to matter or must stay out of line:
  - `udiv` runs 1.5 M times per run, but its body is 8 instructions.
  - `shl_cc` and `v7m_mrs` each run about 70 k times per run.
  - `exception_*` never return.
  - The softmmu slow paths are a TLB-conflict problem, not a call-overhead problem (§3.3).
- For the MSR pair, the inputs that the helper tests at run time (privilege, handler mode, security state, CPU features) are already fixed by the TB key and the CPU model. So the best "inline" is partial evaluation down to one store, plus deleting the identity rebuild. Copying the helper body is not needed.

### 1.2 Honest expected payoff

| item | pairs/run | saving/pair | ms/run | share of 934 ms |
|---|---|---|---|---|
| **v1 core**: C1+C2 on the 3 hot units with no LDST before the MSR (0x1b24, 0x1b4e, 0x1b7a) | 0.928 M MODEL | ≈ 13.5 ns MODEL (≈ 250 insns × κ). ESTIMATE 6.7-27 ns, for κ uncertain ×0.5-×2 | 12.5 MODEL; 6-25 ESTIMATE | **≈ 1.3 % MODEL; 0.7-2.7 % ESTIMATE** |
| + P2b: the 2 PendSV units (0x1b60, 0x1b82), which need an `ldst_slow` axiom row (§4.4.4) | +0.145 M MODEL | same | +2.0 | +0.2 % |
| C2 alone (ablation) | 0.928 M | ≈ 11 ns MODEL (204 insns) | ≈ 10 | ≈ 1.1 % |
| C1 alone (ablation) | 0.928 M | ≈ 2.6 ns MODEL (49 insns) | ≈ 2.4 | ≈ 0.26 % (below noise) |

How the numbers are built:
- κ = `c_lookup` / (lookup jump-cache-hit path) = 7.60 ns / 141 insns ≈ 0.054 ns/insn (MODEL).
- `c_lookup` was MEASURED on the qint binary (`R/WP16-probe.md:44`). The 141-instruction path was MEASURED on the gate binary (7/8 gdb samples).
- The model assumes that helper code retires at the lookup path's rate.

What this means:
- **The noise floor.** A/A in one B5 comparison is ±0.22 % (`R/B5-evaluation.md:226`). The minimum detectable effect at n = 90 pairs is ≈ 1 % (`D/B3-B4-design.md:937`). The R11 "faster" flag needs a median ≥ 1 % with the CI above 0 (`R/B5-evaluation.md:85`).
- So the v1 result is expected **at or just above the detection limit, and it may be null.**
- **About 81 % of the value is C2**, the rebuild elision (DERIVED: 204/253 insns, or 10/12.5 ms).
- This is why the first milestone (P0, §7) is a hand-patched probe copy timed at n = 90 pairs, with a written go/no-go, **before any pass code is written**.
- If P0 shows nothing, the student still ships:
  - the F13 soundness fix;
  - the certified write-set and identity rows;
  - a MEASURED per-pair cost;
  - and a clean negative result (§7.3).

### 1.3 What comes later, and what is not built

**Later (stretch, each behind its own gate, §4.8):**
- **C3 `hs-msr-exit-chain`** replaces the main-loop round trip after the MSR with a B3-style guarded transfer.
  - That round trip is the larger cost: MSR TBs cause 82.8 % of all `exit_tb(NULL,0)` exits (MODEL).
  - ESTIMATE 1.5-4.3 %.
  - It needs a signed review against the spec, because the project specification (line 64; not in the repository) says "QEMU requires returning to the execution loop after state changes that can unmask interrupts".
- **C6 `inline-lookup-probe`** handles the about 4-5 M lookups per run that B3 leaves unguarded (DERIVED). This is survey item B3-7; it needs the agreement of the B3-7 owner.

**Kept only as a documented mechanism:**
- The **Shape A call-to-stub** form (`nop; call rel32 S`; slow path `jmp [pool]`), taken from design A. It is the only legal stub form for a helper that can unwind (§4.7).
- It is the fallback for sites where a key fact is missing (for example MSR PSP in thread mode).

**Not built (§8):**
- `udiv`/`sdiv` templates or value guards;
- `shl_cc` and `v7m_mrs` speed work;
- Shape B splicing;
- design B's binary-lifting import pipeline;
- the softmmu victim-TLB template.

---

## 2. Background the student needs

### 2.1 What a `.sc` unit is

A supercache unit is one TCG translation block (TB), frozen as x86-64 host code and reloaded by QEMU at startup.

**What the unit carries:**
- the host code;
- the TB key `(pc, flags, cs_base, cflags)`;
- the **search rows**: one row per guest instruction, holding its `insn_start` words and its host end offset;
- relocation and site metadata (the derived `.scm`);
- a constant pool at the end of the code.

scopt imports units (`S/importer/`), analyses them (`S/analysis/`: HostOps, `hcfg`, `hlive`, `hconst`, effects), proposes edits through passes (`S/passes/`), links and packages the artifact (`S/link.py`), and validates it (`S/validate.py` V-rows, `S/validate_sym/` = **hostsym**). Read `D/B1-design.md` §1 and `D/B3-B4-design.md` §1-4 first.

**Registers and stack inside a TB (`H/mechanics.md` §1.2):**
- `rbp` holds `env` (`CPUARMState*`) for the whole TB (`tcg/i386/tcg-target.h:87`; the prologue moves `rdi` to `rbp` at `tcg-target.c.inc:4787`).
- `rsp ≡ 0 (mod 16)` inside a TB. DERIVED: `FRAME_SIZE` = 1216, `stack_addend` = 1160 (`tcg-target.c.inc:4735-4755`).
- `rbx` and `r12-r15` may hold cached guest registers.
- `[rsp+128, rsp+1152)` holds TCG spill slots.

### 2.2 A real helper call site (MEASURED, offline capstone disassembly of the compute snapshot)

The hottest MSR TB is unit `tb:28a2376d…`:
- guest `vPortEnterCritical`, vaddr 0x1b24;
- 1,283,113 steady-phase executions (entries − aborted) over 3 train runs, from the profile (`H/compute-sites.json`; with the boot phase 1,283,403; the census log's raw `entries=` is 1,283,452). The differences are ≤ 0.03 % and do not affect the ±1 % P0 acceptance;
- key `flags 0x110`, `cs_base 0x800400`, `cflags 0xff022000`. Note that `cs_base` is **not** what env holds: env `hflags.flags2` is 0x400 at entry, because THUMB (bit 23) is added by the lookup (§3.2, §4.2 item 2);
- two guest instructions, whose host starts are 0x16 and 0x21.

```
 0: mov  ebx,[rbp-8]          ; icount entry check: budget -= 2 guest insns
 3: add  ebx,-2
 6: test ebx,ebx
 8: jl   0x4b                 ;   -> exit_tb(tb|EXIT_REQUESTED)
 e: mov  [rbp-8],bx
12: mov  byte [rbp-4],0       ; can_do_io = 0               <- guest insn 0 (host row starts 0x16)
16: mov  dword [rbp+0xc],0x50 ; r3 = 0x50  (mov r3,#0x50)
1d: mov  byte [rbp-4],1       ; can_do_io = 1 before the last insn
21: mov  edx,0x50             ; arg2 val                     <- guest insn 1 = MSR BASEPRI,r3 (row starts 0x21)
26: mov  esi,0x811            ; arg1 maskreg (TCG constant: (mask<<10)|SYSm, SYSm 17 = BASEPRI)
2b: mov  rdi,rbp              ; arg0 env
2e: call qword [rip+0x2c]     ; pool slot -> helper_v7m_msr        (6 bytes: ff 15 disp32)
34: mov  rdi,rbp
37: call qword [rip+0x1b]     ; pool slot -> helper_rebuild_hflags_m32_newel
3d: mov  ebx,[rbp+0x3c]       ; PC += 8   (gen_lookup_tb)
40: add  ebx,8
43: mov  [rbp+0x3c],ebx
46: jmp  tcg_code_gen_epilogue   ; exit_tb(NULL,0): back to the main loop
4b: lea  rax,[rip-0x10f] ; jmp epilogue
58: <pool: 8-byte absolute helper addresses>
```

This TB comes from `trans_MSR_v7m` (`Q/target/arm/tcg/translate.c:4923-4937`):
1. `v7m_msr(env, tcg_constant (mask<<10)|sysm, Rn)`;
2. `gen_rebuild_hflags(s, true)`;
3. `gen_lookup_tb` (DISAS_EXIT → `exit_tb(NULL,0)`, `translate.c:1353-1357`).

So **the MSR is always the last guest instruction of its TB**.

**Facts to take from this example:**
- **Every QEMU helper call in the snapshot is the 6-byte pool-indirect form `ff 15 disp32`** (MEASURED, `H/census.md` §0): all 716 helper calls, and all 5688 LDST slow-path calls, so 6404 of the 6424 CALL ops. The other 20 CALL ops are FastDyn plugin callbacks, which are `call rel32` into `libfastdyn.so` (§3.1 last row). The code arena (`0x7fff6c…`) is more than 2 GiB from the PIE text (`0x5555…`), so `tcg_out_branch` cannot use `call rel32` (`tcg/i386/tcg-target.c.inc:1967-1992`).
- **The pool slot holds the absolute helper address.** The design relies on ASLR being off; the loader enforces this (`qemu_supercache_check_aslr`; `N/map-supercache-format.md:21,203`). For all 11 named helpers, the slot equals `nm` address + PIE base `0x555555554000` (MEASURED).
- **Argument setup** follows SysV: `rdi = env` (`mov rdi,rbp` at every site, MEASURED), then `rsi`, `rdx`, `rcx`, `r8`, `r9`. The result is in `rax`. Every call clobbers `rax rcx rdx rsi rdi r8-r11`, the XMM registers and EFLAGS (`tcg/tcg.c:6185-6190`; `tcg-target.c.inc:4838-4851`). **At a call site, all of these are dead except the argument registers.** After the call, only `rax` is live among them.
- **What TCG emits around a call depends on its flags** (`include/tcg/tcg.h:229-245`; `tcg/tcg.c:6192-6203`):

  | flags | what TCG emits around the call | examples |
  |---|---|---|
  | `NO_RWG` (0x1) | nothing: guest registers stay cached in `rbx`/`r12-r15` across the call | `udiv`, `rebuild_hflags_m32_newel` |
  | `NO_WG` (0x2) | dirty globals are stored before the call and stay valid in registers | |
  | `0` | everything is stored before the call and reloaded after it | `v7m_msr` |

  **NO_RWG means only "does not read or write TCG globals"** (guest `r0-r15`, NZCV, …). It says nothing about other env fields; this is the F13 issue (§4.6).

A second example: the hot `udiv` site, unit `tb:88612dff…`, `prvProcTask`, 352 k entries per run (MODEL):
```
4f: mov r13d,[rbp+0x1c]   ; r7
53: add r12d,r13d         ; num lives in r12 (callee-saved) across the call
56: mov edx,[rbp]         ; den = r0
59: mov esi,r12d
5c: mov rdi,rbp
5f: call [rip+0xf3]       ; helper_udiv, NO_RWG: no env sync before it; ebx/r12 live across
65: mov r13d,eax ; shl r13d,8 ; sub r13d,eax ; sub r12d,r13d   ; x - q*255
```
The helper body is `endbr64; test edx,edx; je slow; mov ecx,edx; mov eax,esi; xor edx,edx; div ecx; ret` (MEASURED). Nothing there is worth removing except the call itself; see §8.

### 2.3 How faults unwind: GETPC and search rows

A helper finds "which guest instruction am I in" from its own return address.

1. `GETPC()` is `__builtin_return_address(0)` (`include/accel/tcg/getpc.h:16-17`).
2. `cpu_restore_state(cs, host_pc)` (`accel/tcg/translate-all.c:200-219`) finds the TB with `tcg_tb_lookup`. For the supercache, that is a binary search on `tc_offset` (`tcg/supercache.c:2047-2077`).
3. `cpu_unwind_data_from_tb` (`translate-all.c:137-169`) subtracts `GETPC_ADJ` = 2 (`accel/tcg/tb-internal.h:23`) and walks the search rows. It stops at the first row whose host end is greater than `host_pc−2`, and returns `insns_left`.
4. `cpu_restore_state_from_tb` (`:175-198`) then:
   - restores PC, condexec and syndrome (`target/arm/cpu.c:101-124`);
   - under `CF_USE_ICOUNT`, gives back the icount charge for the instructions that did not run.
5. **A return address in the wrong place has two different outcomes** (DERIVED):
   - **TB found, row walk fails.** If `host_pc` is inside `[tc_offset, tc_offset + tc_size)` of the unit (for example in a stub after the pool) but `host_pc − 2` is past the last row, `cpu_unwind_data_from_tb` returns −1 and `cpu_restore_state_from_tb` **returns silently** (`translate-all.c:180-182`). The restore and the icount fix-up are skipped. A raised exception is taken with a stale PC, and icount drifts. `cpu_io_recompile` (`translate-all.c:576-620`) does **not** abort in this case: it continues and re-executes from the stale PC with `CF_NOIRQ`. **This failure is silent, which is worse than an abort.**
   - **TB not found.** Only if `host_pc` is outside the unit's `[tc_offset, tc_offset + tc_size)` does the supercache lookup return NULL (`Q/tcg/supercache.c:2071-2074`). `cpu_restore_state` then does nothing, and `cpu_io_recompile` calls `cpu_abort` (`translate-all.c:582-586`).
   - U1, U7 and U9 (§4.7) exist to rule out both cases.

Consequences:
- **Rule.** Any call to code that can reach `GETPC` must keep its return address inside the row of its guest instruction. This covers `raise_exception_ra` (`target/arm/tcg/op_helper.c:73-85`), reached from the `udiv`/`sdiv` DIV_0_TRP trap (`op_helper.c:134-144`), `tlb_fill`, and `cpu_io_recompile`. TCG's own softmmu slow paths avoid the problem by passing an explicit `retaddr` (`tcg-target.c.inc:2118-2158`).
- **Leaving a TB without returning.** `cpu_loop_exit` longjmps to `cpu_exec_setjmp` (`accel/tcg/cpu-exec-common.c:67-74`). Every guest value must already be in env at that point.
- **icount.** The TB entry charges all `n` instructions up front (`accel/tcg/translator.c:43-104`). The translator stores `can_do_io = 0`, then `1` before the last instruction (`translator.c:208-220`).
- **Interrupts.** `cpu_interrupt` sets `interrupt_request` and `icount_decr.high = -1`. That `high` value makes the next TB's entry check exit to the main loop. The main loop's `cpu_handle_interrupt` then clears `high` and, if `interrupt_request` is set, takes the BQL and asks the NVIC (`accel/tcg/cpu-exec.c:788-892`).

**v1 does not need any of this machinery, because it removes only calls that provably cannot unwind.** The rule matters as soon as anyone adds a stub (§4.7).

### 2.4 What the optimizer already guarantees, and how B3 guards work

**B3/B4 invariants** (`D/B3-B4-design.md:57-65`), which this family inherits:

| # | invariant |
|---|---|
| 1 | General TBs stay general |
| 2 | Every direct transfer that replaces a lookup is guarded by the full key and the breakpoint list |
| 3 | **No edit changes TB shape.** Same-length inside instructions; growth only after the pool; rows, icount, cflags and entry check byte-identical |
| 4 | No compensation code |
| 5 | **Guest effects are compared, never inferred.** Every helper call is a compared event in hostsym, and profiles never certify a fact |
| 6 | Everything ships on one binary |
| 7 | Fail closed offline, fail loud online |

This design relaxes invariant 5 in exactly one way, **invariant 5′** (§4.5): a helper-call event may be replaced by a certified, exe-bound axiom whose preconditions are entailed by key facts and CPU-model facts. A reviewer must accept 5′ explicitly (gate G-HS, §5.6).

**B3 guarded slots** (`D/B3-B4-design.md:260-322`). A lookup tail `mov rdi,rbp; call [lookup]; jmp rax` becomes `jmp S`. The stub `S` lives after the pool:

```
S:  cmp qword [rbp-0x2980],0 ; jne F     ; g_bp: no breakpoints
    cmp byte  [rbp+0x14d],1  ; jne F     ; g_thumb
    cmp dword [rbp+0x228],0  ; jne F     ; g_cond
    cmp dword [rbp+0x150],Kf ; jne F     ; g_flags  (hflags.flags of target key)
    cmp qword [rbp+0x158],Kf2; jne F     ; g_flags2
    cmp dword [rbp+0x3c],Vc  ; je  L1    ; g_pc
    jmp F
L1: jmp rel32 <slot>                      ; GENERAL slot, linked by the main loop
F:  mov rdi,rbp ; call [rip+d'] ; jmp rax ; the original tail: baseline behaviour
```

- The guard reads env and writes only EFLAGS.
- On failure, `F` is byte-equal to the original tail.
- `hostsym` proves the pair with `check_guarded_chain` (`S/validate_sym/equiv.py:443`).
- C3 (§4.8) reuses this exact machinery.

**The SFB precedent.**
- `icount-entry-sfb` (`S/passes/icount_entry_sfb.py`) is the model for a pass family that lives outside B3/B4. It has:
  - its own `ANNOTATE` edits and packager (`package_sfb`, `:870-946`);
  - its own validation rows (`SFB1-SFB9`, `:612-756`);
  - a native-execution harness (`_Native`, `:248-316`);
  - its own CLI group (`S/commands/b4.py:137-160`) and pipeline (`T/pipelines/tcg-entry-sfb.toml`);
  - its own B5 arm.
- v1 of this family copies that structure.

---

## 3. Hot-helper census (compute workload, gate binary)

Method (`H/census.md`): every `OpKind.CALL` in HostOps of all 2222 units was enumerated, giving 6424 sites. Helper names and flags come from the unit's `HelperBinding`. "Calls/run" is MODEL: steady-phase unit executions × sites, ÷ 3 runs. That is exact for must-execute sites and an upper bound for may-skip sites. "Meas." is MEASURED hits in one untimed gdb run on train-1. Cost per hit for these gdb counts is ≈ 65 µs per trap (MEASURED).

### 3.1 Table

| callee | flags | sites (units) | calls/run MODEL | Meas. train-1 | helper path, insns (MEASURED `stepi`) | can unwind / longjmp | v1 decision |
|---|---|---|---|---|---|---|---|
| `lookup_tb_ptr` | 0x6 | 575 (575) | 15.17 M | not counted; WP16 14.25 M hits/run | 141 (jump-cache hit, 7/8) | yes (breakpoint `cpu_loop_exit`) | out of scope: B3 owns it. Stretch C6 targets the residual |
| softmmu slow paths `ld*/st*_mmu` | n/a (out-of-line LDST) | 5688 | ≤ 90.7 M summed over all slow-path helpers (DERIVED from `H/census.md` §2 rows 2-4, 11, 12; bound: every access misses). `ldul` alone ≤ 39.3 M | **≈ 2.22 M** (ldul 1,649,473; stl 477,328; ldub 96,910) | ldul 262-304, stl 301-308 (14/14) | yes (explicit `retaddr`; MMIO) | not a helper-inlining target (§3.3) |
| `v7m_msr` | 0 | 25 (25) | 1.080 M | **1,076,941** | **49** (6/6, BASEPRI) | only the `SP_NS` path (`M_SECURITY`, not on CM3; `m_helper.c:2655-2669`) | **C1 fold** |
| `rebuild_hflags_m32_newel` | 0x1 | 23 (23) | 1.080 M | = msr (paired) | **204** (6/6) | no | **C2 elide** |
| `udiv` | 0x1 | 55 (38) | 1.526 M | 1,488,620 | 8 (3/3) | yes if `den==0 && DIV_0_TRP` (`op_helper.c:134-144,166-173`) | not built (§8) |
| `exception_internal` | 0x8 | 7 (7) | ≤ 153.5 k | 75,107 | n/a | always (longjmp) | never |
| `v7m_mrs` | 0 | 2 (2) | 72.5 k | | 31 (3/3) | no | optional hostsym test vehicle (C4) |
| `shl_cc` | 0 | 26 (26) | ≤ 71.1 k | | 16 (3/3) | no | not built |
| `rebuild_hflags_m32`, `exception_with_syndrome` | 0x1, 0x8 | 2, 1 | ≈ 0 (boot) | | | | never |
| FastDyn plugin callbacks (`call rel32` into `libfastdyn.so`) | 0 | 20 (6) | 20 | | | n/a | never (instrumentation contract) |

### 3.2 The hot MSR units (MEASURED keys and LDST counts; executions from the profile)

The "steady execs" column is the profile's steady-phase executions of the unit, i.e. entries − aborted, summed over the 3 train runs (`H/compute-sites.json`). "Per run" is that ÷ 3 (MODEL calls/run, as in §3.1). Boot-phase executions (38-290 per unit) are not included.

| unit | vaddr (function) | write | value arg (host) | key flags / cs_base | handler | LDST blocks before the MSR | steady execs (3 runs) | per run |
|---|---|---|---|---|---|---|---|---|
| `28a2376d` | 0x1b24 (`vPortEnterCritical`) | BASEPRI | `mov edx,0x50` | 0x110 / 0x800400 | 0 | 0 | 1,283,113 | 427.7 k |
| `8fc4f4f2` | 0x1b4e (`vPortExitCritical`) | BASEPRI | `mov edx,[rbp+0xc]` (r3) | 0x110 / 0x800400 | 0 | 0 | 1,283,118 | 427.7 k |
| `2dc6a851` | 0x1b7a (`xPortPendSVHandler`) | BASEPRI | `xor edx,edx` | 0x110 / 0x800401 | 1 | 0 | 217,429 | 72.5 k |
| `4acba635` | 0x1b60 (`xPortPendSVHandler`) | BASEPRI | `mov edx,0x50` | 0x110 / 0x800401 | 1 | **13** | 217,423 | 72.5 k |
| `24192331` | 0x1b82 (`xPortPendSVHandler`) | PSP (`0x809`) | `mov edx,ebx` | 0x110 / 0x800401 | 1 | **12** | 217,421 | 72.5 k |

**Two facts from this table:**
- **The argument window is uniform** (MEASURED in all five units). Some value move into `edx` comes first: an immediate, a register, `xor`, or an env load. It is followed by the 14-byte tail `be imm32 | 48 89 ef | ff 15 disp32`. The rebuild call `48 89 ef | ff 15 disp32` (9 B) follows immediately after.
- **`flags 0x110` gives MMU index 1 (`MPriv`).** The MMUIDX field is flags bits 4-7 (`Q/target/arm/cpu.h:2960`). The `HANDLER` bit is flags2 bit 0 (`cpu.h:3006`). So all five units run privileged. Each unit's handler state is fixed by its key.
- **`cs_base = hflags.flags2 | uncached bits`, not `hflags.flags2`.** `arm_get_tb_cpu_state` starts from `env->hflags` (`Q/target/arm/tcg/hflags.c:556-557`) and ORs into a local copy bits that are never stored in env: THUMB (bit 23) and CONDEXEC (bits 24-31), and for M-profile LSPACT, NEW_FP_CTXT_NEEDED, FPCCR_S_WRONG and MVE_NO_PRED (cs_base bits 2-5) (`hflags.c:567-611`; "Not cached" at `cpu.h:2958, 2972-2973, 3010-3016`). In `flags` the same holds for PSTATE__SS (bit 2, `hflags.c:623-625`). So for the hot units, key `cs_base` 0x800400 = THUMB | NS while env `hflags.flags2` = 0x400 at entry (DERIVED). scopt already has the mask: `NOTCACHED2` and `PSTATE_SS` in `S/guard.py:57-63`, applied by `guard_key` (`S/guard.py:165-187`).

**Runtime state at the calls** (MEASURED, diag-H: 4,000 sampled `v7m_msr` entries on train-1):
- `secure = 0` in all samples;
- `features = 0x140102c2e8` in all samples, i.e. `M` = bit 9 set, `M_MAIN` = bit 36 set, `M_SECURITY` = bit 35 clear;
- privileged in all samples;
- `interrupt_request ≠ 0` at 131 of 3,729 BASEPRI writes (3.5 %), which matters for C3;
- **40,000 consecutive rebuild calls changed `hflags` 0 times.**

### 3.3 Why the softmmu slow paths are not a target here

They have the largest MEASURED helper-call volume, ≈ 2.2 M per run. Each call costs 262-308 instructions, which suggests victim-TLB hits (INFERRED from path length).

The cause is direct-mapped TLB conflicts:
- A flash page and an SRAM page share an index `(addr>>10) & mask` (`accel/tcg/cputlb.c:130-136`).
- The two pages ping-pong through the 8-entry victim TLB (`cputlb.c:1312-1339`; `include/hw/core/cpu.h:207`).
- The hot source is the literal-pool load `ldr r2,[pc,#imm]` of `&uxCriticalNesting` from ROM in `vPortEnter/ExitCritical`.

A template would have to reproduce `victim_tlb_hit`: an 8-entry loop, the `tlb.c.lock` spinlock, and the entry swap with DWARF-derived layouts. Avoiding the conflict (a ROM-literal fold, or conflict-aware placement) is worth more, and it is a different project with its own contract amendment (§8, §9).

---

## 4. The design

### 4.1 Pass family overview

| order | pass | module | edit route (v1) | scope | phase |
|---|---|---|---|---|---|
| 1 | `hs-msr-fold@1` (C1) | `S/passes/hs_fold.py` | `ANNOTATE` key `x_hs_fold`, materialized by `package_hs` | unit | **v1** |
| 2 | `hs-hflags-elide@1` (C2) | same module | `ANNOTATE` key `x_hs_fold` (kind `elide`) | unit | **v1** |
| 3 | `hs-mrs-fold@1` (C4) | same module | same | unit | optional (hostsym test of a result-returning axiom) |
| 4 | `hs-msr-exit-chain@1` (C3) | `S/passes/hs_exit_chain.py` | `REDIRECT_EXIT` GuardedSlot, `exit_class="msr_exit"` | snapshot | stretch S1, gate G-C3 |
| 5 | `inline-lookup-probe@1` (C6) | `S/passes/lookup_probe.py` | own route, agreed with the B3-7 owner | snapshot | stretch S2 |

**Pipeline rules.**
- `hs-*` passes go only in `T/pipelines/hs-fold.toml` (later also `hs-exitchain.toml` and `hs-probe.toml`). **Never** add them to `b3.toml` or `b4.toml`.
- **v1 needs no new frozen `EditKind`.** It uses the SFB route: ANNOTATE plus its own packager, PATCH_TEMPLATES entries under the pass name, and its own validation rows. A typed `EditKind.FOLD_CALL` is a v2 option, and it needs a design §3.7 edit with owner sign-off (`S/passes/base.py:147-152`).
- **Composition with B3/B4 is by re-import, the "B4 on sfb" precedent** (`S/report/b5.py:522`): package the `hs` artifact, re-import it as a new base, then run B3/B4 on it. This has no conflicts, because the MSR units end in `exit_tb(0)`, not in a lookup tail.

### 4.2 Fact sources and trust model

The design relies on four kinds of fact. The strongest comes first.

**1. AX-H, the hflags-fresh axiom.** This is the single trusted axiom shared by C1, C2 and any later template.
- **Statement.** At every TB lookup, `env->hflags == rebuild(env)`.
- **QEMU itself asserts it**, but only under `CONFIG_DEBUG_TCG` (`Q/target/arm/tcg/hflags.c:505-519`, called from `arm_get_tb_cpu_state` at `:556`). That option is off in the gate build.
- **Domain.** AX-H is claimed **only for runs that start by reset and never load VM state**: no `-loadvm`, no `-incoming`, no monitor `loadvm`/`migrate-incoming`. The reason is a concrete counterexample (DERIVED, [I]; see "Snapshot load" below). The refusal is enforced by launch discipline (§4.10, row HS-RUN in §5.1), because neither the loader nor the RuntimeIdentity sees the QEMU command line.
- **Writer audit.** The claim is: **every writer of a rebuild input is followed by a rebuild before the next TB lookup.** Many writers do not rebuild themselves; they rely on a later rebuild on the same path. The student re-does this table and records each `file:line`:

  | writer (inputs written) | rebuild that follows it, before the next lookup |
  |---|---|
  | `helper_v7m_msr` (`m_helper.c:2556`): `faultmask` (`:2736`), `control` through `write_v7m_control_spsel` (`:2751`) and the nPRIV update | the translated `gen_rebuild_hflags` right after the call in the same TB (`Q/target/arm/tcg/translate.c:4934`) |
  | exception entry `v7m_exception_taken` (`m_helper.c:833`): NVIC acknowledge sets `vectors[].active` (`:966` → `Q/hw/intc/armv7m_nvic.c:793`), `write_v7m_control_spsel` (`:970`), `v7m.exception` (`:504`) | `m_helper.c:980` at the end of `v7m_exception_taken` |
  | exception return `do_v7m_exception_exit` (`m_helper.c:1360`): `faultmask` (`:1450, 1453`), NVIC complete clears `active` (`:1457` → `armv7m_nvic.c:896`), `write_v7m_control_spsel_for_secstate` (`:1528`), `v7m.exception` | `m_helper.c:1904` on success; every failure path ends in `v7m_exception_taken`, so `:980` ([I]: the student checks every early return) |
  | `v7m_bxns` / `blxns`, function return, NSC/SG emulation | `m_helper.c:590, 652, 1977, 2177` |
  | NVIC/SCS MMIO writes `nvic_sysreg_write` (`armv7m_nvic.c:2340`): `v7m.ccr` (`:1694, 1709`), SHCSR `active` bits (`:1717-1776`), … | `armv7m_nvic.c:2462` at `exit_ok`, reached by every successful write |
  | NVIC reset `armv7m_nvic_reset` (`armv7m_nvic.c:2583`) | `armv7m_nvic.c:2654` (this is in the reset function, not a register write) |
  | CPU reset (`Q/target/arm/cpu.c:404-405, 427-428`, …) | `cpu.c:578` |
  | vmstate load of `v7m.secure` (`Q/target/arm/machine.c:700`, the `cpu/m-security` subsection) and of every other CPU field | `cpu_post_load` → `machine.c:1021`. **Not sufficient**, see "Snapshot load" |
  | translated code writing an input | `gen_rebuild_hflags` after every hflags-changing instruction (e.g. `translate.c:4934`), before the TB ends |

  - gdb writes to M-profile system registers are a no-op (`Q/target/arm/gdbstub.c:413-416`).
  - An xPSR write does not touch the cached hflags fields (`D/B3-B4-design.md` G33).
- **Snapshot load breaks AX-H** (DERIVED, [I]). The CPU is realized before the NVIC (`Q/hw/arm/armv7m.c:365` vs `:370`), and neither vmstate sets a priority (no `MIG_PRI` in `armv7m_nvic.c` or `machine.c`). So on `loadvm` or incoming migration, `cpu_post_load`'s `arm_rebuild_hflags` (`machine.c:1021`) runs while the NVIC still holds its pre-load state. `nvic_post_load` (`armv7m_nvic.c:2473-2498`) recomputes NVIC state but does not rebuild hflags. If the snapshot was taken with HardFault or NMI active, or with a different active state than the running VM, the NEGPRI bit of the MMU index (`armv7m_nvic.c:394-419`) is stale until the next rebuild. In the baseline, the next MSR's rebuild heals it; under C2 that rebuild is gone, so later TBs keep running with the wrong `mmu_idx` until the next exception entry or return. Hence the domain restriction above. A later student may lift it by adding a fixture that does `savevm` inside a HardFault handler and then `loadvm`, with O-HF and O1/O2 equal to warm.
- **Evidence:** (a) and (b) are required, (c) is optional:
  - (a) the audit above, re-done by the student and recorded with each `file:line`;
  - (b) the full-run oracle O-HF (§5.5): 0 changed `hflags` over every rebuild call of full runs on all train, held-out and final inputs and on the fault fixtures (§5.5). MEASURED so far: 0/40,000 (diag-H) and 6/6 (diag-D);
  - (c) optional, **only if the supervisor permits building QEMU outside the frozen trees**: a `--enable-debug-tcg` scratch build of the same tag, running the fixtures and the compute workload cold (no `.sc`). The assert then checks AX-H at every lookup.
- **Within a TB.** AX-H holds at TB entry. It keeps holding up to a site if no rebuild input is written before the site (premise P-IN, §4.4).
- **Entry paths.** Every way of entering a TB preserves "env->hflags == key":
  - lookup by key;
  - stock chaining, because TCG ends a TB after every hflags-changing instruction;
  - B3 guarded slots, which check `g_flags`/`g_flags2`.

**2. Key facts** (per unit, decoded from the **normalized** key):
- **Normalization first.** The key is not what env holds (§3.2). Define
  - `K.flags_env = K.flags & ~PSTATE_SS` (bit 2), and
  - `K.flags2_env = K.cs_base & ~NOTCACHED2` (THUMB, CONDEXEC and cs_base bits 2-5),

  reusing `NOTCACHED2` and `PSTATE_SS` from `S/guard.py:57-63` (import them; do not copy the constants). Like `guard_key` (`S/guard.py:165-187`), **refuse** (`key_not_normal`) a key that has `SS_ACTIVE`, `PSTATE__SS` or any of the cs_base bits 2-5 set: they never occur on cortex-m3, and masking them would hide a mismatch. `K.flags_env` and `K.flags2_env` are the only key values that may be used as env facts or as hostsym preconditions (§5.2).
- `priv = (K.flags_env >> 4) & 1`, the MMUIDX `M_PRIV` bit;
- `negpri = (K.flags_env >> 5) & 1`;
- `ss_active = (K.flags_env >> 1) & 1` (`cpu.h:2957`);
- `handler = K.flags2_env & 1`.

The `M_PRIV` bit is computed by the same expression as `arm_current_el` for M-profile: `handler || !(control[secure] & 1)`. Compare `m_helper.c:195-197` with `Q/target/arm/internals.h:459-463`. By AX-H, `(K.flags_env, K.flags2_env)` equals what `rebuild(env)` gives at TB entry.

**3. CPU-model facts** (per artifact). They are bound **offline** by validation row **HS-CPU** against the artifact's bundle `cpu_model` (`S/formats/bundle_v1.py:600-617`, `ADMITTED_CPU_MODELS = ("cortex-m3",)`), as V20 does for B3 (`S/validate.py:1136`). **At run time nothing checks the CPU model:** the `.sc` loader binds only the exe identity, page size, `TARGET_PAGE_BITS` and `sizeof(TranslationBlock)` (`N/map-supercache-format.md` C17, C18), not `-cpu`. The run-time binding comes from campaign launch discipline (the campaign's fixed machine and CPU options), exactly as for the baseline and for B3. The facts are:
- `ARM_FEATURE_M` (bit 9), which `arm_current_el` depends on (`internals.h:459`);
- `M_MAIN` (bit 36);
- `¬M_SECURITY` (bit 35);
- `v7m.secure == 0`.

Feature bit indices are DERIVED from `enum arm_features` (`cpu.h:2397-2434`) and match the MEASURED feature word.

**Why `secure == 0` holds without `M_SECURITY`** (DERIVED; the student closes the last [I] in P1):
- Reset leaves `secure` at 0 unless `M_SECURITY` is set (`Q/target/arm/cpu.c:404-405`).
- The only other writer is `switch_v7m_security_state` (`m_helper.c:516-539`). Its six callers:

  | caller | gating |
  |---|---|
  | `:587` (`bxns`) | asserts `env->v7m.secure` (`:582`) |
  | `:649` (`blxns`) | asserts it as well (`:606`) |
  | `:969` (exception entry) | `targets_secure` from the NVIC is false unless `M_SECURITY` (`exc_targets_secure`, `armv7m_nvic.c:174-176`) or the pending exception is in the secure bank |
  | `:1617` (exception return) | `return_to_secure` is gated on `M_SECURITY` (`:1480`) |
  | `:1968` (`do_v7m_function_return`) | reached only under `assert(M_SECURITY)` (`:2371`) |
  | `:2174` (NSC) | reached only for `M_FAKE_FSR_NSC_EXEC` faults (`m_helper.c:2263`) |

- **[I] left open:**
  - (i) the secure-bank `sec_vectors[].pending` is never set without `M_SECURITY`;
  - (ii) `M_FAKE_FSR_NSC_EXEC` (`internals.h:809`) cannot arise without an SAU.
- **If either cannot be shown,** C1 must not use the unguarded fold. It falls back to the guarded Shape A stub (§4.7), whose fast path tests `secure == 0` at run time.
- **C2 does not depend on this question.** For `rsi = 0x811`, every path of `helper_v7m_msr` writes at most `basepri[secure]` (`m_helper.c:2566-2576, 2716-2721`; the `M_SECURITY` switch has no case for reg 0x11, and `bad_reg` at `:2776-2779` only logs). So its may-write set is `basepri[0..1]` = env `[0xc04, 0xc0c)` whatever `secure`, the key or the CPU features are (row `ws.msr.basepri@1`, §4.3). Neither entry is an hflags input.

**4. Axiom rows** (per helper, per exe sha), §4.3.

**Profiles** select and rank candidates only. They never certify a fact (invariant 5).

### 4.3 The helper-axiom table

**Location.** `S/formats/helperaxioms/<exe_sha256>.json`, loaded by `S/formats/helperaxioms_v1.py`. It is bound like an EnvLayout (`S/formats/envlayout_v1.py:27-30`).

**Row schema** (example):
```json
{"axiom_id": "msr.basepri.priv@1",
 "helper": "helper_v7m_msr", "addr": "0x877d20", "size": 1663,
 "exe_sha256": "56d8bfb3…", "bytes_sha256": "<sha256 of [addr, addr+size)>", "tcg_flags": 0,
 "match": {"rsi": {"const": 2065}},
 "requires": {"key": {"priv": 1},
              "cpu": {"M": true, "M_MAIN": true, "M_SECURITY": false, "secure0": true},
              "assume": ["AX-H"], "premise": ["P-IN"]},
 "effect": {"env_writes": [{"off": 3076, "size": 4, "term": "<hostsym term of movzx r32,r8 on arg_rdx>"}],
            "env_reads": [], "ret": null,
            "unwinds": false, "getpc": false, "calls": [], "mmio": false, "bql": false, "locks": false},
 "evidence": {"source": ["target/arm/tcg/m_helper.c:2566-2576", "2716-2721", "internals.h:459-463"],
              "dynamic_cert": "<path + sha of D3 certificate>",
              "box_cert": "<path + sha of exe-in-a-box certificate>",
              "review": "<sha256 of the signed review note>"}}
```

**v1 rows:**

| axiom_id | match | requires | effect | source |
|---|---|---|---|---|
| `msr.basepri.priv@1` | `rsi = 0x811` | key `priv=1`; CPU facts | `env32[0xc04] := zext32(rdx & 0xff)` | `m_helper.c:2566-2576`, `2716-2721` |
| `msr.basepri.unpriv@1` | `rsi = 0x811` | key `priv=0` | none (the write is ignored: `cur_el==0 && reg>7`) | `m_helper.c:2570-2575` |
| `msr.psp.handler@1` | `rsi = 0x809` | key `priv=1, handler=1`; CPU facts | `env32[0xbf0] := rdx & ~3` (`other_sp`: `v7m_using_psp` is false in handler mode, `internals.h:1217-1226`) | `m_helper.c:2694-2700` |
| `rebuild.identity@1` | `helper_rebuild_hflags_m32_newel` | AX-H (in its domain, §4.2), P-IN, P-W (§4.4.2) | none (writes back the existing `hflags`) | `hflags.c:460-467`; §4.4.2 |
| `ws.msr.basepri@1` (**C2 first ship**) | write-set row for `v7m_msr`, `rsi = 0x811` | none (holds on every key and CPU) | **may write** `env[0xc04, 0xc0c)` (`basepri[0..1]`); no unwind, no GETPC, no MMIO, no lock | `m_helper.c:2566-2576, 2716-2721, 2776-2779` |
| `ws.rebuild_hflags_m32*@1` | write-set override (F13) | — | **may write** `env[0x150, 0x160)` | `hflags.c:460-467` |
| `ws.udiv@1`, `ws.sdiv@1` | write-set override | — | returning path writes no env; raising path longjmps (S6 class, unchanged) | `op_helper.c:134-173` |
| `mrs.psp.handler@1` (C4, optional) | `rsi = 9` | key `priv=1, handler=1` | `rax := zext32(env32[0xbf0])` | `m_helper.c:2429-2451` |

**Refused in v1 (`axiom_unbound`):**
- `0x812` BASEPRI_MAX (conditional write, `m_helper.c:2722-2730`);
- `0x810` PRIMASK;
- `0x813` FAULTMASK;
- `0x814` CONTROL;
- the xPSR forms (`0x80x`);
- `0x809` PSP in thread mode (needs `control.SPSEL`, which is not in the key, so `axiom_needs_guard` → the Shape A fallback, later).

**Binding check (row HS1).** A mismatch skips the site (`axiom_unbound`) and never fails the artifact.
- `exe_sha256` equals the gate binary's sha.
- `bytes_sha256` is recomputed from the ELF (`S/decode/symbols.py` `ElfImage.symbol`).
- For `rebuild`, the sha of every function in its call closure also matches.
- The unit's HELPER binding `func` equals `PIE_BASE + addr`.

**Certification of every row.** A row is used only when legs 1 and 2 pass. Leg 3 is **behind the MVR line** (§7.1): it is built only after the first ship, and once built it is required for every row it covers.
1. **Source derivation.** A review note with `file:line` references, hashed into the row.
2. **D3 dynamic certificate** (untimed gdb diag, §5.5):
   - For ≥ 3000 sampled calls per row, hash the env range before and after the call: all of `CPUARMState` plus the `neg` region.
   - Require `after == effect(before)` (for a `ws.*` row: every changed byte is inside the may-write set), and no other byte changed.
   - Check that every sample satisfied the row's `requires`.
   - **Freeze the other threads.** Between the entry breakpoint and `finish`, the main-loop thread may run NVIC/SysTick code and `arm_cpu_set_irq`, which writes `env->irq_line_state` (`Q/target/arm/cpu.c:1073-1075`) and `interrupt_request`/`icount_decr.high`. Run D3 with gdb `set scheduler-locking on` across `finish`. This is safe because the certified helpers take no BQL and no lock (HS-I2), so the frozen threads cannot block them. Fallback if a gdb build lacks it: exclude the named async fields `irq_line_state` (env 0xcdc), `cpu.interrupt_request` (−0x2af0) and `neg.icount_decr.u16.high` (−6) from the comparison, and record that in the certificate.
   - **Sample source per row** (each must reach 3000):

     | row | sample source |
     |---|---|
     | `ws.msr.basepri@1`, `msr.basepri.priv@1`, `rebuild.identity@1`, `ws.rebuild_hflags_m32*@1` | compute train-1 (≈ 1.0 M calls) |
     | `msr.psp.handler@1`, `mrs.psp.handler@1` | compute train-1 (PendSV, ≈ 72 k calls) plus F-HS5 |
     | `msr.basepri.unpriv@1` | **a fixture loop only**: F-HS3 runs its unprivileged MSR ≥ 3000 times. FreeRTOS tasks on compute run privileged, so compute has no sample. If F-HS3 is not ready, drop this row from the release (unprivileged BASEPRI sites are then refused with `axiom_unbound`) |
     | `ws.udiv@1`, `ws.sdiv@1` | compute train-1 for `udiv`; a fixture loop for `sdiv` if compute has fewer than 3000 calls |
3. **Exe-in-a-box** (from design B; behind the MVR line):
   - In a forked child, `mmap` the gate binary's `PT_LOAD` segments at a free base. Then call the real `helper_v7m_msr` on synthetic env buffers.
   - The buffers span env offsets −0x2b00 to +0x16000, with randomized `features` (measured word, ±M, ±M_MAIN, ±M_SECURITY), `secure`, `exception`, `control`, and edge values of `val` (0, 0x50, 0xff, 0x1ff, 0xffffffff, random).
   - Compare the env diff with the row's effect. Inputs outside `requires` must either differ or fault in the PLT (for example `bad_reg` → `qemu_log`). Those inputs prove that `requires` is necessary, not that the row is wrong.
   - Use the SFB `_Native` pattern (`S/passes/icount_entry_sfb.py:248-316`).
   - For `rebuild.identity@1`, the box is optional: it needs a fake NVIC object for `armv7m_nvic_neg_prio_requested`, with its layout taken from DWARF. If the box is built, run an **input-sensitivity test**: flip each env byte outside the `hflags-inputs` list and check that the rebuild output is unchanged.

### 4.4 C1 and C2: admission, code shape, premises

#### 4.4.1 C1 `hs-msr-fold@1`

**Admission, per `CALL` op.** Every check below is fail-closed with a stable reason code.

1. **Binding.** The binding is `v7m_msr`, and the axiom table row binds (`axiom_unbound`).
2. **Window shape.** The window is exactly the 14 bytes `be imm32 | 48 89 ef | ff 15 disp32`, immediately preceded by an instruction that defines `edx` (`arg_window_shape`).
   - The window lies inside one search row (`edit_on_insn_boundary`).
   - The window is not a branch target (`hcfg`) and does not overlap a LABEL, LDST or LOG_REG block (V15 rules, `S/validate.py:1861-1891`).
3. **Constant argument.** `imm32` matches an axiom row (`hconst` confirms it), and the row's key and CPU requirements hold (`key_fact_missing`, `cpu_fact_unbound`).
4. **Last instruction.** The call is in the **last guest instruction** of the unit, and the unit ends in the original `exit_tb(NULL,0)` (`site_shape`).
5. **Premise P-IN** (§4.4.3) holds on every path from TB entry to the call (`hflags_input_written_before`, `unaxiomatized_call_before`).
6. **Unit status.**
   - The unit has no plugin callback (`instrumentation_in_unit`).
   - It has normal cflags, `meta_status ok`, is `relocatable`, and is not a variant source (`unit_not_relocatable`).
7. **Profitability.** Train entries × MODEL saving > 0 (`not_profitable`). This affects ranking only.

**Code shape** (same length, in place; the value move into `edx` is kept unchanged):

```
BASEPRI, any value form (e.g. unit 8fc4f4f2, +0x18..+0x26):
  before: be 11 08 00 00        mov  esi,0x811
          48 89 ef              mov  rdi,rbp
          ff 15 32 00 00 00     call [rip+0x32]          ; helper_v7m_msr
  after:  0f b6 d2              movzx edx,dl             ; val & 0xff
          89 95 04 0c 00 00     mov  [rbp+0xc04],edx     ; v7m.basepri[0]
          0f 1f 44 00 00        nop5

PSP in handler mode (unit 24192331; P2b only, see 4.4.4):
  after:  83 e2 fc              and  edx,-4              ; val & ~3
          89 95 f0 0b 00 00     mov  [rbp+0xbf0],edx     ; v7m.other_sp
          0f 1f 44 00 00        nop5

BASEPRI, unprivileged key (no hot site; F-HS3 fixture):
  after:  0f 1f 84 00 00 00 00 00 ; 66 0f 1f 44 00 00     nop8 + nop6 (no effect)
```

- One template covers every value form (immediate, register, `xor`, env load), because the value move stays.
- The emitted forms clobber only `rdx` and EFLAGS. Both are dead after the original call (§2.2: only `rax` is live after a call, and these sites ignore it).
- The env store uses `[rbp + const]`.
- No branch, no other memory access.
- The pool slot of `v7m_msr` becomes unreferenced data inside `tc_size`. This is harmless; HS3 records it as `pool_slot_orphaned`.

**What stays byte-identical:**
- the TCG `save_globals` stores before the call and the reloads after it (flags 0);
- the `can_do_io` stores;
- the search rows, icount and entry check;
- `PC += len` and the `exit_tb(0)`.

**Why not keep the runtime-indexed form** `mov ecx,[rbp+0xc60]; mov [rbp+rcx*4+0xc04],edx`? It would drop the `secure == 0` dependency, but:
- hostsym cannot model symbolic env offsets;
- it does not fit every window.

So v1 uses the fact form only.

#### 4.4.2 C2 `hs-hflags-elide@1`

**Admission, per `CALL` op of `rebuild_hflags_m32_newel`:**

1. **Window.** The window is the 9 bytes `48 89 ef | ff 15 disp32` in the same guest instruction as an MSR call that is folded, or that is kept but covered by an effect or write-set row (`ws.msr.basepri@1` in the first ship) (`site_shape`).
2. **Premise P-IN** holds from TB entry to this call.
3. **Premise P-W.** Let **W** be the **may-write set**: the union over **all** paths from TB entry to the rebuild site of:
   - every host env store on the path, computed by hostsym on the *candidate* bytes, so C1's inserted store counts;
   - the axiom write set of every helper call on the path that has a row.

   **Every call without a row makes W = ⊤** (havoc all inputs). That includes softmmu slow paths, which may do MMIO, plugin callbacks, and `v7m_msr` under a key that no row matches. P-W holds iff `W ∩ hflags-inputs = ∅`.
   - Note: a must-write set ("written on every path") would be unsound here. It must be the union.
4. **`hflags-inputs@1`** is a **fail-closed** list. Offsets are MEASURED from EnvLayout `56d8bfb3…` (`S/formats/envlayouts/56d8bfb3….json`):

   | field | env offset | read by (gate tree) |
   |---|---|---|
   | `features` | 0x132e8 | every `arm_feature` test in the closure |
   | `uncached_cpsr` (`CPSR_E`) | 0x160 | `rebuild_hflags_common_32` → `arm_cpu_data_is_big_endian_a32` (`hflags.c:93-103` → `internals.h:487-509`), on **every** M-profile rebuild |
   | `cp15.sctlr_el[*]` | 0x2a8-0x2c8 | `arm_sctlr_b` (`cpu.h:2919-2930`) |
   | `cp15.scr_el3` | 0x3c0 | `access_secure_reg` (`hflags.c:69-75`), behind `ARM_FEATURE_EL3` |
   | `cp15.mdscr_el1` | 0x828 | `arm_singlestep_active` (`hflags.c:86` → `Q/target/arm/debug_helper.c:167-172`) |
   | `v7m.control[0..1]` | 0xc0c | `arm_current_el` (`internals.h:459-463`), `arm_v7m_mmu_idx_for_secstate` (`m_helper.c:194-197`) |
   | `v7m.ccr[0..1]` | 0xc14 | `rebuild_hflags_m32` (`hflags.c:110-140`) |
   | `v7m.exception` | 0xc48 | `arm_v7m_is_handler_mode` (`cpu.h:2629-2632`) |
   | `v7m.faultmask[0..1]` | 0xc54 | `armv7m_nvic_neg_prio_requested` (`armv7m_nvic.c:394-419`) |
   | `v7m.aircr` | 0xc5c | `exc_targets_secure` for the NMI case (`armv7m_nvic.c:413-414` → `:171-188`), behind `ARM_FEATURE_M_SECURITY` |
   | `v7m.secure` | 0xc60 | everywhere (bank index) |
   | `v7m.fpccr[0..1]` | 0xc8c | not read by this rebuild; kept as a conservative entry |
   | `v7m.cpacr[0..1]` | 0xc9c | `fp_exception_el` → `v7m_cpacr_pass` (`helper.c:11223-11250`, `internals.h:1247-1261`) |
   | `v7m.nsacr` | 0xca4 | `fp_exception_el`, behind `ARM_FEATURE_M_SECURITY` |
   | `nvic` (the pointer) | 0x13370 | `arm_v7m_mmu_idx_for_secstate_and_priv` (`m_helper.c:185-191`) |
   | `hflags.flags`, `hflags.flags2` | 0x150-0x160 | the rebuild's own output; any store to them breaks the identity |
   | pseudo-field `NVIC` | (not env) | `vectors[HARD].active`, `sec_vectors[HARD].active`, `vectors[NMI].active` (`armv7m_nvic.c:405-416`). Any call without a row writes it |

   **Closure rule.** A field that the closure reads only behind a test of another listed field (for example `scr_el3` behind `ARM_FEATURE_EL3`, or everything after the `mdscr_el1` bit-0 test in `arm_singlestep_active`, which on M-profile then stops at `arm_el_is_aa64` because `ARM_FEATURE_AARCH64` is clear, `internals.h:422-452`) is still listed when it is an env field, and the guard field must be listed too. If a field the rebuild reads is missing from the list, C2 is unsound. So:
   - **Non-cuttable P1 acceptance item: a hand re-derivation of the list**, one row per read, each with `file:line`, over at least `hflags.c:69-140` and `:455-467`, `fp_exception_el` (`Q/target/arm/helper.c:11223-11250`), `arm_mmu_idx_el` (`helper.c:11363-11368` → `m_helper.c:158-197`), `armv7m_nvic_neg_prio_requested` (`armv7m_nvic.c:394-419`) and every other function in the call closure of `helper_rebuild_hflags_m32_newel` in the gate ELF (the census lists `arm_security_space` among them, `H/census.md` §2 row 6). The table above is the starting point, not the result.
   - `helper-rwsum@1` (§4.9) is the automated version of the same check. It stays a later item, but the hand derivation does not.

**Code.** `48 89 ef ff 15 d32` → `66 0f 1f 84 00 00 00 00 00` (nop9).

**Why it is sound** (DERIVED):
1. At TB entry, AX-H gives `env->hflags == rebuild(env)`.
2. P-IN and P-W say that no rebuild input and no `hflags` byte changes between entry and the site, on any path.
3. So the call would store the value already there.
4. `rebuild_hflags_m32_newel` cannot unwind, takes no lock and does no MMIO (`hflags.c:460-467`). It reaches `__assert_fail` only on internal inconsistency.
5. Its flags are NO_RWG, so TCG emitted no sync code around it that could now be unbalanced.

**Expected result:**
- BASEPRI sites: W = {`r3` (env 0xc), `basepri[0]`} → elide.
- PSP sites (P2b): W = {`other_sp`} plus whatever the `ldst_slow` row allows → elide.
- CONTROL, FAULTMASK and PRIMASK sites: no row → W = ⊤ → refuse (`hflags_input_written`).

**C2 is its own pass, so it can be ablated.** It is valid with the original `call v7m_msr` kept, as long as that call has a matching row.

#### 4.4.3 Premise P-IN (key-fact validity at the site)

On every path from TB entry to the call there is:
- no host env store to any field of `hflags-inputs@1` (§4.4.2 item 4; one list serves both premises);
- no helper or plugin call without an axiom or write-set row, and no row whose write set meets that list.

This is checked by hostsym's env-write tracking, not by HostOps pattern matching alone. A plain guest store (inline LDST fast path to RAM) writes guest memory, not env, so it is allowed. Its slow path is a call without a row, so it is not. (Today hostsym models an LDST block as one atomic access followed by an env havoc that keeps only the TCG globals and `can_do_io`, `S/validate_sym/paths.py:143-155, 663`; that havoc covers every hflags input, so it already counts as ⊤ here.)

#### 4.4.4 Coverage in v1 and the P2b extension

**v1 core.** The three hot units with no LDST before the MSR (`28a2376d`, `8fc4f4f2`, `2dc6a851`) pass P-IN and P-W as-is: 0.928 M pairs/run MODEL, ≈ 86 % of all MSR calls. The two PendSV units (`4acba635`, `24192331`) have 12-13 LDST blocks before the MSR (MEASURED), so they are refused in v1.

**P2b extension** adds the row `ldst_slow.preserves_H@1`. Its claim: no softmmu slow-path call writes an `hflags-inputs@1` field, except through an NVIC/SCS MMIO store, and every such store reaches `arm_rebuild_hflags` at `exit_ok` of `nvic_sysreg_write` (`armv7m_nvic.c:2462`), so AX-H still holds after the call.

How P2b changes the rules:
- With this row, a slow-path call is treated as "preserves AX-H", not as ⊤. P-IN and P-W are then re-stated as "AX-H holds at the site".
- The row needs its own source review of the MMIO write handlers (`nvic_sysreg_write` and the SCS paths) and an O-HF run.
- It is P2b, not v1, because it widens the trusted base.

### 4.5 Numbered correctness invariants (v1)

| id | invariant | checked by |
|---|---|---|
| HS-I1 | **Shape.** An edit replaces exactly the C1 14-byte window or the C2 9-byte window by same-length bytes inside one guest instruction's host range. No other byte of the unit changes. Search rows, icount, cflags, the entry check, the `can_do_io` stores and the exit bytes are byte-identical (B3 invariant 3) | HS3 |
| HS-I2 | **Only non-unwinding calls are removed.** The row must say `unwinds=false`, `getpc=false`, `calls=[]`, `mmio=false`, `bql=false`, `locks=false` on its `requires` domain. Rows with `unwinds≠false` are never eligible for removal; they can only get the Shape A form (§4.7) | HS1, HS5 |
| HS-I3 | **Emitted bytes.** They write only `rdx` and EFLAGS, plus exactly the row's env write set, through `[rbp + const]`. They never touch `rbx`, `rbp`, `r12-r15`, `rsp` or the TCG frame. No branch, no backward edge | HS4 |
| HS-I4 | **Preconditions.** They come only from key facts (decoded from the **normalized** key `K.flags_env`, `K.flags2_env`, §4.2 item 2; never from the raw `cs_base`), bound CPU-model facts (HS-CPU), AX-H and the per-site premises P-IN/P-W (hostsym, may-write union over all paths) | HS2, HS7 |
| HS-I5 | **No row → havoc.** Any call without an axiom row is treated as writing all env and the NVIC pseudo-field (F13 fix). `NO_RWG` means only "no TCG-global write" | HS7 (hostsym) |
| HS-I6 | **The MSR site is the TB's last guest instruction, and `exit_tb(0)` is kept.** Interrupt re-evaluation after the MSR is unchanged | HS2, HS3 |
| HS-I7 | **Flag mis-declarations are preserved, never "fixed".** TCG's save/reload code is not edited; that is B4's job, done after re-import | HS3 |
| HS-I8 | **No new process address enters the `.sc`.** The orphaned pool slot stays | HS3, HS6 |
| HS-I9 | **Exe binding.** Every row is bound to the exe sha and to the helper-bytes sha (the closure for rebuild). A mismatch gives `axiom_unbound`, and the baseline is kept | HS1 |
| HS-I10 | **Refused units.** Units with plugin calls, special cflags, variants, `meta_status ≠ ok` or `relocatable = false` are refused | HS2 |
| HS-I11 | **Fail closed.** Any undecided case keeps the baseline bytes with a stable reason code | all |
| HS-I12 | **Retirement.** The artifact stays v5. The derived `.scm` retires exactly the folded or elided `HELPER_CALL` site and its `helper_call` recovery point, and nothing else. The result re-imports and passes the B1 cross-check (V8) | HS6 |
| HS-I13 | **AX-H domain.** An `hs` artifact is marked `vmstate_load_forbidden`. It is run only from reset: never with `-loadvm`, `-incoming`, or a monitor `loadvm`/`migrate-incoming` (§4.2) | HS-RUN |

### 4.6 The hostsym soundness fix (F13): a prerequisite

**The problem.**
- `call_class` maps every `NO_RWG` helper that is not `udiv`/`sdiv` to `"pure"` (`S/analysis/effects.py:41-55`).
- `_call` then havocs no env for classes `pure` and `raising` (`S/validate_sym/paths.py:617-618`).
- But `rebuild_hflags_m32_newel` (flags 0x1) writes `env+0x150` (4 B) and `env+0x158` (8 B) (`hflags.c:460-467`).

**The consequence.** Today hostsym would accept a mutant that moves an `hflags` read across a rebuild call. No current pass hits this, because B3/B4 facts cover only `r0-r12`/NZCV.

**Fix:**
1. `call_class` keeps the S-class for B4 sync purposes. The env havoc in `_call` instead comes from a new lookup, `helper_axioms.write_set(helper)`:
   - a row gives the exact may-write set;
   - **no row means havoc all env**, plus the NVIC pseudo-field.
2. Add rows `ws.rebuild_hflags_m32*@1` (0x150-0x160), `ws.udiv@1` and `ws.sdiv@1` (∅ on the returning path). This keeps today's behaviour for those helpers, now with evidence instead of by accident.
3. Add synthetic mutant M13 (§5.3), and record that it is accepted before the fix and rejected after it.
4. **Re-validate every existing B3 and B4 artifact** (the B5 arms) with the fixed hostsym. Each verdict change must be either none or explained. This is an acceptance test of P1, and it is **time-boxed to 1.5 weeks** with this triage rule:
   - a unit whose verdict goes from `equivalent` to `mismatch`: stop and report it to the B3/B4 owner the same day (it is a candidate soundness bug; do not "fix" it inside this project);
   - `equivalent` → `inconclusive` because a helper now havocs env: add a `ws.*` row only if the helper is one of the census helpers and its write set can be read from the source in under a day; otherwise record it as "conservative, explained" and move on;
   - anything still open at the end of the time box goes into a written list for the owner, and P1 continues.

### 4.7 The stub mechanism for later templates (Shape A), and forbidden shapes

v1 needs no stub. Any later template that keeps a slow path (for example the thread-mode PSP fallback) must use **Shape A**:

```
site (same 6 bytes):   ff 15 d32  call [rip+P]   ->   90 ; e8 rel32  nop ; call S   ; return address = site+6, unchanged
stub S (after the pool, appended by a generalized grow_unit / append_stub):
   <guard on args/env: jcc .slow>  ; straight-line, no backward branch
   <fast path: writes only caller-saved regs, EFLAGS, declared env fields>
   ret                              ; back to site+6
.slow:
   jmp qword [rip+P]                ; tail call through the SAME pool slot: helper sees [rsp] = site+6
```

**Rules U1-U8**, taken from design A (`H/design-A-templates.md` §7):

| rule | content |
|---|---|
| U1 | A slow path that reaches a helper that may unwind is a **tail jump** from a stub entered by the site's `call rel32`, so `[rsp]` = the original site+6. The `nop` precedes the `call`. Swapping them is mutant M-swap: detectable only statically |
| U2 | Rows, icount and cflags are byte-identical |
| U3 | The stub pushes nothing but its return address, so a longjmp out of the helper leaves nothing inconsistent |
| U4 | On the slow path the state is the baseline's: no added or removed env stores, flag quirks preserved |
| U5 | The `can_do_io` stores are unchanged; the fast path does no I/O |
| U6 | No new process pointers; only the unit's own pool slots |
| U7 | A **non-tail** slow call inside the stub is admissible only for helpers whose row proves `unwinds=false ∧ getpc=false` **from the exe bytes for that constant argument**. A review is not enough. Use the base-tracked summary of §4.9, or the exe-in-a-box run reaching no `[rsp]` read and no call to an unwinding function |
| U8 | Interrupt delivery points are unchanged |
| U9 | **Stack alignment.** Inside a TB `rsp ≡ 0 (mod 16)` (§2.1), so inside a Shape A stub, after `call S`, `rsp ≡ 8 (mod 16)`. The tail `jmp [pool]` is therefore correct: the helper sees the same alignment as from the original `call`. A **non-tail** `call` from the stub would enter the helper with `rsp ≡ 0 (mod 16)`, which breaks the SysV alignment that SSE spills in the helper rely on. Such a call must realign first (`sub rsp,8` … `add rsp,8`), which pushes a word and so breaks U3 and moves the frame that a `GETPC`/unwind would see. **A non-tail call from a stub is therefore admissible only under U7**, and only with the realignment |

**Restartability rules R1/R2**, taken from design B. They are the admission test for any slow exit:
- **R1.** No env, global or memory write, atomic or call happens before the exit.
- **R2.** Every argument register the helper reads holds its call-site value at the exit.

**Forbidden shapes:**

| shape | why |
|---|---|
| **A′**: `jmp S` at the site with a slow `call` in the stub | The return address lies past the last row, so the unwind returns −1 (§2.3). Allowed only under U7 |
| **B** with `push ra; jmp [pool]` | It breaks call/ret pairing under CET user shadow stacks. It is admissible only after showing that shadow stacks are off for the qemu process (`x86_Thread_features` in `/proc/<pid>/status`, captured in a diag run), or by using a construct that keeps call and ret paired. The Shape A tail jump *is* paired: one `call S`, one `ret` in the helper |
| Copying a GETPC read (`mov rsi,[rsp+8]`) into a TB | It would read TCG frame data |
| Referencing a QEMU address the unit does not already call | There is no HELPER row, so V8 re-import fails |
| Same-length fast path in the 6 call bytes | Does not fit |

### 4.8 Stretch passes (each is its own phase with its own gate)

#### 4.8.1 C3 `hs-msr-exit-chain@1` (stretch S1, gate G-C3)

**The opportunity.** After an MSR, the TB returns to the main loop. 82.8 % of all `exit_null` exits come from MSR TBs (MODEL, `H/mechanics.md` §3). Each hot MSR TB continues at its static successor:
- `0x1b4e` → `0x1b52` 1,178,254 times and → PendSV `0x1b58` 104,584 times (8.2 %);
- the other four units go to one successor in 100 % of cases (MEASURED profile edges).

**The design.** The 5-byte `jmp epilogue` exit becomes `jmp S`. `S` is the B3 guard of §2.4 (`emit_guard`, `S/guard.py:230`), with two changes:
- A new first atom **`g_irq`**: `cmp dword [rbp-0x2af0],0; jne F`. This compares the **whole 32-bit `cpu->interrupt_request` word**, never only the HARD bit.
- `tail_kind = "exit0"`: `F` is the original `jmp tcg_code_gen_epilogue`.

**Soundness core** (DERIVED; checked against source by the soundness reviewer):
- The NVIC raises `CPU_INTERRUPT_HARD` iff a pending exception outranks the active one, **ignoring BASEPRI, PRIMASK and FAULTMASK** (`armv7m_nvic.c:474-491`). So if `interrupt_request == 0`, no mask write can make anything takeable.
- With `interrupt_request == 0`, `cpu_handle_interrupt` only clears `icount_decr.high` and checks `exit_request`/`icount_exit_request` (`cpu-exec.c:788-892`).
- A later `cpu_interrupt` or `cpu_exit` sets `high = -1`, which the next TB's entry check catches.

**Admission requirements (from the reviews):**
1. **A signed written review of the argument against the project specification (line 64; not in the repository).** It is required before C3 appears in any headline arm. Start with the `nolower` subset, which contains only writes that cannot lower the mask: PSP and the constant-0x50 BASEPRI sites. That subset is ≈ 0.57 M exits/run (DERIVED).
2. **A forced-icount-budget fixture.** It settles the [I] on the `icount_exit_request` path (`cpu-exec.c:777-786`): the budget runs out exactly at the MSR TB, and O2a must equal warm.
3. **V-HS6 tooling refusal of profile mode.** The `scp_*` hooks run on the main-loop path (`scp_dispatch`, `cpu-exec.c:1014`), and profile mode never chains. Mark the artifact `profile_mode_forbidden`, and make `scopt run` refuse `QEMU_SC_PROFILE` with it.
4. **Document `align_clocks`** (`cpu-exec.c:1021`): under `-icount align` it runs less often. This changes host pacing only, never guest state.
5. **Arbitration order** with B3: `chain-static` → `chain-returns` → `msr_exit`. B3 rule 6 and V13's "no DISAS_EXIT R" (`S/analysis/return_edges.py:20-22`; `S/validate.py:1836-1837`) are relaxed **only** for `msr_exit`.

**Payoff.** ESTIMATE 1.5-4.3 %, because `c_exit` is unknown. Calibrate first with an `exitchain-hand` probe plus a real placement control (same `tc_size` growth, `int3` filled).

#### 4.8.2 C6 `inline-lookup-probe@1` (stretch S2; survey B3-7)

**Ownership.** It overlaps B3-7, so **agree ownership with the B3-7 owner before any work.**

**What it does.** At every `goto_ptr` exit left without a B3 slot, put a jump-cache probe in front of the lookup call. Read `tb` first, then compare `jc.pc`, `flags`, `cs_base` and `cflags` in `tb_lookup`'s order (`cpu-exec.c:233-251`). Store `can_do_io = 1` (`:391`) and `jmp [tb+0x20]`. On a miss, run the original tail. Design C §6.5 (`H/design-C-specialize.md`) gives the byte-level sketch.

**Must-fixes before it is admissible:**
1. **The rebuilt `cs_base`** must include or guard every dynamic flags2 bit that `arm_get_tb_cpu_state` adds: `FPCCR_S_WRONG`, `NEW_FP_CTXT_NEEDED`, `LSPACT`, `MVE_NO_PRED` (`hflags.c:567-592`).
   - An omitted bit gives a **false hit**, which is unsound.
   - Either guard the inputs (`fpccr[0]`, `fpccr[1]`, `vpr`, `ltpsize`) or bind "these are always 0" as a checked CPU-model row.
2. **The constant `cflags`** is valid only while `curr_cflags` cannot change (`cpu-exec-common.c:38-58`). Refuse the probe, or guard on `one_insn_per_tb`, the nochain log level and `singlestep_enabled`, as B3 AC2 does.
3. **The jump-cache hash depends on `TARGET_PAGE_BITS`**, which is runtime-variable (`TARGET_PAGE_BITS_VARY`, `target/arm/cpu-param.h:31-32`; 10 is chosen at `cpu.c:2272-2288`). Bind the hash constant and the `TranslationBlock`/`CPUJumpCache` offsets in a `tb-layout@1` file per exe sha, checked by a validation row.
4. **It bypasses the `scp_*` hooks**, so the artifact gets the same `profile_mode_forbidden` marking and refusal as C3.

### 4.9 Base-tracked read/write summaries (P3, recommended; taken from design B)

This is a small analysis, `helper-rwsum@1`. It is **not** design B's import pipeline. It is a capstone pass over a helper and its direct-call closure in the gate ELF. For each memory operand it tracks the base register:

| base | class |
|---|---|
| `env + c`, or `env + idx*s + c` with a range | `env` |
| an `rsp`-relative address | `frame` |
| RIP-relative | `rodata` or `global` |
| a pointer loaded from an env field | `deref(env+c)` |
| anything else | `unknown` |

**Rules:**
- `fs:`/`gs:` (TLS, e.g. `current_cpu`), `global`, `unknown`, PLT calls and unresolved indirect jumps make the summary **⊤ (may alias anything)**, on the read side as well as the write side.
- A summary is **never** inferred from the fact that a pointer was not passed as an argument.

**Uses:**
- Cross-check the hand list `hflags-inputs@1` against `R(rebuild)`. The two must agree, or the gap must be explained.
- Confirm `W(rebuild) = {0x150, 0x158}`. MEASURED by design B's displacement-level scan, but **not base-tracked**, so it is still only a hint.
- Prove U7 (`unwinds=false`, `getpc=false`) for any future non-tail slow call.
- Later, give B4 precise write sets.

### 4.10 Interaction rules

| concern | C1/C2 (v1) | stretch C3/C6 |
|---|---|---|
| **FastDyn plugin callbacks** | Units containing them are refused (`instrumentation_in_unit`); none of the hot MSR units has one (MEASURED). Plugin code can write registers through the gdb API (`N/b3/map-state-sync-traffic.md:151-158`), so a plugin call is an un-axiomatized call (⊤) | B3 GENERAL-slot rules (TB-start callbacks see the predecessor's `can_do_io`, G30) |
| **icount / record-replay** | No change to instruction counts, rows, entry charge or exits. O2a and O2b must equal warm in both icount modes | [I] on `icount_exit_request`; forced-budget fixture (§4.8.1) |
| **Interrupts** | `exit_tb(0)` kept (HS-I6); the delivery point is unchanged (O2b, F-HS1/F-HS6) | `g_irq` over the whole word; G-C3 |
| **Breakpoints / single-step** | A TB under a breakpoint or single-step runs with other cflags, so a normal-cflags unit is not used (`D/B3-B4-design.md` G10). gdb writes to M system registers are no-ops (`gdbstub.c:413-416`, G33), so key facts and AX-H survive | `g_bp` atom; C6 guards on the cflags inputs |
| **MMIO / BQL** | The removed calls do no MMIO and take no BQL (row fields `mmio`, `bql`, `locks`, §4.3). Earlier MMIO (an LDST slow path) makes the site ⊤ in v1; P2b adds the reviewed row | The BQL is needed only if `interrupt_request ≠ 0`, which falls back |
| **SMC / TB invalidation** | Guest bytes, `guest_hash` and page bindings are unchanged. Invalidating the TB removes the folded unit exactly as it would the original (B1 contract) | GENERAL slots are unlinked by stock jump lists and the v6 TLB hook (B3 §5.6); C6 compares `cflags`, so `CF_INVALID` fails safe |
| **Profile mode** | Allowed: no edge semantics change. Optimized artifacts are not profiled anyway | **Forbidden** (V-HS6) |
| **Migration / snapshot load** | **Forbidden** (HS-I13). `cpu_post_load` rebuilds hflags (`machine.c:1021`) before the NVIC state is loaded (`armv7m.c:365` vs `:370`, no `MIG_PRI`), and `nvic_post_load` does not rebuild (`armv7m_nvic.c:2473-2498`), so the NEGPRI bit can be stale after load; C2 would keep it stale (§4.2). `scopt run` refuses `-loadvm`/`-incoming` for an artifact marked `vmstate_load_forbidden` (row HS-RUN) | same |
| **B3 / B4 / SFB** | Compose by re-import. B4 may later remove the now-unneeded flag-0 save/reload around the folded MSR. B4 D1 is worth ≈ 0 per store (MEASURED 0.036 ns), so it is not counted | C3 competes with B3 for exit slots (arbitration order §4.8.1) |

---

## 5. Validation

### 5.1 Static rows

New `HS*` rows in `V_ROWS` (`S/validate.py:1184`), produced by `validate_hs` in `S/passes/hs_fold.py`. They are modelled on SFB1-SFB9 (`S/passes/icount_entry_sfb.py:612-756`). A FAIL keeps the baseline for that unit.

| row | check |
|---|---|
| **HS1 binding** | Exe sha; EnvLayout id; every used row's `bytes_sha256` (and the closure for rebuild) re-read from the ELF; binding `func` = PIE base + `addr` |
| **HS-CPU cpu_model** | The bundle's `cpu_model` is `cortex-m3` (`S/formats/bundle_v1.py:600-617`, as V20), and the bound CPU facts (`M`, `M_MAIN`, `¬M_SECURITY`, `secure0`) are recorded for it. Mismatch → every C1 fold is refused. Offline only: the run-time CPU model is bound by launch discipline (§4.2 item 3). C2 (first ship) uses no CPU fact |
| **HS-RUN launch domain** | The artifact carries `vmstate_load_forbidden` (and later `profile_mode_forbidden` for C3/C6). `scopt run` and the campaign tooling refuse to launch it with `-loadvm` or `-incoming`; a unit test checks the refusal (HS-I13) |
| **HS2 preconditions** | For each edit: key facts as the row requires; last guest insn; unit status; P-IN (C1, C2) and P-W (C2), computed by hostsym on the candidate as a may-write union over all paths |
| **HS3 placement / body identity** | Edits cover exactly the 14-byte / 9-byte windows, inside one row, with no branch target. Every other byte, the rows, icount, cflags, entry and exit are identical. Emitted bytes are exactly the template forms. The orphaned pool slot is recorded |
| **HS4 emitted-form lint** | Written registers ⊆ {`rdx`, EFLAGS}; env offsets = the row's write set; no frame or `rsp` access; no branch |
| **HS5 axiom evidence** | Every row used has a source review hash and a D3 certificate, plus a box certificate once leg 3 is built (§4.3), and its `requires` domain. `rebuild.identity@1` has its O-HF evidence and the hand re-derivation of `hflags-inputs@1` (§4.4.2 item 4) |
| **HS6 re-import** | V8 re-import with the derived `.scm`: exactly the retired sites and recovery points are gone, the B1 cross-check passes (`S/importer/crosscheck.py:93-94, 652-667`), and HostOps builds with no pins |
| **HS7 hostsym** | V12-style equivalence of every changed unit under axiom substitution (§5.2), with the F13 fix active |
| **HS8 untouched** | Units without an edit are byte-identical |

Existing rows to adjust:
- V6 recovery: drop the retired points.
- V12 coverage.
- V18: new term forms in the native self-test.
- V21: growth = 0 for v1.

### 5.2 hostsym extension (critical path)

1. **The F13 fix** (§4.6).
2. **Axiom substitution** in `_call` (`S/validate_sym/paths.py:582`), on the *original* program. When:
   - a call's helper has a row whose `match` constants are syntactically equal to the argument terms;
   - and the row's `requires` hold;

   then **no `call` event is emitted.** Instead:
   - the effect terms are applied to env;
   - `rax` gets the `ret` term (if any);
   - the caller-saved registers and flags become dead clobbers.

   The `requires` check means:
   - key facts are entailed by `pre`. `check_equiv` already takes env byte preconditions (`S/validate_sym/equiv.py:338`); pass `{(0x150,4): K.flags_env, (0x158,8): K.flags2_env}`, i.e. `K.flags & ~PSTATE_SS` and `K.cs_base & ~NOTCACHED2` (§4.2 item 2). **Never pass the raw key.** With the raw `cs_base` (0x800400 on the hot units, while env holds 0x400) the precondition is false, and hostsym would prove things under a false premise. Concretely, mutant M18 (§5.3), which replaces the rebuild call by `mov qword [rbp+0x158],0x800400`, would match the identity axiom (post = pre = 0x800400) and be accepted;
   - **C2 alone passes no `hflags` precondition at all.** The identity axiom then compares the post-state against the symbolic initial `hflags` bytes, so any store to them, whatever the constant, is a mismatch. Only C1 needs the key precondition, for `priv` and `handler`;
   - the CPU facts are bound by HS-CPU;
   - P-IN holds on this path, from env-write tracking in `State`.

   The candidate must contain no call at that point. A term mismatch fails with `helper_axiom_mismatch`; a precondition that is not entailed gives `validation_inconclusive`.
3. **Identity axiom.** `rebuild.identity@1` drops the rebuild event when P-W holds on the path. A kept `v7m_msr(0x811)` call is still a compared call event on both sides; its env havoc is restricted to `ws.msr.basepri@1`'s write set by the F13 `write_set` lookup.
4. **Term normalization** (`S/validate_sym/state.py:113-170`). Write each row's effect term as *exactly* the term hostsym builds for the emitted instruction (`movzx r32,r8`, `and r32,imm8`). Generate it by running hostsym's own semantics over the template, then check it natively in V18 (`S/validate_sym/selftest.py:47-170`). Equality stays syntactic; there is no SMT solver.
5. **Clobber liveness.** After a removed call, `hlive` must show the caller-saved registers dead (`S/analysis/hlive.py:12-18, 72-74`), which TCG guarantees.

### 5.3 Mutation suite

Each mutant must be rejected by a named static row, and where possible by a dynamic detector. Put synthetic-unit mutants in `S/validate_sym/mutants.py` and real-unit mutants in a new `S/hs_mutants.py` (style of `S/b4_mutants.py`).

| # | mutant | static | dynamic |
|---|---|---|---|
| M1 | store to `0xc08` (`basepri[1]`) | HS4, HS7 | F-HS1 (masking has no effect) |
| M2 | drop `& 0xff` (no `movzx`) | HS3, HS7 | F-HS2 (0x1ff reads back 0x1ff) |
| M3 | the priv row used on a `priv=0` key | HS2 | F-HS3 (SVC-handler readback of BASEPRI) |
| M4 | FAULTMASK (`0x813`) folded as BASEPRI | HS1 (no row) | F-HS4 (IRQ taken while it should be masked) |
| M5 | BASEPRI_MAX (`0x812`) folded as BASEPRI | HS1 | F-HS9 |
| M6 | C2 elision after a CONTROL write | HS2 (P-W) | F-HS8 |
| M7 | C2 elision in a synthetic unit with an earlier `v7m.ccr` env store | HS2, HS7 | — (`static_only`) |
| M8 | C2 elision with an earlier LDST slow path or plugin call (no row) | HS2 | F-HS10 |
| M9 | PSP fold under a `handler=0` key | HS2 | F-HS5 |
| M10 | the fold writes `rbx`/`r12` | HS4 | O1 |
| M11 | the fold window spans a row boundary | HS3 | — (`static_only`) |
| M12 | row bound to another exe sha, or helper bytes altered by 1 byte | HS1 | — |
| M13 | **F13 regression**: an `hflags` read moved across a rebuild call (synthetic) | HS7 (accepted before the fix, rejected after) | — |
| M14 | 2-byte store `mov [rbp+0xc04],dx` | HS4, HS7 | — (`static_only`: every writer masks `basepri` to 8 bits, `m_helper.c:2720, 2729`, so bytes 0xc06-0xc07 are always 0 and the mutant is dynamically equivalent) |
| M15 | `exit_tb(0)` replaced by a jump to the successor without a guard | HS3 | F-HS1/F-HS6 (O2b position differs) |
| M16 | PSP fold without `& ~3` | HS7 | F-HS5 (writes an unaligned PSP value) |
| M17 | fold applied to an MSR that is not the last guest insn | HS2 | — |
| M18 | **"C2-prime"**: the rebuild call replaced by a store of the *unstripped* key constant, `mov qword [rbp+0x158],0x800400` (+ nops) | HS7 (must reject: the precondition is the normalized 0x400, or absent for C2) and HS4 | O1/O2 may well be equal, because the lookup ORs THUMB in again and builds the same key. Detected by O-HF run on the mutant (env `flags2` has bit 23 set at the next rebuild entry, so that rebuild changes `hflags`) or by the DEBUG_TCG assert (§4.2 leg c). Hence HS7 is the real detector |

### 5.4 Fixtures to add (guest code)

**Where.** New directory `fixtures/hs/`, built like `fixtures/b3`: same toolchain and Makefile pattern, `-mcpu=cortex-m3 -mthumb`, MPS2 machine, semihosting output, `EXPECTED.md` style. They are qualified with `qualify.sh` over {icount, noicount}. Each fixture compares guest output plus the O2a/O2b records against warm.

**Non-vacuity is part of every fixture.** A refused site leaves the unit byte-equal to warm, so "O1/O2 equal to warm" then passes trivially and every dynamic mutant kill is void. Therefore:
- **Per-fixture HS report.** For each fixture, `scopt hs propose --report` on its warm snapshot lists every MSR/rebuild site with its reason code. The fixture's `EXPECTED.md` entry names the sites that must be admitted (reason `ok`) and, for negative fixtures (F-HS8, F-HS9, F-HS10 in v1), the expected refusal code.
- **Per-fixture O3.** In the folded arm, O3 shows a drop > 0 in `helper_v7m_msr` (C1) and/or `helper_rebuild_hflags_m32_newel` (C2) hits at the expected sites.
- Both are G-HS items (§5.6).
- **Write fixture values with `movw`/`movt` or `mov.w` immediates** (no flag setting). Do not use `ldr r3,=imm`: that is a literal-pool load, an LDST block in the same TB before the MSR, which makes P-IN/P-W refuse the site. Avoid flag-setting `movs` and other dirty globals just before the MSR: for a flags-0 helper, TCG loads the arguments first and then emits the clobber spills and `save_globals` stores (`Q/tcg/tcg.c:6141-6201`), which can split the 14-byte window (`arg_window_shape`). If a capture still splits the window, restructure the guest code. `fixtures/hs/README.md` states these rules and records the captured window bytes of each expected site.

| fixture | content | mainly kills |
|---|---|---|
| F-HS1 | BASEPRI raised to 0x50 with SysTick pending at a lower priority, then lowered to 0. The IRQ must be taken right after the lowering MSR (O2b position) | M1, M15 |
| F-HS2 | `msr basepri, r3` with r3 = 0x150 and 0x1ff (`movw r3,#0x150` / `movw r3,#0x1ff`); read back with privileged MRS | M2 |
| F-HS3 | MSR BASEPRI from unprivileged thread mode (`CONTROL.nPRIV = 1`): ignored. Unprivileged MRS BASEPRI reads 0 (`m_helper.c:2451-2453`), so **observe through privilege**: after the unprivileged MSR, `svc #0`; the SVC handler reads BASEPRI with MRS and prints it (expected: the value before, e.g. 0). Loop ≥ 3000 times for the D3 sample of `msr.basepri.unpriv@1` | M3 |
| F-HS4 | FAULTMASK set, then an enabled IRQ is pended (NVIC ISPR store); it must **not** be taken until FAULTMASK is cleared, and is taken right after. **No fault may occur while FAULTMASK is set**: a synchronous fault at priority −1 cannot escalate, and QEMU does not model lockup, so it calls `cpu_abort` on both arms (`armv7m_nvic.c:643-650`). `negpri` is observed through interrupt masking only | M4 |
| F-HS5 | MSR/MRS PSP in thread mode with SPSEL = 0/1 and in handler mode. The handler-mode write uses an **unaligned value with the low bits set**, e.g. `movw r0,#0x2003; movt r0,#0x2000` (0x20002003); MRS PSP must read back 0x20002000. The handler-mode MSR TB must have no LDST before the MSR, so that the v1 row `msr.psp.handler@1` has an admitted site (compute has none in v1) | M9, M16 |
| F-HS6 | PendSV pended inside a critical section, taken at `msr basepri,0` at exactly the warm icount | M15 |
| F-HS7 | gdb breakpoint on the instruction after an MSR; single-step through an MSR | interaction rules |
| F-HS8 | **negative**: MSR CONTROL (nPRIV/SPSEL) followed by rebuild. C2 must refuse; the unit is byte-equal to warm | M6 |
| F-HS9 | `msr basepri_max` must be skipped (`axiom_unbound`) | M5 |
| F-HS10 | an MSR in the same TB after a store to the NVIC SCS (MMIO). v1 must refuse; with P2b, AX-H must still hold | M8 |
| F-HS11 (S1 only) | forced icount budget exhausted exactly at an MSR TB | C3 [I] |
| F-HS12 | **negpri states.** (a) Take a HardFault by escalation (e.g. `udf` with UsageFault disabled), run a `BASEPRI` critical section (the FreeRTOS enter/exit pattern) inside the HardFault handler, fix the stacked PC and return. (b) The same inside an NMI handler (pend NMI through `ICSR.NMIPENDSET`). The MSR TBs in the handlers have `negpri = 1` keys, so C1/C2 apply to them too | AX-H under negpri (O-HF), M6-style errors |

The existing fixtures, including F-B4-6 `b4-div0` and `b_fault_sdiv` (`fixtures/EXPECTED.md:30,62`), run unchanged on every arm. Their output must equal warm.

### 5.5 Dynamic oracles

All are untimed diag runs:
- gdb `-nx -batch` under `setarch -R`;
- cwd outside any QEMU tree;
- through `scopt.run.diag.prepare_diag`/`gdb_hits` (`S/run/diag.py:161, 537`).

Cost is ≈ 65 µs per trap (MEASURED). O-HF on one compute input needs an entry trap and a `finish` trap per rebuild call (2 × 1.08 M) plus the O-CPU trap at every MSR (1.08 M): ≈ 3.2 M traps × 65 µs ≈ **210 s per input** (DERIVED). A D3 run of 3000 samples is a few seconds.

| oracle | what | requirement |
|---|---|---|
| O1 / O2a / O2b / O6 | output, register/icount trace, interrupt positions, coverage (`S/run/oracles.py`) | equal to warm |
| O3 non-vacuity | `helper_v7m_msr` and `helper_rebuild_hflags_m32_newel` hit counts on the baseline vs the folded artifact (`o3_nonvacuity`, `oracles.py:387`) | the drop equals the folded-site executions (> 0 per hot site) |
| O4 | the mutants of §5.3 as negative controls | each detected |
| **O-HF** | On the **baseline**, break at rebuild entry, record `hflags.flags/flags2`, `finish`, compare. Run on every train, held-out and final compute input, **and** on the fixtures that reach the states where AX-H is most likely to break: `fixtures/fault` (all variants, including `b_fault_sdiv` and the MMIO variant), F-HS4 (FAULTMASK), F-HS10 (SCS MMIO), F-HS12 (HardFault/NMI active, negpri), and a SYSRESETREQ case if one is added. Also log `secure` and `features` at every MSR (the O-CPU part) | 0 changes; `secure` and `features` constant |
| **D3 certificate** | ≥ 3000 sampled calls per row from the named sample source (§4.3): hash env + `neg` before and after under `set scheduler-locking on`, check `after == effect(before)` | 100 % |
| D1 (S1 only) | `interrupt_request` at MSR entry, per site | gives the C3 guard pass rate |
| D5 (S2 only) | jump-cache hit share at residual lookups | gives the C6 value |

### 5.6 Gates

- **G-HS** (a pass enabled in a headline arm). It is passed per ship: **G-HS/C2** for the first ship, **G-HS/C1** when C1 is added. All of:
  - a reviewer has accepted invariant 5′ and AX-H (with its no-`loadvm` domain) in writing;
  - the hand re-derivation of `hflags-inputs@1` is done and reviewed (§4.4.2 item 4);
  - HS1-HS8 and HS-RUN pass on compute; HS-CPU too for G-HS/C1;
  - the fixtures qualify in both icount modes: for G-HS/C2, F-HS1, F-HS2, F-HS6, F-HS7, F-HS8, F-HS10 and F-HS12; for G-HS/C1, also F-HS3, F-HS4, F-HS5 and F-HS9;
  - **non-vacuity per fixture**: each fixture's HS report shows its expected sites admitted (reason `ok`) or refused with the expected code, and the folded arm shows an O3 drop > 0 at every expected admitted site (§5.4);
  - every mutant that applies to the ship is rejected (static rows; dynamic detectors where the table names one);
  - O-HF shows 0 changes on all compute inputs and on the fault fixtures (§5.5);
  - the D3 certificates of the rows used pass (and the box certificates, once leg 3 is built);
  - O1/O2 are equal on held-out;
  - the B3/B4 re-validation after F13 is done within its time box, with every change triaged (§4.6).
- **G-C3.** A signed review against spec line 64, plus F-HS1/F-HS6/F-HS7/F-HS11 with O2a/O2b equal to warm, plus the D1 rates recorded.

---

## 6. Measurement plan

**Campaign rules** (all from B5):
- **All timing is done by the student, through the campaign tooling** (`scopt.run.arms.make_schedule`, `S/run/arms.py:785`), which takes the workspace timing lock itself.
- Timing happens only in a sanctioned slot, never at the same time as a B5 audit, and always on the gate binary.
- **Interleaved pairs.** At least 30 interleaved pairs per arm per set. Use B5's 30 reps × 3 inputs = **90 pairs**, which gives MDE ≈ 1 %.
- **Inputs.** Train inputs are for calibration only (P0). Claims are made on **held-out**, then confirmed once on **final**.
- **Statistics.** Median paired speedup with a 95 % percentile bootstrap (10,000 resamples), as in B5 (`R/B5-evaluation.md:62, 83`).
- **Twin checks.** Every timed run has an untimed diag twin with 0 `tlb_flush*` after `benchmark_start` (`S/run/diag.py:610-620`).
- **Arm names have equal length.** B5 found an arm-name path-length bias (`R/B5-evaluation.md:212`).

**Arms:**

| arm | content | compared with | purpose |
|---|---|---|---|
| `warm` | base snapshot | | baseline |
| `idnt` | bytes = warm, packaged by `scopt hs package` with no edits | warm | A/A floor. For v1's in-place edits this is **also the placement control**: v1 changes no size or position, so a separate grown-but-unpatched control would be identical to it |
| `hsfd` | C1 + C2 | `idnt`, warm | the claim |
| `hsc1` | C1 only | `idnt` | ablation |
| `hsc2` | C2 only (msr call kept, covered by `ws.msr.basepri@1`) | `idnt` | **the first-ship claim**; later the ablation that is expected to carry most of the effect |
| `b3rf` / `b3hs` | fresh B3 reference / B3 on re-imported `hsfd` | each other | additivity with B3 |
| stretch `hsx3`, `hsxn`, `hsxp` | C3, C3-nolower, and a **real placement control** for C3 (same `tc_size` growth and move, `int3`-filled, exits not redirected) | `hsxp` | the C3 effect without layout effects |

**Attribution.**
- Per pair: `c_pair = (t_idnt − t_hsfd) / N_fold` (DERIVED).
- `N_fold` is the MEASURED O3 drop in `helper_v7m_msr` hits for that input, from an untimed twin. This is the WP16 method (`R/WP16-probe.md:40-48`).
- Compare `c_pair` with the MODEL 13.5 ns.
- Split: `(t_idnt − t_hsc1)/N` and `(t_idnt − t_hsc2)/N`. Check additivity: `hsfd` vs `hsc1 + hsc2`.
- **Optional mechanism check**, if `perf stat` is allowed on the host and untimed: retired instructions for `hsfd` vs `idnt`. MODEL: ≈ 250 × 0.93 M ≈ 230 M fewer per run.

**Reporting.**
- Report this as its own appendix category, "helper specialization", like `sfb`: not caching, not B3/B4.
- Report Δms, CI, R11 flag, `c_pair` with its label, O3 counts and every correctness row.
- **Report a null result as a null result.** Never claim an effect inside the A/A CI.

---

## 7. Phased plan (one graduate student)

### 7.1 Core (ESTIMATE 14-18 weeks; planned as 18 with slack; first ship ≈ week 11)

The first version of this plan put everything into 10 weeks. That is not realistic for one new student, and the overrun would land on items that must never be cut. The plan below therefore:
- ships **F13 + C2 alone first** (≈ 81 % of the modelled value, §1.2). C2 needs only the identity axiom, the `ws.msr.basepri@1` write-set row and `hflags-inputs@1`: no key facts, no CPU facts, no `secure == 0` closure, no axiom substitution with effect terms;
- adds C1 as a second ship;
- puts the box leg, C4, `helper-rwsum@1` and P2b **behind the MVR line**: they are started only after the first ship;
- treats Q1 (§9) as a **blocking week-1 dependency**;
- time-boxes the B3/B4 re-validation (§4.6 item 4).

ESTIMATE per phase: P0 2-3 weeks, P1 4-5, P2 3, P3 1, P4 3-4, P5 1 (14-17 in total). The table plans each phase at its upper end and adds one explicit slack week, which also absorbs waits for a sanctioned timing slot.

| phase | weeks (ESTIMATE) | work | acceptance test |
|---|---|---|---|
| **P0 Reproduce, unblock, probe** | 1-3 | **Week 1, blocking:** get written answers to Q1 (site retirement via an ANNOTATE packager, or a typed edit?) and Q2 (debug build), and book a timing slot. Read §2 and `D/B3-B4-design.md` §1-4. Re-run the census, the hot-unit disassembly and the key decode (§3). Run O-HF (untimed) on train-1. **By hand** (`T/experiments/hs_probe.py`, in the style of `T/experiments/b4_probe.py`; labelled experiments, never published), build the `hsfd`, `hsc1` and `hsc2` copies of the 3 v1-core units. Check them untimed with O1/O2a/O2b/O3. Run 90 train pairs × {idnt, hsfd, hsc1, hsc2} in the slot | Q1 and Q2 answered in writing. Census numbers within ±1 % of §3. Hand copies equal warm on O1/O2. O-HF 0 changes. **A written go/no-go**: continue if `hsc2` or `hsfd` vs `idnt` has CI > 0 (R11 noted if the median ≥ 1 %); otherwise take the MVR path (§7.3). If no slot is available by week 3, start P1 (it is needed in every outcome) and take the decision before P2 |
| **P1 F13 and the C2 proof** | 4-8 | Axiom-table format, loader and ELF binding (`helper-axioms@1`, HS1), with `write_set(helper, args)`. **The F13 fix** (§4.6). Rows `ws.rebuild_hflags_m32*@1`, `ws.udiv@1`, `ws.sdiv@1`, `ws.msr.basepri@1`, `rebuild.identity@1`, each with a source review and a D3 certificate (`scopt hs axioms {cert,check}`). `hflags-inputs@1` and **its hand re-derivation** (§4.4.2 item 4). hostsym: identity axiom and P-W (no axiom substitution yet). **B3/B4 re-validation, time-boxed to 1.5 weeks** (§4.6). Synthetic mutants M7, M11, M12, M13, M18 | The hand `hsc2` bytes validate as `equivalent`. These synthetic mutants are 100 % rejected (M13 accepted before the fix). The re-validation is triaged within its time box. Hand derivation reviewed |
| **P2 First ship: C2 end-to-end** | 9-11 | `hs-hflags-elide@1` (ANNOTATE + `package_hs` + site and recovery retirement as decided in Q1 + derived `.scm`). HS1-HS3, HS5-HS8, HS-RUN. Fixtures for G-HS/C2 with their HS reports and O3 (§5.4), F-HS12 included. O-HF on compute and the fault fixtures. Real-unit mutants for C2. CLI `scopt hs {census,propose,package,validate,oracle}`. `T/pipelines/hs-fold.toml` | **Gate G-HS/C2** (§5.6). The artifact re-imports (HS6). Packaged bytes equal the P0 `hsc2` hand bytes on the 3 units |
| **P3 Evaluate the first ship** | 12 | Arms `warm`, `idnt`, `hsc2` (and `b3rf`/`b3hs` if a slot allows) on held-out, then final. Untimed O3 twins | Report table with CIs and `c_pair` for C2 |
| **P4 Second ship: C1** | 13-16 | `key-facts@1` with key normalization (§4.2 item 2); CPU facts and HS-CPU; close the `secure==0` [I] (or fall back, R3); axiom substitution, term normal forms and V18 (§5.2); rows `msr.basepri.priv@1`, `msr.basepri.unpriv@1` (or drop it, §4.3), `msr.psp.handler@1` with D3; HS4; fixtures F-HS3, F-HS4, F-HS5, F-HS9; mutants M1-M5, M9, M10, M14, M16, M17. Then `hsfd`/`hsc1` in a slot | **Gate G-HS/C1**. `hsfd` vs `hsc2` reported, null or not |
| **P5 Report** | 17 | B5-style report section, "helper specialization" appendix category | Report reviewed. Every number labelled |
| **slack** | 18 | Explicit slack: slot waits, re-validation fall-out, fixture rework | — |

**The MVR line.** Everything above is needed for the first ship or the MVR. Everything below is started only after G-HS/C2, in this order:
1. the exe-in-a-box certification leg (§4.3 leg 3);
2. `hs-mrs-fold@1` (C4);
3. the `helper-rwsum@1` cross-check (§4.9);
4. P2b: the `ldst_slow.preserves_H@1` row and the two PendSV units;
5. the stretch items of §7.2.

### 7.2 Stretch (each needs its own short design note and probe before any build)

- **S1 C3 exit-chain.** First the `exitchain-hand` probe with the `hsxp` placement control, then G-C3, then the `nolower` subset, then all sites.
- **S2 C6 inline lookup probe.** Only with the B3-7 owner's agreement; `tb-layout@1` binding; D5 first.
- **S3 Shape A guarded fallback** for thread-mode PSP or unbound CPU facts (§4.7). Work items:
  - `append_stub` factored out of `grow_unit` (`S/link.py:746-866`). `grow_unit` refuses an already-grown unit (`link.py:770-771`), so this needs a stub list.
  - **hostsym local call/ret into the unit's own stub.** Today `_call` treats a `call rel32` whose target has no HELPER binding as an `unknown` call with full env havoc and a fresh `rax` (`S/validate_sym/paths.py:582-631`), so a Shape A site could never be proved equivalent. hostsym needs a modelled return-address push, execution of the stub body, `ret` back to site+6, and the tail `jmp [pool]` turned into the original call event with the original `[rsp]`.
  - Static checks for U1, U3, U7 and U9.
- **Follow-on project, not this one:** ROM-literal fold or TLB-conflict avoidance for the softmmu slow paths. Its entry gate is an O-VT oracle: slow-path hits vs `arm_cpu_tlb_fill` hits.

**Cut order if time runs short:**
1. S2;
2. S1;
3. S3;
4. P2b;
5. `hs-mrs-fold` (C4);
6. the `helper-rwsum@1` cross-check (keep the fail-closed hand list and its hand re-derivation, and label it so);
7. the box leg;
8. **P4 (C1)**: the project then ships C2 alone, which is a complete result.

**Never cut:** the F13 fix; the B3/B4 re-validation (time-boxed, not skipped); the hand re-derivation of `hflags-inputs@1`; the HS rows and HS-RUN for whatever ships; per-fixture non-vacuity; O-HF on compute and the fault fixtures; the P0 decision rule.

### 7.3 Minimum viable result

If P0 shows no effect (CI includes 0), the student stops building passes after P1 and delivers:
1. the F13 hostsym fix with M13 and the triaged B3/B4 re-validation, which is a real soundness improvement;
2. the exe-bound write-set rows (`ws.*`) and `rebuild.identity@1`, each with a source review and a D3 certificate, plus the reviewed `hflags-inputs@1` derivation;
3. the MEASURED `c_pair` with CI, and the `hsc1`/`hsc2` split;
4. a short report of the null result: helper call overhead on this workload is below the detection limit.

If P0 is positive, the default first ship is C2 alone (`hs-hflags-elide`) on the 3 core units (P2). Its code change is one `nop9` per site.

---

## 8. Rejected alternatives

| alternative | from | why rejected (or deferred) |
|---|---|---|
| **Hand-written Shape A stubs as the main mechanism** (`udiv.nz`, `v7m_msr.basepri+rb`) | A | For the hot MSR sites, the key facts make the run-time guards unnecessary. The in-place fold removes strictly more work (both indirect calls and the guard instructions) with no stub, growth or local-call frame in hostsym. A's milestones also spent ~6 weeks on zero-value udiv machinery before the only payoff. **Kept:** Shape A as the documented fallback (§4.7), U1-U8, the mutant ideas, O-HF, P-RB (→ P-IN/P-W), and the placement-control discipline |
| **Machine-code import / slicing from the exe** (`hxi-import`, helper-mode hostsym) | B | Needs a binary lifter (CFG, jump tables, constant folding, renaming, certificate replay) plus a much larger trusted hostsym kernel, for an msr import worth 0.3-0.9 % (ESTIMATE). The imported msr stub indexes env by a run-time value (`[rbp+rcx*4+0xc04]`), which hostsym cannot model. **Kept:** R1/R2, exe-in-a-box, base-tracked R/W summaries (§4.9), and elision as its own ablatable pass |
| **`udiv`/`sdiv` fast paths** (survey D-2) | A, B, census | Shape A saves ≈ 0 (the helper is already 8 insns). Shape B saves 1-3 ns/call ≈ 0.15-0.5 % (ESTIMATE), below the noise floor, and breaks invariant 3. |
| **`hs-div-valguard`** (constant-divisor strength reduction behind `den == 255`) | C5 | Its premise (`den == 255` is the hot divisor) was an [I]. diag-D MEASURED `den = 0xfff1` (the Adler-32 modulus) in 3/3 samples, and only 0.68 % of calls have a constant divisor. Below the noise floor either way. Dropped |
| **Shape B splice** (inline fast path, shift rows by Δ) | A, B | ≤ 0.5 % value. Breaks invariant 3, so it needs the full SFB treatment (own rows, own arm). `push ra; jmp` breaks CET pairing (§4.7) |
| **`shl_cc`, `v7m_mrs` templates** | A, B | ≤ 71 k and 72 k calls/run; < 0.1 %. The real `shl_cc` cost is the flag-0 save/reload, which is B4's business. `mrs` is kept only as the optional hostsym test vehicle C4 |
| **Softmmu victim-TLB template** (`ldst.vtlb`) | A | Must reproduce `victim_tlb_hit`, the `tlb.c.lock` xchg, the full-entry swap and DWARF layouts. The read-only variant changes TLB state, which the B1 contract does not allow yet. Avoiding the conflict misses is worth more. Deferred to a follow-on project gated on O-VT |
| **Removing `exit_tb(0)` after MSR as part of v1** | C3 | Conflicts with the literal text of spec line 64, and the `icount_exit_request` path is still [I]. Moved to stretch S1 behind G-C3 |
| **New frozen `EditKind`s in v1** (`FOLD_CALL=17`, `INLINE_PROBE=18`, `HELPER_STUB=19`) | C | They need a design §3.7 edit (`S/passes/base.py:147-152`). The SFB ANNOTATE route does the job without one; the typed route is v2, with owner sign-off |
| **Generic same-helper/same-args CSE** | C | No hot instance: the two udivs per TB have different numerators (MEASURED). Analysis only, if anything |
| **"Fixing" TCG flag mis-declarations** (udiv `NO_RWG` but raising; `shl_cc` flag 0) | all | Forbidden (HS-I7). The slow path must reproduce baseline state exactly; fixtures F-B4-6 pin the quirk |
| **Inlining `lookup_tb_ptr` in general** | — | B3 owns it. C6 handles only the residual, with the owner's agreement |

**If a later student revives division fast paths, these hazards apply** (DERIVED from `Q/target/arm/tcg/op_helper.c:134-173`):
1. **`INT_MIN / -1`.** QEMU's `sdiv` returns `INT_MIN` (`op_helper.c:160-162`). An inline x86 `idiv` raises `#DE`, and QEMU dies with SIGFPE. The fast path must test for it and take the slow path.
2. **Division by 0.** An inline `div`/`idiv` by 0 also raises `#DE`. The guard `den != 0` must come before the divide, on every path.
3. **DIV_0_TRP is a run-time input, not a key fact.** Whether `den == 0` traps depends on `v7m.ccr[secure] & DIV_0_TRP`, read at run time (`op_helper.c:140-143`); it is not a TB-key bit. So `den == 0` must always take the real helper.
4. **The slow path can unwind.** The trap is raised with `GETPC()` (`op_helper.c:157, 169`), so any slow call must follow U1 (tail jump from a Shape A stub) or U7/U9.
5. **Keep the `NO_RWG` mis-declaration.** `udiv`/`sdiv` are declared `NO_RWG` although they can raise, so guest registers held in host registers are **not** stored to env before the call, and env is stale when the trap is taken. This baseline behaviour must be reproduced exactly, not fixed (HS-I7; fixtures F-B4-6 `b4-div0` and `b_fault_sdiv` pin it).

---

## 9. Risks and open questions

**Risks.**

| # | risk | mitigation |
|---|---|---|
| R1 | **The payoff is marginal or null.** MODEL 1.3 % rests on κ, which was borrowed from a different helper and binary | P0 hand probe at n = 90 before any pass code; MVR (§7.3) |
| R2 | **AX-H is violated** by a writer missed in the audit, e.g. a FastDyn virtual writing system registers through an internal API | Writer audit with `file:line`; O-HF on every input; refuse configurations with virtuals outside AC4; optional DEBUG_TCG build |
| R3 | The `secure == 0` closure fails (§4.2 [I]) | Fall back to the Shape A guarded stub (S3) for C1. C2 is unaffected only if the msr row still binds; otherwise C2 refuses too |
| R4 | hostsym normal forms grow ad hoc | Effect terms are generated from hostsym's own semantics over the template, plus a V18 self-test per form |
| R5 | Site and recovery retirement surprises the importer cross-check | HS6 in P2. Ask the format owner early (Q1) |
| R6 | Binary drift | Every row and offset is exe-sha bound; a new binary gives `axiom_unbound` and the baseline |
| R7 | **The F13 fix changes B3/B4 verdicts** | That is the point of the re-validation acceptance test. Each change is either a found bug or needs a row |
| R8 | Path-length or layout bias hides effects < 0.5 % | Equal-length arm names; `idnt` A/A; never claim inside the A/A CI |
| R9 | Workload specificity: the value sits in FreeRTOS critical sections (blinky ≈ 4.6 k msr/run) | Report as workload-specific; blinky is a no-regression check |
| R10 | **AX-H fails after a VM-state load** (§4.2, "Snapshot load") | Domain restriction HS-I13, enforced by HS-RUN; a savevm-in-HardFault fixture is needed before lifting it |
| R11 | **Schedule overrun** lands on never-cut items | C2-first plan with the MVR line and cut order of §7.1-7.2; Q1 blocking in week 1; re-validation time box |

**Open questions** (to settle with the supervisor or owner in week 1):
1. **Format owner (blocking, week 1).** Is retiring a `HELPER_CALL` site in the derived `.scm` via an ANNOTATE packager acceptable, or must it be a typed edit? P2 cannot start without the answer.
2. **Debug build.** May the student build a `--enable-debug-tcg` QEMU of the same tag in a scratch tree (AX-H evidence leg c)?
3. **Reporting.** Is `hs` an appendix arm like `sfb` (recommended), or a fifth B5 category?
4. **C3.** Who signs G-C3 against spec line 64?
5. **C6.** Does the B3-7 owner want C6 inside B3 instead?
6. **Profile mode.** Does any profile-mode tooling ever load optimized artifacts today? Audit `scopt run` before S1/S2.
7. **CET.** Is CET shadow-stack enforcement off for qemu on the timing host? This matters only if Shape B ever comes back.

---

## 10. File map: where each piece goes, and what to copy from

| piece | new or changed file | copy or pattern from |
|---|---|---|
| Axiom table format and loader | new `S/formats/helperaxioms_v1.py`, `S/formats/helperaxioms/56d8bfb3….json` | `S/formats/envlayout_v1.py` (binding rule `:27-30`), `S/formats/envlayouts/56d8bfb3….json` |
| Axiom analysis | new `S/analysis/helper_axioms.py` (`helper-axioms@1`: `axiom_for(...)`, `write_set(helper)`), registered in `S/analysis/registry.py` | `S/analysis/envlayout.py`; ELF access `S/decode/symbols.py` (`ElfImage.symbol`) |
| Key and CPU facts | new `S/analysis/key_facts.py` (`key-facts@1`, with key normalization) | `unit.key` (`TBKey`); `NOTCACHED2`, `PSTATE_SS`, `guard_key` from `S/guard.py:57-63, 165-187` (import, do not copy); bundle `cpu_model` as used by V20 |
| Launch-domain refusal (HS-RUN) | `vmstate_load_forbidden` marking in the `hs` packager; refusal of `-loadvm`/`-incoming` in `scopt run` | the planned `profile_mode_forbidden` refusal of C3 (§4.8.1 item 3) |
| Call sites | new `S/analysis/callsites.py` (`helper-callsites@1`: window bytes, arg terms via `hconst`, row index, `rax` liveness via `hlive`) | `S/analysis/hostops.py:804-815` (CALL ops), `hconst.py`, `hlive.py` |
| hflags inputs | new `S/analysis/hflags_inputs.py` (`hflags-inputs@1`, fail-closed) | EnvLayout names |
| R/W summaries (P4/S) | new `S/analysis/helper_rwsum.py` | capstone use in `S/decode/x86.py`; prototype idea in design B §4.1 |
| F13 fix | `S/analysis/effects.py:41-55` (keep S-classes), `S/validate_sym/paths.py:582-631` (env havoc from `write_set`) | — |
| hostsym axiom substitution, identity axiom, normal forms | `S/validate_sym/paths.py`, `equiv.py` (near `check_equiv :338`, `_compare :350`), `state.py:113-170`, `selftest.py:47-170`, `mutants.py` | `check_guarded_chain` (`equiv.py:443`) for structure; `T/tests/unit/test_hostsym.py` |
| Passes | new `S/passes/hs_fold.py`: `PASSES = [hs-msr-fold@1, hs-hflags-elide@1, (hs-mrs-fold@1)]`, `HsReason`, ANNOTATE key `x_hs_fold`, `package_hs`, `validate_hs` (HS rows) | `S/passes/icount_entry_sfb.py` (`SfbReason :98-112`, rows `:612-756`, `package_sfb :870-946`, `_Native :248-316`); discovery `S/manager.py:135-187` |
| Patch templates | `PATCH_TEMPLATES` entries `hs_fold_basepri`, `hs_fold_psp`, `hs_elide_call` under the pass name (`S/passes/base.py:178`); pass/template pairing enforced by `S/proposal.py:417-438` | existing B3/B4 templates |
| Site and recovery retirement, derived `.scm` | the packager in `hs_fold.py`; `S/importer/recovery.py:88-109`, `S/importer/crosscheck.py:93-94, 652-667` must accept the retired sites | SFB's derived-`.scm` handling (`_shift_chunk`, `icount_entry_sfb.py:436-482`) |
| Validation rows | `V_ROWS` entries in `S/validate.py:1184` (HS1-HS8, HS-CPU) | SFB1-SFB9 |
| Real-unit mutants | new `S/hs_mutants.py` | `S/b4_mutants.py` |
| CLI | new `S/commands/hs.py`: `scopt hs {census,propose,package,validate,oracle,axioms}` | `S/commands/b4.py:137-160` (`scopt sfb`) |
| Pipeline | new `T/pipelines/hs-fold.toml` (`allowed_capabilities = ["ANNOTATE"]`) | `T/pipelines/tcg-entry-sfb.toml` |
| Hand probe (P0) | new `T/experiments/hs_probe.py` | `T/experiments/b4_probe.py` |
| Diag oracles | O-HF, D3, O-CPU in `scopt hs oracle` via `S/run/diag.py` (`prepare_diag :161`, `gdb_hits :537`); O3 requirements in `S/run/oracles.py:387` | the census gdb scripts (`H/msrdiag.py`, `H/design-scripts/stepcount*.py`; not durable, re-create them) |
| Fixtures | new `fixtures/hs/` (sources, `Makefile`, `qualify.sh`, `EXPECTED.md` entries) | `fixtures/b3/` (`Makefile`, `qualify.sh`, `qualify_b3.py`, `README.md`) |
| Tests | `T/tests/unit/test_hs_fold.py`, `test_helper_axioms.py`, `test_hostsym_axioms.py`; `T/tests/acceptance/test_hs_gate.py` | `test_icount_entry_sfb.py`, `test_hostsym.py`, `T/tests/acceptance/test_b4_gate.py` |
| Report | arm category in `S/report/b5.py` (stacking via re-import, `:522`) | the `sfb` arms |
| Stretch C3 | `S/passes/hs_exit_chain.py`; `g_irq` in `S/guard.py` `ATOMS`; `emit_guard(tail_kind="exit0")`; `msr_exit` in `S/analysis/return_edges.py` and `EXIT_CLASSES` (`base.py`); `check_exit_chain` in `equiv.py` | `S/passes/chain_static.py`, `chain_returns.py`, `S/guard.py:230`, `S/link.py:746-866` |
| Stretch C6 | `S/passes/lookup_probe.py`; `tb-layout@1` JSON per exe; `emit_probe` | `S/guard.py` (`decode_guard :384`) |

---

## 11. Review must-fixes and where they are applied

| must-fix (from the two reviews) | where |
|---|---|
| Fix the hostsym `NO_RWG` havoc gap before any elision or fold; havoc for helpers with no row; re-validate the B3/B4 artifacts | §4.6; HS-I5; P1 acceptance; M13 |
| The elision premise is a **may-write** union over all paths; un-axiomatized calls (LDST slow paths, plugins) havoc all inputs | §4.4.2 P-W; §4.4.3; §4.4.4 (PendSV units refused in v1) |
| Write sets are never inferred from argument passing; TLS and globals are ⊤ | §4.9 |
| State AX-H once, with the writer audit, the full-run oracle and the optional DEBUG_TCG build | §4.2 item 1; O-HF §5.5 |
| Bind the CPU-model facts (M, M_MAIN, ¬M_SECURITY, secure==0) by a row; close the `secure==0` question or fall back to the guarded stub | §4.2 items 2-3; HS-CPU; R3 |
| `ARM_FEATURE_M` is in the domain | §4.2 item 3; axiom `requires.cpu.M` |
| Admission: the MSR is the last guest insn; no earlier write to control, exception, ccr, faultmask | §4.4.1 items 4-5; HS-I6 |
| No new frozen EditKinds in v1 (ANNOTATE route); site retirement passes V8/B1 | §4.1; HS6; HS-I12; Q1 |
| C3 out of the v1 headline: signed G-C3, whole-word `g_irq`, forced-icount fixture, profile-mode refusal, `align_clocks` note | §4.8.1; F-HS11; §5.6 |
| C5 dropped (den 0xfff1 MEASURED) | §8 |
| C6: dynamic cs_base bits, constant cflags, `TARGET_PAGE_BITS` binding, scp bypass, ownership | §4.8.2 |
| Shape A slow exits for unwinding helpers are tail jumps; a non-tail call only with `unwinds`/`getpc` proven from the exe bytes | §4.7 U1, U7 |
| Shape B `push ra; jmp` forbidden unless shadow stacks are confirmed off | §4.7 forbidden shapes; Q7 |
| Every saving is ESTIMATE; a hand probe with controls at n = 90 in a sanctioned slot comes first; report nulls | §1.2; §6; P0; §7.3 |
| Payoff milestone before zero-value machinery | §7.1: payoff probe in weeks 1-3, first ship (F13 + C2) by week 11 |

---

## 12. Review responses (third review, 2026-11-18)

Every issue was checked against the gate tree, scopt `main` and the scout data before it was applied. All twelve were confirmed. Where this revision goes further than the suggested fix, or differs from it, the reason is given.

| # | severity | issue | verified against | where applied | notes |
|---|---|---|---|---|---|
| 1 | major | Raw key used as hostsym precondition; `cs_base ≠ hflags.flags2` | `hflags.c:556-625`; `cpu.h:2958, 2972-2973, 3010-3016`; `S/guard.py:57-63, 165-187`; EnvLayout (flags2 at 0x158) | §2.2, §3.2, §4.2 item 2 (normalization and refusal), HS-I4, §5.2 item 2, mutant M18 | **Goes further:** C2 alone now passes *no* `hflags` precondition, so the identity axiom compares against the symbolic initial bytes and any store to them is rejected. Only C1 needs the normalized key precondition. **Differs on M18's dynamic detector:** the mutant is probably dynamically equivalent on O1/O2, because the lookup ORs THUMB in again and builds the same key; it is caught by HS7, by O-HF run on the mutant, or by the DEBUG_TCG assert. Keys with `SS_ACTIVE`, `PSTATE__SS` or cs_base bits 2-5 set are refused, as `guard_key` does, rather than silently masked |
| 2 | major | `hflags-inputs@1` incomplete | `hflags.c:69-140, 455-467`; `internals.h:422-509`; `debug_helper.c:167-172`; `armv7m_nvic.c:171-188, 394-419`; `helper.c:11223-11250, 11363-11368`; `m_helper.c:185-199`; EnvLayout offsets | §4.4.2 item 4 (new table with offsets and readers, closure rule), §4.4.3 (P-IN uses the same list), §7.1 P1, §7.2 never-cut list, G-HS | Added `uncached_cpsr`, `cp15.mdscr_el1`, `cp15.scr_el3`, `v7m.aircr`, and also the **`nvic` pointer** (env 0x13370), which the review did not list but `arm_v7m_mmu_idx_for_secstate_and_priv` dereferences. `scr_el3` and `aircr` are read only behind `ARM_FEATURE_EL3` / `ARM_FEATURE_M_SECURITY`, so on cortex-m3 they are unreachable; they are listed anyway under the closure rule. The hand re-derivation is a non-cuttable P1 acceptance item; rwsum stays the automated version |
| 3 | major | AX-H broken by `loadvm`/incoming migration; audit framing wrong; `2654` mislabelled; `machine.c:700` missing | `armv7m.c:365-372`; no `MIG_PRI` in `armv7m_nvic.c`/`machine.c`; `machine.c:931, 1021, 700`; `armv7m_nvic.c:2473-2498, 2583, 2654` | §4.2 (domain, restated audit table with each writer's rebuild site, "Snapshot load" paragraph), HS-I13, HS-RUN, §4.10, R10, file map | Chose the domain restriction (refuse `-loadvm`/`-incoming`) as the v1 fix and kept the savevm-in-HardFault fixture as the way to lift it later. The refusal is a launch-discipline row (HS-RUN), not HS-CPU, because the RuntimeIdentity does not record the QEMU command line (it records `normalized_config_sha256`, exe and plugin identity, page bits and TB size). Note that the stale NEGPRI bit after load is already a baseline behaviour; C2 only lengthens it, but that is enough to break equivalence |
| 4 | major | Fixtures can be vacuous | `Q/tcg/tcg.c:6141-6201` (arguments loaded before clobber spills and `save_globals`) | §5.4 (non-vacuity rules, per-fixture HS report and O3, value-loading rules, README), G-HS | Also warns against flag-setting `movs` just before the MSR, since dirty NZ globals can add `save_globals` stores inside the window |
| 5 | major | Schedule unrealistic | — (judgement) | §1.1 (ship order), §7.1 (new plan, ESTIMATE 14-18 weeks, C2 first, MVR line), §7.2 cut order and never-cut list, §7.3, §4.6 item 4 (time box and triage rule), Q1 blocking, R11 | **Goes further:** C2-only needs not even the `secure == 0` closure or any CPU fact, because `v7m_msr(0x811)` writes at most `basepri[0..1]` on every path (`m_helper.c:2566-2576, 2716-2721, 2776-2779`). That is the new row `ws.msr.basepri@1` |
| 6 | minor | D3 flakes; unprivileged row has no compute samples | `cpu.c:1073-1075`; EnvLayout (`irq_line_state` 0xcdc, `interrupt_request` −0x2af0) | §4.3 leg 2 (scheduler-locking, fallback exclusion list, sample source per row), §5.5 D3 row | The unprivileged row is sourced from an F-HS3 loop, or dropped from the release if F-HS3 is not ready |
| 7 | minor | M14 equivalent; F-HS3/F-HS4/F-HS5 cannot observe their mutants | `m_helper.c:2451-2453, 2720, 2729`; `armv7m_nvic.c:643-650` | §5.3 (M3, M4, M14 `static_only`, M16), §5.4 (F-HS2, F-HS3 SVC readback, F-HS4 without a fault, F-HS5 unaligned value) | — |
| 8 | minor | O-HF coverage and cost | arithmetic | §4.2 evidence (b), §5.5 (cost ≈ 210 s per input, fault fixtures), new fixture F-HS12 (HardFault/NMI) | SYSRESETREQ is listed as "if one is added"; no existing fixture exercises it |
| 9 | minor | Census facts wrong | `H/census.md` §0 and §2; `H/compute-sites.json`; `H/census.txt` | §2.2, §3.1, §3.2 (column definition and corrected counts), §1.1/§1.2 (81 %) | The per-unit column is now steady-phase executions (entries − aborted). The old numbers differed by ≤ 0.03 % |
| 10 | minor | §2.3 unwind failure misdescribed | `translate-all.c:175-198, 576-620`; `supercache.c:2065-2077` | §2.3 item 5 | — |
| 11 | minor | Shape A stack alignment; hostsym cannot follow a local call | `tcg-target.c.inc:4735-4755` (§2.1); `paths.py:582-631` | §4.7 rule U9, §7.2 S3 work items | — |
| 12 | minor | Division hazards not recorded; HS-CPU is offline only | `op_helper.c:134-173`; `S/formats/bundle_v1.py:600-617`; `S/validate.py:1136`; `N/map-supercache-format.md` C17/C18 | §8 "If a later student revives division fast paths", §4.2 item 3, HS-CPU row | — |

**Other cheap fixes made in passing:**
- §4.2 AX-H evidence said "all three required" while leg (c) is optional; it now says (a) and (b) are required.
- §4.4.1 referred to an "F2 below" that does not exist; it now cites §2.2.
- §4.10 MMIO row cited "F2/F5 in §4.3"; it now names the row fields.
- P2b's NVIC claim no longer cites the reset-time rebuild (`2654`) as a register-write rebuild.
