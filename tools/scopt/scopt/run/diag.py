"""Direct QEMU diagnostics from a recorded argv (``scopt diag exec``, §5.11).

Re-runs QEMU directly (no FastDyn frontend) with the argv and environment the
deploy oracle recorded for a run directory, in a separate diagnostics
directory with its own cache copy, RAM files, work files and QMP socket, and
adds ``-d exec[,nochain][,int] -D <dir>/qemu.log``. The source run directory
is never written.

The log is parsed into ``Trace`` rows (``tc.ptr``, cs_base, pc, flags,
cflags) and ``Linking TBs`` rows, and every traced ``tc.ptr`` is checked
against ``code_address + tc_offset`` of the valid records of the deployed
snapshot (T-B1-5). Adding ``-d`` does not change the supercache
``runtime_signature`` (it hashes the r-x mappings, not the arguments);
``nochain`` sets CF_NO_GOTO_TB, so those TBs are translated anew and appended
to the diagnostics copy only.

B3/B4 additions (WP26, design §5.8, §9.4 O2a/O3/O6, plan "Runtime rules"
m-CE7). All of them are diagnostics: never timed, always in a fresh
directory, QEMU under ``setarch x86_64 -R`` and ``timeout``, gdb always
``-nx -batch`` with cwd = FastDyn's own cwd (never a QEMU tree):

* :func:`prepare_diag` is the shared preparation (fresh dir, cache copy, RAM
  banks with the run's inputs, argv rewrite); :func:`diag_exec` keeps its
  behaviour and gains ``exec_trace`` (``False`` with ``interrupts``: the
  ``-d int`` only run that O2a compares) and ``v6_stats``.
* **Stats file collection** (:func:`stats_env`, :func:`read_v6_stats`):
  ``QEMU_SC_V6_STATS=<abs path>`` (opt-in, written at exit by the ``qint-v6``
  loader, schema ``qemu-sc-v6-stats@1``). Absent on a v5-only binary: the
  reader then reports ``present = false`` (unknown, never zero).
* **gdb hit counters** (:func:`gdb_hits`): hardware breakpoints (``hbreak``,
  at most 4 per gdb run; more targets are split into several runs) with an
  ignore count of 1e9 on host addresses of the code arena, set after the
  first ``cpu_exec`` stop (the arena is mapped by then), read back from
  ``info breakpoints``. Hardware breakpoints write no ``int3`` into the
  shared file mapping of the cache. :func:`artifact_probe_addrs` gives the
  addresses of every variant ``C'.tc.ptr``, guard stub ``S`` and fallback
  ``F`` of a v6 file.
* **Diag twin** (:func:`diag_twin`): the plan's m-CE7 TLB counter, ported from
  ``experiments/diag_twin.py`` (WP16): counting breakpoints on every
  ``tlb_flush*`` entry point, the flush workers and ``tcg_commit[_cpu]``,
  enabled at ``benchmark_start``; a twin is clean when they count 0 after
  ``benchmark_start``, every one of them resolved to an address (an
  unresolved one counts unknown calls; listed in ``e_map.unresolved``), the
  breakpoint/watchpoint lists are empty, QEMU exits 0 and the guest output
  equals the source run. ``cov=True`` also dumps
  FastDyn's LOG_REG coverage ring at ``benchmark_end`` (O6, WP16 note 4).
"""

from __future__ import annotations

import collections
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from ..formats.sc_v5 import read_sc, sparse_copy
from ..paths import fastdyn_root, fastdyn_src
from .launcher import FASTDYN_SRC, RunDir, qmp_socket_path, scrub_env, write_json
from .toml_rewrite import memory_banks

__all__ = ["DiagError", "diag_exec", "parse_exec_log", "scan_exec_log", "rewrite_argv",
           "prepare_diag", "stats_env", "read_v6_stats", "gdb_hits", "parse_hits",
           "artifact_probe_addrs", "diag_twin", "parse_twin", "FLUSH_FUNCS",
           "V6_STATS_SCHEMA", "QEMU_TREES"]

V6_STATS_SCHEMA = "qemu-sc-v6-stats@1"
V6_STATS_ENV = "QEMU_SC_V6_STATS"
#: Known QEMU trees: the FastDyn checkout's submodule
#: (``$SCOPT_FASTDYN_SRC/third_party/common/qemu``) and the maintainer workspace
#: layout (``$FASTDYN_ROOT/wt/qemu*``, ``$FASTDYN_ROOT/src/qemu``); any other QEMU
#: source tree is recognised by :func:`_in_qemu_tree`. gdb must not start inside
#: one (its ``.gdbinit``).
QEMU_TREES = (str(fastdyn_src() / "third_party/common/qemu"), str(fastdyn_root() / "wt/qemu"),
              str(fastdyn_root() / "src/qemu"))


def _in_qemu_tree(p: Path) -> bool:
    """Is ``p`` inside a QEMU tree (a listed one, or a directory with QEMU's
    ``qemu-options.hx`` among ``p`` and its parents)?"""
    if any(str(p).startswith(t) for t in QEMU_TREES):
        return True
    return any((d / "qemu-options.hx").is_file() for d in (p, *p.parents))

DIAG_SCHEMA = "scopt.diag_exec/1"

_TRACE_RE = re.compile(r"^Trace (\d+): (0x[0-9a-f]+) \[([0-9a-f]+)/([0-9a-f]+)/([0-9a-f]+)/"
                       r"([0-9a-f]+)\](?: (.*))?$")
