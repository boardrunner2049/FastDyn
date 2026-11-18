#!/usr/bin/env python3
"""Regenerate the bounded real-firmware TCG inventory used by the report."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path
from types import ModuleType

from fastdyn.preflight import evaluate_generated_host_inventory


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REFERENCE = ROOT / "tests/sample_binaries/RTOS/RTOSDemo.axf"
DEFAULT_CANDIDATE = (
    ROOT / "build/preflight/thumb-cortex-m3-hot-fop-2026-11-16/"
    "RTOSDemo.hot-thumb-fop.axf"
)
DEFAULT_CONFIG = ROOT / "configs/minimal.toml"
DEFAULT_FASTDYN = ROOT / "fastdyn-env/bin/fastdyn"
DEFAULT_OUTPUT = (
    ROOT / "build/preflight/thumb-cortex-m3-hot-fop-2026-11-16/tcg-diagnostic"
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_script(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_variant(
    hot_pilot: ModuleType,
    *,
    name: str,
    image: Path,
    config_source: Path,
    fastdyn: Path,
    output: Path,
    work_units: int,
) -> dict[str, object]:
    run_dir = output / name
    work_dir = run_dir / "fastdyn"
    run_dir.mkdir(parents=True)
    ram = run_dir / "ram0.ram"
    config = run_dir / "run.toml"
    config_text = hot_pilot.config_text(
        config_source, image, ram, run_dir / "qmp.sock", work_units
    )
    config_text = config_text.replace(
        'log_options = "none"',
        'log_options = "in_asm,op_opt,out_asm"',
        1,
    )
    if 'log_options = "in_asm,op_opt,out_asm"' not in config_text:
        raise RuntimeError("failed to enable the frozen QEMU diagnostic log options")
    config.write_text(config_text, encoding="utf-8")
    command = [
        str(fastdyn), "-q", "run", "-c", str(config), "-o", str(work_dir)
    ]
    process = subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True, timeout=120
    )
    (run_dir / "stdout.txt").write_text(process.stdout, encoding="utf-8")
    (run_dir / "stderr.txt").write_text(process.stderr, encoding="utf-8")
    qemu_log = work_dir / "qemu.log"
    if process.returncode != 0 or not qemu_log.is_file():
        raise RuntimeError(
            f"{name} diagnostic failed with {process.returncode}; see {run_dir}"
        )
    ram_record: dict[str, object] | None = None
    if ram.is_file():
        ram_record = {
            "bytes": ram.stat().st_size,
            "sha256": sha256(ram),
            "retained": False,
        }
        ram.unlink()
    return {
        "command": command,
        "returncode": process.returncode,
        "image": str(image),
        "image_sha256": sha256(image),
        "config": str(config),
        "qemu_log": str(qemu_log),
        "qemu_log_sha256": sha256(qemu_log),
        "ram": ram_record,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference", type=Path, default=DEFAULT_REFERENCE)
    parser.add_argument("--candidate", type=Path, default=DEFAULT_CANDIDATE)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--fastdyn", type=Path, default=DEFAULT_FASTDYN)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--address", type=lambda value: int(value, 0), default=0x080066F8
    )
    parser.add_argument("--work-units", type=int, default=1)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()

    output = args.output.resolve()
    if output.exists():
        if not args.replace:
            raise SystemExit(f"output exists: {output}")
        shutil.rmtree(output)
    output.mkdir(parents=True)

    hot_pilot = load_script(
        "fastdyn_thumb_hot_pilot",
        ROOT / "scripts/preflight/run-thumb-hot-fop-pilot.py",
    )
    runs = {
        name: run_variant(
            hot_pilot,
            name=name,
            image=image.resolve(),
            config_source=args.config.resolve(),
            fastdyn=args.fastdyn.resolve(),
            output=output,
            work_units=args.work_units,
        )
        for name, image in (
            ("reference", args.reference),
            ("candidate", args.candidate),
        )
    }
    analyzer = ROOT / "scripts/preflight/analyze-thumb-tcg.py"
    command = [
        "python3",
        str(analyzer),
        str(runs["reference"]["qemu_log"]),
        str(runs["candidate"]["qemu_log"]),
        "--address",
        hex(args.address),
        "--output",
        str(output / "report.json"),
    ]
    subprocess.run(command, cwd=ROOT, check=True)
    inventory = json.loads((output / "report.json").read_text())
    cost_decision = evaluate_generated_host_inventory(inventory)
    (output / "cost-decision.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": cost_decision.status,
                "reason_code": cost_decision.reason_code,
                "evidence": dict(cost_decision.evidence),
            },
            sort_keys=True,
            separators=(",", ":"),
        ) + "\n",
        encoding="utf-8",
    )
    manifest = {
        "schema_version": 1,
        "classification": "reproducible_static_tcg_diagnostic",
        "work_units_per_variant": args.work_units,
        "guest_address": args.address,
        "runs": runs,
        "analyzer_command": command,
        "limits": {
            "dynamic_hardware_counts": False,
            "full_harness_profitability": False,
            "board_timing_equivalence": False,
        },
    }
    (output / "generation.json").write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    print(output / "report.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
