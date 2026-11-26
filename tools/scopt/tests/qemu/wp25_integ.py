#!/usr/bin/env python3
"""WP25 integration: B4 ``eliminate-state-sync`` on the WP24 fixtures and on compute.

    cd wt/scopt-wp25-int
    PYTHONPATH=$PWD python tests/qemu/wp25_integ.py --out runs/wp25-int [steps...]

Steps (default: all but ``timing``, which is ``tests/qemu/wp25_timing.sh``):

``package``   v2 RuntimeIdentity (admission facts of the fixture config) for every
              ``(mode, config)`` base snapshot of ``OUT/fx`` (captured on the current
              binary by ``tests/qemu/wp20_fixture_snapshots.py``), ``propose_b4``
              (classes D1|D2|D3) and ``package_b4`` into ``OUT/art/<mode>/<cfg>/store``;
              plus the fixture-specific static checks of design §9.5 (F-B4-4 prefix
              ``can_do_io = false`` store kept as ``observed_at_sync_point``; F-B4-5 the
              IT-block ``r1`` store before the LDST kept; F-B4-7 no D3 deletion and the
              UPDATE_MEM host store untouched).
``dynamic``   ``scopt.run.oracles.dynamic_oracles`` (O1, O2a, O2b, O5) of every B4
              artifact against its own base snapshot (warm), with the fixture's per-mode
              oracle (``fixtures/b3/expected.py``); plus non-vacuity (gdb hardware
              breakpoint counts on the entries of rewritten units of the fixture's
              kernel), F-B4-4's ``cpu_io_recompile`` count (artifact == warm) and F-B4-7's
              ``events.tsv`` (artifact == warm).
``mutants``   every B4 mutant kind found in the icount fixture snapshots, plus the real
              UPDATE_MEM host-store deletion on F-B4-7, packaged through ``package_b4``
              (must be rejected statically); a few of them also as negative controls
              (bypass V12-V17) whose O1/O2b must change (O4).
``compute``   V10 (``scopt validate --dynamic``) of the WP25 compute artifacts (all
              classes, D1, D2, D3 and the ``icount-entry-sfb`` artifact) on the held-out
              inputs in icount.
``verdict``   ``OUT/verdict.json``.

Never timed (no lock). Every QEMU run goes through the WP8/WP26 launcher
(``setarch x86_64 -R``, timeout, fresh run directory, ``supercache_flat = false``);
gdb is always ``gdb -nx -batch`` with cwd in the run directory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(os.environ.get("FASTDYN_ROOT") or Path(__file__).resolve().parents[4])
FX = ROOT / "fixtures" / "b3"
QINT = ROOT / "wt/qemu-int/build/qemu-system-arm"
WT = Path(__file__).resolve().parents[2]
PY = sys.executable
CONFIGS = ["b4-1", "b4-2", "b4-3", "b4-4", "b4-5", "b4-6", "b4-7"]
MODES = ["icount", "noicount"]
#: guest label each fixture's kernel is built around (fixtures/b3/b3.nm)
KERNEL_LABEL = {"b4-1": "b4_1_ld", "b4-2": "b4_2_ld", "b4-3": "b4_3_ld", "b4-4": "b4_4_mmio",
                "b4-5": "b4_5_ld", "b4-6": "b4_6_sdiv", "b4-7": "b4_7_ld"}
#: repetitions per mode (icount is deterministic; noicount timer phases are not)
REPS = {"icount": 2, "noicount": 4}
WP25 = ROOT / "runs/wp25"
COMPUTE_TOML = ROOT / "runs/wp13-r2/compute/configs/compute-sc.toml"
COMPUTE_SNAP = ROOT / ("runs/b2/compute/store/snapshots/"
                       "53b4132fc05526ff2925cd3acc610bf613e8b11e2315e15e520bff7fe080dab2")
HELDOUT = [ROOT / f"fixtures/inputs/heldout/freertos-mps2-compute/heldout-{i}.bin"
           for i in (1, 2, 3)]

sys.path.insert(0, str(FX))
import b3conf  # noqa: E402


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def dump(p: Path, doc: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, indent=1, sort_keys=True, default=str) + "\n")


def sym(name: str) -> int:
    for line in (FX / "b3.nm").read_text().splitlines():
        a, _t, n = line.split()[:3]
        if n == name:
            return int(a, 16)
    raise KeyError(name)


def snap_info(out: Path, mode: str, cfg: str) -> dict[str, Any]:
    return json.loads((out / "fx" / mode / cfg / "snap.json").read_text())


def load(out: Path, mode: str, cfg: str):
    from scopt.formats.bundle_v1 import (admission_facts, extend_runtime_identity,
                                         read_runtime_identity)
    from scopt.formats.envlayout_v1 import find_envlayout
    from scopt.importer.import_cache import open_snapshot

    sj = snap_info(out, mode, cfg)
    d = Path(sj["snapshot"])
    s = open_snapshot(d)
    ri = read_runtime_identity(d / "runtime_identity.json")
    lay = find_envlayout(ri["qemu_sha256"])
    man = json.loads((d / "manifest.json").read_text())
    adm = admission_facts(manifest=man, qemu_sha256=ri["qemu_sha256"], envlayout=lay,
                          config_text=Path(sj["base_toml"]).read_text())
    ri2 = extend_runtime_identity(ri, adm)
    return sj, s, lay, ri2


def unit_pcs(u: Any) -> list[int]:
    va = list(u.vaddrs or ())
    if not va or not u.insns:
        return list(va)
    w0 = u.insns[0].words[0]
    return [va[0] + i.words[0] - w0 for i in u.insns]


def selftest(out: Path) -> dict[str, Any]:
    from scopt.validate_sym.selftest import load_or_run

    return load_or_run(str(out / "selftest.json"))


# ---------------------------------------------------------------------------
# package + static fixture checks
# ---------------------------------------------------------------------------

def static_checks(cfg: str, s: Any, prop: Any) -> dict[str, Any]:
    """Fixture-specific static properties (design §9.5 F-B4-4/5/7)."""
    from scopt.analysis.hostops import OpKind, build_hostops

    lab = sym(KERNEL_LABEL[cfg])
    rows: list[dict[str, Any]] = []
    for u in s.units():
        pcs = unit_pcs(u)
        if lab not in pcs:
            continue
        sd = prop.syncdead.get(u.uid)
        r: dict[str, Any] = {"uid": u.uid, "pcs": [hex(x) for x in pcs],
                             "cflags": hex(u.key.cflags), "insn0_start": u.insn0_start,
                             "status": None if sd is None else sd.status,
                             "reason": None if sd is None else sd.reason,
                             "patched": len(prop.patches.get(u.uid, ())),
                             "dead": [] if sd is None else
                             [f"{x.cls}:{x.field}@{x.off}" for x in sd.dead],
                             "kept": [] if sd is None else
                             [f"{x.cls}:{x.field}@{x.off}:{x.reason}" for x in sd.refused]}
        if sd is not None and sd.status == "ok":
            prefix_cdio = [x for x in sd.refused if x.field == "neg.can_do_io"
                           and x.off < u.insn0_start]
            prefix_cdio_dead = [x for x in sd.dead if x.field == "neg.can_do_io"
                                and x.off < u.insn0_start]
            r["prefix_cdio_kept"] = [f"{x.off}:{x.reason}" for x in prefix_cdio]
            r["prefix_cdio_deleted"] = [x.off for x in prefix_cdio_dead]
            r["mmio_insn_index"] = pcs.index(lab)
        rows.append(r)
    doc: dict[str, Any] = {"label": KERNEL_LABEL[cfg], "label_addr": hex(lab), "units": rows}
    ok_units = [r for r in rows if r["status"] == "ok"]
    if cfg == "b4-4":
        # the loop TB with the CPUID load as a non-last insn: its prefix
        # can_do_io = false store protects the MMIO access (S5) and must be kept
        multi = [r for r in ok_units if len(r["pcs"]) > 1 and r["mmio_insn_index"] > 0]
        doc["check"] = "F-B4-4: prefix can_do_io store kept (observed_at_sync_point) in every " \
                       "admitted unit where the MMIO load is a non-last insn"
        doc["pass"] = bool(multi) and all(
            r["prefix_cdio_kept"] and all(k.endswith("observed_at_sync_point")
                                          for k in r["prefix_cdio_kept"])
            and not r["prefix_cdio_deleted"] for r in multi)
        doc["units_checked"] = len(multi)
    elif cfg == "b4-5":
        # it eq; moveq r1,#5; ldr r2,[r3]; movs r1,#7: the r1 store before the LDST
        # is needed by the fault path (S5) and must not be deleted (the D4 shape)
        lab_units = list(ok_units)
        doc["check"] = "F-B4-5: no regs[1] store deleted in a unit holding the IT-block LDST; " \
                       "the regs[1] store is kept as observed_at_sync_point"
        doc["pass"] = bool(lab_units) and all(
            not any(d.startswith("D1:regs[1]@") for d in r["dead"])
            and any(k.startswith("D1:regs[1]@") and k.endswith("observed_at_sync_point")
                    for k in r["kept"]) for r in lab_units)
        doc["units_checked"] = len(lab_units)
    elif cfg == "b4-7":
        from scopt.formats.envlayout_v1 import find_envlayout

        lay = find_envlayout((s.runtime or {}).get("qemu_sha256"))
        hs = []
        for u in s.units():
            h = build_hostops(u, lay, s.header.code_address)
            for o in h.ops:
                if o.kind == OpKind.HOST_ABS_STORE:
                    touched = [p for p in prop.patches.get(u.uid, ())
                               if p.host_off < o.off + o.size and o.off < p.host_off +
                               len(p.old_bytes)]
                    hs.append({"uid": u.uid, "off": o.off, "text": o.text,
                               "abs_addr": hex(o.abs_addr) if o.abs_addr else None,
                               "touched_by_patch": len(touched)})
        d3 = prop.by_class()["D3"]
        st = prop.result.stats
        doc["update_mem_stores"] = hs
        doc["d3_deleted"] = d3
        doc["d3_observer_contract_unknown"] = st.get("D3:observer_contract_unknown", 0)
        doc["check"] = "F-B4-7: no D3 deletion under memory callbacks; every UPDATE_MEM host " \
                       "store untouched"
        doc["pass"] = d3 == 0 and bool(hs) and all(x["touched_by_patch"] == 0 for x in hs)
    return doc


def step_package(out: Path) -> dict[str, Any]:
    from scopt.passes.eliminate_state_sync import class_ledger_rows, package_b4, propose_b4

    st = selftest(out)
    rows = []
    for mode in MODES:
        for cfg in CONFIGS:
            sj, s, lay, ri2 = load(out, mode, cfg)
            d = out / "art" / mode / cfg
            d.mkdir(parents=True, exist_ok=True)
            dump(d / "runtime_identity.v2.json", ri2)
            t0 = time.monotonic()
            prop = propose_b4(s, layout=lay, runtime=ri2)
            row: dict[str, Any] = {
                "mode": mode, "config": cfg, "snapshot": sj["snapshot"],
                "snapshot_id": s.snapshot_id, "units": len(list(s.units())),
                "admission": {k: ri2["admission"][k] for k in
                              ("tb_start_cb_possible", "tb_start_cb_reasons",
                               "mem_get_value_outside_mem_cb")},
                "by_class": prop.by_class(), "units_edited": len(prop.patches),
                "outcome": prop.result.outcome.value, "reason": prop.result.reason,
                "stats": dict(prop.result.stats),
                "patched_units": {u: dict(Counter(p.justification.split(":", 1)[0] for p in v))
                                  for u, v in prop.patches.items()},
                "ledger": [r.to_json() for r in class_ledger_rows(prop).rows]}
            row["static"] = static_checks(cfg, s, prop)
            if prop.patches:
                r = package_b4(s, prop, d / "store", candidate_id=f"wp25int-{mode}-{cfg}",
                               layout=lay, runtime=ri2, selftest=st)
                row.update(status=r.status, pkg_reason=r.reason, artifact_id=r.artifact_id,
                           artifact_dir=r.artifact_dir)
                if r.validation is not None:
                    v = r.validation
                    row["rows"] = dict(Counter(f"{x['code']}:{x['result']}" for x in v.rows))
                    row["v12_pass"] = sum(1 for x in v.rows if x["code"] == "V12"
                                          and x["result"] == "pass")
                    row["failed"] = [x for x in v.rows if x["result"] == "fail"][:5]
                if r.artifact_dir:
                    side = json.loads((Path(r.artifact_dir) / "cache.sc.scopt.json").read_text())
                    row["loader_requirement"] = side["loader_requirement"]
            else:
                row["status"] = "no_change"
            row["seconds"] = round(time.monotonic() - t0, 1)
            rows.append(row)
            log(f"package {mode} {cfg}: {row['status']} {row['by_class']} units "
                f"{row['units_edited']} static={row['static'].get('pass')} "
                f"v12_pass={row.get('v12_pass')}")
    doc = {"rows": rows}
    dump(out / "package.json", doc)
    return doc


# ---------------------------------------------------------------------------
# dynamic
# ---------------------------------------------------------------------------

def gdb_count_symbol(run_dir: Path, out: Path, symbol: str, timeout_s: float = 300) -> dict:
    """Calls of QEMU function ``symbol`` in an untimed diag re-run of ``run_dir``."""
    from scopt.run.diag import _gdb_cmd, _drop_big, prepare_diag

    prep = prepare_diag(run_dir, out)
    cmds = ["set pagination off", "set confirm off", "set startup-with-shell off",
            "set width 0", "set breakpoint pending on",
            "handle SIGUSR1 SIGUSR2 SIGPIPE SIGALRM SIGCHLD SIGIO SIGPROF SIGWINCH "
            "nostop noprint pass",
            f"break {symbol}", "ignore 1 2000000000", "run", "info breakpoints"]
    g = _gdb_cmd(timeout_s, cmds, prep["argv"])
    p = subprocess.run(g, cwd=str(prep["cwd"]), env=prep["env"], capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, timeout=timeout_s + 60)
    (Path(prep["out"]) / "gdb.log").write_text(p.stdout + "\n--- stderr ---\n" + p.stderr)
    m = re.search(r"breakpoint already hit (\d+) time", p.stdout)
    resolved = bool(re.search(r"^1\s+breakpoint\s+keep\s+y\s+0x[0-9a-f]+", p.stdout, re.M))
    em = re.search(r"exited (?:normally|with code (\d+))", p.stdout)
    qexit = 0 if em and em.group(1) is None else (int(em.group(1), 8) if em else None)
    _drop_big(Path(prep["out"]))
    return {"symbol": symbol, "hits": (int(m.group(1)) if m else 0) if resolved else None,
            "resolved": resolved, "qemu_exit": qexit, "dir": str(prep["out"])}


def rewritten_probes(art_dir: Path, s: Any, prop_units: dict[str, int], cfg: str,
                     limit: int = 8) -> list[dict[str, Any]]:
    """Entry addresses of rewritten units that hold the fixture's kernel label (else the
    rewritten units nearest to it), checked against the artifact's bytes."""
    from scopt.formats.sc_records import read_sc

    f = read_sc(art_dir / "cache.sc")
    by_tb = {r.tb_offset: i for i, r in enumerate(f.records) if r is not None}
    base = f.header.code_address
    lab = sym(KERNEL_LABEL[cfg])
    cands = []
    for u in s.units():
        if u.uid not in prop_units:
            continue
        pcs = unit_pcs(u)
        if not pcs:
            continue
        dist = 0 if lab in pcs else min(abs(p - lab) for p in pcs)
        i = by_tb.get(u.tb_offset)
        if i is None:
            continue
        body = f.bodies[i] or b""
        changed = body[:f.records[i].tc_size] != u.code[:u.tc_size] if hasattr(u, "code") \
            else None
        cands.append((dist, pcs[0], {"what": f"unit {u.uid} @{pcs[0]:#x}", "uid": u.uid,
                                      "addr": base + f.records[i].tc_offset,
                                      "guest_pcs": [hex(p) for p in pcs],
                                      "holds_label": lab in pcs, "stores_deleted":
                                      prop_units[u.uid], "bytes_changed": changed}))
    cands.sort(key=lambda t: (t[0], t[1]))
    return [c[2] for c in cands[:limit]]


