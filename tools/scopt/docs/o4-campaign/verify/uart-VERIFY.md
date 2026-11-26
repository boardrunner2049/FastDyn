# uart: scopt O4 arms, built and gated (O4-design 3.1 P0-P2, 1.7, 3.2)

Date 2026-11-26. Nothing was timed. Every arm in `/data/scfix-o4/design/commands.txt` for uart
was built except `o4_tasks` (deferred, design 2.4). Every build passed G0 and its validity gate.

* P0 rebuild check: c7b3, fl3 and the probe's hl, rebuilt at fd-int `o4` (ece9b5e), are
  byte-identical to round 2 and the probe.
* P1 identity: O4-r1, built with `-O4`, is byte-identical to c7b3 (`ae360ff7…abd378`).
* P2: 16 arms built (15 from commands.txt plus `o4_pinned`), all rc 0. All have rc 0 on the
  functional check, with the 16 oracle words and 1 BENCH line in each of the 4 runs per arm.
* Arms with the same bytes as o4 on uart: `o4_legacy` (predicted), `o4_rank` (not predicted)
  and `o4_pinned`.

Machine-readable results:
* `/data/scfix-o4/uart/arms-built.json`: arm → cache, sha256, same_bytes_as, gates.
* `/data/scfix-o4/uart/gates.json`: every gate and the regions replays.
* `/data/scfix-o4/uart/receipts.json`: receipts per build.
* `/data/scfix-o4/uart-w/fcheck/<arm>/result.json`: the functional checks.

## Setup

| Item | Value (source) |
|---|---|
| Code | fd-int `/data/fastdyn-work/fd-int`, branch `o4`, HEAD `ece9b5e`. `git status --porcelain --ignored` was empty at the end: no untracked files and no `.pyc`. No commit was made, because this step changed no code |
| QEMU | `/data/qemu-scfix-bin/aacca05/qemu-system-arm`, sha256 `951775b0…eebc60`, the same at the end (inode 87162887) |
| Env | `/data/scfix-o4/uart-w/env.sh`, unchanged. It sets `PYTHONPATH` to fd-int, `PYTHONDONTWRITEBYTECODE=1`, and unsets `SCOPT_*_EXES` and `SCOPT_PIPELINES_DIR`. Python runs with `-B` |
| Builds | `/data/scfix-o4/uart/build.sh`: `nice -n 10 taskset -c 21-31 /usr/bin/time -v timeout 3600 $PY -B -m scopt optimize …`. Each build directory has `command.txt`, `optimize.json`, `optimize.log`, `time-v.txt` and `wall.log` (rc, wall s). P0/P1 ran 4 light builds at once (`p01.sh` and the o4 build). In P2 the 2 heavy builds ran together, with at most 2 light builds beside them (`p2.sh`) |
| Arm lines | `/data/scfix-o4/uart/arm_cmds.py` → `arm_cmds.json`: the uart lines of commands.txt (sha256 in the file), run by `run_arm.sh` |
| Functional check | `/data/scfix-o4/uart/fcheck.py`, adapted from round 2's `/data/scfix-r2/uart/fcheck.py`. It stages with the O4 driver (`/data/scfix-o4/driver/compare_rtos.py`: `stage()`, `header()`, `runtime_identity_check()`). Runs were under `nice -n 10 taskset -c 21-31` |
| Arm launches | `arms/<arm>/launch.json` for each built arm was derived by `o4lib.derive_arm_launch` from the private c7b3 launch, which is what `make_plan.py` does. Every derived launch passed `check_launch_paths` |

## P0: rebuild check (design 1.7 step 1)

The builds are in `/data/scfix-o4/uart-w/rebuild/<name>` (`p01.sh`). The commands are round 2's
c7b3 and fl3 lines (`/data/scfix-r2/uart-w/arms/{c7b3,fl3}/command.txt`) and the probe's hl line
(`/data/scfix-o4/design/probe/out/uart-hl/command.txt`, pipeline `probe/pipelines/hl-b3.toml`).
Only the paths changed, and they point at the private inputs: the probe's uart profile, ELF,
runtime.json and prep are byte-identical to `uart-w/in`.

| Build | rc / wall s / peak RSS KiB | cache.sc sha256 | Reference | `cmp` |
|---|---|---|---|---|
| c7b3 (`sealed-pgo-7-b3`) | 0 / 123.6 / 1,399,024 | `ae360ff7…abd378` | round 2 `ae360ff7…abd378` | equal |
| fl3 (`sealed-firmlooper-v3`) | 0 / 22.4 / 545,276 | `57b7424b…621a30` | round 2 `57b7424b…621a30` | equal |
| hl (probe `hl-b3.toml`) | 0 / 37.3 / 779,292 | `8ecb39f6…971331` | probe `8ecb39f6…971331` | equal |

The c7b3 log repeats round 2's note: "--pgo-phase steady@0x822fc had no effect". No existing
pipeline changed bytes.

## P1: O4-r1 identity (design 1.7 step 2)

The `o4` line runs `-O4` with the private paths. It is in `opt/o4/command.txt`.

* rc 0, 127.3 s, 1,397,788 KiB.
* cache.sc `ae360ff7…abd378`. `cmp` is silent against round 2's c7b3, the c7b3 anchor copy and
  the rebuild.
* The `o4` block of `opt/o4/optimize.json` records:
  * pipeline `O4`, `pipeline_id` `sha256:5e415286…`;
  * `required_stages`: all 8 O4 passes;
  * `profile_match` 1.0;
  * snapshot `icount_off` true and `sealed` true;
  * runtime capabilities v6, trusted_v7 and member_skip all from `capability_table`;
  * runtime_signature `0xa1cbf008b2329bbb`.
* `effective_configuration`: host-inline is `no_change` (`no_eligible_native_calls`). The other 7
  passes are `edited`.

So O4-r1 inherits c7b3's measurements on uart.

`o4_pinned` (`--pipeline design/pipelines/O4.toml`) is also `ae360ff7…`. Its store was moved to
`opt/o4_pinned/store` (see Deviations).

## P2: builds (`opt/<arm>`, receipts in `receipts.json`)

Columns: rc / wall s / peak RSS (`time-v.txt`); cache sha256; allocated bytes; reserved;
regions / members / member-descriptor regions (from `decode_directory`). Every cache has
22,836 records.

