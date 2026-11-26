# cli (FreeRTOS+CLI, FirmBOLT -O4): scopt O4 builds and gates (O4-design 3.1 P0-P2)

Nothing was timed. Every QEMU run here is one untimed functional or diagnostic run; BENCH lines
were only counted. The wall times below are build times, measured while two other agents'
builds shared CPUs 21-31, so they are rough.

## Setup

| item | value | source |
|---|---|---|
| scopt/FastDyn | `/data/fastdyn-work/fd-int` branch o4 at `ece9b5e284b8ef11339515996e4090936b409449`, unchanged during this work | `git rev-parse HEAD` |
| QEMU | `/data/qemu-scfix-bin/aacca05/qemu-system-arm`, sha256 `951775b0…bc60` before and after; stat dev 2050, ino 87162887, size 137,799,456 | `sha256sum`, `stat` |
| environment | `/data/scfix-o4/cli-w/env.sh` (infra agent); `QEMU_SC_*` unset before each build | `cli/build.sh` |
| inputs | `/data/scfix-o4/cli-w/in/*`, `launch/launch.json`, `cases.json` (2 cases: commands, help, 16 oracle words each) | `cli-w/SETUP.json` |
| commands | `/data/scfix-o4/design/commands.txt`, cli section, verbatim; materialized as `cli/cmds.json` by `cli/make_cmds.py` | `cli/cmds.json` |
| build runner | `cli/build.sh ARM`: `setsid timeout 3600 /usr/bin/time -v nice -n 10 taskset -c 21-31 python -B -m scopt …`, plus an RSS watchdog that kills the build above 8 GiB. It writes `command.txt`, `optimize.json`, `optimize.log`, `time-v.txt`, `wall.log` and `rss-watch.log` | `cli/build.sh` |
| concurrency | Phase 1: 5 builds at once (`cli/build-phase1.log`). Phase 2: the 2 heavy arms together (`cli/build-heavy.log`) and light arms 3 at a time (`cli/build-light.log`). Never more than 2 heavy builds | logs |
| gates | `cli/gates.py` (receipts, functional check, regions replay), `cli/gate_queue.sh`, `cli/gates-queue.log`, `cli/summarize.py` | – |

Per arm, the outputs are:

- `cli-w/opt/<arm>/`: the build;
- `cli-w/receipts/<arm>.json`: the receipt summary;
- `cli-w/arms/<arm>/launch.json`: the launch, derived by `o4lib.derive_arm_launch`, as `make_plan.py` would derive it;
- `cli-w/fcheck/<arm>/result.json`: the functional check;
- `cli-w/regions/<arm>/result.json`: the regions replay.

The rebuilds are in `cli-w/rebuild/{c7b3,fl3,hl}/`.

## 1. Rebuild check (1.7 step 1): PASS, no byte change

The commands are round 2's `command.txt` (`/data/scfix-r2/cli-w/opt/{c7b3,fl3}/command.txt`)
and the probe's (`/data/scfix-o4/design/probe/out/cli-hl/command.txt`). Only the input paths
were changed, to point at the private copies. The c7b3 line keeps round 2's `--enable` flags
and `--pgo-phase steady@0x82fc4`.

The private inputs are byte-identical to the probe's: sha256 of `merged.profile.json`
`009bba97…`, `fw.elf` `8c2aed59…` and `runtime.json` `4dba06b6…` are equal, and
`diff -r`/`diff -rq` of `prep` and of the snapshot directory are empty. The only `diff`
message was that `qmp.sock` is a socket in both trees.

| rebuild | rc | wall s | cache sha256 | reference | `cmp` |
|---|---|---|---|---|---|
| c7b3 | 0 | 184.9 | `869f92952417c6268fcf0f558b3f2aad7d4e79aea79e244f06c9509e24bc40f2` | `cli-w/arms/c7b3/artifact/cache.sc`, round 2's c7b3 (`SETUP.json` anchors) | equal |
| fl3 | 0 | 33.3 | `a1b3783c24a25bf2e2c60e2cb659c468d2ed9048c08855eab4129b16528f278d` | `cli-w/arms/fl3/artifact/cache.sc`, round 2's fl3 | equal |
| hl (probe) | 0 | 54.6 | `9a0dbd6d4271df898c8e8dfe5d165e0e85109dbed7c72aebc38610f852aca356` | `design/probe/out/cli-hl/store/artifacts/7d795e28…/cache.sc` | equal |

Source: `cli/build-phase1.log`, `cli-w/rebuild/*/wall.log`, `arms-built.json` `rebuild_check`.

## 2. Identity check (1.7 step 2): PASS

- **`o4`**, built with the `-O4` line, has cache.sc sha256 `869f9295…40f2`. `cmp` against
  c7b3's is equal, so on cli O4-r1 inherits c7b3's measurements.
- **`o4_pinned`**, built with `--pipeline /data/scfix-o4/design/pipelines/O4.toml`, has the same
  bytes.
- **The `-O4` result's `o4` block** (`cli-w/opt/o4/optimize.json`):
  - pipeline `O4`, pipeline_id `sha256:5e415286…`;
  - profile_match 1.0;
  - snapshot icount_off true and sealed true;
  - runtime exe `951775b0…`, and v6, trusted_v7 and member_skip all from `capability_table`;
  - runtime_signature `0xa1cbf008b2329bbb`;
  - 8 required stages.
- **Stage gate.** In `effective_configuration`, host-inline is `no_change`, and the other
  7 stages are `edited`.