_LINK_RE = re.compile(r"^Linking TBs (0x[0-9a-f]+) index (\d+) -> (0x[0-9a-f]+)")


class DiagError(RuntimeError):
    def __init__(self, code: str, detail: str = ""):
        self.code = code
        super().__init__(f"{code}: {detail}" if detail else code)


def parse_exec_log(text: str) -> dict[str, Any]:
    traces = []
    links = []
    for line in text.splitlines():
        m = _TRACE_RE.match(line)
        if m:
            traces.append({"cpu": int(m.group(1)), "tc": int(m.group(2), 16),
                           "cs_base": int(m.group(3), 16), "pc": int(m.group(4), 16),
                           "flags": int(m.group(5), 16), "cflags": int(m.group(6), 16),
                           "sym": m.group(7)})
            continue
        m = _LINK_RE.match(line)
        if m:
            links.append({"src_tc": int(m.group(1), 16), "index": int(m.group(2)),
                          "dst_tc": int(m.group(3), 16)})
    return {"traces": traces, "links": links}


def scan_exec_log(path: str | os.PathLike) -> dict[str, Any]:
    """Stream ``path`` (a ``-d exec`` log) into aggregate counts.

    Unlike :func:`parse_exec_log` it never holds the log or one object per Trace
    line in memory: a chained FreeRTOS window writes a 6 GB log (60M+ lines), and
    the list-based parse grew past 33 GB RSS. Returns ``trace_lines``,
    ``tc_counts`` (tc.ptr -> n), ``entries`` ((pc, flags, cs_base) -> n) and the
    ``links`` (distinct ``Linking TBs`` lines in first-seen order)."""
    tc_counts: collections.Counter = collections.Counter()
    entries: collections.Counter = collections.Counter()
    links: list[dict[str, int]] = []
    seen: set[tuple[int, int, int]] = set()
    n = 0
    with open(path, "r", errors="replace") as f:
        for line in f:
            if line.startswith("Trace "):
                m = _TRACE_RE.match(line.rstrip("\n"))
                if m:
                    n += 1
                    tc_counts[int(m.group(2), 16)] += 1
                    entries[(int(m.group(4), 16), int(m.group(5), 16),
                             int(m.group(3), 16))] += 1
            elif line.startswith("Linking TBs "):
                m = _LINK_RE.match(line)
                if m:
                    k = (int(m.group(1), 16), int(m.group(2)), int(m.group(3), 16))
                    if k not in seen:
                        seen.add(k)
                        links.append({"src_tc": k[0], "index": k[1], "dst_tc": k[2]})
    return {"trace_lines": n, "tc_counts": tc_counts, "entries": entries, "links": links}


def rewrite_argv(argv: list[str], src: RunDir, dst_root: Path, *, d_items: str,
                 log_path: Path | None) -> list[str]:
    """Point every run-directory path of ``argv`` at ``dst_root``; replace -d/-D
    (an empty ``d_items`` drops them)."""
    out: list[str] = []
    src_root = str(src.root)
    src_qmp = str(src.qmp)
    dst_qmp = str(qmp_socket_path(dst_root))
    skip = False
    for i, a in enumerate(argv):
        if skip:
            skip = False
            continue
        if i > 0 and a in ("-d", "-D"):
            skip = True
            continue
        a = a.replace(src_qmp, dst_qmp).replace(src_root + os.sep, str(dst_root) + os.sep)
        out.append(a)
    if not d_items:
        return out
    return out + ["-d", d_items, "-D", str(log_path)]


