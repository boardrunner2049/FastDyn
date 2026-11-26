"""``scopt publish`` and ``scopt direct {deploy,launch}`` (WP26, B3/B4 design §9.3-§9.5)."""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

import click


def register(group: click.Group) -> None:
    group.add_command(publish_cmd)
    group.add_command(direct)


def publish_directory(src: str, store: str) -> dict:
    """Publish the artifact directory ``src`` into ``store`` (a verified copy;
    ``src`` is not changed).

    Nothing is copied unless the source is the artifact its validation
    describes (B1-design §4.6 "on any validation failure nothing is
    published", fail closed):

    1. not a negative control / test-policy artifact (:mod:`scopt.run.policy`);
    2. ``cache.sc`` and a schema-valid sidecar without ``x_uncertified``;
    3. the source's own ``MANIFEST.sha256`` verifies (every file, nothing
       unlisted): a ``cache.sc`` changed after packaging is refused;
    4. the sidecar's ``validation_sha256`` is the sha256 of ``validation.json``;
    5. ``validation.json`` says ``accepted``, has no failing row, and names the
       sidecar's input/output SnapshotIds; for B3/B4 edits its V12/V13/V14
       ``coverage`` rows name and certify every edited unit, guarded R' and
       variant (:func:`scopt.validate.certification_problems`);
    6. the output SnapshotId recomputed from the ``cache.sc`` bytes (as the
       canonical loader sees them) equals that SnapshotId;
    7. the staged copy hashes to the source manifest byte for byte, so the
       published ArtifactId is the source's.

    Refusals (``ArtifactError.code``): ``negative_control``,
    ``test_only_artifact``, ``not_an_artifact``, ``sidecar_schema``,
    ``certification_not_run``, ``certification_coverage``, ``manifest_verify``, ``validation_mismatch``,
    ``validation_failed``, ``output_snapshot_mismatch``, ``copy_mismatch`` and
    :func:`scopt.formats.artifact.publish`'s own codes."""
    import shutil

    from ..canon import sha256_id
    from ..formats import artifact as A
    from ..formats.sc_records import ScFormatError, sparse_copy
    from ..formats.supercache import canonical_snapshot_id
    from ..run.policy import publish_refusal

    p = Path(src).resolve()
    why = publish_refusal(p)
    if why is not None:
        raise A.ArtifactError(*why)
    side = p / A.SIDECAR
    if not (p.is_dir() and side.is_file() and (p / "cache.sc").is_file()):
        raise A.ArtifactError("not_an_artifact", f"{p}: needs cache.sc and {A.SIDECAR}")
    try:
        doc = json.loads(side.read_text())
    except ValueError as e:
        raise A.ArtifactError("sidecar_schema", f"unreadable sidecar: {e}") from e
    errs = A.sidecar_errors(doc) if isinstance(doc, dict) else ["not an object"]
    if errs:
        raise A.ArtifactError("sidecar_schema", "; ".join(errs[:3]))
    if doc.get("x_uncertified"):
        raise A.ArtifactError("certification_not_run",
                              f"sidecar lists uncertified rows {doc['x_uncertified']}")
    probs = A.verify_manifest(p)
    if probs:
        raise A.ArtifactError("manifest_verify", "source " + "; ".join(probs[:5]))
    src_manifest = (p / A.MANIFEST).read_bytes()
    vp = p / A.VALIDATION
    if not vp.is_file():
        raise A.ArtifactError("validation_failed", f"{vp} missing")
    vb = vp.read_bytes()
    if doc.get("validation_sha256") != sha256_id(vb):
        raise A.ArtifactError("validation_mismatch",
                              f"sidecar validation_sha256 {doc.get('validation_sha256')} != "
                              f"sha256 of validation.json {sha256_id(vb)}")
    try:
        val = json.loads(vb)
    except ValueError as e:
        raise A.ArtifactError("validation_failed", f"unreadable validation.json: {e}") from e
    if not isinstance(val, dict):
        raise A.ArtifactError("validation_failed", "validation.json is not an object")
    rows = val.get("rows", [])
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise A.ArtifactError("validation_failed", "validation.json rows are not a list of "
                              "objects")
    bad = [r for r in rows if r.get("result") == "fail"]
    if bad:
        raise A.ArtifactError("validation_failed",
                              f"{len(bad)} failing rows, first {bad[0].get('code')}")
    if val.get("verdict") != "accepted":
        raise A.ArtifactError("validation_failed", f"validation verdict {val.get('verdict')!r}")
    for k in ("input_snapshot_id", "output_snapshot_id"):
        if val.get(k) != doc.get(k):
            raise A.ArtifactError("validation_mismatch",
                                  f"validation.json {k} {val.get(k)} != sidecar {doc.get(k)}")
    from ..validate import certification_problems

    cp = certification_problems(doc, val)
    if cp:
        # B3/B4 edits need a certifying hostsym verdict per edited unit, guarded R'
        # and SPEC link (validate_b3 coverage rows), not any one passing row
        raise A.ArtifactError("certification_coverage", "; ".join(cp[:5]))
    try:
        sid = canonical_snapshot_id(p / "cache.sc")
    except (ScFormatError, OSError, ValueError) as e:
        raise A.ArtifactError("output_snapshot_mismatch", f"cache.sc unreadable: {e}") from e
    if sid is None or sid != doc.get("output_snapshot_id"):
        raise A.ArtifactError("output_snapshot_mismatch",
                              f"cache.sc SnapshotId {sid} != validated output "
                              f"{doc.get('output_snapshot_id')}")
    stage = A.staging_dir(store)
    try:
        for f in sorted(p.rglob("*")):
            rel = f.relative_to(p)
            if rel.as_posix() == A.MANIFEST:
                continue
            dst = stage / rel
            if f.is_dir():
                dst.mkdir(parents=True, exist_ok=True)
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            if rel.as_posix() == "cache.sc":
                sparse_copy(f, dst)
            else:
                shutil.copyfile(f, dst)
        staged = A.manifest_bytes(stage)
        if staged != src_manifest:
            raise A.ArtifactError("copy_mismatch", "the staged copy does not hash to the "
                                  "source MANIFEST.sha256")
        aid, final, status = A.publish(stage, store, known=A.parse_manifest(staged))
    finally:
        if stage.exists():
            A.remove_tree(stage)
    return {"artifact_id": aid, "artifact_dir": str(final), "status": status, "source": str(p)}


