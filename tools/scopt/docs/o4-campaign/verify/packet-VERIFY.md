# scopt O4 campaign, packet: builds and gates (O4-design 3.1 P0-P2, 1.7, 3.2)

Date 2026-11-26. Builds, functional checks and regions replays only. **Nothing was timed.**
Wall times are bookkeeping (builds overlapped with each other and with the uart/cli agents'
builds); `elapsed_ns` values in the gate logs are not measurements.

Paths: `P=/data/scfix-o4/packet` (scripts, reports), `W=/data/scfix-o4/packet-w` (inputs,
builds, launches, gate runs).

## Environment

* Code: fd-int `/data/fastdyn-work/fd-int`, branch o4, head `ece9b5e284b8…` (committed
  00:41:58, before the first build at 00:45); `git status --short --ignored` printed 0 lines after all
  builds and runs (`P/identity.txt`, "post-run checks"). No code was changed by this step.
* QEMU `/data/qemu-scfix-bin/aacca05/qemu-system-arm`, sha256 `951775b0…eebc60` (rechecked
  after all runs, `P/identity.txt`). Every built artifact's `runtime_identity.json` names it, and the driver's
  staging check `runtime_identity_check` passed for every arm (`W/gates/arms/<arm>/result.json`
  `runtime_identity_ok`).
* Environment `W/env.sh` (PYTHONPATH fd-int, `PYTHONDONTWRITEBYTECODE=1`). Every build and every
  QEMU run ran under `nice -n 10 taskset -c 21-31`. Build wrapper `P/build-arm.sh` (timeout
  3600 s, `/usr/bin/time -v` on every arm, `$PY -B -m scopt optimize`).
* Scripts: `P/step1.sh` (1.7 checks + o4), `P/step3.sh` (all other arms), `P/gate.py`
  (functional gate + regions replay), `P/receipts.py` -> `P/receipts.json`, `P/assess.py` ->
  `P/arms-built.json` + `P/assess.json`, `P/tables.py` (the tables below), `P/watch_pwhopr.sh`
  (8 GB RSS watchdog).

## 1. Identity checks before any timing (design 1.7; `P/identity.txt`)