def prepare_diag(run_dir: str | os.PathLike, out: str | os.PathLike, *, d_items: str = "",
                 sc: str | os.PathLike | None = None,
                 inputs: dict[str, str | os.PathLike] | None = None,
                 v6_stats: bool = False, log_name: str = "qemu.log") -> dict[str, Any]:
    """Prepare the fresh diagnostics directory ``out`` for a direct QEMU re-run of
    ``run_dir`` (see :func:`diag_exec`); returns the argv, environment and paths.

    Refusals (:class:`DiagError`): ``diag_dir_not_fresh``, ``no_recorded_argv``,
    ``no_work_dir``, ``input_bank_missing``, ``input_missing``,
    ``input_too_large``, ``argv_rewrite``; nothing is written before the input
    checks pass."""
    src = RunDir.open(run_dir)
    out = Path(out).resolve()
    if out.exists() and any(out.iterdir()):
        raise DiagError("diag_dir_not_fresh", str(out))
    orec_path = src.root / "oracle.json"
    if not orec_path.is_file():
        raise DiagError("no_recorded_argv", f"{orec_path} missing (deploy the run dir first)")
    orec = json.loads(orec_path.read_text())
    argv = orec.get("argv")
    if not argv:
        raise DiagError("no_recorded_argv", "oracle.json has no argv (seed deploys record none)")
    if not src.work.is_dir():
        raise DiagError("no_work_dir", f"{src.work} missing; the plugin needs its generated files")

    if sc is None:
        dep = src.root / "deploy.json"
        sc = json.loads(dep.read_text())["artifact"]["sc"] if dep.is_file() else src.cache
    if inputs is None:
        rj = src.root / "run.json"
        rec_inputs = json.loads(rj.read_text()).get("inputs") if rj.is_file() else None
        inputs = {k: v["path"] for k, v in (rec_inputs or {}).items()}
    # check every input against its bank before anything is written: an input
    # larger than the bank would spill past the guest RAM region
    file_banks = {b.id: b for b in memory_banks(src.config()) if b.is_file and b.memory_file}
    missing = sorted(set(inputs) - set(file_banks))
    if missing:
        raise DiagError("input_bank_missing", f"no file-backed bank {missing} in {src.toml}")
    for bid, path in inputs.items():
        try:
            n = os.path.getsize(path)
        except OSError as e:
            raise DiagError("input_missing", f"{path}: {e}") from e
        size = file_banks[bid].size
        if size is not None and n > size:
            raise DiagError("input_too_large", f"{path}: {n} bytes > bank {bid!r} size {size}")
    out.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src.work, out / "work", symlinks=True)
    cache_rel = src.cache.relative_to(src.root) if src.cache else None
    cache_path = None
    if cache_rel is not None:
        cache_path = out / cache_rel
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        sparse_copy(sc, cache_path)
        os.chmod(cache_path, 0o600)  # store artifacts are read-only; QEMU opens O_RDWR
    used_inputs: dict[str, Any] = {}
    for bank in memory_banks(src.config()):
        if bank.is_file and bank.memory_file:
            p = Path(str(bank.memory_file).replace(str(src.root) + os.sep, str(out) + os.sep))
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "wb") as f:
                if bank.size:
                    f.truncate(bank.size)
                if bank.id in inputs:
                    data = Path(inputs[bank.id]).read_bytes()
                    f.seek(0)
                    f.write(data)
                    used_inputs[bank.id] = {"path": str(inputs[bank.id]), "size": len(data)}
    missing = sorted(set(inputs) - set(used_inputs))
    if missing:
        raise DiagError("input_bank_missing", f"no file-backed bank {missing} in {src.toml}")
    qmp = qmp_socket_path(out)
    qmp.parent.mkdir(parents=True, exist_ok=True)
    if qmp.exists():
        qmp.unlink()

    log_path = out / log_name if d_items else None
    qargv = rewrite_argv(argv, src, out, d_items=d_items, log_path=log_path)
    # FastDyn passes the plugin relative to its own cwd; make it absolute so the
    # argv does not depend on the cwd
    ocwd = Path(orec.get("cwd") or FASTDYN_SRC)
    for k in range(1, len(qargv)):
        if qargv[k - 1] == "--plugin" and not qargv[k].startswith("/"):
            qargv[k] = str(ocwd / qargv[k])
    src_prefix = str(src.root) + os.sep
    leaks = [a for a in qargv if src_prefix in a or str(src.qmp) in a]
    if leaks:
        raise DiagError("argv_rewrite", f"source run-dir paths left in argv: {leaks}")

    env, env_diff = scrub_env(qemu=None)
    for k in orec.get("env_unset", []):
        env.pop(k, None)
    for k, v in (orec.get("env_set") or {}).items():
        if not k.startswith("QEMU_SC_"):
            env[k] = v.replace(src_prefix, str(out) + os.sep) if isinstance(v, str) else v
    stats_path = None
    if v6_stats:
        stats_path = out / "v6-stats.json"
        env.update(stats_env(stats_path))
    cwd = Path(FASTDYN_SRC)
    if _in_qemu_tree(cwd):  # pragma: no cover - layout guard
        raise DiagError("cwd_in_qemu_tree", str(cwd))
    return {"src": src, "out": out, "argv": qargv, "env": env, "env_diff": env_diff,
            "sc": str(sc), "inputs": used_inputs, "log_path": log_path,
            "stats_path": stats_path, "cache": cache_path, "cwd": cwd,
            "orec": orec}


