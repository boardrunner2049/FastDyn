"""Collect everything a report needs from a workspace (WP13; design §5.9, §5.11, §7, §8).

:func:`collect` walks the given directories and builds one JSON-able model.
It never raises for bad or missing inputs: every problem becomes an entry in
``model["problems"]`` and the affected section renders as ``not_run`` or
``unsupported`` rows. What it reads:

* ``campaign.json`` + ``runs.jsonl`` (``scopt run``, :mod:`scopt.run.arms`):
  arms, schedule, frozen identities, one summary row per run;
* ``run.json`` files of WP8 run directories that no campaign owns ("loose"
  runs, e.g. ``scopt rundir launch``); their arm is taken from the run
  directory label ``<campaign>/<arm>/<rep>``, else from the path
  (``<arm>/<rep>/run.json`` or ``<arm>/run.json``);
* ``*ledger.jsonl`` (WP9/WP10/WP13 outcome ledgers, §5.9);
* artifact directories (``cache.sc`` with ``cache.sc.scopt.json`` and/or
  ``validation.json``);
* Profile v1 files (``profile.json[.gz]``) for the hot-set *model* metrics.

Stages are never inferred from one another (spec §6): a ledger cell with no
row is ``not_run``, even when a later stage has a row.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import time
import traceback
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from . import stats

__all__ = ["collect", "discover", "sc_metrics", "hotset_metrics", "STAGES", "SPEC_ARMS",
           "METRICS"]

STAGES = ("inspected", "optimized", "compiled", "accepted", "validated_dynamic", "measured")
# Spec §8 primary arms, in report order.
SPEC_ARMS = ("nocache", "empty", "warm", "identity", "layout", "semantic", "combined")
SPEC_UNSUPPORTED = {
    "semantic": "capability_not_enabled: semantic cross-TB passes are B3/B4 (need .sc v6)",
    "combined": "capability_not_enabled: needs the semantic arm (B3/B4)",
}
# metric name -> (row key, unit label, scale to display unit)
METRICS = {
    "bench": ("bench_ns", "us", 1e-3),     # guest window (BENCH), vCPU throughput: primary
    "wall": ("wall_s", "s", 1.0),          # full harness (FastDyn + QEMU process)
    "cpu": ("cpu_s", "s", 1.0),            # children user+sys
}
_PRUNE = {"work", "ram", ".git", "__pycache__", "check", "node_modules", "hardware_log"}
_TAG_RE = re.compile(r"^r?(\d+)(?:-i(\d+))?$")


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------

def _load_json(p: Path) -> Any:
    data = p.read_bytes()
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return json.loads(data)


def discover(roots: Sequence[str | os.PathLike]) -> dict[str, list[str]]:
    """Paths of campaign, run, ledger, artifact and profile files under ``roots``."""
    found: dict[str, list[str]] = {k: [] for k in
                                   ("campaigns", "runs", "ledgers", "artifacts", "profiles")}
    seen: set[str] = set()

    def visit_file(p: Path) -> None:
        rp = str(p.resolve())
        if rp in seen:
            return
        seen.add(rp)
        n = p.name
        if n == "campaign.json":
            found["campaigns"].append(rp)
        elif n == "run.json":
            found["runs"].append(rp)
        elif n.endswith("ledger.jsonl"):
            found["ledgers"].append(rp)
        elif n in ("profile.json", "profile.json.gz") or n.endswith(".profile.json"):
            found["profiles"].append(rp)

    for root in roots:
        root = Path(root)
        if root.is_file():
            visit_file(root)
            continue
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if d not in _PRUNE)
            dp = Path(dirpath)
            names = set(filenames)
            if "cache.sc" in names and ({"cache.sc.scopt.json", "validation.json",
                                         "MANIFEST.sha256"} & names):
                rp = str(dp.resolve())
                if rp not in found["artifacts"]:
                    found["artifacts"].append(rp)
            for f in sorted(filenames):
                if f in ("campaign.json", "run.json", "profile.json", "profile.json.gz") or \
                        f.endswith("ledger.jsonl") or f.endswith(".profile.json"):
                    visit_file(dp / f)
                elif dp.name == "rejections" and f.endswith(".json"):
                    # a rejected candidate (store/rejections/<candidate>.json) carries
                    # its ledger rows inside the document; nothing else holds them
                    rp = str((dp / f).resolve())
                    if rp not in seen:
                        seen.add(rp)
                        found["ledgers"].append(rp)
    for k in found:
        found[k] = sorted(dict.fromkeys(found[k]))
    return found


# --------------------------------------------------------------------------
# .sc metrics (spec §5: capacity, logical size, allocated, populated bytes)
# --------------------------------------------------------------------------

_SC_RECORD_FIELDS = ("arena_capacity_bytes", "records", "valid_records", "skipped_records",
                     "populated_native_bytes", "host_code_bytes", "search_data_bytes",
                     "tb_struct_bytes", "prologue_end", "max_end", "used_span_bytes")


def sc_metrics(path: str | os.PathLike) -> dict[str, Any]:
    """Arena capacity, logical file size, allocated blocks and populated native bytes.

    ``status`` is ``ok``, or ``unknown`` when the file cannot be read, QEMU would
    not load it (header problems) or its record array is truncated: then every
    header/record-derived value is ``None`` (never 0, which would read as an
    empty cache) and ``reason`` says why. File size and allocated blocks come
    from ``stat`` and are kept whenever the file exists.
    """
    from ..formats.sc_v5 import read_sc

    p = Path(path)
    doc: dict[str, Any] = {"path": str(p), "status": "unknown", "reason": None,
                           "loadable": None, "header_problems": [], "problems": []}
    doc.update({k: None for k in _SC_RECORD_FIELDS})
    doc.update(logical_file_size=None, allocated_blocks=None, allocated_bytes=None)
    try:
        st = os.stat(p)
        doc.update(logical_file_size=st.st_size, allocated_blocks=st.st_blocks,
                   allocated_bytes=st.st_blocks * 512)  # 512-byte units (stat st_blocks)
        sc = read_sc(p)
    except Exception as e:  # noqa: BLE001 - an unreadable file is a row, not a crash
        doc["reason"] = f"unreadable: {type(e).__name__}: {e}"
        return doc
    doc["loadable"] = sc.loadable
    doc["header_problems"] = list(sc.header_problems)
    doc["problems"] = [f"{f.code} {f.detail}".strip() for f in sc.problems]
    if not sc.loadable:
        doc["reason"] = "not loadable: " + ", ".join(sc.header_problems)
        return doc
    if sc.problems:
        doc["reason"] = "truncated or damaged: " + ", ".join(f.code for f in sc.problems)
        return doc
    h = sc.header
    units = sc.units()
    recs = [u.record for u in units]
    doc.update({
        "status": "ok",
        "arena_capacity_bytes": h.code_size,
        "records": sc.count,
        "valid_records": len(units),
        "skipped_records": len(sc.skipped),
        "populated_native_bytes": sum(r.end_offset - r.tc_offset for r in recs),
        "host_code_bytes": sum(r.tc_size for r in recs),
        "search_data_bytes": sum(r.end_offset - r.tc_offset - r.tc_size for r in recs),
        "tb_struct_bytes": sum(r.tc_offset - r.tb_offset for r in recs),
        "prologue_end": h.prologue_end,
        "max_end": sc.max_end,
        "used_span_bytes": (sc.max_end - min(r.tb_offset for r in recs)) if recs else 0,
        "snapshot_id": sc.snapshot_id(),
    })
    return doc


def _sc_units_layout(path: str | os.PathLike) -> dict[str, tuple[int, int, int, int]]:
    """UnitId -> (tb_offset, tc_offset, tc_size, end_offset) for the valid slots."""
    from ..formats.sc_v6 import read_sc6

    cache = read_sc6(path)
    if not cache.loadable:
        raise ValueError(f"unloadable cache layout: {cache.problem or cache.sc.header_problems}")
    sc = cache.sc
    out = {}
    for uid, u in zip(sc.unit_ids(), sc.units()):
        r = u.record
        out[uid] = (r.tb_offset, r.tc_offset, r.tc_size, r.end_offset)
    return out


def _cover(ranges: Iterable[tuple[int, int]], gran: int) -> int:
    blocks: set[int] = set()
    for a, b in ranges:
        if b > a:
            blocks.update(range(a // gran, (b - 1) // gran + 1))
    return len(blocks)


def hotset_metrics(profile_doc: Mapping[str, Any], sc_path: str | os.PathLike, *,
                   phase: str = "steady", coverage: float = 0.90) -> dict[str, Any]:
    """Static locality *model* (design §7 F-B2-7): hot bytes, 64 B lines and 4 KiB pages
    covering ``coverage`` of the phase's exact entries, for one ``.sc`` layout.

    Units join by UnitId. Only ``exact`` counts are used; units with other
    statuses are counted and reported, never treated as zero. These are
    models from offsets in the arena, not measured cache behaviour.
    """
    layout = _sc_units_layout(sc_path)
    phases = {p["name"]: p for p in profile_doc.get("phases", [])}
    ph = phases.get(phase)
    out: dict[str, Any] = {"label": "model (static offsets x profile counts), not measured",
                           "profile_id": profile_doc.get("profile_id"),
                           "profile_snapshot_id": profile_doc.get("snapshot_id"),
                           "sc": str(sc_path), "phase": phase, "coverage_target": coverage}
    if ph is None:
        out.update(status="not_run", reason=f"phase_missing:{phase}")
        return out
    if not ph.get("reached"):
        out.update(status="not_run", reason=f"phase_not_reached:{phase}")
        return out
    pid = str(ph["id"])
    counts: dict[str, int] = {}
    non_exact = 0
    total = matched = 0
    for u in profile_doc.get("units", []):
        n = int((u.get("phase", {}).get(pid) or {}).get("entries", 0))
        if u.get("status") != "exact":
            non_exact += 1
            continue
        total += n
        if u["unit"] in layout:
            matched += n
            counts[u["unit"]] = n
    out["exact_units"] = len(counts)
    out["non_exact_units"] = non_exact
    out["phase_entries_exact"] = total
    out["join_coverage"] = None if total == 0 else matched / total
    out["units_in_layout"] = len(layout)
    if matched == 0:
        out.update(status="not_run", reason="no_matching_entries")
        return out
    order = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    hot, acc = [], 0
    for uid, n in order:
        if acc >= coverage * matched or n <= 0:
            break
        hot.append(uid)
        acc += n
    executed = [uid for uid, n in order if n > 0]

    def footprint(uids: Sequence[str]) -> dict[str, Any]:
        code = [(layout[u][1], layout[u][1] + layout[u][2]) for u in uids]
        full = [(layout[u][0], layout[u][3]) for u in uids]
        lo = min((a for a, _ in full), default=0)
        hi = max((b for _, b in full), default=0)
        return {"units": len(uids), "code_bytes": sum(b - a for a, b in code),
                "bytes_with_struct": sum(b - a for a, b in full),
                "lines64_code": _cover(code, 64), "pages4k_code": _cover(code, 4096),
                "lines64_with_struct": _cover(full, 64),
                "pages4k_with_struct": _cover(full, 4096), "span_bytes": hi - lo}

    out["hot"] = {**footprint(hot), "entries_covered": acc,
                  "fraction": acc / matched if matched else None}
    out["executed"] = footprint(executed)
    out["status"] = "ok" if (out["join_coverage"] or 0) >= 0.95 else "low_join_coverage"
    return out


# --------------------------------------------------------------------------
# runs
# --------------------------------------------------------------------------

def _sha_lines(lines: Sequence[str] | None) -> str | None:
    import hashlib
    if lines is None:
        return None
    return "sha256:" + hashlib.sha256(("\n".join(lines) + "\n").encode()).hexdigest()


def _loose_row(path: Path, problems: list) -> dict[str, Any] | None:
    from ..run.arms import classify_run

    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        problems.append({"where": str(path), "problem": f"unreadable run.json: {e}"})
        return None
    if doc.get("schema") != "scopt.run/1":
        return None
    root = path.parent
    label = None
    rdj = root / "rundir.json"
    meta: dict[str, Any] = {}
    if rdj.is_file():
        try:
            meta = json.loads(rdj.read_text())
            label = meta.get("label")
        except (OSError, ValueError):
            pass
    campaign, arm, tag = None, None, None
    if label and label.count("/") >= 2:
        campaign, arm, tag = label.split("/")[-3:]
    else:
        m = _TAG_RE.match(root.name)
        if m:
            arm, tag = root.parent.name, root.name
            campaign = root.parent.parent.name
        else:
            arm, campaign = root.name, root.parent.name
    rep = inp = None
    if tag:
        m = _TAG_RE.match(tag)
        if m:
            rep = int(m.group(1))
            inp = None if m.group(2) is None else int(m.group(2))
    deployed = (root / "deploy.json").is_file()
    kind = "deploy" if deployed else ("empty" if meta.get("cache_name") else "nocache")
    cache_present = any((root / "cache").glob("*.sc")) if (root / "cache").is_dir() else False
    valid, reason = classify_run(kind, doc, cache_present=cache_present)
    t = doc.get("timings") or {}
    cpu = None
    if t.get("children_user_s") is not None and t.get("children_sys_s") is not None:
        cpu = t["children_user_s"] + t["children_sys_s"]
    bench = doc.get("bench") or []
    return {
        "campaign": f"loose:{campaign}", "arm": arm, "kind": kind, "rep": rep, "input": inp,
        "input_name": None, "split": None, "run_dir": str(root), "run_json": str(path),
        "valid": valid, "reason": reason, "verdict": doc.get("verdict"),
        "exit_code": doc.get("exit_code"), "timed_out": doc.get("timed_out"),
        "bench": bench, "bench_ns": sum(int(b["elapsed_ns"]) for b in bench) if bench else None,
        "wall_s": t.get("wall_s"), "cpu_s": cpu, "maxrss_kb": t.get("children_maxrss_kb"),
        "appended": doc.get("appended"), "qemu": doc.get("qemu"),
        "qemu_sha256": doc.get("qemu_sha256"),
        "guest_norm_sha256": _sha_lines(doc.get("guest_norm")),
        "guest_norm": doc.get("guest_norm"), "started": t.get("start_unix"),
        "deployed_sc": ((json.loads((root / "deploy.json").read_text()).get("artifact") or {})
                        .get("sc") if deployed else None),
    }


def _campaign(path: Path, problems: list) -> dict[str, Any] | None:
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        problems.append({"where": str(path), "problem": f"unreadable campaign.json: {e}"})
        return None
    if doc.get("schema") != "scopt.campaign/1":
        return None
    rows = []
    rj = path.parent / "runs.jsonl"
    if rj.is_file():
        for n, line in enumerate(rj.read_text().splitlines(), 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except ValueError as e:
                problems.append({"where": f"{rj}:{n}", "problem": f"bad row: {e}"})
    else:
        problems.append({"where": str(path), "problem": "runs.jsonl missing (campaign not run?)"})
    return {"path": str(path), "dir": str(path.parent), "doc": doc, "rows": rows}


# --------------------------------------------------------------------------
# timing analysis
# --------------------------------------------------------------------------

def _pairs(rows: Sequence[Mapping[str, Any]], a: str, b: str, key: str,
           inp: Any = "*") -> tuple[list[float], list[float], list[Any]]:
    """Values of ``key`` for arms a and b in the same (rep, input) pair, both valid."""
    by: dict[tuple, dict[str, float]] = defaultdict(dict)
    for r in rows:
        if not r.get("valid") or r.get(key) is None or r.get("rep") is None:
            continue
        if inp != "*" and r.get("input") != inp:
            continue
        by[(r["rep"], r.get("input"))][r["arm"]] = float(r[key])
    xs, ys, ks = [], [], []
    for k in sorted(by, key=lambda t: (t[0], -1 if t[1] is None else t[1])):
        d = by[k]
        if a in d and b in d:
            xs.append(d[a])
            ys.append(d[b])
            ks.append(list(k))
    return xs, ys, ks


def _analyze_group(name: str, rows: list[dict[str, Any]], arms: list[dict[str, Any]],
                   reference: str | None, *, bootstrap: int, seed: int) -> dict[str, Any]:
    arm_names = [a["name"] for a in arms]
    inputs = sorted({r.get("input") for r in rows}, key=lambda x: -1 if x is None else x)
    input_names = {}
    for r in rows:
        input_names.setdefault(r.get("input"), r.get("input_name"))
    paired = any(r.get("rep") is not None for r in rows)
    out: dict[str, Any] = {"name": name, "reference": reference, "arms": arms,
                           "qemu_sha256_valid": sorted({str(r["qemu_sha256"]) for r in rows
                                                        if r.get("valid")
                                                        and r.get("qemu_sha256")}),
                           "inputs": [{"index": i, "name": input_names.get(i)} for i in inputs],
                           "paired": paired, "per_arm": {}, "comparisons": [],
                           "raw": [], "correctness": [], "n_runs": len(rows),
                           "n_valid": sum(1 for r in rows if r.get("valid"))}
    for a in arm_names:
        mine = [r for r in rows if r["arm"] == a]
        valid = [r for r in mine if r.get("valid")]
        pa: dict[str, Any] = {
            "runs": len(mine), "valid": len(valid),
            "invalid_reasons": dict(Counter(str(r.get("reason")) for r in mine
                                            if not r.get("valid"))),
            "appended": stats.describe([r["appended"] for r in valid
                                        if r.get("appended") is not None]),
            "appended_nonzero_runs": sum(1 for r in valid if (r.get("appended") or 0) > 0),
            "maxrss_kb": stats.describe([r["maxrss_kb"] for r in valid
                                         if r.get("maxrss_kb") is not None]),
            "guest_norm_variants": len({r.get("guest_norm_sha256") for r in valid}),
        }
        for m, (key, _unit, _sc) in METRICS.items():
            pa[m] = stats.describe([r[key] for r in valid if r.get(key) is not None])
        ca = [r["cache_after"] for r in valid if isinstance(r.get("cache_after"), dict)
              and "record_count" in r["cache_after"]]
        if ca:
            pa["cache_after"] = {
                "record_count": stats.describe([c["record_count"] for c in ca]),
                "allocated_bytes": stats.describe([c["allocated_bytes"] for c in ca]),
                "size": stats.describe([c["size"] for c in ca]),
            }
        out["per_arm"][a] = pa
    # comparisons: every arm vs the reference; ablations vs their parent
    comps: list[tuple[str, str, str]] = []
    if reference in arm_names:
        comps += [(reference, a, "vs_reference") for a in arm_names if a != reference]
    for a in arms:
        if a.get("parent") in arm_names:
            comps.append((a["parent"], a["name"], "ablation_vs_parent"))
    k = 0
    for ref, arm, why in comps:
        for inp in ([*inputs, "*"] if len(inputs) > 1 else ["*"]):
            for m, (key, _unit, _sc) in METRICS.items():
                k += 1
                if paired:
                    xs, ys, ks = _pairs(rows, ref, arm, key, inp)
                    s = stats.paired_speedup(xs, ys, reps=bootstrap, seed=seed + k)
                    s["pair_keys"] = ks
                else:
                    xs = [r[key] for r in rows if r["arm"] == ref and r.get("valid")
                          and r.get(key) is not None and (inp == "*" or r.get("input") == inp)]
                    ys = [r[key] for r in rows if r["arm"] == arm and r.get("valid")
                          and r.get(key) is not None and (inp == "*" or r.get("input") == inp)]
                    s = stats.unpaired_speedup(xs, ys, reps=bootstrap, seed=seed + k)
                out["comparisons"].append({"reference": ref, "arm": arm, "why": why,
                                           "input": inp, "metric": m, **s})
    # raw times
    for r in sorted(rows, key=lambda r: (r.get("pos") if r.get("pos") is not None else 1 << 30,
                                         r.get("started") or 0)):
        out["raw"].append({k2: r.get(k2) for k2 in (
            "pos", "rep", "input", "arm", "valid", "reason", "verdict", "bench_ns", "wall_s",
            "cpu_s", "maxrss_kb", "appended", "run_dir")})
    # correctness: guest-visible output vs the reference run of the same pair
    if reference in arm_names:
        refmap = {(r.get("rep"), r.get("input")): r for r in rows
                  if r["arm"] == reference and r.get("valid")}
        refany = {}
        for r in rows:
            if r["arm"] == reference and r.get("valid"):
                refany.setdefault(r.get("input"), r)
        for r in rows:
            if r["arm"] == reference or not r.get("guest_norm_sha256"):
                continue
            ref_r = refmap.get((r.get("rep"), r.get("input"))) or refany.get(r.get("input"))
            if ref_r is None:
                continue
            if r["guest_norm_sha256"] != ref_r.get("guest_norm_sha256"):
                diff = None
                if r.get("guest_norm") is not None and ref_r.get("guest_norm") is not None:
                    import difflib
                    diff = list(difflib.unified_diff(ref_r["guest_norm"], r["guest_norm"],
                                                     reference, r["arm"], lineterm="", n=0))[:20]
                out["correctness"].append({"arm": r["arm"], "rep": r.get("rep"),
                                           "input": r.get("input"), "run_dir": r["run_dir"],
                                           "run_valid": bool(r.get("valid")),
                                           "reference_run": ref_r["run_dir"], "diff": diff})
    return out


# --------------------------------------------------------------------------
# ledger
# --------------------------------------------------------------------------

def _pass_scopes(rows: Sequence[Mapping[str, Any]], art_id: str | None,
                 aliases: Mapping[str, Sequence[str]]) -> list[str | None]:
    """The candidate each ``pass`` row of one ledger file belongs to (WP11).

    Pass rows (§5.9 ``subject: pass``, id ``<name>@<version>``) carry no
    candidate id: the PassManager writes them before the candidate exists.
    A ledger inside a published artifact directory (``art_id``) belongs to that
    artifact. Elsewhere (a rejected candidate, a merged ledger) a pass row
    belongs to the candidate of the next ``artifact`` row of the same file
    (ValidateAndPackage writes the artifact rows right after the pass rows),
    mapped to its artifact_id through ``aliases`` when exactly one artifact was
    published from it (else the candidate_id itself). ``None``:
    no candidate follows (reported as ``unscoped``). Non-pass rows get ``None``.
    """
    out: list[str | None] = [None] * len(rows)
    pending: list[int] = []
    for i, r in enumerate(rows):
        if r["subject"] == "pass":
            if art_id is not None:
                out[i] = art_id
            else:
                pending.append(i)
        elif r["subject"] == "artifact" and pending:
            arts = aliases.get(r["id"]) or ()
            scope = arts[0] if len(arts) == 1 else r["id"]
            for j in pending:
                out[j] = scope
            pending = []
    return out


def _ledger_lines(p: Path) -> list[str]:
    """JSONL lines of a ledger file, or of the ``ledger`` rows embedded in a
    rejection document (``scopt.rejection``, ``store/rejections/<candidate>.json``)."""
    if p.suffix == ".json":
        doc = _load_json(p)
        if not isinstance(doc, dict) or doc.get("schema") != "scopt.rejection":
            raise ValueError("not a scopt.rejection document")
        return [json.dumps(r) for r in doc.get("ledger") or []]
    return p.read_text().splitlines()


def _ledger(paths: Sequence[str], problems: list,
            aliases: Mapping[str, Any] | None = None,
            art_dirs: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Ledger table. ``aliases`` maps a candidate_id to its published artifact_ids:
    WP10 writes the ``inspected``/``compiled``/``accepted`` rows of an artifact
    under its candidate_id (the artifact_id is the hash of a MANIFEST that covers
    the ledger, so it cannot appear in its own rows), while ``validated_dynamic``
    and ``measured`` rows use the artifact_id. Both are one subject; the row keeps
    the artifact_id and lists the candidate_id under ``aliases``.

    Several artifacts can share one candidate_id (the same input optimized
    twice). A candidate row read from a ledger inside an artifact directory
    (``art_dirs``: resolved directory -> artifact_id) joins that artifact only;
    a candidate row from anywhere else joins every artifact of the candidate.

    Unit ``inspected`` rows are counted once per (unit, snapshot): several
    optimize invocations of one snapshot repeat them, and the report counts
    units, not invocations (the latest row wins).

    ``pass`` rows are kept per candidate (``scope``, see :func:`_pass_scopes`):
    one pipeline's pass gets one table row per candidate, never one row that
    mixes the verdicts of several candidates (WP11). ``passes_by_scope`` lists
    them per candidate in pipeline order. Every cell also counts its rows'
    verdicts (``verdicts``), so several rows in one cell stay visible."""
    from ..ledger import LedgerError, LedgerRow

    aliases = {k: ([v] if isinstance(v, str) else list(v)) for k, v in (aliases or {}).items()}
    art_dirs = dict(art_dirs or {})

    rows: list[dict[str, Any]] = []
    seen: set[tuple] = set()
    units_by_source: dict[str, dict[str, Counter]] = {}
    for p in paths:
        try:
            lines = _ledger_lines(Path(p))
        except (OSError, ValueError) as e:
            problems.append({"where": p, "problem": f"unreadable ledger: {e}"})
            continue
        frows: list[dict[str, Any]] = []
        for n, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                d = json.loads(line)
                frows.append(LedgerRow.from_json(d).to_json())
            except (ValueError, KeyError, LedgerError) as e:
                problems.append({"where": f"{p}:{n}", "problem": f"bad ledger row: {e}"})
                continue
        scopes = _pass_scopes(frows, art_dirs.get(str(Path(p).resolve().parent)), aliases)
        for r, scope in zip(frows, scopes):
            r["scope"] = scope
            key = tuple(r[k] for k in ("subject", "id", "stage", "verdict", "reason",
                                       "evidence_ref", "ts", "scope"))
            if r["subject"] == "unit":
                by_src = units_by_source.setdefault(p, {})
                by_src.setdefault(r["stage"], Counter())[r["verdict"]] += 1
            if key in seen:
                continue
            seen.add(key)
            r["source"] = p
            rows.append(r)
    subjects: dict[tuple[str, str, str], dict[str, Any]] = {}
    units: dict[str, Counter] = {s: Counter() for s in STAGES}
    runs: dict[str, dict[str, Counter]] = defaultdict(lambda: {s: Counter() for s in STAGES})
    alias_of: dict[str, set[str]] = {}
    inspected_units: dict[tuple[str, Any], str] = {}
    # scope ("" = unscoped) -> pass id -> latest row; pipeline order = first-seen order
    passes_by_scope: dict[str, dict[str, dict[str, Any]]] = {}
    for r in sorted(rows, key=lambda r: r["ts"]):
        if r["subject"] == "unit":
            if r["stage"] == "inspected":
                inspected_units[(r["id"], r["evidence_ref"])] = r["verdict"]
            else:
                units[r["stage"]][r["verdict"]] += 1
            continue
        if r["subject"] == "run" and r["id"].count("/") >= 2:
            # <campaign>/<arm>/<rep>: one table row per campaign arm, verdicts counted
            runs[r["id"].rsplit("/", 1)[0]][r["stage"]][r["verdict"]] += 1
            continue
        sids = [r["id"]]
        if r["subject"] == "artifact" and r["id"] in aliases:
            own = art_dirs.get(str(Path(r["source"]).resolve().parent))
            sids = [own] if own in aliases[r["id"]] else aliases[r["id"]]
            for sid in sids:
                alias_of.setdefault(sid, set()).add(r["id"])
        scope = r.get("scope") if r["subject"] == "pass" else None
        if r["subject"] == "pass":
            by_scope = passes_by_scope.setdefault(scope or "", {})
            by_scope[r["id"]] = {"verdict": r["verdict"], "reason": r["reason"],
                                 "evidence_ref": r["evidence_ref"],
                                 "rows": by_scope.get(r["id"], {}).get("rows", 0) + 1}
        for sid in sids:
            cell = subjects.setdefault((r["subject"], sid, scope or ""), {}).setdefault(
                r["stage"], {"verdict": None, "reason": "", "rows": 0, "evidence_ref": None,
                             "verdicts": {}})
            cell.update(verdict=r["verdict"], reason=r["reason"],
                        evidence_ref=r["evidence_ref"])
            if r.get("counts") is not None:
                cell["counts"] = r["counts"]
            cell["rows"] += 1
            cell["verdicts"][r["verdict"]] = cell["verdicts"].get(r["verdict"], 0) + 1
    for v in inspected_units.values():
        units["inspected"][v] += 1
    table = []
    for (subj, sid, scope), cells in sorted(subjects.items()):
        t = {"subject": subj, "id": sid,
             "stages": {s: cells.get(s) or {"verdict": "not_run", "reason": "",
                                            "rows": 0, "evidence_ref": None}
                        for s in STAGES}}
        if subj == "pass":
            t["scope"] = scope or None  # the candidate/artifact this pass row belongs to
        if subj == "artifact" and sid in alias_of:
            t["aliases"] = sorted(alias_of[sid])
        table.append(t)
    for grp, st in sorted(runs.items()):
        table.append({"subject": "run", "id": f"{grp}/*", "aggregated": True,
                      "stages": {s: ({"verdict": ", ".join(f"{k}: {v}" for k, v in
                                                          sorted(st[s].items())),
                                      "reason": "", "rows": 1, "evidence_ref": None}
                                     if st[s] else {"verdict": "not_run", "reason": "",
                                                    "rows": 0, "evidence_ref": None})
                                 for s in STAGES}})
    return {"files": list(paths), "rows": len(rows), "table": table,
            "units": {s: dict(c) for s, c in units.items()},
            "unit_rows": sum(sum(c.values()) for c in units.values()),
            # per ledger file (one per artifact): unit rows that are not
            # de-duplicated across candidates (e.g. ``compiled``) stay attributable
            "units_by_source": {src: {s: dict(c) for s, c in st.items()}
                                for src, st in sorted(units_by_source.items())},
            "passes_by_scope": {sc: {"scope": sc or None, "passes": list(ps.items())}
                                for sc, ps in sorted(passes_by_scope.items())}}


