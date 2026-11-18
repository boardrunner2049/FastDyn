"""``scopt run``: interleaved, paired, seeded timing arms (WP13, design §5.11, spec §8)."""

from __future__ import annotations

import json
import sys

import click


def register(group: click.Group) -> None:
    group.add_command(run_cmd)


def _kv(items, what: str) -> dict[str, str]:
    out = {}
    for s in items:
        k, sep, v = s.partition("=")
        if not sep or not k or not v:
            raise click.BadParameter(f"{what} must be NAME=PATH, got {s!r}")
        out[k] = v
    return out


@click.command("run")
@click.option("-c", "--config", "base_toml", type=click.Path(exists=True, dir_okay=False),
              help="Base FastDyn TOML with [Machine].supercache (all cache arms use it).")
@click.option("--campaign", "campaign_file", type=click.Path(exists=True, dir_okay=False),
              help="Campaign TOML (frozen arms, sha256 values, inputs); see "
              "scopt.run.arms.load_campaign. Options given here override its scalars.")
@click.option("--arms", "arms", default="nocache,empty,warm,identity,layout", show_default=True,
              help="Comma-separated arms. Deploy arms other than warm need --artifact.")
@click.option("--artifact", "artifacts", multiple=True, metavar="ARM=PATH",
              help="Artifact directory or .sc for a deploy arm (repeatable).")
@click.option("--warm", type=click.Path(exists=True),
              help="Warm (unoptimized) cache .sc or snapshot dir; default: record one with a "
              "cold run of the campaign's QEMU (auto).")
@click.option("--ablation", "ablations", multiple=True, metavar="PASS=PATH",
              help="Artifact optimized with --disable PASS; adds arm layout-no-PASS.")
@click.option("--nocache-config", type=click.Path(exists=True, dir_okay=False),
              help="TOML for the nocache arm (default: the base with supercache commented "
              "out). It may differ from the base only in supercache/supercache_flat.")
@click.option("--set", "split", type=click.Choice(["train", "heldout", "final", "fixed"]),
              default=None, help="Input split label (default: from --input-set, else fixed "
              "without inputs and unlabelled with --inputs).")
@click.option("--inputs", "inputs", multiple=True, type=click.Path(exists=True,
              dir_okay=False), help="Input files written into the input RAM bank (repeatable).")
@click.option("--input-set", type=click.Path(exists=True, file_okay=False),
              help="WP12 split directory with MANIFEST.json (e.g. fixtures/inputs/final/"
              "freertos-mps2-compute): every listed input, sha256-verified; the split label "
              "comes from the manifest.")
@click.option("--seed-input", type=click.Path(exists=True, dir_okay=False),
              help="Input for the warm=auto seed run (default: the first input). Use a train "
              "input when timing held-out or final inputs.")
@click.option("--input-bank", default=None, help="RAM bank id for inputs (default input).")
@click.option("--reps", type=int, default=None, help="Repetitions (default 30).")
@click.option("--seed", type=int, default=None, help="Order seed (default 1).")
@click.option("--interleave/--no-interleave", default=True, show_default=True,
              help="Interleaving is the only supported mode; --no-interleave is refused.")
@click.option("--reference", default=None, help="Reference arm (default warm).")
@click.option("--qemu", type=click.Path(exists=True, dir_okay=False),
              help="QEMU binary for every arm (FASTDYN_QEMU).")
@click.option("--quiet/--no-quiet", "quiet", default=None,
              help="Run FastDyn with -q (oracle and launch agree).")
@click.option("--timeout", "timeout_s", type=float, default=None)
@click.option("--no-check", is_flag=True, help="Skip deploy --check on each deploy arm's "
              "first run.")
@click.option("--name", default=None, help="Campaign name (default: the --out basename).")
@click.option("--out", required=True, type=click.Path(file_okay=False),
              help="Fresh output directory.")
@click.option("--dry-run", is_flag=True, help="Preflight and write the schedule only.")
@click.option("--report/--no-report", "do_report", default=True, show_default=True,
              help="Render OUT/report.md at the end.")
def run_cmd(base_toml, campaign_file, arms, artifacts, warm, ablations, nocache_config, split,
            inputs, input_set, seed_input, input_bank, reps, seed, interleave, reference, qemu, quiet, timeout_s,
            no_check, name, out, dry_run, do_report):
    """Run timing arms: fresh run dir per run, seeded interleaved order, paired reps.

    Writes OUT/campaign.json, OUT/runs.jsonl, OUT/ledger.jsonl and one WP8 run
    directory per run. Exit status: 0 every run is a valid sample; 3 some runs
    are not samples (see the report); 1 the frozen sources changed during the
    campaign; 2 the campaign was refused before any run.
    """
    from pathlib import Path

    from ..run.arms import CampaignError, campaign_from_args, load_campaign, run_campaign

    if not interleave:
        click.echo("refused: only interleaved campaigns are supported", err=True)
        sys.exit(2)
    name = name or Path(out).name
    try:
        if campaign_file:
            camp = load_campaign(campaign_file, reps=reps, seed=seed, split=split,
                                 qemu=qemu, quiet=quiet, timeout_s=timeout_s,
                                 reference=reference, input_bank=input_bank)
            if base_toml:
                camp.base_config = str(Path(base_toml).resolve())
            if inputs:
                camp.inputs = [str(Path(i).resolve()) for i in inputs]
                camp.input_set = None
            if input_set:
                camp.input_set = str(Path(input_set).resolve())
                camp.inputs = []
            if seed_input:
                camp.seed_input = str(Path(seed_input).resolve())
            if no_check:
                camp.check = False
        else:
            if not base_toml:
                raise click.UsageError("-c/--config or --campaign is required")
            camp = campaign_from_args(
                base_config=str(Path(base_toml).resolve()),
                arms=[a.strip() for a in arms.split(",") if a.strip()], out_name=name,
                artifacts={k: str(Path(v).resolve()) for k, v in
                           _kv(artifacts, "--artifact").items()},
                warm=str(Path(warm).resolve()) if warm else None,
                ablations={k: str(Path(v).resolve()) for k, v in
                           _kv(ablations, "--ablation").items()},
                reps=30 if reps is None else reps, seed=1 if seed is None else seed,
                split=split, inputs=[str(Path(i).resolve()) for i in inputs],
                input_set=str(Path(input_set).resolve()) if input_set else None,
                seed_input=str(Path(seed_input).resolve()) if seed_input else None,
                input_bank=input_bank or "input", reference=reference or "warm",
                nocache_config=str(Path(nocache_config).resolve()) if nocache_config else None,
                qemu=str(Path(qemu).resolve()) if qemu else None,
                timeout_s=120.0 if timeout_s is None else timeout_s, quiet=bool(quiet),
                check=not no_check)
        doc = run_campaign(camp, out, log=lambda m: click.echo(m, err=True), dry_run=dry_run)
    except CampaignError as e:
        click.echo(f"refused: {e.code}: {e.detail}", err=True)
        sys.exit(2)
    click.echo(json.dumps({k: doc.get(k) for k in ("status", "n_runs", "n_valid", "freeze_ok",
                                                    "duration_s")}, sort_keys=True))
    if do_report and not dry_run:
        from ..report.collect import collect
        from ..report.render import write_report

        paths = write_report(collect([out]), Path(out) / "report.md")
        click.echo(f"report: {paths['markdown']}")
    if doc.get("status") == "dry_run":
        sys.exit(0)
    if doc.get("status") != "done":
        sys.exit(1)                      # freeze violation
    sys.exit(0 if doc.get("n_valid") == doc.get("n_runs") else 3)