def diag_exec(run_dir: str | os.PathLike, out: str | os.PathLike, *, nochain: bool = False,
              interrupts: bool = True, sc: str | os.PathLike | None = None,
              timeout_s: float = 120.0,
              inputs: dict[str, str | os.PathLike] | None = None,
              exec_trace: bool = True, v6_stats: bool = False) -> dict[str, Any]:
    """Run QEMU directly from ``run_dir``'s recorded argv in the fresh directory ``out``.

    RAM banks start zeroed, except input banks: ``inputs`` (bank id -> file), or by
    default the inputs ``run_dir/run.json`` recorded, are written into their banks
    (the fixture contract of docs/fixtures.md), so the replay runs the same input.
    ``exec_trace=False`` drops ``-d exec`` (with ``interrupts``: the ``-d int``
    run of O2a); ``v6_stats`` sets ``QEMU_SC_V6_STATS`` (doc key ``v6_stats``)."""
    items = ",".join(x for x in (
        ("exec" + (",nochain" if nochain else "")) if exec_trace else "",
        "int" if interrupts else "") if x)
    prep = prepare_diag(run_dir, out, d_items=items, sc=sc, inputs=inputs, v6_stats=v6_stats)
    src, out, qargv, env, env_diff = (prep["src"], prep["out"], prep["argv"], prep["env"],
                                      prep["env_diff"])
    sc = prep["sc"]
    used_inputs = prep["inputs"]
    log_path = prep["log_path"] or out / "qemu.log"
    cmd = ["setarch", "x86_64", "-R", "timeout", "--signal=TERM", "--kill-after=10",
           f"{timeout_s:g}", *qargv]
    t0 = time.monotonic()
    with open(out / "stdout.log", "wb") as f:
        p = subprocess.run(cmd, cwd=str(prep["cwd"]), env=env, stdout=f,
                           stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                           timeout=timeout_s + 30)
    wall = time.monotonic() - t0

    if log_path.is_file():
        parsed = scan_exec_log(log_path)
    else:
        parsed = {"trace_lines": 0, "tc_counts": collections.Counter(),
                  "entries": collections.Counter(), "links": []}
    snap = read_sc(sc, versions=(7,))
    base = snap.header.code_address
    snap_tc = {base + snap.records[i].tc_offset: i for i in snap.valid_slots}
    variant_tc: dict[int, int] = {}
    from ..formats.sc_v6 import SC7_SEMANTIC_MARKER

    if snap.header.version == 7 and snap.header.reserved & SC7_SEMANTIC_MARKER:
        from ..formats.sc_v6 import read_sc6

        f6 = read_sc6(sc)
        if f6.ext is not None:
            variant_tc = {base + v.tc_offset: v.variant_id for v in f6.ext.variants}
    counts = parsed["tc_counts"]
    outside = sorted(tc for tc in counts if tc not in snap_tc and tc not in variant_tc)
    entries = parsed["entries"]
    doc = {
        "schema": DIAG_SCHEMA,
        "source_run_dir": str(src.root),
        "diag_dir": str(out),
        "argv": cmd,
        "env_diff": env_diff,
        "d_items": items,
        "inputs": used_inputs,
        "exit_code": p.returncode,
        "wall_s": wall,
        "sc": str(sc),
        "code_address": f"0x{base:x}",
        "snapshot_slots": len(snap_tc),
        "trace_lines": parsed["trace_lines"],
        "log_bytes": log_path.stat().st_size if log_path.is_file() else 0,
        "unique_tc": len(counts),
        "links": len(parsed["links"]),
        "tc_in_snapshot": sum(1 for tc in counts if tc in snap_tc),
        "tc_not_in_snapshot": [f"0x{tc:x}" for tc in outside],
        "all_traces_in_snapshot": not outside and parsed["trace_lines"] > 0,
        "per_slot_entries": {str(snap_tc[tc]): n for tc, n in sorted(counts.items())
                             if tc in snap_tc},
        "per_variant_entries": {str(variant_tc[tc]): n for tc, n in sorted(counts.items())
                                if tc in variant_tc},
        "v6_stats": read_v6_stats(prep["stats_path"]) if v6_stats else None,
        "file_version": snap.header.version,
        "entries_by_key": [{"pc": f"0x{k[0]:x}", "flags": f"0x{k[1]:x}",
                            "cs_base": f"0x{k[2]:x}", "count": n}
                           for k, n in sorted(entries.items())],
        "linking": [{"src_tc": f"0x{x['src_tc']:x}", "index": x["index"],
                     "dst_tc": f"0x{x['dst_tc']:x}",
                     "src_slot": snap_tc.get(x["src_tc"]), "dst_slot": snap_tc.get(x["dst_tc"])}
                    for x in parsed["links"]],
    }
    write_json(out / "diag.json", doc)
    return doc


# ---------------------------------------------------------------------------
# v6 run-time statistics (design §5.8; WP22 note 6)
# ---------------------------------------------------------------------------

_STATS_COUNTERS = (
    "activations", "links_armed", "variants_built", "tlb_hook_calls", "disarm_tlb_events",
    "disarm_tlb_links", "flush_resets", "disarm_flush_links", "forbid_requests",
    "disarm_forbid_events", "disarm_forbid_links", "slots_unlinked", "variant_slots_unlinked",
    "variant_exits", "variant_entry_exits", "variant_restores", "invariant_checks",
    "invariant_violations", "tlb_disarm_budget_used", "n_variants", "n_slots", "n_links",
    "file_version", "feature_flags", "ext_load_ns")


def stats_env(path: str | os.PathLike) -> dict[str, str]:
    """``{"QEMU_SC_V6_STATS": <absolute path>}`` (the loader exits on a relative one)."""
    p = Path(path)
    if not p.is_absolute():
        raise DiagError("v6_stats_path", f"{path}: QEMU_SC_V6_STATS must be absolute")
    return {V6_STATS_ENV: str(p)}


def read_v6_stats(path: str | os.PathLike | None) -> dict[str, Any]:
    """Parse a ``qemu-sc-v6-stats@1`` file.

    Returns ``{"present": False, ...}`` when the file does not exist (a
    v5-only binary ignores the variable; counters are then unknown, never 0),
    otherwise the document plus ``present = True`` and ``problems`` (schema
    or type errors; a counter that is not a non-negative integer is a problem,
    not a zero)."""
    if path is None or not Path(path).is_file():
        return {"present": False, "path": None if path is None else str(path),
                "problems": ["stats file not written (v5-only binary, QEMU did not exit, "
                             "or QEMU_SC_V6_STATS unset)"]}
    try:
        doc = json.loads(Path(path).read_text())
    except (OSError, ValueError) as e:
        return {"present": False, "path": str(path), "problems": [f"unreadable: {e}"]}
    probs = []
    if not isinstance(doc, dict) or doc.get("schema") != V6_STATS_SCHEMA:
        probs.append(f"schema is not {V6_STATS_SCHEMA}")
        doc = doc if isinstance(doc, dict) else {}
    for k in _STATS_COUNTERS:
        v = doc.get(k)
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            probs.append(f"{k}: {v!r} is not a non-negative integer")
    ref = doc.get("refused")
    if not isinstance(ref, dict) or any(not isinstance(v, int) for v in ref.values()):
        probs.append("refused: not a {reason: count} object")
    if not isinstance(doc.get("links"), list):
        probs.append("links: not a list")
    return {**doc, "present": True, "path": str(path), "problems": probs}


