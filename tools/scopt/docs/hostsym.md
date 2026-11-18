# hostsym: the B3/B4 validator kernel (WP19)

Design: `docs/design/B3-B4-design.md` §9.2 (kernel), §9.3 (mutation suite), §9.1 rows V12, V13
(path-condition part), V14, V18. Code: `scopt/validate_sym/`.

## What it checks

`hostsym` symbolically executes the host bytes of one unit version from offset 0 and
compares the observable traces of an original `O` and a candidate `N`.

| Function | Row | Meaning |
|---|---|---|
| `check_equiv(O, N, P, mu, contract)` | V12 | Same set of symbolic branch signatures under precondition `P`; equal traces per path |
| `check_guarded_chain(R, R', contract)` | V12 + V13 entailment | Same as above, plus the guarded-chain rule for slot exits |
| `certify_fact(R', slot, (off, w))` | V14 | `env[off:off+w]` is the same constant on every path that reaches the slot. With `slot=None` the check uses the lookup exit of an ungrown R |
| `selftest.run_selftest()` | V18 | Native check of the concrete semantics, plus the synthetic mutation suite |

A trace is the ordered list of events of one path:

- `guest`: one per LDST block. It carries `(k, oi, ld/st, address, store value)` and the env.
- `call`: the target, the call class, the six SysV argument registers, the env and the frame.
- `host_store`: a store that is not to env and not inside an LDST block (UPDATE_MEM / LOG_REG).
- `barrier`: TCG's `lock or [rsp],0`.
- `exit`: the exit kind and payload (goto_tb slot, guarded slot, `rax` of exit_tb, goto_ptr target) and the env.

Env snapshots are compared byte by byte. Only the fields that `obs-contract@1` makes observable
at the event are compared:

- call events, by class: lookup at S3; flags-0, plugin, noreturn, unknown targets and helpers without a row in the helper effect table at S4; rowed `NO_RWG` helpers that may fault (`udiv`/`sdiv`) at S6; rowed `NO_RWG` helpers that cannot fault (pure) at P. At every call event the env bytes in the helper's `env_reads` are compared too.
- `guest` events at S5.
- `exit` events at S2, by exit kind.

With no contract every byte is compared. A field the layout cannot name is always compared.

## Semantics

- **Concrete:** mov, movabs, movzx, movsx, movsxd, lea, add, sub, and, or, xor, not, neg, inc, dec, test, cmp, shl/shr/sar by an immediate (32/64-bit), setcc, cmovcc and nop. Flags ZF, SF, CF, OF and PF are modelled; undefined flags are terms. V18 checks every form natively.
- **Uninterpreted:** every other mnemonic is a deterministic function of all its inputs: explicit and implicit registers, immediates, memory values and the flags it reads. Its result is the term `("uf", sig, inputs, slot)`.
- **Unsupported:** the path ends UNSUPPORTED and the verdict is `validation_inconclusive`. This happens for:
  - backward branches and `ret`;
  - locked ops other than the barrier, and string ops;
  - writes to `rbp`/`rsp`;
  - env accessed through a register other than `rbp`;
  - a jump into the middle of an LDST block;
  - a branch leaving the unit other than to `tb_ret`, the epilogue, a goto_tb field, a guarded slot or `jmp reg`;
  - running over the step or path budget (256 paths).

## Abstractions

**LDST block k.** The block starts at its QEMU_LD/ST OpMark and also holds the register
allocator's input loads.
- Its fast path runs normally, except the TLB-miss `jcc`, which is not a branch.
- At the access the kernel emits the `guest` event. The address is the base/index register that does not hold a TLB value. A load gets `("gld", k, oi, addr, seq)`.
- After the block these are havocked: registers written after the `jcc` or in the slow path, the flags, and the env bytes that are not TCG globals or `neg.can_do_io`.
- The block's bytes (fast and slow path, compared modulo relocation) must be identical in `O` and `N`, else `edit_inside_ldst_block`.

**Havoc.** A byte that a havoc does not keep becomes `("hv", epoch, old)`. So a store the other
side deleted stays visible after a call or an LDST, and two sides that agreed still agree.

**Calls.** Caller-saved registers and the flags are havocked, and `rax` gets the term
`("ret", seq, target)`. What happens to env and host memory comes from the **helper effect
table** (`scopt/analysis/helper_effects.py`; tables in `scopt/formats/helper_effects/<exe
sha256>.json`, bound to the QEMU executable like the EnvLayout). Each row gives the helper's
env read and write sets (by EnvLayout field), whether it reads or writes host memory, whether it
may fault, and the QEMU source lines it was taken from. hostsym havocks exactly the row's
`env_writes` and bumps the host-memory version iff `hostmem_writes`:
- `rebuild_hflags_m32[_newel]` (`NO_RWG`): `env->hflags` (the 16-byte `CPUARMTBFlags`), no host memory;
- `udiv`/`sdiv` (`NO_RWG`, may fault): nothing on the returning path;
- `lookup_tb_ptr`: all env except the TCG globals (the lookup sets `can_do_io`);
- flags-0 helpers (`v7m_msr`, `v7m_mrs`, `shl_cc`) and plugin callbacks: all env except `neg.can_do_io` (an over-approximation);
- noreturn helpers: all env.

