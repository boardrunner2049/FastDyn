#!/usr/bin/env python3
"""Run training or held-out paired timing for the recovered firmware corpus."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ROOT = ROOT / "build/preflight/recovered-firmware-corpus-2026-11-16"
MODES = {
    "crc8": 0,
    "crc8dvbs2": 1,
    "dshotcrc": 2,
    "vListInitialise": 3,
    "vListInitialiseItem": 4,
    "vListInsertEnd": 5,
    "vListInsert": 6,
    "uxListRemove": 7,
    "combined_all": 8,
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--phase", choices=("training", "held-out"), required=True)
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--work-scale", type=int, default=4)
    parser.add_argument("--affinity-cpu", type=int)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.work_scale <= 0:
        raise SystemExit("--work-scale must be positive")
    report = json.loads((root / "report.json").read_text())
    wrapper = root / "bolt-firmware-wrapper"
    reference = root / "corpus-O0"
    stock = root / "corpus-O0.bolt-pgo"
    full = wrapper / "corpus-O0.bolt-wrapper-relocatable"
    compact = wrapper / "corpus-O0.bolt-qualified-compact"
    allowed = sorted(os.sched_getaffinity(0))
    affinity = args.affinity_cpu if args.affinity_cpu is not None else allowed[0]
    if affinity not in allowed:
        raise SystemExit(f"CPU {affinity} is outside affinity {allowed}")

    if args.phase == "training":
        program_seed = 11
        randomization_seed = 20261116
        output = wrapper / "timing"
        selection = None
    else:
        program_seed = 13
        randomization_seed = 20261117
        output = wrapper / "held-out-timing"
        selection = json.loads((wrapper / "policy-selection.json").read_text())
    output.mkdir(parents=True, exist_ok=True)

    for name, mode in MODES.items():
        iterations = int(report["timings"][name]["iterations"]) * args.work_scale
        variants = {
            "reference": reference,
            "stock_bolt": stock,
        }
        if selection is None:
            variants.update({"full_fop": full, "compact_fop": compact})
        else:
            choice = selection["choices"][name]
            variants["relocatable_policy"] = {
                "reference": reference,
                "full_fop": full,
                "compact_fop": compact,
            }[choice["relocatable_policy"]]
            variants["fixed_layout_policy"] = {
                "reference": reference,
                "compact_fop": compact,
            }[choice["fixed_layout_policy"]]
        command = [
            "taskset", "-c", str(affinity), "python3",
            str(ROOT / "scripts/preflight/benchmark-finite-variants.py"),
            "--paired-blocks", "--samples", str(args.samples),
            "--seed", str(randomization_seed),
            f"--program-arg={mode}", f"--program-arg={iterations}",
            f"--program-arg={program_seed}",
        ]
        for variant, path in variants.items():
            command.extend(("--variant", f"{variant}={path}"))
        command.extend(("--output", str(output / f"{name}.json")))
        subprocess.run(command, cwd=ROOT, check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
