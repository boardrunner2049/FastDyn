"""WP16 diag twin (plan "Runtime rules", m-CE7) and E-MAP / E-BP counters.

Re-runs QEMU directly from the argv the deploy oracle recorded for a finished
run directory (like ``scopt diag exec``, never writing the source run dir),
under ``gdb -nx -batch`` with cwd **outside** every QEMU tree:

* breakpoint 1 ``benchmark_start`` (FastDyn virtual, pending until the plugin
  loads): enables the counters and records the breakpoint/watchpoint list heads
  and ``singlestep_enabled`` of ``first_cpu`` (E-BP);
* breakpoint 2 ``benchmark_end``: records the same again and prints
  ``info breakpoints`` (hit counts);
* breakpoints 3.. : every ``tlb_flush*`` entry point and the three flush workers
  (cputlb.c), and ``tcg_commit_cpu`` (physmem.c), disabled until
  ``benchmark_start`` and set to ignore a huge count, so they never stop the run
  and their "already hit" counts are the number of calls after ``benchmark_start``
  (E-MAP).

A twin is **clean** when all counters are 0, the guest output equals the source
run's, both list heads are NULL and the exit status is 0. The twin is never timed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

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
#: the workspace ($FASTDYN_ROOT, else the directory that holds tools/) and its QEMU trees
ROOT = Path(os.environ.get("FASTDYN_ROOT") or Path(__file__).resolve().parents[3])
QEMU_TREES = (str(ROOT / "wt") + "/", str(ROOT / "src/qemu"))
SCHEMA = "wp16.diag_twin/1"


def gdb_script(path: Path, *, count_from_start: bool = False, cov_dump: Path | None = None) -> None:
    """``count_from_start``: non-vacuity control, the counters are enabled from the first
    instruction (boot-time TLB flushes and memory commits must then be counted)."""
    n = len(FLUSH_FUNCS)
    lines = [
        "set pagination off", "set confirm off", "set breakpoint pending on", "set print pretty off",
        "set width 0",
        "handle SIGUSR1 SIGUSR2 SIGPIPE SIGALRM SIGCHLD SIGIO SIGPROF SIGWINCH nostop noprint pass",
        "break benchmark_start",
        "break benchmark_end",
    ]
    lines += [f"break {f}" for f in FLUSH_FUNCS]
    if not count_from_start:
        lines += [f"disable 3-{2 + n}"]
    for k in range(3, 3 + n):
        lines.append(f"ignore {k} 2000000000")
    lines += [
        "commands 1", "silent",
        'printf "WP16 benchmark_start bp_head=%p wp_head=%p singlestep=%d\\n", '
        "cpus_queue.tqh_first->breakpoints.tqh_first, cpus_queue.tqh_first->watchpoints.tqh_first, "
        "cpus_queue.tqh_first->singlestep_enabled",
        f"enable 3-{2 + n}", "continue", "end",
        "commands 2", "silent",
        'printf "WP16 benchmark_end bp_head=%p wp_head=%p singlestep=%d\\n", '
        "cpus_queue.tqh_first->breakpoints.tqh_first, cpus_queue.tqh_first->watchpoints.tqh_first, "
        "cpus_queue.tqh_first->singlestep_enabled",
        'printf "WP16 info-breakpoints-begin\\n"', "info breakpoints", 'printf "WP16 info-breakpoints-end\\n"',
        *([  # X-REHY: FastDyn coverage ring (LOG_REG of every executed TB's first insn)
            'printf "WP16 cov index=%u consumed=%u\\n", core_cc_list.log_buf.index, tracer_consumed_index',
            f"dump binary memory {cov_dump} core_cc_list.log_buf.buffer "
            "((char *)core_cc_list.log_buf.buffer)+65536"] if cov_dump else []),
        "continue", "end",
        "run",
    ]
    path.write_text("\n".join(x for x in lines if x) + "\n")


def parse_gdb(text: str) -> dict[str, Any]:
    out: dict[str, Any] = {"start": None, "end": None, "hits": {}, "locations": {}}
    m = re.search(r"WP16 benchmark_start bp_head=(\S+) wp_head=(\S+) singlestep=(\d+)", text)
    if m:
        out["start"] = {"bp_head": m.group(1), "wp_head": m.group(2), "singlestep": int(m.group(3))}
    m = re.search(r"WP16 benchmark_end bp_head=(\S+) wp_head=(\S+) singlestep=(\d+)", text)
    if m:
        out["end"] = {"bp_head": m.group(1), "wp_head": m.group(2), "singlestep": int(m.group(3))}
    lines_ = text.splitlines()
    try:
        a = lines_.index("WP16 info-breakpoints-begin")
        b = lines_.index("WP16 info-breakpoints-end", a)
        blk = "\n".join(lines_[a + 1:b])
    except ValueError:
        blk = ""
    cur = None
    for line in blk.splitlines():
        m = re.match(r"^(\d+)\s+breakpoint\s+keep\s+(y|n)\s+(\S+)?\s*(.*)$", line)
        if m:
            cur = int(m.group(1))
            out["hits"].setdefault(cur, 0)
            out["locations"][cur] = line.strip()
            continue
        m = re.search(r"breakpoint already hit (\d+) time", line)
        if m and cur is not None:
            out["hits"][cur] = int(m.group(1))
    named = {}
    for k, f in enumerate(FLUSH_FUNCS, start=3):
        named[f] = out["hits"].get(k, 0)
    out["counters"] = named
    out["counters_total"] = sum(named.values())
    out["counters_resolved"] = sum(1 for k in range(3, 3 + len(FLUSH_FUNCS)) if k in out["locations"])
    return out


def norm_guest(stdout: str) -> list[str]:
    keep = []
    for line in stdout.splitlines():
        m = re.search(r"(Reg \d+:0x[0-9a-f]+)", line)
        if m:
            keep.append(m.group(1))
    return keep


def twin(run_dir: Path, out: Path, *, timeout_s: float = 300.0, d_int: bool = False,
         count_from_start: bool = False, cov: bool = False) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    out = out.resolve()
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"diag dir not fresh: {out}")
    out.mkdir(parents=True, exist_ok=True)
    orec = json.loads((run_dir / "oracle.json").read_text())
    rj = json.loads((run_dir / "run.json").read_text())
    dep = json.loads((run_dir / "deploy.json").read_text()) if (run_dir / "deploy.json").is_file() else None
    argv = list(orec["argv"])
    src, dst = str(run_dir) + os.sep, str(out) + os.sep
    shutil.copytree(run_dir / "work", out / "work", symlinks=True)
    # cache: the deployed artifact (never the run's possibly-appended copy)
    sc_src = dep["artifact"]["sc"] if dep else None
    new_argv = [a.replace(src, dst) for a in argv]
    # FastDyn may pass the plugin relative to its own cwd (src/fastdyn); the twin runs
    # elsewhere (never inside a QEMU tree), so make it absolute
    ocwd = orec.get("cwd") or os.environ.get("SCOPT_FASTDYN_SRC") or str(ROOT / "src/fastdyn")
    for k in range(1, len(new_argv)):
        if new_argv[k - 1] == "--plugin" and not new_argv[k].startswith("/"):
            new_argv[k] = os.path.join(ocwd, new_argv[k])
    m = [a for a in new_argv if "supercache=" in a]
    if m:
        cache = Path(m[0].split("supercache=")[1].split(",")[0])
        cache.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["cp", "--sparse=always", sc_src, str(cache)], check=True)
        os.chmod(cache, 0o600)  # store snapshots are read-only; QEMU opens the cache O_RDWR
        # the FastDyn manifest is irrelevant for a direct QEMU run
    for a in new_argv:
        mm = None if "memory-backend-file" not in a else re.search(r"mem-path=([^,]+)", a)
        if mm:
            p = Path(mm.group(1))
            size = re.search(r"size=(\d+)([KMG]?)", a)
            n = int(size.group(1)) * {"": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30}[size.group(2)]
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "wb") as f:
                f.truncate(n)
                for bid, inp in (rj.get("inputs") or {}).items():
                    if Path(inp["bank_file"]).name == p.name:
                        f.seek(0)
                        f.write(Path(inp["path"]).read_bytes())
    if d_int:
        new_argv += ["-d", "int", "-D", str(out / "qemu-int.log")]
    leaks = [a for a in new_argv if src in a]
    if leaks:
        raise SystemExit(f"argv still points at the source run dir: {leaks}")
    script = out / "count.gdb"
    gdb_script(script, count_from_start=count_from_start, cov_dump=(out / "covbuf.bin") if cov else None)
    env = dict(os.environ)
    for k in list(env):
        if k.startswith("QEMU_SC_"):
            env.pop(k)
    env.update({k: v.replace(src, dst) for k, v in (orec.get("env_set") or {}).items()})
    cwd = Path(ocwd)  # FastDyn's own cwd (src/fastdyn): relative -kernel/--plugin paths resolve there
    assert not any(str(cwd).startswith(t) for t in QEMU_TREES)
    cmd = ["setarch", "x86_64", "-R", "timeout", "--signal=KILL", f"{timeout_s:g}",
           "gdb", "-nx", "-batch", "-x", str(script), "--args", *new_argv]
    t0 = time.monotonic()
    p = subprocess.run(cmd, cwd=str(cwd), env=env, capture_output=True, text=True,
                       stdin=subprocess.DEVNULL)
    wall = time.monotonic() - t0
    (out / "gdb.log").write_text(p.stdout + "\n--- stderr ---\n" + p.stderr)
    g = parse_gdb(p.stdout)
    src_norm = rj.get("guest_norm") or []
    src_norm = [x for x in src_norm if x.startswith("Reg ")]
    twin_norm = norm_guest(p.stdout + "\n" + p.stderr)
    m = re.search(r"exited (?:normally|with code (\d+))", p.stdout)
    qemu_exit = 0 if m and m.group(1) is None else (int(m.group(1)) if m else None)
    lists_null = all(x and x["bp_head"] in ("0x0", "(nil)") and x["wp_head"] in ("0x0", "(nil)")
                     for x in (g["start"], g["end"]))
    doc = {
        "schema": SCHEMA, "count_from_start": count_from_start, "source_run_dir": str(run_dir), "diag_dir": str(out), "cmd": cmd,
        "artifact_sc": sc_src, "gdb_exit": p.returncode, "qemu_exit": qemu_exit, "wall_s": wall,
        "benchmark_start_seen": g["start"] is not None, "benchmark_end_seen": g["end"] is not None,
        "e_bp": {"start": g["start"], "end": g["end"], "lists_empty": lists_null},
        "e_map": {"counters": g["counters"], "total_after_benchmark_start": g["counters_total"],
                  "breakpoints_resolved": g["counters_resolved"], "of": len(FLUSH_FUNCS)},
        "guest_norm_equal_source": twin_norm == src_norm if src_norm else None,
        "twin_guest_norm_sha256": "sha256:" + hashlib.sha256("\n".join(twin_norm).encode()).hexdigest(),
        "source_guest_norm_sha256": rj.get("guest_norm_sha256"),
    }
    if cov:
        m = re.search(r"WP16 cov index=(\d+) consumed=(\d+)", p.stdout)
        cb = out / "covbuf.bin"
        doc["coverage_ring"] = {"index": int(m.group(1)) if m else None,
                                "consumed": int(m.group(2)) if m else None,
                                "buffer_sha256": ("sha256:" + hashlib.sha256(cb.read_bytes()).hexdigest())
                                if cb.is_file() else None}
    doc["clean"] = bool(doc["benchmark_start_seen"] and doc["benchmark_end_seen"]
                        and g["counters_total"] == 0 and lists_null and qemu_exit == 0
                        and doc["guest_norm_equal_source"] is True
                        and g["counters_resolved"] >= len(FLUSH_FUNCS) - 2)
    (out / "twin.json").write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    # large artifacts of the twin are not needed after parsing
    for big in (out / "work",):
        shutil.rmtree(big, ignore_errors=True)
    for f in out.rglob("*.sc"):
        f.unlink()
    for f in out.rglob("*.ram"):
        f.unlink()
    return doc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--d-int", action="store_true")
    ap.add_argument("--count-from-start", action="store_true")
    ap.add_argument("--cov", action="store_true", help="dump FastDyn's coverage ring at benchmark_end")
    a = ap.parse_args(argv)
    doc = twin(a.run_dir, a.out, timeout_s=a.timeout, d_int=a.d_int,
               count_from_start=a.count_from_start, cov=a.cov)
    print(json.dumps({k: doc[k] for k in ("clean", "e_map", "e_bp", "guest_norm_equal_source",
                                          "qemu_exit", "wall_s")}, indent=1))
    return 0 if doc["clean"] else 1


if __name__ == "__main__":
    sys.exit(main())
