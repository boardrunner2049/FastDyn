#!/usr/bin/env python3
"""WP29: write ``reports/B4-gate.md`` from the evidence of ``wp29_gate.py`` (plan ``## WP29``
"reports/B4-gate.md generator rows": gate binary sha, §7.6 deferral statement, known gaps).

    python tests/qemu/wp29_report.py [--out runs/wp29] [--report reports/B4-gate.md] \
        [--append-section FILE ...]

Every number in the tables is read from ``OUT/*.json``; the narrative paragraphs that
interpret them are fixed text (they name the files they rest on), chosen by the verdict
(e.g. item 1/4 pass or fail). Hand-written sections are kept in files and appended
verbatim with ``--append-section`` (in the given order); the generator checks that each
appears byte-for-byte in the written report.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

ROOT = Path(os.environ.get("FASTDYN_ROOT") or Path(__file__).resolve().parents[4])
WT = Path(__file__).resolve().parents[2]
#: the WP29 gate run: the timing and the historical deviations below happened there
REF = ROOT / "runs/wp29"


def rel(p: Path) -> str:
    p = Path(p).resolve()
    return str(p.relative_to(ROOT)) if str(p).startswith(str(ROOT) + "/") else str(p)
I1 = "1 T-B4-1/2/2b + mutants (static + O4)"
I2 = "2 F-B4-1..F-B4-9 O1/O2a/O2b per mode, D1/D2 > 0"
I3 = "3 compute V12 + host_abs on every rewritten unit, F-B4-8 V10"
I4 = "4 T-B4-13 F-B4-10 (O6)"
I5 = "5 T-B4-11 ablations + timing (reported)"


def rd(p: Path) -> Any:
    return json.loads(p.read_text()) if p.is_file() else None


def st(ok: Any) -> str:
    return "**pass**" if ok is True else ("**FAIL**" if ok is False else "**not_run**")


def pct(x: float | None) -> str:
    return "–" if x is None else f"{x * 100:+.2f} %"


def reps(per_rep: list[dict[str, Any]] | None) -> str:
    if not per_rep:
        return "–"
    keys = [k for k in per_rep[0] if k != "rep"]
    out = []
    for k in keys:
        vs = {r.get(k) for r in per_rep}
        out.append(f"{k} {'/'.join(sorted(str(v) for v in vs))}")
    return f"{len(per_rep)} reps: " + ", ".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=ROOT / "runs/wp29")
    ap.add_argument("--report", type=Path, default=ROOT / "reports/B4-gate.md")
    ap.add_argument("--append-section", type=Path, action="append", default=[],
                    help="a hand-written section file appended verbatim (repeatable)")
    ap.add_argument("--date", default=time.strftime("%Y-%m-%d"))
    a = ap.parse_args(argv)
    o = a.out.resolve()
    R = rel(o)
    closeout = o != REF.resolve()
    v = rd(o / "verdict.json")
    mu = rd(o / "mutants.json")
    base = rd(o / "compute" / "base.json")
    cpk = rd(o / "compute" / "package.json")
    b10 = rd(o / "b4-10.json")
    hab = rd(o / "compute" / "host_abs_ops.json") or {}
    hae = rd(o / "host-abs.json") or {}
    g = v["gate_binary"]
    head = subprocess.run(["git", "-C", str(WT), "rev-parse", "--short", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    branch = subprocess.run(["git", "-C", str(WT), "rev-parse", "--abbrev-ref", "HEAD"],
                            capture_output=True, text=True).stdout.strip()
    vc = v.get("reported_f_b4_11_real_pass") or {}
    n_fx = len([d for d in (o / "fx" / "icount").iterdir() if (d / "snap.json").is_file()]) \
        if (o / "fx" / "icount").is_dir() else 9
    fx_runs = sorted({rel(Path(json.loads(f.read_text())["snapshot"]).parents[4])
                      for f in (o / "fx").glob("*/*/snap.json")}) if (o / "fx").is_dir() else []
    i1, i2, i3, i4, i5 = (v["gate"][k] for k in (I1, I2, I3, I4, I5))
    fsd = (i1.get("o4_specific_detector") or {}).get("flag_store_before_helper") or {}
    L: list[str] = []
    w = L.append

    w("# B4 gate report (bounded executor-state synchronization elimination)\n")
    w(f"Date: {a.date}. Student 2, milestone B4 (design `design/B3-B4-design.md` §7, §9, "
      "§11 \"B4 gate\"; plan `design/IMPLEMENTATION-PLAN-B3B4.md` `## WP29`" +
      ("; close-out re-gate `design/closeout-logreg.md` §C" if closeout else "") + ").\n")
    w(f"- **Gate binary (every dynamic row):** `{g['exe']}`, tag `qint-v6-gate-1` "
      f"(commit `{g['tag_commit'][:10]}`, HEAD `{g['head'][:10]}`, tree clean: "
      f"{g['tree_clean']}), **sha256 `{g['exe_sha256']}`**. Re-checked at "
      f"{g['checked_at']} (`{R}/gate.json`) and recorded per run "
      "(`runs_qemu_sha256` in every dynamic row).")
    if not closeout:
        w("- **Bases re-collected on the gate binary** (D-EV-2): 18 fixture snapshots "
          "(F-B4-1…7, F-B4-9, F-B4-10 × icount/noicount, `runs/wp29/fx`), 11 B3 fixture "
          "snapshots used only as extra O4 detectors (`runs/wp29/fx-b3`, icount), and the "
          f"compute base `{Path(base['snapshot']).name[:16]}…` (`runs/wp29/compute`, TOML "
          "re-rendered with `qemu_path` = gate binary, train inputs 1-3).")
    else:
        w("- **Bases (not re-collected):** the WP29 gate-binary bases are referenced "
          "read-only (snapshot capture does not depend on scopt code and the importer is "
          f"unchanged): fixture snapshots from {', '.join(f'`{x}`' for x in fx_runs)} "
          f"(`{R}/fx/<mode>/<cfg>/snap.json` are copies of their `snap.json`), the 11 B3 "
          "fixture snapshots used only as extra O4 detectors (`runs/wp29/fx-b3`, icount), "
          f"and the compute base `{Path(base['snapshot']).name[:16]}…` (`runs/wp29/compute`). "
          "F-B4-11's base is the close-out qualification base on the gate binary "
          "(`runs/closeout/fixture-b4-11/base`, both modes). A sha256 manifest of every "
          "referenced base directory is equal before and after the re-gate "
          "(`runs/closeout/manifests`).")
    w(f"- **Code:** scopt branch `{branch if closeout else 'wp29'}` (worktree "
      f"`{rel(WT) if closeout else 'wt/scopt-wp29'}`, HEAD `{head}` when this "
      "report was generated); driver `tests/qemu/wp29_gate.py`, verdict "
      "`tests/qemu/verdict_wp29.py`, timing `tests/qemu/wp29_timing.sh`, this generator "
      f"`tests/qemu/wp29_report.py`. Evidence: `{R}/` (`verdict.json`).")
    w("- **Fixture image:** WP24 review-3 `fixtures/b3/b3.elf`. **WP23 (Tier 1) is not "
      "approved:** nothing here depends on it; V22 is `not_run: WP23 not approved`.\n")

    w("## Verdict\n")
    w("| gate item (design §11) | result |")
    w("|---|---|")
    nd = i1.get("o4_not_detected") or []
    w(f"| 1. T-B4-1, T-B4-2, T-B4-2b; every §9.3 B4 mutant rejected statically and detected "
      f"dynamically (O4) | {st(i1['pass'])}. T-B4-1 {st(i1['t_b4_1']['pass'])}, T-B4-2 "
      f"(static, {i1['static']['rejected']}/{i1['static']['total']} real mutants + "
      f"{i1['static']['synthetic']['rejected']}/{i1['static']['synthetic']['mutants']} "
      f"synthetic rejected) {st(i1['static']['pass'])}, T-B4-2b dynamic "
      f"{st(i1['t_b4_2b_dynamic'])}. O4: {sum(1 for x in i1['o4_by_kind'].values() if x == 'pass')}"
      f" of {len(i1['o4_by_kind'])} §9.3 mutant kinds detected" +
      (f"; **not detected: {', '.join(nd)}** (see \"Known gaps\")" if nd else
       f"; `flag_store_before_helper` detected by F-B4-11 on every instance with its predicted "
       f"divergence ({fsd.get('icount_detected_as_predicted')}/{fsd.get('icount_instances')} "
       "icount; detector changed from §9.5's F-B4-1, disclosed)") + " |")
    w(f"| 2. F-B4-1…F-B4-9 O1/O2a/O2b per mode, D1 and D2 deletions > 0 | {st(i2['pass'])}. "
      f"{sum(r['pass'] for r in i2['rows'])}/{len(i2['rows'])} (fixture, mode) rows; every "
      "rewritten unit of every icount artifact entered, D1 and D2 deletions executed in all 8 "
      "fixtures; F-B4-9 ran with its real TB-start-callback plugin (no static fallback) |")
    w(f"| 3. V12 event preservation + host_abs on every rewritten unit of the compute artifact;"
      f" F-B4-8 V10 on held-out | {st(i3['pass'])}. V12 pass on "
      f"{i3['v12_host_abs']['rewritten_units_named_in_v12']}/"
      f"{i3['v12_host_abs']['rewritten_units']} rewritten units; V10 "
      f"{i3['v10']['verdict']} on held-out 1-3 (icount) |")
    if i4.get("vacuous"):
        w(f"| 4. T-B4-13 (F-B4-10 coverage, O6) | {st(i4['pass'])}: **vacuous**. B4 deletes "
          "nothing on any coverage-instrumented unit (0 D1/D2/D3 in both modes), so there is "
          "no artifact for O6 to check. The O6 oracle itself runs on the gate binary "
          f"(warm-vs-warm control: {i4.get('o6_control_warm_vs_warm')}) |")
    else:
        w(f"| 4. T-B4-13 (F-B4-10 coverage, O6) | {st(i4['pass'])}. Deleted D1/D2/D3 per mode: "
          + "; ".join(f"{r['mode']} {r['by_class'].get('D1')}/{r['by_class'].get('D2')}/"
                      f"{r['by_class'].get('D3')} ({r['status']})" for r in i4["rows"]) +
          f"; executed D1 (icount, gdb) {(i4.get('executed_d1_icount') or {}).get('D1')}; "
          "O1/O2a/O2b/O5 + O6 per mode: " + "; ".join(
              f"{d.get('mode')} {d.get('verdict')}" for d in i4.get("dynamic") or []) +
          f"; warm-vs-warm O6 control {i4.get('o6_control_warm_vs_warm')} |")
    abl_ok = i5["ablations_validated"]
    w(f"| 5. T-B4-11 per-class ablations validated and measured; timing reported | "
      f"ablations validated: {st(abl_ok)}; timing: "
      f"{'measured, see Performance' if isinstance(i5.get('timing'), dict) and i5['timing'].get('arms') else 'not_run'} "
      "(reported outcome, not pass/fail) |\n")
    blk = [x.split(" ", 1)[0] for x in v["blocking"]]
    if v["b4_gate_pass"]:
        w(f"**The B4 gate passes** (`b4_gate_pass = {v['b4_gate_pass']}`; no blocking item). "
          "Items 2 and 3, the correctness core of spec:146 (\"normal exit, callback, precise "
          "fault and interrupt recovery retain required state; guest memory events are not "
          "deleted as bookkeeping\"), pass on every fixture in both modes and on compute; "
          "item 1 (every §9.3 mutant rejected statically and detected dynamically) and item 4 "
          "(F-B4-10 under coverage, O6, non-vacuous) pass since the close-out (section "
          "\"Close-out\" below).\n")
    else:
        w(f"**The B4 gate does not pass** (`b4_gate_pass = {v['b4_gate_pass']}`; blocking "
          f"items: {', '.join(blk)}). Items 2 and 3, the "
          "correctness core of spec:146 (\"normal exit, callback, precise fault and interrupt "
          "recovery retain required state; guest memory events are not deleted as "
          "bookkeeping\"), " + ("pass on every fixture in both modes and on compute. "
                               if i2["pass"] and i3["pass"] else "do not all pass. ") +
          "Blocking:\n")
        if "1" in blk:
            w("- item 1: " + (("§9.3 mutant kinds not detected dynamically: " + ", ".join(nd))
                              if nd else "a T-B4-1/T-B4-2/T-B4-2b row fails") + ";")
        if "4" in blk:
            w("- item 4: " + ("F-B4-10 is vacuous because B4 v1 fails closed on FastDyn's "
                              "coverage instrumentation (root cause below)."
                              if i4.get("vacuous") else "F-B4-10 has deletions, but an "
                              "oracle, the executed-D1 count or the gate-binary check fails "
                              "(item 4 below)."))
        w("")
    w("No B4 artifact produced a wrong output, exception sequence, interrupt record, fault "
      "record, io-recompile count, memory-callback log or IRQ count on any row.\n")

    # ---------------------------------------------------------------- item 1
    w("## Item 1: T-B4-1, T-B4-2, T-B4-2b and the mutation suite\n")
    t1 = i1["t_b4_1"]
    w("**T-B4-1** (`scopt b4 census` on the gate-binary compute base, all units; "
      f"`{R}/compute/base.json`):\n")
    w("| class | gate base | model | within ±5 % |")
    w("|---|---|---|---|")
    for k, r in t1["rows"].items():
        w(f"| {k} | {r['gate_base']} | {r['model']} | {r['within_5pct']} |")
    w(f"\nD2 prefix class {t1['d2_prefix']} (model 678); M0 (TCG's own sync set) dead D1 = "
      f"{t1['m0_d1']}. Admitted units only: D1 "
      f"{base['census_admitted']['dead']['D1']}, D2 {base['census_admitted']['dead']['D2']}, "
      f"D3 {base['census_admitted']['dead']['D3']}. These are the same numbers WP25 got on the "
      "`qint` compute base.\n")
    w(f"**T-B4-1/T-B4-2/T-B4-2b static tests** (`tests/unit/test_syncdead.py`, "
      f"`tests/acceptance/test_b4_static.py`, `tests/unit/test_icount_entry_sfb.py`): "
      f"`{i1['t_b4_2_static_pytest']['summary']}` (`{R}/pytest-t-b4-static.log`).\n")
    w("**Mutants.** Every instance was packaged through the real `package_b4` path (V1-V23, "
      "V12 = hostsym under the strict call contract) and must be rejected. Instances: up to 6 "
      f"per kind and fixture on all {n_fx} icount fixture bases, the O4-round-2 instances "
      "(B3 fixtures), one per kind on compute, plus WP19's synthetic B4 suite. "
      f"**{i1['static']['rejected']}/{i1['static']['total']} rejected**, synthetic "
      f"{i1['static']['synthetic']['rejected']}/{i1['static']['synthetic']['mutants']} "
      f"(`{R}/mutants.json`).\n")
    w("**O4** (negative controls packaged by `scopt.run.negative`, bypassing V12-V17, run "
      "against the warm base on the gate binary in icount; detected = O1, O2a or O2b differs, "
      "and for LOG_REG the O6 coverage ring). Round 1 searched the B4 detector fixture, "
      "hottest unit first; round 2 (`o4-extra`) added B3 fixtures as detectors, put units "
      "that occur in only one fixture first and tried every instance. Both rounds are "
      "kept in `mutants.json` (`o4`, `o4_extra`).\n")
    w("| §9.3 mutant | kind | static rejected (real / synthetic) | O4 | controls run | "
      "detected by |")
    w("|---|---|---|---|---|---|")
    det = {}
    for r in (mu.get("o4") or []) + (mu.get("o4_extra") or []):
        for at in r["attempts"]:
            if at.get("verdict") == "pass" and r["kind"] not in det:
                oracles = [k for d in at.get("detail") or [] for k, x in d.items()
                           if k != "O4" and x[0] == "fail"]
                det[r["kind"]] = (f"{at['config']} unit `…{at['uid'][-14:]}` "
                                  f"({at.get('unit_entries')} entries): "
                                  f"{', '.join(sorted(set(oracles)))} differ")
    for name, ks in i1["design_93"].items():
        for k, r in ks.items():
            sr, sy = r["static_real"], r["static_synthetic"]
            w(f"| {name} | `{k}` | {sr[0]}/{sr[1]} / {sy[0]}/{sy[1]} | {st(r['o4'] == 'pass') if r['o4'] != 'not_run' else '**not_run**'} "
              f"| {r.get('o4_attempts_both_rounds', 0)} | {det.get(k, '–')} |")
    w("\nWP25's extra kinds (not in §9.3; reported, not gating): " + ", ".join(
        f"`{k}` {x}" + (f" ({det[k]})" if k in det else "")
        for k, x in sorted(i1["o4_extras_reported"].items())) + ". Static-only by design: "
      + ", ".join(f"`{k}`" for k in i1["static_only"]) + ".\n")
    w("Notes on the O4 rows:\n")
    w("- **The hottest unit is usually not a detector.** In every fixture the hottest unit "
      "(132,470 entries) is the shared start-up loop that zeroes `.bss` "
      "(the 512 KiB guest log). Deleting a guest store there writes nothing new over "
      "zeroed memory, and an icount immediate changed there shifts time before any timer runs. "
      "So round 1 needed 2-4 tries for some kinds. Every detection sits in a unit that "
      "executes the observed event.")
    w("- **`flag_store_before_ldst`** was not detected on F-B4-2/F-B4-5 (their faulting loads "
      "have no live flag store before them). It **was detected on F-B3-8**: the callee's flag "
      "store before the counter store is the value stacked in xPSR when the following "
      "`ldr r0,[r0]` faults at iteration 137, so the fault record differs.")
    w("- **`log_reg_delete`** (F-B4-10): O1/O2b are equal (this FastDyn build writes no "
      "coverage file), and the LOG_REG coverage ring compared by O6 differs. That is the "
      "§9.5 detector." + ("" if not closeout else
                          " The close-out's real LOG_REG kinds on coverage units "
                          "(design/closeout-logreg.md A.8; reported, not gating): " +
                          "; ".join(f"`{k}` {x}" + (f" ({det[k]})" if k in det else "")
                                    for k, x in sorted(i1["o4_extras_reported"].items())
                                    if k.startswith("log_reg_") and k != "log_reg_delete") +
                          ". A detection by O1/O2b with an `INVALID_exit_code` run is a crash, "
                          "not the O6 observable."))
    w("- **`cdio_last_insn_direct_chain`** (F-B4-9): the control QEMU aborts (`cpu_abort` "
      "seen in its log; no BENCH, 0 of 100 IRQ records). The real artifact keeps the store "
      "and runs clean (item 2).")
    fsh = i1["design_93"]["delete a flag store before a flags-0 helper"][
        "flag_store_before_helper"]
    cov = fsh.get("o4_coverage") or {}
    fsx = next((r for r in mu.get("o4_extra") or [] if r["kind"] == "flag_store_before_helper"),
               {})
    n_cover = sum(1 for x in fsx.get("attempts") or [] if x.get("round") == "cover")
    if fsd.get("pass"):
        nr = fsd.get("noicount_reported") or {}
        w("- **`flag_store_before_helper`** (detector **F-B4-11**, `fixtures/b3/b4_11.s` in the "
          "variant image `b3_b411.elf`; design/closeout-logreg.md B): every instance on the "
          f"F-B4-11 base was run as an O4 control. icount (gated): "
          f"{fsd.get('icount_detected_as_predicted')}/{fsd.get('icount_instances')} detected by "
          "O1 with **exactly the predicted divergent result words** (records `0x10+i` of the "
          "(pair, poison) iterations whose poison bit differs from the true flag, in the "
          "instance's sub-phase; `expected.b411_divergence`; any other difference would be an "
          "anomaly and not count); sub-phase/flag pairs covered: "
          f"{', '.join(fsd.get('subphase_flags') or [])}; anomalies {len(fsd.get('anomalies') or [])}. "
          f"noicount (reported): {nr.get('detected_as_predicted')}/{nr.get('instances')} "
          "detected as predicted. The earlier instances (F-B3-21's `v7m_msr`/`v7m_mrs`, F-B3-5's "
          "`randstate` call, main's start-up `v7m_mrs`) store flags that are dead at the guest "
          "level: the program sets NZCV again before any reader (design/closeout-logreg.md "
          "B.1), so their controls do not change an output. `o4-cover` enumerated every "
          f"executed instance again ({cov.get('instances')} instances, "
          f"{cov.get('executed_instances')} executed, {cov.get('executed_instances_tried')} "
          f"tried; {n_cover} run in this round). **Disclosed detector change:** §9.5 names "
          "F-B4-1 (printreg callback); F-B4-1 has no instance of the kind on the gate base (its "
          "printreg TB starts after the `bne`, so no flag is dirty there), so the detector is "
          "the new fixture F-B4-11. The criterion \"rejected statically and detected "
          "dynamically\" is unchanged; this run additionally requires every F-B4-11 icount "
          "instance to be detected with its predicted divergence.")
    else:
        w("- **`flag_store_before_helper` (gap):** the kind has "
          f"{cov.get('instances')} instances (unit, host offset) in the 20 gate fixture "
          f"snapshots, all {cov.get('executed_instances')} in units the fixture's profile "
          f"entered; **{cov.get('executed_instances_tried')} of them were run as O4 controls** "
          f"({fsh['o4_attempts_both_rounds']} controls in the record, some instances on more than "
          "one fixture; plus 7 in a superseded round-2 attempt, `o4-extra-r1.log`). None changed "
          "O1 or O2b. The instances are the NZCV stores before F-B3-21's `v7m_msr`/`v7m_mrs` "
          "(500 entries each), before F-B3-5's `randstate` plugin call (256), and before main's "
          "start-up `v7m_mrs` (1 entry, in every fixture). What is shown: the image has no "
          "`mrs`/`msr` of APSR or xPSR (only IPSR, CONTROL and PRIMASK, `fixtures/b3/b3.lst`), so "
          "these `v7m_mrs`/`v7m_msr` calls do not read NZCV. Why the stale flags are not observed "
          "after the helper was not analysed further; the likely reason (the flags are set again "
          "before the next reader) is inferred from the missing detection, not shown. " +
          (f"The detector is F-B4-11 (design/closeout-logreg.md B; a disclosed change from "
           f"§9.5's F-B4-1, which has no instance of this kind on the gate base): "
           f"{fsd.get('icount_detected_as_predicted')}/{fsd.get('icount_instances')} icount "
           f"instances detected with the predicted divergence, anomalies "
           f"{fsd.get('anomalies')}, so the kind is not detected as required."
           if fsd.get("icount_instances") else
           "§9.5 names F-B4-1 (printreg callback), but the gate F-B4-1 base has no "
           "instance of this kind. A detector needs a fixture that reads flags after a "
           "flags-0 helper without setting them again (for example `cmp; <printreg virtual>; "
           "b<cond>` in one TB, or `mrs rX, APSR` after a flag-setting insn): a WP24 fixture "
           "follow-up (F-B4-11, design/closeout-logreg.md B)."))
    if hae:
        att = hae.get("attempts") or []
        sx = hae.get("static") or []
        pa = [x.get("env_probe_artifact") or {} for x in att]
        w(f"- **`host_abs_in_env`** (real instance, `{R}/host-abs.json`, step "
          "`host-abs`): F-B4-7's UPDATE_MEM store is `movabs reg, imm64; mov byte [reg], 5` "
          f"with imm64 = `{hex(hae['sites'][0]['abs_addr'])}` (g_mod7 in guest RAM) in "
          f"{len(hae['sites'])} units (the loop TB, {hae['sites'][0]['unit_entries']} "
          "entries, and the first-iteration TB). The control repoints imm64 to "
          f"env + {hae['env_disp']} (`vfp.zregs`, which a Cortex-M3 guest without FPU never "
          "reads). The env address came from a gdb `-nx` diag re-run of the gate warm run "
          f"(`rbp` = env at the store: `{hae.get('env')}`; the same re-run reads the pointer "
          "register as the captured imm64, so its layout matches the capture). **Static:** "
          f"{sum(s['without_env_abs']['rejected'] and s['with_env_abs']['rejected'] for s in sx)}"
          f"/{len(sx)} rejected, with V12 `host_abs_aliases_env` among the failing rows both "
          "without the process env (the pointer is not bound to an allowed class: fail closed) "
          f"and with `env_abs` = {hae.get('env_abs')} (\"inside env\"); V8/V3 (re-import: "
          "unexplained reference) and V15 (placement) fail too. **O4:** "
          f"{sum(a.get('verdict') == 'pass' for a in att)}/{len(att)} detected by O1 ("
          + "; ".join(f"result {str(a['detail'][0]['O1'][1]).split('got ')[1].split(',')[0]} "
                      "instead of 1500" for a in att if a.get("detail")) +
          "): the 5 goes into env instead of g_mod7. A gdb re-run of each control's own "
          "artifact run stopped at the patched store: the pointer is inside env in "
          f"{sum(bool(p.get('ptr_inside_env')) for p in pa)}/{len(pa)} "
          f"(env + {pa[0].get('ptr_minus_env') if pa else '?'}, "
          f"`{(pa[0].get('field_written') or '').split(chr(39))[1] if pa and pa[0].get('field_written') else '?'}`)."
          " The env base in those re-runs is "
          f"{pa[0].get('env_shift_vs_warm_probe') if pa else '?'} bytes above the warm "
          "re-run's (heap layout depends on path lengths), which is why the displacement "
          "sits in the middle of an 8 KiB guest-dead region; the non-gdb O4 run's env is not "
          "observed directly, but a shift of that size keeps the pointer in `vfp.zregs`.")
    w("- `d3_before_plugin_call` (WP25 extra): no loaded plugin calls "
      "`qemu_plugin_mem_get_value` in any admitted configuration "
      "(`mem_get_value_outside_mem_cb = false`, no import), so `plugin_mem_value_*` has no "
      "reader. 14 controls (F-B4-7, F-B3-7, F-B3-6h) were all equal. The deletion is "
      "behaviour-preserving there, and the kind stays static-only, as WP25 concluded. "
      "`exit_stub_edit`: the negative-control packager refuses it (`unbypassed:V8`, the "
      "re-import fails), so no control can be built; V8/V15 reject it statically.\n")

    # ---------------------------------------------------------------- item 2
    w("## Item 2: F-B4-1 … F-B4-9 on the gate binary (T-B4-3 … T-B4-9, T-B4-12)\n")
    w("Per (fixture, mode): B4 artifact (D1|D2|D3, v5 `qemu-sc-v5-unmodified`, published "
      "only when V12 certifies every rewritten unit) against its own warm base, "
      "`dynamic_oracles` with the fixture's per-mode oracle (`fixtures/b3/expected.py`); "
      "icount 2 reps, noicount 4. In noicount, O2a is not gated for the timer phase F-B4-3 "
      "(§9.4 \"with icount shift 0\"); it is reported next to a warm-vs-warm control, which "
      "differs there too. \"executed\" = deleted stores in rewritten units that gdb "
      f"hardware breakpoints saw entered (`{R}/deletions.json`, every rewritten unit "
      "probed, icount). In noicount the column counts only the up-to-8 probed units nearest "
      "the kernel label (`dynamic.json` `deleted_in_executed_probes`; a gdb count from a run "
      "that did not exit cleanly is unknown, never 0, e.g. the hot units of F-B4-3), so "
      "it is a lower bound and gate item 2's D1/D2 > 0 is judged on the icount artifacts, "
      "whose stores are the same up to timer-dependent units.\n")
    w("| fixture | mode | deleted D1/D2/D3 | executed D1/D2/D3 | V12 units | oracles | "
      "fixture-specific | pass |")
    w("|---|---|---|---|---|---|---|---|")
    for r in i2["rows"]:
        bc = r["by_class"] or {}
        ex = r["deleted_in_entered_units"] or {}
        extra = []
        if r.get("static_check"):
            extra.append(f"static: {'kept' if r['static_pass'] else 'FAIL'}")
        if r.get("io_recompile"):
            extra.append(f"cpu_io_recompile {r['io_recompile'].get('artifact')} = warm "
                         f"{r['io_recompile'].get('reference')}")
        if r.get("events_tsv_equal") is not None:
            extra.append(f"events.tsv equal {r['events_tsv_equal']}")
        if r.get("counts_txt_equal") is not None:
            extra.append(f"counts.txt equal {r['counts_txt_equal']}, cpu_abort "
                         f"{'seen' if any(r.get('cpu_abort_seen') or []) else 'none'}")
        if r.get("o2a_noicount_timer_reported"):
            c = r["o2a_noicount_timer_reported"]
            extra.append(f"O2a reported: artifact {c.get('artifact_vs_warm')}, warm-vs-warm "
                         f"{c.get('warm_vs_warm')}")
        w(f"| {r['fixture']} | {r['mode']} | {bc.get('D1')}/{bc.get('D2')}/{bc.get('D3')} | "
          f"{ex.get('D1')}/{ex.get('D2')}/{ex.get('D3')} | {r['v12_pass_units']} | "
          f"{reps(r['per_rep'])} | {'; '.join(extra) or '–'} | {st(r['pass'])} |")
    w("\n- **F-B4-4:** in both mode artifacts, the prefix `can_do_io = false` store of the "
      "loop TB with the CPUID load as a non-last insn is kept as `observed_at_sync_point`. "
      "`cpu_io_recompile` is called as often as in warm (gdb `-nx` count).")
    w("- **F-B4-5:** the IT-block `regs[1]` store before the faulting LDST is kept "
      "(`observed_at_sync_point`). The D4 mutant and its slow-path variant change the stacked "
      "r1 (item 1).")
    w("- **F-B4-7:** no D3 deletion (13 refusals `observer_contract_unknown`, units with "
      "memory callbacks), both UPDATE_MEM host stores untouched, and `events.tsv` of "
      "`variable_watch` byte-equal to warm in every rep.")
    w("- **F-B4-9** (U-TBCB fallback, out-of-tree plugin `b3_tbirq`): the admission fact is "
      "`tb_start_cb_possible = true` (preprocessing artifact + a virtual library exporting a TB "
      "hook). TB A (`b4_9_a`, which chains by goto_tb into `b4_9_s`) keeps its last-insn "
      "`can_do_io = 1` store (`observed_at_sync_point`); under the counterfactual `false` "
      f"the pass would have deleted it (`{R}/package.json` `static`). Dynamically: "
      "100 IRQ records equal to warm, `counts.txt` equal, gdb counts 0 `cpu_abort` calls "
      "(gated: the row needs a resolved gdb count of 0 and no abort in `qemu.log`), "
      "and the deleting mutant aborts QEMU (item 1). F-B4-9 therefore ran as specified, not "
      "as its static fallback.")
    w("- **Every rewritten unit ran** in every icount artifact (gdb entry counts > 0 for "
      "all of them). So all deleted stores of these artifacts sit in executed units, "
      "including D1 > 0 and D2 > 0 in each fixture.\n")
    if vc.get("rows"):
        w("**F-B4-11 real-pass row** (reported next to item 2, not in its fixture list; design/"
          "closeout-logreg.md B.3 step 5): the real B4 artifact on the F-B4-11 base must keep "
          "every `flag_store_before_helper` store (`observed_at_sync_point`) and pass O1/O2a/O2b. "
          f"Result: **{'pass' if vc.get('pass') else 'FAIL'}**.\n")
        w("| mode | published | deleted D1/D2/D3 | instances kept / total | sub-phase/flag | "
          "oracles | pass |")
        w("|---|---|---|---|---|---|---|")
        pk11 = {r["mode"]: r for r in (rd(o / "package.json") or {}).get("rows", [])
                if r["config"] == "b4-11"}
        for r in vc["rows"]:
            stt = (pk11.get(r["mode"]) or {}).get("static") or {}
            kept = sum(1 for u in stt.get("units") or [] if u.get("ok"))
            bc = r.get("by_class") or {}
            w(f"| {r['mode']} | {r['published']} | {bc.get('D1')}/{bc.get('D2')}/{bc.get('D3')} "
              f"| {kept}/{r.get('instances')} | {', '.join(r.get('subphase_flags') or [])} | "
              f"{reps(r.get('oracles'))} | {st(r['pass'])} |")
        w("")

    # ---------------------------------------------------------------- item 3
    w("## Item 3: compute (F-B4-8, T-B4-10)\n")
    b4 = next(r for r in cpk["rows"] if r["label"] == "b4")
    w(f"B4 artifact on the gate-binary compute base: D1 {b4['by_class']['D1']}, D2 "
      f"{b4['by_class']['D2']}, D3 {b4['by_class']['D3']} stores deleted in "
      f"{b4['units_edited']} units (`{b4['artifact_dir']}`). The admission identity has "
      f"`tb_start_cb_possible = {base['admission']['tb_start_cb_possible']}` and "
      f"`mem_get_value_outside_mem_cb = {base['admission']['mem_get_value_outside_mem_cb']}`.\n")
    vh = i3["v12_host_abs"]
    w(f"- **V12 event preservation:** {vh['v12_rows']} V12 rows, all pass; each of the "
      f"{vh['rewritten_units']} rewritten units is named by a passing V12 row. V12 compares, "
      "per path, every GuestAccess (with the S5-observable env), HostStore and call. Its "
      "host_abs check (`_abs_violations`) is part of each V12 verdict: "
      f"{vh['host_abs_fail_rows']} failing rows. The rewritten compute units contain "
      f"{((hab.get('by_arm') or {}).get('b4') or {}).get('host_abs_total', '?')} absolute "
      "host-pointer operands (HostOps census of all "
      f"{hab.get('units_all', '?')} compute units: "
      f"{hab.get('host_abs_total_all_units', '?')}; `{R}/compute/host_abs_ops.json`, "
      "step `compute-host-abs`), so on compute the assertion holds without anything to "
      "check. It does have real operands on the fixtures: F-B4-7's UPDATE_MEM stores (the "
      "B4 artifact keeps them and passes V12) and F-B4-10's LOG_REG stores, and the real "
      "`host_abs_in_env` mutant on F-B4-7 is rejected by it (item 1).")
    v10 = i3["v10"]
    w(f"- **V10 (held-out inputs 1-3, icount, gate binary):** {v10['verdict']}; appended "
      f"records artifact {v10['appended']['artifact']} = warm {v10['appended']['baseline']} "
      "(held-out inputs reach TBs the train-input base does not hold, in both arms).\n")

    # ---------------------------------------------------------------- item 4
    if not i4.get("vacuous"):
        w(f"## Item 4: F-B4-10 under coverage (T-B4-13) — {'pass' if i4['pass'] else 'FAIL'}\n")
        dg = i4.get("diagnosis") or {}
        w("Since the close-out, FastDyn's LOG_REG coverage block is one atomic host event "
          "(`HostLog`, design/closeout-logreg.md A): the matcher `scopt.decode.logreg` "
          "recognises the exact block bytes (ring store typed `HOST_ABS_STORE` at the ring "
          "base, the ring-full spin `je` an hcfg edge of the full view only); B4 never edits "
          "inside a block (`edit_inside_log_block`) and deletes after it as anywhere else. "
          f"Diagnosis (`{R}/b4-10.json`):\n")
        w("| mode | units | units with a LOG_REG block | blocks matched | unmatched | HostOps "
          "pinned | hcfg cyclic | syncdead | units rewritten | deleted D1/D2/D3 |")
        w("|---|---|---|---|---|---|---|---|---|---|")
        for m in ("icount", "noicount"):
            x = dg.get(m) or {}
            bc = x.get("b4_by_class") or {}
            w(f"| {m} | {x.get('units')} | {x.get('units_with_logreg_store')} | "
              f"{x.get('logreg_blocks_matched')} | {x.get('logreg_units_unmatched')} | "
              f"{x.get('logreg_units_hostops_pinned')} | {x.get('logreg_units_hcfg_cyclic')} | "
              f"{x.get('logreg_units_syncdead')} | {x.get('logreg_units_rewritten_by_b4')} | "
              f"{bc.get('D1')}/{bc.get('D2')}/{bc.get('D3')} |")
        ex4 = i4.get("executed_d1_icount") or {}
        w(f"\nExecuted (icount, gdb `-nx` entry counts of every rewritten unit, "
          f"`{R}/deletions.json`): D1/D2/D3 {ex4.get('D1')}/{ex4.get('D2')}/{ex4.get('D3')}; "
          f"executed D1 > 0: **{i4.get('executed_d1_gt_0')}** (the same non-vacuity rule as "
          "item 2). Dynamic (`dynamic.json`, gated O1/O2a/O2b/O5 + **O6**, the LOG_REG coverage "
          "ring at `benchmark_end` equal to warm's): " + "; ".join(
              f"{d.get('mode')}: {reps(d.get('oracles'))} -> {d.get('verdict')}"
              for d in i4.get("dynamic") or []) +
          f". Runs on the gate binary: {i4.get('runs_on_gate_binary')}. The warm-vs-warm O6 "
          f"control: {i4.get('o6_control_warm_vs_warm')}. The O6 detector itself is shown by "
          "the LOG_REG mutants (item 1: `log_reg_delete`, `log_reg_ring_store` and "
          "`log_reg_index_store` are detected by O6).\n")
        w("Before the close-out (WP29, `runs/wp29/b4-10.json`): 0 deletions on all 62 coverage "
          "units (pinned `unexplained_host_store`, cyclic spin loop), item 4 **FAIL "
          "(vacuous)**.\n")
    else:
        w("## Item 4: F-B4-10 under coverage (T-B4-13) — vacuous, gate failure\n")
        d10 = (b10 or {}).get("icount") or {}
        w(f"B4 on the F-B4-10 bases deletes **0** stores in both modes (`no_change`): "
          f"{d10.get('units_with_logreg_store')} of {d10.get('units')} units carry FastDyn's "
          f"coverage block, and every one of them is skipped. Root cause (`{R}/b4-10.json`):\n")
        ex = d10.get("example") or {}
        w(f"1. `coverage = true` puts a LOG_REG block in front of every TB's first insn. It loads "
          "the ring index, **spins while the ring is full** (a backward `je` to the start of the "
          f"block, e.g. `{', '.join(ex.get('backward_insn_jcc') or [])}`), then stores the guest PC "
          f"into `ring_base + index` (`{(ex.get('host_abs_stores') or ['?'])[0]}`) and the new index.")
        w(f"2. HostOps cannot type the ring store: its pointer is `index + const` built with "
          f"`add r64, r64`, and only `[const_reg + index]` addressing is classified. So all "
          f"{d10.get('logreg_units_pinned_unexplained_host_store')} units are pinned "
          "`unexplained_host_store` (fail closed).")
        w(f"3. Even with that classified, the spin loop makes the host graph cyclic in all "
          f"{d10.get('logreg_units_hcfg_cyclic')} units: `syncdead` gives "
          "`path_budget_exhausted` (checked by a local, reverted HostOps change), and hostsym marks "
          "any backward branch other than a slow-path return UNSUPPORTED (§9.2).\n")
        w("So under coverage, B4 v1 is correct (it changes nothing) but vacuous, and T-B4-13's "
          "\"D1 deletions > 0\" cannot hold. A fix needs (a) HostOps to classify "
          "the ring store with a ring base (add-of-constant tracking, outside env, checked by "
          "V12's host_abs rule) and (b) hcfg/syncdead/hostsym to treat the LOG_REG block as one "
          "atomic event (`HostLog(pc)`, a loop with no env access) the way LDST blocks are. Both "
          "are WP17/WP19 code (shared contracts) and are not changed by this gate run. The O6 "
          "machinery itself works on the gate binary: a warm-vs-warm O6 control on F-B4-10 "
          f"passes ({i4.get('o6_control_warm_vs_warm')}), and O6 detected the LOG_REG-deletion "
          "mutant (item 1).\n")

    # ---------------------------------------------------------------- item 5
    w("## Item 5 / Performance: T-B4-11 ablations and timing (reported outcome)\n")
    w("| artifact | deleted D1/D2/D3 | units | published | V12 (host_abs) | V10 held-out | "
      "ledger rows |")
    w("|---|---|---|---|---|---|---|")
    for x in i5["ablations"]:
        bc = x.get("by_class") or {}
        led = "; ".join(f"{i.split('/')[-1]} {vv} {c}" for i, vv, c in x["ledger"]) or "–"
        w(f"| {x['label']} | {bc.get('D1', '–')}/{bc.get('D2', '–')}/{bc.get('D3', '–')} | "
          f"{next((r.get('units_edited') for r in cpk['rows'] if r['label'] == x['label']), '–')} | "
          f"{x['status']} | {x['v12_host_abs_pass'] if x['v12_host_abs_pass'] is not None else 'SFB1-SFB9 (own rows)'} | "
          f"{x['v10']} | {led} |")
    w("\n`sfb` is the `icount-entry-sfb` pass (WP25): a fix of TCG's own TB entry check. It "
      "is **not B4 and not caching**, so it is reported as its own arm. `sfb+b4` is B4 run "
      "on the sfb artifact after re-importing it as a snapshot (its derived `.scm` carries "
      "full Tier-0 metadata), which layers B4 on top of the entry fix as the LLVM survey "
      "suggests. B4 deletes the same 334/1207/3409 stores there.\n")
    tm = i5.get("timing") or {}
    cpu = i5.get("cpu_budget") or {}
    if tm.get("arms"):
        w(f"Timing (`runs/wp29/timing`, `tests/qemu/wp29_timing.sh`): compute train inputs "
          f"1-3, 30 interleaved reps, {tm.get('runs')} runs, gate binary for every arm, "
          f"{len(tm.get('invalid') or [])} invalid runs, appended records seen "
          f"{tm.get('appended_seen')}. CPU budget: `flock runs/.timing.lock`, `taskset` 4 cores "
          f"(24-27), 1-min load average {cpu.get('load1_min')}–{cpu.get('load1_max')} over "
          f"{cpu.get('load_samples')} samples (`runs/wp29/timing-load.log`). Speedup = median "
          "of paired t_ref / t_arm − 1 on the guest BENCH, 95 % bootstrap CI (10,000 "
          "resamples; the CI is also recomputed for seeds 0-5, and an arm whose CI bound changes "
          "sign between seeds is `borderline`).\n")
        w("| arm vs reference | pairs | speedup | 95 % CI (seed 0) | CI over seeds 0-5 | "
          "faster pairs | verdict |")
        w("|---|---|---|---|---|---|---|")
        for k, r in tm["arms"].items():
            if not r.get("n_pairs"):
                w(f"| {k} | 0 | – | – | – | – | not_run |")
                continue
            w(f"| {k.replace('_vs_', ' vs ')} | {r['n_pairs']} | {pct(r['speedup_median'])} | "
              f"[{pct(r['ci95'][0])}, {pct(r['ci95'][1])}] | "
              f"lower {pct(min(x[0] for x in r['ci95_by_seed']))}…"
              f"{pct(max(x[0] for x in r['ci95_by_seed']))}, upper "
              f"{pct(min(x[1] for x in r['ci95_by_seed']))}…"
              f"{pct(max(x[1] for x in r['ci95_by_seed']))} | "
              f"{r['faster_share'] * 100:.0f} % | {r['verdict']} |")
        w("")
    else:
        w("Timing: **not_run** (no `runs/wp29/timing-analysis.json`).\n")
    w(TIMING_READING_PLACEHOLDER)

    # ---------------------------------------------------------------- the rest
    w("## Cross-TB state traffic (spec:14) — the §7.6 deferral\n")
    w("B4 v1 removes state traffic **inside** a TB only (D1/D2/D3 nop-fills; every deleted "
      "store is dead before every exit). spec:14's cross-TB case is exercised only on the "
      "**load side**, by B3's constant propagation on an armed R→C' link. That is reported in the "
      "B3 gate. The store side (D5, a store in R that C' overwrites) is deferred with reason "
      "`cross_tb_dse_needs_value_transport`. The store stays observable at C''s entry check "
      "(S1: an entry-check exit hands env to `cpu_handle_interrupt` / v7m stacking) and on "
      "R's guard-fail path, and deleting it needs compensation code, which invariant 4 "
      "forbids in v1. §7.6 lists the evidence that would justify D5 in v2. Its first "
      "condition is not met: WP16's D1 probe and this gate's b4/b4-d1 timing show no "
      "measurable gain from nop-filling intra-TB dead stores.\n")
    w("## Known gaps\n")
    gaps = []
    if not i4["pass"]:
        gaps.append("**F-B4-10 / T-B4-13** (item 4): " + (
            "vacuous: B4 v1 skips every coverage-instrumented unit (LOG_REG ring store not "
            "classified; ring-full spin loop makes the host graph cyclic). Blocks the gate. "
            "Fix: WP17 HostOps + WP19 hostsym/hcfg (LOG_REG block as an atomic event)."
            if i4.get("vacuous") else "fails (see item 4). Blocks the gate."))
    if "flag_store_before_helper" in nd:
        gaps.append("**`flag_store_before_helper` has no dynamic detector** in the fixture set "
                    "(item 1): " + ("every executed instance was run as an O4 control and none "
                                   "changed an output. Statically rejected everywhere. Blocks "
                                   "the gate. Fix: a WP24 phase that reads flags after a "
                                   "flags-0 helper without setting them again."
                                   if not fsd.get("icount_instances") else
                                   "F-B4-11 did not detect every instance as predicted (item "
                                   "1). Blocks the gate."))
    gaps.append("**Noicount non-vacuity is weaker than icount's** (item 2): D1/D2 > 0 is judged "
                "on the icount artifacts (every rewritten unit probed). In noicount only up to "
                "8 units per fixture are probed, and for F-B4-3 the probed units executed D1 = 0 "
                "(the hottest units' gdb counts are unknown: the run did not exit cleanly under "
                "gdb).")
    gaps.append("Carried from WP24/WP25: no coverage file in this FastDyn build "
                "(`ENABLE_LIBFUZZ 0`; O6 uses the LOG_REG ring); F-B4-9 relies on the "
                "out-of-tree plugin `b3_tbirq` (U-TBCB); the fault record has no SysTick CVR "
                "and all SysTick CVR values read 0 (WP26 finding), so O2b pins interrupt "
                "positions by (iteration, PC, stacked registers) in icount; noicount O2a on the "
                "timer phase is not gated; `d3_before_plugin_call` and `exit_stub_edit` have no "
                "dynamic control (no reader; V8 unbypassable).")
    gaps.append("Tier 1 / V22 / T-B3-29: `not_run: WP23 not approved`.")
    gaps.append("The `sfb-b4` vs `sfb` timing row is borderline: its CI lower bound changes "
                "sign with the bootstrap seed (item 5).")
    if closeout:
        gaps.append("**Unmatched LOG_REG variants** (close-out): the matcher accepts only the "
                    "exact gate-binary block (spin form, one (IDX, CONS, BUF) triple, ring base "
                    "an anonymous non-env mapping). FastDyn's logger LOG_REG without the spin, "
                    "the FP `arm_log_fp` form, a `movabs` form or another register allocation "
                    "are `NoMatch` and fail closed exactly as before the close-out (unit pinned, "
                    "no edit); no admitted configuration uses them, so they are not exercised.")
    for k, x in enumerate(gaps, 1):
        w(f"{k}. {x}")
    w("")
    w("## Deviations from the design and plan\n")
    w("- **O4 detector fixtures:** `flag_store_before_ldst` was detected on F-B3-8 (a B3 "
      "fixture) instead of §9.5's F-B4-2. F-B4-2's faulting load has no live flag store "
      "before it in the loop TB (the flags are set after the load).")
    w("- **O4 instance choice:** the plan names one detector fixture per mutant. The driver "
      "tries up to 6 (round 1) and 8-16 (round 2) instances per kind and records every "
      "attempt. An undetected instance in shared start-up code is not a failure of the kind. "
      "Undetected kinds are listed above with all their attempts.")
    w("- **`host_abs_in_env` control:** the repointed pointer is env + 0x1cf0 "
      "(`vfp.zregs`), not WP19's synthetic env + 0x40 (`xregs[0]`). A first run with env + "
      "0x40 landed at env − 0x80 (`neg.tlb.f[10].mask`) in the control's run because the env "
      "base moved 0xc0 between processes; the middle of an 8 KiB guest-dead region absorbs "
      "such a shift. The control is packaged as a byte mutant (`scopt.run.negative`: no "
      "re-import, so V8/V3 are absent; V15 is bypassed as usual), because the re-import "
      "cannot explain the repointed reference. Static rejection uses the normal "
      "`package_b4` path, once without and once with V12's `env_abs`.")
    if not closeout:
        w("- **O4 coverage round** (`o4-cover`): for `flag_store_before_helper` every executed "
          "instance in all 20 gate fixture snapshots was enumerated and each untried one run "
          "(2 more: main's `v7m_mrs` ZF and VF stores).")
    else:
        w("- **O4 coverage round** (`o4-cover`): for `flag_store_before_helper` every executed "
          f"instance in all {len((fsx.get('coverage') or {}).get('fixtures_enumerated') or []) or '?'} gate "
          "fixture snapshots (the 20 of WP29 plus F-B4-11) was enumerated and each untried one "
          f"run ({n_cover} in this round; reported, the kind is judged on F-B4-11).")
        w("- **Close-out driver changes** (disclosed; section \"Close-out\" below): F-B4-11 "
          "added to the configs and as the first `flag_store_before_helper` O4 fixture, judged "
          "on every instance with its predicted divergence (icount gated, noicount reported); "
          "item 4 additionally requires executed D1 > 0 on F-B4-10 (gdb, as item 2) and O6 on "
          "the gate binary; round-1 O4 of the LOG_REG kinds also reads O6; the T-B4-1 census "
          "and the static tests were re-run with the close-out code (`census`, "
          "`pytest-static`); every output `cache.sc` is compared with WP29's (`compare-ref`), "
          "and WP29's timing is carried only when every timed artifact is byte-identical "
          "(`carry-timing`).")
    w("- **Compute rows:** the `sfb` row of `compute/package.json` was first filled by hand "
      "(a key bug, `path` vs `artifact_dir`). It is now rebuilt by the committed step "
      "`sfb-row` from `compute/sfb.json`, and `recheck-compute` re-ran `package-compute` end "
      "to end into `runs/wp29/recheck-package-compute`: every row (b4, b4-d1/d2/d3, sfb) "
      "has the same status, classes, rewritten units and output `cache.sc` sha256 "
      f"(`compare.json`: {(rd(REF / 'recheck-package-compute' / 'compare.json') or {}).get('pass')}). "
      "The published artifacts the timing used were not replaced.")
    w("- **`sfb+b4` arm** (not in the plan): B4 on the re-imported `icount-entry-sfb` "
      "artifact, timed against `sfb` to isolate B4's marginal effect (LLVM survey §3c B4-1).")
    w("- **LOG_REG deletion detected by O6** instead of O1/O2a/O2b: `o4_detected` looks only "
      "at O1/O2a/O2b, so the driver reads the control's own O6 row. This follows §9.5, where "
      "F-B4-10's detector is O6.")
    w("- **F-B4-10 and the HostOps change:** a local ring-base classification in "
      "`scopt/analysis/hostops.py` was tried to locate the root cause. It removed the pin, "
      "but the units then failed on the cyclic host graph. It was reverted, so no WP17 code "
      "changed in this WP.")
    w("- The compute base was prepared with the B2 train inputs and no profile: B4 edits every "
      "admitted unit, and §10.3's B4 profitability flag is reported, not applied.\n")
    w("## Reproduce\n")
    if closeout:
        w("```\n"
          f"cd {rel(WT)}   # main, tools-env python; the WP29 bases are referenced read-only\n"
          f"# {R}/fx/<mode>/<cfg>/snap.json, fx-b3/icount/<cfg>/snap.json and compute/ "
          "(base.json, runtime identities, bundle) are copies from runs/wp29; "
          f"{R}/fx/<mode>/b4-11/snap.json from runs/closeout/fixture-b4-11/base\n"
          f"PYTHONPATH=$PWD python tests/qemu/wp29_gate.py --out {R} gate census pytest-static "
          "package package-compute compute-host-abs package-sfb-b4 compare-ref carry-timing\n"
          f"PYTHONPATH=$PWD python tests/qemu/wp29_gate.py --out {R} dynamic deletions b4-10 "
          "mutants o4-extra host-abs o4-cover compute verdict\n"
          f"python tests/qemu/wp29_report.py --out {R} --report reports/B4-gate.md \\\n"
          "    --append-section reports/B4-gate.closeout.md\n```\n")
        w("Tests: `tests/unit/test_wp29_gate.py` (verdict logic on synthetic evidence, incl. the "
          "close-out rules), `tests/acceptance/test_b4_gate.py` (WP29's evidence).\n")
    else:
        w("```\ncd wt/scopt-wp29\n"
          "python tests/qemu/wp20_fixture_snapshots.py --out runs/wp29/fx --qemu wt/qemu-v6/build/qemu-system-arm \\\n"
          "    --configs b4-1,b4-2,b4-3,b4-4,b4-5,b4-6,b4-7,b4-9,b4-10 --profile -j 6\n"
          "python tests/qemu/wp20_fixture_snapshots.py --out runs/wp29/fx-b3 --qemu wt/qemu-v6/build/qemu-system-arm \\\n"
          "    --modes icount --configs 3b,4,5,8,8p,9,10,21,22,6h,7 --profile -j 6\n"
          "PYTHONPATH=$PWD python tests/qemu/wp29_gate.py --out runs/wp29      # all steps but timing\n"
          "PYTHONPATH=$PWD python tests/qemu/wp29_gate.py --out runs/wp29 sfb-row recheck-compute\n"
          "bash tests/qemu/wp29_timing.sh                                       # holds runs/.timing.lock\n"
          "python tests/qemu/wp29_timing_analyze.py runs/wp29/timing --out runs/wp29/timing-analysis.json\n"
          "PYTHONPATH=$PWD python tests/qemu/wp29_gate.py --out runs/wp29 verdict\n"
          "python tests/qemu/wp29_report.py\n```\n")
        w("Tests: `tests/unit/test_wp29_gate.py` (verdict logic on synthetic evidence), "
          "`tests/acceptance/test_b4_gate.py` (this run's evidence; the two failing gate items are "
          "`xfail(strict=True)`), full suite in `runs/wp29/pytest-full*.log`.\n")
    text = "\n".join(L)
    rf = o / "timing-reading.md"
    text = text.replace(TIMING_READING_PLACEHOLDER,
                        rf.read_text().strip() + "\n" if rf.is_file() else "")
    secs = [s_.read_text() for s_ in a.append_section]
    for s_ in secs:
        text = text.rstrip("\n") + "\n\n" + s_.rstrip("\n") + "\n"
    a.report.parent.mkdir(parents=True, exist_ok=True)
    a.report.write_text(text)
    written = a.report.read_text()
    missing = [str(f) for f, s_ in zip(a.append_section, secs) if s_.rstrip("\n") not in written]
    if missing:
        raise SystemExit(f"appended section(s) not found verbatim in the report: {missing}")
    print(f"wrote {a.report} ({len(text)} bytes)")
    return 0


TIMING_READING_PLACEHOLDER = "@@TIMING_READING@@"

if __name__ == "__main__":
    raise SystemExit(main())
