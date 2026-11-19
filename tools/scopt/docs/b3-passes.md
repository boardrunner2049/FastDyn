# B3 passes and the B3 manager (WP21)

Design: `docs/design/B3-B4-design.md` §3.4-3.9, §10.3, and the WP21 implementation note at its
end. Plan: `docs/design/IMPLEMENTATION-PLAN-B3B4.md` `## WP21`.

## Pipeline

`pipelines/b3.toml` (allowed: CLONE, REDIRECT_EXIT, ADD_ENTRY, ATTACH_FACT, PURE_REWRITE):

| # | Pass | Scope | Edits | Reads |
|---|---|---|---|---|
| 1 | `specialize-returns@1` | snapshot | CLONE C->C', REDIRECT_EXIT (SPEC), ADD_ENTRY, ATTACH_FACT | `return-edges`, certified CalleeSummary facts, `hconst`, profile (steady) |
| 2 | `constant-propagation@1` | region (variant) | PURE_REWRITE `cp_xor_zero`, `cp_mov_imm` | `hconst`, `hostops`, `hlive` (EFLAGS dead) |
| 3 | `branch-folding@1` | region | PURE_REWRITE `bf_jcc_to_jmp`, `bf_jcc_to_nop` | `hconst`, `hostops`, `hcfg` |
| 4 | `dead-host-ops@1` | region | PURE_REWRITE `dho_nop` | `hostops`, `hcfg`, `hconst`, `hlive` |
| 5 | `chain-returns@1` | snapshot | REDIRECT_EXIT (GENERAL) on return exits | `return-edges`, profile |
| 6 | `chain-static@1` | snapshot | REDIRECT_EXIT (GENERAL) on static off-page exits | same |

Arms (§10.1) are ablations of this file: `b3-chain` = `--only chain-returns`, `b3-static` =
`--only chain-static`, `b3-spec` = `--disable chain-returns --disable chain-static`.
Pass 1 claims slots before 5/6, so ablations are not additive.

## Running

```
scopt optimize SNAP --pipeline b3 --store STORE --profile steady.profile.json \
    --prep PREPDIR [--elf FIRMWARE] --runtime-identity RI_V6.json [--v6-exe SHA] \
    [--selftest hostsym_selftest.json] [--disable PASS] [--only PASS] [--json]
```

A pipeline that allows a B3 kind (or `--b3`) runs `scopt.manager_b3.optimize_b3`:

1. `PassManager(..., b3=B3Env(...))` runs the passes. B3/B4 edits go to the
   `B3State` overlay (the B2 `WorkingSet` refuses B3 kinds) after the §3.7 checks.
2. `materialize` builds WP18's `B3Plan`: patches composed per original host instruction,
   the variant id `sha256(C.uid, R.uid, E, sorted(F), patch set)`, the **downgrade rule**
   (a variant whose bytes equal C becomes a GENERAL slot: `no_simplification`), and one
   `ADD_REGION` record per SPEC link.
3. `package_b3` (WP18) links (NativeLinker v2), writes `.sc` v6, and validates V1-V23 with
   WP19's hostsym (`hostsym_inputs`, provenance from the certified CalleeSummary rows).
   Publication needs V12, V14 and V18 to certify (fail closed). The sidecar carries
   `x_b3_manager` (per-pass byte delta, edits, origins, deps added, downgrades, regions);
   the ledger carries the pass rows with `counts`, one `no_simplification` row per
   downgrade and one `ADD_REGION` row per SPEC link.

A candidate without any B3/B4 edit (identity pipeline, every pass `no_change` or skipped,
all passes disabled) is packaged by the B1/B2 `ValidateAndPackage` as a v5 file, so V9 byte
identity applies unchanged (T-B3-7).

Inputs: the guest ELF and the patch-plan content are resolved by hash
(`scopt.analysis.b3_inputs`; `--prep` / `--elf`). Facts are certified by WP19's V14
certifier on the captured R (`B3Ctx.summaries()`); without a bound EnvLayout no fact is
citable and no guard can be emitted (`guard_layout_unbound`).

## Manager rules (B3 mode)

* Region scope (`unit_scope = "region"`) is accepted; such a pass runs once per variant on
  a `VariantView` (C's metadata, the variant's current bytes, its facts and train weight).
  With no variant it returns `no_change` (`no_variants`).
