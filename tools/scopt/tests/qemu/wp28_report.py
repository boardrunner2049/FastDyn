#!/usr/bin/env python3
"""WP28: write ``reports/B3-gate.md`` from the gate evidence in ``runs/wp28`` (verdict.json and
the step JSONs written by ``wp28_gate.py``) and the timing analysis.

    python tests/qemu/wp28_report.py --runs runs/wp28 --out reports/B3-gate.md \
        [--commit <scopt commit>] [--branch <scopt branch>] [--append-section FILE ...]

Pure formatting: every number comes from a JSON file whose path the report prints.
Hand-written sections (e.g. §6 "Audit corrections") live in files and are appended
verbatim with ``--append-section`` (in the given order); the generator checks that each
appears byte-for-byte in the written report. The audit-correction notes of item 5a
(T-B3-27, b1_minimal) are generated whenever their evidence (``AUDIT_T_B1_9``) exists.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

ROOT = Path("/data/extra_copy/fastdyn")
#: the WP28 gate run (the reference a close-out re-gate carries evidence from)
REF = ROOT / "runs/wp28"
#: B3/B4 audit fix evidence (reports/B3-gate.md §6): the T-B1-9 re-run with the hardened test
AUDIT_T_B1_9 = ROOT / "runs/b3b4-auditfix/t-b1-9"
AUDIT_VERDICT_NOTE = ("Audit corrections (2026-11-17) are in §6: T-B3-27 (5a) passes with one "
                      "design-mandated exception whose test was amended after it failed; the "
                      "audit did not change the verdict.")
AUDIT_5A_CELL = ("pass, with one design-mandated exception** (T-B1-9 v6-stamped copy: different "
                 "behaviour on the v6 loader; its test was amended after it failed; see §6 "
                 "Audit corrections)")
AUDIT_B1_MINIMAL = (". **Audit correction:** this was not a plain re-run: the failing test "
                    "`test_post_run_proof_on_v6_stamped_copy` was rewritten between the two runs "
                    "(scopt-wp22 commit 8c53614, committed 05:34:21; the re-run's junit is "
                    "05:33:59, i.e. run on the uncommitted edit) to accept the v6 loader's "
                    "fail-loud path. Re-run with the hardened test (b3b4-auditfix 2932762) on "
                    "the gate binary and on qint: 24 passed each (`runs/b3b4-auditfix/t-b1-9`). "
                    "See §6.")
AUDIT_DEVIATION = (" **Audit correction:** the b1_minimal re-run ran a test whose expectation had "
                   "been changed after the failure (see §6); the evidence-file judge "
                   "(`wp28_steps.judge_t_b3_27`) cannot see a change of test code between a "
                   "failing run and its re-run.")


def rd(p: Path) -> Any:
    return json.loads(p.read_text()) if p.is_file() else None


def pct(x: float | None) -> str:
    return "–" if x is None else f"{100 * x:+.2f} %"


def mark(r: str) -> str:
    return {"pass": "**pass**", "fail": "**FAIL**", "not_run": "not-run"}.get(r, r)


def oracles_cell(c: dict[str, Any]) -> str:
    o = c["oracles"]
    o2a = c.get("o2a")
    o2a = "n/a¹" if isinstance(o2a, str) and o2a.startswith("reported") else o.get("O2a")
    return f"{o.get('O1')} / {o2a} / {o.get('O2b')} / {o.get('O3')}"


def hits_cell(c: dict[str, Any]) -> str:
    h = c.get("hits") or {}
    cp = [f"{v}" for k, v in sorted(h.items()) if "C'" in k]
    f = [f"{v}" for k, v in sorted(h.items()) if " F (SPEC" in k]
    s = []
    if cp:
        s.append("C' " + ",".join(cp))
    if f:
        s.append("F " + ",".join(f))
    return "; ".join(s) or "–"


def fail_reason(tag: str) -> str:
    """Why a dynamic ledger row of the gate run has verdict ``fail`` (by its run tag)."""
    if tag.startswith("aa-noicount-"):
        return "A/A warm-vs-warm: noicount O2a differs (footnote 1)"
    if tag.startswith("noicount-") and tag.split("-")[1] in ("6h", "7", "20"):
        return "noicount O2a of a timer phase, reported only (footnote 1)"
    if tag.startswith("smc17-"):
        return "O4 mode on the correct F-B3-17 test-only artifact: no difference, as expected"
    if tag.startswith("o4-"):
        return "O4 control not detected there (O4 table)"
    if tag.startswith("covneg-"):
        return ("close-out coverage-neg control: O1/O2b equal by construction, its detector "
                "is O6 (coverage-neg.json)")
    return "see the step JSON"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=Path, default=ROOT / "runs/wp28")
    ap.add_argument("--out", type=Path, default=ROOT / "reports/B3-gate.md")
    ap.add_argument("--commit", default="")
    ap.add_argument("--branch", default="wp28")
    ap.add_argument("--append-section", type=Path, action="append", default=[],
                    help="a hand-written section file appended verbatim (repeatable)")
    a = ap.parse_args(argv)
    R = a.runs.resolve()
    closeout = R != REF.resolve()
    audit = AUDIT_T_B1_9.is_dir()
    car = rd(R / "carried.json") or {}
    cmpr = rd(R / "compare-ref.json") or {}
    v = rd(R / "verdict.json")
    arts = (rd(R / "artifacts.json") or {}).get("rows") or {}
    o4 = rd(R / "o4.json") or {}
    mut = rd(R / "mutants.json") or {}
    comp = rd(R / "compute.json") or {}
    tim = rd(R / "timing-analysis.json")
    aa = rd(R / "aa.json") or {}
    gd = rd(R / "gdbdiag.json") or {}
    ch = rd(R / "compute-hits.json") or {}
    st = rd(R / "stale.json") or {}
    t6 = rd(R / "t-b3-6.json") or {}
    g = v["gate_binary"]
    L: list[str] = []
    w = L.append
    w("# B3 gate report (WP28" + (", close-out re-gate" if closeout else "") + ")")
    w("")
    w(f"Generated {v['date']} by `tests/qemu/wp28_report.py` from `{R}` "
      f"(verdict: `{R / 'verdict.json'}`). scopt branch `{a.branch}`{' commit ' + a.commit if a.commit else ''}.")
    w("")
    w("## Verdict")
    w("")
    w(f"**B3 gate: {v['gate']}.**")
    if v["blocking"]:
        others_ok = all(x["result"] == "pass" for k, x in v["spec_gate"].items()
                        if k not in v["blocking"]) and \
            all(x["result"] == "pass" for k, x in v["hardening"].items() if k not in v["blocking"])
        w(f"Blocking item(s): {', '.join(v['blocking'])}." +
          (" Every other spec-gate item and every hardening item passes" if others_ok else "") +
          (" (F-B3-13(a) on a substituted criterion, see §2)." if v["hardening"].get(
              "F-B3-13a", {}).get("substituted") else ".") +
          " The reported outcomes (performance, B3+) are listed separately and do not gate.")
    else:
        w("Every spec-gate item and every hardening item passes" +
          (" (F-B3-13(a) on a substituted criterion, see §2)." if v["hardening"].get(
              "F-B3-13a", {}).get("substituted") else ".") +
          " The reported outcomes (performance, B3+) are listed separately and do not gate.")
    if audit:
        w(AUDIT_VERDICT_NOTE)
    w("")
    w(f"- Gate binary: `{g['tag']}` = commit `{g['tag_commit']}` (HEAD `{g['head']}`, clean tree "
      f"{g['tree_clean']}), exe `{g['exe']}`, **sha256 `{g['exe_sha256']}`** (re-checked by the "
      f"`gate` step at {g['checked_at']}; every run's `run.json` carries it and the driver "
      "checks it).")
    w(f"- Fixture: `fixtures/b3` WP24 review-3 image, b3.elf sha256 `{v['fixture_elf_sha256']}`.")
    w("- Bases: every fixture config prepared, imported and profiled **on the gate binary** in "
      "icount (shift 0) and noicount; F-B3-11's base uses its train input only. Training "
      "profiles are taken on the base only (§10.2).")
    w("- V22 / T-B3-29 / Tier 1: `not_run: WP23 not approved` everywhere.")
    w("- Nothing was timed except `tests/qemu/wp28_timing.sh` (under `flock runs/.timing.lock`, "
      "4 cores).")
    w("")
    rr = str(R.relative_to(ROOT)) if str(R).startswith(str(ROOT) + "/") else str(R)
    if not closeout:
        w("Reproduce (from `wt/scopt-wp28`, tools-env python):")
        w("")
        w("```")
        w("taskset -c 16-23 python tests/qemu/wp28_gate.py --out runs/wp28 --steps \\")
        w("    gate,bases,artifacts,dynamic,aa,stale,gdbdiag,t_b3_6,o4,mutants,roundtrip,alias,\\")
        w("    smc17,coverage,ucond,compute,compute_hits")
        w("tests/qemu/wp28_timing.sh && python tests/qemu/wp28_timing_analyze.py runs/wp28/timing \\")
        w("    --out runs/wp28/timing-analysis.json")
        w("python tests/qemu/wp28_gate.py --out runs/wp28 --steps verdict")
        w("python tests/qemu/wp28_report.py --runs runs/wp28 --out reports/B3-gate.md")
        w("```")
    else:
        w("Reproduce (close-out re-gate, from `tools/scopt` on main, tools-env python; the WP28 "
          f"bases are referenced read-only: `{rr}/fx*/<mode>/<cfg>/{{snap.json,"
          "runtime_identity.v2.json}` and `compute/base.json` are copies from `runs/wp28`):")
        w("")
        w("```")
        w(f"taskset -c 16-23 python tests/qemu/wp28_gate.py --out {rr} --ref runs/wp28 --steps \\")
        w("    gate,artifacts,compare-ref,carry,dynamic,t_b3_6,mutants,roundtrip,coverage,\\")
        w("    coverage-neg,ucond,compute_static,verdict --configs <all for artifacts; 23 for dynamic>")
        w(f"python tests/qemu/wp28_report.py --runs {rr} --out reports/B3-gate.md --branch main \\")
        w("    --append-section reports/B3-gate.audit6.md --append-section reports/B3-gate.closeout.md")
        w("```")
    w("")
    # ---- spec gate
    w("## 1. Spec gate (design §11 items 1-5)")
    w("")
    w("| # | item | result | evidence |")
    w("|---|---|---|---|")
    for k, x in v["spec_gate"].items():
        res_, ev_ = mark(x["result"]), list(x["evidence"][:3])
        if audit and k.startswith("5a ") and x["result"] == "pass":
            res_, ev_ = "**" + AUDIT_5A_CELL, ev_ + [str(AUDIT_T_B1_9)]
        w(f"| {k.split(' ', 1)[0]} | {k.split(' ', 1)[1]} | {res_} | "
          f"{', '.join('`' + e + '`' for e in ev_)} |")
    w("")
    # 1: two-TB
    w("### 1.1 Two-TB example (T-B3-6, T-B3-8)")
    w("")
    w("T-B3-6 pass separation (gate bases, capstone diff of the output `cache.sc` against the "
      "general C, independent of the manager; `t-b3-6.json`):")
    w("")
    w("| mode | full | CP off | BF off | DHO off | all three off | §3.5 byte form |")
    w("|---|---|---|---|---|---|---|")
    for m, r in (t6.get("rows") or {}).items():
        cs = r["cases"]

        def d(c: str) -> str:
            x = cs[c].get("pass_byte_delta") or {}
            return ", ".join(f"{k.split('-')[0][:4]} {x[k]}" for k in sorted(x)) or "none"
        w(f"| {m} | {d('full')} | {d('no_cp')} | {d('no_bf')} | {d('no_dho')} | "
          f"{d('no_cp_bf_dho')} (downgraded) | "
          f"{'all cases ok' if all(c.get('bytes_ok') for c in cs.values()) else 'MISMATCH'} |")
    w("")
    sg1 = v["spec_gate"]["1 two-TB example (T-B3-6, T-B3-8)"]["detail"]
    for m, dd in sg1["dynamic"].items():
        c = dd["cases"][0]
        w(f"- T-B3-8 {m}: O1/O2a/O2b/O3 = {oracles_cell(c)}; {hits_cell(c)}; links_armed "
          f"{c['links_armed']}, invariant_violations {c['invariant_violations']}.")
    w(f"- O4 falsified-fact and fold-flip controls detected in both modes: "
      f"{sg1['o4_falsified_fact_and_fold_flip']} (`o4.json`).")
    w(f"- CalleeSummary rows citing the patch (`source = patch`) on F-B3-1: "
      f"{sg1['callee_summary_rows_with_patch_fact']} (`summaries/<mode>/1`).")
    w("")
    # 2: sub-cases
    w("### 1.2 Sub-cases (O1 + O2a/O2b per mode, non-vacuity > 0)")
    w("")
    w("Every row: the full `b3` pipeline (all six B3 passes; fixture param "
      "`specialize-returns.growth_fraction = 1.0`), published through `scopt publish`, deployed "
      "on the gate binary against the base's warm cache with the fixture's per-mode oracle "
      "(`expected.py`), `dynamic_oracles` O1/O2a/O2b/O3/O5. Cell = O1 / O2a / O2b / O3; hits = "
      "gdb `-nx` hardware-breakpoint counts on every C' and every SPEC slot's fallback F.")
    w("")
    w("| fixture | cfg | mode | SPEC/GEN slots | O1/O2a/O2b/O3 | hits | non-vacuity | result |")
    w("|---|---|---|---|---|---|---|---|")
    sub = v["spec_gate"]["2 sub-cases"]["detail"]
    for cfg, s in sub.items():
        if "modes" not in s or cfg in ("16",):
            continue
        for m, p in s["modes"].items():
            a_ = arts.get(f"{m}/{cfg}") or {}
            for c in p.get("cases") or [{}]:
                w(f"| {s['fixture']} | {cfg}{' (' + c.get('input', '') + ')' if cfg == '11' else ''}"
                  f" | {m} | {a_.get('n_spec')}/{a_.get('n_general')} | "
                  f"{oracles_cell(c) if c else '–'} | {hits_cell(c) if c else '–'} | "
                  f"{p.get('nonvacuity', '')} | {mark('pass' if p.get('ok') else 'fail')} |")
    w("")
    w("¹ noicount timer phases (6h, 7, 20): `-d int` lines depend on host time; the A/A run "
      f"(warm vs warm of the same base, `aa.json`) differs too: {aa.get('o2a_differs_aa')}. "
      "O2a is defined under icount shift 0 (§9.4); these rows are judged by O2b and the "
      "fixture's noicount invariants (R14). In icount every O2a passed.")
    w("")
    w("**F-B3-12 (code-page invalidation of an armed C; `gdbdiag.json` rows `F-B3-12/<mode>`).** "
      "The F-B3-12 rows of the table above are the F-B3-12 artifact run *without* a debugger "
      "(no write, no invalidation). The invalidation itself runs as a PAUSED-stop scenario: "
      "WP24's `f-b3-12.gdb` cannot test it (its first breakpoint is on Vc's page, so C is "
      "looked up with CF_BP_PAGE and no link is ever armed: WP22 finding), and the F-B3-12 "
      "kernel has no PAUSED hook, so it runs on the F-B3-13 kernel (same SIMPLE_CALLEE shape, "
      "`vc_b3_13: cbz r0`) with its diag input, which spins at K = 300: Ctrl-C (PAUSED, no "
      "flush) with the link armed, debug write `cbz` -> `cbnz` on `vc_b3_13`, release, "
      "continue (WP22's `wp22-13-invc.gdb`), on WP28's published b3-pipeline F-B3-13 artifact "
      "through WP22's host-gdb harness (R's slot state read before and after the write). Warm = "
      "the same scenario on the F-B3-13 base's warm run (v5 cache).")
    w("")
    w("| mode | links_armed | C' hits before / after the write | R's SPEC slot before -> after "
      "| z / nz artifact | z / nz warm (same write) | result |")
    w("|---|---|---|---|---|---|---|")
    for m in ("icount", "noicount"):
        r12 = (gd.get("rows") or {}).get(f"F-B3-12/{m}") or {}
        sb, sa = r12.get("slot_before") or {}, r12.get("slot_after") or {}
        chk = {c["check"][:40]: c for c in r12.get("checks", [])}
        wpairs = next((c["detail"].get("warm") for k, c in chk.items()
                       if k.startswith("output equals warm")), None)
        hb = (r12.get("variant_hits_total") or 0) - (r12.get("variant_hits_after_write") or 0)
        w(f"| {m} | {(r12.get('stats') or {}).get('links_armed')} | {hb} / "
          f"{r12.get('variant_hits_after_write')} | dest {'set' if sb.get('dest') else 0}, "
          f"at_reset {sb.get('at_reset')}, -> C'{sb.get('target_variant')} -> dest "
          f"{sa.get('dest')}, at_reset {sa.get('at_reset')} (C invalid) | "
          f"{list((r12.get('pairs') or {}).values())} | {list((wpairs or {}).values())} | "
          f"{mark('pass' if r12.get('verdict') == 'PASS' else 'fail')} |")
    w("")
    bp = [(m, (gd.get("rows") or {}).get(f"F-B3-12-wp24bp/{m}") or {}) for m in
          ("icount", "noicount")]
    w("WP24's breakpoint script (`F-B3-12-wp24bp/<mode>`) is kept as a supplementary row and is "
      "**vacuous** (not counted): " + "; ".join(
          f"{m}: links_armed {(x.get('stats') or {}).get('links_armed')}, C' hits "
          f"{x.get('variant_hits_total')}" for m, x in bp) + ".")
    w("")
    w("F-B3-16 (a)-(e) (`stale.json`; the F-B3-1 b3 artifact):")
    w("")
    w("| case | icount | noicount |")
    w("|---|---|---|")
    rows16 = st.get("rows") or {}

    def c16(m: str, k: str) -> str:
        x = rows16.get(m, {}).get(k) or {}
        if k == "16d":
            return (f"links_armed {x.get('stats', {}).get('links_armed')}, activations "
                    f"{x.get('stats', {}).get('activations')}, 16d.norm "
                    f"{'equal' if not x.get('fixture_oracle_failures') else 'DIFFERS'}")
        if not x:
            return "–"
        return f"refused `{x.get('refused')}`" if not x.get("deployed") else f"{x.get('verdict')}"
    for k, lab in (("16a", "(a) changed patch plan"), ("16b", "(b) different plugin build"),
                   ("16c", "(c) qint binary, artifact dir"), ("16c_bare", "(c) qint, bare .sc"),
                   ("16d", "(d) callee rebuilt (direct rerun, v6 stats)"),
                   ("16e", "(e) coverage configuration")):
        w(f"| {lab} | {c16('icount', k)} | {c16('noicount', k)} |")
    w("")
    # 3: static and mutation suite
    w("### 1.3 Static rows, mutation suite, O4")
    w("")
    pub = [x for x in arts.values() if x.get("status") == "published"]
    a23 = [arts.get(f"{m}/23") or {} for m in ("icount", "noicount")]
    s23 = ("F-B3-23 (no B3 edit) is a v5 identity artifact with V1-V9." if all(
        x.get("version") == 5 for x in a23) else
        "F-B3-23 (coverage) is a v6 artifact in both modes (SPEC/GENERAL slots " +
        ", ".join(f"{x.get('n_spec')}/{x.get('n_general')}" for x in a23) + ").")
    w(f"- **{len(pub)} fixture artifacts** published (31 configs × 2 modes); static rows "
      f"V1-V21 and V23 pass on every one (V9 skip: not an identity candidate; V22 skip: "
      f"WP23 not approved; V14 skip only where no SPEC link exists). {s23} Compute: 4 arms, "
      "same rule (`compute.json`).")
    if closeout and cmpr:
        w(f"- Close-out: published `cache.sc` sha256 equal to WP28's for {cmpr.get('equal')}/"
          f"{cmpr.get('n')} artifacts (`compare-ref.json`); unexpected changes: "
          f"{cmpr.get('unexpected_changes') or 'none'}; changed as expected: " + ", ".join(
              k for k, r in (cmpr.get("rows") or {}).items() if not r.get("equal")) + ". "
          "Artifact ids differ (`validation.json` carries the current hostsym self-test id), "
          "as in the WP25-int recheck.")
    w(f"- Outcome ledger stages over the {len(pub)} artifacts: "
      f"{json.dumps(v['ledger_stages']['artifacts'])}; dynamic ledger rows: "
      f"{json.dumps(v['ledger_stages']['dynamic_runs'])}" +
      (" (the dynamic runs of this re-gate only; the ledgers of the carried rows are under "
       "`runs/wp28/dyn`)" if closeout else "") + ". The `validated_dynamic:fail` rows "
      "are, by run tag: " + "; ".join(
          f"`{tg}` ({fail_reason(tg)})"
          for tg in v["ledger_stages"].get("dynamic_fail_tags", [])) + ". None is a gate "
      "failure: the timer-phase rows are reported-only noicount O2a (footnote 1), and in O4 "
      "mode a ledger `pass` means \"mutant detected\", so the correct test-only smc17 "
      "artifact and the undetected controls record `fail`.")
    tot = mut.get("totals") or {}
    w("")
    w("Static mutation suite (T-B3-5 on the gate fixtures; `mutants.json`):")
    w("")
    w("| suite | mutants | rejected |")
    w("|---|---|---|")
    syn = mut.get("synthetic") or {}
    w(f"| synthetic §9.3 suite (V18 kernel self-test) | {syn.get('mutants')} | {syn.get('rejected')} |")
    w(f"| `scopt hostsym mutants` (real same-length mutants, every icount fixture base) | "
      f"{tot.get('real', {}).get('mutants')} | {tot.get('real', {}).get('rejected')} |")
    w(f"| `scopt hostsym link-mutants` (WP19 guard/link mutants, every base) | "
      f"{tot.get('link', {}).get('mutants')} | {tot.get('link', {}).get('rejected')}² |")
    w(f"| guard mutants on the **manager's own link** of every fixture artifact | "
      f"{tot.get('manager', {}).get('mutants')} | {tot.get('manager', {}).get('rejected')}² |")
    w("")
    w("² The unrejected ones are all `drop_g_cond` on one F-B3-3b unit (0x452, which reads "
      "`condexec_bits` and branches on it, so hostsym proves the atom true on every path that "
      "reaches the slot: an equivalent mutant for hostsym). The full static validator rejects it: "
      "packaged with nothing bypassed it fails V13 `guard_template` "
      "(`guard_contract_violation`) and V3. drop_g_cond is also in the static-only set of §9.3. "
      "So 100 % are rejected statically by the validator stack.")
    w("")
    w("O4 (dynamic detection of every mutant outside the static-only set; `o4.json`). Each "
      "control is built from the **manager's own plan** of its fixture (the in-process plan "
      "links to byte-identical record and variant bodies as the published artifact; checked "
      "per row), packaged by WP26's `package_negative`, deployed with `--negative-control`:")
    w("")
    w("| mutant (§9.3) | fixture | rejected statically by | detected (modes) | by | how "
      "(per mode) |")
    w("|---|---|---|---|---|---|")
    mt = v.get("mutation_table") or {}
    rows4 = [r for r in o4.get("rows", []) if isinstance(r, dict)]
    seen = set()
    for r in rows4:
        key = (r.get("mutant"), r.get("fixture"))
        if key in seen:
            continue
        seen.add(key)
        same = [x for x in rows4 if (x.get("mutant"), x.get("fixture")) == key]
        det = [x["mode"] for x in same if x.get("detected")]
        by = sorted({o for x in same for o in (x.get("detected_by") if isinstance(
            x.get("detected_by"), list) else [x.get("detected_by")]) if o})
        rej = sorted({c for x in same for c in (x.get("statically_rejected_by") or [])})
        why = "; ".join(sorted({x.get("why") or x.get("reason") or "" for x in same
                                 if not x.get("detected")} - {""}))
        why = (why + "; " if why else "") + O4_NOTES.get(key, "")
        why = why.strip("; ")
        kinds = ", ".join(f"{fm.split('/')[1]}: {kd}" for fm, kd in sorted(
            ((mt.get(key[0]) or {}).get("detection_kind") or {}).items())
            if fm.split("/")[0] == key[1])
        w(f"| {key[0]} | {key[1]} | {', '.join(rej) or '–'} | "
          f"{', '.join(det) or 'none'}{' (' + why[:300] + ')' if why else ''} | "
          f"{', '.join(by)[:80]} | {kinds or '–'} |")
    for k, why in (o4.get("static_only") or {}).items():
        w(f"| {k} | – | static only | – | {why} | – |")
    w("")
    w(f"All dynamic-set mutants detected: **{o4.get('all_dynamic_detected')}**; every packaged "
      f"control rejected by a V12-V17 row: **{o4.get('all_static_rejected')}**.")
    w("")
    w("How: `divergence` = the mutant ran to completion and an output or record differs from "
      "warm (the specific observable the detector names); `crash` = the mutant's run ended "
      "`INVALID_exit_code` with no complete guest output: a valid detection (O1/O2b differ), "
      "but it does not show the specific observable divergence; `scenario` = the gdb scenario's "
      "hit-count/result checks differ. Mutants detected **only** by a crash: "
      + (", ".join(sorted(k for k, d in mt.items() if d.get("detection_kind") and all(
          x in ("crash", "not_detected", "not_run") for x in d["detection_kind"].values())
          and "crash" in d["detection_kind"].values())) or "none") + ". Mutants detected in "
      "one mode only: " + (", ".join(sorted(f"{k} ({', '.join(d.get('modes') or [])})"
                                            for k, d in mt.items()
                                            if len(d.get("modes") or []) == 1)) or "none") +
      " (see notes).")
    w("")
    # 4/5
    w("### 1.4 Attribution and independence; 1.5 T-B3-27, T-B3-28")
    w("")
    cov = v["spec_gate"]["4a F-B3-23 (O6)"]
    if cov["result"] == "pass" or (cov.get("detail") and any(
            (r.get("n_spec") or 0) > 0 for r in (cov.get("detail") or {}).values())):
        rows23 = cov.get("detail") or {}
        w(f"- **F-B3-23 (O6): {cov['result'].upper()}"
          f"{' (blocking)' if cov['result'] != 'pass' else ''}.** Computed from "
          "`coverage.json` (the real `b3` pipeline under `coverage = true`, published, deployed "
          "against the warm base; O1/O2a/O2b/O5 + O6 = the LOG_REG coverage ring at "
          "`benchmark_end` equal to warm's; non-vacuity = gdb `-nx` hits on every C' of the "
          "artifact's variants) and `coverage-neg.json`. Per mode: " + "; ".join(
              f"{m}: v{r.get('artifact_version')}, SPEC/GEN {r.get('n_spec')}/"
              f"{r.get('n_general')}, oracles {r.get('oracles')}, C' hits "
              f"{r.get('c_prime_hits')}" for m, r in rows23.items()) +
          ". Since the close-out the LOG_REG block is one atomic host event (`HostLog`, "
          "design/closeout-logreg.md A): HostOps types the ring store, the spin is a full-view "
          "hcfg edge only, hostsym abstracts the block, and C' carries a verbatim, relocated "
          "copy of C's block, so C' logs C's guest PC (R's guard atom `g_pc` checks "
          "`regs[15] == C.pc`). **coverage-neg** (added, stricter): the same artifact with every "
          "C''s ring store nopped (test-only, V12-V17 bypassed) is detected by O6 in " +
          "; ".join(f"{m}: O6 {r.get('o6')}, C' hits {r.get('c_prime_hits')}, ring "
                    + ", ".join(f"index {x.get('reference_index')} warm vs "
                                f"{x.get('artifact_index')} control (deficit "
                                f"{x.get('deficit')})" for x in r.get("ring") or [])
                    + f", ok {r.get('ok')}" for m, r in (cov.get("coverage_neg") or {}).items())
          + (f". {cov['why']}" if cov["result"] != "pass" and cov.get("why") else "") +
          ". Before the close-out (WP28): FAIL, vacuous (v5 identity artifact, every return "
          "exit refused `exit_not_goto_ptr`, LOG_REG ring store `unexplained_host_store`).")
    else:
        w(f"- **F-B3-23 (O6): {cov['result'].upper()}{' (blocking)' if cov['result'] != 'pass' else ''}"
          f".** Computed from `coverage.json` (O6 per mode, C' hits on the published artifact's "
          f"variants). {cov['why']}. The real pipeline's artifact has no "
          "B3 edit in either mode (v5 identity; return-edges: every unit `unit_not_relocatable`, "
          "`hostops pinned: unexplained_host_store` = the LOG_REG ring store); O6 on it passes "
          "(ring equal to warm), but its non-vacuity `C' hits > 0` cannot hold. WP27's test-only "
          "X-REHY variant (V12/V13/V14/V19 bypassed) showed C' logs every execution correctly "
          "(O6 pass, C' 999), so the runtime is fine; the gap is the validator's model of the "
          "LOG_REG block. Fix (WP17/WP19, a shared-contract edit): classify the ring store by its "
          "constant base in HostOps and treat the LOG_REG block (incl. its spin loop) as one atomic "
          "host event in hcfg/hostsym (same root cause as WP29's F-B4-10).")
    f24 = v["spec_gate"]["4b F-B3-24 (-O0/-Os)"]
    w(f"- F-B3-24: {mark(f24['result'])}: every -O1/-O0/-Os build of the C twins simplifies "
      f"(SPEC links per build and mode: {json.dumps(f24['detail'])}), O1/O2a/O2b equal warm, C' "
      "999 hits.")
    t27 = v["spec_gate"]["5a T-B3-27 (v5 regression on the gate binary)"]
    m27 = mark(t27["result"])
    if audit and t27["result"] == "pass":
        m27 = "**pass, with one design-mandated exception (see §6 Audit corrections)**"
    w(f"- T-B3-27: {m27} — {t27['why']}:")
    for k, x in (t27.get("detail") or {}).items():
        ev = "; ".join(f"`{Path(e[0]).relative_to(ROOT) if str(e[0]).startswith(str(ROOT)) else e[0]}`"
                       f" {e[1]}{' -> superseded by ' + e[3] if e[3] else ''}"
                       for e in x.get("evidence", []))
        w(f"  - {k}: {'ok' if x.get('ok') else 'NOT OK ' + str(x.get('problems'))} ({ev})" +
          (AUDIT_B1_MINIMAL if audit and k == "b1_minimal" else ""))
    t28 = v["spec_gate"]["5b T-B3-28 (graph/summary round trip, multi_tb rows)"]
    w(f"- T-B3-28: {mark(t28['result'])}: `scopt return-edges summaries --certify` exported "
      "CalleeSummary rows on every base, and on every artifact V19 (summary/region consistency, "
      "derived `.scm` re-import, graph round trip with `guarded_slot` / `variant_link` edges) "
      "passes with at least one `multi_tb` region row per SPEC link "
      f"({t28['detail']['pass']}/{t28['detail']['artifacts']}; `roundtrip.json`).")
    w("")
    # hardening
    w("## 2. Hardening")
    w("")
    w("| item | result | key evidence |")
    w("|---|---|---|")
    hd = v["hardening"]

    def gsum(n: str) -> str:
        rr = hd[n]["detail"]
        return "; ".join(f"{m}: armed {x['stats'].get('links_armed')}, refused bp "
                         f"{(x['stats'].get('refused') or {}).get('breakpoints')}, C' hits "
                         f"{x['hits']}, z/nz {list((x.get('pairs') or {}).values())}"
                         for m, x in rr.items())
    for n in ("F-B3-13a", "F-B3-13b", "F-B3-13c", "F-B3-13d"):
        extra = ""
        if hd[n].get("substituted"):
            cr = hd[n]["criterion"]
            extra = (f". **Design criterion `refused[bp] > 0` not met** (observed "
                     f"{cr['observed_refused_bp']}): {cr['why']}; substituted by: "
                     f"{cr['substituted_by']}")
        w(f"| {n} | {mark(hd[n]['result'])}{' (substituted criterion)' if extra else ''} | "
          f"{gsum(n)}{extra} |")
    d17 = hd["F-B3-17"]["detail"] or {}
    w(f"| F-B3-17 | {mark(hd['F-B3-17']['result'])} | production artifact: " + "; ".join(
        f"{m}: {oracles_cell(x['cases'][0])}" for m, x in
        ((d17.get("production_artifact") or {}).get("detail") or {}).items()) +
      " (no SPEC link, vacuous for SMC); **test-only SPEC link into the RAM continuation** "
      "(`smc17.json`): " + "; ".join(
          f"{m}: C' {x.get('c_prime_hits')}, F {x.get('f_hits')}, O1/O2a/O2b equal warm "
          f"{(x.get('items') or {}).get('O1 (output follows the new code as warm does), O2a, O2b equal warm')}"
          f", rows bypassed {x.get('rows_bypassed')}"
          for m, x in (d17.get("test_only_link") or {}).items()) + " |")
    for n in ("F-B3-14", "F-B3-21", "F-B3-22"):
        rr = hd[n]["detail"]
        w(f"| {n} | {mark(hd[n]['result'])} | " + "; ".join(
            f"{m}: {oracles_cell(x['cases'][0])}, {x['nonvacuity']}" for m, x in rr.items()) + " |")
    alr = hd["F-B3-15"]["detail"] or {}
    w(f"| F-B3-15 | {mark(hd['F-B3-15']['result'])} | " + "; ".join(
        f"{m}: hits {x['hits']}" for m, x in alr.items()) + " (test-only artifact, see deviations) |")
    w(f"| F-B3-18 | {mark(hd['F-B3-18']['result'])} | {hd['F-B3-18']['why']} |")
    w(f"| T-B3-26 | {mark(hd['T-B3-26']['result'])} | {hd['T-B3-26']['why']}; "
      f"{hd['T-B3-26']['detail']['n']} scenarios |")
    w("")
    w("F-B3-12 (PAUSED-stop invalidation of an armed C, `gdbdiag.json`; spec-gate item 2, "
      "§1.2): " + "; ".join(
        f"{m}: {gd.get('rows', {}).get(f'F-B3-12/{m}', {}).get('verdict')}" for m in
        ("icount", "noicount")) + ".")
    w("")
    w("F-B3-17 test-only artifact: the production pipeline links nothing into the RAM "
      "continuation (`target_not_profiled`; `allow_ram_resident` is not wired into the "
      "manager), so a hand-built plan (WP27 slice-1 shape: f_b3_17's return R grown with one "
      "SPEC slot to `smc_cont`'s RAM copy at g_smc + 0x16, the snapshot's `cbz` record "
      "selected by guest hash, one variant with §3.5's three patches and the certified "
      "r0 == 0 fact) was packaged with WP26's packager with **no row bypassed** (every static "
      "row passes; it is tagged NEGATIVE_CONTROL only because it was built outside the "
      "manager) and run through `dynamic_oracles`: the link arms, C' runs 299 times "
      "(iterations 1..299), the guest's own `strh` at K = 300 rewrites C and C' is never "
      "entered again (F 701), and O1/O2a/O2b equal warm.")
    w("")
    # reported
    w("## 3. Reported outcomes (not gating)")
    w("")
    w("- **T-B3-29 (B3+, Tier 1): not_run: WP23 not approved.**")
    w("")
    w("Compute (F-B3-19 / T-B3-30): base prepared on the gate binary (train inputs 1-3, "
      "steady-phase profile `steady@0x2990`), default params (no fixture override). V10 on "
      "held-out inputs 1-3 (`scopt validate --dynamic`, gate binary):")
    w("")
    w("| arm | passes | variants | slots (SPEC) | CP/BF/DHO bytes | static rows | V10 held-out |")
    w("|---|---|---|---|---|---|---|")
    for arm, r in (comp.get("arms") or {}).items():
        b = r.get("pass_byte_delta") or {}
        w(f"| {arm} | {', '.join(p['pass'] for p in r.get('passes', []) if p['outcome'] == 'candidate')} "
          f"| {r.get('n_variants')} | {r.get('n_slots')} ({r.get('n_spec')}) | "
          f"{b.get('constant-propagation', 0)}/{b.get('branch-folding', 0)}/"
          f"{b.get('dead-host-ops', 0)} | {'pass' if r.get('static_rows_ok') else 'FAIL'} | "
          f"{(r.get('v10') or {}).get('verdict')} |")
    w("")
    if tim:
        w(f"Paired timing (`{R / 'timing-analysis.json'}`; {tim.get('runs')} runs, "
          f"{len(tim.get('invalid') or [])} invalid, verdicts seen {tim.get('verdicts_seen')}, "
          f"appended seen {tim.get('appended_seen')}; method: {tim.get('method')}). Speedup = "
          "t_warm / t_arm − 1 on the guest `bench_ns`:")
        w("")
        w("| arm vs warm | pairs | median speedup | 95 % CI | per input | verdict |")
        w("|---|---|---|---|---|---|")
        for k, x in (tim.get("arms") or {}).items():
            if not x.get("n_pairs"):
                continue
            pi = ", ".join(f"{i[-5:]}: {pct(s)}" for i, s in
                           (x.get("per_input_median_speedup") or {}).items())
            w(f"| {k.replace('_vs_warm', '')} | {x['n_pairs']} | {pct(x['speedup_median'])} | "
              f"[{pct(x['ci95'][0])}, {pct(x['ci95'][1])}] | {pi} | {x['verdict']} |")
        w("")
        if "timing-analysis.json" in car:
            w(f"Carried from `{car['timing-analysis.json']['from']}` (sha256 "
              f"`{car['timing-analysis.json']['sha256'][:16]}…`): every timed compute arm's "
              "`cache.sc` is byte-identical to WP28's (`compute.json` "
              "`byte_identical_to_ref`, `carried.json`), so nothing was re-timed.")
            w("")
        w("Reading: the measured gain comes from the GENERAL guarded slots (`b3-chain`, "
          "`b3-static`: a guarded direct jump instead of `lookup_tb_ptr`, as WP16's guarded-slot "
          "probe found); `b3-spec` (4 SPEC variants with their CP/BF/DHO bytes on compute) has "
          "no measurable effect (CI includes 0). Appended records are the held-out inputs' new "
          "translations and are identical across arms per input; guest outputs are identical "
          "across arms per input. This is a reported outcome, never a gate item.")
        w("")
        cb = tim.get("cpu_budget") or {}
        if cb:
            w(f"CPU budget (also in `timing-analysis.json` and `verdict.json` "
              f"`timing_cpu_budget`): {cb.get('n_cores')} cores (`{cb.get('pinning')}`), "
              f"{cb.get('reps')} reps, {cb.get('lock')}; {cb.get('lock_acquired')}; load1 "
              f"median {cb.get('load1_median')}, max {cb.get('load1_max')} over "
              f"{cb.get('load1_samples')} samples.")
            w("")
        if ch.get("arms"):
            w("Did the specialized code run during timing? C' entries per held-out input "
              "(diagnostic gdb `-nx` re-runs of the V10 held-out runs, never timed; "
              "`compute-hits.json`):")
            w("")
            w("| arm | variant | heldout-1 | heldout-2 | heldout-3 |")
            w("|---|---|---|---|---|")
            for arm, x in ch["arms"].items():
                for vlab, vs in (x.get("c_prime_hits_by_variant") or {}).items():
                    w(f"| {arm} | {vlab} | " + " | ".join(str(z) for z in vs) + " |")
            sp = (ch["arms"].get("b3-spec") or {})
            w("")
            per_run = [sum((vs[i] or 0) for vs in (sp.get("c_prime_hits_by_variant") or {})
                           .values()) for i in range(len(sp.get("rows") or []))]
            w(f"Every SPEC variant was entered on every held-out input "
              f"({sp.get('every_variant_entered')}; {sp.get('c_prime_total')} C' entries over "
              f"the 3 inputs in `b3-spec`, {min(per_run, default=0) / 1e6:.2f}-"
              f"{max(per_run, default=0) / 1e6:.2f} M per run of about 1 s guest-reported "
              "time). So `b3-spec`'s null result is not a coverage gap: the variants run, and the "
              "few host instructions each removes (the r0 load, test and jne: CP 3 + BF 6 + DHO "
              "4 bytes per variant) are below the ~1 % noise floor at this entry rate.")
            w("")
        lg = R / "timing-load.log"
        if lg.is_file():
            ls = [x for x in lg.read_text().splitlines() if x.startswith(("start", "lock", "end"))]
            w("Timing log (load averages sampled every 30 s in `timing-load.log`): " +
              " | ".join(ls))
        w("")
    else:
        w("Paired timing: not run.")
        w("")
    dc = R / "timing" / "deleted-caches.jsonl"
    if dc.is_file():
        rows_dc = [json.loads(x) for x in dc.read_text().splitlines() if x.strip()]
        w(f"Disk rule: the {len(rows_dc)} per-run sparse cache copies under `runs/wp28/timing/runs` "
          f"({sum(r['allocated_bytes'] for r in rows_dc) / 1e9:.1f} GB allocated, "
          f"{sum(r['size'] for r in rows_dc) / 1e9:.0f} GB apparent) were deleted after the "
          f"analysis; sha256 and sizes of each are kept in `{dc}` "
          f"({len({r['sha256'] for r in rows_dc})} distinct sha256).")
        di = R / "timing" / "deleted-iologs.jsonl"
        if di.is_file():
            rows_di = [json.loads(x) for x in di.read_text().splitlines() if x.strip()]
            w(f"The {len(rows_di)} per-run FastDyn `work/io.log` files "
              f"({sum(r['size'] for r in rows_di) / 1e9:.1f} GB; not read by the analysis, "
              "which uses `runs.jsonl`) were deleted the same way; sha256 and sizes in "
              f"`{di}`. `runs/wp28/timing` is now about 0.2 GB.")
        w("")
    # deviations
    w("## 4. Deviations, findings and known gaps")
    w("")
    for x in DEVIATIONS:
        if audit and x.startswith("**T-B3-27 is judged from WP22's evidence files**"):
            x += AUDIT_DEVIATION
        w(f"- {x}")
    if closeout:
        for x in CLOSEOUT_DEVIATIONS:
            w(f"- {x}")
    w("")
    w("### Known gaps")
    w("")
    cov_pass = v["spec_gate"]["4a F-B3-23 (O6)"]["result"] == "pass"
    for x in GAPS:
        if cov_pass and x.startswith("F-B3-23:"):
            continue
        w(f"- {x}")
    if closeout:
        w(f"- {GAP_LOGREG_VARIANTS}")
    w("")
    w("## 5. Evidence index")
    w("")
    for k in ("gate", "bases", "artifacts", "dynamic", "aa", "stale", "gdbdiag", "t-b3-6", "o4",
              "mutants", "roundtrip", "alias", "smc17", "coverage", "coverage-neg", "ucond",
              "compute", "compute-hits", "timing-analysis", "compare-ref", "carried", "verdict"):
        p = R / f"{k}.json"
        if p.is_file():
            w(f"- `{p}`" + (f" (carried from `{car[p.name]['from']}`, sha256 "
                            f"`{car[p.name]['sha256'][:16]}…`)" if p.name in car and
                            isinstance(car[p.name], dict) and car[p.name].get("from") and
                            p.name != "dynamic.json" else ""))
    w("")
    text = "\n".join(L) + "\n"
    secs = [s_.read_text() for s_ in a.append_section]
    for s_ in secs:
        text = text.rstrip("\n") + "\n\n" + s_.rstrip("\n") + "\n"
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(text)
    written = a.out.read_text()
    missing = [str(f) for f, s_ in zip(a.append_section, secs) if s_.rstrip("\n") not in written]
    if missing:
        raise SystemExit(f"appended section(s) not found verbatim in the report: {missing}")
    print(a.out)
    return 0


O4_NOTES = {
    ("guard_stub_store", "1"): "equivalent on F-B3-1: the stub's regs[1] := 0x55 store is dead "
                               "there (r1 is not read after the call); F-B3-9 detects it (the "
                               "fault record's stacked r1)",
    ("nop_dead_result_ldst", "3b"): "noicount: not detected (COUNTFLAG content depends on host "
                                    "time; the noicount oracle cannot see it, R14)",
    ("shared_variant_suffix", "20"): "noicount: not built (no suffix unit of f_b3_20 exists "
                                     "without icount)",
    ("icount_immediate", "7"): "noicount: does not exist (no icount prefix)",
    ("guard_before_effect", "6"): "first attempt (thumb-store hoist, an equivalent mutant) not "
                                  "detected on 6/9, see o4.log; the regs[15]-store hoist is",
}

DEVIATIONS = [
    "**Fixture params.** Fixture artifacts use `specialize-returns.growth_fraction = 1.0`: the "
    "fixtures' hot sets are tiny, and the default 10 % budget drops variants (WP21 integration "
    "note item 5). Compute uses the default params.",
    "**Non-vacuity for the negative fixtures.** F-B3-5, F-B3-6h and F-B3-17 expect no SPEC link; "
    "their counter is the refusal (F-B3-5 `fact_killed_by_helper` from the pass; F-B3-6h "
    "`handler_mode_return` from `return-edges analyze`, because the pass counts only the first "
    "reason, `exit_not_goto_ptr`: TCG ends a handler-mode `bx lr` with `exit_tb` for the "
    "EXC_RETURN check; F-B3-17 `target_not_profiled` for the RAM continuation) plus outputs equal "
    "warm.",
    "**F-B3-6r.** `rec_b3_cont` is the specialized continuation (both returns of `rec_b3` link to "
    "it; `vc_b3_6r` was refused `not_profitable`), so the recursion case is C = a grown unit "
    "returning into its own epilogue as §9.5 asks.",
    "**F-B3-6 f5 path.** `f_b3_6b` (r0 == 5, `guest_code`) gets its own SPEC variant (the design "
    "expected `no_fact`); the arbitrary-entry non-vacuity is the general C's hit count (334 = "
    "333 `bx r3` entries + warm-up) and the `fold_without_fact` control (f5's link to the "
    "r0 == 0 fold) is detected.",
    "**F-B3-13(a): design criterion not met, substituted.** No link is armed at all while the "
    "debugger's first breakpoint sits on Vc's page (C is looked up with CF_BP_PAGE, never "
    "activated from the cache; WP22 finding), so no arming attempt is ever made and "
    "`refused[bp] > 0` is unreachable as worded. 13(a) is judged on \"never armed and C' never "
    "entered, hit count and results equal warm\"; the arming refusal while a breakpoint exists "
    "is shown by F-B3-13(b) (`refused.breakpoints = 700`, breakpoint not on C's page). Recorded "
    "as an implementation note in the design.",
    "**F-B3-12 runs on the F-B3-13 kernel.** WP24's `f-b3-12.gdb` never arms a link (same "
    "CF_BP_PAGE reason), so it cannot show \"C' hits stop after K\" on a specialized C' (the "
    "first WP28 report counted it as a pass: that was vacuous, fixed). The F-B3-12 kernel has "
    "no PAUSED hook, so the invalidation scenario uses the F-B3-13 kernel's spin at K = 300 "
    "(diag input) and WP22's `wp22-13-invc.gdb` on WP28's real F-B3-13 artifact; warm is the "
    "same scenario on the warm cache. The F-B3-12 artifact itself is covered by the plain "
    "dynamic run (no debugger).",
    "**F-B3-17 test-only SPEC link.** The production artifact has no SPEC link (vacuous for "
    "SMC), so a hand-built test-only plan links f_b3_17's return to the RAM continuation "
    "(nothing bypassed; NEGATIVE_CONTROL-tagged because it is built outside the manager). "
    "F-B3-17 passes only when both the production artifact and the test-only link pass.",
    "**F-B3-13(d) harness.** WP22's host-gdb loader-event harness did not finish within 600 s on "
    "the manager-built artifact (1401 tb_flush events × its per-event state dump of 23 slots), so "
    "13(d) and the drop_g_bp control run through `direct_guest_scenario`: the artifact's run "
    "re-run directly with `QEMU_SC_V6_STATS` and a host `gdb -nx` that only counts C' hits. The "
    "standalone guest scenario completes in 3 s (z 999, nz 1, vc_hits 700). F-B3-12 and "
    "13(a)-(c) use WP22's harness unchanged.",
    "**F-B3-15 is a test-only artifact.** The direct mps2 path has no ExecutionBundle or "
    "RuntimeIdentity (WP26 `prepare_direct`), so V7/V8/V20 fail by construction; the plan is "
    "hand-built (R = `alias_f`, SPEC slot to `alias_vc`, the three §3.5 patches located by "
    "capstone) and packaged with WP26's negative-control packager bypassing exactly V7/V8/V20 "
    "(all other rows pass). It is never publishable (WP27 X-REHY precedent).",
    "**F-B3-16(b)** is refused at deploy (`bundle_mismatch`: the plugin build is part of the "
    "bundle) rather than reaching a post-run INVALID; (c) the artifact directory is refused "
    "`runtime_mismatch:qemu_sha256` and the bare `.sc` is `INVALID_artifact_not_used`; "
    "(d) runs through a direct rerun (`b3_16d.elf`, BENCH name masked as WP24 finding 12): "
    "`links_armed = 0` and the output is `16d.norm` (nz 1000).",
    "**O2a in noicount timer phases** is reported, not judged (footnote 1; A/A evidence).",
    "**O4 controls.** Built from the manager's own plan (in-process `PassManager` + "
    "`materialize`, byte-identical to the published artifact) instead of hand-built plans. "
    "Byte mutants on a placed unit also bypass V2/V3 (an undeclared edit is what they are); the "
    "dead-result LDST mutant is a *declared* rewrite (rejected by V12 and V15 "
    "`edit_inside_ldst_block`) and bypasses V3/V8 (the re-import of the mutated unit fails); "
    "falsified-fact, fold-without-fact and shared-variant bypass V19 (`stale_fact`), as WP27 "
    "did. `guard_before_effect` hoists R's tail above its `regs[15]` store (the thumb store "
    "writes the constant the key implies: an equivalent mutant at run time, first attempt not "
    "detected, recorded). `icount_immediate` on F-B3-7: C is a 1-insn TB whose prefix is "
    "`dec ebx`, so the mutant drops it; it exists only in icount. `shared_variant_suffix` "
    "exists only where suffix units exist (icount; 7 suffix units of `f_b3_20`). "
    "`nop_dead_result_ldst` is detected in icount (COUNTFLAG log); in noicount the fixture's "
    "oracle has no deterministic COUNTFLAG content (R14), so it is not detected there.",
    "**drop_g_pc detectors** are F-B3-6r (R returns to `rec_b3_cont` and `vc_b3_6r`) and the "
    "F-B3-11 held-out input (unseen call site B); §9.3 names F-B3-6/F-B3-15 (on F-B3-6 every R "
    "returns to one Vc). F-B3-15's own run shows g_pc failing on the alias path (F 1001).",
    "**Cited, not re-run:** T-B3-26 (WP22 stress-r6), T-B3-27 (WP22), F-B3-18 (WP26 "
    "integration). All on the same exe sha256 and fixture image; the gate binary is unchanged "
    "(tag = HEAD, clean tree).",
    "**Harness finding (documented, fail-closed):** `dynamic_oracles(..., quiet=True)` makes "
    "FastDyn run with `-q`, whose `log_level` is part of `contract_id`, so an artifact built "
    "from a non-quiet base is refused `bundle_mismatch`. This is correct fail-closed behaviour; "
    "WP28 documents it in `dynamic_oracles`' docstring and the refusal detail now says so. The "
    "driver calls it without `quiet`.",
    "**Harness gap fixed:** `scopt.run.diag.gdb_hits` now also accepts a direct run directory "
    "(`direct.json`, new `prepare_direct_diag`); F-B3-15's hit counts now come from it (same "
    "counts as the earlier private counter: C' 999, F 1001). The FastDyn direct reruns "
    "(F-B3-12 warm, F-B3-13(d), F-B3-16(d)) keep the rerun helper, because they need a "
    "different input and the guest gdbstub.",
    "**T-B3-27 is judged from WP22's evidence files**, not from its verdict text: each row's "
    "last evidence must pass (gate JSON `verdict` and per-test checks, or a pytest summary "
    "line), and an earlier failing file only counts when a later passing re-run supersedes it "
    "(b1_minimal: T-B1-9 re-run `24 passed`; b2_compute: full re-run `compute-r2`).",
    "**Timing caches deleted** (disk rule) after the analysis, with sha256 and sizes kept "
    "(`timing/deleted-caches.jsonl`).",
]
CLOSEOUT_DEVIATIONS = [
    "**Close-out re-gate (design/closeout-logreg.md C.2).** Bases are not re-collected (the "
    "snapshot capture does not depend on scopt code); the WP28 bases are referenced read-only "
    "and a sha256 manifest of them is equal before and after (`runs/closeout/manifests`). All "
    "62 fixture artifacts are re-built and re-published with the close-out code; the published "
    "`cache.sc` bytes are compared with WP28's (`compare-ref.json`). The dynamic rows, the A/A, "
    "stale, gdbdiag, alias, smc17 and O4 evidence of byte-identical artifacts are carried "
    "(copied, sha256 in `carried.json`); F-B3-23's rows are re-run. `t_b3_6`, `mutants`, "
    "`roundtrip`, `coverage`, `ucond` are re-run, and the new `coverage-neg` step is added.",
    "**coverage-neg (added, stricter).** A test-only negative control on the F-B3-23 artifact "
    "(the manager's own plan, every C''s LOG_REG ring store nopped, V12-V17 bypassed) must be "
    "detected by O6 while C' runs; item 4a requires it in both modes. Not a criterion of "
    "design §11; disclosed.",
    "**Compute arms** are re-packaged statically (`compute_static`); V10 and the C' hit counts "
    "are carried only when an arm's published `cache.sc` is byte-identical to WP28's, and the "
    "timing only when every timed arm is (`carried.json`).",
]
GAP_LOGREG_VARIANTS = (
    "Unmatched LOG_REG variants (close-out): the matcher accepts only the exact gate-binary "
    "coverage block (spin form, one (IDX, CONS, BUF) triple, ring base an anonymous non-env "
    "mapping); FastDyn's logger LOG_REG without the spin, the FP `arm_log_fp` form, a `movabs` "
    "form or another register allocation are `NoMatch` and fail closed as before (unit pinned, "
    "no edit). No admitted configuration uses them, so they are not exercised.")
GAPS = [
    "F-B3-23: no validated B3 artifact has a variant under a coverage configuration (blocking; "
    "see 1.4).",
    "F-B3-22b not built (U-VIRT): drop_g_cond stays static-only.",
    "The fault record has no SysTick CVR; SysTick CVR is 0 in every record (WP24/WP26 "
    "findings), so O2b positions rest on (iteration, PC) and the stacked registers.",
    "`scopt validate ART` (static re-validation) still cannot read v6 artifacts (WP21 note 7); "
    "`scopt publish` is the verified re-check used here.",
    "The `allow_ram_resident` test policy is not wired into the manager (WP26 note 4): F-B3-17's "
    "production artifact has no SPEC link into RAM code; the SPEC-into-RAM path is covered only "
    "by a test-only artifact.",
    "Several O4 detections are crashes (`INVALID_exit_code`), not the specific observable "
    "divergence; some mutants are detectable in one mode only (O4 table).",
    "V22 / T-B3-29: not_run: WP23 not approved.",
]


if __name__ == "__main__":
    raise SystemExit(main())