# ---------------------------------------------------------------------------
# gdb -nx hit counters (O3; design §5.8 "C' execution counts come from diag runs")
# ---------------------------------------------------------------------------

_BP_ROW = re.compile(r"^(\d+)\s+(?:hw )?breakpoint\s+keep\s+[yn]\s+(0x[0-9a-f]+)")
_HIT = re.compile(r"^\s+breakpoint already hit (\d+) time")


def parse_hits(text: str) -> dict[int, int]:
    """``info breakpoints`` output -> ``{address: hits}``.

    One block per breakpoint (its row and the indented lines under it); a
    block without ``breakpoint already hit N time(s)`` counts 0; any other
    line closes the block; a later table overrides an earlier one (the same
    reading as ``fixtures/b3/diag/o3_hits.py`` after the WP24 rework)."""
    counts: dict[int, int] = {}
    cur = None
    for line in text.splitlines():
        m = _BP_ROW.match(line)
        if m:
            cur = int(m.group(2), 16)
            counts[cur] = 0
            continue
        if cur is not None and line[:1] in (" ", "\t") and line.strip():
            h = _HIT.match(line)
            if h:
                counts[cur] = int(h.group(1))
            continue
        cur = None
    return counts


def artifact_probe_addrs(sc: str | os.PathLike) -> list[dict[str, Any]]:
    """Host addresses of a v6 file's variants and guard stubs (absolute, in the
    arena at the header's ``code_address``): per variant ``C'.tc.ptr``; per slot
    the stub start ``S`` and its fallback ``F`` (decoded with the guard template;
    when the stub does not decode, ``F`` is the last 11 bytes of the guard
    range, the original lookup tail, and the row says so)."""
    from ..formats.sc_v6 import read_sc6
    from ..guard import GuardError, decode_guard

    f = read_sc6(sc)
    if f.ext is None:
        return []
    base = f.sc.header.code_address
    by_tb = {f.sc.records[i].tb_offset: i for i in f.sc.valid_slots}
    out: list[dict[str, Any]] = []
    for v in f.ext.variants:
        out.append({"what": f"variant {v.variant_id} C'", "kind": "variant",
                    "variant": v.variant_id, "general_tb_offset": hex(v.general_tb_offset),
                    "addr": base + v.tc_offset})
    for j, s in enumerate(f.ext.slots):
        i = by_tb.get(s.src_tb_offset)
        if i is None:
            continue
        rec = f.sc.records[i]
        code = f.sc.bodies[i] or b""
        how = "decoded"
        try:
            g = decode_guard(code, s.guard_off, tc_abs=base + rec.tc_offset,
                             struct_off=rec.tb_offset - rec.tc_offset)
            f_off = g.f_off
        except (GuardError, IndexError, ValueError):
            f_off = s.guard_off + s.guard_len - 11
            how = "guard_len-11 (stub did not decode)"
        kind = {1: "GENERAL", 2: "SPEC"}.get(s.kind, str(s.kind))
        tc = base + rec.tc_offset
        out.append({"what": f"slot {j} S ({kind}, R tb {s.src_tb_offset:#x}, n={s.slot_n})",
                    "kind": "stub", "slot": j, "addr": tc + s.guard_off})
        out.append({"what": f"slot {j} F ({kind}, R tb {s.src_tb_offset:#x}, n={s.slot_n})",
                    "kind": "fallback", "slot": j, "addr": tc + f_off, "how": how})
    return out


def _gdb_cmd(timeout_s: float, cmds: list[str], argv: list[str],
             script: Path | None = None) -> list[str]:
    """gdb ``-nx -batch`` under ``setarch -R`` and ``timeout``. With ``script`` the
    commands are written there and passed with ``-x`` (needed for multi-line
    ``commands ... end`` blocks, which ``-ex`` cannot carry)."""
    g = ["setarch", "x86_64", "-R", "timeout", "--signal=KILL", f"{timeout_s:g}",
         "gdb", "-nx", "-batch"]
    if script is not None:
        script.write_text("\n".join(cmds) + "\n")
        g += ["-x", str(script)]
    else:
        for c in cmds:
            g += ["-ex", c]
    return g + ["--args", *argv]


def is_direct_run(run_dir: str | os.PathLike) -> bool:
    """A direct (no FastDyn) run directory of :func:`scopt.run.deploy.deploy_direct`:
    ``direct.json`` and no WP8 ``rundir.json``."""
    d = Path(run_dir)
    return (d / "direct.json").is_file() and not (d / "rundir.json").is_file()


