#!/usr/bin/env python3
"""Profile exact unused Thumb frame-pointer initializations in real firmware.

This is a training-only screen.  A candidate must save r7 before ``mov r7,sp``,
never read the assigned value, and restore r7 on every syntactically decoded
return in the symbol-bounded function.  The profile does not authorize a
rewrite or prove that asynchronous observers cannot inspect r7.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from capstone import CS_ARCH_ARM, CS_MODE_MCLASS, CS_MODE_THUMB, Cs
from elftools.elf.elffile import ELFFile

from fastdyn.preflight.model import canonical_json_bytes, hash_file

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_IMAGE = ROOT / "tests/sample_binaries/RTOS/RTOSDemo.axf"
DEFAULT_CONFIG = ROOT / "configs/minimal.toml"
DEFAULT_FASTDYN = ROOT / "fastdyn-env/bin/fastdyn"
DEFAULT_OUTPUT = ROOT / "build/preflight/thumb-cortex-m3-frame-profile-2026-11-17"
BENCHMARK_END = re.compile(
    r"\n\[\[CPU\.cpu0\.virtuals\]\]\s*# vTaskStartScheduler\(\): print the time and stop\n"
    r'at = "0x08001940"\n'
    r'instruction = "benchmark_end"\n'
    r'args = \["minimal"\]\n',
    re.MULTILINE,
)


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    function: str
    function_address: int
    function_size: int
    mov_address: int
    mov_encoding: str
    save_address: int
    restore_addresses: tuple[int, ...]
    marker: str


def write_json(path: Path, value: object) -> None:
    path.write_bytes(canonical_json_bytes(value) + b"\n")


def discover(image: Path) -> list[Candidate]:
    decoder = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_MCLASS)
    decoder.detail = True
    candidates: dict[int, Candidate] = {}
    with image.open("rb") as stream:
        elf = ELFFile(stream)
        symbols = elf.get_section_by_name(".symtab")
        if symbols is None:
            raise RuntimeError("frame profiling requires a symbol table")
        segments = [item for item in elf.iter_segments() if item["p_type"] == "PT_LOAD"]
        functions = sorted(
            (
                (item.name, int(item["st_value"]) & ~1, int(item["st_size"]))
                for item in symbols.iter_symbols()
                if str(item["st_info"]["type"]) == "STT_FUNC" and int(item["st_size"]) > 0
            ),
            key=lambda item: (item[1], item[0]),
        )
        for name, address, size in functions:
            body = None
            for segment in segments:
                start = int(segment["p_vaddr"])
                if start <= address and address + size <= start + int(segment["p_filesz"]):
                    stream.seek(int(segment["p_offset"]) + address - start)
                    body = stream.read(size)
                    break
            if body is None:
                continue
            instructions = list(decoder.disasm(body, address))
            if not instructions or sum(item.size for item in instructions) != size:
                continue
            for index, instruction in enumerate(instructions):
                if not (
                    instruction.mnemonic == "mov"
                    and instruction.op_str == "r7, sp"
                    and instruction.size == 2
                    and bytes(instruction.bytes).hex() == "6f46"
                ):
                    continue
                prior = instructions[:index]
                saves = tuple(
                    item.address for item in prior
                    if item.mnemonic == "push" and "r7" in item.op_str
                )
                if not saves:
                    continue
                restores = tuple(
                    item.address for item in instructions[index + 1:]
                    if item.mnemonic == "pop" and "r7" in item.op_str
                )
                if not restores:
                    continue
                if any(
                    item.mnemonic in {"bx", "blx"} and item.op_str == "lr"
                    for item in instructions[index + 1:]
                ):
                    continue
                if any(
                    item.mnemonic == "pop" and "pc" in item.op_str and "r7" not in item.op_str
                    for item in instructions[index + 1:]
                ):
                    continue
                reads_assigned_r7 = False
                writes_before_restore = False
                for item in instructions[index + 1:]:
                    if item.address in restores:
                        continue
                    try:
                        reads, writes = item.regs_access()
                    except Exception:
                        reads_assigned_r7 = True
                        break
                    read_names = {item.reg_name(reg) for reg in reads}
                    write_names = {item.reg_name(reg) for reg in writes}
                    reads_assigned_r7 |= "r7" in read_names
                    writes_before_restore |= "r7" in write_names
                if reads_assigned_r7 or writes_before_restore:
                    continue
                marker = f"THUMB_DEAD_FP_{instruction.address:08X}"
                candidates.setdefault(
                    instruction.address,
                    Candidate(
                        f"{name}:0x{instruction.address:08x}", name, address, size,
                        instruction.address, bytes(instruction.bytes).hex(),
                        saves[-1], restores, marker,
                    ),
                )
    return sorted(candidates.values(), key=lambda item: item.mov_address)


def build_config(source: Path, image: Path, output: Path, candidates: list[Candidate]) -> None:
    text = source.read_text()
    text = text.replace(
        'binary = "tests/sample_binaries/RTOS/RTOSDemo.axf"',
        f'binary = "{image}"', 1,
    )
    text = text.replace("monitor_port = 5555", "monitor_port = 0", 1)
    text = text.replace(
        'qmp_socket = "/tmp/fastdyn-minimal.qmp"',
        f'qmp_socket = "{output.parent / "profile.qmp"}"', 1,
    )
    text, removed = BENCHMARK_END.subn("\n", text, count=1)
    if removed != 1:
        raise RuntimeError("could not remove benchmark_end from frozen config")
    for item in candidates:
        text += (
            "\n[[CPU.cpu0.virtuals]]\n"
            f'at = "0x{item.mov_address:08x}"\n'
            'instruction = "debug_log"\n'
            f'args = ["{item.marker}"]\n'
        )
    output.write_text(text)


def run_window(
    fastdyn: Path, config: Path, work_dir: Path, stdout_path: Path,
    stderr_path: Path, seconds: float,
) -> dict[str, object]:
    command = [str(fastdyn), "-q", "run", "-c", str(config), "-o", str(work_dir)]
    started = time.monotonic()
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        process = subprocess.Popen(
            command, cwd=ROOT, stdout=stdout, stderr=stderr,
            start_new_session=True,
        )
        timed_out = False
        try:
            returncode = process.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGTERM)
            try:
                returncode = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                returncode = process.wait(timeout=5)
    return {
        "command": command,
        "requested_window_seconds": seconds,
        "elapsed_seconds": time.monotonic() - started,
        "timed_out_as_planned": timed_out,
        "returncode_after_window": returncode,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", type=Path, default=DEFAULT_IMAGE)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--fastdyn", type=Path, default=DEFAULT_FASTDYN)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--seconds", type=float, default=3.0)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        if not args.replace:
            raise SystemExit(f"output exists: {output}")
        shutil.rmtree(output)
    output.mkdir(parents=True)
    image = args.image.resolve()
    candidates = discover(image)
    if not candidates:
        raise RuntimeError("no exact unused-frame-pointer candidates")
    config = output / "profile.toml"
    build_config(args.config.resolve(), image, config, candidates)
    run = run_window(
        args.fastdyn.resolve(), config, output / "run",
        output / "stdout.log", output / "stderr.log", args.seconds,
    )
    combined = (
        (output / "stdout.log").read_text(errors="replace") + "\n"
        + (output / "stderr.log").read_text(errors="replace")
    )
    rows = []
    for item in candidates:
        row = asdict(item)
        row["hit_count"] = combined.count(item.marker)
        rows.append(row)
    rows.sort(key=lambda item: (-int(item["hit_count"]), int(item["mov_address"])))
    report = {
        "schema_version": 1,
        "classification": "training_only_unused_thumb_frame_pointer_profile",
        "input": {"path": str(image), "sha256": hash_file(image)},
        "environment": {
            "source_config": str(args.config.resolve()),
            "source_config_sha256": hash_file(args.config.resolve()),
            "generated_config": str(config),
            "generated_config_sha256": hash_file(config),
            "benchmark_end_removed": True,
            "diagnostic_virtuals": len(candidates),
        },
        "run": run,
        "candidate_count": len(candidates),
        "hit_candidate_count": sum(int(item["hit_count"]) > 0 for item in rows),
        "total_hits": sum(int(item["hit_count"]) for item in rows),
        "selection_contract": {
            "exact_encoding": "mov r7,sp / 6f46",
            "r7_saved_before": True,
            "r7_not_read_or_overwritten_before_restore": True,
            "r7_restored": True,
            "asynchronous_r7_observer_qualified": False,
            "rewrite_authorized": False,
        },
        "candidates": rows,
        "claims": {
            "performance_result": False,
            "semantic_acceptance": False,
            "runtime_coverage_complete": False,
        },
    }
    write_json(output / "report.json", report)
    print(json.dumps({
        "candidate_count": len(rows),
        "hit_candidate_count": report["hit_candidate_count"],
        "total_hits": report["total_hits"],
        "top": [
            (item["function"], item["mov_address"], item["hit_count"])
            for item in rows[:10]
        ],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
