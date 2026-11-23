"""``scopt diag {exec,probes,hits,twin,stats}``: direct QEMU diagnostics from a run
directory's recorded argv (WP8; hits/twin/stats/probes WP26, never timed)."""

from __future__ import annotations

import json
import sys

import click


def register(group: click.Group) -> None:
    group.add_command(diag)


@click.group()
def diag() -> None:
    """Direct QEMU diagnostics."""


@diag.command("exec")
@click.option("--run-dir", required=True, type=click.Path(exists=True, file_okay=False),
              help="A deployed run directory (its oracle.json holds the argv).")
@click.option("--out", required=True, type=click.Path(file_okay=False),
              help="Fresh diagnostics directory (own cache copy, RAM, work, QMP).")
@click.option("--nochain", is_flag=True, help="Add nochain (changes cflags; new TBs are "
              "appended to the diagnostics copy only).")
@click.option("--no-int", "no_int", is_flag=True, help="Do not add -d int.")
@click.option("--no-exec", "no_exec", is_flag=True, help="Do not add -d exec (with -d int: O2a).")
@click.option("--v6-stats", is_flag=True, help="Collect QEMU_SC_V6_STATS.")
@click.option("--sc", type=click.Path(exists=True, dir_okay=False),
              help="Cache to copy (default: the artifact deployed into the run dir).")
@click.option("--timeout", "timeout_s", type=float, default=120.0, show_default=True)
@click.option("--json", "as_json", is_flag=True)
def diag_exec_cmd(run_dir, out, nochain, no_int, no_exec, v6_stats, sc, timeout_s, as_json):
    """Run QEMU with -d exec[,nochain][,int] and check Trace tc.ptr against the snapshot."""
    from ..run.diag import DiagError, diag_exec

    try:
        doc = diag_exec(run_dir, out, nochain=nochain, interrupts=not no_int, sc=sc,
                        timeout_s=timeout_s, exec_trace=not no_exec, v6_stats=v6_stats)
    except DiagError as e:
        click.echo(f"diag refused: {e}", err=True)
        sys.exit(2)
    if as_json:
        click.echo(json.dumps(doc, indent=2, sort_keys=True))
    else:
        for k in ("exit_code", "d_items", "trace_lines", "unique_tc", "links", "tc_in_snapshot",
                  "tc_not_in_snapshot", "all_traces_in_snapshot"):
            click.echo(f"{k}: {doc[k]}")


def _emit(doc, as_json, keys):
    if as_json:
        click.echo(json.dumps(doc, indent=2, sort_keys=True, default=str))
    else:
        for k in keys:
            click.echo(f"{k}: {doc.get(k)}")


@diag.command("probes")
@click.argument("sc", type=click.Path(exists=True))
@click.option("--json", "as_json", is_flag=True)
def diag_probes_cmd(sc, as_json):
    """Host addresses of a v6 file's variants (C'), guard stubs (S) and fallbacks (F)."""
    from pathlib import Path

    from ..run.diag import artifact_probe_addrs

    p = Path(sc)
    rows = artifact_probe_addrs(p / "cache.sc" if p.is_dir() else p)
    if as_json:
        click.echo(json.dumps([{**r, "addr": f"0x{r['addr']:x}"} for r in rows], indent=2))
    else:
        for r in rows:
            click.echo(f"0x{r['addr']:x}  {r['what']}")


@diag.command("hits")
@click.option("--run-dir", required=True, type=click.Path(exists=True, file_okay=False))
@click.option("--out", required=True, type=click.Path(file_okay=False))
@click.option("--addr", "addrs", multiple=True, help="Host address (hex) to count; repeatable.")
@click.option("--probe", "probes", multiple=True,
              help="A label from `scopt diag probes` of the deployed artifact; repeatable.")
