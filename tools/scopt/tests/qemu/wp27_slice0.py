#!/usr/bin/env python3
"""WP27 unit stage: slices 0a and 0b of the first vertical slice (plan ``## WP27``,
design §12 "First vertical slice", §3.5, §9.4) on the **current** binary (``qint``,
``wt/qemu-int``, sha256 ``8864c726…``). No QEMU change, nothing timed, no timing lock.

    wp27_slice0.py --out runs/wp27 [--steps bases,summary,static,slice0b]
                   [--modes icount,noicount]

Run from the scopt worktree with the tools-env python. Every QEMU run goes through the
scopt/FastDyn launcher (``setarch x86_64 -R``, timeout, fresh run directory,
``supercache_flat = false``); gdb is always ``gdb -nx -batch`` with cwd = FastDyn's cwd.

Steps:

* ``bases`` (slice 0a step 1): WP24 phase F-B3-1 (``fixtures/b3/b3-1.toml.in``, modifier
  ``r0 <- 0`` at ``f_b3_1``'s ``bx lr``) rendered for ``qint``, Tier-0 capture
  (``scopt prepare``), import, steady Profile v1 (``steady@b3_bench_start``, untimed),
  with WP20's driver; then ``scopt admission identity`` (RuntimeIdentity v2 of ``qint``,
  EnvLayout ``sha256:05db4e81…``).
* ``summary`` (slice 0a step 2): ``scopt return-edges summaries --certify``: the
  ``exit-facts`` proposer's ``r0 == 0`` with ``source = patch`` (plan entry
  ``0x10002 r0 <- 0``) certified by WP19's V14 certifier (hostsym); the CalleeSummary row of
  R is exported.
* ``static`` (slice 0a step 3): ``scopt optimize --pipeline b3 --b3`` for the full pipeline
  and CP/BF/DHO disabled in turn and all three disabled. A v6 artifact needs a v6-capable
  runtime (V7 ``v6_capable_runtime``) and ``qint`` is not one, so the cases run with the
  **test-only** override ``--v6-exe <qint sha>`` into an evidence store under ``OUT`` (never
  deployed); the full case is also run without the override and must be rejected by V7
  **alone** (every other static row passing). Checked: published, every static row
  (V1-V8, V11-V21, V23) pass (V9/V22 skip; V14 skip when downgraded), the variant bytes
  diffed against the general C **from the output cache.sc** (capstone; relocations
  separated) equal §3.5's expected sites per ablation, each of CP/BF/DHO with its own ledger
  row whose ``bytes_changed`` equals the manager's per-pass delta, distinct ablation
  variants, all three off downgraded ``no_simplification`` with the slot now GENERAL, and
  the multi_tb FactRef carrying the CalleeSummary row hash.
* ``slice0b`` (slice 0b, icount only): ``experiments.wp27_inplace.build_inplace_copy``
  writes the v5 copy ``experimental_unsound_in_place_specialization`` (R's GENERAL guarded
  slot, registered, + the slice-0a C' patches written into the general C); never published.
  Runs on ``qint``: the base warm cache (reference) and the copy through
  ``scopt.run.oracles.dynamic_oracles`` (O1 with the fixture's icount oracle, O2a ``-d int``,
  O2b, O5); gdb ``-nx`` hardware-breakpoint hit counts on C's ``tc.ptr``, R's stub ``S``,
  its fallback ``F`` and the slot's reset path ``exit_tb(R|n)`` (copy and warm); the m-CE7
  diag twin (``scopt.run.diag.diag_twin``) of both runs; and the WP27 ordering twin
  (:func:`experiments.wp27_inplace.window_twin_script`), which shows at which hit counts of
  C and R's stub each TLB flush after ``benchmark_start`` happened.

Writes ``OUT/wp27-slice0.json`` and ``OUT/verdict.json``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
WT = HERE.parents[1]
sys.path.insert(0, str(WT))
sys.path.insert(0, str(HERE))
ROOT = Path("/data/extra_copy/fastdyn")
FIX = ROOT / "fixtures/b3"
QINT = ROOT / "wt/qemu-int/build/qemu-system-arm"
QINT_SHA = "8864c72671971cf4488b5afa76eeee02a3e2a472c45af81ec1314464e6035f41"
PY = sys.executable
CPUS = os.environ.get("WP27_CPUS", "16-23")
os.environ.setdefault("WP21_CPUS", CPUS)

import wp21_integ as W  # noqa: E402  (variant_diff, check_case, _validation, CASES, EXPECT)

N_ITER = 1000  # F-B3-1 KERNEL b3_1, f_b3_1, 1000
SELF_PASSES = ("constant-propagation", "branch-folding", "dead-host-ops")


def log(*a: Any) -> None:
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def sha256(p: Path) -> str:
    return W.sha256(p)


def cli(args: list[str], logf: Path, *, timeout: float = 1800) -> tuple[int, str]:
    return W.cli(args, logf, timeout=timeout)


def fixture_addr(sym: str) -> int:
    sys.path.insert(0, str(FIX))
    import b3conf  # noqa: E402

    return b3conf.resolve(str(FIX / "b3.elf"), sym)


# ---------------------------------------------------------------------------
# bases (slice 0a step 1)
# ---------------------------------------------------------------------------

def step_bases(out: Path, modes: list[str]) -> dict[str, Any]:
    res = W.step_bases(out, modes, QINT, QINT_SHA)
    for m in modes:
        b = W.base_info(out, m)
        ri = json.loads(b["ri"].read_text())
        res[m]["admission"] = ri.get("admission")
        res[m]["records"] = res[m].get("records")
    return res


# ---------------------------------------------------------------------------
# CalleeSummary + V14 (slice 0a step 2)
# ---------------------------------------------------------------------------

def step_summary(out: Path, modes: list[str]) -> dict[str, Any]:
    res: dict[str, Any] = {}
    rva = fixture_addr("f_b3_1_ret")
    for m in modes:
        b = W.base_info(out, m)
        od = out / "summary" / m
        if od.exists():
            shutil.rmtree(od)
        rc, so = cli(["return-edges", "summaries", str(b["snapshot"]), "--profile",
                      str(b["profile"]), "--prep", str(b["prep"]), "--elf", str(b["elf"]),
                      "--out-dir", str(od), "--certify"], out / "logs" / f"summary-{m}.log")
        rows = [json.loads(x) for x in
                (od / "summaries/callee_summaries.jsonl").read_text().splitlines() if x.strip()] \
            if (od / "summaries/callee_summaries.jsonl").is_file() else []
        rr = [r for r in rows if r.get("return_insn_vaddr") == hex(rva)]
        facts = [f for r in rr for f in r.get("facts", [])]
        r0 = [f for f in facts if f.get("field") == "r0"]
        ok_fact = bool(r0) and all(
            f.get("value") == "0x0" and f.get("source") == "patch" and
            f.get("status") == "certified" and f.get("cert_hash") and
            (f.get("plan_entry") or {}).get("vaddr") == hex(rva) for f in r0)
        patches_listed = all(any(p.get("vaddr") == hex(rva) and p.get("defines_fact")
                                 for p in r.get("patches", [])) for r in rr)
        try:
            summ = json.loads(so)
        except ValueError:
            summ = None
        res[m] = {"exit": rc, "rows": len(rows), "r_rows": len(rr),
                  "r_units": [r.get("return_unit") for r in rr],
                  "row_hashes": [r.get("row_hash") for r in rr],
                  "callee": [r.get("callee") for r in rr], "r0_facts": r0,
                  "patches": [r.get("patches") for r in rr],
                  "file": str(od / "summaries/callee_summaries.jsonl"),
                  "file_sha256": sha256(od / "summaries/callee_summaries.jsonl") if rows else None,
                  "cli_summary": summ,
                  "ok": rc == 0 and len(rr) >= 1 and ok_fact and patches_listed}
        log("summary", m, res[m]["ok"], res[m]["r_units"], [(f["field"], f["value"], f["source"],
                                                             f["status"]) for f in r0])
    return res


# ---------------------------------------------------------------------------
# static (slice 0a step 3, T-B3-6 static part on the current binary)
# ---------------------------------------------------------------------------

def _ledger_pass_rows(adir: Path) -> dict[str, Any]:
    rows = {}
    for line in (adir / "ledger.jsonl").read_text().splitlines():
        r = json.loads(line)
        if r.get("subject") == "pass":
            rows[r["id"].split("@")[0]] = {"verdict": r.get("verdict"), "reason": r.get("reason"),
                                           "bytes_changed": (r.get("counts") or {}).get(
                                               "bytes_changed")}
    return rows


def _multi_tb_facts(adir: Path) -> list[dict[str, Any]]:
    facts = []
    rp = adir / "regions.jsonl"
    for line in (rp.read_text().splitlines() if rp.is_file() else []):
        row = json.loads(line)
        if row.get("kind") != "multi_tb":
            continue
        for e in row.get("entries", []):
            for fr in (e.get("precondition") or {}).get("facts", []):
                facts.append(fr)
    return facts


def _optimize(out: Path, b: dict[str, Any], m: str, case: str, disable: tuple[str, ...],
              store: Path, override: bool) -> dict[str, Any]:
    args = ["optimize", str(b["snapshot"]), "--pipeline", "b3", "--b3", "--store", str(store),
            "--profile", str(b["profile"]), "--runtime-identity", str(b["ri"]), "--prep",
            str(b["prep"]), "--elf", str(b["elf"]), "--selftest",
            str(out / "hostsym_selftest.json"), "--json"]
    if override:
        args += ["--v6-exe", QINT_SHA]
    for p in disable:
        args += ["--disable", p]
    t0 = time.time()
    rc, so = cli(args, out / "logs" / f"optimize-{m}-{case}.log")
    try:
        doc = json.loads(so)
    except ValueError:
        return {"exit": rc, "error": "no JSON on stdout", "ok": False}
    return {"exit": rc, "seconds": round(time.time() - t0, 1), "status": doc.get("status"),
            "reason": doc.get("reason"), "artifact_dir": doc.get("artifact_dir"),
            "artifact_id": doc.get("artifact_id"), "rejection_path": doc.get("rejection_path"),
            "pass_byte_delta": doc.get("pass_byte_delta"), "downgraded": doc.get("downgraded"),
            "validation_codes": doc.get("validation"),
            "passes": [{"pass": p.get("pass"), "outcome": p.get("outcome"),
                        "reason": p.get("reason")} for p in doc.get("passes", [])]}


def step_static(out: Path, modes: list[str], summary: dict[str, Any]) -> dict[str, Any]:
    from scopt.formats.envlayout_v1 import find_envlayout
    from scopt.importer.import_cache import open_snapshot

    res: dict[str, Any] = {}
    for m in modes:
        b = W.base_info(out, m)
        ri = json.loads(b["ri"].read_text())
        lay = find_envlayout(ri["qemu_sha256"])
        r0_off = lay.offset("regs[0]")
        in_vaddrs = [u.vaddrs[0] if u.vaddrs else None
                     for u in open_snapshot(b["snapshot"]).units()]
        store = out / "static" / "store-v7override" / m
        rm: dict[str, Any] = {"envlayout_id": lay.envlayout_id}
        # V7 without the override: rejected by V7 alone
        nov = _optimize(out, b, m, "full-no-override", (), out / "static" / "store-strict" / m,
                        override=False)
        rows = []
        if nov.get("rejection_path"):
            rej = json.loads(Path(nov["rejection_path"]).read_text())
            rows = [{"code": r.get("code"), "check": r.get("check"), "result": r.get("result"),
                     "reason": r.get("reason")} for r in (rej.get("validation") or {}).get("rows", [])
                    if r.get("result") == "fail"]
        nov["fail_rows"] = rows
        nov["ok"] = (nov.get("status") == "rejected" and nov.get("reason") ==
                     "V7:v6_runtime_required" and
                     [(r["code"], r["check"]) for r in rows] == [("V7", "v6_capable_runtime")])
        rm["full_no_override"] = nov
        log(m, "full without V7 override:", nov.get("status"), nov.get("reason"), rows)
        for case, dis in W.CASES.items():
            r = _optimize(out, b, m, case, dis, store, override=True)
            if r.get("artifact_dir"):
                adir = Path(r["artifact_dir"])
                r["cache_sc_sha256"] = sha256(adir / "cache.sc")
                r["validation"] = W._validation(adir)
                vd = W.variant_diff(adir / "cache.sc", r0_off, in_vaddrs)
                r["sc"] = vd
                r["bytes_ok"], r["bytes_why"] = W.check_case(case, vd)
                codes = r["validation"]["codes"]
                want_skip = {"V9", "V22"} | ({"V14"} if case == "no_cp_bf_dho" else set())
                r["static_rows_ok"] = (r["validation"]["verdict"] == "accepted" and
                                       all(codes.get(c) == "pass" for c in W.STATIC_ROWS
                                           if c not in want_skip) and
                                       all(codes.get(c) in ("pass", "skip") for c in want_skip))
                r["spec_slots"] = sum(1 for s in vd["slots"] if s["kind"] == "SPEC")
                r["general_slots"] = sum(1 for s in vd["slots"] if s["kind"] == "GENERAL")
                led = _ledger_pass_rows(adir)
                r["ledger"] = led
                delta = r.get("pass_byte_delta") or {}
                lok = []
                for p in SELF_PASSES:
                    row = led.get(p) or {}
                    if p in dis:
                        lok.append(row.get("verdict") == "skipped_unsupported" and
                                   row.get("reason") == "ablation_disabled")
                    elif case == "no_cp_bf_dho":
                        lok.append(False)
                    elif p in delta:
                        # the pass changed bytes: its own candidate row, same delta
                        lok.append(row.get("verdict") == "candidate" and
                                   bool(row.get("bytes_changed")) and
                                   row.get("bytes_changed") == delta.get(p))
                    else:
                        # enabled but nothing to do (§3.5: with BF off the test stays,
                        # DHO reports flags_live): a skip row with its reason, no bytes
                        lok.append(str(row.get("verdict", "")).startswith("skipped_") and
                                   bool(row.get("reason")) and not row.get("bytes_changed") and
                                   (case != "no_bf" or p != "dead-host-ops" or
                                    row.get("reason") == "flags_live"))
                r["ledger_ok"] = all(lok) if case != "no_cp_bf_dho" else \
                    all(led.get(p, {}).get("verdict") == "skipped_unsupported" for p in SELF_PASSES)
                fr = _multi_tb_facts(adir)
                r["multi_tb_facts"] = fr
                hashes = set(summary.get(m, {}).get("row_hashes") or [])
                r["factref_summary_row_ok"] = (case == "no_cp_bf_dho" and not fr) or (
                    bool(fr) and all(f.get("summary_row_hash") in hashes and
                                     f.get("field") == "r0" and f.get("source") == "patch"
                                     for f in fr))
            rm[case] = r
            log(m, case, r.get("status"), r.get("reason"), r.get("pass_byte_delta"),
                "bytes_ok", r.get("bytes_ok"), r.get("bytes_why"), "rows_ok",
                r.get("static_rows_ok"), "ledger_ok", r.get("ledger_ok"),
                "factref", r.get("factref_summary_row_ok"))
        bodies = {k: json.dumps([v.get("patches") for v in
                                 ((rm.get(k) or {}).get("sc") or {}).get("variants") or []],
                                sort_keys=True) for k in ("no_cp", "no_bf", "no_dho")}
        deltas = {k: (rm.get(k) or {}).get("pass_byte_delta") or {} for k in W.CASES}
        all_off = rm.get("no_cp_bf_dho") or {}
        full_spec = [(s["src_vaddr"], s["slot_n"], s["target_vaddr"])
                     for s in ((rm.get("full") or {}).get("sc") or {}).get("slots", [])
                     if s["kind"] == "SPEC"]
        off_general = [(s["src_vaddr"], s["slot_n"], s["target_vaddr"])
                       for s in (all_off.get("sc") or {}).get("slots", []) if s["kind"] == "GENERAL"]
        sep = {"distinct_variant_bytes": len(set(bodies.values())) == 3,
               "nonzero_deltas": {k: bool(deltas[k]) and all(x > 0 for x in deltas[k].values())
                                  for k in ("full", "no_cp", "no_bf", "no_dho")},
               "deltas": deltas,
               "downgrade_all_off": [d.get("reason") for d in (all_off.get("downgraded") or [])],
               "full_spec_slots": full_spec,
               "all_off_spec_slot_now_general": bool(full_spec) and
               all(t in off_general for t in full_spec)}
        rm["separation"] = sep
        rm["ok"] = (nov["ok"] and
                    all((rm.get(k) or {}).get("status") == "published" for k in W.CASES) and
                    all(rm[k].get("bytes_ok") and rm[k].get("static_rows_ok") and
                        rm[k].get("ledger_ok") and rm[k].get("factref_summary_row_ok")
                        for k in W.CASES) and
                    sep["distinct_variant_bytes"] and all(sep["nonzero_deltas"].values()) and
                    sep["downgrade_all_off"] == ["no_simplification"] and
                    sep["all_off_spec_slot_now_general"])
        log(m, "slice 0a static", rm["ok"], json.dumps(sep, default=str)[:300])
        res[m] = rm
    return res


# ---------------------------------------------------------------------------
# slice 0b (dynamic, current binary, experimental, never published)
# ---------------------------------------------------------------------------

def _gdb_window_twin(run_dir: str, outd: Path, probes: list[int], timeout_s: float
                     ) -> dict[str, Any]:
    from experiments.wp27_inplace import parse_window_twin, window_twin_script
    from scopt.run.diag import _drop_big, _gdb_cmd, prepare_diag

    prep = prepare_diag(run_dir, outd, log_name="qemu-twin.log")
    o = prep["out"]
    gcmd = _gdb_cmd(timeout_s, window_twin_script(probes), prep["argv"], script=o / "wtwin.gdb")
    t0 = time.monotonic()
    p = subprocess.run(gcmd, cwd=str(prep["cwd"]), env=prep["env"], capture_output=True,
                       text=True, stdin=subprocess.DEVNULL, timeout=timeout_s + 60)
    (o / "gdb.log").write_text(p.stdout + "\n--- stderr ---\n" + p.stderr)
    doc = parse_window_twin(p.stdout, probes)
    m = re.search(r"exited (?:normally|with code (\d+))", p.stdout)
    doc.update(diag_dir=str(o), gdb_exit=p.returncode, wall_s=time.monotonic() - t0,
               qemu_exit=0 if m and m.group(1) is None else (int(m.group(1), 8) if m else None))
    (o / "wtwin.json").write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    _drop_big(o)
    return doc


def guest_results(guest_norm: list[str]) -> dict[int, int]:
    """``b3_res(idx, value)`` pairs (tag ``0x0052000k``) from a run's ``guest_norm``."""
    regs = [x for x in guest_norm if x.startswith("Reg ")]
    out: dict[int, int] = {}
    for a, b in zip(regs, regs[1:]):
        if a.startswith("Reg 0:") and b.startswith("Reg 1:"):
            tag = int(a.split(":")[1], 16)
            if tag >> 16 == 0x52:
                out[tag & 0xFFFF] = int(b.split(":")[1], 16)
    return out