def step_dynamic(out: Path, only: list[str] | None = None) -> dict[str, Any]:
    from scopt.importer.import_cache import open_snapshot
    from scopt.run.diag import gdb_hits
    from scopt.run.oracles import dynamic_oracles

    pk = json.loads((out / "package.json").read_text())
    res = []
    for row in pk["rows"]:
        mode, cfg = row["mode"], row["config"]
        if only and f"{mode}/{cfg}" not in only and cfg not in only:
            continue
        if not row.get("artifact_dir"):
            res.append({"mode": mode, "config": cfg, "verdict": "not_run",
                        "why": f"no artifact ({row['status']})"})
            continue
        sj = snap_info(out, mode, cfg)
        art = Path(row["artifact_dir"])
        dd = out / "dyn" / mode / cfg
        if dd.exists():
            import shutil
            shutil.rmtree(dd)
        t0 = time.monotonic()
        # O2a is the icount oracle (§9.4 "with icount shift 0"). Without icount a
        # timer phase's exception sequence depends on host time (WP24 review 3), so
        # there it is reported, next to a warm-vs-warm control, and not gated.
        timer = bool(b3conf.BY_NAME[cfg].timing)
        gated = ("O1", "O2b", "O5") if (mode == "noicount" and timer) else \
            ("O1", "O2a", "O2b", "O5")
        doc = dynamic_oracles(art, [sj["base_toml"]], dd / "oracles",
                              oracles=gated, baseline=sj["snapshot"],
                              reps=REPS[mode], qemu=QINT, timeout_s=300,
                              inputs=sj["inputs"], fixture_expected=FX / "expected.py",
                              fixture_inputs=[cfg], quiet=False)
        r: dict[str, Any] = {"mode": mode, "config": cfg, "artifact_dir": str(art),
                             "gated_oracles": list(gated), "timer_phase": timer,
                             "verdict": doc["verdict"], "failures": doc["failures"],
                             "incomplete": doc["incomplete"], "by_class": row["by_class"],
                             "oracles": [{"rep": c["rep"], **{o["oracle"]: o["verdict"]
                                                              for o in c["oracles"]}}
                                         for c in doc["cases"]],
                             "runs": [c["runs"] for c in doc["cases"]]}
        qs = {c["runs"]["artifact"].get("run_dir") and
              json.loads((Path(c["runs"]["artifact"]["run_dir"]) / "run.json").read_text())
              .get("qemu_sha256") for c in doc["cases"] if c["runs"].get("artifact")}
        r["artifact_qemu_sha256"] = sorted(str(x) for x in qs)
        a0 = Path(doc["cases"][0]["runs"]["artifact"]["run_dir"])
        r0 = Path(doc["cases"][0]["runs"]["reference"]["run_dir"])
        # non-vacuity: the rewritten units of the kernel ran
        s = open_snapshot(sj["snapshot"])
        prop_units = dict(row["patched_units"])
        probes = rewritten_probes(art, s, prop_units, cfg)
        r["probes"] = [{k: (hex(v) if k == "addr" else v) for k, v in p.items()}
                       for p in probes]
        if probes:
            h = gdb_hits(a0, dd / "hits", [(p["what"], p["addr"]) for p in probes],
                         timeout_s=60 if (mode == "noicount" and timer) else 300)
            r["hits_ok"] = h["ok"]
            # non-vacuity: a rewritten unit was entered; whether the unit that holds
            # the kernel label was rewritten and entered is reported on its own
            # a count is used only from a gdb run whose QEMU exited 0 (else unknown);
            # in noicount a gdb run on a hot unit of a timer phase can livelock (every
            # stop costs host time while SysTick keeps firing) and time out
            clean = {k for k, x in enumerate(h["runs"]) if x["qemu_exit"] == 0}
            good = [t for t in h["targets"] if t["run"] in clean and t["hits"] is not None]
            r["hits"] = [{"what": t["what"], "hits": t["hits"] if t in good else None}
                         for t in h["targets"]]
            r["nonvacuous"] = any(t["hits"] > 0 for t in good)
            r["kernel_unit_rewritten_and_entered"] = any(
                t["hits"] > 0 for t in good
                if next(p for p in probes if p["what"] == t["what"])["holds_label"])
            ran: Counter[str] = Counter()
            for t in good:
                if t["hits"] > 0:
                    ran.update(next(p for p in probes if p["what"] == t["what"])
                               ["stores_deleted"])
            r["deleted_in_executed_probes"] = {k: ran.get(k, 0) for k in ("D1", "D2", "D3")}
        else:
            r["nonvacuous"] = False
        if mode == "noicount" and timer:
            ctl = {}
            for lab_, src in (("artifact_vs_warm", art), ("warm_vs_warm",
                                                          Path(sj["snapshot"]))):
                cd = dynamic_oracles(src, [sj["base_toml"]], dd / f"o2a-{lab_}",
                                     oracles=("O2a",), baseline=sj["snapshot"], reps=2,
                                     qemu=QINT, timeout_s=300, inputs=sj["inputs"],
                                     quiet=False)
                ctl[lab_] = [o["verdict"] for c in cd["cases"] for o in c["oracles"]]
            r["o2a_reported_noicount_timer"] = {
                **ctl, "note": "not gated: without icount the exception sequence of a "
                               "timer phase depends on host time; warm_vs_warm shows the "
                               "same nondeterminism"}
        if cfg == "b4-4":
            ca = gdb_count_symbol(a0, dd / "iorc-artifact", "cpu_io_recompile")
            cr = gdb_count_symbol(r0, dd / "iorc-reference", "cpu_io_recompile")
            r["io_recompile"] = {"artifact": ca, "reference": cr,
                                 "equal": ca["hits"] is not None and ca["hits"] == cr["hits"]
                                 and ca["qemu_exit"] == 0 and cr["qemu_exit"] == 0}
        if cfg == "b4-7":
            ev = []
            for c in doc["cases"]:
                pair = {}
                for side_ in ("artifact", "reference"):
                    f = Path(c["runs"][side_]["run_dir"]) / "work/run-artifacts/variable_watch" \
                        / "events.tsv"
                    pair[side_] = ("sha256:" + hashlib.sha256(f.read_bytes()).hexdigest(),
                                   f.stat().st_size) if f.is_file() else None
                ev.append(pair)
            r["events_tsv"] = ev
            r["events_equal"] = bool(ev) and all(p["artifact"] and p["reference"] and
                                                 p["artifact"] == p["reference"] and
                                                 p["artifact"][1] > 0 for p in ev)
        r["seconds"] = round(time.monotonic() - t0, 1)
        r["pass"] = (doc["verdict"] == "pass" and r["nonvacuous"]
                     and r.get("io_recompile", {}).get("equal", True)
                     and r.get("events_equal", True))
        res.append(r)
        log(f"dynamic {mode} {cfg}: oracles={doc['verdict']} nonvacuous={r['nonvacuous']} "
            f"hits={r.get('hits')} iorc={r.get('io_recompile', {}).get('equal')} "
            f"events={r.get('events_equal')} pass={r['pass']}")
        dump(out / "dynamic.partial.json", {"rows": res})
    old = json.loads((out / "dynamic.json").read_text())["rows"] if only and \
        (out / "dynamic.json").is_file() else []
    keep = [x for x in old if not any(x["mode"] == y["mode"] and x["config"] == y["config"]
                                      for y in res)]
    doc = {"rows": sorted(keep + res, key=lambda x: (x["mode"], x["config"]))}
    dump(out / "dynamic.json", doc)
    return doc


