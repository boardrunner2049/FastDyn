"""``scopt prepare``: capture-on cold run(s) for ``scopt import`` (§5.11; WP14)."""

from __future__ import annotations

import json
import sys

import click


def register(group: click.Group) -> None:
    group.add_command(prepare_cmd)


@click.command("prepare")
@click.option("-c", "--config", "base_toml", required=True,
              type=click.Path(exists=True, dir_okay=False), help="Base FastDyn TOML.")
@click.option("--out", required=True, type=click.Path(file_okay=False),
              help="Fresh prep directory (a WP8 run directory).")
@click.option("--inputs", "inputs", multiple=True, type=click.Path(exists=True, dir_okay=False),
              help="Input files for the input RAM bank; the first is the cold run, each "
                   "further one a capture-on warm run appending to the same cache.")
@click.option("--input-bank", default="input", show_default=True)
@click.option("--qemu", type=click.Path(exists=True, dir_okay=False),
              help="Export FASTDYN_QEMU (a qemu_path in the TOML still wins).")
@click.option("--quiet/--no-quiet", "quiet", default=False, show_default=True)
@click.option("--no-oracle", is_flag=True, help="Skip the oracle-only run directory OUT/oracle.")
@click.option("--timeout", "timeout_s", type=float, default=300.0, show_default=True)
@click.option("--json", "as_json", is_flag=True)
def prepare_cmd(base_toml, out, inputs, input_bank, qemu, quiet, no_oracle, timeout_s, as_json):
    """Capture-on cold run of BASE.toml into OUT (QEMU_SC_CAPTURE set; §4.3).

    OUT holds cache/<name>.sc, its manifest and capture.scm, ready for
    ``scopt import OUT``; OUT/oracle holds the QEMU argv for ``scopt bundle
    runtime-identity --run-dir OUT/oracle``. Exit 0 PASS, 1 FAIL, 2 refused.
    """
    from ..run.launcher import RunDirError
    from ..run.prepare import PrepareError, prepare

    try:
        doc = prepare(base_toml, out, inputs=inputs, input_bank=input_bank, qemu=qemu,
                      quiet=quiet, timeout_s=timeout_s, oracle=not no_oracle)
    except (PrepareError, RunDirError) as e:
        click.echo(f"refused: {e.code}: {e.detail}", err=True)
        sys.exit(2)
    if as_json:
        click.echo(json.dumps(doc, indent=2, sort_keys=True))
    else:
        click.echo(f"verdict: {doc['verdict']}")
        for r in doc["runs"]:
            click.echo(f"  run {r['k']} ({r['kind']}): exit={r['exit_code']} "
                       f"records={r.get('records_after')} banner={r['capture_banner']} "
                       f"capture={r['capture']}")
        for f in doc["failures"]:
            click.echo(f"  FAIL: {f}")
    sys.exit(0 if doc["verdict"] == "PASS" else 1)