* Preconditions checked on each proposal (refusal: the proposal is dropped, the pass row
  gets the code and `B3Reason.outcome(code)`; `edit_precondition`/`stale_fact` are
  `failed`):
  * REDIRECT_EXIT: lookup tail at `tail_off`, `tail_problem` clear, the `return-edges` row
    admitted and of the declared class, the slot free in the record and unclaimed,
    `Vc` an admitted candidate whose unit has key `key_C`, one guard key per exit,
    EnvLayout bound (`guard_layout_unbound`), R not a variant source; SPEC needs a CLONE of
    C and an ADD_ENTRY in the same proposal, and `spec_binding_problem` clear.
  * CLONE: source relocatable, `meta ok`, not RAM-resident, **not grown** (see the
    implementation note: a clone of a grown C would carry its registered GENERAL reset);
    every cited fact attached to the predecessor in the same proposal; budgets
    `context_budget` (2 per C, 64 per artifact) and `growth_budget` (variants + SPEC stubs
    ≤ 10 % of the 99 %-coverage hot-set bytes). `specialize-returns` applies the same
    growth budget itself, in rank order (train weight): a variant that does not fit is
    dropped and counted as `growth_budget`, the rest are proposed. The manager's check of
    the whole proposal stays as the backstop (before WP21's integration stage it was the
    only check, so one oversized proposal lost every variant: F-B3-6).
  * ADD_ENTRY: exactly one per variant, for a variant of this proposal.
  * ATTACH_FACT: the FactRef cites a current certified CalleeSummary row (`stale_fact`).
  * PURE_REWRITE: old bytes equal the **current** bytes (edits on clones compose); whole
    instructions; inside one guest insn's `insn` block (`edit_in_prefix` except the B4 D2
    store, `edit_inside_ldst_block`, `edit_in_exit_stub`, `edit_on_insn_boundary`); no
    fixup field or lookup tail; the template belongs to the pass (builder check).
* Linker/packaging rejections are classified by `b3_reject_outcome`: §3.8 classes, plus
  `LINK_FAILED_CODES` (`link_missing`, `variant_pred_slot`, `slot_kind`, …) as `failed`
  (an internally inconsistent plan is a tool bug, spec:47).

## Refusal codes per pass

| Pass | Codes it reports (most frequent becomes the pass reason when nothing is proposed) |
|---|---|
| specialize-returns | the exit's §3.1 code, `fact_not_constant` (or the summary row's first refusal), `target_not_profiled`, `fact_unused`, `not_profitable`, `unit_not_relocatable`, `slot_budget`, `context_budget`, `growth_budget` |
| constant-propagation | `no_same_length_encoding`, `flags_live`, `not_profitable` |
| branch-folding | `fact_unused` (no decided branch), `no_same_length_encoding`, `not_profitable` |
| dead-host-ops | `flags_live`, `not_profitable` |
| chain-returns / chain-static | the exit's §3.1 code, `no_free_slot`, `unit_not_relocatable`, `continuation_key_mismatch`, `not_profitable`, `slot_budget` (256 units) |

`tbb`, `ldr pc,[rX]` (non-SP base), `bx rX`, `blx rX` exits are `indirect_multiway` (§3.1
rule 2) and are never guarded in v1 (deferred; candidate `chain-indirect`, §14).

## Profitability (§10.3)

`scopt/analysis/profitability.py`. Exit: `Σ n_i·c_lookup − N_E·c_guard − (N_E − Σ n_i)·c_fail
> 0` and guard pass share ≥ 50 % (WP16 calibration: 7.6 / 1.5 / 0 ns). CP/BF/DHO: work units
removed × C' entries > 0 (one per folded env load, decided branch, nopped instruction). B4:
removed stores × unit entries; `b4_predicted_unprofitable` flags < 0.5 % of train BENCH.

The GENERAL chaining passes reserve up to one main-loop link installation per
target per recorded training replay. Their aggregate profile cannot show
whether every target appeared in every replay. They model only remaining hits
as avoided lookups and reject targets with zero estimated reuse. A new guard
charges every lookup attempt at the exit, including traffic to unmatched
targets. Added targets must improve the estimate when the exit already has a
guard. The SPEC cost model uses its own arming path.