`TCG_CALL_NO_RWG` only promises that the TCG globals are untouched, so it never makes a call
env-pure by itself. Fail closed: a named helper without a row (or whose TCG flags differ from
the row's), an unknown call target, and every helper when no table is bound to the layout's
executable havoc all env, bump the host-memory version and are compared at S4 with every env
byte.

**`can_do_io` across a returning access or helper (WP19 integration).** In the QEMU source, the
only writers of `cpu->neg.can_do_io` are generated code, `cpu_tb_exec`, `helper_lookup_tb_ptr`,
`cpu_loop_exit` (which longjmps and never returns into the TB), `cpu_reset` and the vCPU thread
start. The MMIO path only reads it (`io_prepare` calls `cpu_io_recompile`, which does not return).
So an LDST block, or a call to a known helper that returns, leaves it unchanged. Before this rule,
every grown return unit whose last guest instruction loads after the `can_do_io = 1` store
(`pop {…, pc}`, `ldr pc, [sp], #4`) gave a false `validation_mismatch` at its guarded slot under
`tb_start_cb_possible`.

**Addresses.** An absolute address inside the unit becomes `("own", rel)`. So a variant's struct
LEAs, pool reads and `raddr` LEAs compare equal to C's (C' ≅ C, relocation).

## Verdicts and reason codes

| Verdict | Reason | Outcome |
|---|---|---|
| equivalent | — | row passes |
| mismatch | `validation_mismatch`, `edit_inside_ldst_block`, `recovery_rows_changed` (search rows, icount or cflags differ), `host_abs_aliases_env`, `guard_contract_violation`, `fact_derivation_mismatch`, `fact_not_constant` | **failed** |
| inconclusive | `validation_inconclusive` (unsupported path or budget) | skipped_unknown |

## Host pointers (§7.5)

Every absolute pointer dereferenced outside an LDST block must lie outside env/CPUState.

- When the env address is known (`env_abs=(lo, hi)`), the effective address is checked against it.
- Otherwise the base pointer must be bound by the importer to `abs.guest_ram`, `abs.plugin_data`, `abs.qemu_data` or `ext.*`. Any other pointer fails closed.

## Linked artifacts (WP19 integration with WP18)

`scopt.validate_sym.link_check` runs the kernel on a `NativeLinkerV2` result (the artifact, not the plan):

- `r_prime(link, uid, world, offsets=…)`: the grown unit as placed. It has one `GSlot` per guard target. The slot's position comes from the record's `jmp_insn_offset[n]`, or from the emitter's slot jump for an unregistered v5 slot. Its **declared** atoms come from the continuation key (C's record key for a linked SPEC slot, else the plan's `GuardedExitSpec` key) and from the ext slot entry's `target_vaddr`, never from the stub bytes.
- `c_prime` / `c_reference`: a variant and what it is compared with. That is the captured C, or the grown C when C is itself grown in the link. NativeLinker v2 clones the effective unit, so such a clone carries C's guard stub.
- `certify_link(snapshot, link, layout=…, contract=…)` runs:
  - `check_guarded_chain` on every grown unit (V12 and V13 entailment);
  - `check_equiv` on every rewritten unit;
  - `check_equiv(C, C', facts ∧ guard atoms)` per variant and per ext link that enters it;
  - `certify_fact(R', n, field)` for every fact of every ext link (V14).

  `only={uid}` restricts it to the given units.
- `hostsym_inputs(...)` is the `hostsym=` callable of `validate_b3` and `package_b3`: `validate_b3` calls it on the link it validates.
- `summary_certifier(snapshot, layout)` is WP20's `Certifier` for `build_callee_summaries`. It certifies a proposed fact on the captured R at that exit's lookup (`certify_fact(…, exit_pc=T+9)`).
- `summary_provenance(rows)` takes V14's provenance from the CalleeSummary fact rows.

`scopt.validate_sym.link_mutants` builds the §9.3 guard and link mutants on a linked artifact:

- `drop_<atom>`: one stub compare replaced by nops (for `g_pc`, `je L_k` becomes `jmp L_k`);
- `guard_before_effect`: R's `jmp S; int3×6` hoisted above each `regs[15]`/`thumb` effect store;
- `guard_stub_store`: an env store in the stub;
- `falsified_fact_link`: a SPEC link whose variant claims a value R does not establish;
- `shared_variant_suffix`: a full return unit's variant also linked from its suffix TB, as a second LinkEntry and as a second copy.

Each mutant has positive controls: the unmutated grown unit and the correct links.

## CLI

```
scopt hostsym selftest [--cache FILE] [--out FILE]
scopt hostsym coverage SNAPSHOT --profile profile.json [--phase steady] [--out FILE]
scopt hostsym check SNAPSHOT UID --patch OFF:HEX ... [--pre OFF:W=VAL ...]
scopt hostsym mutants [SNAPSHOT] [--limit N] [--out FILE]
scopt hostsym link-mutants SNAPSHOT [--limit N] [--out FILE]   # WP18 emitter/placement mutants
```