# --------------------------------------------------------------------------
# artifacts and validation
# --------------------------------------------------------------------------

def _artifact(path: str, problems: list) -> dict[str, Any]:
    p = Path(path)
    out: dict[str, Any] = {"path": path, "id": p.name}
    side = p / "cache.sc.scopt.json"
    if side.is_file():
        try:
            sd = json.loads(side.read_text())
            out["sidecar"] = {k: sd.get(k) for k in (
                "input_snapshot_id", "output_snapshot_id", "bundle_id", "runtime_identity_id",
                "loader_requirement", "profile_ids")}
            out["pipeline"] = sd.get("pipeline") or []
            out["pinned"] = len(sd.get("pinned") or sd.get("pinned_units") or [])
            out["moved"] = len(sd.get("move_map") or {})
            if sd.get("x_candidate_id"):
                out["candidate_id"] = sd["x_candidate_id"]
            if sd.get("x_pipeline_name"):
                out["pipeline_name"] = sd["x_pipeline_name"]
            if sd.get("x_ablation"):
                out["ablation"] = sd["x_ablation"]
            if isinstance(sd.get("x_locality"), dict):
                out["locality"] = sd["x_locality"]  # static locality MODEL (WP11)
        except (OSError, ValueError) as e:
            problems.append({"where": str(side), "problem": f"unreadable sidecar: {e}"})
    man = p / "MANIFEST.sha256"
    if man.is_file():
        from ..ids import artifact_id
        out["artifact_id"] = artifact_id(man.read_bytes())
    val = p / "validation.json"
    if val.is_file():
        try:
            vd = json.loads(val.read_text())
            checks = vd.get("checks", vd.get("rows", [])) if isinstance(vd, dict) else vd
            rows = [{"code": c.get("code") or c.get("check") or c.get("id"),
                     "check": c.get("check") if c.get("code") else None,
                     "verdict": c.get("verdict") or c.get("result") or (
                         "PASS" if c.get("ok") else "FAIL" if "ok" in c else None),
                     "detail": str(c.get("detail") or c.get("reason") or "")[:200]}
                    for c in (checks if isinstance(checks, list) else []) if isinstance(c, dict)]
            # an empty/unrecognized validation.json (e.g. "{}") holds no check: not_run
            out["validation"] = rows or None
            if not rows:
                out["validation_note"] = "validation.json has no check rows"
            if isinstance(vd, dict) and "verdict" in vd:
                out["validation_verdict"] = vd["verdict"]
        except (OSError, ValueError, AttributeError) as e:
            problems.append({"where": str(val), "problem": f"unreadable validation.json: {e}"})
    try:
        out["sc"] = sc_metrics(p / "cache.sc")
    except Exception as e:  # noqa: BLE001
        problems.append({"where": str(p / "cache.sc"), "problem": f"sc metrics failed: {e}"})
    return out


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def _guard(problems: list, where: str):
    class _G:
        def __enter__(self):
            return self

        def __exit__(self, et, ev, tb):
            if et is not None and issubclass(et, Exception):
                problems.append({"where": where, "problem": f"{et.__name__}: {ev}",
                                 "traceback": "".join(traceback.format_tb(tb))[-1500:]})
                return True
            return False
    return _G()


