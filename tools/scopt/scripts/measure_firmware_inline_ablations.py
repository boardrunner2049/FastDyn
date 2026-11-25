#!/usr/bin/env python3
"""Paired real-firmware timing for runtime firmware-inline ablations.

Each ``--arm`` names an artifact.  ``--env`` attaches runtime-only environment
settings to one arm, so all ablations can use one QEMU executable and one
captured artifact.  A diagnostic launch per arm proves behavior, zero appended
translations and (for v7) the exact loaded artifact id before timing begins.
Diagnostic counters are not enabled in the scored launches.

By default an A/A arm (``<reference>-aa``) deploys the reference arm's
artifact again under another name.  Its paired ratio is the harness noise
floor, and every comparison reports its block-ratio median, bootstrap
interval, sign test and verdict against that floor
(``scopt.run.paired_stats``).  Fewer than ``--min-blocks`` pairs are refused
unless ``--allow-few-blocks`` is given.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import random
import re
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from scopt.run.deploy import deploy
from scopt.run import paired_stats
from scopt.run.launcher import create_run_dir, launch
from scopt.run.v7_stats import counter_problems, summary as counter_summary


_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_EXPERIMENT_KEYS = {
    "source-index": "source_index",
    "linear-source-scan": "linear_source_scan",
    "trusted-set": "trusted_set",
    "no-tree": "no_tree",
    "incoming-chain": "incoming_chain",
    "disable-chaining": "disable_chaining",
    "no-regions": "no_regions",
    "no-trusted-exit-chain": "no_trusted_exit_chain",
    "no-sealed-sc6": "no_sealed_sc6",
}


@dataclass
class Arm:
    name: str
    artifact: Path
    env: dict[str, str] = field(default_factory=dict)
    diagnostic_env: dict[str, str] = field(default_factory=dict)
    cache_sha256: str | None = None


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()


ROUNDS = 10_000


def interval(values: list[float], rng: random.Random,
             rounds: int = ROUNDS) -> list[float]:
    return paired_stats.bootstrap_median_ci(values, rng, rounds)


def _parse_env_rows(rows: list[str], arms: dict[str, Arm], attr: str) -> None:
    for row in rows:
        if ":" not in row or "=" not in row.split(":", 1)[1]:
            raise ValueError(f"invalid environment setting {row!r}; expected ARM:KEY=VALUE")
        name, setting = row.split(":", 1)
        key, value = setting.split("=", 1)
        if name not in arms or not key or "=" in key or "\0" in value:
            raise ValueError(f"invalid environment setting {row!r}")
        getattr(arms[name], attr)[key] = value


def parse_arms(rows: list[str], env_rows: list[str],
               diagnostic_env_rows: list[str] | None = None) -> dict[str, Arm]:
    arms: dict[str, Arm] = {}
    for row in rows:
        if "=" not in row:
            raise ValueError(f"invalid --arm {row!r}; expected NAME=ARTIFACT")
        name, artifact = row.split("=", 1)
        if not _NAME.fullmatch(name) or name in arms or not artifact:
            raise ValueError(f"invalid or duplicate arm name in {row!r}")
        arms[name] = Arm(name, Path(artifact).resolve())
    _parse_env_rows(env_rows, arms, "env")
    _parse_env_rows(diagnostic_env_rows or [], arms, "diagnostic_env")
    if len(arms) < 2:
        raise ValueError("at least two --arm values are required")
    for arm in arms.values():
        cache = arm.artifact / "cache.sc"
        if not cache.is_file():
            raise ValueError(f"{arm.name}: {cache} is missing")
    return arms


def parse_comparisons(rows: list[str], arms: dict[str, Arm]) -> list[tuple[str, str]]:
    if not rows:
        baseline = next(iter(arms))
        return [(name, baseline) for name in arms if name != baseline]
    out: list[tuple[str, str]] = []
    for row in rows:
        if ":" not in row:
            raise ValueError(f"invalid --compare {row!r}; expected CANDIDATE:BASELINE")
        candidate, baseline = row.split(":", 1)
        if candidate not in arms or baseline not in arms or candidate == baseline:
            raise ValueError(f"invalid --compare {row!r}")
        pair = (candidate, baseline)
        if pair in out:
            raise ValueError(f"duplicate --compare {row!r}")
        out.append(pair)
    return out


def add_aa_arm(arms: dict[str, Arm], comparisons: list[tuple[str, str]],
               reference: str | None = None) -> tuple[str, str]:
    """Add the A/A copy of ``reference`` (default: the first comparison's
    baseline) to ``arms`` after the comparisons were parsed, so it is never
    a default comparison.  Returns ``(aa_arm, reference)``."""
    reference = reference or comparisons[0][1]
    if reference not in arms:
        raise ValueError(f"--aa-reference {reference!r} is not an arm")
    name = paired_stats.aa_arm_name(reference, arms)
    if not _NAME.fullmatch(name):
        raise ValueError(f"invalid A/A arm name {name!r}")
    source = arms[reference]
    arms[name] = Arm(name, source.artifact, env=dict(source.env),
                     diagnostic_env=dict(source.diagnostic_env),
                     cache_sha256=source.cache_sha256)
    return name, reference


def validate_run(block: str, arm: Arm, run: dict[str, Any],
                 expected_norm: list[str] | None) -> list[str]:
    if run.get("verdict") != "PASS" or run.get("appended") != 0 or not run.get("bench"):
        raise RuntimeError(f"{block} {arm.name}: invalid run {run.get('verdict')}, "
                           f"appended={run.get('appended')}")
    norm = run.get("guest_norm")
    if expected_norm is not None and norm != expected_norm:
        raise RuntimeError(f"{block} {arm.name}: guest output changed")
    return norm


def validate_experiment(arm: Arm, stats: dict[str, Any]) -> None:
    raw = arm.env.get("QEMU_SC_V7_EXPERIMENT")
    if raw is None:
        return
    tokens = {token for token in raw.split(",") if token}
    unknown = tokens - _EXPERIMENT_KEYS.keys()
    if unknown:
        raise RuntimeError(f"diagnostic {arm.name}: unknown requested tokens {sorted(unknown)}")
    have = stats.get("experiment")
    expected = {field: token in tokens for token, field in _EXPERIMENT_KEYS.items()}
    if (stats.get("loaded") is True and stats.get("mode") in {"safe", "trusted"} and
            "linear-source-scan" not in tokens):
        expected["source_index"] = True
    if have != expected:
        raise RuntimeError(f"diagnostic {arm.name}: runtime experiment flags "
                           f"{have!r} != {expected!r}")


def diagnostic(config: Path, qemu: Path, out: Path, arm: Arm,
               timeout: float, expected_norm: list[str] | None) -> tuple[dict, list[str]]:
    rd = create_run_dir(config, out / f"diagnostic-{arm.name}", qemu=qemu,
                        label=f"firmware-inline-ablation-diagnostic-{arm.name}")
    dep = deploy(arm.artifact, rd, timeout_s=timeout)
    stats_path = rd.root / "v7-stats.json"
    diagnostic_env = {key: value.replace("{run_dir}", str(rd.root))
                      for key, value in arm.diagnostic_env.items()}
    run = launch(rd, timeout_s=timeout,
                 extra_env={**arm.env, **diagnostic_env,
                            "QEMU_SC_V7_STATS": str(stats_path)})
    expected_norm = validate_run("diagnostic", arm, run, expected_norm)
    try:
        stats = json.loads(stats_path.read_text())
    except (OSError, ValueError) as exc:
        stats = {"schema": None, "problems": [f"unreadable: {exc}"]}
    state = dep.get("state") or {}
    expected_id = state.get("firmware_artifact_id8")
    if state.get("file_version") == 7:
        if (stats.get("schema") != "qemu-sc-v7-stats@1" or
                stats.get("loaded") is not True or
                stats.get("artifact_id8") != expected_id):
            raise RuntimeError(f"diagnostic {arm.name}: loaded v7 artifact identity "
                               f"{stats.get('artifact_id8')!r} != {expected_id!r}")
    validate_experiment(arm, stats)
    problems = counter_problems(stats)
    if problems:
        raise RuntimeError(f"diagnostic {arm.name}: counter invariants {problems}")
    diagnostic_outputs: dict[str, Any] = {}
    for key, value in diagnostic_env.items():
        path = Path(value)
        if not path.is_file():
            continue
        try:
            diagnostic_outputs[key] = json.loads(path.read_text())
        except (OSError, ValueError):
            diagnostic_outputs[key] = {"path": str(path), "sha256": sha256(path)}
    return {
        "arm": arm.name,
        "artifact": str(arm.artifact),
        "artifact_file_sha256": arm.cache_sha256,
        "env": dict(sorted(arm.env.items())),
        "diagnostic_env": dict(sorted(diagnostic_env.items())),
        "diagnostic_outputs": diagnostic_outputs,
        "deploy": dep,
        "run": run,
        "v7_stats": stats,
        "v7_counters": counter_summary(stats),
        "loaded_artifact_id8": stats.get("artifact_id8"),
        "run_dir": str(rd.root),
    }, expected_norm


def summarize(rows: list[dict[str, Any]], arms: dict[str, Arm],
              comparisons: list[tuple[str, str]], pairs: int,
              seed: int, *, aa: tuple[str, str] | None = None,
              min_blocks: int = paired_stats.DEFAULT_MIN_BLOCKS) -> dict[str, Any]:
    """Paired summary; ``speedup = baseline / candidate`` per block.

    ``bootstrap_95`` is the same interval as before the shared protocol; the
    added keys are the verdict fields of ``paired_stats.summarize_pair``
    against the ``aa`` noise floor when one was measured.
    """
    by_block = {b: {r["arm"]: r for r in rows if r["block"] == b}
                for b in range(pairs)}

    def speedups(candidate: str, baseline: str) -> list[float]:
        return [by_block[b][baseline]["native_ns"] /
                by_block[b][candidate]["native_ns"] for b in by_block]

    floor = None
    if aa:
        name, reference = aa
        values = speedups(name, reference)
        floor = paired_stats.aa_floor(values, rng=random.Random(seed ^ 0xAA),
                                      min_blocks=min_blocks, rounds=ROUNDS)
        floor.update(arm=name, reference=reference,
                     ratio=f"{reference}/{name}", speedup_per_block=values)
    result = {}
    for index, (candidate, baseline) in enumerate(comparisons):
        ratios = speedups(candidate, baseline)
        paired = paired_stats.summarize_pair(
            ratios, rng=random.Random(seed ^ index), floor=floor,
            min_blocks=min_blocks, rounds=ROUNDS)
        result[f"{candidate}_vs_{baseline}"] = {
            "speedup_per_block": ratios,
            "median_speedup": statistics.median(ratios),
            "bootstrap_95": paired["ci95"],
            **{key: value for key, value in paired.items()
               if key not in ("median", "ci95")},
        }
    native = {name: [r["native_ns"] for r in rows if r["arm"] == name]
              for name in arms}
    return {
        "native_ns": native,
        "median_native_ns": {name: statistics.median(values)
                             for name, values in native.items()},
        "comparisons": result,
        "noise_floor": floor,
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", required=True, type=Path)
    p.add_argument("--qemu", required=True, type=Path)
    p.add_argument("--arm", action="append", default=[], metavar="NAME=ARTIFACT",
                   help="repeat for each measured arm")
    p.add_argument("--env", action="append", default=[], metavar="ARM:KEY=VALUE",
                   help="runtime environment setting for one arm; repeatable")
    p.add_argument("--diagnostic-env", action="append", default=[],
                   metavar="ARM:KEY=VALUE",
                   help="extra setting used only by the diagnostic launch; "
                        "{run_dir} expands to that launch directory")
    p.add_argument("--compare", action="append", default=[],
                   metavar="CANDIDATE:BASELINE",
                   help="paired comparison; default compares every arm with the first")
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--pairs", type=int, default=12)
    p.add_argument("--seed", type=int, default=0xF17E)
    p.add_argument("--timeout", type=float, default=120.0)
    paired_stats.add_protocol_arguments(p)
    p.add_argument("--aa-control", choices=("auto", "none"), default="auto",
                   help="add an A/A copy of the reference arm (default auto)")
    p.add_argument("--aa-reference", metavar="ARM",
                   help="arm copied by the A/A control "
                        "(default: the baseline of the first comparison)")
    args = p.parse_args()
    if args.pairs < 2:
        p.error("--pairs must be at least 2")
    aa = None
    try:
        warning = paired_stats.check_min_blocks(args.pairs, args.min_blocks,
                                                args.allow_few_blocks)
        arms = parse_arms(args.arm, args.env, args.diagnostic_env)
        comparisons = parse_comparisons(args.compare, arms)
        if args.aa_control == "auto":
            aa = add_aa_arm(arms, comparisons, args.aa_reference)
    except ValueError as exc:
        p.error(str(exc))
    if warning:
        print(f"WARNING: {warning}", file=sys.stderr)
    args.out.mkdir(parents=True, exist_ok=False)
    config, qemu = args.config.resolve(), args.qemu.resolve()
    if not config.is_file() or not qemu.is_file():
        p.error("--config and --qemu must be files")
    cache_hashes: dict[Path, str] = {}
    for arm in arms.values():
        cache = (arm.artifact / "cache.sc").resolve()
        if cache not in cache_hashes:
            cache_hashes[cache] = sha256(cache)
        arm.cache_sha256 = cache_hashes[cache]

    expected_norm = None
    diagnostics = []
    for arm in arms.values():
        doc, expected_norm = diagnostic(config, qemu, args.out, arm, args.timeout,
                                        expected_norm)
        diagnostics.append(doc)
    (args.out / "diagnostics.json").write_text(
        json.dumps(diagnostics, indent=2, sort_keys=True) + "\n")

    rng = random.Random(args.seed)
    rows: list[dict[str, Any]] = []
    for block in range(args.pairs):
        order = list(arms)
        rng.shuffle(order)
        for name in order:
            arm = arms[name]
            rd = create_run_dir(config, args.out / f"b{block:02d}-{name}", qemu=qemu,
                                label=f"firmware-inline-ablation-{block}-{name}")
            dep = deploy(arm.artifact, rd, timeout_s=args.timeout)
            run = launch(rd, timeout_s=args.timeout, extra_env=arm.env)
            validate_run(f"block {block}", arm, run, expected_norm)
            rows.append({
                "block": block,
                "order": order,
                "arm": name,
                "env": dict(sorted(arm.env.items())),
                "native_ns": run["bench"][0]["elapsed_ns"],
                "wall_s": run["timings"]["wall_s"],
                "deploy_s": dep["deploy_seconds"],
                "appended": run["appended"],
                "artifact": str(arm.artifact),
                "artifact_file_sha256": arm.cache_sha256,
                "run_dir": str(rd.root),
            })
    with (args.out / "runs.jsonl").open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, sort_keys=True) + "\n")
    timing = summarize(rows, arms, comparisons, args.pairs, args.seed,
                       aa=aa, min_blocks=args.min_blocks)
    summary = {
        "schema": "scopt.firmware-inline-ablation-timing/1",
        "pairs": args.pairs,
        "seed": args.seed,
        "host": {
            "node": platform.node(),
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
            "affinity": sorted(os.sched_getaffinity(0)),
        },
        "qemu": str(qemu),
        "qemu_sha256": sha256(qemu),
        "arms": {name: {"artifact": str(arm.artifact),
                        "artifact_file_sha256": arm.cache_sha256,
                        "env": dict(sorted(arm.env.items()))}
                 for name, arm in arms.items()},
        "diagnostics": str(args.out / "diagnostics.json"),
        "protocol": paired_stats.protocol_record(
            blocks=args.pairs, min_blocks=args.min_blocks,
            allow_few_blocks=args.allow_few_blocks, rounds=ROUNDS, seed=args.seed,
            aa_control=({"arm": aa[0], "reference": aa[1],
                         "artifact_file_sha256": arms[aa[0]].cache_sha256}
                        if aa else None),
            ratio="baseline/candidate native time; above 1 means the candidate is faster",
            warning=warning),
        **timing,
    }
    (args.out / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True))
    floor = timing["noise_floor"]
    if floor:
        print(paired_stats.format_row(f"A/A {floor['ratio']}", floor),
              f"band [{floor['band'][0]:.4f}, {floor['band'][1]:.4f}]",
              file=sys.stderr)
    for label, row in timing["comparisons"].items():
        print(paired_stats.format_row(label, dict(row, median=row["median_speedup"],
                                                  ci95=row["bootstrap_95"])),
              file=sys.stderr)


if __name__ == "__main__":
    main()
