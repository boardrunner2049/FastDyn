# B3/B4 linker, `.sc` v6 and structural V-rows (WP18)

Design: `design/B3-B4-design.md` §2, §3.4, §3.7, §3.9, §4.1, §5.2-5.4, §9.1 and the
WP18 implementation note at its end. Plan: `design/IMPLEMENTATION-PLAN-B3B4.md` `## WP18`.

## Modules

| Module | Content |
|---|---|
| `scopt/guard.py` | canonical guard template `guard-v1` (§4.1): `emit_guard`, strict `decode_guard`, `guard_key` (Kf/Kf2, THUMB/CONDEXEC/SS rules), `guard_atoms`, `GuardOffsets.from_layout(EnvLayout)` |
| `scopt/link.py` (v2 part) | `lookup_tails`, `tail_problem`, `exit_class_of`, `free_slots`, `grow_unit`, `apply_pure_rewrite`, `rewrite_unit`, `B3Plan`, `VariantSpec`, `NativeLinkerV2`, `derive_scm_v2` |
| `scopt/formats/sc_v6.py` | `write_sc6`, `read_sc6`, `ext_problem` (the loader's checks and messages, in its order), `ExtTable`/`VariantEntry`/`SlotEntry`/`LinkEntry`, `sc6_snapshot_id`, `sc6parse_compare` (V11) |
| `notes/critic-scripts/sc6parse.py` | independent parser written from the C loader (`wt/qemu-v6` `tcg/supercache.c`); stdlib only |
| `scopt/formats/scm_v1.py` | TB chunk v2 (`stub_start` in the low u32 of `reserved`), chunk 9 `VARIANT` (Python-only) |
| `scopt/formats/region_v1.py` | `guarded_slot` exits of grown units, `build_multi_tb_region` |
| `scopt/formats/schemas/cache_region.v1.json` | multi_tb branch: `native.members`, typed `GuardAtom`/`FactRef`, `state_fixups: []` |
| `scopt/graph.py` | `guarded_slot` and `variant_link` edges, `b3_edges` |
| `scopt/passes/base.py`, `scopt/proposal.py` | typed payloads `GuardedSlot`, `Clone`, `SpecEntry`, `FactRef`, `Patch`; `B3Reason`; typed builder methods |
| `scopt/validate.py` | `V_ROWS` registry; `validate_b3` (V1-V9 relaxed, V7 v6 clause, V9', V11, V13 structural, V15, V16, V17, V19, V21, V23; V20 via `v20_rows`; skip rows for V12/V14/V18 (WP19/WP20) and V22 (WP23)); `multi_tb_rows`; `package_b3` |

## Using it

```python
from scopt.link import B3Plan, GuardedExitSpec, VariantSpec, NativeLinkerV2, lookup_tails
from scopt.guard import GuardTarget, GuardOffsets
from scopt.validate import package_b3

t = lookup_tails(R)[0]
spec = GuardedExitSpec(t.off, "return", (GuardTarget(vc, 0, "SPEC"),), C.key.flags, C.key.cs_base)
plan = B3Plan(grow={R.uid: (spec,)}, variants=(VariantSpec(C.uid, R.uid, 0, patches=...),))
r = package_b3(snapshot, plan, store, candidate_id=..., runtime=v6_identity)
```

* `register_slots=False` gives a **v5** artifact: the stubs are emitted, the slots
  are not registered and every reset path is `jmp F` (stock `tb_add_jump` would
  patch `tc.ptr + 0xffff` for an unregistered slot, so `exit_tb(R|n)` is never
  used there).
* `force_v6=True` with an empty plan gives the identity-v6 artifact (V9').
* V7 accepts a v6 artifact only when the RuntimeIdentity's exe sha256 is v6-capable:
  `v6_exes=` / `$SCOPT_V6_EXES` / `runs/wp22/gate-binary.json` (`exe_sha256`).
* A SPEC slot is bound to the key of the C whose clone it enters: the linker refuses
  `continuation_key_mismatch` unless `guard_key(spec) == guard_key(C.flags, C.cs_base)`
  and `Vc` is one of C's virtual addresses (`spec_binding_problem`), and V13
  `spec_key_binding` re-checks every ext link from the written guard bytes and C's
  record (independent of the plan). `Vc` is compared exactly (bit 0 set is refused).
  `guard_key` refuses PSTATE__SS, SS_ACTIVE and the M-profile FP/MVE cs_base bits
  (LSPACT, NEW_FP_CTXT_NEEDED, FPCCR_S_WRONG, MVE_NO_PRED; `CS_M_FP_BITS`), which the
  guard does not compare and which cortex-m3 never sets.
* `package_b3` is fail-closed: an edited plan is published only when V12, V14 and V18
  passed (`CERTIFYING_ROWS`). They run when the caller passes
  `hostsym={"equiv": [(label, Verdict)], "entailment": [...], "facts": [...],
  "selftest": doc}` (WP19 bodies `v12_rows`, `v13_entailment_rows`, `v14_rows`,
  `v18_rows`); without it they are `not_run` and the plan is rejected
  `certification_not_run`. V14's vacuous "no SPEC link" skip counts only when the link
  has no variant; V12's "no changed unit" skip never certifies an edited plan. Structural
  tests pass `allow_uncertified=True`, and the sidecar lists the rows in `x_uncertified`.
  A table the loader would refuse is rejected as `failed:<ext_problem code>` (e.g.
  `failed:link_page_offset`); a multi_tb row that cannot be built is
  `failed:multi_tb_region`. `skip_reimport` is refused by `package_b3` (ValueError). V20
  fails (`guard_layout_unbound`) for B3 edits without a RuntimeIdentity.
* `validate_b3` on a file the loader refuses emits fail rows (V11 with the loader's
  reason; V13 `spec_key_binding` fails without indexing the refused table) and never
  raises.
* Import: a version-6 `.sc` whose extension is present but refused is rejection
  `sc6_extension`; a version-6 header without the extension (feature EXT_TABLE clear)
  keeps B1-9's stable reason `version`.

## Tests and evidence

`tests/unit/test_sc_v6.py` (T-B3-1), `test_link_v2.py` (guard, growth, payloads, V-rows,
T-B3-2, T-B3-3), `test_region_multi_tb.py`, `test_graph_b3.py` (T-B3-28);
`runs/wp18/run_acceptance.py` (full sweeps), `runs/wp18/integ_v10.py` (v5 grown units V10 on
minimal and fault, qint binary).