def collect(roots: Sequence[str | os.PathLike], *, profiles: Sequence[str] = (),
            ledgers: Sequence[str] = (), artifacts: Sequence[str] = (),
            scs: Mapping[str, str] | None = None, reference: str | None = None,
            bootstrap: int = stats.DEFAULT_BOOTSTRAP, seed: int = 0,
            hot_coverage: float = 0.90, phase: str = "steady",
            b5_campaign: str | None = None) -> dict[str, Any]:
    """Build the report model. Never raises for missing or malformed inputs.

    With ``b5_campaign`` (a ``campaigns/b5-*.toml`` definition or frozen campaign; also
    found as ``b5-campaign.toml`` in a root) the model gets the B5 section
    (:mod:`scopt.report.b5`): every campaign arm per split, including
    ``skipped_unsupported`` and excluded arms, and the §10.2 metric rows."""
    problems: list[dict[str, Any]] = []
    model: dict[str, Any] = {
        "schema": "scopt.report/1",
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "roots": [str(Path(r).resolve()) for r in roots],
        "problems": problems, "groups": [], "arms": [], "ledger": None, "artifacts": [],
        "sc_metrics": [], "hotset": [], "regressions": [], "campaigns": [],
    }
    found = {k: [] for k in ("campaigns", "runs", "ledgers", "artifacts", "profiles")}
    with _guard(problems, "discover"):
        found = discover(roots)
    found["ledgers"] = sorted(dict.fromkeys([*found["ledgers"],
                                             *(str(Path(x).resolve()) for x in ledgers)]))
    found["artifacts"] = sorted(dict.fromkeys([*found["artifacts"],
                                               *(str(Path(x).resolve()) for x in artifacts)]))
    explicit_profiles = [str(Path(x).resolve()) for x in profiles]
    model["inputs_found"] = {k: len(v) for k, v in found.items()}

    # ---- campaigns and runs
    groups: list[tuple[str, list[dict], list[dict], str | None, dict]] = []
    owned: set[str] = set()
    for cp in found["campaigns"]:
        with _guard(problems, cp):
            c = _campaign(Path(cp), problems)
            if c is None:
                continue
            cd = c["doc"]
            camp = cd.get("campaign", {})
            arms = [dict(a) for a in camp.get("arms", [])]
            ids = (cd.get("identities") or {}).get("arms", {})
            for a in arms:
                a["identity"] = ids.get(a["name"])
            for r in c["rows"]:
                if r.get("run_json"):
                    owned.add(str(Path(r["run_json"]).resolve()))
            owned.update(str(p.resolve()) for p in Path(c["dir"]).rglob("run.json"))
            meta = {"path": cp, "name": camp.get("name"), "split": camp.get("split"),
                    "reps": camp.get("reps"), "seed": camp.get("seed"),
                    "status": cd.get("status"), "freeze_ok": cd.get("freeze_ok"),
                    "freeze_check": cd.get("freeze_check"),
                    "qemu": (cd.get("identities") or {}).get("qemu"),
                    "base_config": (cd.get("identities") or {}).get("base_config"),
                    "nocache_config": (cd.get("identities") or {}).get("nocache_config"),
                    "inputs": (cd.get("identities") or {}).get("inputs"),
                    "input_set": (cd.get("identities") or {}).get("input_set"),
                    "seed_input": (cd.get("identities") or {}).get("seed_input"),
                    "qemu_seen": cd.get("qemu_seen"),
                    "host_start": cd.get("host_start"), "host_end": cd.get("host_end"),
                    "unsupported": camp.get("unsupported") or {},
                    "not_run": camp.get("not_run") or {},
                    "arm_names": [a.get("name") for a in arms],
                    "warm_auto": cd.get("warm_auto") or {},
                    "n_runs": len(c["rows"]), "n_scheduled": len(cd.get("schedule") or [])}
            model["campaigns"].append(meta)
            groups.append((f"campaign:{camp.get('name')}", c["rows"], arms,
                           reference or camp.get("reference"), meta))
    loose: dict[str, list[dict]] = defaultdict(list)
    for rp in found["runs"]:
        if rp in owned:
            continue
        with _guard(problems, rp):
            r = _loose_row(Path(rp), problems)
            if r is not None:
                loose[r["campaign"]].append(r)
    for gname, rows in sorted(loose.items()):
        arm_names = list(dict.fromkeys(r["arm"] for r in sorted(
            rows, key=lambda r: (SPEC_ARMS.index(r["arm"]) if r["arm"] in SPEC_ARMS else 99,
                                 r["arm"]))))
        kinds = {r["arm"]: r["kind"] for r in rows}
        scs_l = {r["arm"]: r.get("deployed_sc") for r in rows if r.get("deployed_sc")}
        arms = [{"name": a, "kind": kinds[a], "source": scs_l.get(a),
                 "role": "loose", "identity": None} for a in arm_names]
        ref = reference if reference in arm_names else (
            "warm" if "warm" in arm_names else next(
                (a for a in arm_names if kinds[a] == "deploy"), None))
        meta = {"path": None, "name": gname, "loose": True}
        groups.append((gname, rows, arms, ref, meta))

    for gname, rows, arms, ref, meta in groups:
        with _guard(problems, f"timing:{gname}"):
            g = _analyze_group(gname, rows, arms, ref, bootstrap=bootstrap, seed=seed)
            g["meta"] = meta
            model["groups"].append(g)

    # ---- arm status table (spec primary arms + any extra arm)
    with _guard(problems, "arms"):
        model["arms"] = _arm_status(model["groups"], model["campaigns"])

    # ---- .sc metrics per arm source (+ extra .sc paths)
    sources: dict[str, str] = {}
    for g in model["groups"]:
        for a in g["arms"]:
            ident = a.get("identity") or {}
            sc = ident.get("sc") or a.get("source")
            if sc and sc != "auto":
                sc = str(Path(sc) / "cache.sc") if Path(sc).is_dir() else sc
                sources.setdefault(f"{g['name']}/{a['name']}", sc)
    for k, v in (scs or {}).items():
        sources[k] = v
    for label, sc in sources.items():
        with _guard(problems, f"sc:{label}"):
            d = sc_metrics(sc)
            d["label"] = label
            model["sc_metrics"].append(d)

    # ---- artifacts
    for ap in found["artifacts"]:
        with _guard(problems, f"artifact:{ap}"):
            model["artifacts"].append(_artifact(ap, problems))

    # ---- ledger
    with _guard(problems, "ledger"):
        aliases: dict[str, list[str]] = {}
        art_dirs: dict[str, str] = {}
        for a in model["artifacts"]:
            if a.get("artifact_id"):
                # the ledger inside an artifact dir is that artifact's (pass rows, WP11)
                art_dirs[str(Path(a["path"]).resolve())] = a["artifact_id"]
            if a.get("candidate_id") and a.get("artifact_id"):
                # re-optimizing the same input gives the same candidate id and a new
                # ArtifactId (ledger timestamps): keep every artifact
                lst = aliases.setdefault(a["candidate_id"], [])
                if a["artifact_id"] not in lst:
                    lst.append(a["artifact_id"])
        model["ledger"] = _ledger(found["ledgers"], problems, aliases, art_dirs)
        _ledger_add_missing(model)

    # ---- hot-set model metrics
    prof_paths = explicit_profiles or found["profiles"]
    model["profiles_used"] = prof_paths
    profs = []
    for pp in prof_paths:
        with _guard(problems, f"profile:{pp}"):
            d = _load_json(Path(pp))
            if d.get("schema") == "fastdyn.profile":
                profs.append((pp, d))
    seen_pid: set[str] = set()
    for pp, d in profs:
        if d.get("profile_id") in seen_pid:
            continue
        seen_pid.add(d.get("profile_id"))
        for m in model["sc_metrics"]:
            with _guard(problems, f"hotset:{pp}:{m['label']}"):
                h = hotset_metrics(d, m["path"], phase=phase, coverage=hot_coverage)
                h["profile_path"] = pp
                h["label"] = m["label"]
                model["hotset"].append(h)

    with _guard(problems, "regressions"):
        model["regressions"] = _regressions(model)

    # ---- B5 section (WP30)
    with _guard(problems, "b5"):
        from .b5 import b5_section, find_b5_campaign, load_b5

        b5p = b5_campaign or find_b5_campaign(roots)
        if b5p:
            model["b5"] = b5_section(model, load_b5(b5p), roots=roots, bootstrap=bootstrap,
                                     seed=seed, problems=problems)
    return model