- **Validity gate for `o4`** (design 3.2, cli: host-inline 0, regions 7, chain-returns 28,
  chain-static 43, bytes equal to c7b3's): host-inline 0 (`no_eligible_native_calls`),
  7 regions, CR 28, CS 43, bytes equal. **PASS.**

## 3. Builds (P2): 17 builds, all rc 0 and published; o4_tasks deferred, not built

| arm | role | rc | wall s | peak RSS KiB | cache sha256 | same bytes as | reserved | regions / members / member-desc regions | allocated B |
|---|---|---|---|---|---|---|---|---|---|
| o4 | O4-r1 (-O4) | 0 | 191.0 | 2,054,764 | `869f92952417c626…` | c7b3, o4_pinned, o4_chained, o4_isb, o4_rank, o4_legacy | 0xc000000f | 7 / 820 / 7 | 16,154,624 |
| o4_pinned | O4-r1 (--pipeline design O4.toml) | 0 | 191.0 | 2,053,592 | `869f92952417c626…` | c7b3, o4, o4_chained, o4_isb, o4_rank, o4_legacy | 0xc000000f | 7 / 820 / 7 | 16,154,624 |
| o4_chained | A1 | 0 | 200.1 | 2,053,328 | `869f92952417c626…` | c7b3, o4, o4_pinned, o4_isb, o4_rank, o4_legacy | 0xc000000f | 7 / 820 / 7 | 16,154,624 |
| o4_isb | I1 | 0 | 200.1 | 2,053,936 | `869f92952417c626…` | c7b3, o4, o4_pinned, o4_chained, o4_rank, o4_legacy | 0xc000000f | 7 / 820 / 7 | 16,154,624 |
| o4_rank | A3 | 0 | 181.9 | 2,053,860 | `869f92952417c626…` | c7b3, o4, o4_pinned, o4_chained, o4_isb, o4_legacy | 0xc000000f | 7 / 820 / 7 | 16,154,624 |
| o4_hotgraph | A4 | 0 | 194.0 | 2,235,796 | `1642b5a88e01eb10…` | - | 0xc000000f | 7 / 820 / 7 | 16,195,584 |
| o4_min2 | A7 | 0 | 197.1 | 2,053,348 | `039ffe30d82c1243…` | - | 0xc000000f | 13 / 1450 / 13 | 16,437,248 |
| o4_legacy | A8 | 0 | 178.9 | 2,054,732 | `869f92952417c626…` | c7b3, o4, o4_pinned, o4_chained, o4_isb, o4_rank | 0xc000000f | 7 / 820 / 7 | 16,154,624 |
| o4_nohi | X1 | 0 | 51.5 | 1,026,168 | `9a0dbd6d4271df89…` | probe hl, rebuild_hl | 0xc000000f | 7 / 820 / 7 | 16,154,624 |
| o4_nocp | X2 | 0 | 178.8 | 2,053,868 | `7622b90dd0b8895c…` | - | 0xc000000f | 7 / 820 / 7 | 16,154,624 |
| o4_nospec | X4 | 0 | 175.8 | 2,055,132 | `accc25ae517b8f33…` | - | 0xc000000d | 7 / 820 / 7 | 16,154,624 |
| o4_noregions | X3 | 0 | 184.9 | 2,054,700 | `a3b919070ebfe799…` | s6b3 (round-2 anchor) | 0x8000000f | 0 / 0 / 0 | 15,765,504 |
| fl3b3 | C1 | 0 | 51.5 | 1,025,660 | `fc935ad41286f2d4…` | probe2 cli-c1 | 0xc000000f | 5 / 407 / 5 | 15,974,400 |
| fl3b3p | C1p | 0 | 51.5 | 1,026,168 | `ec1febf25e52a9fd…` | probe2 cli-c1p | 0xc000000f | 2 / 196 / 2 | 15,876,096 |
| b3l | C2 (optional) | 0 | 63.7 | 1,026,076 | `87888a11f984879a…` | - | 0x8000000f | 0 / 0 / 0 | 15,724,544 |
| o4_pwhopr | A5 (heavy) | 0 | 203.2 | 2,054,076 | `cdb87351477dbdd1…` | - | 0xc000000f | 14 / 1470 / 14 | 16,449,536 |
| o4_hostall | A6 (heavy) | 0 | 215.3 | 2,245,140 | `763be97b20e979ea…` | - | 0xc000000f | 7 / 820 / 7 | 20,172,800 |

Every cache is 2,151,199,792 B apparent with 35,102 records. Full sha256 values are in
`arms-built.json`.

Sources:

- rc and wall: `cli-w/opt/<arm>/wall.log`;
- peak RSS: `time-v.txt`;
- sha256, reserved and allocated: `cli-w/receipts/<arm>.json` `artifact`;
- regions: the decoded v7 directory.

The probe2 matches were checked with `sha256sum` of `design/probe2/out/cli-c1` and
`cli-c1p`. The pipeline differences there are only `enabled = true` against `--enable`, and
`member_slot_skip = "on"` against allowlisted `auto`.

### Receipts (B3 rows, host-inline, firmware-inline)

| arm | host-inline applied (outcome) | SR variants | CP / BF / DHO patches | CR slots | CS slots (refusals) | fw-inline embedded / discovered / nested | fw refusals | profit_net_ns | isb_boundary (exits) | nested_decisions |
|---|---|---|---|---|---|---|---|---|---|---|
| o4 | 0 (no_change) | 1 | 1 / 1 / 1 | 28 | 43 (handler_mode_return 19, not_profitable 1, target_not_profiled 1613) | 7 / 208 / 19 | not_profitable 8 | 1,276,456.1 | False (0) | 10 |
| o4_pinned | 0 (no_change) | 1 | 1 / 1 / 1 | 28 | 43 (same as o4) | 7 / 208 / 19 | not_profitable 8 | 1,276,456.1 | False (0) | 10 |
| o4_chained | 0 (no_change) | 1 | 1 / 1 / 1 | 28 | 43 (same as o4) | 7 / 208 / 19 | not_profitable 8 | 1,273,642.1 | False (0) | 10 |
| o4_isb | 0 (no_change) | 1 | 1 / 1 / 1 | 28 | 43 (same as o4) | 7 / 208 / 19 | not_profitable 8 | 1,276,456.1 | True (0); 35 ISB-ending sources | 10 |
| o4_rank | 0 (no_change) | 1 | 1 / 1 / 1 | 28 | 43 (same as o4); params rank_by_net_work true, c_target_check 0.0 | 7 / 208 / 19 | not_profitable 8 | 1,276,456.1 | False (0) | 10 |
| o4_hotgraph | 2 (candidate): helper_udiv ×2; helper_lookup_tb_ptr refused ×41 `helper_graph_size_policy`; growth 13,920 B | 1 | 1 / 1 / 1 | 28 | 43 (same as o4) | 7 / 208 / 19 | not_profitable 8 | 1,269,496.1 | False (0) | 10 |
| o4_min2 | 0 (no_change) | 1 | 1 / 1 / 1 | 28 | 43 (same as o4) | 13 / 208 / 30 | not_profitable 15 | 1,591,514.9 | False (0) | 10 |
| o4_legacy | 0 (no_change) | 1 | 1 / 1 / 1 | 28 | 43 (same as o4) | 7 / 208 / 19 | not_profitable 8 | 1,641,491.7 | False (0) | 10 |
| o4_nohi | absent | 1 | 1 / 1 / 1 | 28 | 43 (same as o4) | 7 / 208 / 19 | not_profitable 8 | 1,276,456.1 | False (0) | 10 |
| o4_nocp | 0 (no_change) | 1 | – / 1 / 1 | 28 | 43 (same as o4) | 7 / 208 / 19 | not_profitable 8 | 1,276,456.1 | False (0) | 10 |
| o4_nospec | 0 (no_change) | – | – / – / – | **29** | 43 (same as o4) | 7 / 208 / 19 | not_profitable 8 | 1,276,456.1 | False (0) | 10 |
| o4_noregions | 0 (no_change) | 1 | 1 / 1 / 1 | 28 | 43 (same as o4) | absent | – | – | – | – |
| fl3b3 | absent | 1 | 1 / 1 / 1 | 28 | 43 (same as o4) | 5 / 220 / 20 | – | 0.0 (unpriced) | True (0) | – |
| fl3b3p | absent | 1 | 1 / 1 / 1 | 28 | 43 (same as o4) | 2 / 220 / 10 | not_profitable 3 | 361,217.0 | True (0) | – |
| b3l | absent | 1 | 1 / 1 / 1 | 28 | 43 (same as o4) | absent | – | – | – | – |
| o4_pwhopr | 0 (no_change) | 1 | 1 / 1 / 1 | 28 | 43 (same as o4) | 14 / 208 / 32 | not_profitable 194, unpatchable_native_exit 14 (pending) | 1,599,068.8 | False (0) | – |
| o4_hostall | 1,472 (candidate) | 1 | 1 / 1 / 1 | 28 | 43 (exit_not_goto_ptr 1, handler_mode_return 19, not_profitable 1, target_not_profiled 1612) | 7 / 208 / 19 | not_profitable 8 | 1,269,496.1 | False (0) | 10 |

Specialize-returns refusals are the same in every arm that runs it: exit_not_goto_ptr 15,
fact_not_constant 15, fact_unused 6, provenance_unexplained 1, target_not_profiled 588. So are
chain-returns' refusals: exit_not_goto_ptr 15, slot_budget 1, target_not_profiled 588
(`cli-w/receipts/<arm>.json`).

**Firmware-inline roots.** These are insn_vaddr, with members and net_ns, from
`receipts/<arm>.json` `passes.firmware-inline.roots`.

- **o4:** 0x82dd0 (98, 96,648; 98, 264,570), 0x83750 (25), 0x83762 (34), 0x8377c (281),
  0x83794 (5), 0x83d06 (279, 427,202).
- **o4_min2:** o4's roots minus 0x83d06, plus 7: 0x83496 ×2 (18 each), 0x83634 (281),
  0x8365c (25), 0x83682 (281), 0x8369a (5) and 0x83728 (281). Every added root carries net_ns.
  That matches the design's "13 (+7 −1)".
- **o4_pwhopr:** 14 roots: o4_min2's 13 plus 0x2daa (20 members).
  - It has 0x82dd0 ×2, 0x83496 ×2, 0x83634, 0x8365c, 0x83682, 0x8369a, 0x83728, 0x83750,
    0x83762, 0x8377c, 0x83794 and 0x2daa.
  - o4's 0x83d06 is refused `not_profitable`.
  - 14 call sites stay pending as `unpatchable_native_exit` (detail `no_rel32_field`), at
    8 PCs: 0x2daa ×3 (another call site there was admitted), 0x853e4 ×2, 0x85446 ×3,
    0x866c4 ×2, and one each at 0x80bdc, 0x80e5e, 0x86244 and 0x86686.
- **fl3b3:** 0x82dd0 ×3 (98 each), 0x83e8c (60), 0x83e9c (53). These are round 2's fl3 roots.
- **fl3b3p:** 0x82dd0 ×2 (98 each).
- **o4_hostall:** host-inline applies 1,472 sites; the largest are
  helper_exception_with_syndrome 979, helper_v7m_msr 146 and helper_rebuild_hflags_m32_newel
  142. Refused: call_target_mismatch 16,029, helper_graph_size_policy 2,331 (all
  helper_lookup_tb_ptr) and unsupported_abi 48. Growth is 3,535,176 B. These are the same counts
  as round 2's w7bd host-inline (`/data/scfix-r2/cli/VERIFY.md`).
- **b3l:** hotcold-partition `classified` (hot 170, coverage 0.990089). llvm-cache-layout ran
  as **`pettis_hansen_fallback`** (detail: "configure the pinned LLVM bridge with
  SCOPT_LLVM_LAYOUT_BRIDGE"): chains 25, merged_fraction 0.720018. order-cold `ordered`. The
  bridge was not used.

## 4. Same bytes (design 3.2)

| pair (cli) | design expectation | result |
|---|---|---|
| o4_chained = o4 | expected | equal (sha256 `869f9295…`) |
| o4_isb = o4 | expected | equal |
| o4_rank = o4 | not expected | **equal** |
| o4_legacy = o4 | not expected ("cli 7 regions with 0x83794 replaced by 0x83788") | **equal** |
| o4_noregions = s6b3 (anchor) | not stated | **equal** (`a3b91907…`, the round-2 s6b3 identity in `SETUP.json`) |
| o4_nohi = probe hl | not stated | equal (`9a0dbd6d…`) |

So on cli, `make_plan.py` will drop o4_chained, o4_isb, o4_rank and o4_legacy as same_bytes
with o4. Its same-bytes check compares only against the reference.

**Why o4_rank equals o4.** chain-static keeps 43 slots with identical refusals. The
256-unit cap does not bind on cli, and the two remaining refusal rules refuse nothing new.

**Why o4_legacy equals o4, against the probe2 prediction.**

- In both builds, 0x83788 and 0x83794 have 1326 calls each.
- hot-loop orders roots by (−count, callsite_id) (`firmware_policy.py` `_hot_loop_calls`).
  The legacy planner keeps that order (only `whopr` re-sorts by partition and address,
  `firmware_inline.py:227-245`).
- callsite_id differs between the hl base and O4:
  - 0x83788 is `297119f9…` in probe hl, probe2 legacy and o4_nohi, but `aa835812…` in
    o4 and o4_legacy;
  - 0x83794 is `d510761d…` against `8876b780…`.
- So on hl, 0x83788 is priced first and admitted, and 0x83794 is refused `not_profitable`
  (probe2 cli-legacy). On O4 the order flips, and the result is o4's roots.
- profit_net_ns still changes, to 1,641,491.7 against 1,276,456.1. 0x83d06 is priced at
  792,237 against 427,202. Members and bytes are identical.
- The ids change with the host-inline stage: o4_nohi has hl's ids.
- Probe2's prediction was built on hl, not on O4-r1.

**What o4_nohi's byte difference is.** It is layout only. o4 and o4_nohi have the same
7 roots, the same member counts and the same net_ns. The decoded v7 directories differ in
region order, region and callsite ids, and private offsets: 157,176 differing bytes by
`cmp -l`. So X1 on cli times a relayout, not a different selection. Host-inline applies 0
sites on cli.

## 5. Gates (design 3.2)

### G0 correctness: PASS for all 17 arms

The functional check is `cli-w/fcheck/<arm>/result.json`. Each case (commands, help) is
one run staged by `/data/scfix-o4/driver/compare_rtos.py` `stage()` on a private sparse copy,
with `QEMU_SC_V7_STATS` on and `QEMU_SC_V6_STATS` on every sc6 arm (all 17). Every arm and
case has:

- exit 0;
- 16 words equal to `cases.json`;
- exactly 1 `[BENCH][fw]` line;
- none of the forbidden strings;
- the private copy's size and header unchanged;
- source sha256 the same before and after;
- run.sh setting only `QEMU_SC_SEALED` and `QEMU_SC_TRUSTED`.

`summarize.py` re-checks:

- every arm has rc 0 and `published`;
- `x_b3.firmware_region_links` refusals are 0 (every regions arm has member descriptors on
  all regions);
- the loader requirement is `qemu-sc-v7-trusted-firmware-regions+sc6+member-skip`, or
  `qemu-sc-v7` (reserved 0x8000000f, sc6-only) for o4_noregions and b3l;
- the driver's `runtime_identity_check`: artifact `runtime_identity.json` qemu_sha256 =
  sha256(launch argv[0]) = `951775b0…`.

v7 and v6 stats from the functional runs (commands; help):

| arm | counters_revision | counter_problems | regions loaded | residual exits | poll unverified / chain_skipped_poll_unverified | v6 activations / links / armed | v6 invariant_violations / flush_resets | loader requirement |
|---|---|---|---|---|---|---|---|---|
| o4 | 4 / 4 | [] / [] | 7 | 3 / 3 | 0 / 0; 0 / 0 | 28 / 1 / 0; 28 / 1 / 0 | 0 / 0; 0 / 0 | qemu-sc-v7-trusted-firmware-regions+sc6+member-skip |
| o4_pinned | 4 / 4 | [] / [] | 7 | 3 / 3 | 0 / 0; 0 / 0 | 28 / 1 / 0; 28 / 1 / 0 | 0 / 0; 0 / 0 | …+sc6+member-skip |
| o4_chained | 4 / 4 | [] / [] | 7 | 3 / 3 | 0 / 0; 0 / 0 | 28 / 1 / 0; 28 / 1 / 0 | 0 / 0; 0 / 0 | …+sc6+member-skip |
| o4_isb | 4 / 4 | [] / [] | 7 | 3 / 3 | 0 / 0; 0 / 0 | 28 / 1 / 0; 28 / 1 / 0 | 0 / 0; 0 / 0 | …+sc6+member-skip |
| o4_rank | 4 / 4 | [] / [] | 7 | 3 / 3 | 0 / 0; 0 / 0 | 28 / 1 / 0; 28 / 1 / 0 | 0 / 0; 0 / 0 | …+sc6+member-skip |
| o4_hotgraph | 4 / 4 | [] / [] | 7 | 4 / 3 | 0 / 0; 0 / 0 | 28 / 1 / 0; 28 / 1 / 0 | 0 / 0; 0 / 0 | …+sc6+member-skip |
| o4_min2 | 4 / 4 | [] / [] | 13 | 10 / 9 | 0 / 0; 0 / 0 | 13 / 1 / 0; 13 / 1 / 0 | 0 / 0; 0 / 0 | …+sc6+member-skip |
| o4_legacy | 4 / 4 | [] / [] | 7 | 3 / 3 | 0 / 0; 0 / 0 | 28 / 1 / 0; 28 / 1 / 0 | 0 / 0; 0 / 0 | …+sc6+member-skip |
| o4_nohi | 4 / 4 | [] / [] | 7 | 3 / 3 | 0 / 0; 0 / 0 | 28 / 1 / 0; 28 / 1 / 0 | 0 / 0; 0 / 0 | …+sc6+member-skip |
| o4_nocp | 4 / 4 | [] / [] | 7 | 3 / 3 | 0 / 0; 0 / 0 | 28 / 1 / 0; 28 / 1 / 0 | 0 / 0; 0 / 0 | …+sc6+member-skip |
| o4_nospec | 4 / 4 | [] / [] | 7 | 3 / 3 | 0 / 0; 0 / 0 | 28 / 0 / 0; 28 / 0 / 0 | 0 / 0; 0 / 0 | …+sc6+member-skip |
| o4_noregions | 4 / 4 | [] / [] | 0 | 0 / 0 | 0 / 0; 0 / 0 | 66 / 1 / 1; 66 / 1 / 1 | 0 / 0; 0 / 0 | qemu-sc-v7 |
| fl3b3 | 4 / 4 | [] / [] | 5 | 1 / 1 | 0 / 0; 0 / 0 | 48 / 1 / 0; 48 / 1 / 0 | 0 / 0; 0 / 0 | …+sc6+member-skip |
| fl3b3p | 4 / 4 | [] / [] | 2 | 1 / 1 | 0 / 0; 0 / 0 | 48 / 1 / 0; 48 / 1 / 0 | 0 / 0; 0 / 0 | …+sc6+member-skip |
| b3l | 4 / 4 | [] / [] | 0 | 0 / 0 | 0 / 0; 0 / 0 | 66 / 1 / 1; 66 / 1 / 1 | 0 / 0; 0 / 0 | qemu-sc-v7 |
| o4_pwhopr | 4 / 4 | [] / [] | 14 | 10 / 8 | 0 / 0; 0 / 0 | 13 / 1 / 0; 13 / 1 / 0 | 0 / 0; 0 / 0 | …+sc6+member-skip |
| o4_hostall | 4 / 4 | [] / [] | 7 | 3 / 3 | 0 / 0; 0 / 0 | 28 / 1 / 0; 28 / 1 / 0 | 0 / 0; 0 / 0 | …+sc6+member-skip |

On every firmware-directory arm:

- v7 is `loaded`, with mode `trusted`;
- artifact_id8 equals `read_sc7`;
- poll entries verified equals regions loaded.

o4_noregions and b3l report v6 66 activations, 1 link and 1 armed. That is round 2's s6b3
result (`/data/scfix-r2/cli/VERIFY.md`).

### Regions replay: PASS for all 15 arms with regions

The replay is `cli-w/regions/<arm>/result.json`. It is the driver's own staging sequence:

1. a sparse copy and `stage()`;
2. a public warm replay with `QEMU_SC_PROFILE`, with its identity check: clean, warm, no
   overflow, record count and checksum equal to the header;
3. the `regions` replay with `QEMU_SC_V7_STATS`, with its private-region identity check
   and `counter_problems` check.

Every check passed on both cases. The counters below are commands / help.

| arm | loaded / activated | region entries | residual exits | T->T allowed / installs | slots armed / skipped | poll verified / unverified | poll-entry exits | loop_returns_nonnull | member shadows |
|---|---|---|---|---|---|---|---|---|---|
| o4 | 7 / 7; 7 / 7 | 7 / 7 | 3 / 4 | 1 / 1; 1 / 1 | 16 / 98; 16 / 98 | 7 / 0; 7 / 0 | 20 / 10 | 792 / 786 | 813 / 813 |
| o4_pinned | 7 / 7; 7 / 7 | 7 / 7 | 3 / 3 | 1 / 1; 1 / 1 | 16 / 98; 16 / 98 | 7 / 0; 7 / 0 | 16 / 2 | 810 / 729 | 813 / 813 |
| o4_chained | 7 / 7; 7 / 7 | 7 / 7 | 3 / 3 | 1 / 1; 1 / 1 | 16 / 98; 16 / 98 | 7 / 0; 7 / 0 | 15 / 5 | 813 / 782 | 813 / 813 |
| o4_isb | 7 / 7; 7 / 7 | 7 / 7 | 3 / 3 | 1 / 1; 1 / 1 | 16 / 98; 16 / 98 | 7 / 0; 7 / 0 | 14 / 10 | 816 / 795 | 813 / 813 |
| o4_rank | 7 / 7; 7 / 7 | 7 / 7 | 3 / 3 | 1 / 1; 1 / 1 | 16 / 98; 16 / 98 | 7 / 0; 7 / 0 | 18 / 5 | 795 / 797 | 813 / 813 |
| o4_hotgraph | 7 / 7; 7 / 7 | 7 / 7 | 4 / 3 | 1 / 1; 1 / 1 | 16 / 98; 16 / 98 | 7 / 0; 7 / 0 | 13 / 6 | 794 / 784 | 813 / 813 |
| o4_min2 | 13 / 13; 13 / 13 | 13 / 13 | 9 / 10 | 1 / 1; 1 / 1 | 34 / 153; 34 / 153 | 13 / 0; 13 / 0 | 17 / 8 | 753 / 732 | 1437 / 1437 |
| o4_legacy | 7 / 7; 7 / 7 | 7 / 7 | 3 / 3 | 1 / 1; 1 / 1 | 16 / 98; 16 / 98 | 7 / 0; 7 / 0 | 14 / 7 | 813 / 778 | 813 / 813 |
| o4_nohi | 7 / 7; 7 / 7 | 7 / 7 | 3 / 3 | 1 / 1; 1 / 1 | 16 / 98; 16 / 98 | 7 / 0; 7 / 0 | 15 / 11 | 798 / 795 | 813 / 813 |
| o4_nocp | 7 / 7; 7 / 7 | 7 / 7 | 3 / 3 | 1 / 1; 1 / 1 | 16 / 98; 16 / 98 | 7 / 0; 7 / 0 | 19 / 5 | 799 / 796 | 813 / 813 |
| o4_nospec | 7 / 7; 7 / 7 | 7 / 7 | 3 / 3 | 1 / 1; 1 / 1 | 16 / 98; 16 / 98 | 7 / 0; 7 / 0 | 21 / 6 | 800 / 796 | 813 / 813 |
| fl3b3 | 5 / 2; 5 / 2 | 2 / 2 | 1 / 2 | 0 / 0; 0 / 0 | 2 / 46; 2 / 46 | 5 / 0; 5 / 0 | 0 / 0 | 892 / 848 | 194 / 194 |
| fl3b3p | 2 / 2; 2 / 2 | 2 / 2 | 1 / 1 | 0 / 0; 0 / 0 | 2 / 46; 2 / 46 | 2 / 0; 2 / 0 | 0 / 0 | 866 / 838 | 194 / 194 |
| o4_pwhopr | 14 / 14; 14 / 14 | 14 / 14 | 10 / 9 | 1 / 1; 1 / 1 | 41 / 153; 41 / 153 | 14 / 0; 14 / 0 | 17 / 7 | 752 / 735 | 1456 / 1456 |
| o4_hostall | 7 / 7; 7 / 7 | 7 / 7 | 3 / 3 | 1 / 1; 1 / 1 | 16 / 98; 16 / 98 | 7 / 0; 7 / 0 | 12 / 5 | 784 / 793 | 813 / 813 |

On cli every region is entered exactly once per run (region_entries = activations = regions
loaded), except fl3b3, where 2 of 5 regions are entered, as in round 2's fl3.

o4's 7 / 7 entries, 16 armed and 98 skipped slots, and 1 T->T install reproduce round 2's
c7b3 (`/data/scfix-r2/cli/VERIFY.md`: 7 / 7, 4 / 3 residual, 1 / 1 T->T, 16 / 98).

Residual exits, poll-entry exits and loop_returns_nonnull vary between single runs of the
same bytes. For example, o4, o4_pinned, o4_chained, o4_isb, o4_rank and o4_legacy share
bytes and give residual 3-4, poll-entry exits 14-20 / 2-10 and loop_returns_nonnull
792-816 / 729-797.

**A1 gate.** o4_chained has the same bytes on cli, so it is inert on cli by construction. The
design's expectation, "cli the same bytes", holds.

### Validity gates (design 3.2, cli): PASS for all 17 arms

| arm | check | result |
|---|---|---|
| o4, o4_pinned | host-inline 0, regions 7, CR 28, CS 43, cache = c7b3's | 0, 7, 28, 43, equal |
| o4_chained | regions about 7; cli same bytes as o4 | 7; same |
| o4_isb | policy.isb_boundary true; isb exits (cli 0); cli same bytes; nested_decisions = o4's; member differences only after ISB members | true; 0; same; 10 = 10; none |
| o4_rank | chain-static applied, not incomplete_lookup_profile; c_target_check 0.0 in receipt; slots and refusals by reason | candidate, 43 slots; 0.0 and rank_by_net_work true; refusals as in §3 |
| o4_hotgraph | about 2 sites; helper_lookup_tb_ptr 0 (helper_graph_size_policy); SPEC variants against 1 | 2 (helper_udiv); 0, with 41 refused helper_graph_size_policy; 1 |
| o4_min2 | regions about 13; every added root priced | 13; 7 added, all with net_ns |
| o4_legacy | regions about 7 | 7 (same bytes as o4; §4) |
| o4_nohi | no host-inline receipt | pass order starts at specialize-returns |
| o4_nocp | constant-propagation absent | absent |
| o4_nospec | the four SPEC passes absent | absent (host-inline, CR, CS, firmware-inline) |
| o4_noregions | no firmware directory | reserved 0x8000000f, loader `qemu-sc-v7` |
| fl3b3 / fl3b3p | 5 / 2 regions with member descriptors | 5 / 5 and 2 / 2 |
| b3l | layout passes ran; bridge or Pettis-Hansen | ran; Pettis-Hansen fallback (bridge not configured) |
| o4_pwhopr | roots, members, growth, time and memory; abort above 8 GB or 60 min | 14 admitted / 194 not_profitable + 14 unpatchable; 1,470 members; allocated +294,912 B over o4; 203.2 s; 2,054,076 KiB; no abort (`rss-watch.log` peak session RSS 2,028,224 KiB) |
| o4_hostall | about 1,472 sites | 1,472 |

Source: `arms-built.json` `arms.<arm>.gates`.

## 6. Deviations from the design

1. **`o4_pinned` store.** The design line writes to `opt/o4/store`, the same store as `o4`.
   It was built into `opt/o4_pinned/store` instead, because `make_plan.py` refuses a store
   with two artifacts. The rest of the line is verbatim.
2. **Rebuild lines.** They are round 2's and the probe's commands with the input paths
   moved to the private copies, which are byte-identical (§1). Their stores are under
   `cli-w/rebuild/`, so `make_plan.py` never sees them.
3. **`/usr/bin/time -v` on every build**, not only the heavy arms.
4. **v6 stats were collected on every arm**, because all 17 carry an sc6 extension.
5. **`b3l` uses the Pettis-Hansen fallback**, not the LLVM bridge. The design marks the bridge
   optional. C2 is not dropped, because nothing refused.

## 7. Problems and observations

1. **Unexpected same bytes on cli: o4_rank and o4_legacy** (§4). With the expected
   o4_chained and o4_isb, four of the six add-on and fidelity arms are not timed on cli.
   The probe2 cli prediction for A8 does not carry over to O4-r1: the tie-break is by
   callsite_id, and the host-inline stage changes those ids.
2. **o4_noregions is byte-identical to the round-2 s6b3 anchor.** The diagnostic comparison
   [s6b3, o4_noregions] in campaign A compares identical bytes on cli. `make_plan.py` will
   time both, because its same-bytes check is only against o4.
3. **o4_nohi differs from o4 only in layout on cli** (§4).
4. **o4_pwhopr refuses o4's 0x83d06 root** (279 members, 427 µs model profit under
   hot-loop) as `not_profitable`, and admits 8 roots o4 does not have. Every region is
   entered once per run (14 / 14).
5. **Single-run counters.** Variation like round 2's applies; the values above are single
   runs.
6. **Hygiene.**
   - fd-int `git status --porcelain --ignored` is empty, and there are no `*.pyc`. HEAD is
     still `ece9b5e`.
   - `find /data/scfix-r2 /data/scfix-campaign /data/trebuchet-clean /data/fastdyn
     /data/fastdyn-work/qemu-fu /data/qemu-scfix-bin -newer cli/make_cmds.py`, run after
     every build and gate, lists one entry: the directory
     `/data/fastdyn/.git/worktrees/fd-int`, mtime 01:04:48. No file in it is newer. That is
     the index.lock create/remove of a `git status` in fd-int, which I ran then. It is the
     fd-int worktree's git admin directory.
   - QEMU sha256 is unchanged.
   - The QEMU runs write `io.log` into each launch cwd, `cli-w/arms/<arm>/`.
7. **Disk.** `cli-w/opt` is 775 MB, `cli-w/rebuild` 87 MB and `cli-w/arms` 124 MB. Private
   cache copies were deleted after each check.

## P4b: o4_tasks (hot-loop-tasks), fd-int b35cdb6

Date 2026-11-26. Build, functional check and regions replay only. **Nothing was timed.**
P4b's scripts and outputs are in `/data/scfix-o4/p4b`. The code is fd-int commit `b35cdb6`
(see packet VERIFY.md P4b for the commit, the unit suite and the uart o4 byte spot check;
the spot check is `cmp` EQUAL).

* **Build.** `C/build.sh o4_tasks`, using the cmds.json argv, which is the commands.txt cli
  `# o4_tasks` line. Result: rc 0, 172.8 s, peak RSS 2,054,640 KiB, published, 1 artifact.
  cache.sc sha256 `869f92952417c6268fcf0f558b3f2aad7d4e79aea79e244f06c9509e24bc40f2`,
  **the same bytes as o4** (and c7b3, o4_pinned, o4_chained, o4_isb, o4_rank, o4_legacy).
  Regions 7, members 820, allocated 16,154,624 B. The other arms' `fd_int_head` is ece9b5e;
  o4_tasks was built at b35cdb6 (its NOTES entry says so).
* **Receipts against o4** (`p4b/cli-validity.json`): `policy.union` hot 15, static 2,
  static_only 0, both 2. Both task-loop roots are also hot roots, so nothing is added. All 7
  o4 roots are embedded unchanged (region ids equal, `net_ns` difference 0.0).
  nested_decisions are equal (10 = 10), nested inlines 19 = 19, and profit_net_ns is equal
  (1,276,456.057). Every other pass is identical.
* **G0** (`C/gates.py o4_tasks` → `cli-w/receipts/o4_tasks.json`,
  `cli-w/fcheck/o4_tasks/result.json`, `cli-w/regions/o4_tasks/result.json`; `C/summarize.py`
  → `C/arms-built.json`): **all pass.** That covers optimize rc 0 and published, the
  functional check (commands and help OK), the loader requirement, region-link refusals 0,
  and the QEMU sha256 equal to the artifact's. The regions replay is OK.
* **Validity** (design 3.2 o4_tasks, shared checker `p4b/tasks_validity.py`): union counts
  present, every o4 root unchanged, nested_decisions equal. All true.
* **Regions replay** (commands / help): loaded, entries and activations 7 / 7; residual
  exits 3 / 3; T->T installs 1 / 1; slots armed / skipped 16 / 98; poll verified 7,
  unverified 0. o4's replay of the same bytes: residual exits 3 / 4, everything else equal
  (run-to-run variation on identical bytes).
* **Records.** Old files are in `p4b/backup/cli`.
  * `C/arms-built.json` gains `o4_tasks` (gates_ok true). The only change to other arms is
    that o4, o4_pinned, o4_chained, o4_isb, o4_rank and o4_legacy add `o4_tasks` to
    `same_bytes_as`.
  * `C/summarize.py` gained `o4_tasks` in ARMS, its validity branch and a NOTES entry.
* **For A2.** o4_tasks has the same bytes as o4 on cli, so it is decided without timing on
  cli (design 3.2).

## P5: o4r2 (O4-r2), fd-int b35cdb6

Date 2026-11-26. P5 decisions (O4-design 3.7) and the O4-r2 build (3.8 Composition). Builds, functional checks and a regions replay only. **Nothing was timed.** P5's scripts and outputs are in `/data/scfix-o4/p5`, and the decisions are in `/data/scfix-o4/decisions.json`.

* **Composition.** O4-r2 = O4-r1 + **A5 absorbed** (rule BEST: o4_pwhopr was faster than O4 in campaign H, cli d +1.97% and pooled +0.93%) + **I1** (isb_boundary = true, FIDELITY) + **X1** (host-inline removed, REMOVE) + **X4** (SPEC bundle removed, REMOVE). X2 is accepted but moot under X4, X3 is rejected, and no add-on passed ADD.
  The passes are chain-returns, chain-static and firmware-inline `{mode exhaustive, cost_model profile, phase steady, isb_boundary true, trust user, planner whopr, partitions 4, workers 4, member_slot_skip on}`.
* **Pipeline.** The revision file is `/data/scfix-o4/design/pipelines/O4-r2.toml` (name "O4-r2", sha256 `1ee63d43790eba14…`).
  * The arm was built from its copy `design/pipelines/o4r2.toml` (sha256 `4ea2b72005203d51…`), which differs only in name ("o4r2") and description (`p5/make_arm_copy.py`; tomllib and scopt `load_pipeline` agree).
  * **Deviation:** `--pipeline O4-r2.toml` itself fails in scopt's O4 preflight before any pass, rc 2: `pipeline 'O4-r2' is not the packaged /data/fastdyn-work/fd-int/tools/scopt/pipelines/O4-r2.toml; O4 is fixed: copy O4-rN.toml under another name to experiment`. That attempt is kept in `p5/failed/cli-o4r2.attempt1`.
  * The pipeline name does not reach cache.sc: O4-r1 via -O4 equals c7b3 byte for byte (section 1). O4.toml and -O4 are not repointed before campaign B.
* **Build.** `p5/o4r2-commands.txt` cli line: the `# o4_nohi` line of commands.txt with only `--pipeline` and `--store opt/o4r2/store` changed, so there is no `--pgo-launch`. It ran through this workload's own wrapper (nice -n 10 taskset -c 21-31, /usr/bin/time -v, timeout 3600) at fd-int b35cdb6, with no code change in P5.
  * rc 0, 51.5 s, peak RSS 1,039,764 KiB (time -v); the watchdog's process-tree peak was 864,096 KiB (limits 8 GiB / 60 min). The artifact was published, 1 in the store.
  * cache.sc sha256 `a4f6af65718360b35a2341fc4c95bcf68efb6263c9b62c89b09ad258b2d75ea2`. Regions / members / descriptor regions: 14 / 1,470 / 14 (o4_pwhopr 14 / 1,470 / 14; o4 7 / 820 / 7).
  * Allocated 16,449,536 B (o4_pwhopr 16,449,536, o4 16,154,624). Reserved 0xc000000d (o4_pwhopr 0xc000000f, o4 0xc000000f). Loader requirement `qemu-sc-v7-trusted-firmware-regions+sc6+member-skip`.
* **Stage-gate equivalent** (fd-int `required_stage_problems` and `effective_configuration` with `gate_status` over O4-r2's stages): chain-returns edited, chain-static edited, firmware-inline edited; problems [].
* **B3 rows.** o4r2: chain-returns 29 slots, refused {'exit_not_goto_ptr': 15, 'target_not_profiled': 588}; chain-static 43 slots, refused {'handler_mode_return': 19, 'not_profitable': 1, 'target_not_profiled': 1613}.
  * They equal o4_nospec's: True. o4: chain-returns 28 slots, refused {'exit_not_goto_ptr': 15, 'slot_budget': 1, 'target_not_profiled': 588}; chain-static 43 slots, refused {'handler_mode_return': 19, 'not_profitable': 1, 'target_not_profiled': 1613}.
* **Firmware-inline** (o4r2 / o4_pwhopr / o4):
  * embedded 14 / 14 / 7; nested inlines 32 / 32 / 19
  * refused {'not_profitable': 194.0, 'unpatchable_native_exit': 14.0} / {'not_profitable': 194.0, 'unpatchable_native_exit': 14.0} / {'not_profitable': 8.0}
  * profit_net_ns 1599068.75 / 1599068.75 / 1276456.057
  * isb_boundary exits (regions) 0 (0) / 0 / 0
  * Roots against o4_pwhopr: added [], dropped [], members changed 0, common 14.
  * Roots against o4: added ['0x8369a', '0x2daa', '0x83728', '0x83496', '0x83496', '0x83682', '0x83634', '0x8365c'], dropped ['0x83d06'], members changed 0.
* **G0** (`gates/cli.json` `o4r2`, regenerated by `driver/make_gates.py`): **all pass** (13 checks).
  * The functional runs exit 0 with the 16 oracle words, 1 BENCH line and no forbidden pattern.
  * Runtime identity: the QEMU sha256 equals the artifact's `951775b0…`.
  * Counters revision 4, `counter_problems` [], every poll entry verified.
  * v6: invariant_violations 0 and flush_resets 0. Cache unchanged; region-link refusals 0; loader requirement ok.
* **Validity** (`p5/cli-o4r2-validity.json`, shared checker `p5/o4r2_validity.py`): pipeline_copy_equals_O4_r2 true, passes_are_O4_r2 true, stage_gate_all_edited_or_no_change true, no_host_inline_receipt true, spec_passes_absent true, firmware_inline_exhaustive_profile true, isb_boundary_true true, member_slot_skip_enabled true, build_within_8GiB_60min true.
* **Same bytes** (`cmp` of cache.sc): o4 differs, o4_isb differs, o4_nohi differs, o4_nospec differs, o4_pwhopr differs. The workload aggregator also compared it with every other built arm and the anchors: no match. o4r2 is not the same bytes as o4 or as any accepted single-item arm, so it is timed on this workload in B1.
* **Regions replay** (single untimed runs):

  | arm | case | loaded | activated | lookup entries | residual exits | T->T installs | slots armed / skipped | poll verified / unverified | loop_returns_nonnull |
  |---|---|---|---|---|---|---|---|---|---|
  | o4r2 | commands | 14 | 14 | 14 | 10 | 1 | 41 / 153 | 14 / 0 | 762 |
  | o4r2 | help | 14 | 14 | 14 | 9 | 1 | 41 / 153 | 14 / 0 | 725 |
  | o4_pwhopr | commands | 14 | 14 | 14 | 10 | 1 | 41 / 153 | 14 / 0 | 752 |
  | o4_pwhopr | help | 14 | 14 | 14 | 9 | 1 | 41 / 153 | 14 / 0 | 735 |
  | o4 | commands | 7 | 7 | 7 | 3 | 1 | 16 / 98 | 7 / 0 | 792 |
  | o4 | help | 7 | 7 | 7 | 4 | 1 | 16 / 98 | 7 / 0 | 786 |

  Launch: `/data/scfix-o4/cli-w/arms/o4r2/launch.json`, derived by the gate script through `o4lib.derive_arm_launch` (the private c7b3 launch with o4r2's cache).
* **Records.** `cli/cmds.json` gains `o4r2` (the argv of `C/build.sh`). `C/arms-built.json` gains `o4r2`, and `cli-w/receipts`, `cli-w/fcheck` and `cli-w/regions` gain `o4r2`. `C/summarize.py` gained `o4r2` in ARMS and its validity branch.
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
  * e.g. o4r3b: `scopt optimize /data/scfix-o4/cli-w/in/snapshots/a384727400b870739b4246add4f1621a704a7f9e3441f40a3b2d7f386b2a90d4 --pipeline /data/scfix-o4/design/pipelines/o4r3b.toml --profile /data/scfix-o4/cli-w/in/merged.profile.json --prep /data/scfix-o4/cli-w/in/prep --elf /data/scfix-o4/cli-w/in/fw.elf --runtime-identity /data/scfix-o4/cli-w/in/runtime.json --pgo-launch /data/scfix-o4/cli-w/launch/launch.json --v6-exe sha256:951775b0300ea1421f32565af8f677c1eaee14e7e1de6238d32bdbe925eebc60 --v7-trusted-exe sha256:951775b0300ea1421f32565af8f677c1eaee14e7e1de6238d32bdbe925eebc60 --v7-member-skip-exe sha256:951775b0300ea1421f32565af8f677c1eaee14e7e1de6238d32bdbe925eebc60 --store /data/scfix-o4/cli-w/opt/o4r3b/store --json`
* **Builds**:
  | arm | rc | wall s | peak RSS KiB (time -v / watchdog) | cache.sc sha256 | regions / members / descriptor regions | allocated B | loader |
  |---|---|---|---|---|---|---|---|
  | o4r3b | 0 | 54.6 | 1,040,156 / 974,688 | `a4f6af65718360b35a2341fc4c95bcf68efb6263c9b62c89b09ad258b2d75ea2` | 14 / 1470 / 14 | 16,449,536 | `qemu-sc-v7-trusted-firmware-regions+sc6+member-skip` |
  | o4r3c | 0 | 175.8 | 2,054,616 / 2,026,404 | `cdb87351477dbdd18486bacc4badc595ac5f9d9127278afae06cc6933f05ebb9` | 14 / 1470 / 14 | 16,449,536 | `qemu-sc-v7-trusted-firmware-regions+sc6+member-skip` |
  | o4r3d | 0 | 54.6 | 1,039,708 / 972,300 | `a4f6af65718360b35a2341fc4c95bcf68efb6263c9b62c89b09ad258b2d75ea2` | 14 / 1470 / 14 | 16,449,536 | `qemu-sc-v7-trusted-firmware-regions+sc6+member-skip` |
* **Receipts** (passes: outcome, reason, edits):
  * o4r3b: chain-returns candidate 29; chain-static candidate 43; firmware-inline candidate firmware_regions_emitted 14
    * firmware-inline: embedded 14, members 1470, nested inlines 32, refused {'not_profitable': 194.0, 'unpatchable_native_exit': 14.0}, profit_net_ns 1599068.75 (o4_pwhopr: 14, 1470, 32, {'not_profitable': 194.0, 'unpatchable_native_exit': 14.0}, 1599068.75); roots against o4_pwhopr: added [], dropped [], members changed 0, common 14
    * chain rows: chain-returns 29 slots refused {'exit_not_goto_ptr': 15, 'target_not_profiled': 588}; chain-static 43 slots refused {'handler_mode_return': 19, 'not_profitable': 1, 'target_not_profiled': 1613} (o4_pwhopr: chain-returns 28 {'exit_not_goto_ptr': 15, 'slot_budget': 1, 'target_not_profiled': 588}; chain-static 43 {'handler_mode_return': 19, 'not_profitable': 1, 'target_not_profiled': 1613}; o4_rank chain-static: 43 {'handler_mode_return': 19, 'not_profitable': 1, 'target_not_profiled': 1613})
    * stage-gate equivalent (`required_stage_problems`, `gate_status`): {'chain-returns': 'edited', 'chain-static': 'edited', 'firmware-inline': 'edited'}; problems []
  * o4r3c: host-inline no_change no_eligible_native_calls 0; specialize-returns candidate 6; constant-propagation candidate 1; branch-folding candidate 1; dead-host-ops candidate 1; chain-returns candidate 28; chain-static candidate 43; firmware-inline candidate firmware_regions_emitted 14
    * host-inline applied 0 (no_change, no_eligible_native_calls); site profile `sha256:d8f061b5a2839a0aebfcbffb3c21a9e69bbd0c17d196e2d3e3d57f1ad14372e3` (input plumbing, not compared)
    * firmware-inline: embedded 14, members 1470, nested inlines 32, refused {'not_profitable': 194.0, 'unpatchable_native_exit': 14.0}, profit_net_ns 1599068.75 (o4_pwhopr: 14, 1470, 32, {'not_profitable': 194.0, 'unpatchable_native_exit': 14.0}, 1599068.75); roots against o4_pwhopr: added [], dropped [], members changed 0, common 14
    * chain rows: chain-returns 28 slots refused {'exit_not_goto_ptr': 15, 'slot_budget': 1, 'target_not_profiled': 588}; chain-static 43 slots refused {'handler_mode_return': 19, 'not_profitable': 1, 'target_not_profiled': 1613} (o4_pwhopr: chain-returns 28 {'exit_not_goto_ptr': 15, 'slot_budget': 1, 'target_not_profiled': 588}; chain-static 43 {'handler_mode_return': 19, 'not_profitable': 1, 'target_not_profiled': 1613}; o4_rank chain-static: 43 {'handler_mode_return': 19, 'not_profitable': 1, 'target_not_profiled': 1613})
    * stage-gate equivalent (`required_stage_problems`, `gate_status`): {'host-inline': 'no_change', 'specialize-returns': 'edited', 'constant-propagation': 'edited', 'branch-folding': 'edited', 'dead-host-ops': 'edited', 'chain-returns': 'edited', 'chain-static': 'edited', 'firmware-inline': 'edited'}; problems []
  * o4r3d: chain-returns candidate 29; chain-static candidate 43; firmware-inline candidate firmware_regions_emitted 14
    * firmware-inline: embedded 14, members 1470, nested inlines 32, refused {'not_profitable': 194.0, 'unpatchable_native_exit': 14.0}, profit_net_ns 1599068.75 (o4_pwhopr: 14, 1470, 32, {'not_profitable': 194.0, 'unpatchable_native_exit': 14.0}, 1599068.75); roots against o4_pwhopr: added [], dropped [], members changed 0, common 14
    * chain rows: chain-returns 29 slots refused {'exit_not_goto_ptr': 15, 'target_not_profiled': 588}; chain-static 43 slots refused {'handler_mode_return': 19, 'not_profitable': 1, 'target_not_profiled': 1613} (o4_pwhopr: chain-returns 28 {'exit_not_goto_ptr': 15, 'slot_budget': 1, 'target_not_profiled': 588}; chain-static 43 {'handler_mode_return': 19, 'not_profitable': 1, 'target_not_profiled': 1613}; o4_rank chain-static: 43 {'handler_mode_return': 19, 'not_profitable': 1, 'target_not_profiled': 1613})
    * stage-gate equivalent (`required_stage_problems`, `gate_status`): {'chain-returns': 'edited', 'chain-static': 'edited', 'firmware-inline': 'edited'}; problems []
* **G0** (`gates/cli.json`, regenerated by `driver/make_gates.py`):
  * o4r3b: g0 **True** (13/13 checks true), cache sha256 `a4f6af65718360b3…`
  * o4r3c: g0 **True** (13/13 checks true), cache sha256 `cdb87351477dbdd1…`
  * o4r3d: g0 **True** (13/13 checks true), cache sha256 `a4f6af65718360b3…`
  * The functional runs exit 0 with the 16 oracle words, 1 BENCH line and no forbidden pattern; the launched QEMU's sha256 equals the artifact's `951775b0…`; counters revision 4, `counter_problems` [], every poll entry verified; sc6 invariant_violations 0 and flush_resets 0; cache unchanged; region-link refusals 0; loader requirement `qemu-sc-v7-trusted-firmware-regions+sc6+member-skip`.
* **Validity** (`r3/<w>-<arm>-validity.json`, shared checker `r3/r3_validity.py`):
  * o4r3b: passes_are_pipeline true, stage_gate_all_edited_or_no_change true, host_inline_as_pipeline true, spec_as_pipeline true, firmware_inline_exhaustive_profile true, isb_boundary_off true, member_slot_skip_enabled true, chain_static_as_pipeline true, build_within_8GiB_60min true
  * o4r3c: passes_are_pipeline true, stage_gate_all_edited_or_no_change true, host_inline_as_pipeline true, spec_as_pipeline true, firmware_inline_exhaustive_profile true, isb_boundary_off true, member_slot_skip_enabled true, chain_static_as_pipeline true, build_within_8GiB_60min true
  * o4r3d: passes_are_pipeline true, stage_gate_all_edited_or_no_change true, host_inline_as_pipeline true, spec_as_pipeline true, firmware_inline_exhaustive_profile true, isb_boundary_off true, member_slot_skip_enabled true, chain_static_as_pipeline true, build_within_8GiB_60min true
* **Same bytes** (`cmp` of cache.sc against every other build in opt/ and rebuild/ and every anchor on this workload):
  * o4r3b: o4r2, o4r3d
  * o4r3c: o4_pwhopr
  * o4r3d: o4r2, o4r3b
* **Regions replay** (single untimed runs; o4_pwhopr and o4 from the earlier gates):

  | arm | case | loaded | activated | entries | residual exits | T->T installs | slots armed / skipped | poll verified / unverified | loop_returns_nonnull |
  |---|---|---|---|---|---|---|---|---|---|
  | o4r3b | commands | 14 | 14 | 14 | 10 | 1 | 41 / 153 | 14 / 0 | 748 |
  | o4r3b | help | 14 | 14 | 14 | 9 | 1 | 41 / 153 | 14 / 0 | 736 |
  | o4r3c | commands | 14 | 14 | 14 | 8 | 1 | 41 / 153 | 14 / 0 | 748 |
  | o4r3c | help | 14 | 14 | 14 | 9 | 1 | 41 / 153 | 14 / 0 | 733 |
  | o4r3d | commands | 14 | 14 | 14 | 10 | 1 | 41 / 153 | 14 / 0 | 748 |
  | o4r3d | help | 14 | 14 | 14 | 8 | 1 | 41 / 153 | 14 / 0 | 732 |
  | o4_pwhopr | commands | 14 | 14 | 14 | 10 | 1 | 41 / 153 | 14 / 0 | 752 |
  | o4_pwhopr | help | 14 | 14 | 14 | 9 | 1 | 41 / 153 | 14 / 0 | 735 |
  | o4 | commands | 7 | 7 | 7 | 3 | 1 | 16 / 98 | 7 / 0 | 792 |
  | o4 | help | 7 | 7 | 7 | 4 | 1 | 16 / 98 | 7 / 0 | 786 |

* **Records.** The workload's receipts / arms-built records gain o4r3b, o4r3c and o4r3d; the aggregator gained their validity branch (the shared checker) and annotations. Every other arm's entry is unchanged, checked key by key against the backups in `/data/scfix-o4/r3/backup/`, except `same_bytes_as` lists that now name the new builds (uart: o4_pwhopr gains o4r3c and rebuild/o4r3a; cli: o4_pwhopr gains o4r3c, o4r2 gains o4r3b and o4r3d).
  * `driver/make_gates.py` added o4r3b, o4r3c, o4r3d to BUILT (sha256 `4de960ae9438d140…` → `22858e5cd7f21d40…`); decide.py is unchanged (`0b724747…`). The rerun rewrote `gates/{packet,uart,cli,all}.json`; for the other arms g0, checks and cache sha256 are unchanged (only evidence strings that quote the regenerated records' sha256 changed).
* **QEMU** `/data/qemu-scfix-bin/aacca05/qemu-system-arm`: sha256 `951775b0…eebc60`, inode 87162887, size 137,799,456, mtime 1795640690, the same before the builds, after the builds and after the gates (`r3/qemu-identity.txt`).
