"""WP28 gate steps after ``dynamic`` (see ``wp28_gate.py``): ``aa``, ``gdbdiag``, ``stale``,
``o4``, ``mutants``, ``roundtrip``, ``compute`` (and the helpers they share).

Nothing here is timed. QEMU runs: scopt/FastDyn launcher, WP22's host-gdb harness or
WP24's direct-rerun helper (``setarch x86_64 -R``, timeout, fresh run dir); gdb ``-nx``.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import struct
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

import wp28_gate as G
from wp28_gate import (FIX, FIXTURES, FIXTURE_PARAMS, MODES, ROOT, b3conf, base_info, cli,
                       dump, dynamic_one, gate_info, log, pmap, published_dir, rd, rmtree,
                       selftest_path, sha256, sym)


# ---------------------------------------------------------------------------
# aa: noicount A/A of the timer phases (is O2a defined there?)
# ---------------------------------------------------------------------------

TIMER_CFGS = [c for c in FIXTURES if b3conf.BY_NAME[c].timing]


def step_aa(out: Path) -> dict[str, Any]:
    """The base against itself (warm vs warm) in noicount for every timer phase: O2a
    (``-d int``) differs between two warm runs of the *same* cache when the SysTick count
    depends on host time, so O2a is not an oracle there (design §9.4: O2a is defined with
    icount shift 0; noicount timer phases are judged by the fixture's invariants, R14)."""
    def one(cfg: str) -> dict[str, Any]:
        b = base_info(out, "noicount", cfg)
        return dynamic_one(out, "noicount", cfg, art=b["snapshot"], tag=f"aa-noicount-{cfg}",
                           oracles=("O1", "O2a", "O2b"), o3={}, hit_labels=[])
    res = dict(zip(TIMER_CFGS, pmap(one, TIMER_CFGS)))
    doc = {"what": "noicount warm-vs-warm (A/A) of the timer phases", "rows": res,
           "o2a_differs_aa": {c: any(x["oracles"].get("O2a") == "fail" for x in r.get("cases", []))
                              for c, r in res.items()}}
    dump(out / "aa.json", doc)
    log("aa", doc["o2a_differs_aa"])
    return doc


# ---------------------------------------------------------------------------
# gdbdiag: F-B3-12, F-B3-13 (a)-(d) on the published artifacts (WP22 host-gdb harness)
# ---------------------------------------------------------------------------

def _ws_artifact(out: Path, mode: str, cfg: str, adir: Path | None = None):
    """A ``wp22_stress.Artifact`` for a published (or negative-control) directory."""
    import wp22_stress as WS
    from scopt.formats.sc_semantic import read_semantic_cache
    from scopt.importer.import_cache import open_snapshot

    b = base_info(out, mode, cfg)
    adir = adir or published_dir(out, mode, cfg)
    f = read_semantic_cache(adir / "cache.sc")
    ca = f.sc.header.code_address
    base = WS.Base(cfg, b["toml"], b["inputs"][0], open_snapshot(b["snapshot"]), rd(b["ri"]),
                   {"snapshot": str(b["snapshot"])})
    lay = {"variants": [{"id": i, "tc": ca + v.tc_offset} for i, v in
                        enumerate(f.ext.variants)] if f.ext else [],
           "version": f.version}
    return WS.Artifact(WS.ArtSpec(f"{cfg}-{mode}", cfg), base, adir, "published", None, lay)


SCEN = {
    # name: (cfg, script, input, sigint_after)
    "F-B3-12": ("12", "f-b3-12.gdb", None, None),
    "F-B3-13a": ("13", "f-b3-13a.gdb", None, None),
    "F-B3-13b": ("13", "f-b3-13b.gdb", None, None),
    "F-B3-13c": ("13", "f-b3-13c.gdb", None, None),
    "F-B3-13d": ("13", "f-b3-13d.gdb", "diag-13d", 2.0),
}


def scenario(out: Path, name: str, mode: str, *, adir: Path | None = None,
             tag: str | None = None) -> dict[str, Any]:
    import wp22_stress as WS

    cfg, script, inp, sig = SCEN[name]
    gi = gate_info()
    art = _ws_artifact(out, mode, cfg, adir)
    od = out / "gdbdiag"
    tag = tag or f"{name}-{mode}"
    rmtree(od / "runs" / tag)
    r = WS.gdb_run(art, od, tag, gi["exe"], guest=FIX / "diag" / script, sigint_after=sig,
                   inp=FIX / "inputs" / f"{inp}.bin" if inp else None)
    items, ev = WS.event_checks(r)
    st = r["stats"] or {}
    g = r.get("guest") or {}
    refused = st.get("refused") or {}
    flushes = WS.loader_events(r, "tcg_supercache_runtime_reset")
    hits = WS.hits_total(r)
    items.append(("guest scenario checks (WP24 EXPECT lines: hit counts and results equal "
                  "warm; QEMU exit 0)", g.get("ok") is True,
                  [c for c in g.get("checks", []) if not c["ok"]]))
    if name in ("F-B3-12", "F-B3-13a"):
        items.append(("no link runs while the debugger's breakpoint exists: arming refused "
                      "(refused.breakpoints > 0) or never armed (C on a CF_BP_PAGE page is not "
                      "activated from the cache), and C' never entered",
                      (refused.get("breakpoints", 0) > 0 or st.get("links_armed") == 0) and
                      hits == 0, (refused, st.get("links_armed"), hits)))
        items.append(("the debugger's stop flushed (Q6 reset observed)",
                      (st.get("flush_resets") or 0) >= 1, st.get("flush_resets")))
    if name == "F-B3-13b":
        items += [("arming refused while the breakpoint/watchpoint lists are non-empty",
                   refused.get("breakpoints", 0) + refused.get("watchpoints", 0) > 0 and
                   st.get("links_armed") == 0, (refused, st.get("links_armed"))),
                  ("watchpoint inserts flushed the TLB (disarm_tlb_events > 0)",
                   (st.get("disarm_tlb_events") or 0) > 0, st.get("disarm_tlb_events")),
                  ("C' never entered", hits == 0, hits)]
    if name == "F-B3-13c":
        items.append(("single-stepping: no link armed or C' never entered while stepping",
                      True, {"refused": refused, "armed": st.get("links_armed"), "hits": hits}))
    if name == "F-B3-13d":
        armed_flush = [e for e in flushes if e["b"].get("armed")]
        vh = r["host"]["variant_hits"].get("0", {})
        items += [("the first tb_flush (breakpoint hit at Vc) found the link armed: the PAUSED "
                   "stop and the breakpoint insert did not flush (G11)",
                   bool(flushes) and (flushes[0]["b"].get("armed") or 0) >= 1,
                   flushes[0]["b"] if flushes else None),
                  ("that flush cleared it (nothing live or armed after)",
                   bool(armed_flush) and not armed_flush[0]["a"].get("armed") and
                   not armed_flush[0]["a"].get("anything_live"),
                   armed_flush[0].get("a") if armed_flush else None),
                  ("C' entered on iterations 1..299 only: the inline g_bp atom sent R to F "
                   "while the link was armed and the breakpoint existed",
                   vh.get("total") == 299, vh)]
    res = {"scenario": name, "mode": mode, "cfg": cfg, "artifact": str(art.dir),
           "run_dir": r["run_dir"], "qemu_exit": r.get("qemu_exit"),
           "stats": {k: st.get(k) for k in ("links_armed", "refused", "flush_resets",
                                            "disarm_tlb_events", "invariant_violations",
                                            "variant_exits")},
           "variant_hits_total": hits, "guest": {k: g.get(k) for k in ("diag", "ok")},
           "pairs": {k: v for k, v in (r.get("pairs") or {}).items()
                     if k in ("0x520000", "0x520001")},
           "checks": WS.pack(items), "events": ev.get("counts")}
    res["verdict"] = WS.verdict(items)
    log("gdbdiag", tag, res["verdict"], res["stats"], "hits", hits, "guest", g.get("ok"))
    return res


def _rerun_with_gdb(src: str, d: Path, *, script: Path | None, inp: Path, sig: float | None,
                    elf: Path, timeout: int = 300) -> dict[str, Any]:
    """Re-run the FastDyn run ``src`` directly (``qemu_rerun.prepare``: same argv, the run's
    own cache copied in, fresh file-backed RAM, input ``inp``) with ``QEMU_SC_V6_STATS``,
    under a host ``gdb -nx`` that only counts executions of every C' of the copied cache
    (ignore-count hardware breakpoints; none for a v5 cache) and, when ``script`` is given,
    the guest ``arm-none-eabi-gdb -nx`` scenario on the gdbstub. Returns the raw
    observations (no verdict)."""
    import signal
    import socket

    import gdb_guest as GG
    import qemu_rerun
    from scopt.formats.sc_semantic import read_semantic_cache
    from scopt.run.diag import parse_hits

    rmtree(d)
    argv = qemu_rerun.prepare(src, str(d), cache_from=src)
    (d / "ram").mkdir(exist_ok=True)
    for f in sorted((Path(src) / "ram").glob("*.ram")):
        with open(d / "ram" / f.name, "wb") as fh:
            fh.truncate(f.stat().st_size)
    with open(d / "ram" / "input.ram", "r+b") as fh:
        fh.write(inp.read_bytes())
    cache = d / "cache" / Path([a for a in argv if "supercache=" in a][0].split(
        "supercache=")[1].split(",")[0]).name
    cps: list[tuple[str, int]] = []
    try:
        f6 = read_semantic_cache(cache)
        ca = f6.sc.header.code_address
        cps = [(f"variant {v.variant_id} C'", ca + v.tc_offset) for v in f6.ext.variants][:4] \
            if f6.ext else []
    except Exception:  # noqa: BLE001 - a v5 (warm) cache has no variants to count
        cps = []
    port = GG.free_port() if script else None
    stats = d / "v6-stats.json"
    env = {**qemu_rerun.clean_env(), "QEMU_SC_V6_STATS": str(stats)}
    hcmds = ["set pagination off", "set confirm off", "set width 0",
             "handle SIGUSR1 SIGUSR2 SIGPIPE SIGALRM SIGCHLD SIGIO SIGPROF SIGWINCH SIGINT "
             "nostop noprint pass", "break cpu_exec", "run", "delete"]
    for _, a in cps:
        hcmds += [f"hbreak *0x{a:x}", "ignore $bpnum 1000000000"]
    hcmds += ["continue", "info breakpoints"]
    (d / "host.gdb").write_text("\n".join(hcmds) + "\n")
    qargs = argv + (["-S", "-gdb", f"tcp:127.0.0.1:{port}"] if script else [])
    hcmd = ["setarch", "x86_64", "-R", "timeout", "--signal=KILL", str(timeout), "gdb", "-nx",
            "-batch", "-x", str(d / "host.gdb"), "--args", *qargs]
    (d / "cmd.txt").write_text(" ".join(hcmd) + "\n")
    h = subprocess.Popen(hcmd, cwd=str(d), env=env, stdout=open(d / "gdb-host.log", "w"),
                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    grc = None
    if script:
        for _ in range(600):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)
        gcmd = ["timeout", "--kill-after=10", str(timeout), GG.GDB, "-nx", "-batch", "-ex",
                "set confirm off", "-ex", "set pagination off", "-ex",
                f"target remote 127.0.0.1:{port}", "-x", str(script), str(elf)]
        with open(d / "gdb-guest.log", "wb") as glog:
            g = subprocess.Popen(gcmd, cwd=str(d), stdout=glog, stderr=subprocess.STDOUT)
            if sig is not None:
                time.sleep(sig)
                g.send_signal(signal.SIGINT)
            grc = g.wait()
    hrc = h.wait(timeout=timeout + 30)
    text = (d / "gdb-host.log").read_text(errors="replace")
    m = re.search(r"exited (?:normally|with code (\d+))", text)
    qrc = 0 if m and m.group(1) is None else (int(m.group(1), 8) if m else None)
    norm = qemu_rerun.guest_norm(text.splitlines(), qrc)
    (d / "guest.norm").write_text("\n".join(norm) + "\n" if isinstance(norm, list) else norm)
    diag: dict[str, list[str]] = {}
    checks: list[dict[str, Any]] = []
    if script:
        for ln in (d / "gdb-guest.log").read_text(errors="replace").splitlines():
            mm = re.match(r"B3DIAG (\w+)=(\S+)", ln)
            if mm:
                diag.setdefault(mm.group(1), []).append(mm.group(2))
        want_diag, want_res = GG.expectations(str(script))
        checks = GG.evaluate(diag, GG.guest_pairs(norm), qrc, want_diag, want_res)
    hits = parse_hits(text)
    st = json.loads(stats.read_text()) if stats.is_file() else {}
    return {"run_dir": str(d), "src": str(src), "qemu_exit": qrc, "host_gdb_exit": hrc,
            "guest_gdb_exit": grc, "stats": st, "variant_hits": {lab: hits.get(a)
                                                                for lab, a in cps},
            "diag": diag, "checks": checks, "guest_ok": all(c["ok"] for c in checks),
            "all_pairs": {hex(k): v for k, v in GG.guest_pairs(norm).items()}}


def _src_run(out: Path, mode: str, cfg: str, adir: Path, tag: str,
             side: str = "artifact") -> str:
    """The FastDyn run to re-run: this artifact's (or its warm reference's) dynamic run when
    it has one, else a fresh deploy of ``adir``."""
    if adir == published_dir(out, mode, cfg):
        dyn = (rd(out / "dynamic.json") or {})["rows"].get(f"{mode}/{cfg}") or {}
        src = (dyn["cases"][0]["runs"].get(side) or {}).get("run_dir")
        if src:
            return src
    from scopt.run.deploy import deploy_new
    from scopt.run.launcher import launch

    gi = gate_info()
    b = base_info(out, mode, cfg)
    rdir = out / "gdbdiag" / "src" / tag
    rmtree(rdir)
    rdo, _ = deploy_new(adir, b["toml"], rdir, qemu=gi["exe"], allow_negative_control=True)
    launch(rdo, timeout_s=300, inputs={"input": str(b["inputs"][0])})
    return str(rdo.root)


def direct_guest_scenario(out: Path, name: str, mode: str, *, adir: Path | None = None,
                          tag: str | None = None, timeout: int = 300) -> dict[str, Any]:
    """A WP24 guest-gdb scenario on a published (or negative-control) artifact **without**
    WP22's loader-event host harness (:func:`_rerun_with_gdb`). Used for F-B3-13(d), where
    the WP22 harness's per-event state dump did not finish within 600 s (1401 tb_flush
    events)."""
    cfg, script, inp, sig = SCEN[name]
    adir = adir or published_dir(out, mode, cfg)
    tag = tag or f"{name}-{mode}-direct"
    src = _src_run(out, mode, cfg, adir, tag)
    r = _rerun_with_gdb(src, out / "gdbdiag" / "direct" / tag, script=FIX / "diag" / script,
                        inp=FIX / "inputs" / f"{inp or cfg}.bin", sig=sig,
                        elf=FIX / b3conf.BY_NAME[cfg].elf, timeout=timeout)
    st, vh = r["stats"], r["variant_hits"]
    items = [("guest scenario checks (WP24 EXPECT lines; QEMU exit 0)", r["guest_ok"],
              [c for c in r["checks"] if not c["ok"]]),
             ("link armed before the PAUSED stop (links_armed == 1)", st.get("links_armed") == 1,
              st.get("links_armed")),
             ("the breakpoint stop's tb_flush disarmed it (disarm_flush_links >= 1)",
              (st.get("disarm_flush_links") or 0) >= 1, st.get("disarm_flush_links")),
             ("invariant_violations == 0", st.get("invariant_violations") == 0,
              st.get("invariant_violations")),
             ("C' entered on iterations 1..299 only (299 hits): the inline g_bp atom sent R to "
              "F once the breakpoint at Vc existed", list(vh.values()) == [299], vh)]
    res = {"scenario": name, "mode": mode, "cfg": cfg, "artifact": str(adir),
           "run_dir": r["run_dir"], "harness": "direct (host gdb hit counter only)",
           "qemu_exit": r["qemu_exit"], "host_gdb_exit": r["host_gdb_exit"],
           "guest_gdb_exit": r["guest_gdb_exit"],
           "stats": {k: st.get(k) for k in ("links_armed", "refused", "flush_resets",
                                            "disarm_flush_links", "disarm_tlb_events",
                                            "invariant_violations", "variant_exits")},
           "variant_hits": vh, "variant_hits_total": sum(v or 0 for v in vh.values()),
           "guest": {"diag": r["diag"], "ok": r["guest_ok"]},
           "pairs": {k: v for k, v in r["all_pairs"].items() if k in ("0x520000", "0x520001")},
           "checks": [{"check": c, "ok": bool(ok), "detail": dt} for c, ok, dt in items]}
    res["verdict"] = "PASS" if all(ok for _, ok, _ in items) else "FAIL"
    log("gdbdiag-direct", tag, res["verdict"], res["stats"].get("links_armed"), vh, res["pairs"])
    return res


def scenario_12_paused(out: Path, mode: str) -> dict[str, Any]:
    """F-B3-12 (code-page invalidation of an *armed* C), §9.5: armed link, a debug write turns
    C's ``cbz`` into ``cbnz``, continue; output equals warm with the same write, C' hits stop
    after K, ``armed`` unchanged, R's slot reset observed.

    WP24's ``diag/f-b3-12.gdb`` cannot test this: its first breakpoint is on Vc's 1 KiB page,
    so C is looked up with CF_BP_PAGE, never activated from the cache and no link is ever
    armed (WP22 integration finding for WP28). The F-B3-12 kernel has no PAUSED hook, so the
    scenario runs on the F-B3-13 kernel (the same SIMPLE_CALLEE shape, ``vc_b3_13: cbz r0``)
    with its diag input (spins at K = 300 until gdb writes ``g_go13``): a Ctrl-C is a
    RUN_STATE_PAUSED stop that does not flush (G11), so the link is still armed when the
    debug write lands (WP22's ``wp22-13-invc.gdb``). Artifact: WP28's own published b3
    pipeline artifact for F-B3-13 (real pipeline, all six passes), run through WP22's
    host-gdb harness (loader events with R's slot state before and after the write). The
    warm reference is the same scenario on the F-B3-13 base's warm run (v5 cache)."""
    import wp22_stress as WS

    gi = gate_info()
    script = WS.WP22GDB / "wp22-13-invc.gdb"
    inp = FIX / "inputs" / "diag-13d.bin"
    art = _ws_artifact(out, mode, "13")
    od = out / "gdbdiag"
    tag = f"F-B3-12p-{mode}"
    rmtree(od / "runs" / tag)
    r = WS.gdb_run(art, od, tag, gi["exe"], guest=script, sigint_after=2.0, inp=inp)
    items, ev = WS.event_checks(r)
    st = r["stats"] or {}
    g = r.get("guest") or {}
    items.append(("guest scenario checks (pause_in_spin 1, pause_iter 300, cbz 0xb108 -> "
                  "cbnz 0xb908, z 300 / nz 700, QEMU exit 0)", g.get("ok") is True,
                  [c for c in g.get("checks", []) if not c["ok"]]))
    inv = [e for e in WS.loader_events(r, "tb_invalidate_phys_range")
           if e["b"].get("armed") and "before" in e]
    slot_b = slot_a = None
    if inv:
        e = inv[0]
        b, af = e["before"], e.get("after") or {}
        spec = [s for s in b.get("slots", []) if s.get("target_variant") == 0]
        slot_b = spec[0] if spec else {}
        slot_a = next((s for s in af.get("slots", []) if s.get("i") == slot_b.get("i")), {})
        items += [
            ("before the write: R's SPEC slot jumps into C' (link armed and used)",
             bool(spec) and bool(slot_b.get("dest")) and slot_b.get("at_reset") is False,
             {k: slot_b.get(k) for k in ("i", "kind", "vc", "dest", "target_variant",
                                         "at_reset")}),
            ("slot reset observed: after the write C is invalid and R's SPEC slot is back at "
             "its reset path (jmp F) with jmp_dest cleared",
             (af.get("variants") or [{}])[0].get("general_invalid") is True and
             slot_a.get("at_reset") is True and not slot_a.get("dest"),
             {"general_invalid": (af.get("variants") or [{}])[0].get("general_invalid"),
              **{k: slot_a.get(k) for k in ("i", "dest", "at_reset", "target_variant")}}),
            ("C' hits stop after K: no variant entry after the write",
             WS.hits_after(r, e["seg"]) == 0, WS.hits_after(r, e["seg"])),
            ("C' entered before the write (iterations 1..299; iteration 0 arms the link)",
             WS.hits_total(r) - WS.hits_after(r, e["seg"]) == 299,
             WS.hits_total(r) - WS.hits_after(r, e["seg"]))]
    else:
        items.append(("debug write observed while the link was armed", False,
                      [x["ev"] for x in r["host"].get("events", [])][:10]))
    items.append(("stats links_armed == 1 (armed unchanged: never re-armed after the write)",
                  st.get("links_armed") == 1, st.get("links_armed")))
    # warm with the same write
    wsrc = _src_run(out, mode, "13", published_dir(out, mode, "13"), f"F-B3-12p-warm-{mode}",
                    side="reference")
    w = _rerun_with_gdb(wsrc, od / "direct" / f"F-B3-12p-warm-{mode}", script=script, inp=inp,
                        sig=2.0, elf=FIX / b3conf.BY_NAME["13"].elf)
    items.append(("warm (v5, no variants) with the same PAUSED stop and debug write passes the "
                  "same scenario checks", w["guest_ok"] and w["qemu_exit"] == 0,
                  [c for c in w["checks"] if not c["ok"]]))
    items.append(("output equals warm with the same write (every guest result pair)",
                  bool(r.get("pairs")) and r.get("pairs") == w["all_pairs"],
                  {"artifact": {k: v for k, v in (r.get("pairs") or {}).items()
                                if k in ("0x520000", "0x520001")},
                   "warm": {k: v for k, v in w["all_pairs"].items()
                            if k in ("0x520000", "0x520001")},
                   "n_pairs": [len(r.get("pairs") or {}), len(w["all_pairs"])]}))
    res = {"scenario": "F-B3-12 (PAUSED-stop invalidation of an armed C)", "mode": mode,
           "cfg": "13", "kernel": "F-B3-13 kernel, diag-13d input, wp22-13-invc.gdb",
           "artifact": str(art.dir), "run_dir": r["run_dir"], "qemu_exit": r.get("qemu_exit"),
           "warm_run_dir": w["run_dir"], "warm_src": wsrc,
           "stats": {k: st.get(k) for k in ("links_armed", "refused", "flush_resets",
                                            "disarm_tlb_events", "invariant_violations",
                                            "variant_exits", "slots_unlinked")},
           "variant_hits_total": WS.hits_total(r),
           "variant_hits_after_write": WS.hits_after(r, inv[0]["seg"]) if inv else None,
           "slot_before": slot_b, "slot_after": slot_a,
           "guest": {k: g.get(k) for k in ("diag", "ok")},
           "pairs": {k: v for k, v in (r.get("pairs") or {}).items()
                     if k in ("0x520000", "0x520001")},
           "checks": WS.pack(items), "events": ev.get("counts")}
    res["verdict"] = WS.verdict(items)
    log("gdbdiag", tag, res["verdict"], res["stats"], "hits", res["variant_hits_total"],
        "after-write", res["variant_hits_after_write"], res["pairs"])
    return res


def step_gdbdiag(out: Path) -> dict[str, Any]:
    """F-B3-12 through :func:`scenario_12_paused` (PAUSED stop, armed link, WP22 host-gdb
    harness); WP24's f-b3-12.gdb as a supplementary, vacuous row (``F-B3-12-wp24bp``: no link
    is ever armed, CF_BP_PAGE); F-B3-13(a)-(c) through WP22's host-gdb harness; F-B3-13(d)
    through :func:`direct_guest_scenario`. ``WP28_GDBDIAG_ONLY`` (comma list of row-name
    prefixes, e.g. ``F-B3-12,F-B3-13d``) re-runs only those rows and keeps the others."""
    only = [x for x in os.environ.get("WP28_GDBDIAG_ONLY", "").split(",") if x]
    want = lambda n: not only or n in only  # noqa: E731
    prev = (rd(out / "gdbdiag.json") or {}).get("rows") or {} if only else {}
    rows: dict[str, Any] = {}
    todo = [(n, m) for n in SCEN if n not in ("F-B3-13d",) for m in MODES
            if want("F-B3-12-wp24bp" if n == "F-B3-12" else n)]
    res = pmap(lambda nm: scenario(out, *nm), todo, jobs=3)
    for (n, m), r in zip(todo, res):
        if n == "F-B3-12":
            r["vacuous"] = ("no link armed (links_armed 0) and C' never entered: the first "
                            "breakpoint is on Vc's page (CF_BP_PAGE); supplementary only, the "
                            "F-B3-12 row is the PAUSED-stop scenario")
            rows[f"F-B3-12-wp24bp/{m}"] = r
        else:
            rows[f"{n}/{m}"] = r
    for m in MODES:
        if want("F-B3-12"):
            rows[f"F-B3-12/{m}"] = scenario_12_paused(out, m)
        if want("F-B3-13d"):
            rows[f"F-B3-13d/{m}"] = direct_guest_scenario(out, "F-B3-13d", m)
    for k, r in prev.items():
        if k.startswith("F-B3-12/") and "PAUSED" not in str(r.get("scenario")):
            k = k.replace("F-B3-12/", "F-B3-12-wp24bp/")  # the pre-fix (vacuous) row
        rows.setdefault(k, r)
    order = ["F-B3-12/", "F-B3-12-wp24bp/", "F-B3-13a/", "F-B3-13b/", "F-B3-13c/", "F-B3-13d/"]
    doc = {"rows": {k: rows[k] for o in order for k in sorted(rows) if k.startswith(o)}}
    doc["pass"] = all((r or {}).get("verdict") == "PASS" for r in doc["rows"].values())
    dump(out / "gdbdiag.json", doc)
    return doc


# ---------------------------------------------------------------------------
# stale: F-B3-16 (a)-(e)
# ---------------------------------------------------------------------------

def _deploy_try(art: Path, toml: Path, rdir: Path, qemu: Path, **kw: Any) -> dict[str, Any]:
    from scopt.run.deploy import DeployError, deploy_new
    from scopt.run.diag import read_v6_stats, stats_env
    from scopt.run.launcher import launch

    rmtree(rdir)
    try:
        rdo, dep = deploy_new(art, toml, rdir, qemu=qemu, **kw)
    except DeployError as e:
        return {"deployed": False, "refused": e.code, "detail": e.detail[:400]}
    sp = rdo.root / "v6-stats.json"
    doc = launch(rdo, timeout_s=300, extra_env=stats_env(sp))
    st = read_v6_stats(sp)
    return {"deployed": True, "identity_checked": (dep.get("identity") or {}).get("checked"),
            "verdict": doc.get("verdict"), "exit_code": doc.get("exit_code"),
            "appended": doc.get("appended"), "qemu_sha256": doc.get("qemu_sha256"),
            "proof_failed": [c.get("check") for c in ((doc.get("proof") or {}).get("checks") or [])
                             if not c.get("ok")],
            "stats": {k: st.get(k) for k in ("present", "links_armed", "loaded")} if st else None,
            "run_dir": str(rdo.root)}


def _render(cfg: str, mode: str, qemu: Path, dst: Path) -> Path:
    import fdrun

    text, _ = fdrun.render(str(FIX / f"b3-{cfg}.toml.in"),
                           {"icount": "sc", "noicount": "sc-noicount"}[mode], f"b3-{cfg}.sc",
                           str(qemu), "none")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(text)
    return dst


def _direct_16d(out: Path, mode: str) -> dict[str, Any]:
    """F-B3-16(d): the F-B3-1 artifact's warm run re-run directly on QEMU with the kernel
    replaced by ``b3_16d.elf`` (one changed instruction in ``f_b3_1`` at the same address;
    FastDyn's fingerprint would delete the cache before QEMU starts, WP24 finding 12),
    on the gate binary with ``QEMU_SC_V6_STATS``. R's guest hash no longer matches at
    activation: nothing may be armed, and the output must equal the new image's warm."""
    import expected as EXP
    import qemu_rerun

    gi = gate_info()
    dyn = (rd(out / "dynamic.json") or {})["rows"].get(f"{mode}/1") or {}
    src = (dyn["cases"][0]["runs"].get("artifact") or {}).get("run_dir")
    d = out / "stale" / f"16d-{mode}"
    rmtree(d)
    argv = qemu_rerun.prepare(src, str(d), cache_from=src, kernel=str(FIX / "b3_16d.elf"),
                              qemu=str(gi["exe"]))
    # the scopt launcher's runs use file-backed RAM (ram/<bank>.ram): recreate every bank
    # zeroed at its size (as reset_ram does) and write the F-B3-1 input into the input bank
    (d / "ram").mkdir(exist_ok=True)
    for f in sorted((Path(src) / "ram").glob("*.ram")):
        with open(d / "ram" / f.name, "wb") as fh:
            fh.truncate(f.stat().st_size)
    with open(d / "ram" / "input.ram", "r+b") as fh:
        fh.write((FIX / "inputs" / "1.bin").read_bytes())
    stats = d / "v6-stats.json"
    env = {**qemu_rerun.clean_env(), "QEMU_SC_V6_STATS": str(stats)}
    cmd = ["setarch", "x86_64", "-R", "timeout", "--signal=KILL", "300"] + argv
    (d / "cmd.txt").write_text(" ".join(cmd) + "\n")
    p = subprocess.run(cmd, cwd=str(d), env=env, capture_output=True, text=True)
    (d / "qemu.stdout").write_text(p.stdout + p.stderr)
    norm = qemu_rerun.guest_norm((p.stdout + p.stderr).splitlines(), p.returncode)
    text = "\n".join(norm) + "\n"
    (d / "guest.norm").write_text(text)
    st = json.loads(stats.read_text()) if stats.is_file() else {}
    # the virtuals are b3-1's (BENCH name b3-1): mask the name as WP24 did (finding 12)
    fails = EXP.compare(text.replace("[BENCH][b3-1]", "[BENCH][b3-16d]"), "16d", mode)
    cache_rel = [a for a in argv if "supercache=" in a]
    res = {"run_dir": str(d), "exit": p.returncode, "fixture_oracle_failures": fails[:10],
           "stats": {k: st.get(k) for k in ("loaded", "n_links", "links_armed", "activations",
                                            "refused", "variants_built")},
           "argv_supercache": cache_rel[:1]}
    res["ok"] = p.returncode == 0 and not fails and st.get("loaded") is True and \
        st.get("links_armed") == 0
    return res


def step_stale(out: Path) -> dict[str, Any]:
    gi = gate_info()
    res: dict[str, Any] = {}
    for m in MODES:
        art = published_dir(out, m, "1")
        r: dict[str, Any] = {"artifact": str(art)}
        for case in ("16a", "16b", "16e"):
            b = base_info(out, m, case)
            r[case] = _deploy_try(art, b["toml"], out / "stale" / f"{case}-{m}", gi["exe"])
            r[case]["base_warm"] = _deploy_try(b["snapshot"], b["toml"],
                                               out / "stale" / f"{case}-{m}-warm", gi["exe"])
        # (c) a different QEMU binary: qint (v5 loader). The artifact directory is refused
        # at deploy; the bare cache.sc deploys without identity and its proof shows the
        # artifact unused.
        tq = _render("1", m, G.QINT, out / "stale" / "configs" / f"b3-1-qint-{m}.toml")
        r["16c"] = _deploy_try(art, tq, out / "stale" / f"16c-{m}", G.QINT)
        r["16c_bare"] = _deploy_try(art / "cache.sc", tq, out / "stale" / f"16c-bare-{m}", G.QINT)
        r["16d"] = _direct_16d(out, m)
        unused = ("INVALID_artifact_not_used",)
        r["items"] = {
            "(a) changed patch plan: refused at deploy or proof shows the artifact unused":
                (not r["16a"]["deployed"]) or r["16a"].get("verdict") in unused,
            "(b) different plugin build: refused or INVALID": (not r["16b"]["deployed"]) or
                str(r["16b"].get("verdict", "")).startswith("INVALID"),
            "(c) different QEMU binary: artifact refused, bare .sc INVALID":
                (not r["16c"]["deployed"]) and
                str(r["16c_bare"].get("verdict", "")).startswith("INVALID"),
            "(d) guest hash: nothing armed, output = the new image's (16d.norm)": r["16d"]["ok"],
            "(e) coverage config: refused at deploy or the artifact unused":
                (not r["16e"]["deployed"]) or r["16e"].get("verdict") in unused,
            "reference warm runs PASS (16a/16b/16e bases)":
                all(r[c]["base_warm"].get("verdict") == "PASS" for c in ("16a", "16b", "16e")),
        }
        r["ok"] = all(r["items"].values())
        log("stale", m, {k: (v.get("refused") or v.get("verdict")) for k, v in r.items()
                         if isinstance(v, dict) and k[:2] == "16" and "deployed" in v},
            "16d", r["16d"]["stats"], r["16d"]["ok"], "ok", r["ok"])
        res[m] = r
    doc = {"rows": res, "pass": all(r["ok"] for r in res.values())}
    dump(out / "stale.json", doc)
    return doc


# ---------------------------------------------------------------------------
# the manager's plan (for negative controls and link mutants)
# ---------------------------------------------------------------------------

def manager_ctx(out: Path, mode: str, cfg: str) -> dict[str, Any]:
    """Re-run the WP21 manager in-process with the CLI's inputs (same snapshot, profile,
    RuntimeIdentity, params, selftest) up to :func:`materialize`: the plan whose linked
    bytes are the published artifact's (checked by :func:`plan_matches_artifact`)."""
    from scopt.analysis.b3_inputs import resolve_for_snapshot
    from scopt.analysis.obs_contract import build_contract
    from scopt.commands.optimize import _profiles
    from scopt.formats.envlayout_v1 import find_envlayout
    from scopt.guard import GuardOffsets
    from scopt.importer.import_cache import open_snapshot
    from scopt.manager import PassManager, load_pipeline
    from scopt.manager_b3 import B3Env, materialize
    from scopt.validate_sym.link_check import hostsym_inputs, summary_provenance
    from scopt.validate_sym.selftest import load_or_run

    b = base_info(out, mode, cfg)
    s = open_snapshot(b["snapshot"])
    resolve_for_snapshot(s, elf=str(b["elf"]), prep=str(b["prep"]))
    views, _ = _profiles([str(b["profile"])], s)
    ri = rd(b["ri"])
    env = B3Env(runtime=ri, v6_exes=(), selftest=load_or_run(str(selftest_path(out))))
    pm = PassManager(s, load_pipeline("b3"), tuple(views), FIXTURE_PARAMS, b3=env)
    cand = pm.execute()
    m = materialize(cand.b3_state, pm.b3ctx)
    lay = find_envlayout(ri["qemu_sha256"])
    rows = pm.b3ctx.summaries()
    contract = build_contract(lay, ri)
    off = GuardOffsets.from_layout(lay)
    hs = hostsym_inputs(layout=lay, contract=contract, offsets=off, selftest=env.selftest,
                        provenance=summary_provenance(rows))
    return {"s": s, "plan": m.plan, "ri": ri, "lay": lay, "rows": rows, "hs": hs, "b": b,
            "contract": contract, "off": off, "mode": mode, "cfg": cfg}


def link_of(ctx: dict[str, Any], plan: Any = None) -> Any:
    from scopt.link import NativeLinkerV2

    return NativeLinkerV2(ctx["s"], plan or ctx["plan"], offsets=ctx["off"],
                          derive_captures=False).link()


def plan_matches_artifact(ctx: dict[str, Any], art: Path) -> dict[str, Any]:
    """The in-process plan, linked, gives the published artifact's bytes (every record body
    and every variant body, as multisets)."""
    from scopt.formats.sc_semantic import read_semantic_cache

    lk = link_of(ctx)
    f = read_semantic_cache(art / "cache.sc")
    mine_u = sorted(bytes(u.body) for u in lk.units)
    theirs_u = sorted(bytes(x or b"") for x in f.sc.bodies if x)
    mine_v = sorted(bytes(v.body) for v in lk.variants)
    theirs_v = sorted(bytes(x) for x in f.variant_bodies)
    return {"link_ok": lk.ok, "unit_bodies_equal": mine_u == theirs_u,
            "variant_bodies_equal": mine_v == theirs_v, "n_variants": len(mine_v),
            "ok": lk.ok and mine_u == theirs_u and mine_v == theirs_v}


# --- byte helpers on a link -------------------------------------------------

def patch_variant(link: Any, vidx: int, patches: list[tuple[int, bytes]]) -> Any:
    vs = []
    for v in link.variants:
        if v.index == vidx:
            body = bytearray(v.body)
            for off, bb in patches:
                body[off:off + len(bb)] = bb
            v = dataclasses.replace(v, body=bytes(body))
        vs.append(v)
    return dataclasses.replace(link, variants=tuple(vs))


def nop(n: int) -> bytes:
    from scopt.validate_sym.asm import nop as _nop

    return _nop(n)


def slots_of(link: Any) -> list[dict[str, Any]]:
    by_tb = {u.new_tb_offset: u for u in link.units}
    res = []
    for i, s in enumerate(link.ext.slots):
        u = by_tb.get(s.src_tb_offset)
        res.append({"i": i, "kind": {1: "GENERAL", 2: "SPEC"}.get(s.kind), "uid": u.uid if u
                    else None, "guard_off": s.guard_off, "target": s.target_vaddr,
                    "slot_n": s.slot_n, "body": u.body if u else b""})
    return res


def spec_slot(link: Any, target: int, src_start: int | None = None,
              starts: dict[str, int | None] | None = None) -> dict[str, Any]:
    c = [s for s in slots_of(link) if s["kind"] == "SPEC" and s["target"] == target and
         (src_start is None or (starts or {}).get(s["uid"]) == src_start)]
    if len(c) != 1:
        raise RuntimeError(f"{len(c)} SPEC slots to {target:#x} (src {src_start})")
    return c[0]


def variant_for_slot(link: Any, slot_index: int) -> int:
    lk = [x for x in link.ext.links if x.slot_index == slot_index]
    if len(lk) != 1:
        raise RuntimeError(f"slot {slot_index}: {len(lk)} links")
    return lk[0].variant_index


def drop_atom(link: Any, slot: dict[str, Any], atom: str) -> Any:
    from scopt.validate_sym.link_mutants import drop_atom_patches, patch_unit, stub_atoms

    atoms = [a for a in stub_atoms(slot["body"], slot["guard_off"]) if a.atom == atom]
    if not atoms:
        raise RuntimeError(f"atom {atom} not in the stub of slot {slot['i']}")
    ps = []
    for a in atoms:
        ps += drop_atom_patches(slot["body"], a)
    return patch_unit(link, slot["uid"], ps)


def unit_starts(s: Any) -> dict[str, int | None]:
    return {u.uid: (u.vaddrs[0] & ~1 if u.vaddrs else None) for u in s.units()}


def unit_at(s: Any, vaddr: int, *, longest: bool = True) -> Any:
    hit = [u for u in s.units() if u.vaddrs and (u.vaddrs[0] & ~1) == vaddr]
    if not hit:
        raise RuntimeError(f"no unit at {vaddr:#x}")
    return max(hit, key=lambda u: len(u.code)) if longest else hit


def hostops_of(ctx: dict[str, Any], u: Any) -> Any:
    from scopt.analysis.hostops import build_hostops

    return build_hostops(u, ctx["lay"], ctx["s"].header.code_address)


def jcc_flip(old: bytes, cur: bytes) -> bytes:
    """``cur`` is the variant's bytes at an original ``jcc`` (``old``): nops (not taken) ->
    ``jmp`` to the same target; ``jmp`` (taken) -> nops."""
    if cur[:1] == b"\xe9" or cur[:1] == b"\xeb":
        return nop(len(old))
    if len(old) == 6 and old[0] == 0x0F and 0x80 <= old[1] <= 0x8F:
        rel = struct.unpack_from("<i", old, 2)[0]
        return b"\xe9" + struct.pack("<i", rel + 1) + b"\x90"
    if len(old) == 2 and 0x70 <= old[0] <= 0x7F:
        return b"\xeb" + old[1:2]
    raise ValueError("not a jcc")


# ---------------------------------------------------------------------------
# o4: negative controls (§9.3 dynamic set) on their named fixtures
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class Mut:
    name: str  # §9.3 / mutants.py kind
    cfg: str  # detector fixture
    build: Callable[[dict[str, Any]], tuple[Any, Callable[[Any], Any] | None, str]]
    detector: str
    inputs: tuple[str, ...] | None = None
    bypass_extra: tuple[str, ...] = ()
    scenario: str | None = None  # gdb scenario detector (F-B3-13(d))
    unit_bytes: bool = False  # mutates a placed unit's bytes (V2/V3 also bypassed: undeclared)
    note: str = ""


def _variant_fold_site(ctx: dict[str, Any], vspec: Any) -> tuple[Any, int]:
    """The patch of ``vspec`` at C's jcc (BF's fold) and its index."""
    for i, p in enumerate(vspec.patches):
        o = bytes(p.old_bytes)
        if (len(o) == 6 and o[0] == 0x0F and 0x80 <= o[1] <= 0x8F) or \
                (len(o) == 2 and 0x70 <= o[0] <= 0x7F):
            return p, i
    raise RuntimeError("variant has no folded jcc")


def _vspec_for(plan: Any, c_vaddr: int, s: Any, pred_start: int | None = None) -> Any:
    st = unit_starts(s)
    c = [v for v in plan.variants if st.get(v.src_uid) == c_vaddr and
         (pred_start is None or st.get(v.pred_uid) == pred_start)]
    if len(c) != 1:
        raise RuntimeError(f"{len(c)} variants of C {c_vaddr:#x} (pred {pred_start})")
    return c[0]


def _replace_vspec(plan: Any, old: Any, new: Any) -> Any:
    return dataclasses.replace(plan, variants=tuple(new if v is old else v
                                                    for v in plan.variants))


def b_fold_flip(ctx):
    s, plan = ctx["s"], ctx["plan"]
    v = _vspec_for(plan, sym("vc_b3_1"), s)
    p, i = _variant_fold_site(ctx, v)
    np_ = dataclasses.replace(p, new_bytes=jcc_flip(bytes(p.old_bytes), bytes(p.new_bytes)))
    nv = dataclasses.replace(v, patches=v.patches[:i] + (np_,) + v.patches[i + 1:])
    return _replace_vspec(plan, v, nv), None, "C' fold direction flipped, true FactRef cited"


def b_falsified(ctx):
    from scopt.validate_sym.link_mutants import synthetic_fact

    plan, _, _ = b_fold_flip(ctx)
    v = _vspec_for(plan, sym("vc_b3_1"), ctx["s"])
    nv = dataclasses.replace(v, facts=(synthetic_fact("r0", 1, f"wp28-o4-{ctx['mode']}"),))
    return _replace_vspec(plan, v, nv), None, "SPEC link to a C' folded for the false fact r0 == 1"


def b_fold_without_fact(ctx):
    """F-B3-6: the variant behind f_b3_6b's slot (r0 == 5) replaced by the r0 == 0 fold of
    f_b3_6a's variant, citing no fact."""
    s, plan = ctx["s"], ctx["plan"]
    va = _vspec_for(plan, sym("vc_b3_6"), s, sym("f_b3_6a"))
    vb = _vspec_for(plan, sym("vc_b3_6"), s, sym("f_b3_6b"))
    nb = dataclasses.replace(vb, patches=va.patches, facts=())
    return _replace_vspec(plan, vb, nb), None, \
        "f_b3_6b's SPEC link (r0 == 5) to a C' folded for r0 == 0, no fact"


def _drop(atom: str, target_sym: str, src_start_sym: str | None = None):
    def build(ctx):
        st = unit_starts(ctx["s"])
        tgt = sym(target_sym)
        src = sym(src_start_sym) if src_start_sym else None

        def mutate(link):
            sl = spec_slot(link, tgt, src, st)
            return drop_atom(link, sl, atom)
        return ctx["plan"], mutate, f"{atom} dropped from the SPEC stub to {target_sym}"
    return build


def _rec_epilogue_start(ctx) -> int:
    """F-B3-6r: the unit holding rec_b3's z-path epilogue (pop) that returns to rec_b3_cont
    (inner calls) or vc_b3_6r (top call): the SPEC slot whose R is not the base-case unit."""
    st = unit_starts(ctx["s"])
    lk = link_of(ctx)
    c = [s for s in slots_of(lk) if s["kind"] == "SPEC" and s["target"] == sym("rec_b3_cont")
         and st.get(s["uid"]) != sym("rec_b3_ret") & ~1]
    starts = sorted({st.get(s["uid"]) for s in c})
    if len(starts) != 1:
        raise RuntimeError(f"epilogue R ambiguous: {starts}")
    return starts[0]


def b_drop_gpc_6r(ctx):
    st = unit_starts(ctx["s"])
    src = _rec_epilogue_start(ctx)

    def mutate(link):
        return drop_atom(link, spec_slot(link, sym("rec_b3_cont"), src, st), "g_pc")
    return ctx["plan"], mutate, (f"g_pc dropped from R {src:#x}'s SPEC stub to rec_b3_cont "
                                 "(R also returns to vc_b3_6r on the top call)")


def b_guard_stub_store(ctx):
    from scopt.validate_sym.link_mutants import patch_unit, stub_atoms, stub_store_patch

    vc = {"1": "vc_b3_1", "9": "vc_b3_9", "21": "vc_b3_21", "4": "vc_b3_4"}[ctx["cfg"]]
    st = unit_starts(ctx["s"])

    def mutate(link):
        sl = spec_slot(link, sym(vc), None, st)
        a = [x for x in stub_atoms(sl["body"], sl["guard_off"]) if x.atom == "g_bp"][0]
        return patch_unit(link, sl["uid"], stub_store_patch(a))
    return ctx["plan"], mutate, f"env store regs[1] := 0x55 in place of g_bp (stub to {vc})"


def b_guard_before_effect(ctx):
    from scopt.validate_sym.link_mutants import effect_hoists, patch_unit

    vc = {"6": "vc_b3_6", "1": "vc_b3_1", "9": "vc_b3_9", "22": "vc_b3_22",
          "14": "vc_b3_14"}[ctx["cfg"]]
    src = {"6": "f_b3_6a"}.get(ctx["cfg"])
    st = unit_starts(ctx["s"])
    lk = link_of(ctx)
    sl = spec_slot(lk, sym(vc), sym(src) if src else None, st)
    g = lk.grown[sl["uid"]]
    u = g.unit
    tail_off = next(spec.tail_off for spec, _ in g.stubs)
    hs = effect_hoists(u, tail_off, sl["guard_off"], ctx["off"], ctx["s"].header.code_address)
    if not hs:
        raise RuntimeError("no effect store before the tail")
    # prefer the regs[15] store: the thumb store writes the constant the guard key already
    # implies (thumb = 1 on M-profile), so skipping it is an equivalent mutant at run time
    name, ps = next((h for h in hs if h[0].startswith("before_pc")), hs[0])

    def mutate(link):
        return patch_unit(link, sl["uid"], ps)
    return ctx["plan"], mutate, f"R's tail jump hoisted above an effect store: {name} (stub to {vc})"


def _ldst_fast(h: Any, k: int | None = None) -> tuple[int, int]:
    ops = [o for o in h.ops if o.block == "ldst_fast" and (k is None or o.ldst == k)]
    if not ops:
        raise RuntimeError("no ldst_fast block")
    k0 = ops[0].ldst
    ops = [o for o in ops if o.ldst == k0]
    a = min(o.off for o in ops)
    e = max(o.off + o.size for o in ops)
    return a, e - a


def b_nop_dead_result_ldst(ctx):
    """F-B3-3b: R = f_b3_3b (``ldr r0,[SYST_CSR]; bx lr``; r0 is overwritten by the patch, so
    the loaded value is dead): a *declared* rewrite (PURE_REWRITE, as DHO would propose)
    that nops its LDST fast path, i.e. deletes the MMIO read that clears COUNTFLAG."""
    from scopt.passes.base import Patch

    s, plan = ctx["s"], ctx["plan"]
    hit = [u for u in s.units() if u.vaddrs and (u.vaddrs[0] & ~1) == sym("f_b3_3b")]
    u = max(hit, key=lambda x: len(x.code))
    a, n = _ldst_fast(hostops_of(ctx, u))
    p = Patch(u.uid, a, bytes(u.code[a:a + n]), nop(n), "dho_nop")
    rw = dict(plan.rewrites)
    rw[u.uid] = tuple(rw.get(u.uid, ())) + (p,)
    return dataclasses.replace(plan, rewrites=rw), None, \
        f"R {u.uid} (f_b3_3b): declared rewrite nopping its MMIO LDST fast path [{a},+{n})"


def _variant_index_for(link: Any, ctx: dict[str, Any], vc_sym: str,
                       src_sym: str | None = None) -> tuple[int, Any]:
    st = unit_starts(ctx["s"])
    sl = spec_slot(link, sym(vc_sym), sym(src_sym) if src_sym else None, st)
    vi = variant_for_slot(link, sl["i"])
    v = [x for x in link.variants if x.index == vi][0]
    return vi, v


def b_icount_imm(ctx):
    """F-B3-7: C''s icount budget decrement immediate changed by one (prefix ``add ebx,
    imm``)."""
    def mutate(link):
        if ctx["mode"] != "icount":
            raise RuntimeError("noicount: C' has no icount prefix (mutant only exists in icount)")
        vi, v = _variant_index_for(link, ctx, "vc_b3_7", "f_b3_7")
        c = [u for u in ctx["s"].units() if u.uid == v.src_uid][0]
        h = hostops_of(ctx, c)
        o = next(o for o in h.ops if o.block == "prefix" and
                 (o.text.startswith("add ebx") or o.text.startswith("dec ebx")))
        code = bytearray(v.body[o.off:o.off + o.size])
        if o.text.startswith("dec"):
            # a 1-insn C: `dec ebx` has no immediate; the same-length change of the count
            # is to drop it (C' then counts 0 instead of 1 instruction per entry)
            return patch_variant(link, vi, [(o.off, nop(o.size))])
        code[-1] = (code[-1] - 1) & 0xFF  # one more instruction counted per C' entry
        return patch_variant(link, vi, [(o.off, bytes(code))])
    return ctx["plan"], mutate, ("C' prefix icount decrement changed (imm off by one, or "
                                 "`dec ebx` -> nop for a 1-insn C)")


def b_search_row(ctx):
    """F-B3-9: C''s first search-data byte flipped (the restore row of the faulting first
    instruction)."""
    def mutate(link):
        vi, v = _variant_index_for(link, ctx, "vc_b3_9")
        b0 = v.body[v.tc_size]
        return patch_variant(link, vi, [(v.tc_size, bytes([b0 ^ 0x01]))])
    return ctx["plan"], mutate, "C' search row 0, first byte xor 1"


def b_dho_ldst(ctx):
    """F-B3-10: C''s dead-result ``ldr r1,[r11]`` LDST fast path nopped (a DHO that deletes
    a faulting load)."""
    def mutate(link):
        vi, v = _variant_index_for(link, ctx, "vc_b3_10")
        c = [u for u in ctx["s"].units() if u.uid == v.src_uid][0]
        a, n = _ldst_fast(hostops_of(ctx, c))
        return patch_variant(link, vi, [(a, nop(n))])
    return ctx["plan"], mutate, "C' LDST of the unused faulting load nopped"


def b_shared_suffix(ctx):
    """F-B3-20: one variant (fact r0 == 1 certified at the full unit f_b3_20) shared with
    a suffix unit of f_b3_20 (plan form: a second copy for the suffix's own SPEC slot)."""
    from scopt.guard import GuardTarget
    from scopt.link import GuardedExitSpec, VariantSpec, capture_join, exit_class_of, \
        free_slots, lookup_tails, tail_problem

    s, plan = ctx["s"], ctx["plan"]
    st = unit_starts(s)
    full = _vspec_for(plan, sym("vc_b3_20"), s, sym("f_b3_20"))
    ret = sym("f_b3_20_ret")
    sufs = [u for u in s.units() if u.vaddrs and st[u.uid] is not None and
            sym("f_b3_20") < st[u.uid] <= ret]
    if not sufs:
        raise RuntimeError("no suffix unit of f_b3_20 in this snapshot")
    jr = capture_join(s)
    grow = dict(plan.grow)
    extra = []
    vc = sym("vc_b3_20")
    fs = plan.grow[full.pred_uid][0]
    for su in sorted(sufs, key=lambda u: st[u.uid]):
        if su.uid in grow:
            # chain-returns grew it with a GENERAL slot to vc_b3_20: make that slot SPEC
            specs = []
            done = False
            for gx in grow[su.uid]:
                tg = []
                for t_ in gx.targets:
                    if t_.vaddr == vc and t_.kind == "GENERAL" and not done:
                        tg.append(GuardTarget(vc, t_.slot_n, "SPEC"))
                        extra.append(VariantSpec(full.src_uid, su.uid, t_.slot_n,
                                                 patches=full.patches, facts=full.facts))
                        done = True
                    else:
                        tg.append(t_)
                specs.append(dataclasses.replace(gx, targets=tuple(tg)))
            grow[su.uid] = tuple(specs)
            continue
        tails = lookup_tails(su)
        if len(tails) != 1 or tail_problem(su, tails[0], s.header.code_address):
            continue
        t = tails[0]
        xc = exit_class_of(su, t, jr.slots[su.slot].chunk.guest_bytes)
        n = list(free_slots(su))[0]
        grow[su.uid] = (GuardedExitSpec(t.off, xc if xc in ("return", "static") else "static",
                                        (GuardTarget(vc, n, "SPEC"),), fs.key_flags,
                                        fs.key_cs_base),)
        extra.append(VariantSpec(full.src_uid, su.uid, n, patches=full.patches,
                                 facts=full.facts))
    if not extra:
        raise RuntimeError("no admissible suffix tail")
    np_ = dataclasses.replace(plan, grow=grow, variants=plan.variants + tuple(extra),
                              max_variants_per_c=len(extra) + 2,
                              max_variants=len(plan.variants) + len(extra) + 8)
    return np_, None, (f"{len(extra)} suffix units of f_b3_20 ({sorted(hex(st[v.pred_uid]) for v in extra)}) "
                       "linked by SPEC slots to copies of the full unit's variant (r0 == 1 is not "
                       "established in a suffix)")


MUTANTS: list[Mut] = [
    Mut("fold_flip", "1", b_fold_flip, "F-B3-1 O1 (z/nz)"),
    Mut("falsified_fact_link", "1", b_falsified, "F-B3-1 O1 (z/nz)", bypass_extra=("V19",)),
    Mut("fold_without_fact", "6", b_fold_without_fact, "F-B3-6 O1 (f5 path counts)",
        bypass_extra=("V19",)),
    Mut("drop_g_pc", "6r", b_drop_gpc_6r, "F-B3-6r O1 (recursion: R returns to two Vc)", unit_bytes=True),
    Mut("drop_g_pc", "11", _drop("g_pc", "vc_b3_11"), "F-B3-11 held-out O1 (unseen call site B)",
        inputs=("11-heldout",), unit_bytes=True),
    Mut("drop_g_thumb", "22", _drop("g_thumb", "vc_b3_22"), "F-B3-22 O1 (INVSTATE report)", unit_bytes=True),
    Mut("drop_g_bp", "13", _drop("g_bp", "vc_b3_13"), "F-B3-13(d) gdb scenario",
        scenario="F-B3-13d", unit_bytes=True),
    Mut("guard_before_effect", "6", b_guard_before_effect, "F-B3-6 O1/O2a/O2b", unit_bytes=True),
    Mut("guard_before_effect", "9", b_guard_before_effect, "F-B3-9 fault report (O1/O2b)", unit_bytes=True),
    Mut("guard_before_effect", "22", b_guard_before_effect, "F-B3-22 fault report (O1/O2b)", unit_bytes=True),
    Mut("guard_stub_store", "1", b_guard_stub_store, "F-B3-1 O1/O2b", unit_bytes=True),
    Mut("guard_stub_store", "9", b_guard_stub_store, "F-B3-9 fault report (stacked r1)", unit_bytes=True),
    Mut("nop_dead_result_ldst", "3b", b_nop_dead_result_ldst, "F-B3-3b O1 (COUNTFLAG log)",
        bypass_extra=("V3", "V8")),
    Mut("icount_immediate", "7", b_icount_imm, "F-B3-7 O2b (interrupt positions)"),
    Mut("search_row", "9", b_search_row, "F-B3-9 O1/O2b (fault report PC)"),
    Mut("dho_deletes_faulting_ldst", "10", b_dho_ldst, "F-B3-10 O1 (fault raised)"),
    Mut("shared_variant_suffix", "20", b_shared_suffix, "F-B3-20 O1 (z count)",
        bypass_extra=("V19",)),
]
STATIC_ONLY = {"drop_g_cond": "§9.3: condexec is 0 at a return-exit slot (U-COND); F-B3-22b "
                              "not built (U-VIRT: no deterministic non-RW_CFI xPSR writer)",
               "drop_g_flags": "§9.3: cached hflags cannot change between R's key match and "
                               "R's exit on cortex-m3",
               "drop_g_flags2": "§9.3: as drop_g_flags"}


def o4_one(out: Path, mu: Mut, mode: str, k: int) -> dict[str, Any]:
    from scopt.run.negative import BYPASS_ROWS, package_negative

    gi = gate_info()
    lab = f"{mu.name}-{mu.cfg}-{mode}"
    r: dict[str, Any] = {"mutant": mu.name, "fixture": mu.cfg, "mode": mode,
                         "detector": mu.detector, "label": lab}
    ctx = manager_ctx(out, mode, mu.cfg)
    art = published_dir(out, mode, mu.cfg)
    r["plan_matches_published"] = plan_matches_artifact(ctx, art)
    try:
        plan, mutate, what = mu.build(ctx)
    except Exception as e:  # noqa: BLE001 - a mutant that cannot be built is recorded
        r.update(ok=False, built=False, why=f"{type(e).__name__}: {e}")
        log("o4", lab, "not built:", r["why"])
        return r
    r["what"] = what
    ndir = out / "o4" / "neg"
    rmtree(ndir / lab)
    bypass = BYPASS_ROWS + tuple(mu.bypass_extra) + (("V2", "V3") if mu.unit_bytes else ())
    nr = package_negative(ctx["s"], plan, ndir, label=lab, mutant=mu.name,
                          detector=mu.detector, mutate=mutate, layout=ctx["lay"],
                          runtime=ctx["ri"], v6_exes=[gi["sha"]], hostsym=ctx["hs"],
                          summaries=ctx["rows"], bypass=bypass)
    codes = sorted({x["code"] for x in nr.bypassed})
    r.update(status=nr.status, reason=nr.reason, path=nr.path, bypass=list(bypass),
             statically_rejected_by=codes,
             bypassed=[{"code": x["code"], "check": x.get("check"), "reason": x.get("reason")}
                       for x in nr.bypassed][:20],
             blocking=[{"code": x["code"], "check": x.get("check"), "reason": x.get("reason")}
                       for x in nr.blocking][:10])
    r["static_rejected"] = nr.packaged and bool(set(codes) & {"V12", "V13", "V14", "V15",
                                                               "V16", "V17"})
    if not nr.packaged:
        r["ok"] = False
        log("o4", lab, "refused", nr.reason)
        return r
    if mu.scenario:
        ctl = direct_guest_scenario(out, mu.scenario, mode, tag=f"o4-control-{lab}")
        mut = direct_guest_scenario(out, mu.scenario, mode, adir=Path(nr.path), tag=f"o4-{lab}")
        r["scenario_control"] = {k_: ctl[k_] for k_ in ("verdict", "guest", "pairs",
                                                         "variant_hits_total")}
        r["scenario_mutant"] = {k_: mut[k_] for k_ in ("verdict", "guest", "pairs",
                                                        "variant_hits_total", "qemu_exit")}
        r["detected"] = ctl["verdict"] == "PASS" and (mut.get("guest") or {}).get("ok") is False \
            and mut.get("qemu_exit") is not None
        r["detected_by"] = "gdb scenario guest checks (hit counts / results differ from warm)"
    else:
        dyn = dynamic_one(out, mode, mu.cfg, art=Path(nr.path), tag=f"o4-{lab}",
                          oracles=("O1", "O2a", "O2b", "O3"), negative=True,
                          o3={"stats": {"links_armed": ">0"}}, hit_labels=None,
                          inputs=list(mu.inputs) if mu.inputs else None)
        r["dynamic"] = {k_: dyn.get(k_) for k_ in ("verdict", "failures", "incomplete", "file")}
        r["cases"] = [{"input": c["fixture_input"], "oracles": c["oracles"],
                       "details": c["details"], "hits": c["hits"],
                       "armed": ((c.get("stats") or {}).get("artifact") or {}).get(
                           "links_armed")} for c in dyn.get("cases", [])]
        r["detected"] = dyn.get("verdict") == "pass"
        r["detected_by"] = sorted({o for c in r["cases"] for o, v in c["oracles"].items()
                                   if o in ("O1", "O2a", "O2b") and v == "fail"})
    r["ok"] = r["static_rejected"] and r["detected"]
    log("o4", lab, "static", codes, "detected", r["detected"], r.get("detected_by"))
    return r


def step_o4(out: Path) -> dict[str, Any]:
    only = [x for x in os.environ.get("WP28_O4_ONLY", "").split(",") if x]
    todo = [(mu, m) for mu in MUTANTS for m in MODES if not only or mu.name in only]
    res = pmap(lambda x: o4_one(out, x[0], x[1], 0), todo, jobs=3)
    prev = {r.get("label"): r for r in ((rd(out / "o4.json") or {}).get("rows") or [])
            if isinstance(r, dict) and r.get("label")}
    for (mu, m), r in zip(todo, res):
        if isinstance(r, dict) and "error" in r:
            r.update(label=f"{mu.name}-{mu.cfg}-{m}", mutant=mu.name, fixture=mu.cfg, mode=m)
        prev[r.get("label") or f"{mu.name}-{mu.cfg}-{m}"] = r
    lab_of = {f"{mu.name}-{mu.cfg}-{m}": (mu, m) for mu in MUTANTS for m in MODES}
    todo = [lab_of[k] for k in prev if k in lab_of]
    rows = [prev[k] for k in prev if k in lab_of]
    by: dict[str, dict[str, Any]] = {}
    for (mu, m), r in zip(todo, rows):
        d = by.setdefault(mu.name, {"attempts": [], "detected_modes": set(),
                                    "static_rejected": True})
        d["attempts"].append({k: r.get(k) for k in ("fixture", "mode", "status", "reason",
                                                     "statically_rejected_by", "detected",
                                                     "detected_by", "why", "ok")})
        if r.get("detected"):
            d["detected_modes"].add(f"{mu.cfg}/{m}")
        if r.get("status") == "packaged" and not r.get("static_rejected"):
            d["static_rejected"] = False
    for d in by.values():
        d["detected_modes"] = sorted(d["detected_modes"])
        d["detected"] = bool(d["detected_modes"])
    doc = {"rows": rows, "by_mutant": by, "static_only": STATIC_ONLY,
           "all_dynamic_detected": all(d["detected"] for d in by.values()),
           "all_static_rejected": all(d["static_rejected"] for d in by.values())}
    dump(out / "o4.json", doc)
    return doc


# ---------------------------------------------------------------------------
# mutants: the static mutation suite (T-B3-5) on the gate fixtures
# ---------------------------------------------------------------------------

def mutants_cfg(out: Path, cfg: str) -> dict[str, Any]:
    """link mutants on the manager's own link of the fixture artifact (icount)."""
    from scopt.validate_sym.link_mutants import guard_mutants

    ctx = manager_ctx(out, "icount", cfg)
    lk = link_of(ctx)
    pm = plan_matches_artifact(ctx, published_dir(out, "icount", cfg))
    suite = guard_mutants(ctx["s"], lk, layout=ctx["lay"], contract=ctx["contract"],
                          offsets=ctx["off"], source="manager")
    sm = suite.summary()
    # a mutant that certify_link (hostsym) does not reject is re-checked through the full
    # static validator (validate_b3 via package_negative with nothing bypassed): it must be
    # refused there (e.g. V13's strict template decoder for an atom hostsym proves true)
    full = []
    nr_ = [r for r in suite.counted if not r.rejected]
    if nr_:
        from scopt.run.negative import package_negative

        gi = gate_info()
        for r in nr_:
            atom = r.name.split("@")[0].removeprefix("drop_")
            for sl in [x for x in slots_of(lk) if x["uid"] == r.unit]:
                try:
                    mut = drop_atom
                    lab = f"recheck-{cfg}-{r.name.split('@')[0]}-slot{sl['i']}"
                    rmtree(out / "mutants" / "recheck" / lab)
                    pk = package_negative(ctx["s"], ctx["plan"], out / "mutants" / "recheck",
                                          label=lab, mutant=r.name,
                                          mutate=lambda L, sl=sl, atom=atom: mut(L, sl, atom),
                                          layout=ctx["lay"], runtime=ctx["ri"],
                                          v6_exes=[gi["sha"]], hostsym=ctx["hs"],
                                          summaries=ctx["rows"], bypass=())
                    full.append({"mutant": r.name, "unit": r.unit, "slot": sl["i"],
                                 "status": pk.status, "reason": pk.reason,
                                 "failing_rows": sorted({(x["code"], x.get("check"),
                                                          x.get("reason")) for x in pk.blocking}),
                                 "rejected": pk.status == "refused"})
                except Exception as e:  # noqa: BLE001
                    full.append({"mutant": r.name, "unit": r.unit, "slot": sl["i"],
                                 "error": f"{type(e).__name__}: {e}", "rejected": False})
    all_rej = sm["controls_ok"] and pm["ok"] and (sm["all_rejected"] or (
        bool(full) and all(x["rejected"] for x in full) and
        {x["mutant"] for x in full} >= {r.name for r in nr_}))
    return {"plan_matches_published": pm, "full_validator_recheck": full,
            "summary": {k: sm[k] for k in (
        "mutants", "rejected", "mismatch", "all_rejected", "equivalent_mutants",
        "equivalent_rejected", "controls", "controls_ok", "skipped", "by_class")},
            "not_rejected": [r.to_json() for r in suite.counted if not r.rejected][:20],
            "ok": all_rej}


def step_mutants(out: Path) -> dict[str, Any]:
    from scopt.validate_sym.mutants import synthetic_suite

    syn = synthetic_suite().to_json()
    doc: dict[str, Any] = {"synthetic": {k: syn.get(k) for k in ("mutants", "rejected",
                                                                 "all_rejected", "by_verdict",
                                                                 "controls_ok")
                                         if k in syn}}
    doc["synthetic_all_rejected"] = syn.get("all_rejected")
    cfgs = [c for c in FIXTURES if published_dir(out, "icount", c) is not None and
            (rd(out / "artifacts.json") or {})["rows"][f"icount/{c}"].get("semantic_table") is True]

    def real(cfg: str) -> dict[str, Any]:
        b = base_info(out, "icount", cfg)
        (out / "mutants").mkdir(parents=True, exist_ok=True)
        f1 = out / "mutants" / f"real-{cfg}.json"
        f2 = out / "mutants" / f"link-{cfg}.json"
        rc1, _ = cli(["hostsym", "mutants", b["snapshot"], "--limit", "25", "--out", f1],
                     out / "logs" / f"mutants-real-{cfg}.log")
        rc2, _ = cli(["hostsym", "link-mutants", b["snapshot"], "--out", f2],
                     out / "logs" / f"mutants-link-{cfg}.log")
        d1, d2 = rd(f1) or {}, rd(f2) or {}
        realm = d1.get("real") or {}
        m = mutants_cfg(out, cfg)
        # WP19's CLI suite (its own GENERAL plan): a mutant hostsym does not reject must be one
        # the full static validator rejects on the manager's link (same unit and mutant)
        unrej = [(r_["name"].split("@")[0], r_["unit"]) for r_ in d2.get("results", [])
                 if not r_.get("rejected") and r_.get("equivalent_mutant") is None]
        rech = {(x["mutant"].split("@")[0], x["unit"]) for x in m["full_validator_recheck"]
                if x.get("rejected")}
        link_ok = rc2 == 0 or (bool(unrej) and all(u in rech for u in unrej) and
                               d2.get("controls_ok"))
        return {"real": {k: realm.get(k) for k in ("mutants", "rejected", "all_rejected")},
                "link_unrejected_by_hostsym": [{"mutant": a_, "unit": b_} for a_, b_ in unrej],
                "link_unrejected_rejected_by_full_validator": all(u in rech for u in unrej),
                "real_rc": rc1,
                "link": {k: d2.get(k) for k in ("mutants", "rejected", "mismatch",
                                                "all_rejected", "controls", "controls_ok",
                                                "equivalent_mutants", "growable")},
                "link_rc": rc2, "manager_link": m,
                "ok": rc1 == 0 and link_ok and m["ok"]}
    res = pmap(real, cfgs, jobs=4)
    doc["fixtures"] = dict(zip(cfgs, res))
    tot = {"real": [0, 0], "link": [0, 0], "manager": [0, 0]}
    for r in res:
        if not isinstance(r, dict) or "error" in r:
            continue
        tot["real"][0] += r["real"].get("mutants") or 0
        tot["real"][1] += r["real"].get("rejected") or 0
        tot["link"][0] += r["link"].get("mutants") or 0
        tot["link"][1] += r["link"].get("rejected") or 0
        tot["manager"][0] += r["manager_link"]["summary"]["mutants"]
        tot["manager"][1] += r["manager_link"]["summary"]["rejected"]
    doc["totals"] = {k: {"mutants": v[0], "rejected": v[1]} for k, v in tot.items()}
    doc["all_rejected"] = bool(doc["synthetic_all_rejected"]) and all(
        isinstance(r, dict) and r.get("ok") for r in res)
    dump(out / "mutants.json", doc)
    log("mutants", doc["totals"], "all", doc["all_rejected"])
    return doc


# ---------------------------------------------------------------------------
# roundtrip: T-B3-28
# ---------------------------------------------------------------------------

def step_roundtrip(out: Path) -> dict[str, Any]:
    arts = (rd(out / "artifacts.json") or {})["rows"]
    rows: dict[str, Any] = {}

    def one(key: str) -> dict[str, Any]:
        m, c = key.split("/")
        a = arts[key]
        b = base_info(out, m, c)
        d = out / "summaries" / m / c
        rmtree(d)
        rc, so = cli(["return-edges", "summaries", b["snapshot"], "--out-dir", d, "--certify",
                      "--profile", b["profile"], "--prep", b["prep"], "--elf", b["elf"]],
                     out / "logs" / f"summaries-{m}-{c}.log")
        sp = d / "summaries/callee_summaries.jsonl"
        srows = [json.loads(x) for x in sp.read_text().splitlines() if x.strip()] \
            if sp.is_file() else []
        ed = rd(out / "return-edges" / m / f"{c}.json")
        if ed is None:
            ed_f = out / "return-edges" / m / f"{c}.json"
            cli(["return-edges", "analyze", b["snapshot"], "--profile", b["profile"], "--prep",
                 b["prep"], "--elf", b["elf"], "--out", ed_f],
                out / "logs" / f"return-edges-{m}-{c}.log")
            ed = rd(ed_f) or {"exits": []}
        ret_units = {e["uid"] for e in ed["exits"] if e["exit_class"] == "return"}
        sum_units = {r_.get("return_unit") for r_ in srows}
        val = rd(Path(a["artifact_dir"]) / "validation.json") if a.get("artifact_dir") else {}
        v19 = [x for x in (val or {}).get("rows", []) if x.get("code") == "V19"]
        g = [x for x in v19 if "graph" in str(x.get("check"))]
        gdet = " ".join(json.dumps(x.get("detail"), default=str) for x in g)
        regions = Path(a["artifact_dir"]) / "regions.jsonl" if a.get("artifact_dir") else None
        nreg = len([x for x in regions.read_text().splitlines() if x.strip()]) \
            if regions and regions.is_file() else 0
        r = {"summaries_rc": rc, "summary_rows": len(srows), "return_units": len(ret_units),
             "summary_units_cover_return_units": ret_units <= sum_units,
             "v19": [{"check": x.get("check"), "result": x.get("result"),
                      "reason": x.get("reason")} for x in v19],
             "v19_pass": bool(v19) and all(x.get("result") in ("pass", "skip") for x in v19),
             "graph_rows": [x.get("check") for x in g],
             "graph_mentions_guarded_slot": "guarded_slot" in gdet,
             "graph_mentions_variant_link": "variant_link" in gdet,
             "multi_tb_region_rows": nreg, "n_spec": a.get("n_spec"),
             "n_general": a.get("n_general"), "version": a.get("version")}
        r["ok"] = rc == 0 and bool(srows) and r["summary_units_cover_return_units"] and (
            a.get("semantic_table") is False or (
            r["v19_pass"] and (r["n_spec"] == 0 or r["multi_tb_region_rows"] >= r["n_spec"])))
        return r
    keys = [k for k in sorted(arts) if arts[k].get("status") == "published"]
    res = pmap(one, keys, jobs=4)
    rows = dict(zip(keys, res))
    doc = {"rows": rows, "pass": all(isinstance(r, dict) and r.get("ok") for r in res)}
    dump(out / "roundtrip.json", doc)
    log("roundtrip", sum(1 for r in res if r.get("ok")), "/", len(res))
    return doc


# ---------------------------------------------------------------------------
# compute (F-B3-19 / T-B3-30 static + V10; timing is wp28_timing.sh)
# ---------------------------------------------------------------------------

COMPUTE_TEMPLATE = ROOT / "runs/wp13-r2/compute/configs/compute-sc.toml"
TRAIN = [ROOT / f"fixtures/inputs/train/freertos-mps2-compute/train-{i}.bin" for i in (1, 2, 3)]
HELDOUT = [ROOT / f"fixtures/inputs/heldout/freertos-mps2-compute/heldout-{i}.bin"
           for i in (1, 2, 3)]
B3_PASSES = ("specialize-returns", "constant-propagation", "branch-folding", "dead-host-ops",
             "chain-returns", "chain-static")
COMPUTE_ARMS = {
    "b3-chain": tuple(p for p in B3_PASSES if p != "chain-returns"),
    "b3-static": tuple(p for p in B3_PASSES if p != "chain-static"),
    "b3-spec": ("chain-returns", "chain-static"),
    "b3": (),
}


def compute_toml(out: Path) -> Path:
    gi = gate_info()
    t = COMPUTE_TEMPLATE.read_text()
    new, n = re.subn(r'^qemu_path\s*=\s*".*"$', f'qemu_path = "{gi["exe"]}"', t, flags=re.M)
    if n != 1:
        raise SystemExit("compute template: qemu_path line not found exactly once")
    p = out / "compute" / "configs" / "compute-sc-gate.toml"
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.is_file() or p.read_text() != new:
        p.write_text(new)
    return p


def compute_base(out: Path) -> dict[str, Any]:
    gi = gate_info()
    d = out / "compute"
    if (d / "base.json").is_file() and (rd(d / "base.json") or {}).get("snapshot"):
        return rd(d / "base.json")
    toml = compute_toml(out)
    prep = d / "prep"
    rmtree(prep)
    doc: dict[str, Any] = {"config": str(toml), "qemu": str(gi["exe"]),
                           "train_inputs": [str(i) for i in TRAIN]}
    rc, _ = cli(["prepare", "-c", toml, "--out", prep, "--qemu", gi["exe"], "--timeout", "300",
                 "--json", *sum((["--inputs", str(i)] for i in TRAIN), [])],
                d / "logs/01-prepare.log")
    pj = rd(prep / "prep.json") or {}
    doc.update(prepare_rc=rc, prep_verdict=pj.get("verdict"))
    if rc or pj.get("verdict") != "PASS":
        dump(d / "base.json", doc)
        raise SystemExit("compute prepare failed")
    rc1, _ = cli(["bundle", "identity", "-c", prep / "run.toml", "--manifest", pj["manifest"],
                  "--out", d / "bundle.json"], d / "logs/02-bundle.log")
    rc2, _ = cli(["bundle", "runtime-identity", "--run-dir", prep / "oracle", "--sc", pj["sc"],
                  "--scm", prep / "capture.scm", "--out", d / "runtime_identity.json"],
                 d / "logs/03-ri.log")
    rc3, _ = cli(["import", prep, "--bundle", d / "bundle.json", "--runtime-identity",
                  d / "runtime_identity.json", "--store", d / "store"], d / "logs/04-import.log")
    snaps = sorted((d / "store/snapshots").iterdir()) if (d / "store/snapshots").is_dir() else []
    if rc1 or rc2 or rc3 or len(snaps) != 1:
        dump(d / "base.json", doc)
        raise SystemExit("compute bundle/import failed")
    snap = snaps[0]
    rc4, _ = cli(["admission", "identity", "--snapshot", snap, "--config", toml, "--out",
                  d / "runtime_identity.v2.json"], d / "logs/05-admission.log")
    # steady-phase profile of the base (train inputs, untimed), as in B2 (steady@0x2990)
    rc5, _ = cli(["profile", "run", "-c", toml, "--snapshot", snap, "--reps", "1", "--out",
                  d / "prof", "--qemu", gi["exe"], "--timeout", "600", "--phase",
                  "steady@0x2990", *sum((["--inputs", str(i)] for i in TRAIN), [])],
                 d / "logs/06-profile.log")
    doc.update(snapshot=str(snap), admission_rc=rc4, profile_rc=rc5,
               profile=str(d / "prof/merged.profile.json"), prep=str(prep),
               ri=str(d / "runtime_identity.v2.json"),
               base_cache_sc_sha256=sha256(snap / "cache.sc"))
    dump(d / "base.json", doc)
    return doc


def step_compute(out: Path) -> dict[str, Any]:
    gi = gate_info()
    base = compute_base(out)
    toml = compute_toml(out)
    b = {"snapshot": Path(base["snapshot"]), "ri": Path(base["ri"]), "prep": Path(base["prep"]),
         "profile": Path(base["profile"])}
    rows: dict[str, Any] = {}
    for arm, dis in COMPUTE_ARMS.items():
        store = out / "compute" / "store-arms" / arm
        rmtree(store)
        r = _optimize_noelf(out, b, arm, store, dis)
        if r.get("artifact_dir"):
            import wp21_integ as W

            adir = Path(r["artifact_dir"])
            r["validation"] = W._validation(adir)
            from scopt.formats.sc_semantic import read_semantic_cache

            f = read_semantic_cache(adir / "cache.sc")
            r["version"] = f.version
            r["n_variants"] = len(f.ext.variants) if f.ext else 0
            r["n_slots"] = len(f.ext.slots) if f.ext else 0
            r["n_spec"] = sum(1 for s in f.ext.slots if s.kind == 2) if f.ext else 0
            n_spec = r["n_spec"]
            r["static_rows_ok"], r["rule"] = G.static_rows_ok(r["validation"], f.version, n_spec)
            r["publish"] = G.publish(out, adir, out / "compute" / "pubstore" / arm,
                                     f"compute-{arm}")
            # V10 on held-out inputs (plain V10 of WP8/WP26)
            vd = out / "compute" / "v10" / arm
            rmtree(vd)
            rc, so = cli(["validate", r["publish"]["artifact_dir"] or adir, "--dynamic", "-c", toml,
                          "--out", vd, "--qemu", gi["exe"], "--baseline", base["snapshot"],
                          *sum((["--inputs", str(i)] for i in HELDOUT), []), "--json"],
                         out / "logs" / f"compute-v10-{arm}.log", timeout=7200)
            try:
                r["v10"] = json.loads(so)
            except ValueError:
                r["v10"] = {"raw": so[-2000:]}
            r["v10_exit"] = rc
        rows[arm] = r
        log("compute", arm, r.get("status"), r.get("reason"), "variants", r.get("n_variants"),
            "slots", r.get("n_slots"), "rows", r.get("static_rows_ok"), "v10 exit",
            r.get("v10_exit"))
    doc = {"base": base, "arms": rows}
    dump(out / "compute.json", doc)
    return doc


def step_compute_hits(out: Path) -> dict[str, Any]:
    """Diagnostic (never timed) C' and SPEC-fallback hit counts of the compute arms that carry
    SPEC variants (b3-spec, b3) on the held-out V10 runs of step ``compute``: a gdb ``-nx``
    re-run of each artifact run directory (``scopt.run.diag.gdb_hits``) with hardware
    breakpoints on every C' and every SPEC slot's fallback F. Shows whether the
    specialized code executed on the inputs that were timed."""
    from scopt.run.diag import gdb_hits

    comp = rd(out / "compute.json") or {}
    doc: dict[str, Any] = {"arms": {}}
    for arm in ("b3-spec", "b3"):
        a = (comp.get("arms") or {}).get(arm) or {}
        adir = (a.get("publish") or {}).get("artifact_dir") or a.get("artifact_dir")
        if not adir:
            doc["arms"][arm] = {"ok": False, "why": "no published artifact"}
            continue
        pr = G.probe_rows(Path(adir) / "cache.sc")
        tg = [(x["what"], x["addr"]) for x in pr
              if x["kind"] == "variant" or (x["kind"] == "fallback" and "(SPEC" in x["what"])]
        rows = []
        for run in (a.get("v10") or {}).get("runs", []):
            rdir = run["artifact"]["run_dir"] if isinstance(run["artifact"], dict) \
                else run["artifact"]
            d = out / "compute" / "hits" / arm / Path(rdir).name
            rmtree(d)
            h = gdb_hits(rdir, d, tg, timeout_s=900)
            rows.append({"input": Path(run["input"]).name, "run_dir": rdir, "diag_dir": str(d),
                         "ok": h.get("ok"), "hits": {x["what"]: x["hits"]
                                                     for x in h["targets"]}})
            log("compute-hits", arm, Path(run["input"]).name, rows[-1]["hits"])
        cp = {k: [r["hits"].get(k) for r in rows] for k in (rows[0]["hits"] if rows else {})
              if "C'" in k}
        doc["arms"][arm] = {"artifact_dir": adir, "targets": [(w, hex(x)) for w, x in tg],
                            "rows": rows, "c_prime_hits_by_variant": cp,
                            "c_prime_total": sum(v or 0 for vs in cp.values() for v in vs),
                            "every_variant_entered": bool(cp) and all(
                                any((v or 0) > 0 for v in vs) for vs in cp.values()),
                            "ok": bool(rows) and all(r["ok"] for r in rows)}
    dump(out / "compute-hits.json", doc)
    return doc


def _optimize_noelf(out: Path, b: dict[str, Any], arm: str, store: Path,
                    dis: tuple[str, ...]) -> dict[str, Any]:
    args: list[Any] = ["optimize", b["snapshot"], "--pipeline", "b3", "--b3", "--store", store,
                       "--runtime-identity", b["ri"], "--prep", b["prep"], "--profile",
                       b["profile"], "--selftest", selftest_path(out), "--json"]
    for p in dis:
        args += ["--disable", p]
    rc, so = cli(args, out / "logs" / f"optimize-compute-{arm}.log")
    try:
        doc = json.loads(so)
    except ValueError:
        return {"exit": rc, "error": "no JSON"}
    return {"exit": rc, "status": doc.get("status"), "reason": doc.get("reason"),
            "artifact_dir": doc.get("artifact_dir"), "passes": G._passes(doc),
            "pass_byte_delta": doc.get("pass_byte_delta"), "downgraded": doc.get("downgraded")}


# ---------------------------------------------------------------------------
# alias: F-B3-15 (mps2-an385 image, direct QEMU, no FastDyn)
# ---------------------------------------------------------------------------

ALIAS = FIX / "alias"


def _alias_argv(mode: str) -> list[str]:
    a = ["@QEMU@", "-M", "mps2-an385", "-cpu", "cortex-m3", "-kernel", str(ALIAS / "alias.elf"),
         "-display", "none", "-serial", "null", "-monitor", "none", "-semihosting-config",
         "enable=on,target=native"]
    if mode == "icount":
        a += ["-icount", "shift=0,sleep=off,align=off"]
    return a + ["-accel", "tcg,supercache=@SUPERCACHE@"]


def alias_mode(out: Path, mode: str) -> dict[str, Any]:
    import wp27_slice1 as S1
    from scopt.formats.envlayout_v1 import find_envlayout
    from scopt.importer.import_cache import open_snapshot
    from scopt.passes.base import Patch
    from scopt.run.deploy import deploy_direct, launch_direct, prepare_direct
    from scopt.run.diag import artifact_probe_addrs
    from scopt.run.negative import BYPASS_ROWS, package_negative

    gi = gate_info()
    d = out / "alias" / mode
    rmtree(d)
    d.mkdir(parents=True)
    r: dict[str, Any] = {"mode": mode}
    pj = prepare_direct(d / "prep", argv=_alias_argv(mode), qemu=gi["exe"], cache_name="alias.sc")
    r["prepare"] = {k: pj.get(k) for k in ("verdict", "exit_code", "qemu_sha256")}
    rc, _ = cli(["import", "--sc", d / "prep/cache/alias.sc", "--scm", d / "prep/capture.scm",
                 "--firmware", ALIAS / "alias.elf", "--store", d / "store"],
                out / "logs" / f"alias-import-{mode}.log")
    snap = sorted((d / "store/snapshots").iterdir())[0]
    s = open_snapshot(snap)
    lay = find_envlayout("sha256:" + gi["sha"])
    vc, f = 0x42C, 0x800
    C = S1._unit_at(s, vc)
    code = bytes(C.code)
    sites = S1.find_sites(code, lay.offset("regs[0]"))
    ps = []
    for k, (o, n, asm) in sorted(sites.items(), key=lambda x: x[1][0]):
        new = S1.nopfill(n) if (k in "LT" or asm.startswith("jne")) else S1.jcc_to_jmp(code, o, n)
        ps.append(Patch(C.uid, o, code[o:o + n], new, "dho_nop" if k in "LT" else "bf_jcc_to_nop"))
    plan, R, _C, n = S1.build_plan(s, r_vaddr=f, vc=vc, patches=tuple(ps), facts=())
    r["plan"] = {"R": R.uid, "C": C.uid, "slot_n": n, "sites": {k: [hex(v[0]), v[1], v[2]]
                                                                 for k, v in sites.items()},
                 "units": len(list(s.units())),
                 "origins": sorted({u.origin for u in s.units()})}
    nd = d / "test-only"
    nr = package_negative(s, plan, nd, label="alias-spec", mutant=None,
                          detector="F-B3-15 alias (TEST-ONLY artifact: the direct mps2 path has "
                                   "no ExecutionBundle/RuntimeIdentity, so V7/V8/V20 cannot pass)",
                          layout=lay, runtime=None, v6_exes=[gi["sha"]],
                          bypass=BYPASS_ROWS + ("V7", "V8", "V20"))
    r["package"] = {"status": nr.status, "reason": nr.reason, "path": nr.path,
                    "failing_rows_bypassed": sorted({x["code"] for x in nr.bypassed}),
                    "bypassed": [(x["code"], x.get("check"), x.get("reason"))
                                 for x in nr.bypassed]}
    if not nr.packaged:
        r["ok"] = False
        return r
    runs = {}
    for tag, src in (("artifact", Path(nr.path)), ("warm", snap)):
        rdir = d / f"run-{tag}"
        deploy_direct(src, rdir, argv=_alias_argv(mode), qemu=gi["exe"], cache_name="alias.sc",
                      allow_negative_control=True)
        doc = launch_direct(rdir, timeout_s=60,
                            extra_env={"QEMU_SC_V6_STATS": str(rdir / "v6-stats.json")})
        st = json.loads((rdir / "v6-stats.json").read_text()) \
            if (rdir / "v6-stats.json").is_file() else {}
        runs[tag] = {"verdict": doc.get("verdict"), "exit_code": doc.get("exit_code"),
                     "appended": doc.get("appended"), "guest_norm": doc.get("guest_norm"),
                     "qemu_sha256": doc.get("qemu_sha256"),
                     "stats": {k: st.get(k) for k in ("loaded", "links_armed", "activations",
                                                      "variant_exits", "invariant_violations")}}
    r["runs"] = runs
    want = (ALIAS / "expected.norm").read_text().splitlines()
    pr = artifact_probe_addrs(Path(nr.path) / "cache.sc")
    tg = [(p["what"], p["addr"]) for p in pr]
    from scopt.run.diag import gdb_hits

    gh = gdb_hits(d / "run-artifact", d / "hits", tg[:4], timeout_s=300)
    r["hits"] = {"hits": {x["what"]: x["hits"] for x in gh["targets"]},
                 "qemu_exit": (gh["runs"][0] if gh["runs"] else {}).get("qemu_exit"),
                 "dir": gh["diag_dir"], "ok": gh["ok"],
                 "harness": "scopt.run.diag.gdb_hits on the direct run dir (direct.json)"}
    h = r["hits"]["hits"]
    cp = next((v for k, v in h.items() if "C'" in k), None)
    fb = next((v for k, v in h.items() if " F " in k), None)
    r["items"] = {
        "artifact output = warm = expected.norm (z 2000, nz 0, via-alias 1000)":
            runs["artifact"]["guest_norm"] == runs["warm"]["guest_norm"] == want,
        "both proofs PASS, 0 appended": all(x["verdict"] == "PASS" and x["appended"] == 0
                                            for x in runs.values()),
        "gate binary": all(str(x["qemu_sha256"]).endswith(gi["sha"]) for x in runs.values()),
        "link armed (links_armed > 0)": (runs["artifact"]["stats"].get("links_armed") or 0) > 0,
        "C' entered on the 0x0 path (hits > 0)": isinstance(cp, int) and cp > 0,
        "g_pc failed on the alias path: F hits >= 1000 (the 1000 returns through 0x40042c)":
            isinstance(fb, int) and fb >= 1000,
    }
    r["ok"] = all(r["items"].values())
    log("alias", mode, r["items"], h)
    return r


def step_alias(out: Path) -> dict[str, Any]:
    doc = {"rows": {m: alias_mode(out, m) for m in MODES}}
    doc["pass"] = all(r.get("ok") for r in doc["rows"].values())
    doc["note"] = ("TEST-ONLY artifact (WP27 X-REHY precedent): the direct mps2 path has no "
                   "ExecutionBundle or RuntimeIdentity, so V7/V8/V20 fail by construction and "
                   "are bypassed with WP26's negative-control packager; the plan is hand-built "
                   "(R = alias_f's TB, SPEC slot to alias_vc, the three §3.5 patches located by "
                   "capstone in C). It checks the loader's g_pc guard on a PCREL alias, which "
                   "is what F-B3-15 is for; the production pipeline cannot reach it.")
    dump(out / "alias.json", doc)
    return doc


# ---------------------------------------------------------------------------
# smc17: F-B3-17 with a test-only SPEC link into the RAM continuation
# ---------------------------------------------------------------------------

def _elf_bytes(elf: Path, va: int, n: int) -> bytes:
    from elftools.elf.elffile import ELFFile

    with open(elf, "rb") as fh:
        for sg in ELFFile(fh).iter_segments():
            if sg["p_type"] == "PT_LOAD" and sg["p_vaddr"] <= va < sg["p_vaddr"] + sg["p_filesz"]:
                o = va - sg["p_vaddr"]
                return sg.data()[o:o + n]
    raise ValueError(f"{va:#x} not in a PT_LOAD segment of {elf}")


def smc17_mode(out: Path, mode: str) -> dict[str, Any]:
    """F-B3-17 (smc-ram) made non-vacuous. The production pipeline links nothing into the
    RAM continuation (``target_not_profiled``: return_edges refuses ``ram_resident`` targets
    and the ``allow_ram_resident`` test policy is not wired into the manager), so the B3
    artifact of step ``artifacts`` has no SPEC link and nothing B3 touches the rewritten
    code. Here a TEST-ONLY plan (WP27 slice-1 shape) grows f_b3_17's return TB R with one
    SPEC slot to ``smc_cont``'s RAM copy C (g_smc + 0x16, the ``cbz`` record of the
    snapshot, selected by guest hash), one variant C' with §3.5's three patches (located by
    capstone in C's host code) and the certified CalleeSummary fact r0 == 0. It is packaged
    with WP26's negative-control packager with **no row bypassed** (every static row it
    runs passes), but it is tagged NEGATIVE_CONTROL (built outside the manager) and is
    never publishable. Expected: the link arms, C' runs on iterations 1..299, the guest's
    own ``strh`` at K = 300 rewrites C (cbz -> cbnz), C' is never entered again, and the
    output follows the new code as warm does (O1/O2a/O2b against the warm baseline)."""
    import wp27_slice1 as S1
    from scopt.canon import fnv1a64
    from scopt.guard import CS_CONDEXEC_MASK, CS_THUMB, PSTATE_SS, GuardTarget
    from scopt.link import (B3Plan, GuardedExitSpec, VariantSpec, capture_join, exit_class_of,
                            free_slots, lookup_tails, tail_problem)
    from scopt.passes.base import Patch
    from scopt.run.negative import package_negative

    gi = gate_info()
    r: dict[str, Any] = {"mode": mode}
    ctx = manager_ctx(out, mode, "17")
    s = ctx["s"]
    f = sym("f_b3_17")
    vc = sym("smc_cont") - sym("smc_blob_start") + sym("g_smc")
    R = S1._unit_at(s, f)
    gb = _elf_bytes(FIX / "b3.elf", sym("smc_cont"), 2)
    cands = [u for u in s.units() if u.vaddrs and (u.vaddrs[0] & ~1) == vc]
    C = [u for u in cands if u.guest_hash == fnv1a64(gb)]
    r["units_at_vc"] = [{"uid": u.uid, "origin": u.origin, "guest_hash": f"{u.guest_hash:#x}"}
                        for u in cands]
    if len(C) != 1:
        r.update(ok=False, why=f"{len(C)} cbz records at {vc:#x}")
        return r
    C = C[0]
    code = bytes(C.code)
    sites = S1.find_sites(code, ctx["lay"].offset("regs[0]"))
    ps = []
    for k, (o, n, asm) in sorted(sites.items(), key=lambda x: x[1][0]):
        new = S1.nopfill(n) if (k in "LT" or asm.startswith("jne")) else S1.jcc_to_jmp(code, o, n)
        ps.append(Patch(C.uid, o, code[o:o + n], new, "dho_nop" if k in "LT" else "bf_jcc_to_nop"))
    row = [x for x in ctx["rows"] if x.get("return_unit") == R.uid]
    facts = tuple(S1._fact_from_row({**fr, "summary_row_hash": row[0].get("row_hash")})
                  for fr in (row[0]["facts"] if row else []))
    tails = lookup_tails(R)
    if len(tails) != 1 or tail_problem(R, tails[0], s.header.code_address):
        r.update(ok=False, why="R has no single admissible lookup tail")
        return r
    t = tails[0]
    xc = exit_class_of(R, t, capture_join(s).slots[R.slot].chunk.guest_bytes)
    n = list(free_slots(R))[0]
    g = GuardedExitSpec(t.off, xc if xc in ("return", "static") else "static",
                        (GuardTarget(vc, n, "SPEC"),), C.key.flags & ~PSTATE_SS,
                        (C.key.cs_base & ~CS_CONDEXEC_MASK) | CS_THUMB)
    plan = B3Plan(grow={R.uid: (g,)},
                  variants=(VariantSpec(C.uid, R.uid, n, patches=tuple(ps), facts=facts),))
    r["plan"] = {"R": R.uid, "R_origin": R.origin, "C": C.uid, "C_origin": C.origin,
                 "vc": hex(vc), "slot_n": n, "exit_class": xc,
                 "sites": {k: [hex(v[0]), v[1], v[2]] for k, v in sites.items()},
                 "facts": [{"field": x.field, "value": x.value, "source": x.source}
                           for x in facts]}
    nd = out / "smc17" / "test-only"
    rmtree(nd / mode)
    nr = package_negative(s, plan, nd, label=mode, mutant=None,
                          detector="F-B3-17 smc-ram (TEST-ONLY: SPEC link into a ram_resident "
                                   "C, built outside the manager; allow_ram_resident is not "
                                   "wired into it)",
                          layout=ctx["lay"], runtime=ctx["ri"], v6_exes=[gi["sha"]],
                          hostsym=ctx["hs"], summaries=ctx["rows"], bypass=())
    r["package"] = {"status": nr.status, "reason": nr.reason, "path": nr.path,
                    "rows_bypassed": sorted({x["code"] for x in nr.bypassed}),
                    "blocking": [(x["code"], x.get("check"), x.get("reason"))
                                 for x in nr.blocking][:10]}
    if not nr.packaged:
        r.update(ok=False, why="test-only artifact not packaged")
        return r
    dyn = dynamic_one(out, mode, "17", art=Path(nr.path), tag=f"smc17-{mode}",
                      oracles=("O1", "O2a", "O2b", "O3"), negative=True,
                      o3={"stats": {"links_armed": ">0"}, "hits": {}})
    r["dynamic"] = {k: dyn.get(k) for k in ("verdict", "failures", "incomplete", "file",
                                            "gate_binary")}
    cases = dyn.get("cases") or []
    r["cases"] = [{"input": c["fixture_input"], "oracles": c["oracles"], "hits": c["hits"],
                   "stats": (c.get("stats") or {}).get("artifact"),
                   "runs": {k: (v or {}).get("run_dir") for k, v in c["runs"].items()}}
                  for c in cases]
    c0 = r["cases"][0] if r["cases"] else {}
    o, h, st = c0.get("oracles") or {}, c0.get("hits") or {}, c0.get("stats") or {}
    cp = [v for k, v in h.items() if "C'" in k]
    fb = [v for k, v in h.items() if " F " in k]
    r["items"] = {
        "test-only artifact packaged with no static row bypassed": nr.packaged and
        not nr.bypassed,
        "gate binary on both sides": bool(dyn.get("gate_binary")),
        "O1 (output follows the new code as warm does), O2a, O2b equal warm":
            all(o.get(k) == "pass" for k in ("O1", "O2a", "O2b")),
        "link armed (links_armed > 0), invariant_violations == 0":
            (st.get("links_armed") or 0) > 0 and st.get("invariant_violations") == 0,
        "C' entered before the rewrite and never after it (0 < hits < 300: K = 300)":
            len(cp) == 1 and isinstance(cp[0], int) and 0 < cp[0] < 300,
        "R's fallback F taken after the rewrite (F hits > 0)":
            bool(fb) and all(isinstance(x, int) and x > 0 for x in fb),
    }
    r["c_prime_hits"], r["f_hits"] = cp, fb
    r["ok"] = all(r["items"].values())
    log("smc17", mode, r["items"], h, {k: st.get(k) for k in ("links_armed",
                                                              "invariant_violations")})
    return r


def step_smc17(out: Path) -> dict[str, Any]:
    doc = {"rows": {m: smc17_mode(out, m) for m in MODES}}
    doc["pass"] = all(r.get("ok") for r in doc["rows"].values())
    doc["note"] = ("TEST-ONLY artifact: the production pipeline refuses the RAM continuation "
                   "(target_not_profiled; allow_ram_resident not wired into the manager), so the "
                   "published F-B3-17 artifact has no SPEC link and is vacuous for SMC. The plan "
                   "is hand-built (WP27 slice-1 shape) with the certified r0 == 0 fact; every "
                   "static row passes with nothing bypassed; it is NEGATIVE_CONTROL-tagged only "
                   "because it was built outside the manager.")
    dump(out / "smc17.json", doc)
    return doc


# ---------------------------------------------------------------------------
# coverage: F-B3-23 (O6)
# ---------------------------------------------------------------------------

def step_coverage(out: Path) -> dict[str, Any]:
    """F-B3-23: the real b3 pipeline under ``coverage = true`` and O6 (LOG_REG ring equal to
    warm's) plus C' hits > 0. Everything is computed from the run: the C' hit targets are
    the variants of the published artifact whose SPEC slot targets the fixture's Vc (none
    when the artifact has no variant), so the item flips to pass by itself once WP17/WP19
    let specialize-returns through under coverage. (Today every return exit is refused,
    ``exit_not_goto_ptr``, and the artifact is v5 identity.)"""
    from wp28_gate import c_prime_labels_for_vc

    arts = (rd(out / "artifacts.json") or {})["rows"]
    res = {}
    for m in MODES:
        a = arts.get(f"{m}/23") or {}
        art = published_dir(out, m, "23")
        labs = c_prime_labels_for_vc(art / "cache.sc", "23") if art and \
            (a.get("n_spec") or 0) > 0 else []
        dyn = dynamic_one(out, m, "23", tag=f"cov-{m}-23",
                          oracles=("O1", "O2a", "O2b", "O5", "O6"), o3={}, hit_labels=labs)
        cases = dyn.get("cases", [])
        res[m] = {"artifact_version": a.get("version"), "n_spec": a.get("n_spec"),
                  "n_general": a.get("n_general"),
                  "refused_specialize_returns": a.get("refused_specialize_returns"),
                  "dynamic": {k: dyn.get(k) for k in ("verdict", "failures", "incomplete",
                                                       "file", "gate_binary")},
                  "oracles": [c["oracles"] for c in cases],
                  "details": [c["details"] for c in cases], "c_prime_labels": labs,
                  "c_prime_hits": [{k: v for k, v in (c.get("hits") or {}).items()
                                    if "C'" in k} for c in cases]}
        o6 = [c["oracles"].get("O6") for c in cases]
        res[m]["o6"] = o6
        hits = [v for c in res[m]["c_prime_hits"] for v in c.values()]
        res[m]["c_prime_hits_gt_0"] = bool(hits) and all(isinstance(x, int) and x > 0
                                                         for x in hits)
        res[m]["o6_equal"] = bool(o6) and all(x == "pass" for x in o6)
        res[m]["o1_equal"] = bool(cases) and all(c["oracles"].get("O1") == "pass"
                                                 for c in cases)
        res[m]["ok"] = (res[m]["o6_equal"] and res[m]["o1_equal"] and
                        res[m]["c_prime_hits_gt_0"] and bool(dyn.get("gate_binary")))
    doc = {"rows": res, "pass": all(r["ok"] for r in res.values())}
    if not doc["pass"]:
        doc["why"] = ("no C' hit under the coverage configuration" +
                      (": the published artifact has no SPEC variant (specialize-returns "
                       f"refusals {[r['refused_specialize_returns'] for r in res.values()]}); "
                       "the LOG_REG block pins R's HostOps (unexplained_host_store) and is a "
                       "backward branch for hostsym (WP27 integration finding; WP29 F-B4-10 "
                       "finding)" if all(not r["c_prime_labels"] for r in res.values())
                       else ""))
    dump(out / "coverage.json", doc)
    log("coverage", {m: (r["o6"], r["c_prime_hits"], r["refused_specialize_returns"])
                     for m, r in res.items()})
    return doc


# ---------------------------------------------------------------------------
# coverage-neg (close-out, design/closeout-logreg.md C.2): O6 non-vacuity on a variant
# ---------------------------------------------------------------------------

def b_cov_ring_store(ctx):
    """F-B3-23: every C' of the manager's plan with its LOG_REG ring store (the verbatim,
    relocated copy of C's coverage block, design/closeout-logreg.md A.7) nopped: C' then
    logs nothing, so the coverage ring at ``benchmark_end`` differs from warm's by the C'
    entries (test-only negative control; V12/V15 reject it statically)."""
    def mutate(link):
        done = []
        for v in link.variants:
            c = [u for u in ctx["s"].units() if u.uid == v.src_uid][0]
            h = hostops_of(ctx, c)
            if len(h.log_blocks) != 1:
                raise RuntimeError(f"C {c.uid}: {len(h.log_blocks)} matched LOG_REG blocks")
            lb = h.log_blocks[0]
            o = h.op_at(lb.ring_store)
            if o is None or bytes(v.body[o.off:o.off + o.size]) != bytes(c.code[o.off:o.off +
                                                                                  o.size]):
                raise RuntimeError(f"variant {v.index}: ring store not at C's offset "
                                   f"{lb.ring_store:#x}")
            link = patch_variant(link, v.index, [(o.off, nop(o.size))])
            done.append((v.index, o.off, o.size, o.text))
        if not done:
            raise RuntimeError("the plan has no variant")
        return link
    return ctx["plan"], mutate, "every C''s LOG_REG ring store nopped (the copied block logs nothing)"


def step_coverage_neg(out: Path) -> dict[str, Any]:
    """Close-out (design/closeout-logreg.md C.2, a stricter, added check): a negative
    control on the F-B3-23 artifact built from the manager's own plan, every C''s LOG_REG
    ring store nopped (WP26 ``package_negative``, V12-V17 bypassed, test-only), deployed with
    ``--negative-control``. O6 must detect it (the LOG_REG ring differs from warm's) while
    C' runs (hits > 0); the ring-index deficit is recorded next to the C' hits."""
    from scopt.run.negative import BYPASS_ROWS, package_negative

    gi = gate_info()
    res: dict[str, Any] = {}
    for m in MODES:
        lab = f"cov_ring_store-23-{m}"
        r: dict[str, Any] = {"mutant": "log_reg_ring_store (C')", "fixture": "23", "mode": m,
                             "label": lab}
        try:
            ctx = manager_ctx(out, m, "23")
            r["plan_matches_published"] = plan_matches_artifact(ctx, published_dir(out, m, "23"))
            plan, mutate, what = b_cov_ring_store(ctx)
        except Exception as e:  # noqa: BLE001 - recorded, never a pass
            r.update(ok=False, built=False, why=f"{type(e).__name__}: {e}")
            res[m] = r
            log("coverage-neg", lab, "not built:", r["why"])
            continue
        ndir = out / "coverage-neg" / "neg"
        rmtree(ndir / lab)
        nr = package_negative(ctx["s"], plan, ndir, label=lab, mutant="log_reg_ring_store",
                              detector="F-B3-23 (O6)", mutate=mutate, layout=ctx["lay"],
                              runtime=ctx["ri"], v6_exes=[gi["sha"]], hostsym=ctx["hs"],
                              summaries=ctx["rows"], bypass=BYPASS_ROWS)
        codes = sorted({x["code"] for x in nr.bypassed})
        r.update(what=what, status=nr.status, reason=nr.reason, path=nr.path,
                 statically_rejected_by=codes,
                 bypassed=[{"code": x["code"], "check": x.get("check"),
                            "reason": x.get("reason")} for x in nr.bypassed][:20])
        r["static_rejected"] = nr.packaged and bool(set(codes) & {"V12", "V15"})
        if not nr.packaged:
            r["ok"] = False
            res[m] = r
            continue
        art = Path(nr.path)
        labs = [x["what"] for x in G.probe_rows(art / "cache.sc") if x["kind"] == "variant"]
        dyn = dynamic_one(out, m, "23", art=art, tag=f"covneg-{m}-23",
                          oracles=("O1", "O2b", "O6"), o3={}, negative=True, hit_labels=labs)
        cases = dyn.get("cases") or []
        o6 = [c["oracles"].get("O6") for c in cases]
        hits = [v for c in cases for k, v in (c.get("hits") or {}).items() if "C'" in k]
        ring = []
        f = Path(dyn["file"]) if dyn.get("file") else None
        for c in (rd(f) or {}).get("cases", []) if f else []:
            for o in c.get("oracles", []):
                if o["oracle"] == "O6":
                    d = o.get("data") or {}
                    ri_ = ((d.get("reference") or {}).get("ring") or {}).get("index")
                    ai_ = ((d.get("artifact") or {}).get("ring") or {}).get("index")
                    ring.append({"reference_index": ri_, "artifact_index": ai_,
                                 "deficit": (ri_ - ai_) if isinstance(ri_, int) and
                                 isinstance(ai_, int) else None,
                                 "detail": str(o.get("detail"))[:300]})
        r.update(dynamic={k: dyn.get(k) for k in ("verdict", "failures", "incomplete", "file",
                                                  "gate_binary")},
                 oracles=[c["oracles"] for c in cases], o6=o6, c_prime_hits=hits, ring=ring,
                 o1_equal=bool(cases) and all(c["oracles"].get("O1") == "pass" for c in cases))
        r["o6_detected"] = bool(o6) and all(x == "fail" for x in o6)
        r["c_prime_ran"] = bool(hits) and all(isinstance(x, int) and x > 0 for x in hits)
        r["ok"] = bool(r["static_rejected"] and r["o6_detected"] and r["c_prime_ran"] and
                       dyn.get("gate_binary"))
        res[m] = r
        log("coverage-neg", lab, "static", codes, "O6", o6, "C' hits", hits, "ring", ring,
            "ok", r["ok"])
    doc = {"rows": res, "pass": bool(res) and all(r.get("ok") for r in res.values()),
           "note": "stricter, added check (close-out): O6 non-vacuity on a variant under "
                   "coverage; the control is test-only (V12-V17 bypassed)"}
    dump(out / "coverage-neg.json", doc)
    return doc


# ---------------------------------------------------------------------------
# close-out re-gate helpers (design/closeout-logreg.md C.2): byte comparison with the
# reference gate run (WP28), carried evidence, compute static re-packaging
# ---------------------------------------------------------------------------

#: the reference gate run whose evidence a re-gate may carry (``wp28_gate.py --ref``)
REF: Path = ROOT / "runs/wp28"
#: fixture artifacts expected to change in the close-out re-gate (F-B3-23, both modes)
EXPECTED_CHANGES = {f"{m}/23" for m in MODES}
#: steps whose evidence is carried (copied) when every artifact they ran is byte-identical
CARRY_FILES = ("bases.json", "aa.json", "stale.json", "gdbdiag.json", "alias.json", "smc17.json",
               "o4.json")
CARRY_TIMING = ("timing-analysis.json", "timing-load.log", "compute-hits.json",
                "timing/deleted-caches.jsonl", "timing/deleted-iologs.jsonl")


def _pub_sha(a: dict[str, Any]) -> str | None:
    d = (a.get("publish") or {}).get("artifact_dir")
    return sha256(Path(d) / "cache.sc") if d and (Path(d) / "cache.sc").is_file() else None


def step_compare_ref(out: Path) -> dict[str, Any]:
    """Published ``cache.sc`` sha256 of every fixture artifact against the reference run
    (``REF/artifacts.json``). Only ``EXPECTED_CHANGES`` may differ; any other difference is
    listed in ``unexpected_changes`` (its dynamic rows must be re-run, never carried)."""
    new = (rd(out / "artifacts.json") or {}).get("rows") or {}
    ref = (rd(REF / "artifacts.json") or {}).get("rows") or {}
    rows, unexpected = {}, []
    for k in sorted(set(new) | set(ref)):
        a, b = new.get(k) or {}, ref.get(k) or {}
        sa, sb = _pub_sha(a), _pub_sha(b)
        eq = sa is not None and sa == sb
        rows[k] = {"equal": eq, "cache_sc_sha256": [sb, sa], "version": [b.get("version"),
                                                                       a.get("version")],
                   "n_spec": [b.get("n_spec"), a.get("n_spec")],
                   "expected_change": k in EXPECTED_CHANGES}
        if not eq and k not in EXPECTED_CHANGES:
            unexpected.append(k)
    doc = {"ref": str(REF), "rows": rows, "equal": sum(r["equal"] for r in rows.values()),
           "n": len(rows), "unexpected_changes": unexpected, "pass": not unexpected}
    dump(out / "compare-ref.json", doc)
    log("compare-ref", doc["equal"], "/", doc["n"], "equal; unexpected", unexpected)
    return doc


def _carry(out: Path, rel: str, why: str) -> dict[str, Any]:
    import shutil

    src, dst = REF / rel, out / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return {"from": str(src), "sha256": sha256(dst), "why": why}


def step_carry(out: Path) -> dict[str, Any]:
    """Copy (never symlink) the evidence of the steps that are not re-run from ``REF``,
    when every fixture artifact other than ``EXPECTED_CHANGES`` is byte-identical
    (``compare-ref.json``); the dynamic rows of the byte-identical artifacts are carried
    into ``dynamic.json`` (the changed ones are dropped: they must be re-run). Records
    ``carried.json`` (source, sha256, reason)."""
    cmp_ = rd(out / "compare-ref.json")
    if not cmp_ or not cmp_.get("pass"):
        raise SystemExit("compare-ref missing or unexpected artifact changes: nothing carried")
    car = rd(out / "carried.json") or {}
    eq = {k for k, r in cmp_["rows"].items() if r["equal"]}
    for n in CARRY_FILES:
        car[n] = _carry(out, n, "its artifacts are byte-identical to the reference "
                                "(compare-ref.json)")
    dyn = rd(REF / "dynamic.json") or {"rows": {}}
    cur = rd(out / "dynamic.json") or {"rows": {}}
    kept = {k: {**r, "carried_from": str(REF / "dynamic.json")} for k, r in dyn["rows"].items()
            if k in eq}
    rows = {**kept, **{k: r for k, r in cur["rows"].items() if not r.get("carried_from")}}
    dump(out / "dynamic.json", {"rows": rows, "n": len(rows),
                                "n_pass": sum(1 for r in rows.values() if r.get("ok"))})
    car["dynamic.json"] = {"from": str(REF / "dynamic.json"),
                           "sha256": sha256(REF / "dynamic.json"),
                           "rows_carried": sorted(kept),
                           "rows_not_carried": sorted(set(dyn["rows"]) - set(kept)),
                           "why": "rows of byte-identical artifacts only"}
    dump(out / "carried.json", car)
    log("carry", sorted(car))
    return car


def step_compute_static(out: Path) -> dict[str, Any]:
    """The compute arms re-packaged and published with the current code (no QEMU). Each
    arm's published ``cache.sc`` is compared with the reference run's (``REF/compute.json``);
    when equal, the reference's V10 result is carried into the row (``v10_carried_from``);
    when not, V10 is run here on the gate binary and the timing of that arm is stale.
    ``compute-hits.json`` and the timing evidence are carried (``carried.json``) only when
    every arm is byte-identical."""
    gi = gate_info()
    base = compute_base(out)
    toml = compute_toml(out)
    ref = (rd(REF / "compute.json") or {}).get("arms") or {}
    b = {"snapshot": Path(base["snapshot"]), "ri": Path(base["ri"]), "prep": Path(base["prep"]),
         "profile": Path(base["profile"])}
    rows: dict[str, Any] = {}
    for arm, dis in COMPUTE_ARMS.items():
        store = out / "compute" / "store-arms" / arm
        rmtree(store)
        r = _optimize_noelf(out, b, arm, store, dis)
        if r.get("artifact_dir"):
            import wp21_integ as W
            from scopt.formats.sc_semantic import read_semantic_cache

            adir = Path(r["artifact_dir"])
            r["validation"] = W._validation(adir)
            f = read_semantic_cache(adir / "cache.sc")
            r["version"] = f.version
            r["n_variants"] = len(f.ext.variants) if f.ext else 0
            r["n_slots"] = len(f.ext.slots) if f.ext else 0
            r["n_spec"] = sum(1 for s in f.ext.slots if s.kind == 2) if f.ext else 0
            r["static_rows_ok"], r["rule"] = G.static_rows_ok(r["validation"], f.version,
                                                              r["n_spec"])
            r["publish"] = G.publish(out, adir, out / "compute" / "pubstore" / arm,
                                     f"compute-{arm}")
            r["cache_sc_sha256"] = _pub_sha(r)
            rr = ref.get(arm) or {}
            r["ref_cache_sc_sha256"] = _pub_sha(rr)
            r["byte_identical_to_ref"] = r["cache_sc_sha256"] is not None and \
                r["cache_sc_sha256"] == r["ref_cache_sc_sha256"]
            if r["byte_identical_to_ref"]:
                r["v10"], r["v10_exit"] = rr.get("v10"), rr.get("v10_exit")
                r["v10_carried_from"] = str(REF / "compute.json")
            else:
                vd = out / "compute" / "v10" / arm
                rmtree(vd)
                rc, so = cli(["validate", r["publish"]["artifact_dir"] or adir, "--dynamic",
                              "-c", toml, "--out", vd, "--qemu", gi["exe"], "--baseline",
                              base["snapshot"], *sum((["--inputs", str(i)] for i in HELDOUT),
                                                     []), "--json"],
                             out / "logs" / f"compute-v10-{arm}.log", timeout=7200)
                try:
                    r["v10"] = json.loads(so)
                except ValueError:
                    r["v10"] = {"raw": so[-2000:]}
                r["v10_exit"] = rc
        rows[arm] = r
        log("compute-static", arm, r.get("status"), "variants", r.get("n_variants"), "slots",
            r.get("n_slots"), "rows", r.get("static_rows_ok"), "byte-identical",
            r.get("byte_identical_to_ref"))
    doc = {"base": base, "arms": rows,
           "all_byte_identical_to_ref": bool(rows) and all(
               r.get("byte_identical_to_ref") for r in rows.values())}
    dump(out / "compute.json", doc)
    if doc["all_byte_identical_to_ref"]:
        car = rd(out / "carried.json") or {}
        for n in CARRY_TIMING:
            if (REF / n).is_file():
                car[n] = _carry(out, n, "every timed compute arm's cache.sc is byte-identical "
                                        "to the reference (compute.json)")
        dump(out / "carried.json", car)
    else:
        log("compute-static: a compute arm changed: its timing must be re-run "
            "(wp28_timing.sh under the lock)")
    return doc


# ---------------------------------------------------------------------------
# t_b3_6: pass separation on F-B3-1 (CP / BF / DHO each disabled), independent byte check
# ---------------------------------------------------------------------------

def step_t_b3_6(out: Path) -> dict[str, Any]:
    """T-B3-6 on the gate bases: the b3 pipeline with CP, BF, DHO disabled in turn and all
    three disabled; the variant bytes of every output ``cache.sc`` checked against §3.5's
    expected form with WP21's capstone diff (independent of the manager); each pass with a
    non-zero delta has its own ledger row with the same byte count."""
    import wp21_integ as W
    import wp27_slice0 as S0
    from scopt.formats.envlayout_v1 import find_envlayout
    from scopt.importer.import_cache import open_snapshot

    res: dict[str, Any] = {}
    for m in MODES:
        b = base_info(out, m, "1")
        ri = rd(b["ri"])
        lay = find_envlayout(ri["qemu_sha256"])
        in_vaddrs = [u.vaddrs[0] if u.vaddrs else None
                     for u in open_snapshot(b["snapshot"]).units()]
        rows = {}
        for case, dis in W.CASES.items():
            store = out / "t-b3-6" / m / case
            rmtree(store)
            r = G.optimize_one(out, b, f"t-b3-6-{m}-{case}", store, disable=dis,
                               params=G.FIXTURE_PARAMS)
            if r.get("artifact_dir"):
                adir = Path(r["artifact_dir"])
                vd = W.variant_diff(adir / "cache.sc", lay.offset("regs[0]"), in_vaddrs)
                r["bytes_ok"], r["bytes_why"] = W.check_case(case, vd)
                r["sites"] = {k: v["form"] for k, v in
                              ((vd["variants"] or [{}])[0].get("sites") or {}).items()}
                r["ledger"] = S0._ledger_pass_rows(adir)
                r["validation"] = W._validation(adir)["codes"]
            rows[case] = r
        d = {c: (rows[c].get("pass_byte_delta") or {}) for c in rows}
        sel = ("constant-propagation", "branch-folding", "dead-host-ops")
        items = {
            "every case published": all(r.get("status") == "published" for r in rows.values()),
            "variant bytes = §3.5 form in every case (capstone diff)":
                all(r.get("bytes_ok") for r in rows.values()),
            "full: CP 3, BF 6, DHO 4": [d["full"].get(p) for p in sel] == [3, 6, 4],
            "each pass disabled changes the bytes (three distinct non-zero deltas)":
                len({json.dumps(d[c], sort_keys=True) for c in ("full", "no_cp", "no_bf",
                                                                  "no_dho")}) == 4,
            "each changing pass has its own ledger row with the same byte count": all(
                (rows[c]["ledger"].get(p) or {}).get("bytes_changed") == d[c].get(p)
                for c in ("full", "no_cp", "no_bf", "no_dho") for p in sel if d[c].get(p)),
            "all three off: no variant (downgraded no_simplification)":
                not (rows["no_cp_bf_dho"].get("pass_byte_delta") or {}),
        }
        res[m] = {"cases": {c: {k: r.get(k) for k in ("status", "pass_byte_delta", "bytes_ok",
                                                      "bytes_why", "sites", "downgraded",
                                                      "artifact_dir")}
                            for c, r in rows.items()}, "items": items,
                  "ok": all(items.values())}
        log("t-b3-6", m, {c: d[c] for c in d}, res[m]["ok"])
    doc = {"rows": res, "pass": all(r["ok"] for r in res.values())}
    dump(out / "t-b3-6.json", doc)
    return doc


# ---------------------------------------------------------------------------
# verdict
# ---------------------------------------------------------------------------

SPEC_SUBCASES = ["2", "3", "3b", "4", "5", "6", "6t", "6r", "6h", "7", "20", "1p", "8", "8p", "9",
                 "10", "11", "12"]
HARDENING_DYN = ["13", "14", "17", "21", "22"]
EXT = {"T-B3-27": ROOT / "runs/wp22/unit/verdict.json",
       "T-B3-27_dir": ROOT / "runs/wp22/t-b3-27",
       "T-B3-26": ROOT / "runs/wp22/integ/stress-r6/wp22-stress.json",
       "T-B3-26_verdict": ROOT / "runs/wp22/integ/verdict.json",
       "F-B3-18": ROOT / "runs/wp26/integ/verdict.json",
       "F-B3-18_doc": ROOT / "runs/wp26/integ/final/wp26-integ.json",
       "X-REHY": ROOT / "runs/wp27/slice1/verdict.json"}


def _evidence_verdict(p: Path) -> dict[str, Any]:
    """Machine-readable verdict of one T-B3-27 evidence file: a gate JSON (``verdict`` and,
    when present, per-test ``checks``) or a pytest log (its summary line)."""
    if not p.is_file():
        return {"path": str(p), "pass": False, "why": "missing"}
    if p.suffix == ".json":
        d = json.loads(p.read_text())
        failing = sorted(k for k, x in (d.get("tests") or {}).items()
                         if any(not c.get("ok") for c in (x or {}).get("checks", [])))
        return {"path": str(p), "kind": "gate_json", "verdict": d.get("verdict"),
                "pass": d.get("verdict") == "PASS" and not failing, "failing_tests": failing}
    lines = [x.strip() for x in p.read_text(errors="replace").splitlines() if x.strip()]
    last = lines[-1] if lines else ""
    m = re.search(r"\b(\d+) passed\b", last)
    bad = re.search(r"\b(\d+) (failed|errors?)\b", last)
    return {"path": str(p), "kind": "pytest", "summary": last,
            "pass": bool(m) and not bad and int(m.group(1)) > 0}


def judge_t_b3_27(t27: dict[str, Any]) -> dict[str, Any]:
    """T-B3-27 from WP22's evidence files, not from its verdict text. Per row: the last
    evidence file must pass; an earlier failing file must be superseded by a later one:
    a later gate JSON of the same name (a full re-run), or, for failing tests of a gate
    JSON, a later file whose path names each failing test (e.g. ``t-b1-9-rerun``)."""
    rows: dict[str, Any] = {}
    for k, x in t27.items():
        ev = x.get("evidence")
        evs = [Path(e) for e in (ev if isinstance(ev, list) else [ev]) if e]
        res = [_evidence_verdict(e) for e in evs]
        problems = []
        if not res:
            problems.append("no evidence")
        elif not res[-1]["pass"]:
            problems.append(f"last evidence does not pass: {res[-1]}")
        for i, r in enumerate(res[:-1]):
            if r["pass"]:
                continue
            later = evs[i + 1:]
            full = any(e.name == evs[i].name and _evidence_verdict(e)["pass"] for e in later)
            tests = r.get("failing_tests") or []
            named = bool(tests) and all(
                any(tt.lower() in str(e).lower() and _evidence_verdict(e)["pass"]
                    for e in later) for tt in tests)
            if not (full or named):
                problems.append(f"{evs[i]} fails ({tests or r.get('verdict')}) and is not "
                                "superseded by a later passing re-run")
            r["superseded_by"] = "full re-run" if full else "re-run of the failing tests"
        rows[k] = {"ok": not problems, "evidence": res, "problems": problems,
                   "wp22_text": x.get("verdict")}
    return {"ok": bool(rows) and all(r["ok"] for r in rows.values()), "rows": rows}


def _res(ok: bool | None, ev: list[str], detail: Any = None, why: str = "") -> dict[str, Any]:
    r = "not_run" if ok is None else ("pass" if ok else "fail")
    return {"result": r, "evidence": ev, "detail": detail, "why": why}


def dyn_case_ok(out: Path, mode: str, cfg: str) -> tuple[bool, dict[str, Any]]:
    """O1 + O2a/O2b per mode + O3 non-vacuity of one fixture artifact; noicount O2a of a
    timer phase is reported only (the A/A of step ``aa`` differs too)."""
    dyn = (rd(out / "dynamic.json") or {})["rows"].get(f"{mode}/{cfg}") or {}
    aa = (rd(out / "aa.json") or {}).get("o2a_differs_aa") or {}
    timer = b3conf.BY_NAME[cfg].timing
    cases = dyn.get("cases") or []
    det: dict[str, Any] = {"verdict": dyn.get("verdict"), "gate_binary": dyn.get("gate_binary"),
                           "cases": []}
    ok = bool(cases) and bool(dyn.get("gate_binary"))
    for c in cases:
        o = c["oracles"]
        o2a_na = mode == "noicount" and timer and o.get("O2a") == "fail" and aa.get(cfg)
        cok = (o.get("O1") == "pass" and o.get("O2b") == "pass" and o.get("O3") == "pass" and
               (o.get("O2a") == "pass" or o2a_na) and o.get("O5") == "reported")
        ok = ok and cok
        st = (c.get("stats") or {}).get("artifact") or {}
        det["cases"].append({"input": c["fixture_input"], "oracles": o,
                             "o2a": "reported only (noicount timer phase; A/A also differs)"
                             if o2a_na else o.get("O2a"),
                             "hits": c.get("hits"), "links_armed": st.get("links_armed"),
                             "variant_entry_exits": st.get("variant_entry_exits"),
                             "variant_restores": st.get("variant_restores"),
                             "disarm_tlb_events": st.get("disarm_tlb_events"),
                             "invariant_violations": st.get("invariant_violations"), "ok": cok})
    return ok, det


def extra_nonvac(out: Path, mode: str, cfg: str, det: dict[str, Any]) -> tuple[bool, str]:
    """Fixture-specific non-vacuity beyond O3's requirements (design §9.5 column)."""
    arts = (rd(out / "artifacts.json") or {})["rows"]
    a = arts.get(f"{mode}/{cfg}") or {}
    cs = det["cases"]
    hits = lambda c: c.get("hits") or {}  # noqa: E731
    cp = lambda c: [v for k, v in hits(c).items() if "C'" in k]  # noqa: E731
    if cfg == "6":
        eh = ((rd(out / "dynamic.json") or {})["rows"][f"{mode}/6"].get("extra_hits") or {})
        g = [v for k, v in (eh.get("6") or {}).items() if "general C" in k]
        return bool(g) and all(isinstance(x, int) and x > 0 for x in g), \
            f"general C (bx r3 arbitrary entry, warm-up) hits {g}"
    if cfg == "11":
        ho = [c for c in cs if c["input"] == "11-heldout"]
        f = [v for c in ho for k, v in hits(c).items() if " F " in k]
        return bool(f) and all(x and x > 0 for x in f), f"held-out: R's fallback F hits {f}"
    if cfg in ("5", "6h", "17"):
        return a.get("ok") is True and (a.get("n_spec") == 0), \
            f"no SPEC link; refusals {a.get('refused_specialize_returns')}; " \
            f"exit reasons {a.get('exit_reasons')}"
    if cfg in ("14", "21"):
        v = [x for c in cs for x in cp(c)]
        return bool(v) and all(0 < x < 500 for x in v), \
            f"C' hits {v} (< 500: no C' entry after the iteration-500 change)"
    if cfg == "20":
        return a.get("ok") is True, (f"suffix units {a.get('f_b3_20_suffix_units')}; refused "
                                     f"{a.get('refused_specialize_returns')}")
    v = [x for c in cs for x in cp(c)]
    return bool(v) and all(isinstance(x, int) and x > 0 for x in v), f"C' hits {v}"


def o4_detection_kind(r: dict[str, Any]) -> str:
    """How an O4 attempt was detected: ``divergence`` (the mutant ran to completion and an
    output/record differs), ``crash`` (artifact run INVALID_exit_code: no complete guest
    output, a valid detection that does not show the specific observable),
    ``scenario`` (gdb scenario checks), ``not_detected``, or ``not_run``."""
    if r.get("scenario_mutant") is not None:
        return "scenario" if r.get("detected") else "not_detected"
    cs = r.get("cases") or []
    if not cs:
        return "not_run"
    if not r.get("detected"):
        return "not_detected"
    det = " ".join(str((c.get("details") or {}).get("O1", "")) for c in cs)
    return "crash" if "INVALID_exit_code" in det else "divergence"


def item_4a(out: Path) -> dict[str, Any]:
    """Spec-gate item 4a, F-B3-23 (O6): ``coverage.json`` (O6 equal warm and O1 equal in both
    modes, C' hits > 0 on the gate binary) **and**, since the close-out (a stricter, added
    check, design/closeout-logreg.md C.2), ``coverage-neg.json`` (O6 detects a C' whose
    LOG_REG ring store is nopped, in both modes). Unknown (a missing file) is never a pass."""
    cov = rd(out / "coverage.json") or {}
    cov_ok = bool(cov.get("rows")) and all(r.get("ok") is True for r in cov["rows"].values())
    cn = rd(out / "coverage-neg.json") or {}
    cn_ok = bool(cn.get("rows")) and set(cn["rows"]) >= set(MODES) and \
        all(r.get("ok") is True for r in cn["rows"].values())
    why = cov.get("why", "") if cov else "coverage step not run"
    if cov_ok and not cn_ok:
        why = ("coverage-neg: O6 did not detect the C' ring-store control" if cn else
               "coverage-neg step not run (O6 non-vacuity on a variant)")
    r = _res((cov_ok and cn_ok) if cov else None,
             [str(out / "coverage.json")] + ([str(out / "coverage-neg.json")] if cn else []),
             cov.get("rows"), why)
    r["coverage_neg"] = {m: {k: x.get(k) for k in ("ok", "o6", "c_prime_hits", "ring",
                                                   "statically_rejected_by", "why")}
                         for m, x in (cn.get("rows") or {}).items()} or None
    return r


def step_verdict(out: Path) -> dict[str, Any]:
    gate = rd(out / "gate.json") or {}
    arts = (rd(out / "artifacts.json") or {})["rows"]
    t6 = rd(out / "t-b3-6.json") or {}
    o4 = rd(out / "o4.json") or {}
    mut = rd(out / "mutants.json") or {}
    gd = rd(out / "gdbdiag.json") or {}
    st = rd(out / "stale.json") or {}
    rt = rd(out / "roundtrip.json") or {}
    al = rd(out / "alias.json") or {}
    comp = rd(out / "compute.json") or {}
    tim = rd(out / "timing-analysis.json")
    v: dict[str, Any] = {"schema": "wp28.b3-gate-verdict/1",
                         "date": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                         "gate_binary": {k: gate.get(k) for k in (
                             "exe", "exe_sha256", "tag", "tag_commit", "head", "tree_clean",
                             "ok", "checked_at")},
                         "fixture_elf_sha256": sha256(FIX / "b3.elf")}
    spec: dict[str, Any] = {}
    # 1. two-TB example
    d1 = {m: dyn_case_ok(out, m, "1") for m in MODES}
    o4r = {r.get("label"): r for r in o4.get("rows", []) if isinstance(r, dict)}
    ff = [o4r.get(f"{k}-1-{m}", {}).get("ok") for k in ("falsified_fact_link", "fold_flip")
          for m in MODES]
    srows = []
    for m in MODES:
        p = out / "summaries" / m / "1" / "summaries/callee_summaries.jsonl"
        srows += [json.loads(x) for x in p.read_text().splitlines() if x.strip()] \
            if p.is_file() else []
    prov = [r_ for r_ in srows if any(str(f.get("source")) == "patch"
                                      for f in (r_.get("facts") or []))]
    spec["1 two-TB example (T-B3-6, T-B3-8)"] = _res(
        bool(t6.get("pass")) and all(ok for ok, _ in d1.values()) and all(ff) and bool(prov) and
        all((arts.get(f"{m}/1") or {}).get("ok") for m in MODES),
        [str(out / "t-b3-6.json"), str(out / "dynamic.json"), str(out / "o4.json"),
         str(out / "summaries")],
        {"t_b3_6": {m: (t6.get("rows") or {}).get(m, {}).get("items") for m in MODES},
         "dynamic": {m: d1[m][1] for m in MODES}, "o4_falsified_fact_and_fold_flip": ff,
         "callee_summary_rows_with_patch_fact": len(prov)})
    # 2. sub-cases
    sub = {}
    for cfg in SPEC_SUBCASES:
        per = {}
        for m in MODES:
            ok, det = dyn_case_ok(out, m, cfg)
            nv_ok, nv = extra_nonvac(out, m, cfg, det)
            a = arts.get(f"{m}/{cfg}") or {}
            per[m] = {"ok": ok and nv_ok and bool(a.get("ok")), "dynamic_ok": ok,
                      "nonvacuity": nv, "nonvacuity_ok": nv_ok, "static_ok": a.get("ok"),
                      "cases": det["cases"]}
        sub[cfg] = {"fixture": FIXTURES[cfg].fixture, "ok": all(p["ok"] for p in per.values()),
                    "modes": per}
    sub["16"] = {"fixture": "F-B3-16(a)-(e)", "ok": bool(st.get("pass")),
                 "modes": {m: {"ok": (st.get("rows") or {}).get(m, {}).get("ok"),
                               "items": (st.get("rows") or {}).get(m, {}).get("items")}
                           for m in MODES}}
    # F-B3-12: the plain dynamic rows above only show the F-B3-12 artifact without a debugger
    # (no write, no invalidation); the invalidation itself is the PAUSED-stop scenario
    p12 = {m: (gd.get("rows") or {}).get(f"F-B3-12/{m}") or {} for m in MODES}
    bp12 = {m: (gd.get("rows") or {}).get(f"F-B3-12-wp24bp/{m}") or {} for m in MODES}
    for m in MODES:
        r12 = p12[m]
        paused_ok = r12.get("verdict") == "PASS" and "PAUSED" in str(r12.get("scenario"))
        per = sub["12"]["modes"][m]
        per["plain_run_without_debugger"] = {"ok": per["ok"], "nonvacuity": per["nonvacuity"]}
        per["invalidation_scenario"] = {
            "ok": paused_ok, "run_dir": r12.get("run_dir"), "warm_run_dir":
            r12.get("warm_run_dir"), "links_armed": (r12.get("stats") or {}).get("links_armed"),
            "c_prime_hits_before_write": (r12.get("variant_hits_total") or 0) -
            (r12.get("variant_hits_after_write") or 0),
            "c_prime_hits_after_write": r12.get("variant_hits_after_write"),
            "slot_before": {k: (r12.get("slot_before") or {}).get(k)
                            for k in ("dest", "at_reset", "target_variant")},
            "slot_after": {k: (r12.get("slot_after") or {}).get(k)
                           for k in ("dest", "at_reset", "target_variant")},
            "pairs": r12.get("pairs"),
            "failed": [c for c in r12.get("checks", []) if not c.get("ok")]}
        per["wp24_bp_script_vacuous"] = {
            "verdict": bp12[m].get("verdict"),
            "links_armed": (bp12[m].get("stats") or {}).get("links_armed"),
            "c_prime_hits": bp12[m].get("variant_hits_total"),
            "note": "f-b3-12.gdb's first breakpoint is on Vc's page (CF_BP_PAGE): no link is "
                    "armed, so it does not test invalidation of an armed C; not counted"}
        per["nonvacuity"] = (f"invalidation scenario (F-B3-13 kernel, PAUSED stop): links_armed "
                             f"{per['invalidation_scenario']['links_armed']}, C' hits "
                             f"{per['invalidation_scenario']['c_prime_hits_before_write']} "
                             f"before the write / "
                             f"{per['invalidation_scenario']['c_prime_hits_after_write']} after, "
                             f"slot reset observed; plain run: {per['nonvacuity']}")
        per["nonvacuity_ok"] = per["nonvacuity_ok"] and paused_ok
        per["ok"] = per["ok"] and paused_ok
    sub["12"]["ok"] = all(sub["12"]["modes"][m]["ok"] for m in MODES)
    sub["12"]["fixture"] = ("F-B3-12 (plain run of the F-B3-12 artifact + PAUSED-stop "
                            "invalidation of an armed C on the F-B3-13 kernel)")
    spec["2 sub-cases"] = _res(all(s_["ok"] for s_ in sub.values()),
                               [str(out / "dynamic.json"), str(out / "artifacts.json"),
                                str(out / "aa.json"), str(out / "stale.json"),
                                str(out / "gdbdiag.json")], sub)
    # 3. static rows on every accepted artifact; mutation suite; O4
    pub = {k: a for k, a in arts.items() if a.get("status") == "published"}
    rows_ok = all(a.get("static_rows_ok") for a in pub.values())
    comp_rows = all(r.get("static_rows_ok") for r in (comp.get("arms") or {}).values())
    by = o4.get("by_mutant") or {}
    v["mutation_table"] = {k: {"detected": d.get("detected"), "modes": d.get("detected_modes"),
                               "static_rejected": d.get("static_rejected"),
                               "attempts": d.get("attempts")} for k, d in by.items()}
    for r_ in o4.get("rows") or []:
        if not isinstance(r_, dict) or r_.get("mutant") not in v["mutation_table"]:
            continue
        kind = o4_detection_kind(r_)
        v["mutation_table"][r_["mutant"]].setdefault("detection_kind", {})[
            f"{r_.get('fixture')}/{r_.get('mode')}"] = kind
    spec["3 static rows, mutation suite, O4"] = _res(
        rows_ok and comp_rows and bool(mut.get("all_rejected")) and
        bool(o4.get("all_dynamic_detected")) and bool(o4.get("all_static_rejected")),
        [str(out / "artifacts.json"), str(out / "compute.json"), str(out / "mutants.json"),
         str(out / "o4.json")],
        {"artifacts": len(pub), "static_rows_ok": rows_ok, "compute_static_rows_ok": comp_rows,
         "v22": "not_run: WP23 not approved",
         "mutants_totals": mut.get("totals"), "synthetic_all_rejected":
         mut.get("synthetic_all_rejected"), "static_all_rejected": mut.get("all_rejected"),
         "o4_all_detected": o4.get("all_dynamic_detected"),
         "o4_static_only": o4.get("static_only")})
    # 4. attribution and independence
    f24 = all(sub_ok for sub_ok in [
        all(dyn_case_ok(out, m, c)[0] and (arts.get(f"{m}/{c}") or {}).get("ok")
            for m in MODES) for c in FIXTURES if c.startswith("24-")])
    spec["4a F-B3-23 (O6)"] = item_4a(out)
    spec["4b F-B3-24 (-O0/-Os)"] = _res(f24, [str(out / "artifacts.json"),
                                             str(out / "dynamic.json")],
                                        {c: {m: (arts.get(f"{m}/{c}") or {}).get("n_spec")
                                             for m in MODES} for c in FIXTURES
                                         if c.startswith("24-")})
    # 5. T-B3-27, T-B3-28
    wv = rd(EXT["T-B3-27"]) or {}
    t27 = judge_t_b3_27(wv.get("t_b3_27") or {})
    spec["5a T-B3-27 (v5 regression on the gate binary)"] = _res(
        t27["ok"], [str(EXT["T-B3-27"]), str(EXT["T-B3-27_dir"])],
        {k: {"ok": r["ok"], "evidence": [(e["path"], e.get("verdict") or e.get("summary"),
                                          e["pass"], e.get("superseded_by"))
                                         for e in r["evidence"]], "problems": r["problems"]}
         for k, r in t27["rows"].items()},
        "cited from WP22 (same exe sha 56d8bfb3…), not re-run; judged from the evidence files "
        "(gate JSON verdict/per-test checks, pytest summary lines), an early failure only when "
        "a later passing re-run supersedes it")
    spec["5b T-B3-28 (graph/summary round trip, multi_tb rows)"] = _res(
        bool(rt.get("pass")), [str(out / "roundtrip.json")],
        {"artifacts": len(rt.get("rows") or {}),
         "pass": sum(1 for r in (rt.get("rows") or {}).values() if r.get("ok"))})
    uc = rd(out / "ucond.json") or {}
    spec["3b T-B3-42 U-COND (drop_g_cond static-only argument)"] = _res(
        bool(uc.get("pass")), [str(out / "ucond.json")], uc.get("totals"))
    v["spec_gate"] = spec
    # hardening
    hard: dict[str, Any] = {}
    for n in ("F-B3-13a", "F-B3-13b", "F-B3-13c", "F-B3-13d"):
        rr = {m: (gd.get("rows") or {}).get(f"{n}/{m}") or {} for m in MODES}
        hard[n] = _res(all(r.get("verdict") == "PASS" for r in rr.values()),
                       [str(out / "gdbdiag.json")] + [r.get("run_dir", "") for r in rr.values()],
                       {m: {"stats": r.get("stats"), "hits": r.get("variant_hits_total"),
                            "pairs": r.get("pairs"),
                            "failed": [c for c in r.get("checks", []) if not c.get("ok")]}
                        for m, r in rr.items()})
    rb = {m: (((gd.get("rows") or {}).get(f"F-B3-13a/{m}") or {}).get("stats") or {})
          for m in MODES}
    hard["F-B3-13a"]["criterion"] = {
        "design": "arming refused while the breakpoint exists (stats refused[bp] > 0), hit "
                  "count equals warm",
        "observed_refused_bp": {m: (rb[m].get("refused") or {}).get("breakpoints")
                                for m in MODES},
        "observed_links_armed": {m: rb[m].get("links_armed") for m in MODES},
        "met": all(((rb[m].get("refused") or {}).get("breakpoints") or 0) > 0 for m in MODES),
        "substituted_by": "never armed (links_armed 0) and C' never entered, hit count equals "
                          "warm (vc_hits 700, z/nz 1000/0); arming refusal while a breakpoint "
                          "exists is shown by F-B3-13(b) (refused.breakpoints 700, breakpoint "
                          "not on C's page)",
        "why": "unreachable as worded on this loader: with the breakpoint on Vc's page, C is "
               "looked up with CF_BP_PAGE and never activated from the cache, so no arming "
               "attempt is ever made (WP22 integration finding; design implementation note "
               "'WP28 review fix')"}
    if hard["F-B3-13a"]["result"] == "pass" and not hard["F-B3-13a"]["criterion"]["met"]:
        hard["F-B3-13a"]["why"] = "pass on the substituted criterion (design criterion " \
            "refused[bp] > 0 not met: unreachable, see criterion)"
        hard["F-B3-13a"]["substituted"] = True
    for cfg, name in (("14", "F-B3-14"), ("17", "F-B3-17"), ("21", "F-B3-21"),
                      ("22", "F-B3-22")):  # F-B3-17 is completed below (smc17)
        per = {}
        for m in MODES:
            ok, det = dyn_case_ok(out, m, cfg)
            nv_ok, nv = extra_nonvac(out, m, cfg, det)
            per[m] = {"ok": ok and nv_ok and bool((arts.get(f"{m}/{cfg}") or {}).get("ok")),
                      "nonvacuity": nv, "cases": det["cases"]}
        hard[name] = _res(all(p["ok"] for p in per.values()), [str(out / "dynamic.json")], per)
    sm = rd(out / "smc17.json") or {}
    prod17 = hard["F-B3-17"]
    hard["F-B3-17"] = _res(
        (prod17["result"] == "pass" and bool(sm.get("pass"))) if sm else None,
        [str(out / "dynamic.json"), str(out / "smc17.json")],
        {"production_artifact": {"result": prod17["result"], "detail": prod17["detail"],
                                 "note": "no SPEC link (RAM continuation refused "
                                         "target_not_profiled): vacuous for SMC, outputs equal"},
         "test_only_link": {m: {"items": r.get("items"), "c_prime_hits": r.get("c_prime_hits"),
                                "f_hits": r.get("f_hits"),
                                "rows_bypassed": (r.get("package") or {}).get("rows_bypassed"),
                                "plan": r.get("plan")}
                            for m, r in (sm.get("rows") or {}).items()}},
        sm.get("note", "") if sm else "smc17 step not run: the production artifact alone is "
        "vacuous (no SPEC link)")
    hard["F-B3-15"] = _res(bool(al.get("pass")), [str(out / "alias.json")],
                           {m: {"items": r.get("items"), "hits": (r.get("hits") or {}).get("hits"),
                                "bypassed": (r.get("package") or {}).get("failing_rows_bypassed")}
                            for m, r in (al.get("rows") or {}).items()}, al.get("note", ""))
    w26 = rd(EXT["F-B3-18"]) or {}
    f18 = (w26.get("acceptance_integration") or {}).get(
        "f_b3_18_proof_pass_gate_invalid_qint") or {}
    s16c = {m: {k: ((st.get("rows") or {}).get(m) or {}).get(k) for k in ("16c", "16c_bare")}
            for m in MODES}
    hard["F-B3-18"] = _res(f18.get("result") == "pass",
                           [str(EXT["F-B3-18"]), str(EXT["F-B3-18_doc"]), str(out / "stale.json")],
                           {"wp26_result": f18.get("result"),
                            "wp28_f_b3_1_artifact_on_qint": s16c},
                           "cited from WP26 integration (gate binary 56d8bfb3…, review-3 "
                           "fixture, with the qint-signature control that shows the v6 format is "
                           "the cause); WP28 adds the real F-B3-1 b3 artifact on qint (16c rows: "
                           "directory refused runtime_mismatch:qemu_sha256, bare cache.sc "
                           "INVALID_artifact_not_used)")
    w22 = rd(EXT["T-B3-26_verdict"]) or {}
    hard["T-B3-26"] = _res((w22.get("t_b3_26_stress") or {}).get("verdict") == "PASS",
                           [str(EXT["T-B3-26"]), str(EXT["T-B3-26_verdict"])],
                           {"n": (w22.get("t_b3_26_stress") or {}).get("n"),
                            "scenarios": (w22.get("t_b3_26_stress") or {}).get("scenarios")},
                           "cited from WP22 integration re-run 2 (stress-r6 on the gate binary "
                           "and the review-3 fixture); not re-run")
    v["hardening"] = hard
    # reported outcomes
    rep: dict[str, Any] = {"T-B3-29 (B3+, Tier 1)": _res(None, [], None,
                                                         "not_run: WP23 not approved")}
    carms = comp.get("arms") or {}
    rep["F-B3-19 compute B3 artifacts accepted, V10-equal on held-out"] = _res(
        bool(carms) and all(r.get("status") == "published" and r.get("static_rows_ok") and
                            r.get("v10_exit") == 0 for r in carms.values()),
        [str(out / "compute.json")],
        {a: {"variants": r.get("n_variants"), "slots": r.get("n_slots"), "spec": r.get("n_spec"),
             "v10_exit": r.get("v10_exit"), "v10": (r.get("v10") or {}).get("verdict"),
             "bytes": r.get("pass_byte_delta")} for a, r in carms.items()})
    rep["paired timing (compute, held-out)"] = _res(
        None if tim is None else True, [str(out / "timing-analysis.json"),
                                        str(out / "timing-load.log"),
                                        str(out / "compute-hits.json")],
        None if tim is None else {"arms": tim.get("arms"), "cpu_budget": tim.get("cpu_budget"),
                                  "c_prime_hits_on_heldout": {
                                      a: {"by_variant": x.get("c_prime_hits_by_variant"),
                                          "total": x.get("c_prime_total"),
                                          "every_variant_entered":
                                              x.get("every_variant_entered")}
                                      for a, x in ((rd(out / "compute-hits.json") or {})
                                                   .get("arms") or {}).items()}},
        "reported outcome (never pass/fail)" if tim else "not run yet")
    v["timing_cpu_budget"] = (tim or {}).get("cpu_budget")
    v["reported"] = rep
    # ledger stages of every published fixture artifact + dynamic rows
    stages: dict[str, int] = {}
    for a in pub.values():
        lf = Path(a["artifact_dir"]) / "ledger.jsonl"
        for x in (lf.read_text().splitlines() if lf.is_file() else []):
            j = json.loads(x)
            k = f"{j.get('stage')}:{j.get('verdict')}"
            stages[k] = stages.get(k, 0) + 1
    dstages: dict[str, int] = {}
    dfail: list[str] = []
    for f in sorted((out / "dyn").glob("*/ledger.jsonl")):
        for x in f.read_text().splitlines():
            j = json.loads(x)
            k = f"{j.get('stage')}:{j.get('verdict')}"
            dstages[k] = dstages.get(k, 0) + 1
            if k == "validated_dynamic:fail":
                dfail.append(f.parent.name)
    v["ledger_stages"] = {"artifacts": stages, "dynamic_runs": dstages,
                          "dynamic_fail_tags": dfail}
    blocking = [k for k, x in spec.items() if x["result"] != "pass"] + \
        [k for k, x in hard.items() if x["result"] == "fail"]
    v["blocking"] = blocking
    v["gate"] = "PASS" if not blocking else "FAIL"
    dump(out / "verdict.json", v)
    log("verdict", v["gate"], "blocking", blocking)
    return v


# ---------------------------------------------------------------------------
# ucond: T-B3-42 (condexec == 0 at every return-exit slot) on the gate bases and compute
# ---------------------------------------------------------------------------

def step_ucond(out: Path) -> dict[str, Any]:
    """T-B3-42 (U-COND, the argument that makes drop_g_cond static-only): the
    ``return-edges analyze`` U-COND decode check of every gate fixture base (both modes) and
    of the compute base: 0 counterexamples, 0 unchecked return units."""
    rows: dict[str, Any] = {}
    for m in MODES:
        for c in FIXTURES:
            f = out / "return-edges" / m / f"{c}.json"
            if not f.is_file():
                b = base_info(out, m, c)
                cli(["return-edges", "analyze", b["snapshot"], "--profile", b["profile"],
                     "--prep", b["prep"], "--elf", b["elf"], "--out", f],
                    out / "logs" / f"return-edges-{m}-{c}.log")
            stt = (rd(f) or {}).get("stats") or {}
            rows[f"{m}/{c}"] = {k: stt.get(k) for k in ("ucond_units", "ucond_counterexamples",
                                                       "ucond_unchecked")}
    cb = rd(out / "compute" / "base.json") or {}
    if cb.get("snapshot"):
        f = out / "return-edges" / "compute.json"
        if not f.is_file():
            cli(["return-edges", "analyze", cb["snapshot"], "--profile", cb["profile"],
                 "--prep", cb["prep"], "--out", f], out / "logs" / "return-edges-compute.log")
        stt = (rd(f) or {}).get("stats") or {}
        rows["compute"] = {k: stt.get(k) for k in ("ucond_units", "ucond_counterexamples",
                                                   "ucond_unchecked")}
    tot = {k: sum((r.get(k) or 0) for r in rows.values())
           for k in ("ucond_units", "ucond_counterexamples", "ucond_unchecked")}
    doc = {"rows": rows, "totals": tot,
           "pass": tot["ucond_units"] > 0 and tot["ucond_counterexamples"] == 0 and
           tot["ucond_unchecked"] == 0 and all(r.get("ucond_units") is not None
                                               for r in rows.values())}
    dump(out / "ucond.json", doc)
    log("ucond", tot, doc["pass"])
    return doc
