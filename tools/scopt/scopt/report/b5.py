"""B5 report section (WP30; design §10.1 arms, §10.2 metrics, §10.3 model).

:func:`b5_section` turns the generic report model (:func:`scopt.report.collect.collect`)
plus a B5 campaign file (the definition ``campaigns/b5-*.def.toml`` or the frozen
``campaigns/b5-*.toml``) into the B5 rows. Every arm of the campaign gets a row in
every split, whether it was measured, not run, ``skipped_unsupported`` (the §10.1 arms
that cannot be built, e.g. ``semantic``) or excluded (degenerate or experimental); no
arm is dropped silently.

Rows (MODEL = arithmetic over profile counts or file bytes, never a time; MEASURED = a
BENCH/wall/RSS value of a timed run; DERIVED = a measured difference divided by a model
count):

* ``arms``: status per split and set;
* ``speedups``: paired BENCH speedup vs warm and vs the parent (ablations), 95 % CI,
  verdict, the R11 flag (median >= 1 % and CI excluding 0);
* ``lookups``: removed lookups per run (MODEL: profile ``lookup_hit`` edges from each
  guarded slot's R to its target, train profile and per-split base profiles), the
  predicted saving with the WP16 constants (MODEL, qint), the measured saving (paired
  ``t_warm - t_arm``) and the DERIVED ns per removed lookup;
* ``stores``: removed env stores per run (MODEL: NOP-filled ``mov [rbp+d], x`` per unit
  versus the artifact's input cache, times the unit's steady entries), predicted,
  measured, DERIVED ns per store;
* ``code_growth``: populated bytes, file/allocated/arena sizes, grown units, guard stub
  bytes, variant bytes, slots by kind, growth relative to the hot set (99 % of steady
  entries);
* ``memory``: children max RSS per arm and split, and the ``.sc`` sizes;
* ``harness``: full-harness wall time per run and its paired speedup (separate from the
  vCPU throughput of the BENCH window, spec:183);
* ``region_coverage``: MODEL share of steady TB entries inside a SPEC (R, C') pair and
  of lookups on guarded edges; MEASURED links armed and C' entries from untimed diag runs;
* ``cold_path``: QEMU process time outside the BENCH window (``qemu.wait - BENCH``,
  DERIVED: start-up plus teardown), deploy time, the v6 ext parse time;
* ``fallback``: appended records per run; disarm and refusal counters (diag);
* ``prep_cost``: pass wall times (sidecar ``duration_s``) and the prep driver's
  optimize/validate/V10 wall times.
"""

from __future__ import annotations

import json
import os
import statistics
import tomllib
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import stats

__all__ = ["load_b5", "b5_section", "render_b5", "guarded_lookup_model",
           "removed_store_model", "code_growth", "profile_view", "cold_path_ms",
           "find_b5_campaign", "SCHEMAS"]

SCHEMAS = ("scopt.b5_definition/1", "scopt.b5_campaign/1")
R11_MIN = 0.01


# --------------------------------------------------------------------------
# campaign file
# --------------------------------------------------------------------------