def prepare_direct_diag(run_dir: str | os.PathLike, out: str | os.PathLike, *,
                        sc: str | os.PathLike | None = None,
                        v6_stats: bool = False) -> dict[str, Any]:
    """:func:`prepare_diag` for a direct run directory (``direct.json``; F-B3-15's mps2
    alias path): a fresh sparse copy of the deployed ``.sc`` (or ``sc``) in ``out/cache``
    and the recorded argv with the cache path rewritten to it. The environment drops every
    ``QEMU_SC_*`` / ``FASTDYN_*`` variable (``QEMU_SC_V6_STATS`` with ``v6_stats``)."""
    src = Path(run_dir).resolve()
    out = Path(out).resolve()
    if out.exists() and any(out.iterdir()):
        raise DiagError("diag_dir_not_fresh", str(out))
    dj = json.loads((src / "direct.json").read_text())
    argv, old = list(dj.get("argv") or ()), dj.get("cache")
    if not argv or not old:
        raise DiagError("no_recorded_argv", f"{src / 'direct.json'} has no argv/cache")
    out.mkdir(parents=True, exist_ok=True)
    new = out / "cache" / Path(old).name
    new.parent.mkdir(parents=True, exist_ok=True)
    sparse_copy(sc or old, new)
    os.chmod(new, 0o600)
    qargv = [a.replace(old, str(new)) for a in argv]
    if any(old in a for a in qargv) or not any(str(new) in a for a in qargv):
        raise DiagError("argv_rewrite", f"cache path {old} not rewritten in {argv}")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("QEMU_SC_", "FASTDYN_"))}
    stats_path = None
    if v6_stats:
        stats_path = out / "v6-stats.json"
        env.update(stats_env(stats_path))
    return {"src": src, "out": out, "argv": qargv, "env": env, "env_diff": {}, "sc": str(new),
            "inputs": {}, "log_path": None, "cwd": out, "stats_path": stats_path}


def gdb_hits(run_dir: str | os.PathLike, out: str | os.PathLike,
             targets: list[tuple[str, int]], *, sc: str | os.PathLike | None = None,
             inputs: dict[str, str | os.PathLike] | None = None, timeout_s: float = 600.0,
             v6_stats: bool = False, per_run: int = 4) -> dict[str, Any]:
    """Count executions of host ``targets`` (``(label, absolute address)``) in
    diagnostic re-runs of ``run_dir`` (never timed). At most ``per_run`` (4,
    the x86 debug registers) hardware breakpoints per gdb run; more targets
    are split over several fresh sub-directories ``out/g<k>``. A target
    missing from ``info breakpoints`` has ``hits = None`` (never 0). ``run_dir`` may also
    be a direct run directory (``direct.json``, :func:`prepare_direct_diag`)."""
    out = Path(out).resolve()
    if out.exists() and any(out.iterdir()):
        raise DiagError("diag_dir_not_fresh", str(out))
    if not targets:
        raise DiagError("no_targets", "gdb_hits needs at least one target")
    if inputs and is_direct_run(run_dir):
        raise DiagError("input_bank_missing", "a direct run directory has no input banks")
    groups = [targets[i:i + per_run] for i in range(0, len(targets), per_run)]
    rows: list[dict[str, Any]] = []
    runs = []
    for k, grp in enumerate(groups):
        sub = out / f"g{k}" if len(groups) > 1 else out
        prep = (prepare_direct_diag(run_dir, sub, sc=sc, v6_stats=v6_stats)
                if is_direct_run(run_dir) else
                prepare_diag(run_dir, sub, sc=sc, inputs=inputs, v6_stats=v6_stats))
        cmds = ["set pagination off", "set confirm off", "set startup-with-shell off",
                "set width 0",
                "handle SIGUSR1 SIGUSR2 SIGPIPE SIGALRM SIGCHLD SIGIO SIGPROF SIGWINCH "
                "nostop noprint pass",
                "break cpu_exec", "run", "delete"]
        for _, addr in grp:
            cmds += [f"hbreak *0x{addr:x}", "ignore $bpnum 1000000000"]
        cmds += ["continue", "info breakpoints"]
        gcmd = _gdb_cmd(timeout_s, cmds, prep["argv"])
        t0 = time.monotonic()
        p = subprocess.run(gcmd, cwd=str(prep["cwd"]), env=prep["env"], capture_output=True,
                           text=True, stdin=subprocess.DEVNULL, timeout=timeout_s + 60)
        wall = time.monotonic() - t0
        (sub / "gdb.log").write_text(p.stdout + "\n--- stderr ---\n" + p.stderr)
        counts = parse_hits(p.stdout)
        m = re.search(r"exited (?:normally|with code (\d+))", p.stdout)
        qemu_exit = 0 if m and m.group(1) is None else (int(m.group(1), 8) if m else None)
        for label, addr in grp:
            rows.append({"what": label, "addr": f"0x{addr:x}", "hits": counts.get(addr),
                         "run": k})
        runs.append({"dir": str(sub), "cmd": gcmd, "gdb_exit": p.returncode,
                     "qemu_exit": qemu_exit, "wall_s": wall,
                     "v6_stats": read_v6_stats(prep["stats_path"]) if v6_stats else None})
        _drop_big(sub)
    missing = [r["what"] for r in rows if r["hits"] is None]
    doc = {"schema": "scopt.diag_hits/1", "source_run_dir": str(Path(run_dir).resolve()),
           "diag_dir": str(out), "targets": rows, "runs": runs, "missing": missing,
           "ok": not missing and all(r["qemu_exit"] == 0 for r in runs)}
    write_json(out / "hits.json", doc)
    return doc


