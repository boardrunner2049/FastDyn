#!/usr/bin/env python3
"""Compare frozen Thumb forwarding policies under one paired fixed-work run."""

from __future__ import annotations

import argparse
import importlib.util
import json
import random
import shutil
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

from fastdyn.preflight import qualify_firmware
from fastdyn.preflight.model import canonical_json_bytes, hash_file


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_IMAGE = ROOT / "tests/sample_binaries/RTOS/RTOSDemo.axf"
DEFAULT_CONFIG = ROOT / "configs/minimal.toml"
DEFAULT_FASTDYN = ROOT / "fastdyn-env/bin/fastdyn"
DEFAULT_PROFILE = (
    ROOT / "build/preflight/thumb-cortex-m3-bounded-fop-profile-2026-11-16/report.json"
)
DEFAULT_OUTPUT = ROOT / "build/preflight/thumb-cortex-m3-bounded-fop-ablation-2026-11-16"
POLICIES = {
    "tick_adjacent": (0x08000D8A,),
    "tick_bounded": (0x08000D8E, 0x08000D8A),
    "handler": (0x080066FC,),
    "combined": (0x08000D8E, 0x08000D8A, 0x080066FC),
}
MINIMUM_USEFUL_CPU_RATIO = 1.005


def load_script(name: str, path: Path) -> ModuleType:
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def write_json(path: Path, value: Any) -> None:
    path.write_bytes(canonical_json_bytes(value) + b"\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", type=Path, default=DEFAULT_IMAGE)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--fastdyn", type=Path, default=DEFAULT_FASTDYN)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--work-units", type=int, default=100_000)
    parser.add_argument("--affinity-cpu", type=int, default=0)
    parser.add_argument("--seed", type=int, default=20261124)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    if args.samples < 1 or args.work_units < 1:
        raise SystemExit("--samples and --work-units must be positive")
    output = args.output.resolve()
    if output.exists():
        if not args.replace:
            raise SystemExit(f"output exists: {output}")
        shutil.rmtree(output)
    output.mkdir(parents=True)

    hot = load_script(
        "fastdyn_thumb_hot_pilot",
        ROOT / "scripts/preflight/run-thumb-hot-fop-pilot.py",
    )
    image = args.image.resolve()
    profile_path = args.profile.resolve()
    profile = json.loads(profile_path.read_text())
    rows = {
        int(item["reload_address"]): item for item in profile["top_candidates"]
    }
    required = {address for policy in POLICIES.values() for address in policy}
    missing = sorted(required - rows.keys())
    if missing:
        raise RuntimeError(f"frozen profile is missing sites: {missing}")
    qualification = qualify_firmware(
        image, SimpleNamespace(arch="arm", cpu="cortex-m3", init_nsvtor=0x08000000)
    )

    variants: dict[str, Path] = {"reference": image}
    policy_evidence: dict[str, Any] = {}
    for name, addresses in POLICIES.items():
        policy_dir = output / "candidates" / name
        policy_dir.mkdir(parents=True)
        emitted, outcomes, changed = hot.install_sites(
            image, policy_dir, [rows[address] for address in addresses],
            qualification.target,
        )
        variants[name] = emitted
        policy_evidence[name] = {
            "reload_addresses": list(addresses),
            "profile_hits": [int(rows[address]["hit_count"]) for address in addresses],
            "image": str(emitted),
            "image_sha256": hash_file(emitted),
            "changed_file_offsets": changed,
            "stage_ledgers": [item["stage_ledger"] for item in outcomes],
        }

    samples: dict[str, list[dict[str, Any]]] = {name: [] for name in variants}
    generator = random.Random(args.seed)
    sequence = 0
    for block in range(args.samples):
        order = list(variants)
        generator.shuffle(order)
        for name in order:
            sequence += 1
            sample = hot.run_one(
                args.fastdyn.resolve(), args.config.resolve(), variants[name],
                output / "runs" / f"{block:02d}-{name}", args.work_units,
                args.affinity_cpu,
            )
            sample.update({"block": block, "sequence": sequence, "variant": name})
            if (
                sample["returncode"] != 0
                or sample["completed_work_units"] != args.work_units
                or sample["guest_benchmark_ns"] is None
            ):
                raise RuntimeError(f"{name} block {block} failed: {sample}")
            samples[name].append(sample)
            print(
                f"[{sequence}/{args.samples * len(variants)}] {name}: "
                f"guest={sample['guest_benchmark_ns'] / 1e9:.6f}s",
                flush=True,
            )

    ram_mismatches: dict[str, list[int]] = {}
    comparisons: dict[str, Any] = {}
    for offset, name in enumerate(POLICIES):
        mismatches = [
            block for block in range(args.samples)
            if samples["reference"][block]["ram_prefix_sha256"]
            != samples[name][block]["ram_prefix_sha256"]
        ]
        ram_mismatches[name] = mismatches
        comparisons[name] = hot.paired_summary(
            {"reference": samples["reference"], "candidate": samples[name]},
            args.seed + offset * 1000,
        )

    accepted = [
        name for name in POLICIES
        if not ram_mismatches[name]
        and comparisons[name]["cpu_ns"]["bootstrap_95_percent"][0]
        >= MINIMUM_USEFUL_CPU_RATIO
    ]
    deployment = (
        max(accepted, key=lambda name: comparisons[name]["cpu_ns"]["median_ratio"])
        if accepted else "reference"
    )
    report = {
        "schema_version": 1,
        "classification": "frozen_real_cortex_m3_forwarding_policy_ablation",
        "input": {"path": str(image), "sha256": hash_file(image)},
        "profile": {"path": str(profile_path), "sha256": hash_file(profile_path)},
        "method": {
            "samples_per_variant": args.samples,
            "paired_randomized_blocks": True,
            "randomization_seed": args.seed,
            "work_units_per_sample": args.work_units,
            "affinity_cpu": args.affinity_cpu,
            "bootstrap_resamples": 100_000,
            "minimum_useful_cpu_ratio": MINIMUM_USEFUL_CPU_RATIO,
            "selection_frozen_before_timing": True,
        },
        "policies": policy_evidence,
        "samples": samples,
        "paired_comparisons": comparisons,
        "ram_mismatches": ram_mismatches,
        "decision": {
            "accepted_policies": accepted,
            "deployment_selected": deployment,
            "reference_retained": deployment == "reference",
        },
        "claims": {
            "real_firmware_execution": True,
            "whole_firmware_equivalence": False,
            "hardware_cycles": False,
        },
    }
    write_json(output / "report.json", report)
    print(json.dumps({
        "report": str(output / "report.json"),
        "deployment_selected": deployment,
        "cpu": {
            name: comparisons[name]["cpu_ns"] for name in POLICIES
        },
        "ram_mismatches": ram_mismatches,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
