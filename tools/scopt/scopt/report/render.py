"""Render the report model from :mod:`.collect` (WP13).

Output files (next to ``--out``):

* ``report.md``: the human report (Markdown);
* ``report.json``: the full model (every number in the Markdown comes from it);
* ``timing.csv``: raw times, one row per run.

Rendering never fails on a partial model: every section has an explicit
``not_run``/``unsupported`` text when its data is missing.
"""

from __future__ import annotations

import csv
import io
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .collect import STAGES

__all__ = ["render_markdown", "write_report"]


def _f(v: Any, nd: int = 3) -> str:
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, int):
        return f"{v:,}"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def _scaled(v: Any, scale: float, nd: int = 1) -> str:
    return "—" if v is None else f"{v * scale:,.{nd}f}"


def _bytes(v: Any) -> str:
    if v is None:
        return "—"
    v = int(v)
    for unit, k in (("GiB", 1 << 30), ("MiB", 1 << 20), ("KiB", 1 << 10)):
        if v >= k:
            return f"{v:,} ({v / k:.2f} {unit})"
    return f"{v:,} B"


def _table(head: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    def esc(x: Any) -> str:
        return str(x).replace("|", "\\|").replace("\n", " ")
    out = ["| " + " | ".join(esc(h) for h in head) + " |",
           "|" + "|".join("---" for _ in head) + "|"]
    out += ["| " + " | ".join(esc(c) for c in r) + " |" for r in rows]
    return out


def _ci(c: Mapping[str, Any]) -> str:
    lo, hi = (c.get("ci") or [None, None])
    if lo is None:
        return "—"
    return f"[{lo:.3f}, {hi:.3f}]"


def _input_label(g: Mapping[str, Any], inp: Any) -> str:
    if inp == "*":
        return "all" if len(g["inputs"]) > 1 else "fixed" if g["inputs"] and \
            g["inputs"][0]["index"] is None else "all"
    for i in g["inputs"]:
        if i["index"] == inp:
            return i["name"] or f"input {inp}"
    return str(inp)


def render_markdown(model: Mapping[str, Any]) -> str:
    L: list[str] = []
    L.append("# scopt report")
    L.append("")
    L.append(f"Generated {model.get('generated')} from: " +
             ", ".join(f"`{r}`" for r in model.get("roots", [])))
    found = model.get("inputs_found") or {}
    L.append("")
    L.append("Inputs found: " + ", ".join(f"{k} {v}" for k, v in found.items()) +
             f"; profiles used {len(model.get('profiles_used') or [])}.")
    L.append("")
    L.append("Source inspection, compilation, accepted rewriting, dynamic validation and "
             "measured benefit are separate outcomes. A stage without a ledger row is "
             "`not_run`; no stage is inferred from another.")
    L.append("")

    # ---------------- arms
    L.append("## Arms")
    L.append("")
    arms = model.get("arms") or []
    if arms:
        L += _table(["arm", "status", "kind", "runs", "valid samples", "reason / notes"],
                    [[a["arm"], a["status"], a.get("kind") or "—", _f(a.get("runs")),
                      _f(a.get("valid")),
                      (a.get("reason") or "") + (f" (ablation of {a['parent']}, pass "
                                                 f"{a.get('ablated_pass')})"
                                                 if a.get("parent") else "")]
                     for a in arms])
    else:
        L.append("not_run: no arm information.")
    L.append("")

    # ---------------- ledger
    L.append("## Outcome ledger (stages as columns)")
    L.append("")
    led = model.get("ledger")
    if not led:
        L.append("not_run: no ledger could be read.")
    else:
        L.append(f"{led['rows']} ledger rows from {len(led['files'])} file(s).")
        L.append("")
        if led["table"]:
            L.append("`pass` rows are per candidate: the id is `<pass>@<version> @ <artifact "
                     "or candidate id>` (`unscoped`: a ledger row that no candidate follows).")
            L.append("")
            L += _table(["subject", "id", *STAGES],
                        [[t["subject"], _ledger_id(t),
                          *[_ledger_cell(t["stages"][s]) for s in STAGES]]
                         for t in led["table"]])
        else:
            L.append("No artifact, pass or run rows: every stage is `not_run`.")
            L.append("")
            L += _table(["subject", "id", *STAGES], [["(none)", "—", *["not_run"] * len(STAGES)]])
        L.append("")
        L += _pass_matrix(led, model.get("artifacts") or [])
        if led.get("unit_rows"):
            nsrc = len(led.get("units_by_source") or {})
            L.append("Unit rows (verdict counts per stage). `inspected` counts each (unit, input "
                     "snapshot) once; every other stage is the **sum over the "
                     f"{nsrc} ledger file(s)** (one per candidate), so with several candidates "
                     "it is not a unit count. See the per-file table below.")
            L.append("")
            L += _table(["stage", "verdicts"],
                        [[s, ", ".join(f"{k}: {v}" for k, v in sorted(led["units"][s].items()))
                          or "not_run"] for s in STAGES])
            L.append("")
            ubs = led.get("units_by_source") or {}
            if len(ubs) > 1:
                L.append("`inspected` unit rows are counted once per (unit, input snapshot): "
                         "every candidate optimized from one snapshot repeats them. Other "
                         "unit stages are per candidate; per ledger file:")
                L.append("")
                L += _table(["ledger file", *STAGES],
                            [[src if len(src) <= 72 else "…" + src[-71:],
                              *[", ".join(f"{k}: {v}" for k, v in
                                          sorted((st.get(s) or {}).items())) or "—"
                                for s in STAGES]] for src, st in ubs.items()])
                L.append("")

    # ---------------- timing
    L.append("## Timing")
    L.append("")
    L.append("Speedup = t(reference) / t(arm): above 1 the arm is faster. Paired statistics "
             "use runs from the same interleaved repetition and input; the value is the "
             "median of per-pair ratios with a percentile bootstrap CI. `bench` is the guest "
             "window between the BENCH markers (vCPU throughput on fixed completed work, the "
             "primary metric); `wall` is the full FastDyn+QEMU harness; `cpu` is the "
             "children's user+sys time.")
    L.append("")
    groups = model.get("groups") or []
    if not groups:
        L.append("not_run: no campaign or run.json found.")
        L.append("")
    for g in groups:
        meta = g.get("meta") or {}
        L.append(f"### {g['name']}")
        L.append("")
        bits = [f"reference arm `{g.get('reference')}`",
                f"{g['n_valid']}/{g['n_runs']} runs valid",
                "paired" if g.get("paired") else "unpaired (no repetition keys)"]
        if meta.get("split"):
            bits.append(f"split `{meta['split']}`")
        if meta.get("reps") is not None:
            bits.append(f"{meta['reps']} repetitions, seed {meta.get('seed')}")
        if meta.get("qemu"):
            q = meta["qemu"]
            bits.append(f"QEMU `{q.get('path')}` ({str(q.get('sha256'))[:23]}…)")
        if len(g.get("qemu_sha256_valid") or []) > 1:
            bits.append(f"**{len(g['qemu_sha256_valid'])} different QEMU binaries** among "
                        "the valid samples")
        if meta.get("input_set"):
            bits.append(f"inputs from `{meta['input_set'].get('path')}` (MANIFEST verified)")
        if meta.get("seed_input"):
            si = meta["seed_input"]
            bits.append(f"warm cache recorded on `{Path(si.get('path', '')).name}`"
                        + (" (also a timed input)" if si.get("also_timed") else
                           " (not a timed input)"))
        elif (meta.get("warm_auto") or {}) and g["inputs"] and \
                g["inputs"][0]["index"] is not None:
            bits.append("warm cache recorded on the first timed input")
        if meta.get("freeze_ok") is not None:
            bits.append("freeze check " + ("ok" if meta["freeze_ok"] else "**VIOLATED**"))
        if meta.get("split") == "fixed" or (g["inputs"] and g["inputs"][0]["index"] is None):
            bits.append("fixed-input workload: a held-out evaluation is a re-run of the "
                        "same input")
        L.append("; ".join(bits) + ".")
        hs, he = meta.get("host_start"), meta.get("host_end")
        if hs and he:
            L.append("")
            L.append(f"Host load average at start {hs.get('loadavg')}, at end "
                     f"{he.get('loadavg')} ({hs.get('nproc')} CPUs; other jobs may share "
                     "the host).")
        L.append("")
        # per-arm medians
        rows = []
        for a in g["arms"]:
            pa = g["per_arm"].get(a["name"], {})
            if not pa:
                continue
            rows.append([a["name"], f"{pa['valid']}/{pa['runs']}",
                         _scaled(pa["bench"]["median"], 1e-3), _scaled(pa["bench"]["q1"], 1e-3)
                         + " – " + _scaled(pa["bench"]["q3"], 1e-3),
                         _scaled(pa["bench"]["min"], 1e-3), _scaled(pa["bench"]["max"], 1e-3),
                         _f(pa["wall"]["median"], 4), _f(pa["cpu"]["median"], 4),
                         _f(pa["maxrss_kb"]["median"], 0) if pa["maxrss_kb"]["median"]
                         is not None else "—",
                         _f(pa["appended"]["median"], 1)])
        L.append("**Per arm (valid samples only)**")
        L.append("")
        L += _table(["arm", "valid/runs", "BENCH median µs", "BENCH IQR µs", "min µs", "max µs",
                     "wall median s", "cpu median s", "max RSS median KiB",
                     "appended (fallback) median"], rows)
        L.append("")
        comps = g.get("comparisons") or []
        if comps:
            L.append("**BENCH comparison (paired speedup vs reference)**" if g.get("paired")
                     else "**BENCH comparison (unpaired ratio of medians)**")
            L.append("")
            crow = []
            for c in comps:
                if c["metric"] != "bench":
                    continue
                n = c.get("n_pairs", min(c.get("n_ref", 0), c.get("n_arm", 0)))
                nbad = c.get("n_invalid") or 0
                crow.append([c["arm"], c["reference"], _input_label(g, c["input"]),
                             _f(n) + (f" ({nbad} invalid: " + ", ".join(sorted(
                                 {str(x.get("reason")) for x in c.get("invalid") or []}))
                                 + ")" if nbad else ""),
                             _f(c["median"], 4), _ci(c), _f(c.get("geomean"), 4),
                             _f(c.get("frac_faster"), 2), c["verdict"]])
            L += _table(["arm", "vs", "input", "pairs", "median speedup",
                         f"{int(100 * (comps[0].get('confidence') or 0.95))}% CI",
                         "geomean", "share of pairs faster", "verdict"], crow)
            L.append("")
            L.append("**Full harness and CPU time (paired speedup vs reference)**")
            L.append("")
            crow = []
            for c in comps:
                if c["metric"] == "bench" or c["input"] != "*":
                    continue
                crow.append([c["arm"], c["reference"], c["metric"],
                             _f(c.get("n_pairs", c.get("n_arm"))), _f(c["median"], 4), _ci(c),
                             c["verdict"]])
            L += _table(["arm", "vs", "metric", "pairs", "median speedup", "CI", "verdict"],
                        crow)
            L.append("")
        else:
            L.append("not_run: no comparison (needs a reference arm and another arm).")
            L.append("")
        if any((g["per_arm"].get(a["name"]) or {}).get("cache_after") for a in g["arms"]):
            L.append("**Cache file after the run** (records include fallback translations "
                     "appended during the run; the empty arm's cache is written from scratch)")
            L.append("")
            L += _table(["arm", "records median", "logical size median",
                         "allocated median"],
                        [[a["name"], _f(ca["record_count"]["median"], 0),
                          _bytes(ca["size"]["median"]), _bytes(ca["allocated_bytes"]["median"])]
                         for a in g["arms"]
                         if (ca := (g["per_arm"].get(a["name"]) or {}).get("cache_after"))])
            L.append("")
        # raw times
        L.append("<details><summary>Raw times (every run, execution order)</summary>")
        L.append("")
        L += _table(["pos", "rep", "input", "arm", "valid", "verdict / reason", "BENCH µs",
                     "wall s", "cpu s", "appended"],
                    [[_f(r["pos"]), _f(r["rep"]), _f(r["input"]), r["arm"],
                      "yes" if r["valid"] else "no",
                      r["verdict"] if r["valid"] else f"{r['verdict']} / {r['reason']}",
                      _scaled(r["bench_ns"], 1e-3, 3), _f(r["wall_s"], 4), _f(r["cpu_s"], 4),
                      _f(r["appended"])] for r in g["raw"]])
        L.append("")
        L.append("</details>")
        L.append("")

    # ---------------- cache / artifact metrics
    L.append("## Cache and artifact sizes")
    L.append("")
    L.append("Arena capacity is the configured TCG code arena (`code_size`); the logical file "
             "size includes the sparse holes; allocated is `stat` st_blocks × 512; populated "
             "native bytes are the bytes QEMU wrote for each valid TB (`[tc, end)`: host code "
             "plus search data). TB structs are rebuilt at load and not stored. A sparse "
             "multi-GiB file is not a multi-GiB working set.")
    L.append("")
    scm = model.get("sc_metrics") or []
    if scm:
        def unk(m: Mapping[str, Any], v: str) -> str:
            return v if m.get("status", "ok") == "ok" else \
                f"unknown ({m.get('reason') or 'unreadable'})"
        L += _table(["arm / source", "arena capacity", "logical file size", "allocated",
                     "valid records", "populated native bytes", "host code bytes",
                     "used span"],
                    [[m["label"], unk(m, _bytes(m.get("arena_capacity_bytes"))),
                      _bytes(m.get("logical_file_size")), _bytes(m.get("allocated_bytes")) +
                      (f" [{m['allocated_blocks']:,} blocks]"
                       if m.get("allocated_blocks") is not None else ""),
                      unk(m, f"{m.get('valid_records')}/{m.get('records')}"),
                      unk(m, _bytes(m.get("populated_native_bytes"))),
                      unk(m, _bytes(m.get("host_code_bytes"))),
                      unk(m, _bytes(m.get("used_span_bytes")))] for m in scm])
    else:
        L.append("not_run: no cache source known (no deploy arm and no artifact).")
    L.append("")

    # ---------------- hot-set model
    L.append("## Hot-set locality (model)")
    L.append("")
    hot = model.get("hotset") or []
    if hot:
        L.append("Static model: profile entry counts joined by UnitId with each layout's arena "
                 "offsets. Not a measurement of cache or TLB behaviour. `hot` = the hottest "
                 "units covering the target share of the phase's exact entries.")
        L.append("")
        L += _table(["layout", "profile", "phase", "status", "join coverage", "hot units",
                     "hot code bytes", "64 B lines (code)", "4 KiB pages (code)",
                     "lines/pages with struct", "hot span", "executed units / lines / pages"],
                    [[h["label"], str(h.get("profile_id"))[:19] + "…", h["phase"],
                      h.get("status") + (f" ({h['reason']})" if h.get("reason") else ""),
                      _f(h.get("join_coverage"), 4),
                      _f((h.get("hot") or {}).get("units")),
                      _f((h.get("hot") or {}).get("code_bytes")),
                      _f((h.get("hot") or {}).get("lines64_code")),
                      _f((h.get("hot") or {}).get("pages4k_code")),
                      (f"{h['hot']['lines64_with_struct']} / {h['hot']['pages4k_with_struct']}"
                       if h.get("hot") else "—"),
                      _bytes((h.get("hot") or {}).get("span_bytes")),
                      (f"{h['executed']['units']} / {h['executed']['lines64_code']} / "
                       f"{h['executed']['pages4k_code']}" if h.get("executed") else "—")]
                     for h in hot])
    else:
        L.append("not_run: no Profile v1 file and cache layout to combine.")
    L.append("")

    # ---------------- per-artifact locality model (sidecar x_locality, WP11)
    L += _locality_section(model.get("artifacts") or [])

    # ---------------- artifacts / validation
    L.append("## Artifacts and validation")
    L.append("")
    arts = model.get("artifacts") or []
    if not arts:
        L.append("not_run: no artifact directory found.")
    for a in arts:
        L.append(f"### artifact `{a.get('artifact_id') or a['id']}`")
        L.append("")
        L.append(f"Path `{a['path']}`.")
        sc = a.get("sidecar") or {}
        if sc:
            L.append("")
            L += _table(["field", "value"], [[k, _f(v)] for k, v in sc.items()])
        if a.get("pipeline"):
            L.append("")
            L += _table(["pass", "version", "outcome", "reason", "affected"],
                        [[p.get("pass"), _f(p.get("version")), p.get("outcome"),
                          p.get("reason") or "", _f(len(p.get("affected") or []))]
                         for p in a["pipeline"] if isinstance(p, dict)])
        if a.get("validation") is not None:
            L.append("")
            L += _table(["check", "verdict", "detail"],
                        [[v["code"], v["verdict"], v["detail"]] for v in a["validation"]])
        else:
            L.append("")
            L.append("validation: not_run (" + (a.get("validation_note") or
                                                "no validation.json") + ").")
        L.append("")
    L.append("")

    # ---------------- regressions
    L.append("## Regressions and anomalies")
    L.append("")
    regs = model.get("regressions") or []
    soft = ("expected_baseline_slower", "performance_not_significant")
    real = [r for r in regs if r["kind"] not in soft]
    exp = [r for r in regs if r["kind"] == "expected_baseline_slower"]
    ns = [r for r in regs if r["kind"] == "performance_not_significant"]
    if not real:
        L.append("None found among the measured arms." if groups else
                 "not_run: nothing was measured.")
    else:
        L += _table(["kind", "group", "arm", "detail"],
                    [[r["kind"], r.get("group", "—"), r.get("arm", "—"), _reg_detail(r)]
                     for r in real])
    if ns:
        L.append("")
        L.append("Slower by the median but not significantly (the CI includes 1, or no CI "
                 "with fewer than 2 samples): " + "; ".join(
            f"{r['arm']} vs {r['vs']} {r['metric']}@{_input_label_flat(r['input'])} "
            f"{r['median']:.4f} {_ci(r)}" for r in ns) + ".")
    if exp:
        L.append("")
        L.append("Baseline arms slower than the reference (expected; listed for "
                 "completeness): " + "; ".join(
                     f"{r['group'].split(':', 1)[-1]}: {r['arm']} vs {r['vs']} {r['metric']} "
                     f"{r['median']:.3f}" for r in exp
                     if r["input"] == "*") + ".")
    L.append("")

    # ---------------- B5 (WP30): only with a B5 campaign file
    if model.get("b5"):
        from .b5 import render_b5

        L += render_b5(model["b5"])

    # ---------------- problems
    L.append("## Collection problems")
    L.append("")
    probs = model.get("problems") or []
    if probs:
        L += _table(["where", "problem"], [[p["where"], p["problem"]] for p in probs])
    else:
        L.append("None.")
    L.append("")
    return "\n".join(L)


def _ledger_id(t: Mapping[str, Any]) -> str:
    sid = t["id"][:48] + ("…" if len(t["id"]) > 48 else "")
    if t.get("subject") == "pass":
        sc = t.get("scope")
        sid += " @ " + (_short_id(sc) if sc else "unscoped")
    return sid


def _short_id(x: Any) -> str:
    x = str(x)
    return x[:19] + "…" if len(x) > 20 else x


def _ledger_cell(c: Mapping[str, Any]) -> str:
    """A ledger cell: its verdict; with several rows, every verdict and its count."""
    if c["rows"] <= 1:
        return c["verdict"]
    vs = c.get("verdicts") or {}
    if len(vs) <= 1:
        return f"{c['verdict']} ({c['rows']}×)"
    return ", ".join(f"{k} ×{v}" for k, v in sorted(vs.items())) + f" (latest {c['verdict']})"


def _pass_matrix(led: Mapping[str, Any], arts: Sequence[Mapping[str, Any]]) -> list[str]:
    """Pass outcomes per candidate (one row per candidate, one column per pass)."""
    pbs = led.get("passes_by_scope") or {}
    if not pbs:
        return []
    label: dict[str, str] = {}
    for a in arts:
        name = a.get("pipeline_name") or "?"
        abl = a.get("ablation") or {}
        if abl.get("disable"):
            name += " --disable " + ",".join(abl["disable"])
        if abl.get("only"):
            name += f" --only {abl['only']}"
        for k in (a.get("artifact_id"), a.get("candidate_id")):
            if k:
                label[k] = name
    cols: list[str] = []
    for v in pbs.values():
        for pid, _ in v["passes"]:
            if pid not in cols:
                cols.append(pid)
    rows = []
    for sc, v in pbs.items():
        cells = dict(v["passes"])
        rows.append([_short_id(sc) if sc else "unscoped", label.get(sc, "—"),
                     *[(lambda c: "—" if c is None else
                        f"{c['verdict']} ({c['reason']})" if c.get("reason") else c["verdict"])(
                         cells.get(pid)) for pid in cols]])
    return ["Pass outcomes per candidate (`optimized` stage; `—`: the pass is not in that "
            "candidate's pipeline):", "",
            *_table(["candidate / artifact", "pipeline", *cols], rows), ""]


def _locality_section(arts: Sequence[Mapping[str, Any]]) -> list[str]:
    """``x_locality`` of each artifact sidecar: input layout vs candidate layout."""
    L = ["## Static locality model per artifact (MODEL, not a measurement)", ""]
    locs = [(a, a["locality"]) for a in arts if isinstance(a.get("locality"), Mapping)]
    if not locs:
        L += ["not_run: no artifact sidecar carries `x_locality` (optimize without a "
              "profile, or an artifact older than WP11).", ""]
        return L
    L.append(str(locs[0][1].get("label") or "MODEL") + ". Rows: hottest exact units covering "
             "the share of the phase's exact entries; `lines`/`pages` count distinct 64 B "
             "lines / 4 KiB pages touched by `[tb, end)` (code only in brackets). Units whose "
             "count is lower_bound or unknown are listed separately and never treated as "
             "zero. Whole-TB moves keep every TB on its own 64 B lines, so line counts are "
             "layout-invariant in B2; pages and span are not.")
    L.append("")
    rows, edge_rows, other = [], [], []
    for a, loc in locs:
        name = (a.get("pipeline_name") or "?")
        abl = a.get("ablation") or {}
        if abl.get("disable"):
            name += " --disable " + ",".join(abl["disable"])
        if abl.get("only"):
            name += f" --only {abl['only']}"
        aid = str(a.get("artifact_id") or a.get("id"))[:23] + "…"
        if loc.get("status") not in ("ok", "no_exact_entries"):
            other.append([aid, name, f"{loc.get('status')} ({loc.get('reason', '')})"])
            continue
        for side in ("input", "candidate"):
            m = loc.get(side) or {}
            for c in m.get("coverage") or []:
                rows.append([aid, name, loc.get("phase"), side, f"{100 * c['coverage']:g}%",
                             _f(c.get("units")), _bytes(c.get("hot_bytes")),
                             _bytes(c.get("hot_code_bytes")),
                             f"{c.get('lines64')} ({c.get('lines64_code')})",
                             f"{c.get('pages4k')} ({c.get('pages4k_code')})",
                             _bytes(c.get("span_bytes"))])
            e = m.get("edges") or {}
            ne = m.get("non_exact") or {}
            edge_rows.append([aid, name, side, _f(e.get("weight")),
                              _f(e.get("weighted_mean_distance_bytes"), 1),
                              _f(e.get("adjacent_fraction"), 4),
                              _f(e.get("same_page_fraction"), 4),
                              f"{(ne.get('lower_bound') or {}).get('units', 0)} / "
                              f"{(ne.get('unknown') or {}).get('units', 0)}"])
    if rows:
        L += _table(["artifact", "pipeline", "phase", "layout", "coverage", "units",
                     "hot bytes [tb,end)", "hot code bytes", "64 B lines (code)",
                     "4 KiB pages (code)", "span"], rows)
        L.append("")
        L.append("Profiled transfer edges: every exact, unflagged chain0/chain1/lookup_hit edge "
                 "between two units of the snapshot (hot or not): total weight, weighted mean "
                 "distance from source end to destination start, share of weight laid out "
                 "adjacently and on the same 4 KiB page (MODEL).")
        L.append("")
        L += _table(["artifact", "pipeline", "layout", "edge weight", "mean distance B",
                     "adjacent", "same page", "lower_bound / unknown units"], edge_rows)
        L.append("")
    if other:
        L += _table(["artifact", "pipeline", "status"], other)
        L.append("")
    return L


def _input_label_flat(inp: Any) -> str:
    return "all" if inp == "*" else str(inp)


def _reg_detail(r: Mapping[str, Any]) -> str:
    k = r["kind"]
    if k.startswith("performance") or k.startswith("pass_"):
        ci = r.get("ci") or [None, None]
        cis = "—" if ci[0] is None else f"[{ci[0]:.3f}, {ci[1]:.3f}]"
        return (f"{r['metric']} input {r['input']}: speedup vs {r['vs']} {r['median']:.4f} "
                f"CI {cis}" + (f"; {r['detail']}" if r.get("detail") else ""))
    if k == "output_mismatch":
        return (f"rep {r.get('rep')} input {r.get('input')}: guest output differs from "
                f"{r.get('reference_run')}"
                + ("" if r.get("run_valid", True) else " (the run is not a timing sample)"))
    if k == "mixed_qemu":
        return "valid samples come from different QEMU binaries: " + ", ".join(
            x[:23] + "…" for x in r.get("detail") or [])
    if k == "invalid_runs":
        return f"{r['invalid']}/{r['runs']} runs not samples: {r['reasons']}"
    if k == "fallback_translations":
        return f"{r['runs']} runs appended records (median {r['appended_median']})"
    if k == "locality_model_worse":
        return f"{r['metric']} {r['value']} > warm {r['reference']} (model)"
    return json.dumps({x: y for x, y in r.items() if x not in ("kind", "group", "arm")},
                      default=str)[:300]


def _strip(model: Mapping[str, Any]) -> dict[str, Any]:
    """The model without bulky per-pair arrays duplicated elsewhere."""
    m = json.loads(json.dumps(model, default=str))
    for g in m.get("groups", []):
        for c in g.get("comparisons", []):
            c.pop("pair_keys", None)
    return m


def write_report(model: Mapping[str, Any], out: str | os.PathLike) -> dict[str, str]:
    """Write ``report.md`` (at ``out``), ``report.json`` and ``timing.csv`` beside it."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    md = render_markdown(model)
    out.write_text(md, encoding="utf-8")
    jpath = out.with_suffix(".json")
    jpath.write_text(json.dumps(_strip(model), indent=1, sort_keys=True) + "\n",
                     encoding="utf-8")
    cpath = out.with_name("timing.csv")
    buf = io.StringIO()
    w = csv.writer(buf)
    cols = ["group", "pos", "rep", "input", "arm", "valid", "reason", "verdict", "bench_ns",
            "wall_s", "cpu_s", "maxrss_kb", "appended", "run_dir"]
    w.writerow(cols)
    for g in model.get("groups", []):
        for r in g.get("raw", []):
            w.writerow([g["name"], *[r.get(c) for c in cols[1:]]])
    cpath.write_text(buf.getvalue(), encoding="utf-8")
    return {"markdown": str(out), "json": str(jpath), "csv": str(cpath)}
