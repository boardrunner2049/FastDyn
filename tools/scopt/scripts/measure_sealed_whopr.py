#!/usr/bin/env python3
"""Measure compiled trusted-sealed launchers in randomized paired blocks.

The launchers are prepared before this script starts, so cache production,
deployment, validation, and Python frontend time are outside the firmware
``[BENCH]`` interval.  Each scored observation is a fresh QEMU process pinned
to one CPU.  Cache hashes and normalized register output are checked before a
result is published.

An automatic A/A control (``--aa-control auto``) clones the baseline launcher
and its cache byte for byte under another name and times it in the same
shuffled blocks.  Its paired ratio is the harness noise floor, and the primary
``comparison`` is the paired block-ratio median with a seeded bootstrap
interval, an exact sign test and a verdict against that floor
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
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

_SCOPT = Path(__file__).resolve().parents[1]
_REPO = Path(__file__).resolve().parents[3]
if str(_SCOPT) not in sys.path:
    sys.path.insert(0, str(_SCOPT))

from scopt.run import paired_stats  # noqa: E402


BENCH = re.compile(r"^\[BENCH\]\[fw\].*\belapsed_ns=(\d+)\b", re.MULTILINE)
REG = re.compile(r"^Reg ([0-3]):(0x[0-9a-fA-F]{8})$", re.MULTILINE)


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()


def cache_path(launcher: Path) -> Path:
    doc = json.loads((launcher.parent / "launch.json").read_text())
    for index, arg in enumerate(doc["argv"]):
        if arg == "-accel" and index + 1 < len(doc["argv"]):
            for item in doc["argv"][index + 1].split(","):
                if item.startswith("supercache="):
                    return Path(item.split("=", 1)[1]).resolve()
    raise ValueError(f"{launcher}: launch.json has no SuperCache path")


def interval(values: list[float], seed: int, rounds: int = 20_000) -> list[float]:
    return paired_stats.bootstrap_median_ci(values, random.Random(seed), rounds)


def _emit_bundle():
    try:
        from fastdyn.compiled_launch import emit_bundle
    except ImportError:
        sys.path.insert(0, str(_REPO / "src"))
        from fastdyn.compiled_launch import emit_bundle
    return emit_bundle


def clone_launcher(launcher: Path, dest: Path) -> Path:
    """Copy a compiled launcher bundle and its cache to ``dest``; return run.sh.

    Paths under the source bundle directory (RAM files, QMP socket, timing
    file, cache) are rebased to ``dest``; everything else (QEMU, ELF, plugin,
    configuration) stays shared.  The cache copy keeps holes and must hash
    equal to the source cache.
    """
    from scopt.formats.sc_records import sparse_copy

    source = launcher.resolve(strict=True).parent
    dest = dest.resolve()
    if dest == source or source in dest.parents:
        raise ValueError(f"A/A clone {dest} must lie outside {source}")
    doc = json.loads((source / "launch.json").read_text())
    cache = cache_path(launcher)
    try:
        relative = cache.relative_to(source)
    except ValueError:
        relative = Path("cache.sc")

    def ignore(directory: str, names: list[str]) -> set[str]:
        skipped = set()
        for name in names:
            path = Path(directory) / name
            if name.endswith(".log"):
                skipped.add(name)
            elif path.is_symlink():
                continue
            elif path.resolve() == cache or not (path.is_file() or path.is_dir()):
                skipped.add(name)
        return skipped

    shutil.copytree(source, dest, symlinks=True, ignore=ignore)
    new_cache = dest / relative
    new_cache.parent.mkdir(parents=True, exist_ok=True)
    sparse_copy(cache, new_cache)
    spellings = {str(source), str(launcher.parent)}

    def rebase(value: str) -> str:
        for old in spellings:
            value = value.replace(old + "/", str(dest) + "/")
        return value

    argv = [rebase(arg) for arg in doc["argv"]]
    for index, arg in enumerate(argv[:-1]):
        if arg == "-accel":
            argv[index + 1] = ",".join(
                f"supercache={new_cache}" if item.startswith("supercache=") else item
                for item in argv[index + 1].split(","))
    cwd = Path(doc["cwd"])
    if cwd == source or source in cwd.parents:
        cwd = dest / cwd.relative_to(source)
    _emit_bundle()(
        dest, argv, cwd,
        {key: rebase(value) for key, value in doc["env_set"].items()},
        [(rebase(row["path"]), row["size"]) for row in doc.get("reset_files", [])],
        metadata={**doc.get("metadata", {}), "aa_clone_of": str(launcher)},
        allow_existing=True,
        disable_aslr=bool(doc.get("disable_aslr", False)),
    )
    if sha256(new_cache) != sha256(cache):
        raise RuntimeError(f"A/A cache copy {new_cache} differs from {cache}")
    return dest / "run.sh"


def run(launcher: Path, cpu: int, log: Path) -> dict[str, Any]:
    env = dict(os.environ)
    env["QEMU_SC_SEALED"] = "1"
    started = time.monotonic_ns()
    completed = subprocess.run(
        ["taskset", "-c", str(cpu), str(launcher)],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env=env, check=False,
    )
    wall_ns = time.monotonic_ns() - started
    log.write_text(completed.stdout)
    benches = BENCH.findall(completed.stdout)
    registers = [value.lower() for _, value in REG.findall(completed.stdout)]
    if completed.returncode or len(benches) != 1 or len(registers) != 16:
        raise RuntimeError(f"invalid launch {launcher}; see {log}")
    return {"native_ns": int(benches[0]), "wall_ns": wall_ns,
            "registers": registers, "log": str(log)}


def summarize(rows: list[dict[str, Any]], pairs: int, seed: int, *,
              min_blocks: int = paired_stats.DEFAULT_MIN_BLOCKS) -> dict[str, Any]:
    """Timing keys of results.json from scored rows (blocks numbered from 1).

    The legacy keys keep their meaning: ``paired_speedups`` are
    baseline/whopr7 per block and ``bootstrap_95`` is ``interval(ratios, seed
    ^ 0x37)``, which ``comparison.ci95`` repeats.  A ``baseline_aa`` arm gives
    the ``noise_floor``.
    """
    by_block = {block: {row["arm"]: row for row in rows if row["block"] == block}
                for block in range(1, pairs + 1)}
    arms = list(dict.fromkeys(row["arm"] for row in rows))
    ratios = [row["baseline"]["native_ns"] / row["whopr7"]["native_ns"]
              for row in by_block.values()]
    floor = None
    if "baseline_aa" in arms:
        aa_ratios = [row["baseline"]["native_ns"] / row["baseline_aa"]["native_ns"]
                     for row in by_block.values()]
        floor = paired_stats.aa_floor(aa_ratios, rng=random.Random(seed ^ 0xAA),
                                      min_blocks=min_blocks)
        floor.update(ratio="baseline/baseline_aa", blocks=aa_ratios)
    comparison = paired_stats.summarize_pair(
        ratios, rng=random.Random(seed ^ 0x37), floor=floor, min_blocks=min_blocks)
    return {
        "median_native_ns": {name: statistics.median(
            row["native_ns"] for row in rows if row["arm"] == name) for name in arms},
        "paired_speedups": ratios,
        "median_paired_speedup": statistics.median(ratios),
        "bootstrap_95": interval(ratios, seed ^ 0x37),
        "noise_floor": floor,
        "comparison": {"ratio": "baseline/whopr7", **comparison},
    }


def parse_args(argv: list[str] | None = None) -> tuple[argparse.Namespace, str | None]:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True,
                        help="compiled baseline run.sh")
    parser.add_argument("--candidate", type=Path, required=True,
                        help="compiled WHOPR-7 run.sh")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--pairs", type=int, default=paired_stats.DEFAULT_MIN_BLOCKS)
    parser.add_argument("--cpu", type=int, default=2)
    parser.add_argument("--seed", type=int, default=0x57484F50)
    paired_stats.add_protocol_arguments(parser)
    parser.add_argument("--aa-control", default="auto", metavar="auto|none|RUN_SH",
                        help="A/A control: clone the baseline (auto, default), "
                             "none, or a prepared byte-identical launcher")
    parser.add_argument("--warmup", type=int, default=1,
                        help="unscored launches of every arm before block 1")
    args = parser.parse_args(argv)
    if args.pairs < 2:
        parser.error("--pairs must be at least two")
    if args.warmup < 0:
        parser.error("--warmup must not be negative")
    try:
        warning = paired_stats.check_min_blocks(args.pairs, args.min_blocks,
                                                args.allow_few_blocks)
    except ValueError as exc:
        parser.error(str(exc))
    if warning:
        print(f"WARNING: {warning}", file=sys.stderr)
    return args, warning


def main(argv: list[str] | None = None) -> None:
    args, warning = parse_args(argv)
    launchers = {"baseline": args.baseline.resolve(strict=True),
                 "whopr7": args.candidate.resolve(strict=True)}
    args.out.mkdir(parents=True, exist_ok=False)
    if args.aa_control == "auto":
        launchers["baseline_aa"] = clone_launcher(launchers["baseline"],
                                                  args.out / "aa-control")
    elif args.aa_control != "none":
        launchers["baseline_aa"] = Path(args.aa_control).resolve(strict=True)
    caches = {name: cache_path(path) for name, path in launchers.items()}
    before = {name: sha256(path) for name, path in caches.items()}
    aa_control = None
    if "baseline_aa" in launchers:
        if (caches["baseline_aa"] == caches["baseline"] or
                before["baseline_aa"] != before["baseline"]):
            raise RuntimeError("the A/A control needs its own byte-identical "
                               "copy of the baseline cache")
        aa_control = {"launcher": str(launchers["baseline_aa"]),
                      "cache_sha256": before["baseline_aa"],
                      "clone_of": str(launchers["baseline"]),
                      "mode": "auto" if args.aa_control == "auto" else "supplied"}
    rng = random.Random(args.seed)
    rows: list[dict[str, Any]] = []
    expected: list[str] | None = None
    warmup: list[dict[str, Any]] = []
    for index in range(args.warmup):
        for name in launchers:
            observed = run(launchers[name], args.cpu,
                           args.out / f"warmup-{index + 1:02d}-{name}.log")
            if expected is None:
                expected = observed["registers"]
            elif observed["registers"] != expected:
                raise RuntimeError(f"warmup {index + 1} {name}: guest output changed")
            warmup.append({"warmup": index + 1, "arm": name, **observed})
    for block in range(args.pairs):
        order = list(launchers)
        rng.shuffle(order)
        for sequence, name in enumerate(order):
            observed = run(launchers[name], args.cpu,
                           args.out / f"block-{block + 1:02d}-{sequence}-{name}.log")
            if expected is None:
                expected = observed["registers"]
            elif observed["registers"] != expected:
                raise RuntimeError(f"block {block + 1} {name}: guest output changed")
            rows.append({"block": block + 1, "sequence": sequence,
                         "order": order, "arm": name, **observed})
    after = {name: sha256(path) for name, path in caches.items()}
    if before != after:
        raise RuntimeError("a measured cache changed")
    timing = summarize(rows, args.pairs, args.seed, min_blocks=args.min_blocks)
    floor, comparison = timing["noise_floor"], timing["comparison"]
    result = {
        "schema": "scopt.sealed-whopr-timing/1",
        "pairs": args.pairs,
        "seed": args.seed,
        "cpu": args.cpu,
        "host": {"node": platform.node(), "platform": platform.platform(),
                 "affinity": sorted(os.sched_getaffinity(0))},
        "launchers": {name: str(path) for name, path in launchers.items()},
        "caches": {name: {"path": str(caches[name]), "sha256": before[name]}
                   for name in launchers},
        "registers": expected,
        "runs": rows,
        **timing,
        "aa_control": aa_control,
        "warmup": warmup,
        "protocol": paired_stats.protocol_record(
            blocks=args.pairs, min_blocks=args.min_blocks,
            allow_few_blocks=args.allow_few_blocks,
            rounds=paired_stats.DEFAULT_ROUNDS, seed=args.seed,
            aa_control=aa_control,
            ratio="baseline/candidate native time; below 1 means the candidate is slower",
            warning=warning),
    }
    (args.out / "results.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    if floor:
        print(paired_stats.format_row("A/A baseline/baseline_aa", floor),
              f"band [{floor['band'][0]:.4f}, {floor['band'][1]:.4f}]",
              file=sys.stderr)
    print(paired_stats.format_row("baseline/whopr7", comparison), file=sys.stderr)


if __name__ == "__main__":
    main()