| Arm | Build | cache.sc | Allocated B | Reserved | Regions / members / md |
|---|---|---|---|---|---|
| o4 | 0 / 127.3 / 1,397,788 | `ae360ff7…abd378` | 10,649,600 | 0xc000000f | 19 / 614 / 19 |
| o4_chained (A1) | 0 / 132.0 / 1,397,432 | `d11b4d7e…a9da0b` | 10,723,328 | 0xc000000f | 23 / 784 / 23 |
| o4_isb (I1) | 0 / 131.0 / 1,398,232 | `425ed606…0425d4` | 10,649,600 | 0xc000000f | 19 / 614 / 19 |
| o4_rank (A3) | 0 / 135.8 / 1,397,136 | `ae360ff7…abd378` = o4 | 10,649,600 | 0xc000000f | 19 / 614 / 19 |
| o4_hotgraph (A4) | 0 / 148.1 / 1,553,808 | `34218f0e…54f7e5` | 10,960,896 | 0xc000000f | 19 / 614 / 19 |
| o4_min2 (A7) | 0 / 140.1 / 1,396,168 | `e412891d…626060` | 10,907,648 | 0xc000000f | 26 / 1,210 / 26 |
| o4_legacy (A8) | 0 / 137.1 / 1,396,984 | `ae360ff7…abd378` = o4 | 10,649,600 | 0xc000000f | 19 / 614 / 19 |
| o4_nohi (X1) | 0 / 42.9 / 778,584 | `8ecb39f6…971331` (= probe hl) | 10,649,600 | 0xc000000f | 19 / 614 / 19 |
| o4_nocp (X2) | 0 / 131.1 / 1,397,324 | `7d4c4211…e8907a` | 10,649,600 | 0xc000000f | 19 / 614 / 19 |
| o4_noregions (X3) | 0 / 132.3 / 1,398,620 | `a8f1020e…8bc314` (= s6b3 anchor) | 10,354,688 | 0x8000000f | none |
| o4_nospec (X4) | 0 / 129.2 / 1,399,068 | `b9a5ee92…5f31a4` | 10,641,408 | 0xc000000d | 19 / 614 / 19 |
| fl3b3 (C1) | 0 / 40.4 / 778,928 | `e569349d…566ab3` (= probe2 c1) | 10,457,088 | 0xc000000f | 7 / 188 / 7 |
| fl3b3p (C1p) | 0 / 40.2 / 779,008 | `53717050…78221b` (= probe2 c1p) | 10,420,224 | 0xc000000f | 3 / 118 / 3 |
| b3l (C2) | 0 / 48.1 / 778,956 | `2736aa2e…e72459` | 10,301,440 | 0x8000000f | none |
| o4_pwhopr (A5, H) | 0 / 135.5 / 1,398,588 | `c4eae805…e7bbe0` | 10,919,936 | 0xc000000f | 27 / 1,228 / 27 |
| o4_hostall (A6, H) | 0 / 149.7 / 1,564,408 | `a84d4588…75ffd4` | 14,929,920 | 0xc000000f | 19 / 614 / 19 |
| o4_pinned | 0 / 131.8 / 1,398,972 | `ae360ff7…abd378` = o4 | 10,649,600 | 0xc000000f | 19 / 614 / 19 |

The full sha256 of each arm is in `arms-built.json`.

**Loader requirements** (`cache.sc.scopt.json`):
* Every region arm: `qemu-sc-v7-trusted-firmware-regions+sc6+member-skip`.
* o4_noregions and b3l: `qemu-sc-v7` (sc6-only).

**x_b3.firmware_region_links:** descriptor refusals are 0 in every region arm.

**Member skip:**
* Every hot-loop arm with 19 regions has 109 members with skip flags and 131 skipped B3 slots.
* o4_chained: 121 and 148. o4_min2 and o4_pwhopr: 143 and 178.
* fl3b3: 35 and 46. fl3b3p: 24 and 31.

**Heavy arms.** On uart, neither heavy arm needed heavy resources:
* o4_pwhopr: 135.5 s and 1,398,588 KiB. The 5 s RSS sampler `opt/o4_pwhopr/rss_watch.log`
  peaked at 1,351,868 KiB and never neared the 8 GB abort.
* o4_hostall: 149.7 s and 1,564,408 KiB.

### B3 rows

The B3 rows are identical in every arm that runs the pass, except where noted:
* specialize-returns: 9 variants; refused exit_not_goto_ptr 14, target_not_profiled 522,
  fact_not_constant 23, fact_unused 3, not_profitable 3, provenance_unexplained 3.
* constant-propagation: 6 patches. branch-folding: 7. dead-host-ops: 7.
* chain-returns: 43 slots; refused exit_not_goto_ptr 14, slot_budget 6, not_profitable 1,
  target_not_profiled 522.
* chain-static: 50 slots; refused handler_mode_return 16, not_profitable 1,
  target_not_profiled 1,370.

Exceptions:
* **o4_nospec:** chain-returns has 52 slots (888,843 guarded train lookups), and its slot_budget
  and not_profitable refusals are gone.
* **o4_hostall:** chain-static refuses exit_not_goto_ptr 2 and target_not_profiled 1,368. This is
  the same shift w7bd_b3 showed in round 2.
* **o4_rank:** the chain-static receipt has
  `{targets_per_exit 2, rank_by_net_work true, c_target_check 0.0}`. It still has 50 slots with
  the same refusals as o4.

### Host-inline (applied / refused of 14,134 sites seen)

| Arm | Outcome | Applied | Refused by outcome | helper_lookup_tb_ptr |
|---|---|---|---|---|
| o4 and every hot-loop-only arm | no_change `no_eligible_native_calls` | 0 | outside_observed_loop 13,780, below_min_site_calls 322, selected_root_unsupported 30, call_target_mismatch 2 | 0 applied |
| o4_hotgraph | candidate `native_calls_spliced` | 20 (helper_v7m_msr 6, helper_rebuild_hflags_m32_newel 6, helper_udiv 6, helper_v7m_mrs 2) | outside_observed_loop 13,422, below_min_site_calls 611, helper_graph_size_policy 71, call_target_mismatch 10 | 0 applied (71 `helper_graph_size_policy`) |
| o4_hostall | candidate `native_calls_spliced` | 1,455 (helper_exception_with_syndrome 964, helper_v7m_msr 151, …) | call_target_mismatch 10,581, helper_graph_size_policy 2,050, unsupported_abi 48 | 0 applied (2,050 `helper_graph_size_policy`, 628 call_target_mismatch) |
| o4_nohi, fl3b3, fl3b3p, b3l | no host-inline pass | – | – | – |

### Firmware-inline selection (`receipts.json` → `firmware-inline`)

hot-loop arms discover 202 calls. FirmLooper v3 arms discover 227.

