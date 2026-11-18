#!/usr/bin/env python3
"""Build and exercise the native FastDyn BOLT patch-contract pass."""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
from pathlib import Path
from typing import Sequence


def _run(command: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=check, capture_output=True, text=True)


def _symbol_address(binary: Path, name: str) -> int:
    result = _run(["nm", "-n", str(binary)])
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) == 3 and fields[2] == name:
            return int(fields[0], 16)
    raise RuntimeError(f"symbol {name!r} is missing from {binary}")


def _binding(address: int) -> dict[str, object]:
    return {
        "schema_version": 1,
        "bindings": [
            {
                "binding_id": "smoke-patch-target",
                "original_identity": f"smoke:pc:{address:#x}",
                "original_pc": address,
                "current_pc": address,
                "isa": "x86_64",
                "mode": "long",
                "trigger_phase": "instruction_entry",
                "kind": "modifier",
                "action": {"expression": "rax <- rax"},
                "inputs": ["architectural_state"],
                "outputs": ["architectural_state"],
                "multiplicity": "each_execution",
                "placement": "exact_anchor",
                "provenance": "identity",
                "clone_policy": "forbidden",
            }
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Run a positive guard/rewrite case and a rejected-anchor case."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--bolt", required=True, type=Path)
    parser.add_argument("--work-dir", required=True, type=Path)
    args = parser.parse_args(argv)

    repository = Path(__file__).resolve().parents[2]
    source = repository / "tests/preflight/bolt_guard_fixture.c"
    args.work_dir.mkdir(parents=True, exist_ok=True)
    original = args.work_dir / "bolt-guard-fixture"
    rewritten = args.work_dir / "bolt-guard-fixture.bolt"
    contracts = args.work_dir / "patch-bindings.json"
    invalid_contracts = args.work_dir / "patch-bindings-invalid.json"
    incompatible_contracts = args.work_dir / "patch-bindings-incompatible.json"
    stoke_csv = args.work_dir / "stoke-info.csv"
    firmware_report = args.work_dir / "post-optimization-firmware-report.json"

    _run(
        [
            "cc",
            "-O2",
            "-g",
            "-fno-pie",
            "-no-pie",
            "-Wl,--emit-relocs",
            str(source),
            "-o",
            str(original),
        ]
    )
    address = _symbol_address(original, "patch_target")
    document = _binding(address)
    contracts.write_text(json.dumps(document, sort_keys=True) + "\n", encoding="utf-8")

    accepted = _run(
        [
            str(args.bolt.resolve()),
            str(original),
            "-o",
            str(rewritten),
            f"--fastdyn-patch-contracts={contracts}",
            f"--fastdyn-firmware-report={firmware_report}",
            "--frame-opt=all",
        ]
    )
    _run([str(original)])
    _run([str(rewritten)])
    if "FastDyn validated 1 binding(s)" not in accepted.stdout:
        raise RuntimeError("FastDyn BOLT pass did not report the accepted binding")
    if "FastDyn wrote post-optimization firmware report" not in accepted.stdout:
        raise RuntimeError("FastDyn BOLT pass did not run the post-optimization reporter")
    report = json.loads(firmware_report.read_text(encoding="utf-8"))
    if report.get("phase") != "post-frame-optimizer-and-alloc-combiner":
        raise RuntimeError("FastDyn firmware report was emitted at the wrong phase")
    reported_targets = [
        item for item in report.get("functions", [])
        if item.get("name") == "patch_target"
    ]
    if len(reported_targets) != 1 or not reported_targets[0].get("ignored"):
        raise RuntimeError("post-optimization report lost the protected target")

    _run(
        [
            str(args.bolt.resolve()),
            str(original),
            "-o",
            str(args.work_dir / "stoke-info.bolt"),
            "--lite=0",
            "--stoke",
            f"--stoke-out={stoke_csv}",
        ]
    )
    with stoke_csv.open(newline="", encoding="utf-8") as stream:
        stoke_rows = list(csv.DictReader(stream))
    target_rows = [row for row in stoke_rows if row["FuncName"] == "patch_target"]
    if len(target_rows) != 1:
        raise RuntimeError("BOLT StokeInfo did not export patch_target exactly once")

    invalid = _binding(address)
    invalid["bindings"][0]["current_pc"] = address + 0x100000  # type: ignore[index]
    invalid["bindings"][0]["original_pc"] = address + 0x100000  # type: ignore[index]
    invalid_contracts.write_text(
        json.dumps(invalid, sort_keys=True) + "\n", encoding="utf-8"
    )
    rejected = _run(
        [
            str(args.bolt.resolve()),
            str(original),
            "-o",
            str(args.work_dir / "should-not-exist"),
            f"--fastdyn-patch-contracts={invalid_contracts}",
        ],
        check=False,
    )
    if rejected.returncode == 0 or "has no instruction" not in rejected.stderr:
        raise RuntimeError("FastDyn BOLT pass did not reject the incompatible binding")

    incompatible = _binding(address)
    incompatible["bindings"][0]["clone_policy"] = "replicate_each_clone"  # type: ignore[index]
    incompatible_contracts.write_text(
        json.dumps(incompatible, sort_keys=True) + "\n", encoding="utf-8"
    )
    policy_rejected = _run(
        [
            str(args.bolt.resolve()),
            str(original),
            "-o",
            str(args.work_dir / "should-not-exist-policy"),
            f"--fastdyn-patch-contracts={incompatible_contracts}",
        ],
        check=False,
    )
    if (
        policy_rejected.returncode == 0
        or "outside the conservative identity-anchor policy" not in policy_rejected.stderr
    ):
        raise RuntimeError("FastDyn BOLT pass accepted an unqualified clone policy")

    print(
        json.dumps(
            {
                "accepted": True,
                "address": address,
                "original_exit": 0,
                "rewritten_exit": 0,
                "invalid_binding_rejected": True,
                "unqualified_clone_policy_rejected": True,
                "stoke_exported_functions": len(stoke_rows),
                "stoke_exported_patch_target": True,
                "post_optimization_reported": True,
                "post_optimization_function_count": report["summary"]["function_count"],
                "protected_target_reported_ignored": True,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