# ---------------------------------------------------------------------------
# mutants
# ---------------------------------------------------------------------------

def update_mem_mutants(s: Any, lay: Any) -> list[Any]:
    """Real F-B4-7 mutants: NOP the FastDyn UPDATE_MEM host store (``mov [r], 5``)."""
    from scopt.analysis.hostops import OpKind, build_hostops
    from scopt.analysis.syncdead import nop_bytes
    from scopt.b4_mutants import B4Mutant
    from scopt.passes.base import Patch

    out = []
    for u in s.units():
        h = build_hostops(u, lay, s.header.code_address)
        for o in h.ops:
            if o.kind == OpKind.HOST_ABS_STORE:
                old = u.code[o.off:o.off + o.size]
                p = Patch(target=u.uid, host_off=o.off, old_bytes=old,
                          new_bytes=nop_bytes(o.size), template_id="b4_nop_store",
                          justification=f"D1:update_mem:{o.text}")
                out.append(B4Mutant("update_mem_delete", u.uid, (p,),
                                    f"NOP FastDyn UPDATE_MEM host store {o.text!r}", None,
                                    "F-B4-7"))
    return out


#: (config, mutant kind, oracles that must change) for the dynamic O4 controls
O4_CONTROLS = [("b4-1", "brcond_reload"), ("b4-1", "flag_store_before_helper"),
               ("b4-1", "d1_before_exit"), ("b4-5", "it_block_d4"),
               ("b4-5", "it_block_d4_slowpath"), ("b4-7", "update_mem_delete"),
               ("b4-7", "guest_store_delete"), ("b4-7", "d3_before_plugin_call"),
               ("b4-2", "flag_store_before_ldst"), ("b4-6", "d1_before_udiv"),
               ("b4-3", "icount_immediate"), ("b4-3", "prefix_icount_store")]


