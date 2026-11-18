# B2 layout: analyses, passes, pipeline (WP11)

Design: `design/B1-design.md` §4.7, §5.7, §5.9, §5.10, §7 and §9. Code:

| piece | module |
|---|---|
| `hotness@1` analysis and the classification rule | `scopt/analysis/hotness.py` |
| `locality@1` analysis and the static locality **model** | `scopt/analysis/locality.py` |
| `hotcold-partition@1` (ANNOTATE) | `scopt/passes/hotcold.py` |
| `cluster-hot@1` (LAYOUT_ORDER, Pettis-Hansen) | `scopt/passes/cluster.py` |
| `order-cold@1` (LAYOUT_ORDER) | `scopt/passes/order_cold.py` |
| pipeline | `pipelines/b2-layout.toml` |
| real-data driver | `tests/qemu/wp11_b2_real.py` (b2-layout + ablations, V1-V10, report/ledger check, two-profile check) |
| real pinned-unit driver | `tests/qemu/wp11_pinned_real.py` (a real hot unit pinned by `unexplained_reference`, then the same) |

```
scopt optimize STORE/snapshots/S --pipeline b2-layout --profile P.json --store STORE \
               [--disable hotcold-partition|cluster-hot|order-cold]
```

## Classification (`hotcold-partition`, `hotness`)

Per unit, from the counts of the profile that has the phase (phase `steady`
by default, plus that profile's `boot` phase; see "Several profiles" below):

| class | rule |
|---|---|
| `hot` | exact, in the hottest prefix (`(-entries, UnitId)` order) covering `coverage` (default 0.99) of the phase's **exact** entries; never a zero |
| `warm` | exact > 0 outside the prefix, or `lower_bound` > 0 |
| `boot_only` | exact 0 in the phase, boot entries > 0 (`exact` **or `lower_bound`**) |
| `cold_zero` | exact 0 in the phase, no boot execution |
| `unknown` | `unknown` status (not collected, not in domain, phase not reached), or `lower_bound` 0 |

- Only an exact zero is ever cold (§9 "unknown is never treated as cold").
- `lower_bound` is priority only (§9): it never enters the hot prefix (its
  total is not known) and never makes a unit cold. A positive lower bound is
  a proven execution and raises the unit to `warm` (placed right after
  `hot`). The §5.10 example code puts every non-exact unit in `unknown`; this
  is the one difference, and it only moves such units earlier.
- `boot_only` also accepts a boot `lower_bound` > 0 (the §5.10 example
  requires an exact boot count; there a unit with an exact steady 0 and a
  boot lower bound > 0 would be `cold_zero`). A positive lower bound is a
  proven boot execution, so `boot_only` is the accurate class. The steady
  count must still be an exact 0, so this never makes a non-exact unit cold,
  and it only swaps that unit from the `cold` section into the `boot`
  section placed just before it. Listed as a deviation.
- A phase with no exact count at all gives SKIPPED_UNKNOWN `no_exact_counts`.
- Every unit is annotated, pinned ones included (annotations are metadata).

## Clustering (`cluster-hot`)

Pettis-Hansen bottom-up chain merge over the movable hot units:

- weight of `(src, dst)` = Σ exact, **unflagged** `chain0` + `chain1` +
  `lookup_hit` edge counts of the phase (an `irq`/`exception`/`longjmp` edge is
  not a transfer the code makes; `first`, `exit_*` and `lookup_miss` are not
  placement edges); self-loops are ignored;
- edges are visited by `(-weight, -chain weight, src, dst)`: heaviest first,
  then the more fallthrough-like (direct-chain) one, then UnitId;
- an edge joins the chain *ending* in `src` to the chain *starting* with `dst`;
- chains are ordered hottest first (`-Σ exact entries`, then first UnitId).

This is the directed tail-to-head form of Pettis-Hansen *basic-block*
positioning applied to whole TBs. There is no undirected merge, and no chain
is reversed to make an edge adjacent: a TB transfer is directed, and only
`src` directly before `dst` turns a transfer into a short jump. Ordering by
total chain heat is a choice the design leaves open. On the minimal
workload this order makes the 50 % span and the mean edge distance worse
(see the WP11 result); that is reported as it is.

The unit test `test_pettis_hansen_hand_computed` checks a 6-unit graph
against a hand-computed order (including a flagged edge that would change it,
a weight tie decided by the chain weight and a skipped non-head merge).

## Sections (`order-cold`) and placement

`order-cold` emits `warm`, `unknown`, `boot`, `cold` in **original slot
order**; units without a `hotness` annotation go to `unknown`. With
`hot_fallback = true` (default) hot units that no earlier pass laid out go
to `hot` in slot order. NativeLinker (WP10, §5.7) places pinned and
non-mentioned units first as fixed obstacles, then the sections hot → warm →
unknown → boot → cold from `ROUND_UP(prologue_end, 64)`; records keep their
original slot order (§4.7).

Pinned, `no_meta` and `incomplete` units are never in an order request.
They are reported in the pass record's `affected` (design §7 F-B2-4), one
entry per reason:

```
skipped_unsupported:<reason>:<UnitId>      e.g. skipped_unsupported:unexplained_reference:tb:…#0
                                                skipped_unsupported:no_meta:tb:…#0
```

`<reason>` is the pin reason without its `@<site>` detail, and `meta_<status>`
becomes `<status>` (`no_meta`, `incomplete`). `cluster-hot` reports the hot
ones (`detail` summarises the counts per reason); `order-cold` reports every
one it would otherwise have ordered (hot ones too while `hot_fallback` is
on, so `--disable cluster-hot` still names them), and counts them in `stats`
as well. A hot unit that an earlier pass already laid out is
`skipped_laid_out:<UnitId>`.

## Several profiles

`scopt optimize` accepts several `--profile`s. `profile.counts@1` is computed
from the *first* attached profile, but edges (`cluster-hot`) and the sidecar
locality model come from the first profile that **has the phase**
(reached preferred, `profile_for_phase`). B2 therefore reads everything
through `scopt.analysis.hotness.phase_profile_counts(ctx, scope, phase)`:
the counts, boot counts and edges of `hotcold-partition`, `cluster-hot`,
`hotness@1`, `locality@1` and `x_locality` all come from `ctx.profile(phase)`,
whatever the order of the `--profile` options (a boot-only profile given
first no longer makes `hotcold-partition` report `no_exact_counts`).

## Ablation

All three passes can be disabled; each gives a published candidate with
V1-V8 pass and V9 pass (0 moved) or skip (units moved) (synthetic test and the real runs below):

| `--disable` | candidate | meaningful? |
|---|---|---|
| `cluster-hot` | hot units in slot order at the front, then the same sections | yes: the partition without clustering |
| `order-cold` | clustered hot units placed first-fit from the floor around every other unit, which stays at its original offset | yes, but it measures "clustering without segregation": hot units fill the holes they and each other left |
| `hotcold-partition` | nothing is classified, both layout passes return SKIPPED_UNKNOWN `missing_hotness`, the candidate is the input layout (0 moved; V9 identity passes) | **no** (degenerate): the two layout passes are defined over its classes. It is accepted so that a scripted ablation loop does not fail, and reported as 0 moved |

`--only` works the same way (`--only hotcold-partition` = 0 moved).

## Locality model (labelled MODEL everywhere)

`locality_model` combines a layout's arena offsets with the phase's entry
counts. For 50 / 90 / 99 % of the **exact** entries (hottest units first):
units, hot bytes (`[tb, end)` and code only), distinct 64 B lines and 4 KiB
pages, span; plus the executed set, and for every exact unflagged
`chain0`/`chain1`/`lookup_hit` edge between two units of the snapshot (hot or
not) the weighted mean distance, adjacent share and same-page share. Units with
`lower_bound` (observed entries) and `unknown` (by reason) counts are reported
separately and never added to the exact total.

Because B2 moves whole TBs by multiples of 64 and every TB starts on a 64 B
line, **64 B line counts cannot change in B2**; only pages, span and the edge
metrics can. The rows are reported anyway.

Where it appears:

- `ValidateAndPackage(..., profiles=...)` (and `scopt optimize --profile`)
  writes sidecar `x_locality` = `{label, phase, input, candidate, delta}`;
  without a profile there is no `x_locality`; if computing it raises, the
  artifact is still packaged with `x_locality = {status: not_run, reason:
  "error:..."}` (a report model never fails an optimize);
- `scopt report`: section "Static locality model per artifact (MODEL, not a
  measurement)", next to WP13's "Hot-set locality (model)" (90 %, per `.sc`);
