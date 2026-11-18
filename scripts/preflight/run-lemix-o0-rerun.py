#!/usr/bin/env python3
"""Run the bounded 2026-11-16 LEMIX O0 BOLT rerun.

The historical experiment is read-only.  Every output is written below a new
run directory.  STOKE funnel stages are orchestrated by a companion script and
are summarized into the same run directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import random
import re
import resource
import shutil
import shlex
import signal
import statistics
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from elftools.elf.elffile import ELFFile


ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / (
    "third_party/preflight/lemix/artifact/Lemix/Ported_Applications/dataset/"
    "FreeRTOS_Apps/FreeRTOS_Apps_Ported/SmartSpeaker_Ported/SmartSpeaker_Ported"
)
REFERENCE = APP / "build/SmartSpeaker"
PROFILE = APP / "build/smartspeaker-training.fdata"
BOLT = ROOT / "third_party/preflight/llvm-build/bin/llvm-bolt"
LLVM_SOURCE = ROOT / "third_party/preflight/llvm-project"
STOKE_SOURCE = ROOT / "third_party/preflight/stoke"
ARCHIVE = ROOT / "third_party/preflight/lemix/Lemix.zip"
PRELOAD = ROOT / "build/preflight/lemix-smartspeaker/lemix_mmio_map.so"
BUILD_LOG = APP / "make-o0.log"
DEFAULT_RUN = ROOT / "build/preflight/lemix-o0-rerun-2026-11-16"
EVENT = b"[JS:Debug] Interrupt_Handler_Task running"

BOLT_REVISION = "7864c0736a8a09670dfbfc513f1e9649799f2486"
STOKE_REVISION = "98d8a0f028f2daf2052bfe607dbc32ec8d55ba9e"

COMMON_ARGS = [
    "--lite=false",
    "--icf=none",
    "--no-inline",
    "--shorten-instructions=true",
    "--eliminate-unreachable=true",
    "--tail-duplication=none",
    "--peepholes=none",
    "--dyno-stats",
    "--time-opts",
]
LAYOUT_OFF = [
    "--reorder-blocks=none",
    "--reorder-functions=none",
    "--split-functions=false",
    "--split-all-cold=false",
]
LAYOUT_ON = [
    "--reorder-blocks=ext-tsp",
    "--reorder-functions=cdsort",
    "--split-functions",
    "--split-all-cold",
]
ARMS = {
    "L0": {"layout": False, "frame_opt": "none"},
    "L1": {"layout": True, "frame_opt": "none"},
    "F0": {"layout": False, "frame_opt": "all"},
    "F1": {"layout": True, "frame_opt": "all"},
    "FH": {"layout": True, "frame_opt": "hot"},
}
PRIMARY_COMPARISONS = [
    ("R", "F1"),
    ("L1", "F1"),
    ("L0", "F0"),
    ("F0", "F1"),
    ("R", "FH"),
]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def md5(path: Path) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def capture(command: list[str], *, cwd: Path = ROOT) -> dict[str, Any]:
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=True)
    return {
        "argv": command,
        "cwd": str(cwd),
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def git_revision(path: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


def version_line(command: list[str]) -> str:
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    output = result.stdout or result.stderr
    return output.splitlines()[0] if output else "unavailable"


def initialize(run_dir: Path) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "commands-and-logs").mkdir(exist_ok=True)
    (run_dir / "binaries").mkdir(exist_ok=True)
    (run_dir / "inventories").mkdir(exist_ok=True)
    (run_dir / "disassembly").mkdir(exist_ok=True)
    for required in (REFERENCE, PROFILE, BOLT, ARCHIVE, PRELOAD, BUILD_LOG):
        if not required.is_file():
            raise SystemExit(f"required recovered artifact is absent: {required}")
    if git_revision(LLVM_SOURCE) != BOLT_REVISION:
        raise SystemExit("pinned BOLT source revision mismatch")
    if git_revision(STOKE_SOURCE) != STOKE_REVISION:
        raise SystemExit("pinned STOKE source revision mismatch")
    if md5(ARCHIVE) != "4ed4916a40f2fe6547361db7560a63bc":
        raise SystemExit("LEMIX archive MD5 mismatch")

    submodules = capture(
        ["git", "-C", str(STOKE_SOURCE), "submodule", "status", "--recursive"]
    )
    cpu_info = Path("/proc/cpuinfo").read_text(errors="replace")
    model_match = re.search(r"^model name\s*:\s*(.*)$", cpu_info, re.MULTILINE)
    cpu_allowed = "unknown"
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("Cpus_allowed_list"):
            cpu_allowed = line.split(":", 1)[1].strip()
    governor_path = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
    cpu_max = Path("/sys/fs/cgroup/cpu.max")
    build_lines = BUILD_LOG.read_text(errors="replace").splitlines()
    gcc_commands = [line for line in build_lines if line.startswith("gcc ")]
    compile_commands = [line for line in gcc_commands if " -c " in line]
    link_commands = [line for line in gcc_commands if " -c " not in line]
    warning_lines = [line for line in build_lines if "warning:" in line]
    historical_paths = {
        "restricted_binary": APP / "build/SmartSpeaker.bolt-restricted",
        "trained_binary": APP / "build/SmartSpeaker.bolt-pgo",
        "restricted_log": APP / "build/bolt-restricted.log",
        "trained_log": APP / "build/bolt-pgo.log",
    }
    historical_control = {
        name: {
            "path": str(path),
            "sha256": sha256(path),
            "bytes": path.stat().st_size,
        }
        for name, path in historical_paths.items()
        if path.exists()
    }
    recovered_logs = run_dir / "commands-and-logs/historical-bolt-recovered"
    recovered_logs.mkdir(parents=True, exist_ok=True)
    for name, path in historical_paths.items():
        if name.endswith("_log") and path.exists():
            shutil.copy2(path, recovered_logs / path.name)

    manifest = {
        "schema_version": 1,
        "run_id": run_dir.name,
        "created_unix_ns": time.time_ns(),
        "historical_evidence_preserved": True,
        "reference_reused_not_rebuilt": True,
        "paths": {
            "repository": str(ROOT),
            "reference": str(REFERENCE),
            "profile": str(PROFILE),
            "preload": str(PRELOAD),
            "archive": str(ARCHIVE),
            "bolt": str(BOLT),
        },
        "revisions": {
            "bolt": BOLT_REVISION,
            "stoke": STOKE_REVISION,
            "stoke_submodules": submodules["stdout"].splitlines(),
        },
        "hashes": {
            "archive_md5": md5(ARCHIVE),
            "archive_sha256": sha256(ARCHIVE),
            "reference_sha256": sha256(REFERENCE),
            "profile_sha256": sha256(PROFILE),
            "preload_sha256": sha256(PRELOAD),
            "bolt_binary_sha256": sha256(BOLT),
            "build_log_sha256": sha256(BUILD_LOG),
        },
        "versions": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "gcc": version_line(["gcc", "--version"]),
            "ld": version_line(["ld", "--version"]),
            "objdump": version_line(["objdump", "--version"]),
            "cmake": version_line(["cmake", "--version"]),
            "bolt": capture([str(BOLT), "--version"])["stdout"].splitlines()[:8],
        },
        "host": {
            "uname": " ".join(platform.uname()),
            "cpu_model": model_match.group(1) if model_match else "unknown",
            "logical_cpus": os.cpu_count(),
            "allowed_cpus": cpu_allowed,
            "timing_affinity": [2, 3],
            "governor": governor_path.read_text().strip()
            if governor_path.exists()
            else "unavailable",
            "cpu_quota": cpu_max.read_text().strip() if cpu_max.exists() else "unavailable",
            "virtualized": "hypervisor" in cpu_info.lower(),
        },
        "shim": {
            "description": "zero-backed fixed-address mappings; not a device model",
            "mapped_ranges": [
                ["0x08000000", "0x09000000"],
                ["0x10000000", "0x10100000"],
                ["0x20000000", "0x21000000"],
                ["0x40000000", "0x60000000"],
                ["0xe0000000", "0xe1000000"],
            ],
        },
        "recovered_build_provenance": {
            "log": str(BUILD_LOG),
            "gcc_commands": len(gcc_commands),
            "compile_commands": len(compile_commands),
            "link_commands": len(link_commands),
            "all_compile_O0": all(" -O0 " in line for line in compile_commands),
            "all_compile_debug": all(" -g " in line for line in compile_commands),
            "all_compile_keep_frame_pointer": all(" -fno-omit-frame-pointer " in line for line in compile_commands),
            "link_uses_pthreads": any("pthread" in line for line in link_commands),
            "link_non_pie": any("-no-pie" in line for line in link_commands),
            "link_emits_relocations": any("--emit-relocs" in line for line in link_commands),
            "warning_lines": len(warning_lines),
            "pointer_width_warning_lines": sum(
                "pointer" in line and "different size" in line for line in warning_lines
            ),
            "commands_recovered_not_reexecuted": True,
        },
        "historical_commands_recovered": {
            "restricted": [
                str(BOLT), str(REFERENCE), "--lite=0", "--reorder-blocks=none",
                "--reorder-functions=none", "--split-functions=0", "--icf=0",
                "--dyno-stats", "-o", str(APP / "build/SmartSpeaker.bolt-restricted"),
            ],
            "trained": [
                str(BOLT), str(REFERENCE), f"--data={PROFILE}", "--lite=0",
                "--reorder-blocks=ext-tsp", "--reorder-functions=cdsort",
                "--split-functions", "--split-all-cold", "--dyno-stats", "-o",
                str(APP / "build/SmartSpeaker.bolt-pgo"),
            ],
            "frame_opt_effective_default": "none",
        },
        "historical_controls_recovered": historical_control,
    }
    write_json(run_dir / "manifest.json", manifest)
    write_json(
        run_dir / "workloads-and-contracts.json",
        {
            "schema_version": 1,
            "workload": "SmartSpeaker paced zero-backed MMIO",
            "reference_sha256": sha256(REFERENCE),
            "profile_sha256": sha256(PROFILE),
            "preload_sha256": sha256(PRELOAD),
            "interrupt_period_ms": 10,
            "timing_window_seconds": 5.0,
            "timing_blocks": 30,
            "timing_affinity": [2, 3],
            "work_marker": EVENT.decode(),
            "work_marker_semantics": "scheduler/interrupt task dispatch marker, not a completed input transaction",
            "primary_cost": "process CPU seconds per observed dispatch",
            "primary_comparisons": [list(item) for item in PRIMARY_COMPARISONS],
            "minimum_useful_reference_over_candidate_ratio": 1.05,
            "bootstrap_unit": "complete randomized repetition block",
            "profile_source": "recovered historical 12-second original-reference profile",
            "held_out_device_inputs": "unsupported by zero-backed shim",
            "capacity_throughput": "not measured by paced workload",
        },
    )
    write_json(
        run_dir / "bolt-arm-options.json",
        {
            "schema_version": 1,
            "reference": {"id": "R", "path": str(REFERENCE)},
            "profile": str(PROFILE),
            "common": COMMON_ARGS,
            "layout_off": LAYOUT_OFF,
            "layout_on": LAYOUT_ON,
            "arms": ARMS,
            "historical_restricted_is_not_L0": True,
            "frame_opt_rm_stores": False,
        },
    )
    help_result = capture([str(BOLT), "--help-hidden"])
    (run_dir / "commands-and-logs/llvm-bolt-help-hidden.stdout").write_text(
        help_result["stdout"]
    )
    (run_dir / "commands-and-logs/llvm-bolt-help-hidden.stderr").write_text(
        help_result["stderr"]
    )


def run_command_logged(run_dir: Path, label: str, command: list[str]) -> None:
    log_dir = run_dir / "commands-and-logs"
    record = {
        "label": label,
        "argv": command,
        "cwd": str(ROOT),
        "started_unix_ns": time.time_ns(),
    }
    started = time.monotonic()
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
    record["elapsed_seconds"] = time.monotonic() - started
    record["returncode"] = result.returncode
    (log_dir / f"{label}.stdout").write_text(result.stdout)
    (log_dir / f"{label}.stderr").write_text(result.stderr)
    write_json(log_dir / f"{label}.command.json", record)
    if result.returncode:
        raise SystemExit(f"{label} failed with status {result.returncode}")


def run_bolt_matrix(run_dir: Path, resume: bool) -> None:
    for arm, settings in ARMS.items():
        output = run_dir / "binaries" / f"SmartSpeaker.{arm}"
        if output.exists():
            if resume:
                continue
            raise SystemExit(f"refusing to overwrite candidate: {output}")
        command = [
            str(BOLT),
            str(REFERENCE),
            f"--data={PROFILE}",
            *COMMON_ARGS,
            *(LAYOUT_ON if settings["layout"] else LAYOUT_OFF),
            f"--frame-opt={settings['frame_opt']}",
            "-o",
            str(output),
        ]
        run_command_logged(run_dir, f"bolt-{arm}", command)


def run_bolt_store_diagnostic(run_dir: Path, resume: bool) -> None:
    """Run the separately labeled experimental store-removal arm."""
    output = run_dir / "binaries/SmartSpeaker.FRS"
    if output.exists():
        if resume:
            return
        raise SystemExit(f"refusing to overwrite diagnostic candidate: {output}")
    command = [
        str(BOLT), str(REFERENCE), f"--data={PROFILE}", *COMMON_ARGS,
        *LAYOUT_ON, "--frame-opt=all", "--frame-opt-rm-stores", "-o", str(output),
    ]
    run_command_logged(run_dir, "bolt-FRS", command)


def inventory_one(run_dir: Path, name: str, path: Path) -> dict[str, Any]:
    inventory_dir = run_dir / "inventories" / name
    inventory_dir.mkdir(parents=True, exist_ok=True)
    commands = {
        "file": ["file", str(path)],
        "size": ["size", str(path)],
        "sections": ["readelf", "-W", "-S", str(path)],
        "segments": ["readelf", "-W", "-l", str(path)],
        "symbols": ["readelf", "-W", "-s", str(path)],
        "relocations": ["readelf", "-W", "-r", str(path)],
        "notes": ["readelf", "-W", "-n", str(path)],
        "objdump_headers": ["objdump", "-h", str(path)],
        "nm": ["nm", "-n", "-S", "--defined-only", str(path)],
    }
    for label, command in commands.items():
        result = capture(command)
        (inventory_dir / f"{label}.stdout").write_text(result["stdout"])
        (inventory_dir / f"{label}.stderr").write_text(result["stderr"])
        if result["returncode"]:
            raise SystemExit(f"inventory command failed for {name}: {command}")
    full_disassembly = capture(["objdump", "-d", "-w", str(path)])
    (run_dir / "disassembly" / f"{name}.objdump.txt").write_text(
        full_disassembly["stdout"]
    )
    accessor = capture(
        ["objdump", "-d", "-w", "--disassemble=prvGetThreadFromTask", str(path)]
    )
    (run_dir / "disassembly" / f"{name}.prvGetThreadFromTask.txt").write_text(
        accessor["stdout"]
    )
    size_output = (inventory_dir / "size.stdout").read_text().splitlines()
    parsed_size: dict[str, int] = {}
    if len(size_output) >= 2:
        fields = size_output[-1].split()
        if len(fields) >= 4:
            parsed_size = {
                "text": int(fields[0]),
                "data": int(fields[1]),
                "bss": int(fields[2]),
                "dec": int(fields[3]),
            }
    return {
        "id": name,
        "path": str(path),
        "sha256": sha256(path),
        "file_bytes": path.stat().st_size,
        "gnu_size": parsed_size,
    }


def create_inventories(run_dir: Path) -> None:
    variants = {"R": REFERENCE}
    variants.update(
        {arm: run_dir / "binaries" / f"SmartSpeaker.{arm}" for arm in ARMS}
    )
    inventory = [inventory_one(run_dir, name, path) for name, path in variants.items()]
    write_json(run_dir / "binary-layout-inventory.json", {"schema_version": 1, "variants": inventory})


def create_store_diagnostic_inventory(run_dir: Path) -> None:
    path = run_dir / "binaries/SmartSpeaker.FRS"
    inventory = inventory_one(run_dir, "FRS", path)
    write_json(
        run_dir / "binary-layout-inventory-experimental.json",
        {
            "schema_version": 1,
            "variant": inventory,
            "classification": "diagnostic only; excluded from the frozen primary timing matrix",
            "frame_opt_rm_stores": True,
        },
    )


def layout_attribution(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        elf = ELFFile(stream)
        executable_sections = []
        for section in elf.iter_sections():
            flags = int(section["sh_flags"])
            if flags & 0x2 and flags & 0x4:
                executable_sections.append(
                    {
                        "name": section.name,
                        "file_offset": int(section["sh_offset"]),
                        "virtual_address": int(section["sh_addr"]),
                        "bytes": int(section["sh_size"]),
                        "alignment": int(section["sh_addralign"]),
                    }
                )
        executable_segments = []
        for segment in elf.iter_segments():
            if segment["p_type"] == "PT_LOAD" and int(segment["p_flags"]) & 0x1:
                executable_segments.append(
                    {
                        "file_offset": int(segment["p_offset"]),
                        "virtual_address": int(segment["p_vaddr"]),
                        "file_bytes": int(segment["p_filesz"]),
                        "memory_bytes": int(segment["p_memsz"]),
                        "alignment": int(segment["p_align"]),
                    }
                )
    by_name = {item["name"]: item["bytes"] for item in executable_sections}
    has_retained_original = ".bolt.org.text" in by_name
    retained_original = by_name.get(".bolt.org.text", 0)
    replacement = (
        by_name.get(".text", 0) + by_name.get(".text.cold", 0)
        if has_retained_original
        else 0
    )
    original_live = by_name.get(".text", 0) if not has_retained_original else 0
    known = {".bolt.org.text", ".text", ".text.cold"}
    other = sum(item["bytes"] for item in executable_sections if item["name"] not in known)
    segment_file_bytes = sum(item["file_bytes"] for item in executable_segments)
    section_bytes = sum(item["bytes"] for item in executable_sections)
    return {
        "path": str(path),
        "file_bytes": path.stat().st_size,
        "executable_sections": executable_sections,
        "executable_section_bytes": section_bytes,
        "loaded_executable_segments": executable_segments,
        "loaded_executable_file_bytes": segment_file_bytes,
        "loaded_executable_memory_bytes": sum(item["memory_bytes"] for item in executable_segments),
        "segment_minus_executable_sections_bytes": segment_file_bytes - section_bytes,
        "attribution": {
            "original_live_text_bytes": original_live,
            "retained_original_text_bytes": retained_original,
            "replacement_hot_and_cold_text_bytes": replacement,
            "other_executable_section_bytes": other,
        },
        "limits": "segment-minus-section bytes include alignment, headers and any non-section contents; section names do not establish dynamic hotness",
    }


def create_layout_attribution(run_dir: Path) -> None:
    variants = {"R": REFERENCE}
    variants.update({arm: run_dir / "binaries" / f"SmartSpeaker.{arm}" for arm in ARMS})
    diagnostic = run_dir / "binaries/SmartSpeaker.FRS"
    if diagnostic.exists():
        variants["FRS"] = diagnostic
    write_json(
        run_dir / "binary-layout-attribution.json",
        {"schema_version": 1, "variants": {name: layout_attribution(path) for name, path in variants.items()}},
    )


def observe(path: Path, duration: float, cpus: set[int]) -> dict[str, Any]:
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = time.monotonic()
    environment = dict(os.environ)
    environment["LD_PRELOAD"] = str(PRELOAD)

    def set_affinity() -> None:
        os.sched_setaffinity(0, cpus)

    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(
            [str(path)],
            stdout=output,
            stderr=subprocess.STDOUT,
            env=environment,
            start_new_session=True,
            preexec_fn=set_affinity,
        )
        time.sleep(duration)
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGINT)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        ended = time.monotonic()
        output.seek(0)
        content = output.read()
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    user = after.ru_utime - before.ru_utime
    system = after.ru_stime - before.ru_stime
    work = content.count(EVENT)
    return {
        "wall_seconds": ended - started,
        "user_seconds": user,
        "system_seconds": system,
        "cpu_seconds": user + system,
        "work_dispatches": work,
        "cpu_seconds_per_dispatch": (user + system) / work if work else None,
        "output_bytes": len(content),
        "returncode": process.returncode,
        "valid": process.returncode == 2 and work > 0,
    }


def percentile(values: list[float], probability: float) -> float:
    return values[min(len(values) - 1, max(0, int(probability * len(values))))]


def analyze_samples(samples: list[dict[str, Any]], seed: int) -> dict[str, Any]:
    by_block: dict[int, dict[str, dict[str, Any]]] = {}
    for sample in samples:
        by_block.setdefault(sample["block"], {})[sample["variant"]] = sample
    comparisons: dict[str, Any] = {}
    rng = random.Random(seed)
    for reference, candidate in PRIMARY_COMPARISONS:
        ratios: list[float] = []
        for block in sorted(by_block):
            pair = by_block[block]
            if reference not in pair or candidate not in pair:
                continue
            left = pair[reference]["cpu_seconds_per_dispatch"]
            right = pair[candidate]["cpu_seconds_per_dispatch"]
            if left is not None and right not in (None, 0):
                ratios.append(left / right)
        draws: list[float] = []
        for _ in range(100_000):
            resample = [rng.choice(ratios) for _ in ratios]
            draws.append(statistics.median(resample))
        draws.sort()
        lower = percentile(draws, 0.025)
        upper = percentile(draws, 0.975)
        comparisons[f"{reference}/{candidate}"] = {
            "blocks": len(ratios),
            "paired_ratios": ratios,
            "median_reference_over_candidate": statistics.median(ratios),
            "bootstrap_95_percent": [lower, upper],
            "minimum_useful_ratio": 1.05,
            "classification": (
                "useful_gain"
                if lower >= 1.05
                else "regression"
                if upper <= 1 / 1.05
                else "inconclusive"
            ),
        }
    variants: dict[str, Any] = {}
    for name in ["R", *ARMS]:
        rows = [item for item in samples if item["variant"] == name]
        variants[name] = {
            "samples": len(rows),
            "all_valid": all(item["valid"] for item in rows),
            "median_cpu_seconds": statistics.median(item["cpu_seconds"] for item in rows),
            "median_work_dispatches": statistics.median(item["work_dispatches"] for item in rows),
            "median_cpu_seconds_per_dispatch": statistics.median(
                item["cpu_seconds_per_dispatch"] for item in rows
            ),
            "median_wall_seconds": statistics.median(item["wall_seconds"] for item in rows),
        }
    return {
        "schema_version": 1,
        "pairing": "variant order randomized within complete repetition block",
        "bootstrap_draws": 100_000,
        "variants": variants,
        "comparisons": comparisons,
    }


def run_measurement(run_dir: Path, blocks: int, duration: float, seed: int) -> None:
    samples_path = run_dir / "samples.jsonl"
    if samples_path.exists():
        raise SystemExit(f"refusing to overwrite samples: {samples_path}")
    variants = {"R": REFERENCE}
    variants.update(
        {arm: run_dir / "binaries" / f"SmartSpeaker.{arm}" for arm in ARMS}
    )
    rng = random.Random(seed)
    samples: list[dict[str, Any]] = []
    sequence = 0
    with samples_path.open("w") as output:
        for block in range(blocks):
            order = list(variants)
            rng.shuffle(order)
            for position, name in enumerate(order):
                sequence += 1
                sample = observe(variants[name], duration, {2, 3})
                sample.update(
                    {
                        "block": block,
                        "position": position,
                        "sequence": sequence,
                        "variant": name,
                        "binary_sha256": sha256(variants[name]),
                    }
                )
                samples.append(sample)
                output.write(json.dumps(sample, sort_keys=True) + "\n")
                output.flush()
                print(
                    f"block={block + 1}/{blocks} variant={name} "
                    f"work={sample['work_dispatches']} cpu={sample['cpu_seconds']:.6f} "
                    f"valid={sample['valid']}",
                    flush=True,
                )
    write_json(run_dir / "paired-results.json", analyze_samples(samples, seed))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_RUN)
    parser.add_argument(
        "--stage",
        choices=("initialize", "bolt", "bolt-store", "inventory", "store-inventory", "attribution", "measure", "all"),
        default="all",
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--blocks", type=int, default=30)
    parser.add_argument("--duration", type=float, default=5.0)
    parser.add_argument("--seed", type=int, default=20261116)
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    initialize(run_dir)
    if args.stage in ("bolt", "all"):
        run_bolt_matrix(run_dir, args.resume)
    if args.stage in ("bolt-store", "all"):
        run_bolt_store_diagnostic(run_dir, args.resume)
    if args.stage in ("inventory", "all"):
        create_inventories(run_dir)
    if args.stage in ("store-inventory", "all"):
        create_store_diagnostic_inventory(run_dir)
    if args.stage in ("attribution", "all"):
        create_layout_attribution(run_dir)
    if args.stage in ("measure", "all"):
        run_measurement(run_dir, args.blocks, args.duration, args.seed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
