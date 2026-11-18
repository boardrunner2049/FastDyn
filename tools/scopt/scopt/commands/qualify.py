"""``scopt qualify-capture``: T-B1-1 byte-identity qualification (WP8, §2.6)."""

from __future__ import annotations

import sys

import click


def register(group: click.Group) -> None:
    group.add_command(qualify_capture_cmd)


@click.command("qualify-capture")
@click.option("-c", "--config", "base_toml", required=True,
              type=click.Path(exists=True, dir_okay=False),
              help="Base TOML; set [Machine].qemu_path to the capture-capable QEMU.")
@click.option("--runs", "off", type=int, default=3, show_default=True,
              help="Capture-off cold runs (T-B1-1a).")
@click.option("--on-runs", "on", type=int, default=2, show_default=True,
              help="Capture-on cold runs (T-B1-1b).")
@click.option("--out", required=True, type=click.Path(file_okay=False),
              help="Fresh output directory; qualification.json is written there.")
@click.option("--qemu", type=click.Path(exists=True, dir_okay=False),
              help="Export FASTDYN_QEMU (a qemu_path in the TOML still wins).")
@click.option("--quiet/--no-quiet", "quiet", default=False, show_default=True)
@click.option("--timeout", "timeout_s", type=float, default=120.0, show_default=True)
@click.option("--input", "input_file", type=click.Path(exists=True, dir_okay=False),
              help="Input file written into the --input-bank RAM bank before every run.")
@click.option("--input-bank", default="input", show_default=True)
def qualify_capture_cmd(base_toml, off, on, out, qemu, quiet, timeout_s, input_file,
                        input_bank):
    """Run T-B1-1a/1b in one run directory and write qualification.json.

    Gate (§2.6): whole-file sha256 equality when the capture-off runs are
    byte-deterministic (mode whole_file); otherwise the weaker per-key
    comparison over the .scm site list (mode per_key, reported as such).
    Exit status: 0 PASS, 1 FAIL.
    """
    from ..run.qualify import qualify_capture

    doc = qualify_capture(base_toml, out, off=off, on=on, qemu=qemu, quiet=quiet,
                          timeout_s=timeout_s,
                          inputs={input_bank: input_file} if input_file else None)
    click.echo(f"verdict: {doc['verdict']} (mode {doc['mode']})")
    for r in doc["runs"]:
        click.echo(f"  {r['name']}: exit={r['exit_code']} records={r.get('record_count')} "
                   f"sha256={str(r.get('sc_sha256'))[:16]} banner={r['capture_banner']}")
    for f in doc["failures"]:
        click.echo(f"  FAIL: {f}")
    sys.exit({"PASS": 0, "FAIL": 1}.get(doc["verdict"], 3))