def _expand_unsupported(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        if r.get("expand") and "<pass>" in r["name"]:
            for p in r["expand"]:
                out.append({"name": r["name"].replace("<pass>", p), "reason": r["reason"],
                            "detail": r.get("detail", "")})
        else:
            out.append({k: r.get(k, "") for k in ("name", "reason", "detail")})
    return out


def load_b5(path: str | os.PathLike) -> dict[str, Any]:
    """Read a B5 definition or frozen campaign and normalize it:
    ``{schema, frozen, name, arms, skipped_unsupported, excluded, sets, workload, ...}``.
    A definition's ``set.arms`` names a list in ``[schedule]``; it is expanded."""
    doc = tomllib.loads(Path(path).read_text())
    if doc.get("schema") not in SCHEMAS:
        raise ValueError(f"{path}: schema {doc.get('schema')!r} is not one of {SCHEMAS}")
    frozen = doc["schema"] == "scopt.b5_campaign/1"
    sched = doc.get("schedule") or {}
    sets = []
    for s in doc.get("set", []):
        arms = s.get("arms")
        if isinstance(arms, str):
            arms = list(sched.get(arms) or [])
        sets.append({**s, "arms": list(arms or [])})
    return {"schema": doc["schema"], "frozen": frozen, "name": doc.get("name"),
            "path": str(Path(path).resolve()), "arms": [dict(a) for a in doc.get("arm", [])],
            "skipped_unsupported": _expand_unsupported(doc.get("skipped_unsupported", [])),
            "excluded": [dict(e) for e in doc.get("excluded", [])],
            "sets": sets, "workload": doc.get("workload") or {}, "runtime":
            doc.get("runtime") or {}, "diag": doc.get("diag") or {},
            "rules": doc.get("rules") or {}, "schedule": sched, "raw": doc}


def find_b5_campaign(roots: Sequence[str | os.PathLike]) -> str | None:
    """``b5-campaign.toml`` (the copy ``b5_campaign.py run`` writes) under a root."""
    for r in roots:
        p = Path(r)
        for cand in (p / "b5-campaign.toml", p.parent / "b5-campaign.toml"):
            if cand.is_file():
                return str(cand.resolve())
    return None


# --------------------------------------------------------------------------
# profiles and model counts
# --------------------------------------------------------------------------

def _load_json(p: str | os.PathLike) -> Any:
    from .collect import _load_json as lj

    return lj(Path(p))


def profile_view(doc: Mapping[str, Any], phase: str = "steady") -> dict[str, Any]:
    """Per-run steady-phase counts of a Profile v1 document: unit entries, lookup_hit
    edges and the unit vaddrs. Counts in the file are merged over its runs; every value
    here is divided by the number of runs (``collection.runs``)."""
    ph = next((p["id"] for p in doc.get("phases", []) if p.get("name") == phase), None)
    nruns = len((doc.get("collection") or {}).get("runs") or []) or 1
    ent: dict[str, float] = {}
    vaddrs: dict[str, set[int]] = {}
    for u in doc.get("units", []):
        vaddrs[u["unit"]] = {int(v, 16) for v in u.get("vaddrs", [])}
        c = (u.get("phase") or {}).get(str(ph)) or {}
        ent[u["unit"]] = float(c.get("entries") or 0) / nruns
    lk: dict[str, list[tuple[str, float]]] = defaultdict(list)
    tot_lk = 0.0
    for e in doc.get("edges", []):
        if e.get("kind") == "lookup_hit" and e.get("phase") == ph and e.get("src"):
            c = float(e.get("count") or 0) / nruns
            lk[e["src"]].append((e["dst"], c))
            tot_lk += c
    splits = sorted({i.get("split") for i in (doc.get("collection") or {}).get("inputs", [])
                     if i.get("split")})
    return {"profile_id": doc.get("profile_id"), "snapshot_id": doc.get("snapshot_id"),
            "phase": phase, "phase_id": ph, "runs": nruns, "splits": splits,
            "entries": ent, "vaddrs": vaddrs, "lookups": lk,
            "total_entries": sum(ent.values()), "total_lookups": tot_lk}


def _sc_units(path: str | os.PathLike) -> tuple[Any, dict[int, str], dict[str, Any]]:
    from ..formats.sc_v6 import read_sc6

    f = read_sc6(path)
    sc = f.sc
    by_off, by_uid = {}, {}
    for uid, u in zip(sc.unit_ids(), sc.units()):
        by_off[u.record.tb_offset] = uid
        by_uid[uid] = u
    return f, by_off, by_uid


def guarded_lookup_model(sc_path: str | os.PathLike, pv: Mapping[str, Any]) -> dict[str, Any]:
    """MODEL: lookups per run that a v6 artifact's guarded slots replace. For every slot
    the ``lookup_hit`` edges from its R unit to a unit at the slot's target vaddr (the
    edges the guard passes on); SPEC slots also count as C' entries."""
    f, by_off, _ = _sc_units(sc_path)
    if not f.has_ext or f.ext is None:
        return {"status": "no_ext", "slots": 0, "guarded_lookups": 0.0,
                "share_of_lookups": 0.0, "spec_entries": 0.0}
    g = spec = 0.0
    kinds: dict[str, int] = defaultdict(int)
    unmapped = 0
    for s in f.ext.slots:
        kinds["SPEC" if s.kind == 2 else "GENERAL" if s.kind == 1 else str(s.kind)] += 1
        r = by_off.get(s.src_tb_offset)
        if r is None:
            unmapped += 1
            continue
        c = sum(n for dst, n in pv["lookups"].get(r, ())
                if s.target_vaddr in pv["vaddrs"].get(dst, ()))
        g += c
        if s.kind == 2:
            spec += c
    tl = pv["total_lookups"] or 0.0
    te = pv["total_entries"] or 0.0
    return {"status": "ok", "slots": len(f.ext.slots), "slots_by_kind": dict(kinds),
            "unmapped_slots": unmapped, "variants": len(f.ext.variants),
            "guarded_lookups": g, "share_of_lookups": (g / tl) if tl else None,
            "spec_entries": spec, "spec_share_of_entries": (spec / te) if te else None,
            "total_lookups": tl, "total_entries": te}


def _is_rbp_store(ins: Any) -> bool:
    from capstone.x86 import X86_OP_MEM, X86_REG_RBP

    return (ins.mnemonic.startswith("mov") and bool(ins.operands)
            and ins.operands[0].type == X86_OP_MEM and ins.operands[0].mem.base == X86_REG_RBP)


def removed_store_model(art_sc: str | os.PathLike, base_sc: str | os.PathLike,
                        pv: Mapping[str, Any] | None) -> dict[str, Any]:
    """MODEL: env stores (``mov [rbp+d], x``) of ``base_sc`` whose bytes changed in the
    same unit (same UnitId, same code length) of ``art_sc`` -- B4's same-length NOP fill.
    Static count, and per run weighted by the unit's steady entries when ``pv`` is given."""
    from capstone import CS_ARCH_X86, CS_MODE_64, Cs

    md = Cs(CS_ARCH_X86, CS_MODE_64)
    md.detail = True
    _, _, base = _sc_units(base_sc)
    _, _, art = _sc_units(art_sc)
    n = units = grown = 0
    w = 0.0
    for uid, u in art.items():
        b = base.get(uid)
        if b is None or b.code == u.code:
            continue
        if len(b.code) != len(u.code):
            grown += 1
            continue
        units += 1
        oc, nc = b.code, u.code
        k = sum(1 for ins in md.disasm(oc, 0)
                if oc[ins.address:ins.address + ins.size] != nc[ins.address:ins.address + ins.size]
                and _is_rbp_store(ins))
        n += k
        if pv is not None:
            w += k * pv["entries"].get(uid, 0.0)
    return {"status": "ok", "units_rewritten": units, "units_resized": grown,
            "static_stores": n, "stores_per_run": w if pv is not None else None}


def code_growth(art_sc: str | os.PathLike, base_sc: str | os.PathLike) -> dict[str, Any]:
    """Byte growth of an artifact over its input cache: populated native bytes, per-unit
    code growth (grown R units), guard stub bytes (Σ guard_len) and variant bytes."""
    fa, _, art = _sc_units(art_sc)
    _, _, base = _sc_units(base_sc)

    def pop(units: Mapping[str, Any]) -> int:
        return sum(u.record.end_offset - u.record.tc_offset for u in units.values())

    grown = [uid for uid, u in art.items() if uid in base
             and u.record.tc_size != base[uid].record.tc_size]
    d_code = sum(art[u].record.tc_size - base[u].record.tc_size for u in grown)
    var_bytes = stub = 0
    nv = ns = 0
    if fa.has_ext and fa.ext is not None:
        var_bytes = sum(v.end_offset - v.tb_offset for v in fa.ext.variants)
        stub = sum(s.guard_len for s in fa.ext.slots)
        nv, ns = len(fa.ext.variants), len(fa.ext.slots)
    st_a, st_b = os.stat(art_sc), os.stat(base_sc)
    return {"populated_bytes": pop(art), "populated_bytes_base": pop(base),
            "populated_delta": pop(art) - pop(base) + var_bytes,
            "units_resized": len(grown), "resized_code_delta": d_code,
            "guard_stub_bytes": stub, "variant_bytes": var_bytes, "slots": ns,
            "variants": nv, "file_size": st_a.st_size, "file_size_base": st_b.st_size,
            "allocated_bytes": st_a.st_blocks * 512,
            "allocated_bytes_base": st_b.st_blocks * 512,
            "arena_capacity": fa.sc.header.code_size}


def hot_set_bytes(base_sc: str | os.PathLike, pv: Mapping[str, Any],
                  coverage: float = 0.99) -> int:
    """Code bytes of the units covering ``coverage`` of the steady entries (§3.4 basis)."""
    _, _, base = _sc_units(base_sc)
    tot = pv["total_entries"] or 0.0
    acc = 0.0
    out = 0
    for uid, e in sorted(pv["entries"].items(), key=lambda kv: -kv[1]):
        if tot and acc >= coverage * tot:
            break
        u = base.get(uid)
        if u is not None:
            out += u.record.tc_size
        acc += e
    return out


# --------------------------------------------------------------------------
# timing-derived rows
# --------------------------------------------------------------------------

def cold_path_ms(run_dir: str | os.PathLike, bench_ns: float | None) -> float | None:
    """DERIVED: ``qemu.wait`` (FastDyn's QEMU process wait) minus the BENCH window, in ms:
    QEMU start-up up to ``benchmark_start`` plus the teardown after ``benchmark_end``."""
    p = Path(run_dir) / "work" / "fastdyn_timing.jsonl"
    if bench_ns is None or not p.is_file():
        return None
    try:
        for line in p.read_text().splitlines():
            if '"qemu.wait"' in line and '"phase_end"' in line:
                d = json.loads(line)
                return float(d["duration_s"]) * 1e3 - float(bench_ns) / 1e6
    except (OSError, ValueError, KeyError):
        return None
    return None


def _paired_diff(raw: Sequence[Mapping[str, Any]], ref: str, arm: str, key: str = "bench_ns",
                 *, reps: int, seed: int) -> dict[str, Any]:
    """Paired ``t_ref - t_arm`` (ms) over (rep, input) pairs with a bootstrap CI."""
    by: dict[tuple, dict[str, float]] = defaultdict(dict)
    for r in raw:
        if r.get("valid") and r.get(key) is not None and r.get("rep") is not None:
            by[(r["rep"], r.get("input"))][r["arm"]] = float(r[key])
    scale = 1e-6 if key == "bench_ns" else 1e3
    ds = [(d[ref] - d[arm]) * scale for d in by.values() if ref in d and arm in d]
    if not ds:
        return {"n_pairs": 0, "median_ms": None, "ci_ms": [None, None]}
    lo, hi = stats.bootstrap_ci(statistics.median, [ds], reps=reps, seed=seed)
    return {"n_pairs": len(ds), "median_ms": statistics.median(ds), "ci_ms": [lo, hi]}


def _r11(c: Mapping[str, Any]) -> bool | None:
    if c.get("median") is None:
        return None
    lo = (c.get("ci") or [None, None])[0]
    return bool(c["median"] - 1.0 >= R11_MIN and lo is not None and lo > 1.0)


# --------------------------------------------------------------------------
# diag (untimed) outputs
# --------------------------------------------------------------------------

def _diag_index(roots: Sequence[str | os.PathLike]) -> dict[str, Any]:
    """Merge every ``b5-diag.json`` under ``roots`` (written by ``b5_campaign.py diag``):
    ``{"oracles": {split: {arm: dynamic_oracles.json}}, "profiles": {split: path}}``."""
    out: dict[str, Any] = {"oracles": defaultdict(dict), "profiles": {}, "files": []}
    for r in roots:
        rp = Path(r)
        if not rp.is_dir():
            continue
        for p in sorted(rp.rglob("b5-diag.json")):
            try:
                d = json.loads(p.read_text())
            except (OSError, ValueError):
                continue
            out["files"].append(str(p))
            for split, arms in (d.get("oracles") or {}).items():
                out["oracles"][split].update(arms)
            out["profiles"].update(d.get("profiles") or {})
    return out


def _diag_rows(dyn_path: str) -> dict[str, Any]:
    """Counters of one untimed diag ``dynamic_oracles.json``: per case the artifact run's
    ``v6-stats.json`` and the gdb C' hit counts."""
    try:
        d = json.loads(Path(dyn_path).read_text())
    except (OSError, ValueError) as e:
        return {"status": "unreadable", "detail": str(e)}
    keys = ("links_armed", "n_links", "n_slots", "n_variants", "activations",
            "disarm_tlb_events", "disarm_tlb_links", "disarm_flush_links",
            "disarm_forbid_links", "slots_unlinked", "variant_slots_unlinked",
            "variant_exits", "variant_entry_exits", "variant_restores", "ext_load_ns",
            "invariant_violations")
    cases = []
    for c in d.get("cases", []):
        art = ((c.get("runs") or {}).get("artifact") or {})
        rd = art.get("run_dir")
        st = {}
        if rd and (Path(rd) / "v6-stats.json").is_file():
            try:
                st = json.loads((Path(rd) / "v6-stats.json").read_text())
            except ValueError:
                st = {}
        hits = {t["what"]: t.get("hits") for t in ((c.get("hits") or {}).get("targets") or [])
                if "C'" in str(t.get("what"))}
        cases.append({"input": c.get("input"), "appended": art.get("appended"),
                      "verdict": art.get("verdict"),
                      **{k: st.get(k) for k in keys},
                      "refused": {k: v for k, v in (st.get("refused") or {}).items() if v},
                      "c_prime_hits": hits})
    return {"status": "ok", "verdict": d.get("verdict"), "cases": cases}


# --------------------------------------------------------------------------
# the section
# --------------------------------------------------------------------------

def _src_sc(arm: Mapping[str, Any]) -> str | None:
    s = arm.get("source")
    if not s or s in ("warm", "auto") or str(s).startswith(("build:", "reuse:")):
        return None
    p = Path(s)
    p = p / "cache.sc" if p.is_dir() else p
    return str(p) if p.is_file() else None


def _sidecar(sc: str | None) -> dict[str, Any]:
    if not sc:
        return {}
    for p in (Path(sc).parent / "cache.sc.scopt.json", Path(sc + ".scopt.json")):
        if p.is_file():
            try:
                return json.loads(p.read_text())
            except ValueError:
                return {}
    return {}


def _out_snapshot_id(sc: str | None) -> str | None:
    if not sc:
        return None
    v = Path(sc).parent / "validation.json"
    try:
        return json.loads(v.read_text()).get("output_snapshot_id") if v.is_file() else None
    except ValueError:
        return None


def b5_section(model: Mapping[str, Any], camp: Mapping[str, Any], *,
               roots: Sequence[str | os.PathLike] = (), bootstrap: int = 10000,
               seed: int = 0, problems: list | None = None) -> dict[str, Any]:
    """Build the B5 section. Never raises for missing data: rows become ``not_run``."""
    probs = problems if problems is not None else []
    sec: dict[str, Any] = {"campaign": camp.get("path"), "schema": camp.get("schema"),
                           "frozen": camp.get("frozen"), "name": camp.get("name")}
    groups = [g for g in model.get("groups", []) if (g.get("meta") or {}).get("split")]
    # B5 sets are the campaign's own (``<set>`` or ``<set>-smoke``); any other timing
    # campaign found under the roots (e.g. an earlier gate WP's rows) is labelled
    # ``foreign`` everywhere and never counts as a B5 measurement of an arm.
    b5names = {s["name"] for s in camp.get("sets", [])}
    b5names |= {n + "-smoke" for n in b5names}
    for g in groups:
        g.setdefault("meta", {})["b5"] = g["meta"].get("name") in b5names
    sec["foreign_sets"] = sorted({g["meta"].get("name") or "?" for g in groups
                                  if not g["meta"]["b5"]})
    splits = list(dict.fromkeys([s["split"] for s in camp.get("sets", [])] +
                                [g["meta"]["split"] for g in groups]))
    sec["splits"] = splits
    arms = camp.get("arms", [])
    arm_by = {a["name"]: a for a in arms}

    # ---- arm status per split
    rows = []
    for a in arms:
        per = {}
        for sp in splits:
            planned = [s["name"] for s in camp.get("sets", []) if s["split"] == sp
                       and a["name"] in s["arms"]]
            got = [(g["meta"].get("name"), (g["per_arm"].get(a["name"]) or {}))
                   for g in groups if g["meta"]["split"] == sp and a["name"] in g["per_arm"]
                   and g["meta"]["b5"]]
            foreign = [g["meta"].get("name") for g in groups if g["meta"]["split"] == sp
                       and a["name"] in g["per_arm"] and not g["meta"]["b5"]]
            valid = sum(x.get("valid", 0) for _, x in got)
            runs = sum(x.get("runs", 0) for _, x in got)
            if got and valid:
                per[sp] = {"status": "measured", "valid": valid, "runs": runs,
                           "sets": [n for n, _ in got]}
            elif got:
                per[sp] = {"status": "not_measured", "valid": 0, "runs": runs,
                           "reason": "no valid sample"}
            elif planned:
                per[sp] = {"status": "not_run", "reason": "planned in " + ", ".join(planned)
                           + "; no run found"}
            else:
                per[sp] = {"status": "not_planned", "reason": "not an arm of any "
                           f"{sp} set"}
            if foreign:
                per[sp]["foreign_sets"] = foreign
        rows.append({"arm": a["name"], "kind": a.get("kind"), "role": a.get("role"),
                     "category": a.get("category"), "parent": a.get("parent"),
                     "priority": a.get("priority"), "note": a.get("note", ""),
                     "per_split": per})
    for u in camp.get("skipped_unsupported", []):
        rows.append({"arm": u["name"], "status": "skipped_unsupported",
                     "reason": u.get("reason"), "detail": u.get("detail", ""),
                     "per_split": {sp: {"status": "skipped_unsupported",
                                        "reason": u.get("reason")} for sp in splits}})
    for e in camp.get("excluded", []):
        rows.append({"arm": e["name"], "status": "excluded", "reason": e.get("reason"),
                     "per_split": {sp: {"status": "excluded"} for sp in splits}})
    sec["arms"] = rows

    # ---- speedups (BENCH, all inputs) per set
    sp_rows = []
    for g in groups:
        for c in g.get("comparisons", []):
            if c.get("metric") != "bench" or c.get("input") != "*":
                continue
            sp_rows.append({"set": g["meta"].get("name"), "split": g["meta"]["split"],
                            "b5": g["meta"]["b5"],
                            "arm": c["arm"], "vs": c["reference"], "why": c["why"],
                            "category": (arm_by.get(c["arm"]) or {}).get("category"),
                            "median": c.get("median"), "ci": c.get("ci"),
                            "verdict": c.get("verdict"), "n_pairs": c.get("n_pairs"),
                            "frac_faster": c.get("frac_faster"), "r11": _r11(c)})
    sec["speedups"] = sp_rows

    # ---- models (train profile + per-split base profiles)
    wl = camp.get("workload") or {}
    warm = arm_by.get("warm") or {}
    base_sc = _src_sc(warm) or (str(Path(wl["snapshot"]["dir"]) / "cache.sc")
                                if isinstance(wl.get("snapshot"), dict) else None)
    diag = _diag_index(roots)
    sec["diag_files"] = diag["files"]
    pvs: dict[str, dict[str, Any]] = {}
    prof = (wl.get("profile") or {}).get("path") if isinstance(wl.get("profile"), dict) else None
    for label, pth in [("train", prof), *sorted(diag["profiles"].items())]:
        if not pth:
            continue
        try:
            pvs[label] = profile_view(_load_json(pth), (wl.get("phase") or "steady@").split("@")[0]
                                      or "steady")
            pvs[label]["path"] = pth
        except Exception as e:  # noqa: BLE001
            probs.append({"where": f"b5:profile:{label}", "problem": f"{type(e).__name__}: {e}"})
    sec["profiles"] = {k: {x: v.get(x) for x in ("path", "profile_id", "runs", "splits",
                                                 "total_entries", "total_lookups")}
                       for k, v in pvs.items()}
    cal = {}
    calp = (wl.get("calibration") or {}).get("path") if isinstance(wl.get("calibration"), dict) \
        else wl.get("calibration")
    if calp and Path(calp).is_file():
        try:
            cc = json.loads(Path(calp).read_text()).get("constants", {})
            cal = {k: (cc.get(k) or {}).get("value_ns") for k in
                   ("c_lookup", "c_guard", "c_lookup_minus_c_guard", "b4_d1_saving_per_store")}
        except ValueError:
            pass
    sec["calibration"] = {"path": calp, "constants_ns": cal,
                          "label": "MODEL constants measured by WP16 on the qint binary"}

    # artifacts' input caches (B4 on sfb: the sfb artifact)
    out_ids = {}
    for a in arms:
        sc = _src_sc(a)
        oid = _out_snapshot_id(sc) if a["name"] != "warm" else None
        if oid:
            out_ids[oid] = sc
    raw_by_split: dict[str, list[tuple[str, list]]] = defaultdict(list)
    for g in groups:
        raw_by_split[g["meta"]["split"]].append(
            ((g["meta"].get("name") or "?") + ("" if g["meta"]["b5"] else " (foreign)"),
             g.get("raw", [])))

    def measured(arm: str, ref: str = "warm") -> dict[str, Any]:
        res = {}
        for sp, lst in raw_by_split.items():
            for name, raw in lst:
                if any(r.get("arm") == arm for r in raw) and any(r.get("arm") == ref
                                                                  for r in raw):
                    res[f"{sp}:{name}"] = _paired_diff(raw, ref, arm, reps=bootstrap,
                                                       seed=seed)
        return res

    def model_key(k: str, models: Mapping[str, Any]) -> str | None:
        """The model of the measured set's split, else the train model (labelled)."""
        sp = k.split(":")[0]
        return sp if sp in models else ("train" if "train" in models else None)

    lookups, stores, growth = [], [], []
    hot = None
    if base_sc and "train" in pvs:
        try:
            hot = hot_set_bytes(base_sc, pvs["train"])
        except Exception as e:  # noqa: BLE001
            probs.append({"where": "b5:hot_set", "problem": f"{type(e).__name__}: {e}"})
    sec["hot_set_bytes_99"] = hot
    c_net = cal.get("c_lookup_minus_c_guard")
    for a in arms:
        sc = _src_sc(a)
        if a.get("kind") != "deploy" or a["name"] == "warm":
            continue
        if sc is None:
            for lst in (lookups, stores, growth):
                lst.append({"arm": a["name"], "status": "not_run",
                            "reason": "artifact not resolved (definition file or missing)"})
            continue
        side = _sidecar(sc)
        in_id = side.get("input_snapshot_id")
        in_sc = out_ids.get(in_id) or base_sc
        meas = measured(a["name"])
        try:
            lm = {sp: guarded_lookup_model(sc, pv) for sp, pv in pvs.items()}
            if any(m["status"] == "ok" and m["slots"] for m in lm.values()):
                row = {"arm": a["name"], "status": "ok", "model": lm, "measured": meas}
                tr = lm.get("train") or {}
                if c_net is not None and tr.get("status") == "ok":
                    row["predicted_saving_ms_qint"] = tr["guarded_lookups"] * c_net / 1e6
                row["model_split_used"] = {k: model_key(k, lm) for k in meas}
                row["derived_ns_per_lookup"] = {
                    k: (m["median_ms"] * 1e6 / lm[mk]["guarded_lookups"]
                        if m.get("median_ms") is not None and mk
                        and lm[mk].get("guarded_lookups") else None)
                    for k, m in meas.items() for mk in [row["model_split_used"][k]]}
                lookups.append(row)
        except Exception as e:  # noqa: BLE001
            lookups.append({"arm": a["name"], "status": "error", "reason": f"{e}"})
        try:
            if in_sc:
                sm = {sp: removed_store_model(sc, in_sc, pv) for sp, pv in pvs.items()} or \
                     {"static": removed_store_model(sc, in_sc, None)}
                if any(m["static_stores"] for m in sm.values()):
                    ref = a.get("parent") if a.get("parent") in arm_by and \
                        _out_snapshot_id(_src_sc(arm_by[a["parent"]])) == in_id else "warm"
                    meas_s = measured(a["name"], ref)
                    row = {"arm": a["name"], "status": "ok", "input_cache": in_sc,
                           "vs": ref, "x_classes": next((p.get("x_classes") for p in
                                                         side.get("pipeline", [])
                                                         if p.get("x_classes")), None),
                           "model": sm, "measured": meas_s}
                    tr = sm.get("train") or {}
                    if cal.get("b4_d1_saving_per_store") is not None and \
                            tr.get("stores_per_run") is not None:
                        row["predicted_saving_ms_qint"] = (tr["stores_per_run"] *
                                                           cal["b4_d1_saving_per_store"] / 1e6)
                    row["model_split_used"] = {k: model_key(k, sm) for k in meas_s}
                    row["derived_ns_per_store"] = {
                        k: (m["median_ms"] * 1e6 / sm[mk]["stores_per_run"]
                            if m.get("median_ms") is not None and mk
                            and sm[mk].get("stores_per_run") else None)
                        for k, m in meas_s.items() for mk in [row["model_split_used"][k]]}
                    stores.append(row)
        except Exception as e:  # noqa: BLE001
            stores.append({"arm": a["name"], "status": "error", "reason": f"{e}"})
        try:
            if in_sc:
                cg = code_growth(sc, in_sc)
                cg["vs_input"] = in_sc
                cg["growth_over_hot_set"] = (cg["populated_delta"] / hot) if hot else None
                cg["pipeline"] = [{"pass": p.get("pass"), "outcome": p.get("outcome"),
                                   "duration_s": p.get("duration_s")}
                                  for p in side.get("pipeline", [])]
                growth.append({"arm": a["name"], "status": "ok", **cg})
        except Exception as e:  # noqa: BLE001
            growth.append({"arm": a["name"], "status": "error", "reason": f"{e}"})
    sec["lookups"] = lookups
    sec["stores"] = stores
    sec["code_growth"] = growth

    # ---- memory, harness throughput, cold path, fallback (timing rows)
    mem, harness, cold, fb = [], [], [], []
    for g in groups:
        sp = g["meta"]["split"]
        name = (g["meta"].get("name") or "?") + ("" if g["meta"]["b5"] else
                                                  f" (foreign, {sp})")
        wall = {c["arm"]: c for c in g.get("comparisons", []) if c.get("metric") == "wall"
                and c.get("input") == "*" and c.get("why") == "vs_reference"}
        for arm, pa in g.get("per_arm", {}).items():
            mem.append({"set": name, "split": sp, "arm": arm,
                        "maxrss_kb_median": (pa.get("maxrss_kb") or {}).get("median"),
                        "n": (pa.get("maxrss_kb") or {}).get("n")})
            harness.append({"set": name, "split": sp, "arm": arm,
                            "wall_s_median": (pa.get("wall") or {}).get("median"),
                            "speedup_vs_warm": (wall.get(arm) or {}).get("median"),
                            "ci": (wall.get(arm) or {}).get("ci"),
                            "verdict": (wall.get(arm) or {}).get("verdict")})
            fb.append({"set": name, "split": sp, "arm": arm,
                       "appended_median": (pa.get("appended") or {}).get("median"),
                       "appended_nonzero_runs": pa.get("appended_nonzero_runs"),
                       "valid": pa.get("valid")})
        cp: dict[str, list[float]] = defaultdict(list)
        for r in g.get("raw", []):
            if not r.get("valid") or not r.get("run_dir"):
                continue
            v = cold_path_ms(r["run_dir"], r.get("bench_ns"))
            if v is not None:
                cp[r["arm"]].append(v)
        for arm in g.get("per_arm", {}):
            xs = cp.get(arm) or []
            cold.append({"set": name, "split": sp, "arm": arm, "n": len(xs),
                         "outside_window_ms_median": statistics.median(xs) if xs else None,
                         "status": "ok" if xs else "not_run",
                         "reason": "" if xs else "no fastdyn_timing.jsonl qemu.wait rows"})
    sec["memory"] = mem
    sec["harness"] = harness
    sec["cold_path"] = cold
    sec["fallback"] = fb

    # ---- diag (untimed): region coverage measured, fallback/disarm, ext parse time
    dg = []
    for sp, arms_d in diag["oracles"].items():
        for arm, p in sorted(arms_d.items()):
            dg.append({"split": sp, "arm": arm, "path": p, **_diag_rows(p)})
    sec["diag"] = dg
    reg = []
    for a in arms:
        lm = next((x for x in lookups if x["arm"] == a["name"] and x.get("status") == "ok"), None)
        if lm is None:
            continue
        drows = [x for x in dg if x["arm"] == a["name"] and x.get("status") == "ok"]
        reg.append({"arm": a["name"],
                    "model": {sp: {"share_of_lookups_guarded": m.get("share_of_lookups"),
                                   "share_of_entries_in_spec_pairs":
                                       m.get("spec_share_of_entries")}
                              for sp, m in lm["model"].items()},
                    "measured": [{"split": x["split"], "cases": [
                        {"input": c.get("input"), "links_armed": c.get("links_armed"),
                         "n_links": c.get("n_links"), "c_prime_hits": c.get("c_prime_hits")}
                        for c in x["cases"]]} for x in drows] or "not_run: no diag run"})
    sec["region_coverage"] = reg

    # ---- preparation cost
    pc = []
    for a in arms:
        sc = _src_sc(a)
        side = _sidecar(sc)
        pc.append({"arm": a["name"], "prep": a.get("prep") or None,
                   "passes": [{"pass": p.get("pass"), "duration_s": p.get("duration_s")}
                              for p in side.get("pipeline", [])] if side else None})
    sec["prep_cost"] = pc
    return sec


# --------------------------------------------------------------------------
# markdown
# --------------------------------------------------------------------------

def _f(v: Any, nd: int = 3) -> str:
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def _pct(v: Any) -> str:
    return "—" if v is None else f"{(v - 1.0) * 100:+.2f} %"


def _ci_pct(ci: Any) -> str:
    if not ci or ci[0] is None:
        return "—"
    return f"[{(ci[0] - 1) * 100:+.2f}, {(ci[1] - 1) * 100:+.2f}] %"


def _ms_ci(m: Mapping[str, Any]) -> str:
    if not m or m.get("median_ms") is None:
        return "not_run"
    lo, hi = m.get("ci_ms") or [None, None]
    ci = f" [{lo:.1f}, {hi:.1f}]" if lo is not None else ""
    return f"{m['median_ms']:.1f} ms{ci} (n={m['n_pairs']})"


def _table(head: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for r in rows:
        out.append("| " + " | ".join(str(x).replace("|", "\\|") for x in r) + " |")
    return out


def render_b5(sec: Mapping[str, Any]) -> list[str]:
    L: list[str] = ["## B5 campaign (WP30; design §10)", ""]
    L.append(f"Campaign file `{sec.get('campaign')}` ({sec.get('schema')}, "
             f"{'frozen' if sec.get('frozen') else 'DEFINITION, not frozen'}). Labels: "
             "MODEL = arithmetic over profile counts or file bytes; MEASURED = timed runs; "
             "DERIVED = measured difference / model count. Slot arbitration makes B3 "
             "ablations non-additive. `sfb` is a baseline-TCG fix, never counted as caching, "
             "B3 or B4.")
    L.append("")
    splits = sec.get("splits") or []
    L.append("### Arms (every arm of the campaign, per split)")
    L.append("")
    rows = []
    for r in sec.get("arms", []):
        cells = []
        for sp in splits:
            x = (r.get("per_split") or {}).get(sp) or {}
            st = x.get("status", "—")
            cell = f"{st} ({x['valid']})" if st == "measured" else st
            if x.get("foreign_sets"):
                cell += " [foreign: " + ", ".join(x["foreign_sets"]) + "]"
            cells.append(cell)
        why = r.get("reason") or ""
        if r.get("detail"):
            why = f"{why}: {r['detail']}"
        rows.append([r["arm"], r.get("category") or r.get("status") or "—",
                     r.get("parent") or "—", *cells, why or r.get("note", "")])
    L += _table(["arm", "category", "parent", *splits, "reason / note"], rows)
    L.append("")
    L.append("### Paired BENCH speedup (MEASURED; vs warm, ablations also vs parent)")
    L.append("")
    sps = sec.get("speedups") or []
    if sec.get("foreign_sets"):
        L.append("Sets marked `(foreign)` are timing campaigns found under the roots that are "
                 "NOT sets of this B5 campaign (e.g. earlier gate WP rows): shown for "
                 "reference only, never counted as a B5 measurement: " +
                 ", ".join(f"`{x}`" for x in sec["foreign_sets"]) + ".")
        L.append("")
    if sps:
        L += _table(["set", "arm", "vs", "category", "speedup", "95 % CI", "verdict", "pairs",
                     "R11 (>=1 %, CI>0)"],
                    [[s["set"] + ("" if s.get("b5") else " (foreign)"), s["arm"], s["vs"],
                      s.get("category") or "—", _pct(s["median"]),
                      _ci_pct(s["ci"]), s["verdict"], _f(s["n_pairs"]), _f(s["r11"])]
                     for s in sps])
    else:
        L.append("not_run: no B5 timing set found (WP30 defines the campaign; nothing timed).")
    L.append("")
    cal = (sec.get("calibration") or {}).get("constants_ns") or {}
    L.append("### Removed lookups: MODEL vs MEASURED")
    L.append("")
    L.append(f"MODEL per run (profile counts / runs). Predicted saving = guarded lookups × "
             f"(c_lookup − c_guard) = × {_f(cal.get('c_lookup_minus_c_guard'))} ns "
             "(WP16, qint binary). DERIVED ns per removed lookup = measured saving / model "
             "count of the same split (train model when no split profile exists).")
    L.append("")
    lr = []
    for x in sec.get("lookups", []):
        if x.get("status") != "ok":
            lr.append([x["arm"], x.get("status"), x.get("reason", ""), "", "", "", ""])
            continue
        tr = x["model"].get("train") or {}
        for k, m in (x.get("measured") or {"—": {}}).items():
            used = (x.get("model_split_used") or {}).get(k)
            lr.append([x["arm"], _f(tr.get("slots")),
                       f"{_f(tr.get('guarded_lookups'), 0)} ({_f(tr.get('share_of_lookups'), 3)})",
                       _f(x.get("predicted_saving_ms_qint"), 1), k, _ms_ci(m),
                       f"{_f((x.get('derived_ns_per_lookup') or {}).get(k), 2)}"
                       + (f" (model {used})" if used else "")])
    L += _table(["arm", "slots", "train guarded lookups/run (share)", "predicted ms (qint)",
                 "set", "measured saving", "DERIVED ns/lookup"], lr) if lr else \
        ["not_run: no v6 artifact with guarded slots resolved."]
    L.append("")
    L.append("### Removed env stores: MODEL vs MEASURED")
    L.append("")
    sr = []
    for x in sec.get("stores", []):
        if x.get("status") != "ok":
            sr.append([x["arm"], x.get("status"), x.get("reason", ""), "", "", "", ""])
            continue
        tr = x["model"].get("train") or x["model"].get("static") or {}
        for k, m in (x.get("measured") or {"—": {}}).items():
            sr.append([x["arm"], f"{tr.get('static_stores')} {x.get('x_classes') or ''}",
                       _f(tr.get("stores_per_run"), 0), _f(x.get("predicted_saving_ms_qint"), 2),
                       f"{k} vs {x.get('vs')}", _ms_ci(m),
                       _f((x.get("derived_ns_per_store") or {}).get(k), 4) +
                       (f" (model {(x.get('model_split_used') or {}).get(k)})"
                        if (x.get("model_split_used") or {}).get(k) else "")])
    L += _table(["arm", "static stores (classes)", "train stores/run", "predicted ms (qint)",
                 "set", "measured saving", "DERIVED ns/store"], sr) if sr else \
        ["not_run: no artifact with removed stores resolved."]
    L.append("")
    L.append(f"### Code growth (MODEL, bytes; hot set 99 % = {_f(sec.get('hot_set_bytes_99'))} B)")
    L.append("")
    gr = [[x["arm"], _f(x.get("populated_delta")), _f(x.get("growth_over_hot_set"), 4),
           _f(x.get("units_resized")), _f(x.get("guard_stub_bytes")), _f(x.get("variant_bytes")),
           _f(x.get("slots")), _f(x.get("variants")), _f(x.get("file_size")),
           _f(x.get("allocated_bytes")), _f(x.get("arena_capacity"))]
          if x.get("status") == "ok" else [x["arm"], x.get("status"), x.get("reason", "")]
          + [""] * 8 for x in sec.get("code_growth", [])]
    L += _table(["arm", "Δ populated", "Δ / hot set", "units resized", "stub bytes",
                 "variant bytes", "slots", "variants", "file size", "allocated",
                 "arena capacity"], gr) if gr else ["not_run"]
    L.append("")
    for title, key, cols in (
            ("Memory (MEASURED children max RSS, kB)", "memory",
             [("maxrss_kb_median", 0), ("n", 0)]),
            ("Full-harness throughput (MEASURED wall s per run; paired wall speedup vs warm)",
             "harness", [("wall_s_median", 3), ("speedup_vs_warm", 4), ("verdict", 0)]),
            ("Cold path (DERIVED: qemu.wait − BENCH = start-up + teardown, ms)", "cold_path",
             [("outside_window_ms_median", 1), ("n", 0), ("status", 0)]),
            ("Fallback frequency (MEASURED appended records per run)", "fallback",
             [("appended_median", 1), ("appended_nonzero_runs", 0), ("valid", 0)])):
        L.append(f"### {title}")
        L.append("")
        rr = sec.get(key) or []
        L += _table(["set", "arm", *[c for c, _ in cols]],
                    [[x["set"], x["arm"], *[_f(x.get(c), nd) for c, nd in cols]]
                     for x in rr]) if rr else ["not_run: no B5 timing set found."]
        L.append("")
    L.append("### Region coverage (MODEL per profile; MEASURED from untimed diag runs)")
    L.append("")
    L.append("MEASURED counts only SPEC links (`links_armed`/`n_links` of qemu-sc-v6-stats) and "
             "C' entries (gdb -nx hardware breakpoints); GENERAL guarded slots are not links "
             "there, so their pass rate shows only through timing (DERIVED ns/lookup).")
    L.append("")
    rg = []
    for x in sec.get("region_coverage", []):
        mm = "; ".join(f"{sp}: lookups {_f(m['share_of_lookups_guarded'], 3)}, "
                       f"SPEC entries {_f(m['share_of_entries_in_spec_pairs'], 4)}"
                       for sp, m in x["model"].items())
        if isinstance(x["measured"], str):
            ms = x["measured"]
        else:
            ms = "; ".join(f"{d['split']}: " + ", ".join(
                f"links {c['links_armed']}/{c['n_links']}" +
                (f" C' {sum(v or 0 for v in c['c_prime_hits'].values())}"
                 if c.get("c_prime_hits") else "") for c in d["cases"]) for d in x["measured"])
        rg.append([x["arm"], mm, ms])
    L += _table(["arm", "MODEL", "MEASURED (diag)"], rg) if rg else ["not_run"]
    L.append("")
    L.append("### Disarm, refusals and ext parse time (MEASURED, untimed diag runs)")
    L.append("")
    dr = []
    for x in sec.get("diag", []):
        for c in x.get("cases", []):
            dr.append([x["split"], x["arm"], c.get("input"), _f(c.get("links_armed")),
                       _f(c.get("disarm_tlb_links")), _f(c.get("disarm_flush_links")),
                       _f(c.get("disarm_forbid_links")), json.dumps(c.get("refused") or {}),
                       _f(c.get("ext_load_ns")), _f(c.get("appended"))])
    L += _table(["split", "arm", "input", "links armed", "disarm tlb", "disarm flush",
                 "disarm forbid", "refused", "ext_load_ns", "appended"], dr) if dr else \
        ["not_run: no b5-diag.json found (b5_campaign.py diag runs after the timing sets)."]
    L.append("")
    L.append("### Preparation cost (wall s)")
    L.append("")
    pr = []
    for x in sec.get("prep_cost", []):
        p = x.get("prep") or {}
        pr.append([x["arm"], _f(p.get("optimize_s"), 1), _f(p.get("validate_s"), 1),
                   _f(p.get("v10_s"), 1),
                   ", ".join(f"{q['pass']} {_f(q['duration_s'], 2)}"
                             for q in (x.get("passes") or [])) or "—"])
    L += _table(["arm", "optimize/package", "re-validate", "V10", "passes (sidecar duration_s)"],
                pr)
    L.append("")
    return L
