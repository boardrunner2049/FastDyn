#!/usr/bin/env python3
"""WP22 unit diag runs of the v6 loader under ``gdb -nx`` (design §5.5-5.6, Q5-Q9).

    v6_gdb_diag.py --qemu QINT_V6 --out OUT [--scenario exec tlb tbflush hmp]

The guarded synthetic file of ``v6_unit.py`` (SPEC links whose variants are
relocated clones of C, GENERAL slots) is deployed into a fresh run directory
by the scopt harness (oracle, sparse copy); QEMU is then started **directly**
with the oracle's argv (``qemu_argv.json``) plus ``-name wp22,debug-threads=on``
under ``setarch x86_64 -R timeout gdb -nx -batch``, cwd ``src/fastdyn`` (not a
QEMU tree). ``QEMU_SC_V6_STATS`` is set in every scenario.

Scenarios (the gdb side is ``v6_gdb_script.py``):

* ``exec``: hardware breakpoints on up to four variant entries ``C'.tc.ptr``
  count how often each clone runs (proof that an armed SPEC slot transfers
  control into C'), nothing is perturbed;
* ``tlb``: at the first variant entry, ``call tlb_flush(cpu)`` on the vCPU
  thread (what an MPU write does, G12): every guarded slot must be unlinked,
  ``disarm_tlb_events == 1``, no invariant violation, and no variant that was
  armed at the flush is entered afterwards (Q7, §5.6 invariant);
* ``tbflush``: at the first ``cpu_exec`` entry after a variant ran, ``call
  tb_flush(cpu)`` (serial context, outside any TB): the links are cleared (Q6)
  and re-armed in the new epoch (``links_armed`` grows, variants rebuilt);
* ``hmp``: at the first variant entry the vCPU stays stopped while only the
  main thread runs (scheduler-locking) and receives HMP ``one-insn-per-tb on``
  on a ``-monitor`` unix socket: the setter must run on a non-vCPU thread and
  only queue work; the disarm (``sc6_disarm_all_work``) and every
  ``tb_unlink_jump`` must run on the vCPU thread in the exclusive context (Q9).

Every scenario also requires exit 0 and the guest output of the v5 control.
Writes ``OUT/v6-gdb-diag.json``.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE))
from scopt.run.deploy import deploy_new  # noqa: E402
from scopt.run.launcher import FASTDYN_SRC, file_sha256, norm_guest, reset_ram, scrub_env  # noqa: E402

import v6_synth as V  # noqa: E402
import v6_unit as U  # noqa: E402

GDB_SCRIPT = HERE / "v6_gdb_script.py"


def run_scenario(u: U.Unit, sc: Path, scenario: str, meta: dict[str, Any],
                 timeout: int = 300) -> dict[str, Any]:
    rd, _ = deploy_new(sc, U.BASE_TOML, u.out / "runs" / scenario, qemu=u.qemu)
    reset_ram(rd)
    qa = json.loads((rd.root / "qemu_argv.json").read_text())
    argv = list(qa["argv"]) + ["-name", "wp22,debug-threads=on"]
    mon = Path(f"/tmp/wp22-mon-{os.getpid()}-{scenario}.sock")
    if scenario == "hmp":
        argv += ["-monitor", f"unix:{mon},server=on,wait=off"]
    variants = [n for n in meta["notes"] if n["kind"] == "spec"]
    params = {
        "scenario": scenario,
        "result": str(rd.root / "gdb-result.json"),
        "variants": [{"vaddr": n["vc"], "r": n["r_vaddr"], "tc": n["variant_tc_abs"],
                      "id": n["variant_id"]} for n in variants][:4],
        "monitor": str(mon),
    }
    env, _ = scrub_env(qemu=u.qemu, extra={**qa.get("env_set", {}),
                                            "QEMU_SC_V6_STATS": str(rd.root / "v6-stats.json"),
                                            "WP22_GDB_PARAMS": json.dumps(params)})
    cmd = ["setarch", "x86_64", "-R", "timeout", "--signal=TERM", "--kill-after=10", str(timeout),
           "gdb", "-nx", "-batch", "-x", str(GDB_SCRIPT), "--args", *argv]
    t0 = time.monotonic()
    with open(rd.root / "gdb.log", "w") as f:
        p = subprocess.run(cmd, cwd=str(FASTDYN_SRC), env=env, stdout=f,
                           stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    text = (rd.root / "gdb.log").read_text(errors="replace")
    res = json.loads(Path(params["result"]).read_text()) if Path(params["result"]).is_file() \
        else {}
    st_p = rd.root / "v6-stats.json"
    return {"run_dir": str(rd.root), "gdb_exit": p.returncode,
            "seconds": round(time.monotonic() - t0, 1), "gdb": res,
            "stats": json.loads(st_p.read_text()) if st_p.is_file() else None,
            "guest_norm": norm_guest(text), "params": params}


def evaluate(u: U.Unit, scenario: str, r: dict[str, Any]) -> list[tuple[str, bool, Any]]:
    g, st = r["gdb"], r["stats"] or {}
    exit_ok = g.get("exit_code") == 0
    out_ok = [l for l in r["guest_norm"] if l.startswith(("Reg ", "[BENCH]"))] == \
        [l for l in u.v5_guest if l.startswith(("Reg ", "[BENCH]"))]
    items = [("QEMU exit 0 under gdb", exit_ok, g.get("exit_code")),
             ("guest output (Reg/BENCH lines) equals the v5 control", out_ok, r["guest_norm"]),
             ("no invariant violation", st.get("invariant_violations") == 0,
              st.get("invariant_violations"))]
    hits = g.get("variant_hits", {})
    if scenario == "exec":
        items += [("an armed SPEC slot transferred control into a variant (hbreak on C'.tc.ptr)",
                   sum(h["total"] for h in hits.values()) >= 1, hits),
                  ("links armed == 7", st.get("links_armed") == len(U.SPEC), st.get("links_armed"))]
    elif scenario == "tlb":
        items += [("tlb_flush called at the first variant entry", g.get("tlb_flush_called"),
                   g.get("events", [])[:3]),
                  ("disarm_tlb_events == 1 and every armed link disarmed",
                   st.get("disarm_tlb_events") == 1
                   and st.get("disarm_tlb_links") == g.get("armed_at_flush"),
                   (st.get("disarm_tlb_events"), st.get("disarm_tlb_links"),
                    g.get("armed_at_flush"))),
                  ("slots unlinked >= 1", (st.get("slots_unlinked") or 0) >= 1,
                   st.get("slots_unlinked")),
                  ("invariant checked", (st.get("invariant_checks") or 0) >= 1,
                   st.get("invariant_checks")),
                  ("no variant armed at the flush is entered after it",
                   all(h["after_flush"] == 0 for k, h in hits.items()
                       if k in g.get("armed_variant_ids_at_flush", [])),
                   {k: h for k, h in hits.items()}),
                  ("disarmed links stay off for the epoch (disarmed_epoch)",
                   sum(1 for l in st.get("links", []) if l["disarmed_epoch"])
                   == g.get("armed_at_flush"), st.get("links"))]
    elif scenario == "tbflush":
        items += [("tb_flush called outside any TB", g.get("tb_flush_called"),
                   g.get("events", [])[:3]),
                  ("variant entry-check exit counted (Q10)",
                   (st.get("variant_entry_exits") or 0) >= 1, st.get("variant_entry_exits")),
                  ("flush reset counted and armed links cleared",
                   (st.get("flush_resets") or 0) >= 1 and (st.get("disarm_flush_links") or 0) >= 1,
                   (st.get("flush_resets"), st.get("disarm_flush_links"))),
                  ("links re-armed in the new epoch (links_armed > 7, variants rebuilt)",
                   (st.get("links_armed") or 0) > len(U.SPEC)
                   and (st.get("variants_built") or 0) > len(U.SPEC),
                   (st.get("links_armed"), st.get("variants_built")))]
    elif scenario == "hmp":
        ev = g.get("events", [])
        setter = [e for e in ev if e["bp"] == "tcg_supercache_v6_one_insn_per_tb"]
        work = [e for e in ev if e["bp"] == "sc6_disarm_all_work"]
        unl = [e for e in ev if e["bp"] == "tb_unlink_jump"]
        vcpu = g.get("vcpu_thread_name")
        items += [("HMP setter ran on a non-vCPU thread",
                   bool(setter) and all(e["thread"] != vcpu for e in setter),
                   setter[:2]),
                  ("disarm ran on the vCPU thread in the exclusive context",
                   bool(work) and all(e["thread"] == vcpu and e.get("exclusive") == 1
                                      for e in work), work[:2]),
                  ("no tb_unlink_jump frame on a non-vCPU thread",
                   all(e["thread"] == vcpu for e in unl), (len(unl), unl[:3])),
                  ("tb_unlink_jump called (slots were live)", len(unl) >= 1, len(unl)),
                  ("stats: forbidden by one_insn_per_tb, disarm counted",
                   st.get("forbidden") is True and st.get("forbid_reason") == "one_insn_per_tb"
                   and st.get("disarm_forbid_events") == 1,
                   {k: st.get(k) for k in ("forbidden", "forbid_reason", "disarm_forbid_events",
                                           "disarm_forbid_links")})]
    return items


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--qemu", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--scenario", nargs="*", default=["exec", "tlb", "tbflush", "hmp"])
    a = ap.parse_args()
    out = a.out.resolve()
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"{out} exists and is not empty")
    (out / "files").mkdir(parents=True)
    u = U.Unit(a.qemu.resolve(), out)
    u.prepare()
    u.case_v5()
    p = out / "files/guarded.sc"
    meta = V.guarded_model(str(u.base_sc), u.snap, U.SPEC, U.GENERAL).write(p)
    results: dict[str, Any] = {}
    for sc in a.scenario:
        r = run_scenario(u, p, sc, meta)
        items = evaluate(u, sc, r)
        verdict = "PASS" if all(ok for _, ok, _ in items) else "FAIL"
        results[sc] = {"verdict": verdict, "checks": [{"check": c, "ok": bool(ok), "detail": d}
                                                      for c, ok, d in items], "run": r}
        print(f"[{verdict}] {sc}" + "".join(f"\n    FAIL {c}: {d}" for c, ok, d in items
                                            if not ok), flush=True)
    verdict = "PASS" if all(v["verdict"] == "PASS" for v in results.values()) else "FAIL"
    doc = {"schema": "wp22.v6-gdb-diag/1", "verdict": verdict, "qemu": str(u.qemu),
           "qemu_sha256": file_sha256(u.qemu), "base": u.base_info, "file": meta,
           "v5_control": u.cases.get("v5-control", {}).get("verdict"),
           "summary": {k: v["verdict"] for k, v in results.items()}, "scenarios": results}
    (out / "v6-gdb-diag.json").write_text(json.dumps(doc, indent=1, default=str) + "\n")
    print(f"verdict {verdict} -> {out / 'v6-gdb-diag.json'}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