@click.command("publish")
@click.argument("artifact", type=click.Path(exists=True, file_okay=False))
@click.option("--store", required=True, type=click.Path(file_okay=False),
              help="Store whose artifacts/ receives the copy.")
@click.option("--json", "as_json", is_flag=True)
def publish_cmd(artifact, store, as_json):
    """Publish an artifact directory into STORE (verified copy).

    The source must verify: its MANIFEST.sha256, the sidecar's validation_sha256,
    an accepted validation.json naming the sidecar's snapshots, and the output
    SnapshotId recomputed from cache.sc. Negative controls (scopt.run.negative)
    and test-policy artifacts are refused.
    Exit status 0 when published, 2 when refused (reason code printed).
    """
    from ..formats.artifact import ArtifactError

    try:
        doc = publish_directory(artifact, store)
    except ArtifactError as e:
        click.echo(f"publish refused: {e.code}: {e.detail}", err=True)
        sys.exit(2)
    if as_json:
        click.echo(json.dumps(doc, indent=2, sort_keys=True))
    else:
        click.echo(f"artifact {doc['artifact_id']} ({doc['status']})\n  {doc['artifact_dir']}")


@click.group()
def direct() -> None:
    """Direct QEMU runs without the FastDyn frontend (mps2 alias, F-B3-15)."""


@direct.command("deploy")
@click.argument("artifact", type=click.Path(exists=True))
@click.option("--run-dir", required=True, type=click.Path(file_okay=False))
@click.option("--argv", "argv_str", default=None,
              help="QEMU command template (shell words) with @SUPERCACHE@ (and @RUN@, @QEMU@).")
@click.option("--argv-json", type=click.Path(exists=True, dir_okay=False),
              help="JSON list with the same template.")
@click.option("--qemu", type=click.Path(exists=True, dir_okay=False),
              help="Replaces @QEMU@ in the template.")
@click.option("--cache-name", default="cache.sc", show_default=True)
@click.option("--test-policy", "test_policies", multiple=True,
              help="Admit an artifact built under this test policy (allow_ram_resident).")
@click.option("--json", "as_json", is_flag=True)
def direct_deploy(artifact, run_dir, argv_str, argv_json, qemu, cache_name, test_policies,
                  as_json):
    """Deploy ARTIFACT (bare .sc or artifact dir) for a direct QEMU run. Exit 2 on refusal."""
    from ..run.deploy import DeployError, deploy_direct

    if (argv_str is None) == (argv_json is None):
        raise click.UsageError("give exactly one of --argv and --argv-json")
    argv = shlex.split(argv_str) if argv_str else json.loads(Path(argv_json).read_text())
    try:
        doc = deploy_direct(artifact, run_dir, argv=argv, qemu=qemu, cache_name=cache_name,
                            test_policies=test_policies)
    except DeployError as e:
        click.echo(f"deploy refused: {e.code}: {e.detail}", err=True)
        sys.exit(2)
    if as_json:
        click.echo(json.dumps(doc, indent=2, sort_keys=True))
    else:
        click.echo(f"run_dir: {doc['run_dir']}\nidentity.checked: {doc['identity']['checked']}"
                   f"\ndeployed_records: {doc['state']['n']}")


@direct.command("launch")
@click.option("--run-dir", required=True, type=click.Path(exists=True, file_okay=False))
@click.option("--timeout", "timeout_s", type=float, default=60.0, show_default=True)
@click.option("--v6-stats", is_flag=True, help="Set QEMU_SC_V6_STATS=<run-dir>/v6-stats.json.")
@click.option("--json", "as_json", is_flag=True)
def direct_launch(run_dir, timeout_s, v6_stats, as_json):
    """Run a direct deploy once and prove it (exit 0 only for verdict PASS)."""
    from ..run.deploy import launch_direct
    from ..run.diag import read_v6_stats, stats_env

    sp = Path(run_dir).resolve() / "v6-stats.json"
    doc = launch_direct(run_dir, timeout_s=timeout_s,
                        extra_env=stats_env(sp) if v6_stats else None)
    if v6_stats:
        doc["v6_stats"] = read_v6_stats(sp)
    if as_json:
        click.echo(json.dumps(doc, indent=2, sort_keys=True))
    else:
        click.echo(f"verdict: {doc['verdict']}\nexit_code: {doc['exit_code']}\n"
                   f"appended: {doc['appended']}")
    sys.exit(0 if doc["verdict"] == "PASS" else 1)