@click.option("--all-probes", is_flag=True, help="Every probe of the deployed v7 artifact.")
@click.option("--v6-stats", is_flag=True, help="Also collect QEMU_SC_V6_STATS.")
@click.option("--timeout", "timeout_s", type=float, default=600.0, show_default=True)
@click.option("--json", "as_json", is_flag=True)
def diag_hits_cmd(run_dir, out, addrs, probes, all_probes, v6_stats, timeout_s, as_json):
    """gdb -nx hardware-breakpoint hit counts on host code (O3; never timed)."""
    from pathlib import Path

    from ..run.diag import DiagError, artifact_probe_addrs, gdb_hits

    targets = [(f"host 0x{int(a, 16):x}", int(a, 16)) for a in addrs]
    if probes or all_probes:
        dep = json.loads((Path(run_dir) / "deploy.json").read_text())
        rows = artifact_probe_addrs(dep["artifact"]["sc"])
        known = {r["what"]: r["addr"] for r in rows}
        missing = [p for p in probes if p not in known]
        if missing:
            raise click.UsageError(f"unknown probes {missing}; known: {sorted(known)}")
        targets += [(w, a) for w, a in known.items() if all_probes or w in probes]
    try:
        doc = gdb_hits(run_dir, out, targets, timeout_s=timeout_s, v6_stats=v6_stats)
    except DiagError as e:
        click.echo(f"diag refused: {e}", err=True)
        sys.exit(2)
    if as_json:
        click.echo(json.dumps(doc, indent=2, sort_keys=True, default=str))
    else:
        for r in doc["targets"]:
            click.echo(f"{r['addr']}  hits={r['hits']}  {r['what']}")
    sys.exit(0 if doc["ok"] else 1)


@diag.command("twin")
@click.option("--run-dir", required=True, type=click.Path(exists=True, file_okay=False))
@click.option("--out", required=True, type=click.Path(file_okay=False))
@click.option("--cov", is_flag=True, help="Dump FastDyn's LOG_REG coverage ring (O6).")
@click.option("--d-int", is_flag=True, help="Also write a -d int log.")
@click.option("--count-from-start", is_flag=True,
              help="Non-vacuity control: count from the first instruction.")
@click.option("--v6-stats", is_flag=True)
@click.option("--allowed-calls", type=int, default=0, show_default=True,
              help="Diagnostic only: exit 0 when at most this many flush calls follow "
                   "benchmark_start (within_allowance). Not m-CE7 clean, never gate "
                   "evidence; 'clean' in the output stays the 0-call rule.")
@click.option("--timeout", "timeout_s", type=float, default=300.0, show_default=True)
@click.option("--json", "as_json", is_flag=True)
def diag_twin_cmd(run_dir, out, cov, d_int, count_from_start, v6_stats, allowed_calls,
                  timeout_s, as_json):
    """Untimed diag twin: TLB-flush/memory-commit counters after benchmark_start (m-CE7)."""
    from ..run.diag import DiagError, diag_twin

    try:
        doc = diag_twin(run_dir, out, cov=cov, d_int=d_int, count_from_start=count_from_start,
                        v6_stats=v6_stats, timeout_s=timeout_s, allowed_calls=allowed_calls)
    except DiagError as e:
        click.echo(f"diag refused: {e}", err=True)
        sys.exit(2)
    _emit(doc, as_json, ("clean", "within_allowance", "allowed_calls", "qemu_exit", "e_map",
                         "e_bp", "guest_norm_equal_source"))
    if allowed_calls and not doc["clean"] and doc["within_allowance"]:
        click.echo(f"NOT m-CE7 clean: {doc['e_map']['total_after_benchmark_start']} flush "
                   f"calls after benchmark_start, within --allowed-calls {allowed_calls} "
                   "(diagnostic only, not gate evidence)", err=True)
    sys.exit(0 if (doc["within_allowance"] if allowed_calls else doc["clean"]) else 1)


@diag.command("stats")
@click.argument("path", type=click.Path(dir_okay=False))
def diag_stats_cmd(path):
    """Parse and check a QEMU_SC_V6_STATS file (qemu-sc-v6-stats@1)."""
    from ..run.diag import read_v6_stats

    doc = read_v6_stats(path)
    click.echo(json.dumps(doc, indent=2, sort_keys=True))
    sys.exit(0 if doc["present"] and not doc["problems"] else 1)