| check | result |
|---|---|
| c7b3 rebuilt at ece9b5e (round 2's command, private paths; `W/rebuild/c7b3/command.txt`) vs round 2 `bb8c417a…/cache.sc` | `cmp` EQUAL, sha256 `7702b19d…` |
| fl3 rebuilt (`W/rebuild/fl3`) vs round 2 `f032b903…/cache.sc` | `cmp` EQUAL, sha256 `bb9d78b0…` |
| probe `hl` rebuilt (`W/rebuild/hl`, `design/probe/pipelines/hl-b3.toml`, byte-identical private inputs) vs `design/probe/out/packet-hl` | `cmp` EQUAL, sha256 `a3aba379…` |
| **Step 2: O4-r1 via `-O4`** (`W/opt/o4`, the `# o4` packet line of `design/commands.txt` verbatim) vs round-2 c7b3 | `cmp` EQUAL, sha256 `7702b19d…` |
| extra: `o4_pinned` (`--pipeline design/pipelines/O4.toml`) vs o4 | `cmp` EQUAL |

So the `enabled` key, `isb_boundary` (absent) and `hot_loop_kinds` (default) left c7b3, fl3 and
hl byte-identical, and O4-r1 inherits c7b3's measurements on packet. The probe inputs used for
hl are byte-identical to the private ones (profile `c8ed38f2…`, ELF `d638b4a0…`, runtime
`1be4119d…`; prep `diff -r` equal).

**-O4 record** (`W/opt/o4/optimize.json` key `o4`): pipeline `O4`, pipeline_id
`sha256:5e415286…`, profile_match 1.0, phase steady, snapshot icount_off and sealed true,
runtime capabilities v6 / trusted_v7 / member_skip all `capability_table` (exe `951775b0…`,
runtime_signature `0xa1cbf008b2329bbb`). Stage gate (`effective_configuration`): host-inline,
chain-returns, chain-static and firmware-inline `edited`; specialize-returns,
constant-propagation, branch-folding and dead-host-ops `no_change`; all 8 required stages
present.

## 2. Builds (`W/opt/<arm>/{command.txt,optimize.json,optimize.log,wall.log,time-v.txt}`; `P/receipts.json`)

Every line of `design/commands.txt` (packet) was built except `o4_tasks` (deferred). All 17
builds: rc 0, `published`, exactly one artifact per store. o4_pwhopr: peak RSS 1,922,900 KiB
and 210 s (limits 8 GB / 60 min; the watchdog's peak for the whole process tree was
1,911,792 KiB, `W/opt/o4_pwhopr/rss-watch.log`).

| arm | rc / wall s / peak RSS KiB | cache sha256 | same bytes as | regions / members / descriptor regions | skip-flag members / skipped B3 slots | allocated B | loader |
|---|---|---|---|---|---|---|---|
| o4 | 0 / 191 / 1,923,528 | `7702b19d324e…` | c7b3 | 48 / 5,967 / 48 | 580 / 653 | 17,141,760 | `qemu-sc-v7-trusted-firmware-regions+sc6+member-skip` |
| o4_chained | 0 / 200 / 1,923,084 | `907da326a55b…` | - | 56 / 5,161 / 56 | 524 / 595 | 16,859,136 | `qemu-sc-v7-trusted-firmware-regions+sc6+member-skip` |
| o4_isb | 0 / 199 / 1,922,324 | `ed26095ff36e…` | - | 48 / 5,952 / 48 | 574 / 647 | 17,133,568 | `qemu-sc-v7-trusted-firmware-regions+sc6+member-skip` |
| o4_rank | 0 / 199 / 1,922,780 | `c618a47e57d1…` | - | 48 / 5,967 / 48 | 580 / 653 | 17,141,760 | `qemu-sc-v7-trusted-firmware-regions+sc6+member-skip` |
| o4_hotgraph | 0 / 195 / 2,062,484 | `00c0f0562914…` | - | 48 / 4,824 / 48 | 467 / 525 | 18,550,784 | `qemu-sc-v7-trusted-firmware-regions+sc6+member-skip` |
| o4_min2 | 0 / 190 / 1,922,500 | `aac88e886515…` | - | 52 / 6,024 / 52 | 593 / 667 | 17,170,432 | `qemu-sc-v7-trusted-firmware-regions+sc6+member-skip` |
| o4_legacy | 0 / 187 / 1,923,580 | `115b27345e4a…` | - | 48 / 5,966 / 48 | 581 / 654 | 17,145,856 | `qemu-sc-v7-trusted-firmware-regions+sc6+member-skip` |
| o4_nohi | 0 / 58 / 908,660 | `a3aba37917b8…` | - | 48 / 5,967 / 48 | 580 / 653 | 17,137,664 | `qemu-sc-v7-trusted-firmware-regions+sc6+member-skip` |
| o4_nocp | 0 / 187 / 1,923,088 | `7702b19d324e…` | o4, c7b3 | 48 / 5,967 / 48 | 580 / 653 | 17,141,760 | `qemu-sc-v7-trusted-firmware-regions+sc6+member-skip` |
| o4_nospec | 0 / 185 / 1,923,124 | `7702b19d324e…` | o4, c7b3 | 48 / 5,967 / 48 | 580 / 653 | 17,141,760 | `qemu-sc-v7-trusted-firmware-regions+sc6+member-skip` |
| o4_noregions | 0 / 186 / 1,922,384 | `505484832e34…` | - | 0 / - / - | - / 0 | 14,356,480 | `qemu-sc-v7` |
| fl3b3 | 0 / 52 / 909,084 | `dfe9b96a2a5d…` | - | 15 / 120 / 15 | 36 / 46 | 14,438,400 | `qemu-sc-v7-trusted-firmware-regions+sc6+member-skip` |
| fl3b3p | 0 / 52 / 909,116 | `45f98a80de52…` | - | 3 / 15 / 3 | 7 / 7 | 14,368,768 | `qemu-sc-v7-trusted-firmware-regions+sc6+member-skip` |
| b3l | 0 / 64 / 909,008 | `54909b638fd3…` | - | 0 / - / - | - / 0 | 14,176,256 | `qemu-sc-v7` |
| o4_pwhopr | 0 / 210 / 1,922,900 | `e6a723f83269…` | - | 53 / 7,052 / 53 | 709 / 801 | 17,780,736 | `qemu-sc-v7-trusted-firmware-regions+sc6+member-skip` |
| o4_hostall | 0 / 203 / 2,066,704 | `619000e03061…` | - | 48 / 4,774 / 48 | 459 / 516 | 20,414,464 | `qemu-sc-v7-trusted-firmware-regions+sc6+member-skip` |
| o4_pinned | 0 / 185 / 1,923,612 | `7702b19d324e…` | o4, c7b3 | 48 / 5,967 / 48 | 580 / 653 | 17,141,760 | `qemu-sc-v7-trusted-firmware-regions+sc6+member-skip` |

Same bytes as o4 on packet (design 3.2: decided without timing on this workload): **o4_nocp,
o4_nospec** (the design's "probably" pairs, confirmed) and o4_pinned. o4_nohi equals the
probe's hl (`a3aba379…`): O4 minus host-inline is exactly B3 + hot-loop.

### Pass receipts (B3 rows, host-inline, firmware-inline)

| arm | host-inline applied | SR variants | CR slots | CS slots | CS refusals | fw discovered / refused | nested calls / decisions | isb exits (regions) | profit_net_ns |
|---|---|---|---|---|---|---|---|---|---|
| o4 | 1 | 0 | 47 | 256 | handler_mode_return 8, not_profitable 8, slot_budget 27, target_not_profiled 1256 | 193.0 / {'not_profitable': 33.0} | 289.0 / 65 | 0 (0) | 19945907.294 |
| o4_chained | 1 | 0 | 47 | 256 | handler_mode_return 8, not_profitable 8, slot_budget 27, target_not_profiled 1256 | 193.0 / {'not_profitable': 25.0} | 241.0 / 65 | 0 (0) | 97472425.991 |
| o4_isb | 1 | 0 | 47 | 256 | handler_mode_return 8, not_profitable 8, slot_budget 27, target_not_profiled 1256 | 193.0 / {'not_profitable': 33.0} | 289.0 / 65 | 33 (15) | 19946106.044 |
| o4_rank | 1 | 0 | 47 | 256 | handler_mode_return 8, not_profitable 19, slot_budget 16, target_not_profiled 1256 | 193.0 / {'not_profitable': 33.0} | 289.0 / 65 | 0 (0) | 19945907.294 |
| o4_hotgraph | 13 | 0 | 47 | 256 | handler_mode_return 8, not_profitable 8, slot_budget 27, target_not_profiled 1256 | 193.0 / {'not_profitable': 33.0} | 223.0 / 65 | 0 (0) | 19473221.144 |
| o4_min2 | 1 | 0 | 47 | 256 | handler_mode_return 8, not_profitable 8, slot_budget 27, target_not_profiled 1256 | 193.0 / {'not_profitable': 36.0} | 292.0 / 67 | 0 (0) | 19954480.894 |
| o4_legacy | 1 | 0 | 47 | 256 | handler_mode_return 8, not_profitable 8, slot_budget 27, target_not_profiled 1256 | 193.0 / {'not_profitable': 33.0} | 289.0 / 65 | 0 (0) | 20199520.794 |
| o4_nohi | - | 0 | 47 | 256 | handler_mode_return 8, not_profitable 8, slot_budget 27, target_not_profiled 1256 | 193.0 / {'not_profitable': 33.0} | 289.0 / 65 | 0 (0) | 19945907.294 |
| o4_nocp | 1 | 0 | 47 | 256 | handler_mode_return 8, not_profitable 8, slot_budget 27, target_not_profiled 1256 | 193.0 / {'not_profitable': 33.0} | 289.0 / 65 | 0 (0) | 19945907.294 |
| o4_nospec | 1 | - | 47 | 256 | handler_mode_return 8, not_profitable 8, slot_budget 27, target_not_profiled 1256 | 193.0 / {'not_profitable': 33.0} | 289.0 / 65 | 0 (0) | 19945907.294 |
| o4_noregions | 1 | 0 | 47 | 256 | handler_mode_return 8, not_profitable 8, slot_budget 27, target_not_profiled 1256 | - / - | - / - | - (-) | - |
| fl3b3 | - | 0 | 47 | 256 | handler_mode_return 8, not_profitable 8, slot_budget 27, target_not_profiled 1256 | 219.0 / - | 0.0 / 0 | 0 (0) | 0.0 |
| fl3b3p | - | 0 | 47 | 256 | handler_mode_return 8, not_profitable 8, slot_budget 27, target_not_profiled 1256 | 219.0 / {'not_profitable': 12.0} | 0.0 / 0 | 0 (0) | 253743.8 |
| b3l | - | 0 | 47 | 256 | handler_mode_return 8, not_profitable 8, slot_budget 27, target_not_profiled 1256 | - / - | - / - | - (-) | - |
| o4_pwhopr | 1 | 0 | 47 | 256 | handler_mode_return 8, not_profitable 8, slot_budget 27, target_not_profiled 1256 | 193.0 / {'not_profitable': 140.0, 'unpatchable_native_exit': 3.0} | 360.0 / 0 | 0 (0) | 21211645.43 |
| o4_hostall | 1,005 | 0 | 47 | 256 | exit_not_goto_ptr 4, handler_mode_return 8, not_profitable 8, slot_budget 27, target_not_profiled 1252 | 193.0 / {'not_profitable': 33.0} | 216.0 / 65 | 0 (0) | 19488473.644 |
| o4_pinned | 1 | 0 | 47 | 256 | handler_mode_return 8, not_profitable 8, slot_budget 27, target_not_profiled 1256 | 193.0 / {'not_profitable': 33.0} | 289.0 / 65 | 0 (0) | 19945907.294 |

Notes on the receipts:

* B3 rows are identical in every arm except o4_rank and o4_hostall (chain-static refusals):
  specialize-returns 0 variants wherever it runs (`skipped_unsupported` / `target_not_profiled`;
  395 return exits), chain-returns 47 slots, chain-static 256 slots, B3 grown 294.
  constant-propagation, branch-folding and dead-host-ops are `no_change` / `no_variants`
  wherever they run (o4_nospec drops all four; o4_nocp drops constant-propagation).
* **o4_rank:** params `{c_target_check: 0.0, rank_by_net_work: true, targets_per_exit: 2}`;
  slots 256; refusals not_profitable 19 (o4: 8) and slot_budget 16 (o4: 27); no
  `no_free_slot` or `inexact_lookup_traffic` refusal; `train_lookups_guarded` 10,457,199
  against o4's 10,368,220; `eligible_exit_plans` 272.
* **o4_hostall:** chain-static refuses 4 exits `exit_not_goto_ptr` and 1,252 (not 1,256)
  `target_not_profiled`; slots still 256.
* **host-inline:** o4 family 1 site (`helper_shl_cc`, 120 B). o4_hotgraph 13 sites
  (helper_udiv 7, shl_cc 2, v7m_msr 2, rebuild_hflags_m32_newel 2; 75,696 B);
  `helper_lookup_tb_ptr` applied 0 (refused `helper_graph_size_policy` 260,
  outside_observed_loop 1,928, below_min_site_calls 308, call_target_mismatch 2).
  o4_hostall 1,005 sites, refused call_target_mismatch 15,185, helper_graph_size_policy 1,975
  (all `helper_lookup_tb_ptr`), unsupported_abi 64; 1,677,808 B.
* **firmware-inline roots against o4** (`P/arms-built.json` `counts.roots_vs_o4`):
  * o4_chained: +9 (0x4a28, 0x4a70 [2,317,210 calls], 0x8241a, 0x85aba, 0x49dc, 0x81914,
    0x81a14, 0x4a02, 0x81eb4), −1 (0x820ca), 10 roots change members; nested 241 — as probe2.
  * o4_min2: +5 (0x84fda, 0x48ec [14 calls], 0x87cd4, 0x850a8, 0x172e), −1 (0x8213a); every
    added root priced (`net_ns` present) — as probe2.
  * o4_legacy: +1 (0x82066), −1 (0x828da) = 48 regions, not the design's ~50 (see Problems).
  * o4_hotgraph 2 and o4_hostall 3 roots change members (members 4,824 and 4,774 against 5,967).
  * o4_pwhopr (exhaustive, cost_model profile): 53 regions, +10 / −5 roots against o4, 140
    refused not_profitable, 3 unpatchable_native_exit (pending `no_rel32_field` 3), nested 360,
    no `nested_decisions`.
  * fl3b3: 15 regions / 120 members (as fl3); selection verdict embedded (prvRxTask 13 of 13,
    prvProcTask 2 of 27, 25 `rtos_shared_public_lookup`). fl3b3p: 3 regions — 0x48ec (memset,
    14 calls) and the two 0x4b1c roots (4 members each); 12 refused not_profitable.
* **b3l:** hotcold-partition `classified`, llvm-cache-layout `candidate` /
  `pettis_hansen_fallback` (Pettis-Hansen, not the LLVM bridge), order-cold `ordered`; sc6-only
  (`qemu-sc-v7`, reserved 0x8000000d).

## 3. Gates (design 3.2)

### G0 functional check (`P/gate.py`; `W/gates/arms/<arm>/result.json`)

Per arm, case heldout-1 (the only packet case, `W/cases.json`), using the campaign driver's own
code (`/data/scfix-o4/driver/compare_rtos.py`): `sparse_copy` of the cache, the staging
`runtime_identity_check`, `stage()`, and `replay()` (exit 0, exactly the 16 oracle words,
exactly 1 BENCH line). Three runs per arm: `public` (the driver's warm run with
`QEMU_SC_PROFILE`; qprof clean, warm, not overflowed, record count and header checksum equal to
the cache's), `regions` (`QEMU_SC_V7_STATS` only), and `v6` (`QEMU_SC_V6_STATS` +
`QEMU_SC_V7_STATS`, for every cache with sc6 bits). Checks: no forbidden log pattern; v7
counters_revision 4 and `scopt.run.v7_stats.counter_problems == []` (fd-int); with a firmware
directory also loaded, mode trusted, user_trusted_input, artifact_id8 equal to `read_sc7`,
guest_hash_checks = public_record_checksum_checks = 0, every poll entry verified
(unverified 0, verified = regions_loaded), `chain_skipped_trusted_poll_unverified` 0; v6
loaded, invariant_violations 0, flush_resets 0; staged copy size, header and sha256 equal to the
source, source sha256 unchanged.

**Result: all 17 built arms pass (`ok: true`), and so do the six anchors** (s6, s6b3, p7, c7b3,
fl3, w7bd_b3; `W/gates/anchors/<arm>/result.json`) from their private copies and retargeted
launches. Further G0 items (`P/arms-built.json` `g0`): `x_b3.firmware_region_links` refusals 0
in every arm; loader requirement `qemu-sc-v7-trusted-firmware-regions+sc6+member-skip` for
every arm with regions, `qemu-sc-v7` (sc6-only) for o4_noregions and b3l. No gate failed.

Launches of built arms: `W/arms/<arm>/launch.json`, derived from the private c7b3 launch by
`o4lib.derive_arm_launch` (the helper `make_plan.py` uses), so plans will reuse them.

### Regions replay (`regions` run; second sample from the `v6` run)

| arm | loaded / activated | lookup entries (regions run / v6 run) | chain installs into regions | residual exits (chainable / unchainable) | T->T allowed / installs / refused | slots armed / skipped | poll verified / unverified / exits | loop_returns_nonnull | v6 activations / slots / invariant viol. / flush resets |
|---|---|---|---|---|---|---|---|---|---|
| o4 | 48 / 46 | 54 / 52 | 52 | 1 (1 / 0) | 0 / 0 / 0 | 52 / 652 | 48 / 0 / 0 | 65,064 | 229 / 303 / 0 / 0 |
| o4_chained | 56 / 54 | 59 / 59 | 55 | 12 (12 / 0) | 0 / 0 / 0 | 67 / 589 | 56 / 0 / 0 | 65,251 | 215 / 303 / 0 / 0 |
| o4_isb | 48 / 46 | 52 / 54 | 52 | 1 (1 / 0) | 0 / 0 / 0 | 85 / 646 | 48 / 0 / 0 | 65,355 | 228 / 303 / 0 / 0 |
| o4_rank | 48 / 46 | 53 / 54 | 52 | 1 (1 / 0) | 0 / 0 / 0 | 52 / 652 | 48 / 0 / 0 | 65,313 | 229 / 303 / 0 / 0 |
| o4_hotgraph | 48 / 46 | 53 / 52 | 52 | 1 (1 / 0) | 0 / 0 / 0 | 48 / 524 | 48 / 0 / 0 | 65,381 | 229 / 303 / 0 / 0 |
| o4_min2 | 52 / 50 | 56 / 58 | 57 | 2 (2 / 0) | 0 / 0 / 0 | 53 / 666 | 52 / 0 / 0 | 65,345 | 226 / 303 / 0 / 0 |
| o4_legacy | 48 / 46 | 53 / 54 | 52 | 1 (1 / 0) | 0 / 0 / 0 | 52 / 652 | 48 / 0 / 0 | 64,712 | 229 / 303 / 0 / 0 |
| o4_nohi | 48 / 46 | 53 / 53 | 52 | 1 (1 / 0) | 0 / 0 / 0 | 52 / 652 | 48 / 0 / 0 | 65,233 | 229 / 303 / 0 / 0 |
| o4_nocp | 48 / 46 | 54 / 55 | 52 | 1 (1 / 0) | 0 / 0 / 0 | 52 / 652 | 48 / 0 / 0 | 65,126 | 229 / 303 / 0 / 0 |
| o4_nospec | 48 / 46 | 53 / 52 | 52 | 1 (1 / 0) | 0 / 0 / 0 | 52 / 652 | 48 / 0 / 0 | 65,106 | 229 / 303 / 0 / 0 |
| o4_noregions | 0 / 0 | 0 / 0 | 0 | 0 (0 / 0) | 0 / 0 / 0 | 0 / 0 | 0 / 0 / 0 | 65,360 | 294 / 303 / 0 / 0 |
| fl3b3 | 15 / 8 | 8 / 8 | 7 | 8 (8 / 0) | 0 / 0 / 0 | 10 / 27 | 15 / 0 / 0 | 65,291 | 283 / 303 / 0 / 0 |
| fl3b3p | 3 / 3 | 3 / 3 | 5 | 1 (1 / 0) | 0 / 0 / 0 | 3 / 7 | 3 / 0 / 0 | 65,248 | 290 / 303 / 0 / 0 |
| b3l | 0 / 0 | 0 / 0 | 0 | 0 (0 / 0) | 0 / 0 / 0 | 0 / 0 | 0 / 0 / 0 | 65,273 | 294 / 303 / 0 / 0 |
| o4_pwhopr | 53 / 50 | 3,106 / 3,157 | 55 | 2 (2 / 0) | 0 / 0 / 0 | 73 / 766 | 53 / 0 / 0 | 65,270 | 226 / 303 / 0 / 0 |
| o4_hostall | 48 / 46 | 54 / 53 | 52 | 1 (1 / 0) | 0 / 0 / 0 | 47 / 515 | 48 / 0 / 0 | 65,409 | 228 / 303 / 0 / 0 |
| o4_pinned | 48 / 46 | 53 / 53 | 52 | 1 (1 / 0) | 0 / 0 / 0 | 52 / 652 | 48 / 0 / 0 | 65,166 | 229 / 303 / 0 / 0 |
| anchor s6 | 0 / 0 | 0 / - | 0 | 0 (0 / 0) | 0 / 0 / 0 | 0 / 0 | 0 / 0 / 0 | 65,504 | - / - / - / - |
| anchor s6b3 | 0 / 0 | 0 / 0 | 0 | 0 (0 / 0) | 0 / 0 / 0 | 0 / 0 | 0 / 0 / 0 | 65,434 | 294 / 303 / 0 / 0 |
| anchor p7 | 56 / 54 | 41,680 / - | 40 | 12 (12 / 0) | 0 / 0 / 0 | 67 / 0 | 56 / 0 / 0 | 65,203 | - / - / - / - |
| anchor c7b3 | 48 / 46 | 53 / 53 | 52 | 1 (1 / 0) | 0 / 0 / 0 | 52 / 652 | 48 / 0 / 0 | 65,326 | 229 / 303 / 0 / 0 |
| anchor fl3 | 15 / 8 | 158 / - | 6 | 8 (8 / 0) | 0 / 0 / 0 | 10 / 0 | 15 / 0 / 0 | 65,301 | - / - / - / - |
| anchor w7bd_b3 | 193 / 116 | 119 / 119 | 98 | 32 (32 / 0) | 8 / 8 / 0 | 143 / 1,415 | 193 / 0 / 1 | 65,139 | 190 / 303 / 0 / 0 |

### Per-arm validity (design 3.2, packet column; `P/arms-built.json` `validity`)

| arm | check | result |
|---|---|---|
| o4 | host-inline 1, regions 48, chain-returns 47, chain-static 256, cache = c7b3 | all true |
| o4_chained | regions about 56 | 56 |
| o4_chained | A1 regions-replay gate | **entered, possibly slower**: activations 54 / 54 against 46 in all 10 replays of o4's bytes (o4, o4_nocp, o4_nospec, o4_pinned, c7b3 anchor; 2 runs each); lookup entries 59 / 59 against 52–55; residual exits 12 against 1 (`P/assess.json` `a1_gate`) |
| o4_isb | `policy.isb_boundary` true; ≥ 33 isb exits | true; 33 exits in 15 regions, all in embedded regions |
| o4_isb | nested_decisions = o4's | 65 = 65 |
| o4_isb | every member difference is a successor of an ISB-ending member | true: 13 roots lose 15 members (508→507 ×2, 509→508, 530→529, 642→640 ×2, 769→768, 126→125 ×6); each removed member's start pc (0x820ee, 0x81ed4, 0x829be) is the `guest_target` of an isb_boundary exit of that region; nothing added (`P/assess.json` `o4_isb_member_check`) |
| o4_rank | chain-static applied, not `incomplete_lookup_profile`; `c_target_check = 0.0` in its receipt | true, true |
| o4_hotgraph | about 13 sites; `helper_lookup_tb_ptr` 0; SPEC variants vs 0 | 13; 0 (helper_graph_size_policy 260); 0 |
| o4_min2 | regions about 52; every added root priced | 52; true |
| o4_legacy | regions about 50 | **48** (prediction missed; planner `legacy` confirmed in the receipt) |
| o4_nohi / o4_nocp / o4_nospec | pass(es) absent | true |
| o4_noregions | no firmware directory | true (regions 0, no firmware-inline receipt, loader `qemu-sc-v7`) |
| fl3b3 / fl3b3p | 15 / 3 regions, with member descriptors | 15/15 and 3/3 descriptor regions |
| b3l | layout passes ran | true (Pettis-Hansen fallback) |
| o4_pwhopr | ≤ 8 GB RSS, ≤ 60 min | 1.92 GB, 210 s |
| o4_hostall | about 1,005 sites | 1,005 |

## 4. Deviations from the design

1. `o4_pinned` was built into `W/opt/o4_pinned/store`, not `opt/o4/store` as in
   `commands.txt`, so o4's store keeps one artifact (`make_plan.py` refuses a store with more).
2. The gate adds two runs to the driver's pre-timing path: a `QEMU_SC_V7_STATS` run for caches
   without a firmware directory (counters revision and `counter_problems` for every arm) and a
   V6+V7 stats run (round 2's fcheck form) for sc6 caches.
3. `/usr/bin/time -v` wrapped every build, not only the heavy ones; o4_pwhopr and o4_hostall
   ran together (the two allowed) while light arms ran three at a time.
4. The anchors were also gated (not required by this step).

## 5. Problems

1. **The host-inline count that O4's one host-inline site depends on is not stable across
   builds.** `selected_site_calls` (the gdb counted replay of `helper_shl_cc`) was 8,372 (o4),
   8,514 (c7b3 rebuild), 2,344–2,438 (the six builds started together at 00:50), 3,464
   (o4_nospec), 8,651–8,969 (others); round 2 recorded 9,433. `min_site_calls` is 1,000. Every
   build still applied the site and every O4-family cache is byte-identical where expected, but a
   count below 1,000 would make `-O4` drop the site and publish o4_nohi's bytes (`a3aba379…`).
   Whether the stage gate would refuse that (host-inline would then be no_change) was not tested.
2. **o4_legacy has 48 regions, not ~50.** Probe2's legacy build was on hl (no host-inline). On
   O4's units the legacy pricing order admits 0x82066 but refuses 0x828da and 0x84db6 as
   not_profitable with 0 calls (captured by an earlier-priced region). The arm still differs
   from o4 only by the planner.
3. **o4_rank's refusals are not the ones the design predicted.** No `no_free_slot` or
   `inexact_lookup_traffic`; instead not_profitable +11 and slot_budget −11 (numbers in §2).
4. **A1 regions are entered** (§3), so A1 goes to the screen as "entered, possibly slower".
   Entries stay tiny (59 lookup entries per run).
5. o4_pwhopr's regions are entered through lookup 3,106 / 3,157 times per run against 52–55 for
   o4 (prediction input for campaign H; not a gate).
6. The infra agent's note that `decide.py`'s normal reference gives ~20% spurious BEST hits was
   not acted on here (outside this step); `decide.py` and `DECIDE.sha256` are unchanged.
7. `find -newer P/build-arm.sh` over `/data/scfix-r2`, `/data/scfix-campaign`,
   `/data/trebuchet-clean`, `/data/fastdyn`, `/data/fastdyn-work/qemu-fu`, `/data/qemu-scfix-bin`,
   `/data/scfix-o4/design` and `/data/scfix-o4/driver` printed only the directory
   `/data/fastdyn/.git/worktrees/fd-int` (its mtime, from `git status` in the fd-int worktree; no
   file in it changed).

## P4b: o4_tasks (hot-loop-tasks), fd-int b35cdb6

Date 2026-11-26. Trigger (design 3.1 P4b): A7 survived the screen, and C1p's pooled d against
o4 was -0.00696. Builds, gates and a regions replay only. **Nothing was timed.** P4b's
scripts and outputs are in `/data/scfix-o4/p4b` (`build.sh`, `gates.sh`, `tasks_validity.py`,
`<w>-validity.json`, `qemu-identity.txt`, `backup/`).

* **Code.** fd-int branch o4, commit `b35cdb6` "Add firmware-inline mode hot-loop-tasks (O4
  candidate A2a)" on top of ece9b5e. It adds the design 2.4 mode, `PolicySelection.price_after`
  and `.flat_expansion`, `firmware_inline.pricing_order`, the o4.py decision (EXCLUDED
  "unmeasured", arm o4_tasks), the docs and 14 tests. Unit suite: 24 failed (exactly the
  `/data/scfix-campaign/impl/int/base-set.txt` ids), 1940 passed, 490 skipped. The baseline
  at ece9b5e had the same 24, with 1926 passed and 490 skipped.
* **Byte spot check.** o4 rebuilt on uart at b35cdb6 equals `ae360ff7…` (`cmp` EQUAL); see
  uart VERIFY.md P4b.
* **Build.** The commands.txt packet `# o4_tasks` line, verbatim, through `P/build-arm.sh`
  (nice -n 10 taskset -c 21-31) into `W/opt/o4_tasks`: rc 0, 177 s, peak RSS 1,923,048 KiB,
  published, 1 artifact. cache.sc sha256 `5a8a9075974deafac9537e6f2d18670ffad3e9285c37dbd784cf554ee7f8f4e2`;
  **not the same bytes as o4** (`cmp` differs in 1,157,662 bytes).
  Regions / members / descriptor regions: 49 / 5,974 / 49. Members with skip flags / skipped
  B3 slots: 583 / 656. Allocated: 17,145,856 B (o4: 48 / 5,967 / 48, 580 / 653, 17,141,760 B).
  Loader requirement: `qemu-sc-v7-trusted-firmware-regions+sc6+member-skip`.
* **Receipts against o4** (`/data/scfix-o4/p4b/packet-validity.json`):
  * `policy.union`: hot 81, static 21, static_only 5, both 16.
  * One added region: root 0x48ec → 0x573e (memset; 7 members, 14 calls, `net_ns`
    1,621.3), `o4_evidence` ["static_task_loop"]. That matches the design's A2a prediction.
  * The other 4 static-only roots were refused at the root by v3's RTOS boundary
    (`rtos_shared_public_lookup`): 0x84dfa → xQueueReceive, 0x84e50 → xQueueGenericSend,
    0x84fda → xQueueSemaphoreTake (964 observed calls) and 0x850a8 → xQueueGenericSend
    (964). 0x84fda and 0x850a8 are two of o4_min2's (A7) added roots.
  * **All 48 o4 roots are embedded unchanged.** Region ids, callsite ids, members and
    source_uids are equal; the largest `net_ns` difference is 0.0.
  * `nested_decisions` equal (65 = 65, full rows). Nested inlines 289 = 289. Refused
    not_profitable 33 = 33. `profit_net_ns` 19,945,907.294 → 19,947,528.594 (+1,621.3).
  * Every other pass has o4's outcome, reason, edit digest and stats, with one exception:
    host-inline's gdb-counted `selected_site_calls` is 8,200 here against o4's 8,372
    (problem 1 above). Its edit digest is equal, and it applied 1 site.
* **G0** (`P/gate.py` → `W/gates/arms/o4_tasks/result.json`; `P/assess.py` →
  `P/arms-built.json`): **all pass.**
  * The public, regions and v6 runs: exit 0, 16 words, 1 BENCH, no forbidden pattern.
  * qprof warm identity ok; runtime identity ok (QEMU sha256 = the artifact's `951775b0…`).
  * Counters revision 4, `counter_problems` [], every poll entry verified (49 / 0),
    `chain_skipped_trusted_poll_unverified` 0.
  * v6: invariant_violations 0, flush_resets 0.
  * Cache unchanged; `x_b3.firmware_region_links` refusals 0; loader requirement ok.
* **Validity** (design 3.2 o4_tasks): union counts present; every o4 root embedded with the
  same members, source_uids and `profit.net_ns` (within max(1 ns, 1e-6 rel)); nested_decisions
  equal o4's. All true, plus the extra checks (mode, hot-loop evidence equal o4's, added roots
  static-only, other passes equal).
* **Regions replay** (o4 in brackets):

  | metric | o4_tasks | o4 |
  |---|---|---|
  | loaded / activated | 49 / 47 | 48 / 46 |
  | lookup entries, regions run / v6 run | 54 / 55 | 54 / 52 (52–55 over every replay of o4's bytes) |
  | chain installs into regions | 54 | 52 |
  | residual exits | 2 (2 chainable / 0) | 1 |
  | slots armed / skipped | 53 / 655 | 52 / 652 |
  | poll verified / unverified / exits | 49 / 0 / 0 | – |
  | loop_returns_nonnull | 65,301 | – |
  | v6 activations / slots / invariant viol. / flush resets | 227 / 303 / 0 / 0 | – |

  The added memset region is activated, and lookup entries stay inside o4's own range.
* **Records.** Every record keeps its other arms unchanged; the old files are in
  `/data/scfix-o4/p4b/backup/`.
  * `P/receipts.json` gains `opt/o4_tasks`. `P/arms-built.json` gains `o4_tasks` (gates_ok
    true). `P/assess.json` gains `o4_tasks_validity`. No other key changed.
  * `P/assess.py` gained `o4_tasks` in ARMS and its validity branch, which calls
    `/data/scfix-o4/p4b/tasks_validity.py`.
  * `/data/scfix-o4/driver/make_gates.py` added o4_tasks to BUILT. Its sha256 went from
    `1b735d59…` (PREREG.md's) to `19f228d9…`, and decide.py is unchanged. The driver
    tests pass (60 passed).
  * The rerun rewrote `/data/scfix-o4/gates/{packet,uart,cli,all}.json`. o4_tasks has g0 true
    on all three workloads (all.json g0 true). For the 17 other arms only the evidence strings
    changed: they quote the sha256 of the regenerated record files. Their g0, checks and
    cache sha256 are unchanged.
* **QEMU** `/data/qemu-scfix-bin/aacca05/qemu-system-arm`: sha256 `951775b0…eebc60`,
  inode 87162887, size 137,799,456, mtime 1795640690, before and after (`p4b/qemu-identity.txt`).
* **For A2.** o4_tasks differs from o4 only on packet, so it is timed on packet only. It
  has the same bytes as o4 on uart and cli, which are decided without timing (design 3.2).

## P5: o4r2 (O4-r2), fd-int b35cdb6

Date 2026-11-26. P5 decisions (O4-design 3.7) and the O4-r2 build (3.8 Composition). Builds, functional checks and a regions replay only. **Nothing was timed.** P5's scripts and outputs are in `/data/scfix-o4/p5`, and the decisions are in `/data/scfix-o4/decisions.json`.

* **Composition.** O4-r2 = O4-r1 + **A5 absorbed** (rule BEST: o4_pwhopr was faster than O4 in campaign H, cli d +1.97% and pooled +0.93%) + **I1** (isb_boundary = true, FIDELITY) + **X1** (host-inline removed, REMOVE) + **X4** (SPEC bundle removed, REMOVE). X2 is accepted but moot under X4, X3 is rejected, and no add-on passed ADD.
  The passes are chain-returns, chain-static and firmware-inline `{mode exhaustive, cost_model profile, phase steady, isb_boundary true, trust user, planner whopr, partitions 4, workers 4, member_slot_skip on}`.
* **Pipeline.** The revision file is `/data/scfix-o4/design/pipelines/O4-r2.toml` (name "O4-r2", sha256 `1ee63d43790eba14…`).
  * The arm was built from its copy `design/pipelines/o4r2.toml` (sha256 `4ea2b72005203d51…`), which differs only in name ("o4r2") and description (`p5/make_arm_copy.py`; tomllib and scopt `load_pipeline` agree).
  * **Deviation:** `--pipeline O4-r2.toml` itself fails in scopt's O4 preflight before any pass, rc 2: `pipeline 'O4-r2' is not the packaged /data/fastdyn-work/fd-int/tools/scopt/pipelines/O4-r2.toml; O4 is fixed: copy O4-rN.toml under another name to experiment`. That attempt is kept in `p5/failed/packet-o4r2.attempt1`.
  * The pipeline name does not reach cache.sc: O4-r1 via -O4 equals c7b3 byte for byte (section 1). O4.toml and -O4 are not repointed before campaign B.
* **Build.** `p5/o4r2-commands.txt` packet line: the `# o4_nohi` line of commands.txt with only `--pipeline` and `--store opt/o4r2/store` changed, so there is no `--pgo-launch`. It ran through this workload's own wrapper (nice -n 10 taskset -c 21-31, /usr/bin/time -v, timeout 3600) at fd-int b35cdb6, with no code change in P5.
  * rc 0, 62.0 s, peak RSS 937,892 KiB (time -v); the watchdog's process-tree peak was 1,829,088 KiB (limits 8 GiB / 60 min). The artifact was published, 1 in the store.
  * cache.sc sha256 `805e430a84114bcaad67c1407811a7629113a9a4c9d3d27ece06ec6b258411ec`. Regions / members / descriptor regions: 53 / 7,036 / 53 (o4_pwhopr 53 / 7,052 / 53; o4 48 / 5,967 / 48).
  * Allocated 17,768,448 B (o4_pwhopr 17,780,736, o4 17,141,760). Reserved 0xc000000d (o4_pwhopr 0xc000000d, o4 0xc000000d). Loader requirement `qemu-sc-v7-trusted-firmware-regions+sc6+member-skip`.
* **Stage-gate equivalent** (fd-int `required_stage_problems` and `effective_configuration` with `gate_status` over O4-r2's stages): chain-returns edited, chain-static edited, firmware-inline edited; problems [].
* **B3 rows.** o4r2: chain-returns 47 slots, refused {'exit_not_goto_ptr': 7, 'not_profitable': 7, 'target_not_profiled': 343}; chain-static 256 slots, refused {'handler_mode_return': 8, 'not_profitable': 8, 'slot_budget': 27, 'target_not_profiled': 1256}.
  * They equal o4_nospec's: True. o4: chain-returns 47 slots, refused {'exit_not_goto_ptr': 7, 'not_profitable': 7, 'target_not_profiled': 343}; chain-static 256 slots, refused {'handler_mode_return': 8, 'not_profitable': 8, 'slot_budget': 27, 'target_not_profiled': 1256}.
* **Firmware-inline** (o4r2 / o4_pwhopr / o4):
  * embedded 53 / 53 / 48; nested inlines 360 / 360 / 289
  * refused {'not_profitable': 140.0, 'unpatchable_native_exit': 3.0} / {'not_profitable': 140.0, 'unpatchable_native_exit': 3.0} / {'not_profitable': 33.0}
  * profit_net_ns 21211886.93 / 21211645.43 / 19945907.294
  * isb_boundary exits (regions) 36 (17) / 0 / 0
  * Roots against o4_pwhopr: added [], dropped [], members changed 14, common 53.
  * Roots against o4: added ['0x8299a', '0x84fda', '0x308a', '0x48ec', '0x8241a', '0x850a8', '0x80e28', '0x172e', '0x159a', '0x8295a'], dropped ['0x81e68', '0x828da', '0x8213a', '0x820ca', '0x82618'], members changed 14.
  * **ISB check against A5** (`p5/packet-o4r2-isb-vs-pwhopr.json`, with assess.py's `isb_member_check`): 14 roots lose 16 members, and each removed member is the guest target of an isb_boundary exit of its own region: True. All 36 exits are in embedded regions: True.
* **G0** (`gates/packet.json` `o4r2`, regenerated by `driver/make_gates.py`): **all pass** (17 checks).
  * The functional runs exit 0 with the 16 oracle words, 1 BENCH line and no forbidden pattern.
  * Runtime identity: the QEMU sha256 equals the artifact's `951775b0…`.
  * Counters revision 4, `counter_problems` [], every poll entry verified.
  * v6: invariant_violations 0 and flush_resets 0. Cache unchanged; region-link refusals 0; loader requirement ok.
* **Validity** (`p5/packet-o4r2-validity.json`, shared checker `p5/o4r2_validity.py`): pipeline_copy_equals_O4_r2 true, passes_are_O4_r2 true, stage_gate_all_edited_or_no_change true, no_host_inline_receipt true, spec_passes_absent true, firmware_inline_exhaustive_profile true, isb_boundary_true true, member_slot_skip_enabled true, build_within_8GiB_60min true.
* **Same bytes** (`cmp` of cache.sc): o4 differs, o4_isb differs, o4_nohi differs, o4_nospec differs, o4_pwhopr differs. The workload aggregator also compared it with every other built arm and the anchors: no match. o4r2 is not the same bytes as o4 or as any accepted single-item arm, so it is timed on this workload in B1.
* **Regions replay** (single untimed runs):

  | arm | case | loaded | activated | lookup entries | residual exits | T->T installs | slots armed / skipped | poll verified / unverified | loop_returns_nonnull |
  |---|---|---|---|---|---|---|---|---|---|
  | o4r2 | heldout-1 | 53 | 50 | 3115 / 2981 | 2 | 0 | 108 / 760 | 53 / 0 | 65,290 |
  | o4_pwhopr | heldout-1 | 53 | 50 | 3106 / 3157 | 2 | 0 | 73 / 766 | 53 / 0 | 65,270 |
  | o4 | heldout-1 | 48 | 46 | 54 / 52 | 1 | 0 | 52 / 652 | 48 / 0 | 65,064 |

  `lookup entries` is the regions run / the v6 run. Launch: `/data/scfix-o4/packet-w/arms/o4r2/launch.json`, derived by the gate script through `o4lib.derive_arm_launch` (the private c7b3 launch with o4r2's cache).
* **Records.** `P/receipts.json` gains `opt/o4r2`, `P/arms-built.json` gains `o4r2`, and `P/assess.json` gains `o4r2_validity`. `P/assess.py` gained `o4r2` in ARMS and its validity branch (the shared checker).
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
  * e.g. o4r3b: `scopt optimize /data/scfix-o4/packet-w/in/snapshots/23ed28c68f58fbdcb9175908f2c337521be90f67a6258b882dba82c9b9880850 --pipeline /data/scfix-o4/design/pipelines/o4r3b.toml --profile /data/scfix-o4/packet-w/in/merged.profile.json --prep /data/scfix-o4/packet-w/in/prep --elf /data/scfix-o4/packet-w/in/fw.elf --runtime-identity /data/scfix-o4/packet-w/in/runtime.json --pgo-launch /data/scfix-o4/packet-w/launch/launch.json --v6-exe sha256:951775b0300ea1421f32565af8f677c1eaee14e7e1de6238d32bdbe925eebc60 --v7-trusted-exe sha256:951775b0300ea1421f32565af8f677c1eaee14e7e1de6238d32bdbe925eebc60 --v7-member-skip-exe sha256:951775b0300ea1421f32565af8f677c1eaee14e7e1de6238d32bdbe925eebc60 --store /data/scfix-o4/packet-w/opt/o4r3b/store --json`
* **Builds**:
  | arm | rc | wall s | peak RSS KiB (time -v / watchdog) | cache.sc sha256 | regions / members / descriptor regions | allocated B | loader |
  |---|---|---|---|---|---|---|---|
  | o4r3b | 0 | 62.0 | 938,528 / 1,765,020 | `df826da59c0b27e06632baa44c550cfcc575e6442c8f77ce95bda12a11f7debc` | 53 / 7052 / 53 | 17,776,640 | `qemu-sc-v7-trusted-firmware-regions+sc6+member-skip` |
  | o4r3c | 0 | 183.0 | 1,923,484 / 3,828,960 | `8ecd5af04b1f742b85b352cb2c46a0d239b54859fd617a367c4e7df75ce9b0a9` | 53 / 7052 / 53 | 17,776,640 | `qemu-sc-v7-trusted-firmware-regions+sc6+member-skip` |
  | o4r3d | 0 | 62.0 | 937,460 / 1,843,732 | `1260bf7939e2cf166f903af831f5d819950f9d476415137e27ac1f5695f1574b` | 53 / 7052 / 53 | 17,776,640 | `qemu-sc-v7-trusted-firmware-regions+sc6+member-skip` |
* **Receipts** (passes: outcome, reason, edits):
  * o4r3b: chain-returns candidate 47; chain-static candidate 256; firmware-inline candidate firmware_regions_emitted 53
    * firmware-inline: embedded 53, members 7052, nested inlines 360, refused {'not_profitable': 140.0, 'unpatchable_native_exit': 3.0}, profit_net_ns 21211675.43 (o4_pwhopr: 53, 7052, 360, {'not_profitable': 140.0, 'unpatchable_native_exit': 3.0}, 21211645.43); roots against o4_pwhopr: added [], dropped [], members changed 0, common 53
    * chain rows: chain-returns 47 slots refused {'exit_not_goto_ptr': 7, 'not_profitable': 7, 'target_not_profiled': 343}; chain-static 256 slots refused {'handler_mode_return': 8, 'not_profitable': 8, 'slot_budget': 27, 'target_not_profiled': 1256} (o4_pwhopr: chain-returns 47 {'exit_not_goto_ptr': 7, 'not_profitable': 7, 'target_not_profiled': 343}; chain-static 256 {'handler_mode_return': 8, 'not_profitable': 8, 'slot_budget': 27, 'target_not_profiled': 1256}; o4_rank chain-static: 256 {'handler_mode_return': 8, 'not_profitable': 19, 'slot_budget': 16, 'target_not_profiled': 1256})
    * stage-gate equivalent (`required_stage_problems`, `gate_status`): {'chain-returns': 'edited', 'chain-static': 'edited', 'firmware-inline': 'edited'}; problems []
  * o4r3c: host-inline candidate native_calls_spliced 1; specialize-returns skipped_unsupported target_not_profiled 0; constant-propagation no_change no_variants 0; branch-folding no_change no_variants 0; dead-host-ops no_change no_variants 0; chain-returns candidate 47; chain-static candidate 256; firmware-inline candidate firmware_regions_emitted 53
    * host-inline applied 1 (candidate, native_calls_spliced); site profile `sha256:bf94ca17e13936428363fe9419b1ee339dbc923904e2324eb918b1158b91e9a8` (input plumbing, not compared)
    * firmware-inline: embedded 53, members 7052, nested inlines 360, refused {'not_profitable': 140.0, 'unpatchable_native_exit': 3.0}, profit_net_ns 21211645.43 (o4_pwhopr: 53, 7052, 360, {'not_profitable': 140.0, 'unpatchable_native_exit': 3.0}, 21211645.43); roots against o4_pwhopr: added [], dropped [], members changed 0, common 53
    * chain rows: chain-returns 47 slots refused {'exit_not_goto_ptr': 7, 'not_profitable': 7, 'target_not_profiled': 343}; chain-static 256 slots refused {'handler_mode_return': 8, 'not_profitable': 19, 'slot_budget': 16, 'target_not_profiled': 1256} (o4_pwhopr: chain-returns 47 {'exit_not_goto_ptr': 7, 'not_profitable': 7, 'target_not_profiled': 343}; chain-static 256 {'handler_mode_return': 8, 'not_profitable': 8, 'slot_budget': 27, 'target_not_profiled': 1256}; o4_rank chain-static: 256 {'handler_mode_return': 8, 'not_profitable': 19, 'slot_budget': 16, 'target_not_profiled': 1256})
    * stage-gate equivalent (`required_stage_problems`, `gate_status`): {'host-inline': 'edited', 'specialize-returns': 'no_change', 'constant-propagation': 'no_change', 'branch-folding': 'no_change', 'dead-host-ops': 'no_change', 'chain-returns': 'edited', 'chain-static': 'edited', 'firmware-inline': 'edited'}; problems []
  * o4r3d: chain-returns candidate 47; chain-static candidate 256; firmware-inline candidate firmware_regions_emitted 53
    * firmware-inline: embedded 53, members 7052, nested inlines 360, refused {'not_profitable': 140.0, 'unpatchable_native_exit': 3.0}, profit_net_ns 21211675.43 (o4_pwhopr: 53, 7052, 360, {'not_profitable': 140.0, 'unpatchable_native_exit': 3.0}, 21211645.43); roots against o4_pwhopr: added [], dropped [], members changed 0, common 53
    * chain rows: chain-returns 47 slots refused {'exit_not_goto_ptr': 7, 'not_profitable': 7, 'target_not_profiled': 343}; chain-static 256 slots refused {'handler_mode_return': 8, 'not_profitable': 19, 'slot_budget': 16, 'target_not_profiled': 1256} (o4_pwhopr: chain-returns 47 {'exit_not_goto_ptr': 7, 'not_profitable': 7, 'target_not_profiled': 343}; chain-static 256 {'handler_mode_return': 8, 'not_profitable': 8, 'slot_budget': 27, 'target_not_profiled': 1256}; o4_rank chain-static: 256 {'handler_mode_return': 8, 'not_profitable': 19, 'slot_budget': 16, 'target_not_profiled': 1256})
    * stage-gate equivalent (`required_stage_problems`, `gate_status`): {'chain-returns': 'edited', 'chain-static': 'edited', 'firmware-inline': 'edited'}; problems []
* **G0** (`gates/packet.json`, regenerated by `driver/make_gates.py`):
  * o4r3b: g0 **True** (17/17 checks true), cache sha256 `df826da59c0b27e0…`
  * o4r3c: g0 **True** (17/17 checks true), cache sha256 `8ecd5af04b1f742b…`
  * o4r3d: g0 **True** (17/17 checks true), cache sha256 `1260bf7939e2cf16…`
  * The functional runs exit 0 with the 16 oracle words, 1 BENCH line and no forbidden pattern; the launched QEMU's sha256 equals the artifact's `951775b0…`; counters revision 4, `counter_problems` [], every poll entry verified; sc6 invariant_violations 0 and flush_resets 0; cache unchanged; region-link refusals 0; loader requirement `qemu-sc-v7-trusted-firmware-regions+sc6+member-skip`.
* **Validity** (`r3/<w>-<arm>-validity.json`, shared checker `r3/r3_validity.py`):
  * o4r3b: passes_are_pipeline true, stage_gate_all_edited_or_no_change true, host_inline_as_pipeline true, spec_as_pipeline true, firmware_inline_exhaustive_profile true, isb_boundary_off true, member_slot_skip_enabled true, chain_static_as_pipeline true, build_within_8GiB_60min true
  * o4r3c: passes_are_pipeline true, stage_gate_all_edited_or_no_change true, host_inline_as_pipeline true, spec_as_pipeline true, firmware_inline_exhaustive_profile true, isb_boundary_off true, member_slot_skip_enabled true, chain_static_as_pipeline true, build_within_8GiB_60min true
  * o4r3d: passes_are_pipeline true, stage_gate_all_edited_or_no_change true, host_inline_as_pipeline true, spec_as_pipeline true, firmware_inline_exhaustive_profile true, isb_boundary_off true, member_slot_skip_enabled true, chain_static_as_pipeline true, build_within_8GiB_60min true
* **Same bytes** (`cmp` of cache.sc against every other build in opt/ and rebuild/ and every anchor on this workload):
  * o4r3b: no match
  * o4r3c: no match
  * o4r3d: no match
* **Regions replay** (single untimed runs; o4_pwhopr and o4 from the earlier gates):

  | arm | case | loaded | activated | lookup entries (regions / v6 run) | residual exits | T->T installs | slots armed / skipped | poll verified / unverified | loop_returns_nonnull |
  |---|---|---|---|---|---|---|---|---|---|
  | o4r3b | heldout-1 | 53 | 50 | 3115 / 3094 | 2 | 0 | 73 / 766 | 53 / 0 | 65,297 |
  | o4r3c | heldout-1 | 53 | 50 | 3144 / 2966 | 2 | 0 | 73 / 764 | 53 / 0 | 65,318 |
  | o4r3d | heldout-1 | 53 | 50 | 3022 / 2953 | 2 | 0 | 73 / 764 | 53 / 0 | 65,234 |
  | o4_pwhopr | heldout-1 | 53 | 50 | 3106 / 3157 | 2 | 0 | 73 / 766 | 53 / 0 | 65,270 |
  | o4 | heldout-1 | 48 | 46 | 54 / 52 | 1 | 0 | 52 / 652 | 48 / 0 | 65,064 |

* **Records.** The workload's receipts / arms-built records gain o4r3b, o4r3c and o4r3d; the aggregator gained their validity branch (the shared checker) and annotations. Every other arm's entry is unchanged, checked key by key against the backups in `/data/scfix-o4/r3/backup/`, except `same_bytes_as` lists that now name the new builds (uart: o4_pwhopr gains o4r3c and rebuild/o4r3a; cli: o4_pwhopr gains o4r3c, o4r2 gains o4r3b and o4r3d).
  * `driver/make_gates.py` added o4r3b, o4r3c, o4r3d to BUILT (sha256 `4de960ae9438d140…` → `22858e5cd7f21d40…`); decide.py is unchanged (`0b724747…`). The rerun rewrote `gates/{packet,uart,cli,all}.json`; for the other arms g0, checks and cache sha256 are unchanged (only evidence strings that quote the regenerated records' sha256 changed).
* **QEMU** `/data/qemu-scfix-bin/aacca05/qemu-system-arm`: sha256 `951775b0…eebc60`, inode 87162887, size 137,799,456, mtime 1795640690, the same before the builds, after the builds and after the gates (`r3/qemu-identity.txt`).