- `locality@1` analysis: the model of the WorkingSet-aware `layout()` (input
  offsets plus explicit PLACE; section orders become offsets only in the
  linker, so the candidate's model is computed from the LinkResult).

## Ledger with several candidates

`b2-layout` and its ablations are several candidates of one snapshot. Each
artifact's `ledger.jsonl` is self-contained (its pass rows, the `inspected`
rows, `compiled`, `accepted`). `scopt report` keeps them apart:

- **Pass rows** (`subject: pass`, id `<pass>@<version>`) carry no candidate
  id in the §5.9 row format (the PassManager writes them before the candidate
  exists). The report scopes each pass row to its candidate: the artifact
  whose directory holds the ledger, or else the candidate of the next
  `artifact` row in the same file (a rejected candidate, a merged ledger),
  else `unscoped`. The ledger table shows one row per (pass, candidate), id
  `<pass>@<version> @ <artifact id>`, and a "Pass outcomes per candidate"
  table (one row per candidate with its pipeline and `--disable`, one column
  per pass, `verdict (reason)`). The result does not depend on the order in
  which the optimizes ran. A cell that still holds several rows lists every
  verdict with its count (`a ×2, b ×1 (latest b)`), never only the latest.
- **Unit rows**: `inspected` counts once per (unit, input snapshot); every
  other unit stage is a sum over the ledger files and is labelled as such,
  with a per-ledger-file table (`units_by_source`) next to it.

No ledger row format changed.