Pass parameters `c_link` and `min_target_hits` default to zero. Set `c_link`
to a measured nonnegative cost in nanoseconds per GENERAL installation.
`min_target_hits` can require a higher aggregate exact count. The ledger
records observed target hits, estimated avoided lookups, estimated installs,
and total guard attempts. These estimates guide selection for measurement.

## Writing a semantic (region) pass

A region pass gets one variant at a time and proposes same-length patches on it:

```python
from typing import Any, ClassVar
from scopt.analysis.api import ScopeRef
from scopt.analysis.hostops import OpKind
from scopt.passes.base import Budgets, EditKind, Outcome, PassDescriptor, PassResult, Patch
from scopt.passes._b3util import nops, refusal_result


class NopDeadMovs:                       # example only: register it in PASSES of its module
    descriptor: ClassVar[PassDescriptor] = PassDescriptor(
        name="dead-host-ops", version=2,  # the template must be allowed for the pass name
        unit_scope="region", requires=(("hostops", 1), ("hcfg", 1), ("hlive", 1)),
        capabilities=frozenset({EditKind.PURE_REWRITE}), budgets=Budgets(max_wall_s=60))

    def run(self, vv: Any, ctx: Any, edits: Any) -> PassResult:
        ctx.analysis("hostops", ScopeRef("unit", vv.src.uid))   # record what you read
        h, g, live, hc = vv.analyses()     # hostops/hcfg/hlive/hconst of the CURRENT bytes
        if not hc.ok or live is None:
            return PassResult(Outcome.SKIPPED_UNKNOWN, "validation_inconclusive")
        out = []
        for i, o in enumerate(h.ops):
            if o.block == "insn" and o.kind in (OpKind.COPY, OpKind.CONST) and \
                    i in hc.reachable and live.dead_after(i):     # reg<-reg / reg<-imm only
                old = bytes(vv.code[o.off:o.end])
                out.append(Patch(vv.variant_id, o.off, old, nops(len(old)), "dho_nop"))
        for p in out:
            edits.pure_rewrite(p)          # old_bytes = the variant's current bytes
        if out:
            return PassResult(Outcome.CANDIDATE, "", f"{len(out)} movs", affected=(vv.src.uid,))
        return PassResult(Outcome.NO_CHANGE, "nothing_dead")
```

The manager checks placement, composes the patches with earlier passes' patches on the same
variant, and hostsym (V12) proves `C' ≡ C` under `facts ∧ guard` at packaging. Facts in
`vv.facts` hold at C''s first instruction (the entry check writes only `neg.*`); `hconst`
carries them until a kill (store to the field, call without `NO_WG`, plugin callback,
unclassified store, exit).

## Tests

`tests/unit/test_wp21_b3_passes.py`: hconst (F-B3-1 C, kill rules, flag model), the §3.5
expected bytes on F-B3-1, T-B3-6 static separation (CP/BF/DHO disabled in turn; all three ->
`no_simplification`), the F-B3-1 package (V1-V21, V23), T-B3-7 (four identity cases,
sha256-equal), T-B3-41 (CP `no_same_length_encoding`, BF `fact_unused`, DHO `flags_live`,
chain-returns `target_not_profiled`, chain-static `no_free_slot`, specialize-returns
refusals), manager preconditions and composition, outcome classes.

`tests/unit/test_wp21_growth.py`: the rank-order growth admission of `specialize-returns`
(fakes; the exact F-B3-1 budget boundary; F-B3-6 on the gate-binary base keeps one of two
variants under the default budget and both with `growth_fraction = 1.0`).

`tests/qemu/wp21_integ.py` (integration, gate binary; evidence in the maintainer's evaluation data (not in the repository)): F-B3-1
bases prepared and profiled on the frozen gate binary with their real RuntimeIdentity v2;
`scopt optimize --b3` for the full pipeline and the CP/BF/DHO ablations, with the variant
bytes checked independently from the output `cache.sc` (capstone diff of the variant
against the general C, relocations told apart from patches); T-B3-7 on the same bases;
`scopt publish` of every artifact; supplementary `scopt validate --dynamic --oracles` (O1,
O2a, O2b, O3, O5) and F-B3-6 (the `bx r3` unit stays unguarded).