def _drop_big(d: Path) -> None:
    """Delete the multi-GB parts of a finished diag directory (the cache copy,
    RAM files and the work copy); keep logs and JSON."""
    shutil.rmtree(d / "work", ignore_errors=True)
    for pat in ("*.sc", "*.ram"):
        for f in d.rglob(pat):
            try:
                f.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# diag twin: the m-CE7 TLB counter (ported from experiments/diag_twin.py, WP16)
# ---------------------------------------------------------------------------

FLUSH_FUNCS = [
    "tlb_flush", "tlb_flush_all_cpus_synced", "tlb_flush_by_mmuidx",
    "tlb_flush_by_mmuidx_all_cpus_synced", "tlb_flush_page", "tlb_flush_page_all_cpus_synced",
    "tlb_flush_page_bits_by_mmuidx", "tlb_flush_page_bits_by_mmuidx_all_cpus_synced",
    "tlb_flush_page_by_mmuidx", "tlb_flush_page_by_mmuidx_all_cpus_synced",
    "tlb_flush_range_by_mmuidx", "tlb_flush_range_by_mmuidx_all_cpus_synced",
    "tlb_flush_by_mmuidx_async_work", "tlb_flush_page_by_mmuidx_async_0",
    "tlb_flush_page_by_mmuidx_async_1", "tlb_flush_page_by_mmuidx_async_2",
    "tlb_flush_range_by_mmuidx_async_0", "tlb_flush_range_by_mmuidx_async_1",
    "tcg_commit_cpu", "tcg_commit",
]


def twin_script(*, count_from_start: bool = False, cov_dump: Path | None = None) -> list[str]:
    """gdb commands of the twin (breakpoint 1 ``benchmark_start``, 2
    ``benchmark_end``, 3.. the flush counters, disabled until 1 unless
    ``count_from_start``, the non-vacuity control)."""
    n = len(FLUSH_FUNCS)
    lines = ["set pagination off", "set confirm off", "set breakpoint pending on",
             "set print pretty off", "set width 0",
             "handle SIGUSR1 SIGUSR2 SIGPIPE SIGALRM SIGCHLD SIGIO SIGPROF SIGWINCH "
             "nostop noprint pass",
             "break benchmark_start", "break benchmark_end"]
    lines += [f"break {f}" for f in FLUSH_FUNCS]
    if not count_from_start:
        lines.append(f"disable 3-{2 + n}")
    lines += [f"ignore {k} 2000000000" for k in range(3, 3 + n)]
    head = ("cpus_queue.tqh_first->breakpoints.tqh_first, "
            "cpus_queue.tqh_first->watchpoints.tqh_first, "
            "cpus_queue.tqh_first->singlestep_enabled")
    lines += ["commands 1", "silent",
              f'printf "SCOPT benchmark_start bp_head=%p wp_head=%p singlestep=%d\\n", {head}',
              f"enable 3-{2 + n}", "continue", "end",
              "commands 2", "silent",
              f'printf "SCOPT benchmark_end bp_head=%p wp_head=%p singlestep=%d\\n", {head}',
              'printf "SCOPT info-breakpoints-begin\\n"', "info breakpoints",
              'printf "SCOPT info-breakpoints-end\\n"']
    if cov_dump is not None:
        # the ring buffer is allocated only with coverage on; a NULL buffer must not
        # abort the command list (the run would then never continue to its exit)
        lines += ['printf "SCOPT cov index=%u consumed=%u buffer=%p\\n", '
                  "core_cc_list.log_buf.index, tracer_consumed_index, "
                  "core_cc_list.log_buf.buffer",
                  "if core_cc_list.log_buf.buffer != 0",
                  f"dump binary memory {cov_dump} core_cc_list.log_buf.buffer "
                  "((char *)core_cc_list.log_buf.buffer)+65536",
                  "end"]
    lines += ["continue", "end", "run"]
    return lines


def parse_twin(text: str) -> dict[str, Any]:
    """Parse the twin's gdb output: list heads at start/end, per-function counts."""
    out: dict[str, Any] = {"start": None, "end": None, "hits": {}, "locations": {}}
    for k in ("start", "end"):
        m = re.search(rf"SCOPT benchmark_{k} bp_head=(\S+) wp_head=(\S+) singlestep=(\d+)", text)
        if m:
            out[k] = {"bp_head": m.group(1), "wp_head": m.group(2),
                      "singlestep": int(m.group(3))}
    lines = text.splitlines()
    try:
        a = lines.index("SCOPT info-breakpoints-begin")
        b = lines.index("SCOPT info-breakpoints-end", a)
        blk = lines[a + 1:b]
    except ValueError:
        blk = []
    cur = None
    for line in blk:
        m = re.match(r"^(\d+)\s+breakpoint\s+keep\s+(y|n)\s+(\S+)?\s*(.*)$", line)
        if m:
            cur = int(m.group(1))
            out["hits"].setdefault(cur, 0)
            out["locations"][cur] = line.strip()
            continue
        m = re.search(r"breakpoint already hit (\d+) time", line)
        if m and cur is not None:
            out["hits"][cur] = int(m.group(1))
    out["counters"] = {f: out["hits"].get(k, 0) for k, f in enumerate(FLUSH_FUNCS, start=3)}
    out["counters_total"] = sum(out["counters"].values())
    # resolved = the breakpoint has an address (a ``<PENDING>`` row or no row
    # at all is unresolved: calls there are unknown, never 0)
    out["unresolved"] = [f for k, f in enumerate(FLUSH_FUNCS, start=3)
                         if not re.search(r"\bkeep\s+[yn]\s+(0x[0-9a-fA-F]+|<MULTIPLE>)",
                                          out["locations"].get(k, ""))]
    out["counters_resolved"] = len(FLUSH_FUNCS) - len(out["unresolved"])
    m = re.search(r"SCOPT cov index=(\d+) consumed=(\d+)(?: buffer=(\S+))?", text)
    out["cov"] = None if not m else {"index": int(m.group(1)), "consumed": int(m.group(2)),
                                     "buffer": m.group(3)}
    return out