def step_slice0b(out: Path, static: dict[str, Any], timeout: float) -> dict[str, Any]:
    from experiments.wp27_inplace import LABEL, build_inplace_copy, patches_from_variant
    from scopt.formats.envlayout_v1 import find_envlayout
    from scopt.importer.import_cache import open_snapshot
    from scopt.run.diag import diag_twin, gdb_hits
    from scopt.run.oracles import dynamic_oracles

    m = "icount"
    b = W.base_info(out, m)
    res: dict[str, Any] = {"mode": m, "label": LABEL}
    full = (static.get(m) or {}).get("full") or {}
    vs = ((full.get("sc") or {}).get("variants") or [])
    if not (full.get("bytes_ok") and len(vs) == 1):
        res["ok"] = False
        res["why"] = "slice 0a full artifact missing or its bytes did not check"
        return res
    spec = [s for s in full["sc"]["slots"] if s["kind"] == "SPEC"]
    snap = open_snapshot(b["snapshot"])
    ri = json.loads(b["ri"].read_text())
    lay = find_envlayout(ri["qemu_sha256"])
    rva, vc = fixture_addr("f_b3_1_ret"), fixture_addr("vc_b3_1")
    sd = out / "slice0b"
    if sd.exists():
        shutil.rmtree(sd)
    copy = sd / "copy"
    patches = patches_from_variant(vs[0])
    desc = build_inplace_copy(snap, b["snapshot"], r_vaddr=rva, vc=vc, patches=patches,
                              out_dir=copy, layout=lay,
                              slot_n=spec[0]["slot_n"] if spec else None,
                              provenance={"slice0a_artifact": full.get("artifact_id"),
                                          "slice0a_cache_sc_sha256": full.get("cache_sc_sha256"),
                                          "variant_patches_from": "capstone diff of the slice-0a "
                                          "cache.sc variant against its general C",
                                          "slice0a_spec_slot": spec})
    res["copy"] = {"dir": str(copy), "cache_sc_sha256": desc["cache_sc_sha256"],
                   "checks": desc["checks"], "R": desc["R"], "C": desc["C"],
                   "patches": desc["patches"], "probes": desc["probes"]}
    res["copy"]["spec_slot_matches_0a"] = bool(spec) and spec[0]["src_vaddr"] == desc["R"][
        "vaddrs"][0] and int(spec[0]["target_vaddr"], 16) == vc
    log("slice0b copy", desc["checks"]["ok"], desc["cache_sc_sha256"])
    if not desc["checks"]["ok"]:
        res["ok"] = False
        return res
    # -- O1 / O2a / O2b / O5: copy vs the warm base on qint ------------------------
    dd = sd / "dyn"
    dyn = dynamic_oracles(copy, [b["toml"]], dd, oracles=("O1", "O2a", "O2b", "O5"),
                          baseline=b["snapshot"], qemu=QINT, timeout_s=timeout,
                          inputs=[b["input"]], fixture_expected=FIX / "expected.py",
                          fixture_inputs=["1"], v6_stats=False)
    case = (dyn.get("cases") or [{}])[0]
    runs = case.get("runs") or {}
    got = {o["oracle"]: o["verdict"] for o in case.get("oracles", [])}
    res["dynamic"] = {"verdict": dyn.get("verdict"), "oracles": got,
                      "details": {o["oracle"]: o.get("detail") for o in case.get("oracles", [])},
                      "runs": runs, "failures": dyn.get("failures"),
                      "incomplete": dyn.get("incomplete"), "file": str(dd / "dynamic_oracles.json")}
    art_run = (runs.get("artifact") or {}).get("run_dir")
    ref_run = (runs.get("reference") or {}).get("run_dir")
    for side, rd in (("artifact", art_run), ("reference", ref_run)):
        if rd and (Path(rd) / "run.json").is_file():
            rj = json.loads((Path(rd) / "run.json").read_text())
            res["dynamic"].setdefault("run_json", {})[side] = {
                k: rj.get(k) for k in ("verdict", "exit_code", "appended", "qemu_sha256",
                                       "output_norm_sha256")}
            res["dynamic"]["run_json"][side]["results"] = guest_results(rj.get("guest_norm") or [])
            res["dynamic"]["run_json"][side]["guest_norm_lines"] = len(rj.get("guest_norm") or [])
    log("slice0b dynamic", dyn.get("verdict"), got)
    probes = [(p["what"], int(p["addr"], 16)) for p in desc["probes"]]
    # -- O3-style hit counts (gdb -nx, hardware breakpoints) ------------------------
    hits: dict[str, Any] = {}
    if art_run and ref_run:
        h = gdb_hits(art_run, sd / "hits-copy", probes, timeout_s=timeout)
        hits["copy"] = {r["what"]: r["hits"] for r in h["targets"]}
        hits["copy_ok"] = h["ok"]
        # warm: only C (R is not grown there; same C offset)
        c_addr = next(a for w, a in probes if w.startswith("C tc.ptr"))
        hw = gdb_hits(ref_run, sd / "hits-warm", [("C tc.ptr (warm, unpatched)", c_addr)],
                      timeout_s=timeout)
        hits["warm"] = {r["what"]: r["hits"] for r in hw["targets"]}
        hits["warm_ok"] = hw["ok"]
    res["hits"] = hits
    log("slice0b hits", hits)
    # -- m-CE7 diag twins (strict) and the WP27 ordering twin ----------------------
    tw: dict[str, Any] = {}
    if art_run and ref_run:
        for side, rd in (("copy", art_run), ("warm", ref_run)):
            t = diag_twin(rd, sd / f"twin-{side}", timeout_s=timeout)
            tw[side] = {k: t.get(k) for k in ("clean", "within_allowance", "qemu_exit",
                                              "benchmark_start_seen", "benchmark_end_seen",
                                              "guest_norm_equal_source", "e_bp")}
            tw[side]["counters_nonzero"] = {k: v for k, v in t["e_map"]["counters"].items() if v}
            tw[side]["total_after_benchmark_start"] = t["e_map"]["total_after_benchmark_start"]
            tw[side]["unresolved"] = t["e_map"]["unresolved"]
        o = _gdb_window_twin(art_run, sd / "wtwin-copy",
                             [a for w, a in probes if not w.startswith("R slot reset")],
                             timeout)
        tw["ordering_copy"] = o
    res["twins"] = tw
    log("slice0b twins", json.dumps({k: (v.get("clean"), v.get("total_after_benchmark_start"))
                                     for k, v in tw.items() if k in ("copy", "warm")}),
        "ordering", (tw.get("ordering_copy") or {}).get("flushes_before_last_probe_hit"),
        (tw.get("ordering_copy") or {}).get("final_probe_hits"))
    # -- verdict items ------------------------------------------------------------
    hc = hits.get("copy") or {}

    def h(prefix: str) -> Any:
        return next((v for k, v in hc.items() if k.startswith(prefix)), None)
    c_hits, s_hits, f_hits, rst = h("C tc.ptr"), h("R stub S"), h("R stub F"), h("R slot reset")
    oc = tw.get("ordering_copy") or {}
    twc = tw.get("copy") or {}
    items = {
        "o1_equal_warm": got.get("O1") == "pass",
        "o2a_equal_warm": got.get("O2a") == "pass",
        "o2b_equal_warm": got.get("O2b") == "pass",
        # F-B3-1: z = N, nz = 0 in both runs (also inside O1's fixture oracle)
        "z_n_nz_0": all((res["dynamic"].get("run_json") or {}).get(s, {}).get("results") ==
                        {0: N_ITER, 1: 0} for s in ("artifact", "reference")),
        "proofs_pass": all((res["dynamic"].get("run_json") or {}).get(s, {}).get("verdict") ==
                           "PASS" for s in ("artifact", "reference")),
        "qemu_is_qint": all((res["dynamic"].get("run_json") or {}).get(s, {}).get("qemu_sha256")
                            in (QINT_SHA, "sha256:" + QINT_SHA) for s in ("artifact", "reference")),
        # "≈ N": every iteration but at most a few warm-up ones
        "patched_c_executed_about_n": isinstance(c_hits, int) and N_ITER - 5 <= c_hits <= N_ITER,
        "r_stub_executed_about_n": isinstance(s_hits, int) and N_ITER - 5 <= s_hits <= N_ITER,
        "r_stub_f_not_taken": f_hits == 0,
        "slot_reset_only_warmup": isinstance(rst, int) and 1 <= rst <= 5,
        "hits_runs_exit_0": bool(hits.get("copy_ok")),
        # m-CE7: strict twin, and the ordering twin's window rule (see the module doc)
        "twin_strict_clean": bool(twc.get("clean")),
        "twin_no_flush_while_r_or_c_live": (oc.get("benchmark_start_seen") and
                                            oc.get("benchmark_end_seen") and
                                            oc.get("qemu_exit") == 0 and
                                            oc.get("flushes_before_last_probe_hit") == 0 and
                                            twc.get("qemu_exit") == 0 and
                                            (twc.get("e_bp") or {}).get("lists_empty") is True and
                                            not twc.get("unresolved") and
                                            twc.get("guest_norm_equal_source") is True),
    }
    res["items"] = items
    res["ok"] = all(v for k, v in items.items() if k != "twin_strict_clean")
    log("slice0b items", items)
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--steps", default="bases,summary,static,slice0b")
    ap.add_argument("--modes", default="icount,noicount")
    ap.add_argument("--timeout", type=float, default=300.0)
    a = ap.parse_args()
    out = a.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    if sha256(QINT) != QINT_SHA:
        raise SystemExit("qint exe sha changed")
    modes = a.modes.split(",")
    steps = a.steps.split(",")
    jf = out / "wp27-slice0.json"
    doc: dict[str, Any] = json.loads(jf.read_text()) if jf.is_file() else {}
    head = subprocess.run(["git", "-C", str(WT), "rev-parse", "HEAD"], capture_output=True,
                          text=True).stdout.strip()
    dirty = subprocess.run(["git", "-C", str(WT), "status", "--porcelain", "--", "scopt",
                            "experiments", "tests"], capture_output=True, text=True).stdout.strip()
    doc["env"] = {"scopt_head": head, "scopt_dirty": bool(dirty), "qemu": str(QINT),
                  "qemu_sha256": QINT_SHA, "fixture_elf_sha256": sha256(FIX / "b3.elf"),
                  "loadavg_start": Path("/proc/loadavg").read_text().strip(), "cpus": CPUS,
                  "timed": False, "timing_lock_taken": False}
    if "bases" in steps:
        doc["bases"] = step_bases(out, modes)
    if "summary" in steps:
        doc["summary"] = step_summary(out, modes)
    if "static" in steps:
        doc["static"] = step_static(out, modes, doc.get("summary", {}))
    if "slice0b" in steps:
        doc["slice0b"] = step_slice0b(out, doc.get("static", {}), a.timeout)
    doc["env"]["loadavg_end"] = Path("/proc/loadavg").read_text().strip()
    jf.write_text(json.dumps(doc, indent=1, sort_keys=True, default=str) + "\n")
    summary = {k: ({m: (doc[k].get(m) or {}).get("ok") for m in modes}
                   if k != "slice0b" else {"icount": doc[k].get("ok")})
               for k in ("bases", "summary", "static", "slice0b") if k in doc}
    log("summary", json.dumps(summary))
    (out / "verdict.json").write_text(json.dumps(verdict(doc, modes), indent=1, sort_keys=True,
                                                 default=str) + "\n")
    return 0 if all(x for v in summary.values() for x in v.values()) else 1


