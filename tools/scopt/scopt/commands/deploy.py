"""``scopt deploy`` and ``scopt rundir``: per-run directories, deploy oracle,
launch and the post-run proof (WP8, design §4.8)."""

from __future__ import annotations

import json
import sys

import click


def register(group: click.Group) -> None:
    group.add_command(deploy_cmd)
    group.add_command(rundir)


def _emit(doc, as_json: bool, summary: dict) -> None:
    if as_json:
        click.echo(json.dumps(doc, indent=2, sort_keys=True))
    else:
        for k, v in summary.items():
            click.echo(f"{k}: {v}")


@click.command("deploy")
@click.argument("artifact", type=click.Path(exists=True))
@click.option("-c", "--config", "base_toml", required=True, type=click.Path(exists=True,
              dir_okay=False), help="Base FastDyn TOML.")
@click.option("--run-dir", required=True, type=click.Path(file_okay=False),
              help="Fresh run directory to create (must not exist or be empty).")
@click.option("--check", is_flag=True, help="Also run the shim twice in scratch work dirs and "
              "require equal fingerprints.")
@click.option("--method", type=click.Choice(["oracle", "seed"]), default="oracle",
              show_default=True, help="oracle: FastDyn shim; seed: one cold seed run.")
@click.option("--quiet/--no-quiet", "quiet", default=False, show_default=True,
              help="Run FastDyn with -q (the oracle and the launch always agree).")
@click.option("--qemu", type=click.Path(exists=True, dir_okay=False),
              help="QEMU binary, exported as FASTDYN_QEMU (default: inherit).")
@click.option("--timeout", "timeout_s", type=float, default=120.0, show_default=True)
@click.option("--x-manifest-override", "manifest_override", hidden=True,
              type=click.Path(exists=True, dir_okay=False),
              help="Testing only: replace the oracle manifest with this file after the copy.")
@click.option("--json", "as_json", is_flag=True)
def deploy_cmd(artifact, base_toml, run_dir, check, method, quiet, qemu, timeout_s,
               manifest_override, as_json):
    """Create RUN_DIR from BASE.toml and deploy ARTIFACT (artifact dir or .sc) into it.

    Exit status 2 when deploy refuses (reason code printed).
    """
    from ..run.deploy import DeployError, deploy_new

    try:
        rd, doc = deploy_new(artifact, base_toml, run_dir, check=check, method=method,
                             quiet=quiet, qemu=qemu, timeout_s=timeout_s,
                             manifest_override=manifest_override)
    except DeployError as e:
        click.echo(f"deploy refused: {e.code}: {e.detail}", err=True)
        sys.exit(2)
    _emit(doc, as_json, {
        "run_dir": doc["run_dir"], "method": doc["method"],
        "fingerprint": doc["oracle"]["fingerprint"],
        "check": None if "check" not in doc else doc["check"]["ok"],
        "deployed_records": doc["state"]["n"],
        "bundle_id": doc["identity"].get("bundle_id"),
    })


@click.group()
def rundir() -> None:
    """Per-run directories: create, launch, prove."""


@rundir.command("create")
@click.option("-c", "--config", "base_toml", required=True, type=click.Path(exists=True,
              dir_okay=False))
@click.option("--run-dir", required=True, type=click.Path(file_okay=False))
@click.option("--quiet/--no-quiet", "quiet", default=False, show_default=True)
@click.option("--qemu", type=click.Path(exists=True, dir_okay=False))
def rundir_create(base_toml, run_dir, quiet, qemu):
    """Create a fresh run directory (run.toml with only the path keys rewritten)."""
    from ..run.launcher import RunDirError, create_run_dir
    from ..run.toml_rewrite import TomlRewriteError

    try:
        rd = create_run_dir(base_toml, run_dir, quiet=quiet, qemu=qemu)
    except (RunDirError, TomlRewriteError) as e:
        click.echo(f"refused: {e}", err=True)
        sys.exit(2)
    click.echo(str(rd.root))


@rundir.command("oracle")
@click.argument("run_dir", type=click.Path(exists=True, file_okay=False))
@click.option("--timeout", "timeout_s", type=float, default=120.0, show_default=True)
def rundir_oracle(run_dir, timeout_s):
    """Run only the deploy oracle in RUN_DIR: writes the FastDyn manifest and oracle.json."""
    from ..run.deploy import DeployError, oracle
    from ..run.launcher import RunDir

    rd = RunDir.open(run_dir)
    try:
        rec = oracle(rd, timeout_s=timeout_s)
    except DeployError as e:
        click.echo(f"refused: {e.code}: {e.detail}", err=True)
        sys.exit(2)
    click.echo(f"manifest: {rd.manifest}")
    click.echo(f"fingerprint: {rec['supercache']['fingerprint']}")


@rundir.command("launch")
@click.argument("run_dir", type=click.Path(exists=True, file_okay=False))
@click.option("--timeout", "timeout_s", type=float, default=120.0, show_default=True)
@click.option("--reference-norm", type=click.Path(exists=True, dir_okay=False),
              help="Compare the B0-normalized output with this .norm/.log file; a "
                   "mismatch makes the exit status 1.")
@click.option("--inputs", "input_file", type=click.Path(exists=True, dir_okay=False),
              help="Input file written into the --input-bank RAM bank after the RAM reset "
                   "(launch resets every RAM bank, so a file put there by hand is lost).")
@click.option("--input-bank", default="input", show_default=True,
              help="Id of the file-backed memory bank that receives --inputs.")
@click.option("--json", "as_json", is_flag=True)
def rundir_launch(run_dir, timeout_s, reference_norm, input_file, input_bank, as_json):
    """Run FastDyn once in RUN_DIR, then the post-run proof; writes run.json.

    Exit status 0 only when the verdict is PASS (or NOT_PROVED for an
    undeployed run that exited 0) and, with --reference-norm, the normalized
    output equals the reference; else 1. 2 when refused.
    """
    from ..run.launcher import RunDir, RunDirError, launch

    inputs = {input_bank: input_file} if input_file else None
    try:
        doc = launch(RunDir.open(run_dir), timeout_s=timeout_s, reference_norm=reference_norm,
                     inputs=inputs)
    except RunDirError as e:
        click.echo(f"refused: {e}", err=True)
        sys.exit(2)
    ref_equal = (doc.get("reference_norm") or {}).get("equal")
    _emit(doc, as_json, {
        "verdict": doc["verdict"], "exit_code": doc["exit_code"], "appended": doc["appended"],
        "bench": doc["bench"], "proof_failed": (doc["proof"] or {}).get("failed"),
        "reference_norm_equal": ref_equal,
    })
    ok = doc["verdict"] in ("PASS", "NOT_PROVED")
    if reference_norm is not None and ref_equal is not True:
        click.echo("output differs from --reference-norm", err=True)
        ok = False
    sys.exit(0 if ok else 1)


@rundir.command("prove")
@click.argument("run_dir", type=click.Path(exists=True, file_okay=False))
def rundir_prove(run_dir):
    """Re-run the §4.8 proof for RUN_DIR (prints JSON)."""
    from ..run.launcher import RunDir
    from ..run.proof import prove_run

    doc = prove_run(RunDir.open(run_dir))
    click.echo(json.dumps(doc, indent=2, sort_keys=True))
    sys.exit(0 if doc["verdict"] == "PASS" else 1)
