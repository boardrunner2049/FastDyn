"""``scopt import``: import a prepared cache into the snapshot store (§4.3, §5.11). WP7."""

from __future__ import annotations

import json
import signal
import sys

import click


def register(group: click.Group) -> None:
    group.add_command(import_cmd)


@click.command("import")
@click.argument("prep", required=False, type=click.Path(exists=True))
@click.option("--sc", "sc", type=click.Path(exists=True, dir_okay=False),
              help="The .sc (default: the single *.sc under PREP).")
@click.option("--scm", "scms", multiple=True, type=click.Path(exists=True, dir_okay=False),
              help="Capture files (default: every *.scm under PREP).")
@click.option("--manifest", type=click.Path(exists=True, dir_okay=False),
              help="FastDyn manifest (default: <sc>.fastdyn.json).")
@click.option("--bundle", type=click.Path(exists=True, dir_okay=False),
              help="ExecutionBundle v1 JSON the snapshot must match.")
@click.option("--runtime-identity", "runtime_identity",
              type=click.Path(exists=True, dir_okay=False), help="RuntimeIdentity JSON.")
@click.option("--firmware", type=click.Path(exists=True, dir_okay=False),
              help="Guest firmware ELF (default: the manifest's firmware).")
@click.option("--plugin", type=click.Path(exists=True, dir_okay=False),
              help="FastDyn plugin .so (default: the manifest's plugin).")
@click.option("--store", type=click.Path(file_okay=False),
              help="Store root; writes STORE/snapshots/<id>/. Without it nothing is written.")
@click.option("--strict", is_flag=True, help="Fail the import on any missing_reference.")
@click.option("--json", "as_json", is_flag=True, help="Print snapshot.json instead of a summary.")
@click.option("--progress/--no-progress", "progress_flag", default=None,
              help="Progress lines on stderr (default: when stderr is a terminal).")
def import_cmd(prep, sc, scms, manifest, bundle, runtime_identity, firmware, plugin, store,
               strict, as_json, progress_flag):
    """Import PREP (a prep dir or a .sc) into a CacheSnapshot; exit 2 on reject.

    Progress lines go to stderr when it is a terminal. Writing the store
    fsyncs every file and can take a minute or more on a slow disk. An
    interrupted import (Ctrl-C, SIGTERM, SIGHUP) removes its temporary
    directory: the store never holds a partial snapshot.
    """
    from ..importer.import_cache import ImportRejected, import_snapshot

    if prep is None and sc is None:
        raise click.UsageError("give PREP or --sc")

    def _interrupt(signum, _frame):
        raise KeyboardInterrupt(signal.Signals(signum).name)

    old = {}
    try:  # only possible in the main thread
        for sig in (signal.SIGTERM, signal.SIGHUP):
            old[sig] = signal.signal(sig, _interrupt)
    except ValueError:
        pass
    if progress_flag is None:
        progress_flag = sys.stderr.isatty()
    progress = (lambda m: click.echo(m, err=True)) if progress_flag else None
    try:
        res = import_snapshot(prep, sc=sc, scms=list(scms) or None, manifest=manifest,
                              store=store, bundle=bundle, runtime_identity=runtime_identity,
                              firmware=firmware, plugin=plugin, strict=strict,
                              progress=progress)
    except ImportRejected as e:
        click.echo(f"import rejected: {e.reason}" + (f" ({e.detail})" if e.detail else ""),
                   err=True)
        sys.exit(2)
    except KeyboardInterrupt as e:
        click.echo(f"import interrupted ({e or 'SIGINT'}); the store holds no partial snapshot",
                   err=True)
        sys.exit(130)
    finally:
        for sig, h in old.items():
            signal.signal(sig, h)
    s = res.summary
    if as_json:
        click.echo(json.dumps(s, indent=2, sort_keys=True))
    else:
        valid = s["records"]["valid"]
        bound = s["capture"]["binding"].get("bound", 0)
        click.echo(f"snapshot {s['snapshot_id']}")
        click.echo(f"slots: {valid} valid, {bound} bound "
                   f"({100.0 * bound / max(valid, 1):.1f}%), skipped records: "
                   f"{len(s['records']['skipped'])}")
        click.echo("status: " + ", ".join(f"{k}={v}" for k, v in s["status"].items()))
        click.echo("pin reasons: " + (", ".join(f"{k}={v}" for k, v in s["pin_reasons"].items())
                                      or "none"))
        if s["capture"]["problems"]:
            click.echo("capture problems: " + ", ".join(
                f"{p['file']}:{p['code']}" for p in s["capture"]["problems"]))
        t = s["tallies"]
        own = ", ".join(f"+{k}:{v}" for k, v in t["own_struct_by_idx"].items())
        click.echo(f"tallies: {t['helper_pool_calls']} / {t['plugin_rel32_calls']} / "
                   f"{t['ext_rip_lea']} / {t['tb_ret_jmps']} / {t['epilogue_jmps']} / "
                   f"{t['own_struct_lea']} ({own}) / {t['raddr_lea']} / {t['goto_ptr']} / "
                   f"{t['movabs']} movabs; helpers {t['helper_names']}")
        for name, chk in (s.get("identity_checks") or {}).items():
            if chk is not None:
                click.echo(f"{name}: {len(chk['verified'])} fields verified; unverified: "
                           + (", ".join(chk["unverified"]) or "none"))
        if res.store_dir:
            click.echo(f"store: {res.store_dir} ({res.store_status})")