| Arm | Regions | Roots refused | Nested inlines | profit_net_ns | isb exits / regions | nested_decisions | Against o4: added / dropped / price-changed / member-changed |
|---|---|---|---|---|---|---|---|
| o4 | 19 | not_profitable 18 | 26 | 10,464,262 | 0 / 0 | 20 | – |
| o4_chained | 23 | not_profitable 14 | 27 | 12,450,098 | 0 / 0 | 20 | 4 (0x83570, 0x834c6, 0x83d74, 0x83e9a) / 0 / 19 / 0 |
| o4_isb | 19 | not_profitable 18 | 26 | 10,464,262 | **2 / 2** (`isb_sources` 40) | 20 | 0 / 0 / 0 / 0 |
| o4_min2 | 26 | not_profitable 24 | 41 | 11,251,997 | 0 / 0 | 20 | 7 (0x81f76, 0x81cda ×2, 0x81f68, 0x81f92, 0x81ed4, 0x81fa8) / 0 / 1 / 0 |
| o4_pwhopr (exhaustive, priced) | 27 | not_profitable 175, unpatchable_native_exit 10 | 43 | 11,308,093 | 0 / 0 | 0 | the 7 of o4_min2 plus 0x819c6 / 0 / 1 / 0 |
| o4_hotgraph, o4_hostall | 19 | not_profitable 18 | 26 | 10,420,690 / 10,418,950 | 0 / 0 | 20 | 0 / 0 / 10 / 0 |
| o4_rank, o4_legacy, o4_pinned, o4_nocp, o4_nospec, o4_nohi | 19 | not_profitable 18 | 26 | 10,464,262 | 0 / 0 | 20 | 0 / 0 / 0 / 0 |
| fl3b3 (v3, unpriced) | 7 | – | 12 | 0 | 0 / 0 | 0 | the probe2 c1 bytes |
| fl3b3p (v3, profile-priced) | 3 | not_profitable 4 | 8 | 1,105,512 | 0 / 0 | 0 | the probe2 c1p bytes |

* The FirmLooper `selection_summary` is in `receipts.json`. TzCtrl, prvIdleTask and prvTimerTask
  are `only_rtos_boundary_calls`, as in round 2.
* fl3b3p prices with `cost_model {enabled true, mode profile, phase steady}`.
* The isb column counts residual entries with reason `isb_boundary`. The v3 arms, like the fl3
  rebuild, record none.

## Gates (design 3.2; `gates.json`)

**G0 passes for all 17 builds.** That covers:
* optimize rc 0, published, and exactly one artifact in the store;
* the functional check;
* the loader requirement;
* region-link refusals 0;
* the launched QEMU's sha256 equals the artifact runtime identity's `qemu_sha256`
  (`compare_rtos.runtime_identity_check`);
* sc6 loaded, `invariant_violations` 0 and `flush_resets` 0 in every run.

**Functional check** (`uart-w/fcheck/<arm>/result.json`, cases commands and help). Each case had a
public run (`QEMU_SC_PROFILE` warm-identity qprof) and a stats run (`QEMU_SC_V7_STATS` and
`QEMU_SC_V6_STATS`). Every run of every arm had:
* exit 0, the 16 oracle words (`uart-w/cases.json`) and 1 `[BENCH][fw]` line;
* no forbidden log line;
* the cache copy's size and header unchanged;
* a clean, warm qprof with no overflow and the loaded identity equal to the header.

The source cache sha256 was the same before and after, and equal to the copy's.

Every v7-directory arm also had:
* loaded, mode trusted, user_trusted_input;
* `artifact_id8` equal to `read_sc7`'s;
* public and guest checks 0;
* `counters_revision` 4 and `counter_problems` [];
* poll entries verified equal to regions loaded, with 0 unverified;
* `chain_skipped_trusted_poll_unverified` 0.

**Regions replay** (the stats run; help in brackets where it differs). Entries are
`region_entries` with `benchmark_region_entries` in parentheses.

| Arm | Regions loaded/activated | Entries (bench) | Residual exits | T->T allowed/installed | Slots armed/skipped | Poll v/u/exits | trusted_tb_calls | Loop returns | v6 act./links |
|---|---|---|---|---|---|---|---|---|---|
| o4 | 19/19 | 19 (12) | 2 | 0/0 | 17/131 | 19/0/0 | 1811 [1808] | 621 [620] | 55/6 |
| o4_chained | 23/23 | 25 (16) | 9 | 2/2 | 31/148 | 23/0/1 [0] | 1786 [1781] | 612 [611] | 52/6 |
| o4_isb | 19/19 | 19 (12) | 2 | 0/0 | 19/131 | 19/0/0 | 1810 [1808] | 620 | 55/6 |
| o4_rank | 19/19 | 19 (12) | 2 | 0/0 | 17/131 | 19/0/0 | 1810 [1808] | 620 | 55/6 |
| o4_hotgraph | 19/19 | 19 (12) | 2 | 0/0 | 17/131 | 19/0/0 | 1810 [1808] | 620 | 55/6 |
| o4_min2 | 26/26 | 27 (20) | 4 | 0/0 | 37/178 | 26/0/0 | 1747 | 599 | 44/6 |
| o4_legacy | 19/19 | 19 (12) | 2 | 0/0 | 17/131 | 19/0/0 | 1810 | 620 | 55/6 |
| o4_nohi | 19/19 | 19 (12) | 2 | 0/0 | 17/131 | 19/0/0 | 1811 [1846] | 621 [632] | 55/6 |
| o4_nocp | 19/19 | 19 (12) | 2 | 0/0 | 17/131 | 19/0/0 | 1808 | 620 | 54/6 |
| o4_nospec | 19/19 | 19 (12) | 3 [2] | 0/0 | 17/131 | 19/0/0 | 1812 [1811] | 622 [621] | 49/0 |
| fl3b3 | 7/4 | 4 (4) | 2 | 0/0 | 4/31 | 7/0/0 | 2062 | 704 | 84/7 |
| fl3b3p | 3/3 | 3 (3) | 2 | 0/0 | 3/31 | 3/0/0 | 2062 [2063] | 704 [705] | 84/7 |
| o4_pwhopr | 27/27 | 28 (21) | 4 [5] | 0/0 | 42/178 | 27/0/0 | 1738 [1736] | 596 | 44/6 |
| o4_hostall | 19/19 | 19 (12) | 3 [2] | 0/0 | 17/131 | 19/0/0 | 1812 [1810] | 622 [620] | 55/6 |
| o4_pinned | 19/19 | 19 (12) | 3 [2] | 0/0 | 17/131 | 19/0/0 | 1811 [1810] | 621 [620] | 55/6 |

o4_noregions and b3l have no v7 directory: v7 stats report `loaded` false. On the sc6 side, both
show loaded, 94 activations, 9 of 9 links armed, 9 variants built, 0 invariant violations and 0
flush resets, in both cases.