def _arm_status(groups: list[dict], campaigns: list[dict]) -> list[dict[str, Any]]:
    unsupported: dict[str, str] = dict(SPEC_UNSUPPORTED)
    not_run: dict[str, str] = {}
    requested: set[str] = set()
    for c in campaigns:
        unsupported.update(c.get("unsupported") or {})
        not_run.update(c.get("not_run") or {})
        requested.update(c.get("not_run") or {})
        requested.update(c.get("arm_names") or [])
    rows: dict[str, dict[str, Any]] = {}
    for g in groups:
        for a in g["arms"]:
            pa = g["per_arm"].get(a["name"], {})
            r = rows.setdefault(a["name"], {"arm": a["name"], "kind": a.get("kind"),
                                            "groups": [], "runs": 0, "valid": 0,
                                            "role": a.get("role")})
            r["groups"].append(g["name"])
            r["runs"] += pa.get("runs", 0)
            r["valid"] += pa.get("valid", 0)
            if a.get("parent"):
                r["parent"] = a["parent"]
                r["ablated_pass"] = a.get("ablated_pass")
    out = []
    names = list(SPEC_ARMS) + sorted(n for n in rows if n not in SPEC_ARMS)
    for n in names:
        r = rows.get(n)
        if r is None:
            if n in unsupported:
                out.append({"arm": n, "status": "unsupported", "reason": unsupported[n]})
            elif n in not_run:
                out.append({"arm": n, "status": "not_run", "reason": not_run[n]})
            elif campaigns and n not in requested:
                # a campaign exists but none asked for this arm: not a missing run
                out.append({"arm": n, "status": "not_requested",
                            "reason": "not requested by any campaign"})
            else:
                out.append({"arm": n, "status": "not_run", "reason": "no runs found"})
            continue
        r = dict(r)
        r["status"] = "measured" if r["valid"] else "not_measured"
        r["reason"] = "" if r["valid"] else ("no valid sample" if r["runs"] else "no runs")
        out.append(r)
    return out


