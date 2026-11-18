# Return edges, exit facts, provenance and CalleeSummary (B3 WP20)

Design: `design/B3-B4-design.md` §3.1-3.3 and the WP20 implementation note at its end.

## Analyses

| Analysis | Scope | Reads | Value |
|---|---|---|---|
| `exit-facts@1` | unit | `hostops`, `hcfg` | facts `env[field] == value` at each lookup tail, refusals, barriers (the proposer) |
| `patch-join@1` | unit | `exit-facts`, bundle, guest ELF, patch plan | provenance per fact: `patch` / `guest_code` or a reason |
| `return-edges@1` | snapshot | `hostops`, `exit-facts`, bundle, `profile:steady` | one row per `goto_ptr`/`exit_null` exit: class, admission reasons, ranked continuation candidates; suffix refusals; U-COND rows |
| `callee-summary@1` | snapshot | the above + `hcfg` | CalleeSummary v1 rows (`fastdyn.callee_summary`) |

The guest ELF and the patch-plan content are external inputs, resolved by hash
(`scopt.analysis.b3_inputs`): register them before the first `get`, or let the
store directory supply them (recorded firmware path; manifest hashes).

## CLI

```
scopt return-edges analyze   SNAP [--profile P] [--prep DIR | --modifiers F --virtuals F] [--elf F] [--out J] [--brief]
scopt return-edges summaries SNAP [same inputs] --out-dir D [--certify]   # D/summaries/callee_summaries.jsonl
scopt return-edges facts     SNAP [UID…] [--vaddr 0x…]           # proposer only
scopt return-edges xcheck    SNAP [--return-only] [--out J]       # two-sided proposer/hostsym check
scopt return-edges ucond     SNAP… [--elf F…] [--out J]           # T-B3-42; exit 1 on a counterexample
scopt return-edges stability SNAP --train P --heldout Q [-k 2]    # U-HELD (reported outcome)
```

`--prep` takes `work/virtuals/{modifiers,virtuals}.txt` of a `scopt prepare` directory;
every file must hash to the manifest's content sha256.

`--certify` checks every proposed fact with WP19's V14 certifier
(`scopt.validate_sym.link_check.summary_certifier`, hostsym on the captured R at the
lookup exit `T + 9` of the fact's tail). A certified fact gets `status: certified` and its
`cert_hash`. A disagreement is refused as `fact_derivation_mismatch` (a tool bug, §3.2)
and makes the exit status 1. An inconclusive exploration is refused as
`validation_inconclusive`. The summary JSON gains `certifier` (tally) and `status` counts.

`xcheck` (`scopt/analysis/fact_xcheck.py`) asks the other direction too. For every
admissible lookup tail of every unit the proposer analyses, and for **every** fact field
(r0..r12, NF, ZF, CF, VF), it compares the proposer's verdict with the constant hostsym
derives on its own (one exploration per unit, `certify_fact`'s rule at `T + 9`). Outcomes:
`agree_const`, `agree_nonconst` (proposer refused or did not track the field and hostsym
finds no constant), `inconclusive` (proposed, hostsym inconclusive or exit unreached),
`proposer_weaker` (hostsym derives a constant the proposer refused: sound, reported) and
`fact_derivation_mismatch` (proposed constant that hostsym contradicts; exit status 1).

### Integration on the WP24 fixtures

```
python tests/qemu/wp20_fixture_snapshots.py --out FX [--modes icount,noicount] [--configs all] [--profile] [-j 4]
python -m tests.acceptance.wp20_fixtures FX --out EVAL     # EVAL/eval.json, per-snapshot summaries + xcheck
WP20_FX_ROOT=FX pytest tests/acceptance/test_wp20_fixtures.py
```

The driver renders each `fixtures/b3/b3-<cfg>.toml.in` with `fixtures/lib/fdrun.py`
(`sc` = icount shift 0, `sc-noicount`), then `scopt prepare` (capture-on, current
`wt/qemu-int` binary by default), `bundle identity`, `bundle runtime-identity`, `import`,
and optionally a steady-phase profile (`steady@b3_bench_start`). Nothing is timed. The
evaluator checks the plan's WP20 integration criteria: F-B3-1/1p/2/3/3b/4/8 `r0 == 0`
`source = patch` certified on every return unit of the patched instruction; F-B3-6 `f5`
gets no `r0 == 0` and no patch fact; F-B3-5 `fact_killed_by_helper`; F-B3-20 suffix units
`fact_not_local_to_return_tb` (with the count present); certifier and `xcheck` mismatch 0
on every snapshot.

## Reason codes produced

Exit admission: `exit_not_goto_ptr`, `tail_is_branch_target`, `indirect_multiway`,
`handler_mode_return`, `pc_modifier_exit`, `special_cflags_unit`, `no_meta`, `ram_resident`,
`unit_not_relocatable`, `no_free_slot`, `target_not_profiled`.
Continuations: `continuation_key_mismatch`, `two_page_target`, `continuation_not_in_snapshot`
(+ the unit codes).
Facts: `fact_not_constant`, `fact_killed_by_helper`, `fact_not_local_to_return_tb`,
`patch_join_mismatch`, `provenance_unexplained`, `unsupported_contract_fact_source`,
`no_patch_bindings`; with a certifier: `fact_derivation_mismatch`, `validation_inconclusive`.