**A1 gate (o4_chained against o4).** The 4 added regions are activated. Region activations are
+4 and region entries +6 in both cases (19 → 25; benchmark entries 12 → 16). Residual exits rise
from 2 to 9, and 2 T->T installs appear. Verdict: **"entered, possibly slower"** (design 3.2).
The residual exits are in regions 0x834a4 (2), 0x8354e (3), 0x83d6e (1) and 0x83e98 (1), besides
o4's 0x821da and 0x820f6.

**Validity** (all pass; the predictions are probe2's and 3.2's uart values):

| Arm | Gate | Measured |
|---|---|---|
| o4 | host-inline 0, regions 19, chain-returns 43, chain-static 50, cache = c7b3 | 0, 19, 43, 50, `ae360ff7…` |
| o4_chained | regions about 23 | 23 |
| o4_isb | `policy.isb_boundary` true; at least 2 isb exits; nested_decisions = o4's; member differences are ISB successors | true; 2 in 2 regions; 20 = 20; no member difference (vacuous) |
| o4_rank | chain-static applied; `c_target_check 0.0` in the receipt | candidate with 50 slots; 0.0 and `rank_by_net_work` true |
| o4_hotgraph | about 20 sites; helper_lookup_tb_ptr 0; SPEC variants against 9 | 20; 0 (71 `helper_graph_size_policy`); 9 |
| o4_min2 | regions about 26; every added root priced | 26; all 7 added roots have calls and `net_ns` |
| o4_legacy | regions about 19; same bytes as o4 | 19; same bytes |
| o4_nohi / o4_nocp / o4_nospec | pass(es) absent | absent |
| o4_noregions | no firmware directory | none, reserved 0x8000000f, `qemu-sc-v7` |
| fl3b3 / fl3b3p | 7 / 3 regions with member descriptors | 7/7 and 3/3 |
| b3l | layout passes ran; bridge or Pettis-Hansen | hotcold-partition `classified`, llvm-cache-layout **`pettis_hansen_fallback`**, order-cold `ordered` |
| o4_pwhopr | roots, members, growth, time, memory; abort above 8 GB or 60 min | 27 admitted, 185 refused, 1,228 members, +270,336 B allocated over o4, 135.5 s, 1.40 GB |
| o4_hostall | about 1,455 sites | 1,455 |

