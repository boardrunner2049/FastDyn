"""WP29: the B4 gate verdict (design §11 "B4 gate" items 1-5) from the evidence files of
``tests/qemu/wp29_gate.py`` / ``wp29_timing.sh`` in one run directory.

Pure function of the files (unit-tested on synthetic directories by
``tests/unit/test_wp29_gate.py``). Unknown is never pass: a missing evidence file makes
its item ``not_run`` and the gate cannot pass.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

#: T-B4-1: the model counts on compute (map-state-sync §7.3; WP25 unit note item 4)
MODEL = {"D1": 469, "D2": 1665, "D2_prefix": 678, "D3": 5681}
FIXTURES_ITEM2 = ["b4-1", "b4-2", "b4-3", "b4-4", "b4-5", "b4-6", "b4-7", "b4-9"]
MODES = ["icount", "noicount"]
#: classes a fixture "allows" (gate item 2: "D1 and D2 deletions > 0 where the fixture
#: allows them"). Every F-B4 fixture has deletable D1 and D2 stores.
NEED_CLASSES = ("D1", "D2")
#: design §9.3 B4 mutants and the kinds that implement them (wp29_gate.DESIGN_93_B4)
DESIGN_93_B4 = {
    "delete a guest store": ["guest_store_delete"],
    "delete an UPDATE_MEM / LOG_REG store": ["update_mem_delete", "log_reg_delete"],
    "delete a flag store before an LDST": ["flag_store_before_ldst"],
    "delete a flag store before a flags-0 helper": ["flag_store_before_helper"],
    "delete a brcond sync store whose global is reloaded (T-B4-2b)": ["brcond_reload"],
    "delete the last-insn can_do_io=1 before a direct-chain exit (F-B4-9)":
        ["cdio_last_insn_direct_chain"],
    "IT-block D4 mutant": ["it_block_d4", "it_block_d4_slowpath"],
    "change an icount immediate": ["icount_immediate"],
    "put a host_abs pointer inside env": ["host_abs_in_env"],
}
#: kinds whose dynamic detection is not required (b4_mutants.STATIC_ONLY, design §9.3)
STATIC_ONLY = {"d2_before_lookup", "d3_without_fact"}
#: close-out (design/closeout-logreg.md B.3): kinds judged on one designated detector
#: fixture, every instance there in icount, each with exactly its predicted divergence
#: (``o4_extra`` attempts with ``predicted_exact``). Disclosed change from design §9.5,
#: which named F-B4-1 (no instance of the kind on the gate base).
SPECIFIC_DETECTOR = {"flag_store_before_helper": "b4-11"}


def _rd(p: Path) -> Any:
    return json.loads(p.read_text()) if p.is_file() else None


def _item(ok: bool | None, **kw: Any) -> dict[str, Any]:
    return {"pass": ok, "status": "pass" if ok else ("not_run" if ok is None else "fail"), **kw}


def item1(out: Path) -> dict[str, Any]:
    """T-B4-1, T-B4-2, T-B4-2b; every B4 mutant rejected statically and detected (O4)."""
    base = _rd(out / "compute" / "base.json") or {}
    allu = (base.get("census_all_units") or {}) if isinstance(base.get("census_all_units"),
                                                               dict) else {}
    m0 = (base.get("census_tcg_m0") or {}) if isinstance(base.get("census_tcg_m0"), dict) else {}
    dead = allu.get("dead") or {}
    t1_rows = {k: {"gate_base": dead.get(k), "model": MODEL[k],
                   "within_5pct": dead.get(k) is not None and
                   abs(dead[k] - MODEL[k]) <= 0.05 * MODEL[k]} for k in ("D1", "D2", "D3")}
    t1 = bool(dead) and all(r["within_5pct"] for r in t1_rows.values()) and \
        (allu.get("d2_split") or {}).get("prefix") == MODEL["D2_prefix"] and \
        (m0.get("dead") or {}).get("D1") == 0
    log = out / "pytest-t-b4-static.log"
    py = log.read_text() if log.is_file() else ""
    mpy = re.search(r"(\d+) passed", py)
    py_ok = bool(mpy) and not re.search(r"\d+ (failed|error)", py)
    mu = _rd(out / "mutants.json")
    if mu is None:
        return _item(None, why="mutants.json missing", t_b4_1=t1_rows)
    st = mu["static"]
    static_ok = st["total"] > 0 and st["rejected"] == st["total"] and \
        bool(st.get("synthetic_b4", {}).get("all_rejected"))
    o4 = {r["kind"]: r for r in (mu.get("o4") or [])}
    tries: dict[str, int] = {}
    for r in (mu.get("o4") or []) + (mu.get("o4_extra") or []):
        tries[r["kind"]] = tries.get(r["kind"], 0) + len(r.get("attempts") or [])
    for r in mu.get("o4_extra") or []:  # later rounds (o4-extra, host-abs, o4-cover):
        # a round-1 detection stands; otherwise the later record (more attempts) is used
        if r["kind"] not in o4 or o4[r["kind"]].get("verdict") != "pass":
            o4[r["kind"]] = {**r, "round": 2}
    required = sorted({k for ks in DESIGN_93_B4.values() for k in ks})
    gate_sha = (_rd(out / "gate.json") or {}).get("exe_sha256")
    want_sha = [f"sha256:{gate_sha}"] if gate_sha else None

    def _specific(k: str) -> dict[str, Any]:
        fx = SPECIFIC_DETECTOR[k]
        rx = next((r for r in mu.get("o4_extra") or [] if r["kind"] == k), {})
        at = [a for a in rx.get("attempts") or [] if a.get("config") == fx]
        ic = [a for a in at if a.get("mode", "icount") == "icount"]
        nc = [a for a in at if a.get("mode") == "noicount"]

        def good(a: dict[str, Any]) -> bool:
            return a.get("verdict") == "pass" and a.get("predicted_exact") is True and \
                want_sha is not None and a.get("runs_qemu_sha256") == want_sha
        return {"fixture": fx, "icount_instances": len(ic),
                "icount_detected_as_predicted": sum(good(a) for a in ic),
                "subphase_flags": sorted({f"{a.get('sub')}-{a.get('flag')}" for a in ic}),
                "noicount_reported": {"instances": len(nc),
                                      "detected_as_predicted": sum(good(a) for a in nc)},
                "anomalies": [a.get("uid") for a in at if a.get("verdict") == "anomaly"],
                "pass": bool(ic) and all(good(a) for a in ic)}

    specific = {k: _specific(k) for k in SPECIFIC_DETECTOR}

    def _det(k: str) -> str:
        if k in specific:
            return "pass" if specific[k]["pass"] else "fail"
        r = o4.get(k) or {}
        v = r.get("verdict", "not_run")
        if v == "pass" and not any(a.get("verdict") == "pass" and want_sha is not None and
                                   a.get("runs_qemu_sha256") == want_sha
                                   for a in r.get("attempts") or []):
            return "fail_not_on_gate_binary"
        return v

    detected = {k: _det(k) for k in required}
    extras = {k: r.get("verdict") for k, r in o4.items() if k not in required}
    design = {}
    for name, ks in DESIGN_93_B4.items():
        rows = {}
        for k in ks:
            real = [r for r in st["rows"] if r["kind"] == k]
            syn = [r for r in st.get("synthetic_b4", {}).get("results", [])
                   if r.get("name") == k]
            rows[k] = {"static_real": (sum(r["check"]["rejected"] for r in real), len(real)),
                       "static_synthetic": (sum(bool(r.get("rejected")) for r in syn), len(syn)),
                       "o4": detected[k],
                       "o4_round": (o4.get(k) or {}).get("round", 1 if k in o4 else None),
                       "o4_attempts_both_rounds": tries.get(k, 0),
                       "o4_coverage": {c: ((o4.get(k) or {}).get("coverage") or {}).get(c)
                                       for c in ("instances", "executed_instances",
                                                 "executed_instances_tried")}
                       if (o4.get(k) or {}).get("coverage") else None}
        design[name] = rows
    t2b = (o4.get("brcond_reload") or {}).get("verdict") == "pass"
    o4_all = all(v == "pass" for v in detected.values())
    return _item(t1 and py_ok and static_ok and o4_all and t2b,
                 t_b4_1={"rows": t1_rows, "d2_prefix": (allu.get("d2_split") or {}).get("prefix"),
                         "m0_d1": (m0.get("dead") or {}).get("D1"), "pass": t1},
                 t_b4_2_static_pytest={"pass": py_ok, "summary": py.strip().splitlines()[-1]
                                       if py.strip() else None},
                 static={"total": st["total"], "rejected": st["rejected"],
                         "synthetic": {k: st.get("synthetic_b4", {}).get(k) for k in
                                       ("mutants", "rejected", "all_rejected")},
                         "pass": static_ok},
                 t_b4_2b_dynamic=t2b, o4_by_kind=detected, o4_extras_reported=extras,
                 static_only=sorted(STATIC_ONLY),
                 o4_not_detected=sorted(k for k, v in detected.items() if v != "pass"),
                 o4_specific_detector=specific, design_93=design)


def _cpu_abort_gdb_zero(d: dict[str, Any] | None) -> bool:
    ca = (d or {}).get("cpu_abort_calls_artifact") or {}
    return bool(ca.get("resolved")) and ca.get("hits") == 0


def item2(out: Path) -> dict[str, Any]:
    gate_sha = (_rd(out / "gate.json") or {}).get("exe_sha256")
    want_sha = [f"sha256:{gate_sha}"] if gate_sha else None
    pk = {(r["mode"], r["config"]): r for r in (_rd(out / "package.json") or {}).get("rows", [])}
    dy = {(r["mode"], r["config"]): r for r in (_rd(out / "dynamic.json") or {}).get("rows", [])}
    de = {r["config"]: r for r in (_rd(out / "deletions.json") or {}).get("rows", [])}
    rows = []
    for cfg in FIXTURES_ITEM2:
        for mode in MODES:
            p, d = pk.get((mode, cfg)), dy.get((mode, cfg))
            st = (p or {}).get("static") or {}
            ex = (de.get(cfg) or {}).get("deleted_in_entered_units") or {}
            dele = {k: ex.get(k, 0) > 0 for k in NEED_CLASSES} if mode == "icount" else None
            on_gate = want_sha is not None and (d or {}).get("runs_qemu_sha256") == want_sha
            abort_ok = cfg != "b4-9" or (_cpu_abort_gdb_zero(d) and
                                         not any((d or {}).get("cpu_abort_seen") or []))
            ok = bool(p and d and p.get("status") == "published" and d.get("pass")
                      and st.get("pass", True) is not False
                      and (dele is None or all(dele.values())) and on_gate and abort_ok)
            rows.append({"config": cfg, "mode": mode, "fixture": (p or {}).get("fixture"),
                         "published": (p or {}).get("status"),
                         "by_class": (p or {}).get("by_class"),
                         "v12_pass_units": (p or {}).get("v12_pass"),
                         "static_check": st.get("check"), "static_pass": st.get("pass"),
                         "gated_oracles": (d or {}).get("gated_oracles"),
                         "oracles_verdict": (d or {}).get("verdict"),
                         "per_rep": (d or {}).get("oracles"),
                         "nonvacuous": (d or {}).get("nonvacuous"),
                         "deleted_in_entered_units": ex if mode == "icount" else
                         (d or {}).get("deleted_in_executed_probes"),
                         "d1_d2_executed": dele,
                         "io_recompile": {k: (v.get("hits") if isinstance(v, dict) else v)
                                          for k, v in ((d or {}).get("io_recompile") or
                                                       {}).items()} or None,
                         "events_tsv_equal": (d or {}).get("events_equal"),
                         "counts_txt_equal": (d or {}).get("counts_equal"),
                         "cpu_abort_seen": (d or {}).get("cpu_abort_seen"),
                         "cpu_abort_gdb_calls": ((d or {}).get("cpu_abort_calls_artifact")
                                                 or {}).get("hits"),
                         "runs_on_gate_binary": on_gate,
                         "o2a_noicount_timer_reported": (d or {}).get(
                             "o2a_reported_noicount_timer"),
                         "runs_qemu_sha256": (d or {}).get("runs_qemu_sha256"),
                         "pass": ok})
    have = bool(pk) and bool(dy) and bool(de)
    return _item(all(r["pass"] for r in rows) if have else None, rows=rows)


def item3(out: Path) -> dict[str, Any]:
    pk = _rd(out / "compute" / "package.json")
    cv = _rd(out / "compute-v10.json")
    if pk is None or cv is None:
        return _item(None, why="compute evidence missing")
    b4 = next((r for r in pk["rows"] if r["label"] == "b4"), None)
    v = next((r for r in cv["rows"] if r["label"] == "b4"), None)
    vh = (b4 or {}).get("v12_host_abs") or {}
    ok = bool(b4 and b4.get("status") == "published" and vh.get("pass") and v and
              v.get("verdict") == "pass")
    return _item(ok, v12_host_abs=vh, by_class=(b4 or {}).get("by_class"),
                 v10={k: (v or {}).get(k) for k in ("verdict", "appended", "failures",
                                                     "runs_qemu_sha256")},
                 artifact=(b4 or {}).get("artifact_dir"))


def item4(out: Path) -> dict[str, Any]:
    """T-B4-13 on F-B4-10 (coverage): B4 deletes D1 stores on coverage units (both modes),
    the deleted D1 stores execute (icount gdb entry counts, ``deletions.json``: the same
    non-vacuity rule as item 2), and O1/O2a/O2b/O5 + **O6** pass on the gate binary."""
    gate_sha = (_rd(out / "gate.json") or {}).get("exe_sha256")
    want_sha = [f"sha256:{gate_sha}"] if gate_sha else None
    pk = {(r["mode"], r["config"]): r for r in (_rd(out / "package.json") or {}).get("rows", [])}
    de = {r["config"]: r for r in (_rd(out / "deletions.json") or {}).get("rows", [])}
    diag = _rd(out / "b4-10.json") or {}
    rows = []
    for mode in MODES:
        p = pk.get((mode, "b4-10")) or {}
        bc = p.get("by_class") or {}
        rows.append({"mode": mode, "status": p.get("status"), "by_class": bc,
                     "reason": p.get("reason"), "stats": p.get("stats"),
                     "d1_deleted": bc.get("D1", 0)})
    vacuous = all(r["d1_deleted"] == 0 for r in rows)
    dy = {(r["mode"], r["config"]): r for r in (_rd(out / "dynamic.json") or {}).get("rows", [])}
    dyn = [dy.get((m, "b4-10")) for m in MODES]
    ex = (de.get("b4-10") or {}).get("deleted_in_entered_units") or {}
    executed_d1 = (ex.get("D1") or 0) > 0
    on_gate = [d is not None and want_sha is not None and d.get("runs_qemu_sha256") == want_sha
               for d in dyn]
    if not pk:
        ok = None
    elif vacuous:
        ok = False  # T-B4-13 needs D1 deletions > 0 (design §9.5 F-B4-10)
    else:
        ok = all(r["status"] == "published" and r["d1_deleted"] > 0 for r in rows) and \
            executed_d1 and all(on_gate) and \
            all(d is not None and d.get("pass") and "O6" in (d.get("gated_oracles") or ())
                for d in dyn)
    return _item(ok, rows=rows, vacuous=vacuous, executed_d1_icount=ex or None,
                 executed_d1_gt_0=executed_d1, runs_on_gate_binary=on_gate,
                 dynamic=[{k: (d or {}).get(k) for k in ("mode", "verdict", "gated_oracles",
                                                         "oracles", "pass")} for d in dyn],
                 diagnosis={m: {k: v for k, v in
                                                               (diag.get(m) or {}).items()
                                                               if k != "example"}
                                                           for m in MODES},
                 o6_control_warm_vs_warm=(diag.get("o6_control_warm_vs_warm") or {}).get(
                     "verdict"))


def item5(out: Path) -> dict[str, Any]:
    pk = _rd(out / "compute" / "package.json")
    cv = _rd(out / "compute-v10.json")
    tm = _rd(out / "timing-analysis.json")
    abl = []
    for r in (pk or {}).get("rows", []):
        v = next((x for x in (cv or {}).get("rows", []) if x["label"] == r["label"]), {})
        abl.append({"label": r["label"], "status": r.get("status"), "by_class": r.get("by_class"),
                    "v12_host_abs_pass": (r.get("v12_host_abs") or {}).get("pass"),
                    "ledger": [(x.get("id"), x.get("verdict"), (x.get("counts") or {}).get(
                        "stores")) for x in r.get("ledger") or []],
                    "v10": v.get("verdict")})
    validated = bool(abl) and all(a["status"] == "published" and a["v10"] == "pass"
                                  for a in abl)
    lf = out / "timing-load.log"
    cpu = None
    if lf.is_file():
        lines = lf.read_text().splitlines()
        loads = [float(x.split()[1]) for x in lines if re.match(r"^\d+ ", x)]
        cpu = {"cores": "taskset (4 cores, see start line)", "lock": "flock runs/.timing.lock",
               "load1_min": min(loads) if loads else None,
               "load1_max": max(loads) if loads else None, "load_samples": len(loads),
               "start": next((x for x in lines if x.startswith("start ")), None),
               "lock_acquired": next((x for x in lines if x.startswith("lock acquired")), None),
               "end": next((x for x in lines if x.startswith("end ")), None)}
    return {"reported_outcome": True, "ablations_validated": validated, "ablations": abl,
            "timing": tm if tm is not None else {"status": "not_run"}, "cpu_budget": cpu}


def reported_b4_11(out: Path) -> dict[str, Any]:
    """F-B4-11 real-pass rows (design/closeout-logreg.md B.3 step 5; reported next to item 2,
    not part of its fixture list): the real B4 artifact keeps every flag_store_before_helper
    store (``observed_at_sync_point``) and passes O1/O2a/O2b in both modes."""
    pk = {(r["mode"], r["config"]): r for r in (_rd(out / "package.json") or {}).get("rows", [])}
    dy = {(r["mode"], r["config"]): r for r in (_rd(out / "dynamic.json") or {}).get("rows", [])}
    rows = []
    for mode in MODES:
        p, d = pk.get((mode, "b4-11")) or {}, dy.get((mode, "b4-11")) or {}
        st = p.get("static") or {}
        rows.append({"mode": mode, "published": p.get("status"), "by_class": p.get("by_class"),
                     "stores_kept": st.get("pass"), "instances": st.get("instances"),
                     "subphase_flags": st.get("subphase_flags"),
                     "oracles": d.get("oracles"), "dynamic_pass": d.get("pass"),
                     "pass": p.get("status") == "published" and st.get("pass") is True and
                     d.get("pass") is True})
    return {"reported_outcome": True, "rows": rows,
            "pass": bool(pk) and all(r["pass"] for r in rows)}


def build_verdict(out: Path) -> dict[str, Any]:
    out = Path(out)
    gate = _rd(out / "gate.json") or {}
    i1, i2, i3, i4 = item1(out), item2(out), item3(out), item4(out)
    i5 = item5(out)
    items = {"1 T-B4-1/2/2b + mutants (static + O4)": i1,
             "2 F-B4-1..F-B4-9 O1/O2a/O2b per mode, D1/D2 > 0": i2,
             "3 compute V12 + host_abs on every rewritten unit, F-B4-8 V10": i3,
             "4 T-B4-13 F-B4-10 (O6)": i4}
    gate_pass = bool(gate.get("ok")) and all(v["pass"] is True for v in items.values())
    return {"schema": "wp29/b4-gate-verdict/1", "gate_binary": gate,
            "gate": {**items, "5 T-B4-11 ablations + timing (reported)": i5},
            "reported_f_b4_11_real_pass": reported_b4_11(out),
            "b4_gate_pass": gate_pass,
            "blocking": [k for k, v in items.items() if v["pass"] is not True]}