def step_mutants(out: Path, o4: bool = True) -> dict[str, Any]:
    from scopt.b4_mutants import check_mutant, find_mutants

    st = selftest(out)
    rows = []
    found: dict[tuple[str, str], Any] = {}
    for cfg in CONFIGS:
        sj, s, lay, ri2 = load(out, "icount", cfg)
        ms = find_mutants(s, lay, ri2, per_kind=1)
        if cfg == "b4-7":
            ms += update_mem_mutants(s, lay)
        store = out / "mutants-store" / cfg
        store.mkdir(parents=True, exist_ok=True)
        for m in ms:
            c = check_mutant(s, m, store, layout=lay, runtime=ri2, selftest=st,
                             candidate_id=f"wp25int-mut-{cfg}-{m.kind}-{m.uid[-14:-2]}")
            rows.append({"config": cfg, **m.to_json(), "check": c.to_json()})
            found.setdefault((cfg, m.kind), (s, lay, ri2, m, sj))
            log(f"mutant {cfg} {m.kind}: {c.status} {c.reason}")
        # for the dynamic controls, the mutant must sit in the fixture's kernel unit
        # (the unit that holds its label); the static search takes the first shape
        # anywhere in the snapshot
        # The loop unit is the label-holding unit that starts at the loop top: the
        # largest start pc <= label (the other one is the fall-in from the function
        # entry, executed once). Mutants found there first; then the other units.
        lab = sym(KERNEL_LABEL[cfg])
        ku = sorted(((unit_pcs(u)[0], u.uid) for u in s.units()
                     if lab in unit_pcs(u) and unit_pcs(u)[0] <= lab
                     and getattr(u, "meta_status", "ok") == "ok"), reverse=True)
        km: list[Any] = []
        for _pc, uid in ku:
            have = {m.kind for m in km}
            km += [m for m in find_mutants(s, lay, ri2, per_kind=1, units=[uid])
                   if m.kind not in have]
            if cfg == "b4-7":
                km += [m for m in update_mem_mutants(s, lay) if m.uid == uid
                       and "update_mem_delete" not in have]
        for m in km:
            c = check_mutant(s, m, store, layout=lay, runtime=ri2, selftest=st,
                             candidate_id=f"wp25int-kmut-{cfg}-{m.kind}-{m.uid[-14:-2]}")
            rows.append({"config": cfg, "kernel_unit": True, **m.to_json(),
                         "check": c.to_json()})
            found[(cfg, m.kind)] = (s, lay, ri2, m, sj)
            log(f"kernel mutant {cfg} {m.kind}: {c.status} {c.reason}")
    rej = sum(1 for r in rows if r["check"]["rejected"])
    doc: dict[str, Any] = {"static": {"total": len(rows), "rejected": rej, "rows": rows,
                                      "kinds": sorted({r["kind"] for r in rows})}}
    if o4:
        doc["o4"] = step_o4(out, found)
    dump(out / "mutants.json", doc)
    return doc


