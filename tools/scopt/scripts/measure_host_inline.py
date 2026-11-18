#!/usr/bin/env python3
"""Measure warm baseline, identity, and native inlining on held-out inputs."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import re
import statistics
from typing import Any

from scopt.formats.artifact import read_artifact

ROOT = Path(__file__).resolve().parents[3]
ARM_NAMES = ("warm", "identity", "host-inline")
SPLICE_ARM = "host-inline-splice"
BENCH_TAG = "host-inline"


def parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "First run bootstrap_host_inline.py, publish the inline artifact, and run V10 "
            "against the same held-out inputs. Then run this script with scopt-env/bin/python. "
            "Use an idle machine. One block contains every selected arm on one input, with "
            "arm order randomized using the standard scopt timing harness."
        ),
    )


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def validate_evidence(
    evidence: Path,
    artifact_dir: Path,
    snapshot_dir: Path,
    config: Path,
    inputs: list[Path],
) -> tuple[str, Path]:
    report = read_json(evidence)
    artifact = read_artifact(artifact_dir)
    if (
        report.get("schema") != "scopt.validate_dynamic"
        or report.get("verdict") != "pass"
        or report.get("failures") != []
        or report.get("artifact_id") != artifact.artifact_id
        or Path(report["artifact_dir"]).resolve() != artifact_dir
        or Path(report["baseline"]).resolve() != snapshot_dir
        or [Path(p).resolve() for p in report["configs"]] != [config]
        or [Path(p).resolve() for p in report["inputs"]] != inputs
        or report.get("input_bank") != "input"
    ):
        raise ValueError(f"{evidence} does not qualify these arms and held-out inputs")
    checked_inputs = set()
    for row in report["runs"]:
        if any(
            row[arm].get("verdict") != "PASS"
            or row[arm].get("exit_code") != 0
            or row[arm].get("appended") != 0
            for arm in ("artifact", "baseline")
        ):
            raise ValueError(f"{evidence} includes an unsuccessful or cold run")
        checked_inputs.add(Path(row["input"]).resolve())
    if checked_inputs != set(inputs):
        raise ValueError(f"{evidence} did not run every held-out input")
    return artifact.artifact_id, Path(report["qemu"]).resolve()


def verify_run(
    run: dict[str, Any],
    expected: int,
    qemu_sha256: str,
    input_digest: str,
) -> int:
    if (
        run.get("verdict") != "PASS"
        or (run.get("proof") or {}).get("verdict") != "PASS"
        or run.get("appended") != 0
        or run.get("exit_code") != 0
        or run.get("timed_out")
        or run.get("cache_invalidated_by_frontend")
        or run.get("stray_capture_files")
        or run.get("qemu_sha256") != qemu_sha256
        or run["inputs"]["input"]["sha256"] != input_digest
    ):
        raise ValueError(f"{run['stdout_log']} did not prove the expected warm run")
    registers = []
    for line in run["guest_norm"]:
        match = re.fullmatch(r"Reg 0:0x([0-9a-fA-F]+)", line)
        if match:
            registers.append(int(match[1], 16))
    if registers != [expected]:
        raise ValueError(f"{run['stdout_log']} did not match oracle {expected:#x}")
    bench = run["bench"]
    if len(bench) != 1 or bench[0]["tag"] != BENCH_TAG or bench[0]["elapsed_ns"] <= 0:
        raise ValueError(f"{run['stdout_log']} has an invalid benchmark sample")
    return bench[0]["elapsed_ns"]


def variability(values: list[float]) -> dict[str, float]:
    median = statistics.median(values)
    return {
        "median": median,
        "mad": statistics.median(abs(value - median) for value in values),
        "min": min(values),
        "max": max(values),
    }


def summarize(
    blocks: list[dict[str, Any]], arms: tuple[str, ...] = ARM_NAMES,
) -> dict[str, Any]:
    comparisons = [("host-inline", "warm"), ("host-inline", "identity")]
    if SPLICE_ARM in arms:
        comparisons.extend(((SPLICE_ARM, "warm"), (SPLICE_ARM, "host-inline")))

    def group(input_blocks: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "blocks": len(input_blocks),
            "arms": {
                arm: {
                    "warm_ns": variability([b["measurements"][arm]["warm_ns"]
                                            for b in input_blocks]),
                    "deploy_s": variability([b["measurements"][arm]["deploy_s"]
                                             for b in input_blocks]),
                    "launch_wall_s": variability([b["measurements"][arm]["launch_wall_s"]
                                                  for b in input_blocks]),
                }
                for arm in arms
            },
            "paired_elapsed_change_pct": {
                f"{arm}_vs_{reference}": variability([
                    100 * (b["measurements"][arm]["warm_ns"]
                           / b["measurements"][reference]["warm_ns"] - 1)
                    for b in input_blocks
                ])
                for arm, reference in comparisons
            },
        }

    return {
        "all": group(blocks),
        "by_input": {
            str(index): group([b for b in blocks if b["input"] == index])
            for index in sorted({b["input"] for b in blocks})
        },
    }


def measure(args: argparse.Namespace) -> dict[str, Any]:
    # These settings also affect the FastDyn subprocesses created by scopt.
    os.environ["FASTDYN_ROOT"] = str(ROOT)
    os.environ["SCOPT_FASTDYN_SRC"] = str(ROOT)
    from scopt.run.arms import campaign_from_args, run_campaign
    from scopt.run.launcher import file_sha256, write_json
    from scopt.run.toml_rewrite import normalized_config_sha256

    baseline = args.baseline.resolve()
    metadata = read_json(baseline / "baseline.json")
    config = (baseline / "host-inline.toml").resolve()
    snapshot = Path(metadata["snapshot"]).resolve()
    identity = Path(metadata["artifact"]).resolve()
    cases = metadata["oracle"]["heldout"]
    inputs = [(baseline / f"heldout-{i}.bin").resolve() for i in range(len(cases))]
    if not inputs:
        raise ValueError("bootstrap evidence has no held-out inputs")
    for path, case in zip(inputs, cases, strict=True):
        if path.read_bytes() != int(case["seed"], 16).to_bytes(4, "little"):
            raise ValueError(f"{path} differs from the bootstrap oracle seed")
    input_digests = [file_sha256(path) for path in inputs]
    identity_id, baseline_qemu = validate_evidence(
        Path(metadata["dynamic"]), identity, snapshot, config, inputs,
    )
    optimized = [(ARM_NAMES[2], args.inline_artifact.resolve(), args.inline_v10.resolve())]
    if args.splice_artifact is not None:
        optimized.append((SPLICE_ARM, args.splice_artifact.resolve(), args.splice_v10.resolve()))
    ids = {"identity": identity_id}
    v10 = {"identity": metadata["dynamic"]}
    for arm, artifact_path, validation_path in optimized:
        ids[arm], arm_qemu = validate_evidence(validation_path, artifact_path, snapshot, config, inputs)
        v10[arm] = str(validation_path)
        if arm_qemu != baseline_qemu:
            raise ValueError(f"{arm} V10 used a different QEMU")
    if len(set(ids.values())) != len(ids):
        raise ValueError("the optimized and identity artifact IDs must differ")
    qemu = baseline_qemu
    qemu_sha256 = "sha256:" + metadata["qemu_sha256"]
    if file_sha256(qemu) != qemu_sha256:
        raise ValueError("timing requires the same source-locked QEMU as V10")

    # The timing harness preflight expects explicit file names for its input
    # bank. Bootstrap's prepared TOML already has them. Each run rewrites all
    # path keys to a private directory, so these are just seed paths.
    timing_config = (baseline / "prep" / "run.toml").resolve()
    if normalized_config_sha256(timing_config.read_text()) != normalized_config_sha256(
        config.read_text()
    ):
        raise ValueError("the prepared TOML differs from the V10 workload")

    sources = {"warm": snapshot, "identity": identity,
               **{arm: path for arm, path, _ in optimized}}
    arms = tuple(sources)
    out = args.out.resolve()
    provenance = {
        "schema": "scopt.host_inline_performance/1",
        "timed_region": "FastDyn host-inline BENCH interval inside warm execution",
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "cpu_affinity": sorted(os.sched_getaffinity(0)),
        },
        "seed": args.seed,
        "repetitions_per_input_per_arm": args.repetitions,
        "qemu": str(qemu),
        "qemu_sha256": qemu_sha256,
        "input_sha256": input_digests,
        "expected": [case["expected"] for case in cases],
        "identity_artifact_id": ids["identity"],
        "inline_artifact_id": ids["host-inline"],
        "v10": v10,
        "config": str(timing_config),
        "v10_config": str(config),
        "sources": {key: str(path) for key, path in sources.items()},
    }
    if SPLICE_ARM in ids:
        provenance["splice_artifact_id"] = ids[SPLICE_ARM]
    campaign = campaign_from_args(
        base_config=str(timing_config),
        arms=arms,
        out_name=BENCH_TAG,
        artifacts={arm: str(sources[arm]) for arm in arms if arm != "warm"},
        warm=str(snapshot),
        reps=args.repetitions,
        seed=args.seed,
        split="heldout",
        inputs=[str(path) for path in inputs],
        qemu=str(qemu),
        timeout_s=args.timeout,
    )
    evidence = run_campaign(campaign, out, log=lambda line: print(line, flush=True))
    expected_count = args.repetitions * len(inputs) * len(arms)
    if (evidence.get("status") != "done" or evidence.get("freeze_ok") is not True
            or evidence.get("n_valid") != expected_count
            or evidence.get("n_runs") != expected_count):
        raise ValueError(f"{out / 'campaign.json'} did not qualify every paired sample")

    blocks: dict[tuple[int, int], dict[str, Any]] = {}
    for line in (out / "runs.jsonl").read_text().splitlines():
        row = json.loads(line)
        rep, index, arm = row["rep"], row["input"], row["arm"]
        run = read_json(Path(row["run_json"]))
        warm_ns = verify_run(run, int(cases[index]["expected"], 16),
                             qemu_sha256, input_digests[index])
        block = blocks.setdefault((rep, index), {
            "repetition": rep, "input": index, "arms": [], "measurements": {},
        })
        block["arms"].append(arm)
        block["measurements"][arm] = {
            "warm_ns": warm_ns,
            "deploy_s": row["deploy_seconds"],
            "launch_wall_s": run["timings"]["wall_s"],
            "run_json": row["run_json"],
        }
    if len(blocks) != args.repetitions * len(inputs) or any(
        sorted(block["arms"]) != sorted(arms) for block in blocks.values()
    ):
        raise ValueError("timing did not produce complete, unique paired blocks")
    completed = list(blocks.values())

    result = {**provenance, "blocks": completed, "summary": summarize(completed, arms)}
    write_json(out / "performance.json", result)
    return result


def main() -> None:
    cli = parser()
    cli.add_argument("--baseline", type=Path, required=True, help="bootstrap output directory")
    cli.add_argument("--inline-artifact", type=Path, required=True, help="published host-inline artifact")
    cli.add_argument("--inline-v10", type=Path, required=True, help="passing inline dynamic.json")
    cli.add_argument("--splice-artifact", type=Path, help="optional fourth arm, a published splice artifact")
    cli.add_argument("--splice-v10", type=Path, help="passing splice dynamic.json")
    cli.add_argument("--out", type=Path, required=True, help="fresh evidence directory")
    cli.add_argument("--repetitions", type=int, default=6, help="blocks per input (at least 5)")
    cli.add_argument("--seed", type=int, default=20261118)
    cli.add_argument("--timeout", type=float, default=60.0)
    args = cli.parse_args()
    if (args.splice_artifact is None) != (args.splice_v10 is None):
        cli.error("--splice-artifact and --splice-v10 must be given together")
    if args.repetitions < 5 or args.timeout <= 0:
        cli.error("repetitions must be at least five and timeout must be positive")
    try:
        result = measure(args)
    except (OSError, ValueError, RuntimeError) as exc:
        cli.exit(1, f"measurement failed: {exc}\n")
    print(json.dumps(result["summary"], indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