**Same bytes** (`cmp` and sha256 against o4):
* o4_legacy (expected in 3.2) and o4_rank (not in 3.2's expected list) equal o4 on uart. Both
  are decided without timing on uart.
* The expected cli and packet pairs are for the other workloads.

## Findings for the lead

1. **o4_rank equals o4 on uart.** It is not in 3.2's expected same-bytes list. With
   `c_target_check = 0.0`, chain-static keeps the same 50 slots and the same refusals, so A3 is
   uart-same-bytes. The design expected the cap to bind only on packet; this agrees with that.
2. **o4_noregions equals the round-2 anchor s6b3** (`a8f1020e…8bc314`). The campaign-A
   diagnostic `[s6b3, o4_noregions]` is therefore a second A/A on uart. `make_plan.py` drops only
   arms whose bytes equal o4's, so it will still time this pair.
3. **o4_nohi differs from o4 in layout only.** 202,146 bytes differ (`cmp -l`), yet the selection
   is the same:
   * Every pass receipt is identical.
   * Every firmware root (keyed by source_uid@pc) has the same members, nested inlines and
     profit.
   * The directory holds the same 19 regions / 614 members / 727 edges.

   The units' `source_hash` differs. For example, `tb:016f2783…#0` has `a13b45dd…` under o4 and
   `92f98917…` under o4_nohi. With o4's host-inline native prefix stage, which runs even with 0
   sites applied, the callsite ids, region ids, region order (sorted by region_id8) and member
   placement all differ.

   o4_nohi's bytes equal the probe's hl. So on uart, X1 times a region-order/placement change, not
   a host-inline difference.
4. **A1 is "entered, possibly slower" on uart.** See the A1 gate above.
5. **I1 on uart** has 2 isb exits in 2 regions and no member or price change. The only runtime
   difference from o4 is 2 more armed residual slots (19 against 17).
6. **C2 (b3l) built and passed.** llvm-cache-layout used the Pettis-Hansen fallback; no bridge.

## Deviations from the design

* `o4_pinned`'s `--store` is `opt/o4_pinned/store` instead of commands.txt's `opt/o4/store`. That
  is a path change only, so each store holds exactly one artifact, as `make_plan.py` requires.
  `o4_pinned` is an identity aid, not a campaign arm.
* The rebuild builds are in `/data/scfix-o4/uart-w/rebuild/`, a location the design does not
  name. The hl rebuild ran without the probe's `pkg` on `PYTHONPATH`. That package only adds the
  prototype policies, which hl does not use.
* `/usr/bin/time -v` was used for every build, not only the heavy ones.
* The functional check is a copy of round 2's fcheck logic. It uses the O4 driver's `stage()` and
  `runtime_identity_check()` and adds the 3.2 poll, sc6 and loader gates. It records no elapsed
  time.

## Hygiene

`find -newer /data/scfix-o4/uart/build.sh` (created before the first build) at the end found:
* 0 entries in `/data/scfix-r2`, `/data/scfix-campaign`, `/data/trebuchet-clean`,
  `/data/qemu-scfix-bin` and `/data/fastdyn-work/qemu-fu`;
* 1 entry in `/data/fastdyn`: the directory `/data/fastdyn/.git/worktrees/fd-int` (mtime
  01:04:07). That is git's per-worktree admin directory, touched by `git status` in fd-int; no
  file in it is newer.

The packet and cli agents built at the same time on CPUs 21-31.

## P4b: o4_tasks (hot-loop-tasks), fd-int b35cdb6

Date 2026-11-26. Builds, functional check and regions replay only. **Nothing was timed.**
P4b's scripts and outputs are in `/data/scfix-o4/p4b`. The code is fd-int commit `b35cdb6`
(see packet VERIFY.md P4b for the commit and the unit suite: the base 24 failures, 1940
passed, 490 skipped).

* **Byte spot check: o4 rebuilt at b35cdb6** (`p4b/spotcheck.sh`): the commands.txt uart
  `# o4` line (`-O4`) with only `--store` moved, through `U/build.sh`.
  * **Attempt 1 failed** (rc 1, 16.1 s) with the known intermittent helper-PGO
    counted-replay error (round 2 TIMING.md problem 7): `Error: training replay failed:
    helper PGO replay failed (1): Catchpoint 1 (syscall 'exit_group' [231])`, then
    `count.gdb:11: Error in sourced command file: Couldn't get registers: No such process.`
    The record (without its empty store) is in `p4b/failed/uart-rebuild-o4-b35cdb6.attempt1`.
  * **Attempt 2**, identical: rc 0, 124.4 s, published. cache.sc sha256 `ae360ff7d8a5…`,
    **`cmp` EQUAL** to `uart-w/opt/o4`'s cache.sc, so the code left O4's bytes unchanged.
  * The rebuild was moved to `p4b/spotcheck-uart-o4-b35cdb6`, so that `U/receipts.py` (which
    scans `uart-w/rebuild`) does not list it next to the build step's rebuilds.
* **Build o4_tasks.** The commands.txt uart `# o4_tasks` line through `U/run_arm.sh` into
  `uart-w/opt/o4_tasks`: rc 0, 120.8 s, peak RSS 1,398,644 KiB, published, 1 artifact.
  cache.sc sha256 `ae360ff7d8a56a52b1f8908a46a0e5e4400771bedbf467e7b84c45c7d2abd378`,
  **the same bytes as o4** (`cmp` EQUAL; also c7b3, o4_rank, o4_legacy and o4_pinned).
  Regions 19, members 614, allocated 10,649,600 B.
* **Receipts against o4** (`p4b/uart-validity.json`): `policy.union` hot 37, static 7,
  static_only 1, both 6. The one static-only root, 0x8235a → 0x81bea (vComputeDone, 1 call,
  `net_ns` −687.7), is refused not_profitable (refused 19 against o4's 18). All 19 o4 roots
  are embedded unchanged (region ids equal, `net_ns` difference 0.0). nested_decisions are
  equal (20 = 20), nested inlines 26 = 26, and profit_net_ns is equal (10,464,262.432).
  Every other pass is identical, edit digests included.
* **G0** (`U/fcheck.py` → `uart-w/fcheck/o4_tasks/result.json`; `U/gates.py` → `U/gates.json`,
  `U/arms-built.json`): **all pass.** Cases commands and help: rc 0, words ok, 1 BENCH, 0
  forbidden. Loader requirement ok. Region-link refusals 0. The launched QEMU is the
  artifact's. sc6 loaded, with 0 invariant violations and 0 flush resets.
* **Validity** (design 3.2 o4_tasks, shared checker `p4b/tasks_validity.py`): union counts
  present, every o4 root unchanged, nested_decisions equal. All true.
* **Regions replay** (commands / help): loaded 19 / 19; activations 19 / 19; entries 19 / 19;
  residual exits 2 / 4; slots armed / skipped 17 / 131; poll verified 19, unverified 0;
  counters revision 4; no counter problems. o4's replay of the same bytes: residual exits
  2 / 2, everything else equal. The help case's 4 against 2 is run-to-run variation on
  identical bytes.
* **Records.** Old files are in `p4b/backup/uart`.
  * `U/receipts.json` gains `opt/o4_tasks`.
  * `U/gates.json` and `U/arms-built.json` gain `o4_tasks` (g0 and validity true). The only
    change to other arms is that o4, o4_rank, o4_legacy and o4_pinned add `o4_tasks` to
    `same_bytes_as`.
  * `U/gates.py` gained the o4_tasks validity branch and annotation.
* **For A2.** o4_tasks has the same bytes as o4 on uart, so it is decided without timing on
  uart (design 3.2).

## P5: o4r2 (O4-r2), fd-int b35cdb6

Date 2026-11-26. P5 decisions (O4-design 3.7) and the O4-r2 build (3.8 Composition). Builds, functional checks and a regions replay only. **Nothing was timed.** P5's scripts and outputs are in `/data/scfix-o4/p5`, and the decisions are in `/data/scfix-o4/decisions.json`.

* **Composition.** O4-r2 = O4-r1 + **A5 absorbed** (rule BEST: o4_pwhopr was faster than O4 in campaign H, cli d +1.97% and pooled +0.93%) + **I1** (isb_boundary = true, FIDELITY) + **X1** (host-inline removed, REMOVE) + **X4** (SPEC bundle removed, REMOVE). X2 is accepted but moot under X4, X3 is rejected, and no add-on passed ADD.
  The passes are chain-returns, chain-static and firmware-inline `{mode exhaustive, cost_model profile, phase steady, isb_boundary true, trust user, planner whopr, partitions 4, workers 4, member_slot_skip on}`.
* **Pipeline.** The revision file is `/data/scfix-o4/design/pipelines/O4-r2.toml` (name "O4-r2", sha256 `1ee63d43790eba14…`).
  * The arm was built from its copy `design/pipelines/o4r2.toml` (sha256 `4ea2b72005203d51…`), which differs only in name ("o4r2") and description (`p5/make_arm_copy.py`; tomllib and scopt `load_pipeline` agree).
  * **Deviation:** `--pipeline O4-r2.toml` itself fails in scopt's O4 preflight before any pass, rc 2: `pipeline 'O4-r2' is not the packaged /data/fastdyn-work/fd-int/tools/scopt/pipelines/O4-r2.toml; O4 is fixed: copy O4-rN.toml under another name to experiment`. That attempt is kept in `p5/failed/uart-o4r2.attempt1`.
  * The pipeline name does not reach cache.sc: O4-r1 via -O4 equals c7b3 byte for byte (section 1). O4.toml and -O4 are not repointed before campaign B.
* **Build.** `p5/o4r2-commands.txt` uart line: the `# o4_nohi` line of commands.txt with only `--pipeline` and `--store opt/o4r2/store` changed, so there is no `--pgo-launch`. It ran through this workload's own wrapper (nice -n 10 taskset -c 21-31, /usr/bin/time -v, timeout 3600) at fd-int b35cdb6, with no code change in P5.
  * rc 0, 40.8 s, peak RSS 759,508 KiB (time -v); the watchdog's process-tree peak was 1,154,096 KiB (limits 8 GiB / 60 min). The artifact was published, 1 in the store.
  * cache.sc sha256 `839cf96bd65256b0c99e804684bebd05e944a2c4cb7f6f0d9555316bbd8cce1b`. Regions / members / descriptor regions: 27 / 1,228 / 27 (o4_pwhopr 27 / 1,228 / 27; o4 19 / 614 / 19).
  * Allocated 10,911,744 B (o4_pwhopr 10,919,936, o4 10,649,600). Reserved 0xc000000d (o4_pwhopr 0xc000000f, o4 0xc000000f). Loader requirement `qemu-sc-v7-trusted-firmware-regions+sc6+member-skip`.
* **Stage-gate equivalent** (fd-int `required_stage_problems` and `effective_configuration` with `gate_status` over O4-r2's stages): chain-returns edited, chain-static edited, firmware-inline edited; problems [].
* **B3 rows.** o4r2: chain-returns 52 slots, refused {'exit_not_goto_ptr': 14, 'target_not_profiled': 522}; chain-static 50 slots, refused {'handler_mode_return': 16, 'not_profitable': 1, 'target_not_profiled': 1370}.
  * They equal o4_nospec's: True. o4: chain-returns 43 slots, refused {'exit_not_goto_ptr': 14, 'not_profitable': 1, 'slot_budget': 6, 'target_not_profiled': 522}; chain-static 50 slots, refused {'handler_mode_return': 16, 'not_profitable': 1, 'target_not_profiled': 1370}.
* **Firmware-inline** (o4r2 / o4_pwhopr / o4):
  * embedded 27 / 27 / 19; nested inlines 43 / 43 / 26
  * refused {'not_profitable': 175.0, 'unpatchable_native_exit': 10.0} / {'not_profitable': 175.0, 'unpatchable_native_exit': 10.0} / {'not_profitable': 18.0}
  * profit_net_ns 11308093.302 / 11308093.302 / 10464262.432
  * isb_boundary exits (regions) 2 (2) / 0 / 0
  * Roots against o4_pwhopr: added [], dropped [], members changed 0, common 27.
  * Roots against o4: added ['0x81f76', '0x819c6', '0x81cda', '0x81f68', '0x81f92', '0x81ed4', '0x81cda', '0x81fa8'], dropped [], members changed 0.
* **G0** (`gates/uart.json` `o4r2`, regenerated by `driver/make_gates.py`): **all pass** (13 checks).
  * The functional runs exit 0 with the 16 oracle words, 1 BENCH line and no forbidden pattern.
  * Runtime identity: the QEMU sha256 equals the artifact's `951775b0…`.
  * Counters revision 4, `counter_problems` [], every poll entry verified.
  * v6: invariant_violations 0 and flush_resets 0. Cache unchanged; region-link refusals 0; loader requirement ok.
* **Validity** (`p5/uart-o4r2-validity.json`, shared checker `p5/o4r2_validity.py`): pipeline_copy_equals_O4_r2 true, passes_are_O4_r2 true, stage_gate_all_edited_or_no_change true, no_host_inline_receipt true, spec_passes_absent true, firmware_inline_exhaustive_profile true, isb_boundary_true true, member_slot_skip_enabled true, build_within_8GiB_60min true.
* **Same bytes** (`cmp` of cache.sc): o4 differs, o4_isb differs, o4_nohi differs, o4_nospec differs, o4_pwhopr differs. The workload aggregator also compared it with every other built arm and the anchors: no match. o4r2 is not the same bytes as o4 or as any accepted single-item arm, so it is timed on this workload in B1.
* **Regions replay** (single untimed runs):

  | arm | case | loaded | activated | lookup entries | residual exits | T->T installs | slots armed / skipped | poll verified / unverified | loop_returns_nonnull |
  |---|---|---|---|---|---|---|---|---|---|
  | o4r2 | commands | 27 | 27 | 28 | 5 | 0 | 44 / 178 | 27 / 0 | 598 |
  | o4r2 | help | 27 | 27 | 28 | 4 | 0 | 44 / 178 | 27 / 0 | 597 |
  | o4_pwhopr | commands | 27 | 27 | 28 | 4 | 0 | 42 / 178 | 27 / 0 | 596 |
  | o4_pwhopr | help | 27 | 27 | 28 | 5 | 0 | 42 / 178 | 27 / 0 | 596 |
  | o4 | commands | 19 | 19 | 19 | 2 | 0 | 17 / 131 | 19 / 0 | 621 |
  | o4 | help | 19 | 19 | 19 | 2 | 0 | 17 / 131 | 19 / 0 | 620 |

  Launch: `/data/scfix-o4/uart-w/arms/o4r2/launch.json`, derived by the gate script through `o4lib.derive_arm_launch` (the private c7b3 launch with o4r2's cache).
* **Records.** `U/receipts.json` gains `opt/o4r2`, and `U/gates.json` and `U/arms-built.json` gain `o4r2`. `U/gates.py` gained the o4r2 validity branch and annotation.
  * Every other arm's entry is unchanged, checked key by key against the backups in `/data/scfix-o4/p5/backup/`.
  * `driver/make_gates.py` added o4r2 to BUILT (sha256 `19f228d9…` → `58a6ebc1…`); decide.py is unchanged (`0b724747…`). The rerun rewrote `gates/{packet,uart,cli,all}.json`.
  * For the other arms only the evidence strings changed (they quote the regenerated records' sha256); their g0, checks and cache sha256 are unchanged. The driver tests pass (60 passed).
* **QEMU** `/data/qemu-scfix-bin/aacca05/qemu-system-arm`: sha256 `951775b0…eebc60`, inode 87162887, size 137,799,456, mtime 1795640690, the same before and after (`p5/qemu-identity.txt`).

## R3: o4r3b, o4r3c, o4r3d (round 3), fd-int b35cdb6

Date 2026-11-26. Round R3 preparation: builds, functional checks and regions replays only. **Nothing was timed.** Scripts and outputs are in `/data/scfix-o4/r3`; the round's rule is `/data/scfix-o4/driver/PREREG-R3.md` (decide_r3.py).

* **Pipelines** (`r3/make_r3.py`; each the exact union of the measured arm deltas against O4-r1.toml, checked with tomllib and scopt `load_pipeline`; pass blocks copied verbatim from o4-pwhopr / o4-nohi / o4-nospec / o4-rank.toml; names outside the O4 preflight pattern):
  * `design/pipelines/o4r3a.toml` (O4-r1 + A5), sha256 `b76c5d06906e05fe…`: host-inline, specialize-returns, constant-propagation, branch-folding, dead-host-ops, chain-returns, chain-static, firmware-inline
  * `design/pipelines/o4r3b.toml` (O4-r1 + A5 + X1 + X4), sha256 `1b27f53932ee06e6…`: chain-returns, chain-static, firmware-inline
  * `design/pipelines/o4r3c.toml` (O4-r1 + A5 + A3), sha256 `1739ead0f8798513…`: host-inline, specialize-returns, constant-propagation, branch-folding, dead-host-ops, chain-returns, chain-static, firmware-inline
  * `design/pipelines/o4r3d.toml` (O4-r1 + A5 + A3 + X1 + X4), sha256 `8dacdb8017cadbb1…`: chain-returns, chain-static, firmware-inline
  * o4r3a's passes equal o4-pwhopr.toml's (only name and description differ): its timed build is the existing o4_pwhopr arm.
* **Build lines** (`r3/commands.txt`): o4_pwhopr's line of `design/commands.txt` with exactly `--pipeline` and `--store opt/<arm>/store` changed. `--pgo-launch` is kept on every line: scopt does not require dropping it without host-inline (the helper-PGO replay runs only for host-inline `hot_loop_only` or firmware-inline without `--profile`; checked in `scopt/commands/optimize.py`). They ran through this workload's own wrapper (nice -n 10 taskset -c 21-31, /usr/bin/time -v, timeout 3600, an 8 GiB RSS watchdog) at fd-int b35cdb6 (`r3/build.sh`, `r3/build.log`), with no timing running.
  * e.g. o4r3b: `scopt optimize /data/scfix-o4/uart-w/in/snapshots/a12a609742ba1001e918e16d9fa42f0690faf75d49d339c72d72793ed38a1169 --pipeline /data/scfix-o4/design/pipelines/o4r3b.toml --profile /data/scfix-o4/uart-w/in/merged.profile.json --prep /data/scfix-o4/uart-w/in/prep --elf /data/scfix-o4/uart-w/in/fw.elf --runtime-identity /data/scfix-o4/uart-w/in/runtime.json --pgo-launch /data/scfix-o4/uart-w/launch/launch.json --v6-exe sha256:951775b0300ea1421f32565af8f677c1eaee14e7e1de6238d32bdbe925eebc60 --v7-trusted-exe sha256:951775b0300ea1421f32565af8f677c1eaee14e7e1de6238d32bdbe925eebc60 --v7-member-skip-exe sha256:951775b0300ea1421f32565af8f677c1eaee14e7e1de6238d32bdbe925eebc60 --store /data/scfix-o4/uart-w/opt/o4r3b/store --json`
* **Builds**:
  | arm | rc | wall s | peak RSS KiB (time -v / watchdog) | cache.sc sha256 | regions / members / descriptor regions | allocated B | loader |
  |---|---|---|---|---|---|---|---|
  | o4r3b | 0 | 41.4 | 760,348 / 1,144,840 | `7f93d0d7c21655885bb6fab8b1efc297a64b6941ed77d462a633ac722d90ee0d` | 27 / 1228 / 27 | 10,911,744 | `qemu-sc-v7-trusted-firmware-regions+sc6+member-skip` |
  | o4r3c | 0 | 124.4 | 1,396,984 / 2,714,260 | `c4eae8055913b9b75b7955a0b1d023add74fad0a0bdbdc2d0b24415e5fe7bbe0` | 27 / 1228 / 27 | 10,919,936 | `qemu-sc-v7-trusted-firmware-regions+sc6+member-skip` |
  | o4r3d | 0 | 40.6 | 759,408 / 1,205,176 | `7f93d0d7c21655885bb6fab8b1efc297a64b6941ed77d462a633ac722d90ee0d` | 27 / 1228 / 27 | 10,911,744 | `qemu-sc-v7-trusted-firmware-regions+sc6+member-skip` |
* **Receipts** (passes: outcome, reason, edits):
  * o4r3b: chain-returns candidate 52; chain-static candidate 50; firmware-inline candidate firmware_regions_emitted 27
    * firmware-inline: embedded 27, members 1228, nested inlines 43, refused {'not_profitable': 175.0, 'unpatchable_native_exit': 10.0}, profit_net_ns 11308093.302 (o4_pwhopr: 27, 1228, 43, {'not_profitable': 175.0, 'unpatchable_native_exit': 10.0}, 11308093.302); roots against o4_pwhopr: added [], dropped [], members changed 0, common 27
    * chain rows: chain-returns 52 slots refused {'exit_not_goto_ptr': 14, 'target_not_profiled': 522}; chain-static 50 slots refused {'handler_mode_return': 16, 'not_profitable': 1, 'target_not_profiled': 1370} (o4_pwhopr: chain-returns 43 {'exit_not_goto_ptr': 14, 'not_profitable': 1, 'slot_budget': 6, 'target_not_profiled': 522}; chain-static 50 {'handler_mode_return': 16, 'not_profitable': 1, 'target_not_profiled': 1370}; o4_rank chain-static: 50 {'handler_mode_return': 16, 'not_profitable': 1, 'target_not_profiled': 1370})
    * stage-gate equivalent (`required_stage_problems`, `gate_status`): {'chain-returns': 'edited', 'chain-static': 'edited', 'firmware-inline': 'edited'}; problems []
  * o4r3c: host-inline no_change no_eligible_native_calls 0; specialize-returns candidate 50; constant-propagation candidate 6; branch-folding candidate 7; dead-host-ops candidate 7; chain-returns candidate 43; chain-static candidate 50; firmware-inline candidate firmware_regions_emitted 27
    * host-inline applied 0 (no_change, no_eligible_native_calls); site profile `sha256:4d464ef3391f2a2f25d0ff654324f70056cb173e8b9a5419fa4bd55caf6c3e0f` (input plumbing, not compared)
    * firmware-inline: embedded 27, members 1228, nested inlines 43, refused {'not_profitable': 175.0, 'unpatchable_native_exit': 10.0}, profit_net_ns 11308093.302 (o4_pwhopr: 27, 1228, 43, {'not_profitable': 175.0, 'unpatchable_native_exit': 10.0}, 11308093.302); roots against o4_pwhopr: added [], dropped [], members changed 0, common 27
    * chain rows: chain-returns 43 slots refused {'exit_not_goto_ptr': 14, 'not_profitable': 1, 'slot_budget': 6, 'target_not_profiled': 522}; chain-static 50 slots refused {'handler_mode_return': 16, 'not_profitable': 1, 'target_not_profiled': 1370} (o4_pwhopr: chain-returns 43 {'exit_not_goto_ptr': 14, 'not_profitable': 1, 'slot_budget': 6, 'target_not_profiled': 522}; chain-static 50 {'handler_mode_return': 16, 'not_profitable': 1, 'target_not_profiled': 1370}; o4_rank chain-static: 50 {'handler_mode_return': 16, 'not_profitable': 1, 'target_not_profiled': 1370})
    * stage-gate equivalent (`required_stage_problems`, `gate_status`): {'host-inline': 'no_change', 'specialize-returns': 'edited', 'constant-propagation': 'edited', 'branch-folding': 'edited', 'dead-host-ops': 'edited', 'chain-returns': 'edited', 'chain-static': 'edited', 'firmware-inline': 'edited'}; problems []
  * o4r3d: chain-returns candidate 52; chain-static candidate 50; firmware-inline candidate firmware_regions_emitted 27
    * firmware-inline: embedded 27, members 1228, nested inlines 43, refused {'not_profitable': 175.0, 'unpatchable_native_exit': 10.0}, profit_net_ns 11308093.302 (o4_pwhopr: 27, 1228, 43, {'not_profitable': 175.0, 'unpatchable_native_exit': 10.0}, 11308093.302); roots against o4_pwhopr: added [], dropped [], members changed 0, common 27
    * chain rows: chain-returns 52 slots refused {'exit_not_goto_ptr': 14, 'target_not_profiled': 522}; chain-static 50 slots refused {'handler_mode_return': 16, 'not_profitable': 1, 'target_not_profiled': 1370} (o4_pwhopr: chain-returns 43 {'exit_not_goto_ptr': 14, 'not_profitable': 1, 'slot_budget': 6, 'target_not_profiled': 522}; chain-static 50 {'handler_mode_return': 16, 'not_profitable': 1, 'target_not_profiled': 1370}; o4_rank chain-static: 50 {'handler_mode_return': 16, 'not_profitable': 1, 'target_not_profiled': 1370})
    * stage-gate equivalent (`required_stage_problems`, `gate_status`): {'chain-returns': 'edited', 'chain-static': 'edited', 'firmware-inline': 'edited'}; problems []
* **G0** (`gates/uart.json`, regenerated by `driver/make_gates.py`):
  * o4r3b: g0 **True** (13/13 checks true), cache sha256 `7f93d0d7c2165588…`
  * o4r3c: g0 **True** (13/13 checks true), cache sha256 `c4eae8055913b9b7…`
  * o4r3d: g0 **True** (13/13 checks true), cache sha256 `7f93d0d7c2165588…`
  * The functional runs exit 0 with the 16 oracle words, 1 BENCH line and no forbidden pattern; the launched QEMU's sha256 equals the artifact's `951775b0…`; counters revision 4, `counter_problems` [], every poll entry verified; sc6 invariant_violations 0 and flush_resets 0; cache unchanged; region-link refusals 0; loader requirement `qemu-sc-v7-trusted-firmware-regions+sc6+member-skip`.
* **Validity** (`r3/<w>-<arm>-validity.json`, shared checker `r3/r3_validity.py`):
  * o4r3b: passes_are_pipeline true, stage_gate_all_edited_or_no_change true, host_inline_as_pipeline true, spec_as_pipeline true, firmware_inline_exhaustive_profile true, isb_boundary_off true, member_slot_skip_enabled true, chain_static_as_pipeline true, build_within_8GiB_60min true
  * o4r3c: passes_are_pipeline true, stage_gate_all_edited_or_no_change true, host_inline_as_pipeline true, spec_as_pipeline true, firmware_inline_exhaustive_profile true, isb_boundary_off true, member_slot_skip_enabled true, chain_static_as_pipeline true, build_within_8GiB_60min true
  * o4r3d: passes_are_pipeline true, stage_gate_all_edited_or_no_change true, host_inline_as_pipeline true, spec_as_pipeline true, firmware_inline_exhaustive_profile true, isb_boundary_off true, member_slot_skip_enabled true, chain_static_as_pipeline true, build_within_8GiB_60min true
* **Same bytes** (`cmp` of cache.sc against every other build in opt/ and rebuild/ and every anchor on this workload):
  * o4r3b: o4r3d, rebuild/o4r3b-nopgo
  * o4r3c: o4_pwhopr, rebuild/o4r3a
  * o4r3d: o4r3b, rebuild/o4r3b-nopgo
* **Check builds** (uart only):
  * `rebuild/o4r3a` (o4r3a.toml, o4_pwhopr's line): cache.sc `c4eae8055913b9b75b7955a0b1d023add74fad0a0bdbdc2d0b24415e5fe7bbe0`, same bytes as ['o4_pwhopr', 'o4r3c']. So o4r3a IS o4_pwhopr byte for byte (the pipeline name does not reach cache.sc), and A5's uart build is reproducible at fd-int b35cdb6 (the o4_pwhopr build was at ece9b5e). Its first attempt hit the known helper-PGO gdb flake after 10.7 s (`Couldn't get registers: No such process.`, rc 1); it is kept in `r3/failed/uart-rebuild-o4r3a.attempt1` and the one retry passed (123.6 s). Validity: all 9 items true.
  * `rebuild/o4r3b-nopgo` (o4r3b without `--pgo-launch`): cache.sc `7f93d0d7c21655885bb6fab8b1efc297a64b6941ed77d462a633ac722d90ee0d`, same bytes as ['o4r3b', 'o4r3d']: `--pgo-launch` is inert without host-inline, so o4r3b's bytes are those of the deployed form.
* **Regions replay** (single untimed runs; o4_pwhopr and o4 from the earlier gates):

  | arm | case | loaded | activated | entries | residual exits | T->T installs | slots armed / skipped | poll verified / unverified | loop_returns_nonnull |
  |---|---|---|---|---|---|---|---|---|---|
  | o4r3b | commands | 27 | 27 | 28 | 5 | 0 | 42 / 178 | 27 / 0 | 597 |
  | o4r3b | help | 27 | 27 | 28 | 6 | 0 | 42 / 178 | 27 / 0 | 598 |
  | o4r3c | commands | 27 | 27 | 28 | 5 | 0 | 42 / 178 | 27 / 0 | 596 |
  | o4r3c | help | 27 | 27 | 28 | 4 | 0 | 42 / 178 | 27 / 0 | 596 |
  | o4r3d | commands | 27 | 27 | 28 | 5 | 0 | 42 / 178 | 27 / 0 | 597 |
  | o4r3d | help | 27 | 27 | 28 | 4 | 0 | 42 / 178 | 27 / 0 | 596 |
  | o4_pwhopr | commands | 27 | 27 | 28 | 4 | 0 | 42 / 178 | 27 / 0 | 596 |
  | o4_pwhopr | help | 27 | 27 | 28 | 5 | 0 | 42 / 178 | 27 / 0 | 596 |
  | o4 | commands | 19 | 19 | 19 | 2 | 0 | 17 / 131 | 19 / 0 | 621 |
  | o4 | help | 19 | 19 | 19 | 2 | 0 | 17 / 131 | 19 / 0 | 620 |

* **Records.** The workload's receipts / arms-built records gain o4r3b, o4r3c and o4r3d; the aggregator gained their validity branch (the shared checker) and annotations. Every other arm's entry is unchanged, checked key by key against the backups in `/data/scfix-o4/r3/backup/`, except `same_bytes_as` lists that now name the new builds (uart: o4_pwhopr gains o4r3c and rebuild/o4r3a; cli: o4_pwhopr gains o4r3c, o4r2 gains o4r3b and o4r3d).
  * `driver/make_gates.py` added o4r3b, o4r3c, o4r3d to BUILT (sha256 `4de960ae9438d140…` → `22858e5cd7f21d40…`); decide.py is unchanged (`0b724747…`). The rerun rewrote `gates/{packet,uart,cli,all}.json`; for the other arms g0, checks and cache sha256 are unchanged (only evidence strings that quote the regenerated records' sha256 changed).
* **QEMU** `/data/qemu-scfix-bin/aacca05/qemu-system-arm`: sha256 `951775b0…eebc60`, inode 87162887, size 137,799,456, mtime 1795640690, the same before the builds, after the builds and after the gates (`r3/qemu-identity.txt`).