def step_o4(out: Path, found: dict) -> list[dict[str, Any]]:
    from scopt.passes.eliminate_state_sync import plan_of
    from scopt.run.negative import package_negative
    from scopt.run.oracles import dynamic_oracles

    res = []
    root = out / "negative"
    for cfg, kind in O4_CONTROLS:
        hit = found.get((cfg, kind))
        if hit is None:
            res.append({"config": cfg, "kind": kind, "verdict": "not_run",
                        "why": "mutant shape not found in this snapshot"})
            continue
        s, lay, ri2, m, sj = hit
        label = f"{cfg}-{kind}"
        if (root / label).exists():
            import shutil
            shutil.rmtree(root / label)
        ri = dict(m.runtime) if m.runtime is not None else ri2
        nr = package_negative(s, plan_of({m.uid: m.patches}), root, label=label,
                              mutant=kind, detector=m.detector or cfg, layout=lay, runtime=ri)
        lab = sym(KERNEL_LABEL[cfg])
        row: dict[str, Any] = {"config": cfg, "kind": kind, "uid": m.uid,
                               "unit_holds_kernel_label": any(
                                   lab in unit_pcs(u) for u in s.units() if u.uid == m.uid),
                               "patches": [p.to_json() for p in m.patches],
                               "package": getattr(nr, "status", None),
                               "package_reason": getattr(nr, "reason", None)}
        if getattr(nr, "path", None) is None:
            row["verdict"] = "not_run"
            res.append(row)
            continue
        dd = out / "dyn-o4" / label
        if dd.exists():
            import shutil
            shutil.rmtree(dd)
        doc = dynamic_oracles(nr.path, [sj["base_toml"]], dd, oracles=("O1", "O2b"),
                              baseline=sj["snapshot"], reps=1, qemu=QINT, timeout_s=300,
                              inputs=sj["inputs"], fixture_expected=FX / "expected.py",
                              fixture_inputs=[cfg], allow_negative_control=True, quiet=False)
        row["verdict"] = doc["verdict"]  # pass = mutant detected
        if cfg == "b4-7":
            ev = []
            for c in doc["cases"]:
                pair = {}
                for side_ in ("artifact", "reference"):
                    rd = c["runs"].get(side_, {}).get("run_dir")
                    f = Path(rd) / "work/run-artifacts/variable_watch/events.tsv" if rd \
                        else None
                    pair[side_] = ("sha256:" + hashlib.sha256(f.read_bytes()).hexdigest()
                                   if f is not None and f.is_file() else None)
                ev.append(pair)
            row["events_tsv"] = ev
            row["detected_by_events_tsv"] = any(
                p["artifact"] and p["reference"] and p["artifact"] != p["reference"]
                for p in ev)
        row["detail"] = [{o["oracle"]: (o["verdict"], str(o.get("detail"))[:160])
                          for o in c["oracles"]} for c in doc["cases"]]
        res.append(row)
        log(f"O4 {label}: {doc['verdict']}")
    return res