def verdict(doc: dict[str, Any], modes: list[str]) -> dict[str, Any]:
    """The WP27 unit-stage acceptance rows (plan ``## WP27`` steps 1-7, T-B3-8)."""
    def pf(x: Any) -> str:
        return "pass" if x else "fail"
    b, s, st, s0 = (doc.get(k) or {} for k in ("bases", "summary", "static", "slice0b"))
    rows = []
    rows.append({"criterion": "step 1: F-B3-1 (modifier r0 <- 0 at f's bx lr) Tier-0 capture + "
                 "Profile v1 + import on qint", "result": pf(all((b.get(m) or {}).get("ok")
                                                                 for m in modes)),
                 "evidence": {m: {k: (b.get(m) or {}).get(k) for k in
                                  ("snapshot", "profile", "prep_verdict", "ri_envlayout_id",
                                   "elf_sha256")} for m in modes}})
    rows.append({"criterion": "step 2: exit-facts r0 == 0 source=patch, certified by hostsym "
                 "(V14); CalleeSummary row exported", "result": pf(
                     all((s.get(m) or {}).get("ok") for m in modes) and
                     all(((st.get(m) or {}).get("full") or {}).get("validation", {}).get(
                         "codes", {}).get("V14") == "pass" for m in modes)),
                 "evidence": {m: {k: (s.get(m) or {}).get(k) for k in
                                  ("r_units", "r0_facts", "file", "file_sha256", "row_hashes")}
                              for m in modes}})
    rows.append({"criterion": "step 3: specialize-returns CLONE C->C' + SPEC slot on R; CP/BF/DHO "
                 "patch C'; static validators accept; byte diff and per-pass ledger rows match "
                 "§3.5 (each ablation)", "result": pf(all((st.get(m) or {}).get("ok")
                                                         for m in modes)),
                 "evidence": {m: {"separation": (st.get(m) or {}).get("separation"),
                                  "v7_without_override": {
                                      k: ((st.get(m) or {}).get("full_no_override") or {}).get(k)
                                      for k in ("status", "reason", "fail_rows")},
                                  "cases": {c: {k: ((st.get(m) or {}).get(c) or {}).get(k)
                                                for k in ("status", "artifact_id", "bytes_ok",
                                                          "static_rows_ok", "ledger_ok",
                                                          "factref_summary_row_ok",
                                                          "pass_byte_delta")}
                                            for c in W.CASES}} for m in modes}})
    it = s0.get("items") or {}
    cp = s0.get("copy") or {}
    rows.append({"criterion": "step 4: v5 copy with R's GENERAL guarded slot and the slice-0a C' "
                 "patches in place in C, labelled experimental_unsound_in_place_specialization, "
                 "never published", "result": pf((cp.get("checks") or {}).get("ok")),
                 "evidence": {k: cp.get(k) for k in ("dir", "cache_sc_sha256", "patches", "R", "C")}})
    rows.append({"criterion": "step 5a: icount run on qint, O1 equal warm (also O2a, O2b; z=N nz=0)",
                 "result": pf(all(it.get(k) for k in ("o1_equal_warm", "o2a_equal_warm",
                                                      "o2b_equal_warm", "z_n_nz_0", "proofs_pass",
                                                      "qemu_is_qint"))),
                 "evidence": {"oracles": (s0.get("dynamic") or {}).get("oracles"),
                              "runs": (s0.get("dynamic") or {}).get("run_json")}})
    rows.append({"criterion": "step 5b: gdb -nx diag: patched C executed ~N times, R's stub F "
                 "not taken after warm-up", "result": pf(all(it.get(k) for k in (
                     "patched_c_executed_about_n", "r_stub_executed_about_n",
                     "r_stub_f_not_taken", "slot_reset_only_warmup", "hits_runs_exit_0"))),
                 "evidence": s0.get("hits")})
    tw = s0.get("twins") or {}
    rows.append({"criterion": "step 5c: m-CE7 diag twin clean (literal rule: 0 tlb_flush*/"
                 "tcg_commit_cpu calls after benchmark_start)",
                 "result": pf(it.get("twin_strict_clean")),
                 "evidence": {k: {x: (tw.get(k) or {}).get(x) for x in
                                  ("clean", "total_after_benchmark_start", "counters_nonzero")}
                              for k in ("copy", "warm")}})
    rows.append({"criterion": "step 5c': no TLB flush while R's linked slot or the in-place C "
                 "could execute (ordering twin: every flush after benchmark_start happened after "
                 "the last execution of C and R's stub)",
                 "result": pf(it.get("twin_no_flush_while_r_or_c_live")),
                 "evidence": {k: (tw.get("ordering_copy") or {}).get(k) for k in
                              ("flush_stops", "flushes", "final_probe_hits",
                               "flushes_before_last_probe_hit", "qemu_exit")}})
    for crit in ("step 6: v6 writer emits one variant, one SPEC slot, one link; deploy on the "
                 "gate binary", "step 7: O1/O2a/O2b equal; O3 armed = 1, C' hits ~ N - warm-up; "
                 "O4 falsified-fact mutant changes O1; X-REHY with the variant (coverage config)",
                 "T-B3-8 (slice dynamic on the gate binary)"):
        rows.append({"criterion": crit, "result": "not_run",
                     "evidence": "integration stage (slice 1 on the frozen gate binary "
                                 "runs/wp22/gate-binary.json); not part of the unit stage"})
    return {"schema": "wp27.verdict/1", "stage": "unit (slices 0a, 0b)", "env": doc.get("env"),
            "verdict": "pass" if all(r["result"] in ("pass", "not_run") for r in rows
                                     if not r["criterion"].startswith("step 5c:")) else "fail",
            "verdict_note": "step 5c (literal m-CE7) fails on every fixtures/b3 run, the warm "
                            "reference included: the fixture's teardown writes MPU_CTRL = 0 after "
                            "the measured loop (WP26 note 10); step 5c' is the WP27 replacement "
                            "evidence and is what the verdict uses",
            "rows": rows, "timed": False, "timing_lock_taken": False}


if __name__ == "__main__":
    sys.exit(main())