def _ledger_add_missing(model: dict[str, Any]) -> None:
    """Artifacts seen in campaigns or on disk but absent from every ledger get a row of
    ``not_run`` cells (they are never silently dropped)."""
    led = model["ledger"]
    have = {(t["subject"], t["id"]) for t in led["table"]}
    ids = []
    for g in model["groups"]:
        for a in g["arms"]:
            ident = a.get("identity") or {}
            aid = ident.get("artifact_id")
            if aid and ident.get("kind") == "artifact_dir":  # a bare .sc is not an artifact
                ids.append(aid)
    for a in model["artifacts"]:
        ids.append(a.get("artifact_id") or a["id"])
    for aid in dict.fromkeys(ids):
        if ("artifact", aid) not in have:
            led["table"].append({"subject": "artifact", "id": aid, "no_ledger_rows": True,
                                 "stages": {s: {"verdict": "not_run", "reason": "",
                                                "rows": 0, "evidence_ref": None}
                                            for s in STAGES}})


def _regressions(model: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for g in model["groups"]:
        roles = {a["name"]: a.get("role") for a in g["arms"]}
        kinds = {a["name"]: a.get("kind") for a in g["arms"]}
        for c in g["comparisons"]:
            if c["median"] is None:
                continue
            baseline = kinds.get(c["arm"]) in ("nocache", "empty")
            if c["why"] == "ablation_vs_parent":
                # the parent is the reference here: a speedup > 1 means disabling the pass helps
                if c["verdict"] == "faster" or (c["median"] > 1.0 and c["metric"] == "bench"):
                    out.append({"kind": "pass_hurts" if c["verdict"] == "faster"
                                else "pass_possibly_hurts", "group": g["name"],
                                "arm": c["arm"], "vs": c["reference"], "metric": c["metric"],
                                "input": c["input"], "median": c["median"], "ci": c["ci"],
                                "detail": "removing the pass is faster than the full pipeline"})
                continue
            if c["verdict"] == "slower" or c["median"] < 1.0:
                out.append({"kind": ("expected_baseline_slower" if baseline else
                                     "performance" if c["verdict"] == "slower"
                                     else "performance_not_significant"),
                            "group": g["name"], "arm": c["arm"], "vs": c["reference"],
                            "metric": c["metric"], "input": c["input"], "median": c["median"],
                            "ci": c["ci"], "role": roles.get(c["arm"])})
        for x in g["correctness"]:
            out.append({"kind": "output_mismatch", "group": g["name"], **x})
        for a, pa in g["per_arm"].items():
            if pa.get("runs") and pa["valid"] < pa["runs"]:
                out.append({"kind": "invalid_runs", "group": g["name"], "arm": a,
                            "invalid": pa["runs"] - pa["valid"], "runs": pa["runs"],
                            "reasons": pa["invalid_reasons"]})
            if kinds.get(a) == "deploy" and pa.get("appended_nonzero_runs"):
                out.append({"kind": "fallback_translations", "group": g["name"], "arm": a,
                            "runs": pa["appended_nonzero_runs"],
                            "appended_median": pa["appended"]["median"]})
            if pa.get("guest_norm_variants", 0) > 1 and len(g["inputs"]) <= 1:
                out.append({"kind": "nondeterministic_output", "group": g["name"], "arm": a,
                            "variants": pa["guest_norm_variants"]})
        qs = set(g.get("qemu_sha256_valid") or [])
        if len(qs) > 1:
            out.append({"kind": "mixed_qemu", "group": g["name"], "detail": sorted(qs)})
        meta = g.get("meta") or {}
        if meta.get("freeze_ok") is False:
            out.append({"kind": "freeze_violation", "group": g["name"],
                        "detail": meta.get("freeze_check")})
    # locality (model): any candidate layout whose hot footprint grows vs the warm source
    by_prof: dict[str, dict[str, dict]] = defaultdict(dict)
    for h in model["hotset"]:
        if h.get("status") in ("ok", "low_join_coverage"):
            by_prof[h["profile_id"]][h["label"]] = h
    for pid, labs in by_prof.items():
        refs = {k: v for k, v in labs.items() if k.endswith("/warm")}
        for rk, rv in refs.items():
            grp = rk.rsplit("/", 1)[0]
            for k, v in labs.items():
                if k == rk or not k.startswith(grp + "/"):
                    continue
                for f in ("lines64_code", "pages4k_code"):
                    if v["hot"][f] > rv["hot"][f]:
                        out.append({"kind": "locality_model_worse", "group": grp,
                                    "arm": k.rsplit("/", 1)[1], "metric": f,
                                    "value": v["hot"][f], "reference": rv["hot"][f],
                                    "profile_id": pid, "label": "model"})
    return out