# ---------------------------------------------------------------------------
# compute V10 (held-out, icount)
# ---------------------------------------------------------------------------

def step_compute(out: Path) -> dict[str, Any]:
    arts = []
    for f in ("package.json", "ablation.json"):
        for a in json.loads((WP25 / f).read_text())["artifacts"]:
            if a["snapshot"] == "compute" and a.get("artifact_dir"):
                arts.append(("b4-" + "-".join(a["classes"]) if f == "ablation.json"
                             else "b4-all", a["artifact_dir"], a["by_class"]))
    sfb = json.loads((WP25 / "sfb.json").read_text())
    for k, v in (sfb.items() if isinstance(sfb, dict) else []):
        if isinstance(v, dict) and k == "compute" and v.get("artifact_dir"):
            arts.append(("sfb", v["artifact_dir"], None))
    rows = []
    for label, adir, bc in arts:
        dd = out / "compute-v10" / label
        if dd.exists():
            import shutil
            shutil.rmtree(dd)
        cmd = [PY, "-m", "scopt", "validate", adir, "--dynamic", "-c", str(COMPUTE_TOML),
               "--out", str(dd), "--qemu", str(QINT), "--timeout", "300", "--json",
               "--baseline", str(COMPUTE_SNAP),
               *sum((["--inputs", str(i)] for i in HELDOUT), [])]
        t0 = time.monotonic()
        p = subprocess.run(cmd, cwd=str(WT), capture_output=True, text=True)
        (out / "compute-v10").mkdir(parents=True, exist_ok=True)
        (out / "compute-v10" / f"{label}.log").write_text(p.stdout + "\n--- stderr ---\n" +
                                                         p.stderr)
        try:
            d = json.loads(p.stdout)
        except ValueError:
            d = {}
        rows.append({"label": label, "artifact_dir": adir, "by_class": bc, "rc": p.returncode,
                     "verdict": d.get("verdict"), "appended": d.get("appended"),
                     "failures": d.get("failures"), "seconds": round(time.monotonic() - t0, 1),
                     "pairs": [{k: x.get(k) for k in ("input", "rep", "verdict", "equal",
                                                      "artifact", "baseline", "differences")
                                if k in x} for x in (d.get("runs") or [])]})
        log(f"V10 {label}: rc={p.returncode} verdict={d.get('verdict')}")
    doc = {"config": str(COMPUTE_TOML), "inputs": [str(i) for i in HELDOUT], "qemu": str(QINT),
           "rows": rows}
    dump(out / "compute-v10.json", doc)
    return doc