def _guest_regs(text: str) -> list[str]:
    return [m.group(1) for m in (re.search(r"(Reg \d+:0x[0-9a-f]+)", x)
                                 for x in text.splitlines()) if m]


def diag_twin(run_dir: str | os.PathLike, out: str | os.PathLike, *,
              sc: str | os.PathLike | None = None,
              inputs: dict[str, str | os.PathLike] | None = None, timeout_s: float = 300.0,
              count_from_start: bool = False, cov: bool = False, d_int: bool = False,
              v6_stats: bool = False, allowed_calls: int = 0) -> dict[str, Any]:
    """The untimed diag twin of a run (plan "Runtime rules", m-CE7; see the module
    docstring). Writes ``out/twin.json``; ``clean`` decides. ``allowed_calls``
    is a recorded diagnostic allowance, **not** an m-CE7 relaxation: the B3
    fixture's phase teardown writes ``MPU_CTRL = 0`` after the measured loop,
    one flush that counts 3 calls (``tlb_flush`` -> ``tlb_flush_by_mmuidx`` ->
    ``tlb_flush_by_mmuidx_async_work``). ``clean`` is always the plan's rule
    (m-CE7: 0 calls after ``benchmark_start``, every counter breakpoint
    resolved); ``within_allowance`` is the same test with ``allowed_calls``
    and is never gate evidence (no design note approves the relaxation;
    WP26 review 2)."""
    prep = prepare_diag(run_dir, out, d_items="int" if d_int else "", sc=sc, inputs=inputs,
                        v6_stats=v6_stats, log_name="qemu-int.log")
    out = prep["out"]
    cov_dump = out / "covbuf.bin" if cov else None
    gcmd = _gdb_cmd(timeout_s, twin_script(count_from_start=count_from_start,
                                           cov_dump=cov_dump), prep["argv"],
                    script=out / "twin.gdb")
    t0 = time.monotonic()
    p = subprocess.run(gcmd, cwd=str(prep["cwd"]), env=prep["env"], capture_output=True,
                       text=True, stdin=subprocess.DEVNULL, timeout=timeout_s + 60)
    wall = time.monotonic() - t0
    (out / "gdb.log").write_text(p.stdout + "\n--- stderr ---\n" + p.stderr)
    g = parse_twin(p.stdout)
    rj = prep["src"].root / "run.json"
    src_norm = [x for x in (json.loads(rj.read_text()).get("guest_norm") or [])
                if x.startswith("Reg ")] if rj.is_file() else []
    twin_norm = _guest_regs(p.stdout + "\n" + p.stderr)
    m = re.search(r"exited (?:normally|with code (\d+))", p.stdout)
    qemu_exit = 0 if m and m.group(1) is None else (int(m.group(1), 8) if m else None)
    nul = ("0x0", "(nil)")
    lists_null = all(x and x["bp_head"] in nul and x["wp_head"] in nul
                     for x in (g["start"], g["end"]))
    doc: dict[str, Any] = {
        "schema": "scopt.diag_twin/1", "source_run_dir": str(prep["src"].root),
        "diag_dir": str(out), "cmd": gcmd, "artifact_sc": prep["sc"],
        "count_from_start": count_from_start, "allowed_calls": allowed_calls,
        "gdb_exit": p.returncode, "qemu_exit": qemu_exit,
        "wall_s": wall, "benchmark_start_seen": g["start"] is not None,
        "benchmark_end_seen": g["end"] is not None,
        "e_bp": {"start": g["start"], "end": g["end"], "lists_empty": lists_null},
        "e_map": {"counters": g["counters"], "total_after_benchmark_start": g["counters_total"],
                  "breakpoints_resolved": g["counters_resolved"], "of": len(FLUSH_FUNCS),
                  "unresolved": g["unresolved"]},
        "guest_norm_equal_source": (twin_norm == src_norm) if src_norm else None,
        "twin_guest_regs_sha256": "sha256:" + hashlib.sha256(
            "\n".join(twin_norm).encode()).hexdigest(),
        "v6_stats": read_v6_stats(prep["stats_path"]) if v6_stats else None,
        "int_log": str(prep["log_path"]) if d_int else None,
    }
    if cov:
        cb = out / "covbuf.bin"
        doc["coverage_ring"] = {**(g["cov"] or {"index": None, "consumed": None}),
                                "buffer_sha256": ("sha256:" + hashlib.sha256(
                                    cb.read_bytes()).hexdigest()) if cb.is_file() else None}
    rest = bool(doc["benchmark_start_seen"] and doc["benchmark_end_seen"] and lists_null
                and qemu_exit == 0 and doc["guest_norm_equal_source"] is True
                and not g["unresolved"])
    doc["clean"] = rest and g["counters_total"] == 0
    doc["within_allowance"] = rest and g["counters_total"] <= allowed_calls
    write_json(out / "twin.json", doc)
    _drop_big(out)
    return doc
