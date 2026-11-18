# B2 fixtures F-B2-1, -2, -4, -5, -6 (WP15A)

Design §7. F-B2-3 (fault attribution with faulting units moved ±64 MiB) and F-B2-7
(FreeRTOS timing) are not part of WP15A. Every run uses the integrated WP-QINT
binary; see `RUNTIME.md`.

## How to run it

```sh
PY=/data/extra_copy/fastdyn/tools-env/bin/python
cd <scopt worktree>
for w in minimal fault compute; do          # compute: F-B2-6 only
  $PY tests/qemu/b2_gate.py --workload $w --out runs/wp15a/$w
done
# re-check the evidence from the files
B2_GATE_ROOT=runs/wp15a $PY -m pytest tests/acceptance/test_b2_gate.py \
    tests/acceptance/test_b2_synthetic.py
# the whole driver on minimal and fault into a temporary directory (real QEMU)
SCOPT_B2_GATE_E2E=1 $PY -m pytest tests/acceptance/test_b2_gate.py -k end_to_end
```

`b2_gate.py` follows `b1_gate.py`:
- It runs every step as a `python -m scopt …` subprocess from the worktree. Commands go to `OUT/cmds.jsonl` and output to `OUT/logs/`.
- It checks the files those commands wrote, and opens the snapshots in-process for the independent byte and capstone checks.
- It writes `OUT/b2-gate.json`, with one entry per fixture holding the verdict (`pass`, `fail` or `not_run`), the checks and the evidence paths.
- A driver exception becomes a failed fixture with its traceback.
- No timing is measured.

## Workloads

| workload | base config | phase | notes |
|---|---|---|---|
| `minimal` | `src/fastdyn/configs/b0-sc.toml` | `steady@0x08003972` | 163 units |
| `fault` | `fixtures/fault/fault-fixture.toml.in` rendered as `fdrun.py --arm sc-noicount` into `OUT/configs/fault-fixture-sc.toml` | `steady@0x58` (`fault_bench_start`) | `fault_fixture.elf`, variants (a)-(d), 48 units. No icount, because §9 keeps CF_USE_ICOUNT units out of the F-B2 gates until qualified. Guest output is also compared with `fixtures/fault/expected/fixture.norm`. |
| `compute` | `runs/wp13-r2/compute/configs/compute-sc.toml` | (WP11 steady profile) | F-B2-6 only. Uses a sparse copy of the B1 gate snapshot (train-1; the source is checked unchanged) and held-out input `heldout-1`. |

**Chain (minimal, fault):**
1. `prepare` (capture on);
2. `bundle identity` and `runtime-identity`;
3. `import`;
4. `profile run` with the phase;
5. the fixtures;
6. `report`.

## Fixtures and what is checked

| fixture | steps | checks |
|---|---|---|
| F-B2-1 | `move-units@1` moves the hottest unit to `ROUND_UP(max_end,64)`, then to that + 4 MiB. On minimal this is the hottest relocatable unit by all-phase entries; on fault it is the hottest steady-phase unit. | V1-V8 pass and V9 skip (validation.json rows). An independent capstone diff: changed bytes and instructions only at declared `prologue.*`/`ext.*` fixup fields, external targets unchanged, intra targets shifted by d. Every other unit is byte-identical. Re-validation. V10 (equal outputs, appended 0, proof PASS). `profile run --artifact`: the raw `.qprof` row at the new tb_offset holds the unit's entries, no row sits at the old offset, and the join gives the same UnitId and count as the snapshot profile. `diag exec`: Trace lines at the new `tc.ptr`, none at the old one. |
| F-B2-2 | `reverse-layout@1`, plus `move-units@1` for the mirror's fixed points | Every relocatable unit moves. Every fixup class is applied, and each field is checked as `v - d` or `v + d` (counts; G17 numbers on minimal). Capstone diff of every moved unit. Slot order is kept and the LIFO winner of every key is unchanged. V10 and diag. On the F-B2-4 tampered snapshot the pinned unit is untouched. **Real-file duplicate key:** a relocated copy of the hottest unit is appended as the last slot (UnitId `#1`, no_meta). After reverse-layout, `diag exec` shows that QEMU runs the later slot and never the earlier one, in the baseline and in the artifact. **Synthetic:** `test_b2_synthetic.py::test_f_b2_2_duplicate_key_lifo`. |
| F-B2-4 | One HELPER_CALL site of the hottest unit that has one is dropped from the capture (`tests.synth.builders.drop_site`); `b2-layout` and `--disable cluster-hot` follow. | The unit is pinned with `unexplained_reference` and has the same SnapshotId. `skipped_unsupported:unexplained_reference:<uid>` appears in `affected` (cluster-hot and/or order-cold). The unit is unmoved and byte-identical. Ledger: unit `inspected` = `pinned`, artifact `inspected` = `partial`. Re-validation and V10. |
| F-B2-5 | The hottest unit with an `ext.plugin` fixup goes to `0x60000000`. Then the same move again, plus a second unit placed at the first one's original offset. | First case: the linker's `rel32_range` rollback (2 attempts). The sidecar lists it as pinned `failed:rel32_range`, the unit ledger row reads `compiled fail rel32_range`, the artifact is byte-equal to the input (V9), and V10 passes. Second case: exit 2, rejected with `failed:rel32_range`, the rejection document shows `rel32_range` and `rollback_slot_taken`, the ledger rows read `compiled fail` and `accepted fail`, nothing is published, and the snapshot is unchanged. |
| F-B2-6 | A capture-off warm run of a training snapshot appends records. The post-run `.sc` is imported with the derived capture of the training snapshot's identity artifact, so the appended slots are `no_meta`. | The appended units are `no_meta`. Under `reverse-layout` and `b2-layout` they stay fixed (`skipped_unsupported:no_meta:<uid>` in `affected`) while other units move. A PLACE onto their interval is rolled back with `place_overlap`. A PLACE of the unit itself is refused by the pass manager (`not_relocatable`). Re-validation and V10 (equal outputs, appended 0). The training snapshot on minimal and fault is the snapshot without its last K=2 records, because these workloads have no input bank. On compute it is the B1 snapshot (train-1) with held-out input `heldout-1`. Synthetic case with the no_meta unit in the middle of the packed region: `test_b2_synthetic.py::test_f_b2_6_no_meta_obstacle_in_the_middle`. |