def _sha(p: Path) -> str:
    return "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()


def step_verdict(out: Path) -> dict[str, Any]:
    def rd(name: str) -> Any:
        f = out / name
        return json.loads(f.read_text()) if f.is_file() else None

    pk, dy, mu, cv = rd("package.json"), rd("dynamic.json"), rd("mutants.json"), \
        rd("compute-v10.json")
    tm = rd("timing-analysis.json")
    lf = out / "timing-load.log"
    if tm is not None and lf.is_file():
        lines = lf.read_text().splitlines()
        loads = [float(x.split()[1]) for x in lines if re.match(r"^\d+ ", x)]
        cj = rd("timing/campaign.json") or {}
        tm = {"reported_outcome": True, **tm, "cpu_budget": {
            "cores": "taskset 8-11 (4 cores)", "lock": "flock runs/.timing.lock",
            "load1_min": min(loads) if loads else None,
            "load1_max": max(loads) if loads else None,
            "load_samples": len(loads),
            "start": next((x for x in lines if x.startswith("start ")), None),
            "lock_acquired": next((x for x in lines if x.startswith("lock acquired")), None),
            "end": next((x for x in lines if x.startswith("end ")), None),
            "qemu": str(QINT), "campaign_seed": (cj.get("campaign") or {}).get("seed"),
            "qemu_seen": cj.get("qemu_seen")}}
    fx = []
    for mode in MODES:
        for cfg in CONFIGS:
            p = next((r for r in (pk or {}).get("rows", []) if r["mode"] == mode
                      and r["config"] == cfg), None)
            d = next((r for r in (dy or {}).get("rows", []) if r["mode"] == mode
                      and r["config"] == cfg), None)
            st = (p or {}).get("static", {})
            ok = bool(p and d and p.get("status") == "published" and d.get("pass")
                      and st.get("pass", True))
            fx.append({"mode": mode, "config": cfg,
                       "fixture": b3conf.BY_NAME[cfg].fixture,
                       "published": None if p is None else p.get("status"),
                       "by_class": None if p is None else p.get("by_class"),
                       "v12_pass_units": None if p is None else p.get("v12_pass"),
                       "static_check": st.get("check"), "static_pass": st.get("pass"),
                       "gated_oracles": None if d is None else d.get("gated_oracles"),
                       "oracles_verdict": None if d is None else d.get("verdict"),
                       "per_rep": None if d is None else d.get("oracles"),
                       "nonvacuous": None if d is None else d.get("nonvacuous"),
                       "deleted_in_executed_probes": None if d is None else
                       d.get("deleted_in_executed_probes"),
                       "io_recompile": None if d is None else
                       {k: (v.get("hits") if isinstance(v, dict) else v)
                        for k, v in (d.get("io_recompile") or {}).items()} or None,
                       "events_tsv_equal": None if d is None else d.get("events_equal"),
                       "o2a_noicount_timer_reported": None if d is None else
                       d.get("o2a_reported_noicount_timer"),
                       "pass": ok})
    qs = sorted({s for r in (dy or {}).get("rows", []) for s in r.get("artifact_qemu_sha256", [])})
    comp = [{k: r.get(k) for k in ("label", "verdict", "rc", "appended", "failures",
                                   "by_class")} for r in (cv or {}).get("rows", [])]
    b4all = next((r for r in comp if r["label"] == "b4-all"), None)
    o4 = (mu or {}).get("o4") or []
    doc = {
        "schema": "wp25-int/verdict/1",
        "qemu": {"path": str(QINT), "sha256": _sha(QINT), "fixture_runs_qemu_sha256": qs},
        "fixture_elf_sha256": _sha(FX / "b3.elf"),
        "acceptance_integration": {
            "F-B4-1..F-B4-7 O1/O2a/O2b on the current binary": {
                "pass": bool(fx) and all(r["pass"] for r in fx), "rows": fx},
            "compute B4 artifact V10-equal on held-out (icount)": {
                "pass": bool(b4all and b4all["verdict"] == "pass"), "rows": comp},
            "early paired timing point (>= 30 pairs, train), reported outcome":
                tm if tm is not None else {"status": "not_run"},
        },
        "mutants": None if mu is None else {
            "static_total": mu["static"]["total"], "static_rejected": mu["static"]["rejected"],
            "kinds": mu["static"]["kinds"],
            "o4": [{k: r.get(k) for k in ("config", "kind", "uid", "unit_holds_kernel_label",
                                          "verdict", "detected_by_events_tsv")}
                   for r in o4]},
    }
    rc = out / "recheck-main84f9fe3"
    if rc.is_dir():
        a = {(r["mode"], r["config"]): r for r in (pk or {}).get("rows", [])}
        b = json.loads((rc / "package.json").read_text())["rows"] \
            if (rc / "package.json").is_file() else []
        same = []
        for r in b:
            o = a.get((r["mode"], r["config"]))
            same.append(bool(o and r.get("artifact_dir") and o.get("artifact_dir") and
                             _sha(Path(r["artifact_dir"]) / "cache.sc") ==
                             _sha(Path(o["artifact_dir"]) / "cache.sc")))
        cj = rc / "compute" / "recheck.json"
        doc["recheck_after_merging_main_84f9fe3"] = {
            "fixtures_republished": sum(1 for r in b if r.get("status") == "published"),
            "fixtures_cache_sc_identical": sum(same), "fixtures": len(b),
            "compute": json.loads(cj.read_text())["rows"] if cj.is_file() else None}
    dump(out / "verdict.json", doc)
    print(json.dumps({k: v.get("pass") if isinstance(v, dict) else v
                      for k, v in doc["acceptance_integration"].items()}, indent=1))
    return doc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, default=ROOT / "runs/wp25-int")
    ap.add_argument("--only", default="", help="dynamic: comma list of cfg or mode/cfg")
    ap.add_argument("--no-o4", action="store_true")
    ap.add_argument("steps", nargs="*",
                    default=["package", "dynamic", "mutants", "compute", "verdict"])
    a = ap.parse_args(argv)
    out = a.out.resolve()
    for st in a.steps:
        log(f"step {st}")
        if st == "package":
            step_package(out)
        elif st == "dynamic":
            step_dynamic(out, [x for x in a.only.split(",") if x] or None)
        elif st == "mutants":
            step_mutants(out, o4=not a.no_o4)
        elif st == "compute":
            step_compute(out)
        elif st == "verdict":
            step_verdict(out)
        else:
            raise SystemExit(f"unknown step {st}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
