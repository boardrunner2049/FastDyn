#!/usr/bin/env python3
"""Profile exact bounded Thumb spill/reload candidates in a real firmware.

This is a training-only probe.  It adds diagnostic virtual instructions at the
reload PCs, runs the existing firmware/environment for a fixed wall window,
and records actual pair-hit counts.  It does not authorize a rewrite.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from capstone import (
    CS_ARCH_ARM,
    CS_GRP_CALL,
    CS_GRP_INT,
    CS_GRP_JUMP,
    CS_GRP_RET,
    CS_MODE_MCLASS,
    CS_MODE_THUMB,
    Cs,
)
from elftools.elf.elffile import ELFFile

from fastdyn.preflight.model import canonical_json_bytes, hash_file


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_IMAGE = ROOT / "tests/sample_binaries/RTOS/RTOSDemo.axf"
DEFAULT_CONFIG = ROOT / "configs/minimal.toml"
DEFAULT_FASTDYN = ROOT / "fastdyn-env/bin/fastdyn"
DEFAULT_OUTPUT = ROOT / "build/preflight/thumb-cortex-m3-profile-2026-11-16"
ACCESS = re.compile(
    r"^(r(?:1[0-2]|[0-9])), \[(?P<base>sp|r7)"
    r"(?:, #(?P<offset>0x[0-9a-f]+|[0-9]+))?\]$"
)
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
    store_address: int
    reload_address: int
    store_encoding: str
    reload_encoding: str
    store_operands: str
    reload_operands: str
    base_register: str
    offset: int
    source_register: str
    destination_register: str
    intervening_addresses: tuple[int, ...]
    intervening_encodings: tuple[str, ...]
    intervening_mnemonics: tuple[str, ...]
    intervening_operands: tuple[str, ...]
    marker: str


def write_json(path: Path, value: object) -> None:
    path.write_bytes(canonical_json_bytes(value) + b"\n")


def parse_access(operands: str) -> tuple[str, str, int] | None:
    match = ACCESS.match(operands)
    if match is None:
        return None
    raw_offset = match.group("offset")
    return (
        match.group(1),
        match.group("base"),
        int(raw_offset, 0) if raw_offset is not None else 0,
    )


def discover(image: Path, max_gap_instructions: int = 0) -> list[Candidate]:
    decoder = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_MCLASS)
    decoder.detail = True
    by_reload: dict[int, Candidate] = {}
    with image.open("rb") as stream:
        elf = ELFFile(stream)
        symbols = elf.get_section_by_name(".symtab")
        if symbols is None:
            raise RuntimeError("candidate profiling requires the retained symbol table")
        segments = [segment for segment in elf.iter_segments() if segment["p_type"] == "PT_LOAD"]
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
                file_size = int(segment["p_filesz"])
                if start <= address and address + size <= start + file_size:
                    stream.seek(int(segment["p_offset"]) + address - start)
                    body = stream.read(size)
                    break
            if body is None:
                continue
            instructions = list(decoder.disasm(body, address))
            if sum(item.size for item in instructions) != size:
                continue
            for store_index, store in enumerate(instructions):
                if store.mnemonic != "str":
                    continue
                store_access = parse_access(store.op_str)
                if store_access is None:
                    continue
                source, store_base, store_offset = store_access
                if store.size != 2:
                    continue
                limit = min(
                    len(instructions), store_index + max_gap_instructions + 2
                )
                for load_index in range(store_index + 1, limit):
                    reload = instructions[load_index]
                    reload_access = (
                        parse_access(reload.op_str)
                        if reload.mnemonic == "ldr" else None
                    )
                    if reload_access is not None:
                        destination, reload_base, reload_offset = reload_access
                        if (
                            store_base == reload_base
                            and store_offset == reload_offset
                            and reload.size == 2
                        ):
                            marker = f"THUMB_FOP_{reload.address:08X}"
                            intervening = instructions[store_index + 1:load_index]
                            candidate = Candidate(
                                candidate_id=f"{name}:0x{reload.address:08x}",
                                function=name,
                                function_address=address,
                                function_size=size,
                                store_address=store.address,
                                reload_address=reload.address,
                                store_encoding=bytes(store.bytes).hex(),
                                reload_encoding=bytes(reload.bytes).hex(),
                                store_operands=store.op_str,
                                reload_operands=reload.op_str,
                                base_register=store_base,
                                offset=store_offset,
                                source_register=source,
                                destination_register=destination,
                                intervening_addresses=tuple(
                                    int(item.address) for item in intervening
                                ),
                                intervening_encodings=tuple(
                                    bytes(item.bytes).hex() for item in intervening
                                ),
                                intervening_mnemonics=tuple(
                                    str(item.mnemonic) for item in intervening
                                ),
                                intervening_operands=tuple(
                                    str(item.op_str) for item in intervening
                                ),
                                marker=marker,
                            )
                            # Symbol aliases may describe the same bytes. Keep
                            # the nearest proven source store for each reload.
                            by_reload.setdefault(reload.address, candidate)
                            if destination != source:
                                break
                            # Reloading the source from the same private slot
                            # refreshes, rather than clobbers, the known value.
                            continue
                    if any(
                        group in reload.groups
                        for group in (CS_GRP_JUMP, CS_GRP_CALL, CS_GRP_RET, CS_GRP_INT)
                    ):
                        break
                    try:
                        _reads, writes = reload.regs_access()
                    except Exception:
                        break
                    written = {reload.reg_name(item) for item in writes}
                    if source in written or store_base in written:
                        break
                    if reload.mnemonic.startswith("str"):
                        access = parse_access(reload.op_str)
                        if access is not None and access[1:] == (store_base, store_offset):
                            break
    return sorted(by_reload.values(), key=lambda item: item.reload_address)


def build_config(source: Path, image: Path, output: Path, candidates: list[Candidate]) -> None:
    text = source.read_text()
    text = text.replace(
        'binary = "tests/sample_binaries/RTOS/RTOSDemo.axf"',
        f'binary = "{image}"',
        1,
    )
    text = text.replace("monitor_port = 5555", "monitor_port = 0", 1)
    text = text.replace(
        'qmp_socket = "/tmp/fastdyn-minimal.qmp"',
        f'qmp_socket = "{output.parent / "profile.qmp"}"',
        1,
    )
    text, removed = BENCHMARK_END.subn("\n", text, count=1)
    if removed != 1:
        raise RuntimeError("could not remove the frozen benchmark_end block")
    for item in candidates:
        text += (
            "\n[[CPU.cpu0.virtuals]]\n"
            f'at = "0x{item.reload_address:08x}"\n'
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
            command,
            cwd=ROOT,
            stdout=stdout,
            stderr=stderr,
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
    parser.add_argument("--max-gap-instructions", type=int, default=0)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()

    if args.max_gap_instructions < 0:
        raise SystemExit("--max-gap-instructions must be non-negative")
    output = args.output.resolve()
    if output.exists():
        if not args.replace:
            raise SystemExit(f"output exists: {output}")
        shutil.rmtree(output)
    output.mkdir(parents=True)
    image = args.image.resolve()
    candidates = discover(image, args.max_gap_instructions)
    config = output / "profile.toml"
    build_config(args.config.resolve(), image, config, candidates)
    run = run_window(
        args.fastdyn.resolve(), config, output / "run",
        output / "stdout.log", output / "stderr.log", args.seconds,
    )
    combined = (output / "stdout.log").read_text(errors="replace") + "\n" + (
        output / "stderr.log"
    ).read_text(errors="replace")
    rows = []
    for item in candidates:
        row = asdict(item)
        row["hit_count"] = combined.count(item.marker)
        rows.append(row)
    rows.sort(key=lambda item: (-int(item["hit_count"]), int(item["reload_address"])))
    with (output / "candidates.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    report = {
        "schema_version": 1,
        "classification": "training_only_exact_thumb_pair_profile",
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
        "selection_policy": {
            "max_gap_instructions": args.max_gap_instructions,
            "intervening_control_transfer_allowed": False,
            "source_or_base_clobber_allowed": False,
            "same_private_slot_overwrite_allowed": False,
        },
        "hit_candidate_count": sum(int(item["hit_count"]) > 0 for item in rows),
        "total_pair_hits": sum(int(item["hit_count"]) for item in rows),
        "top_candidates": rows[:50],
        "claims": {
            "rewrite_authorized": False,
            "performance_result": False,
            "runtime_coverage_complete": False,
        },
    }
    write_json(output / "report.json", report)
    print(json.dumps({
        "report": str(output / "report.json"),
        "candidates": len(candidates),
        "hit_candidates": report["hit_candidate_count"],
        "total_pair_hits": report["total_pair_hits"],
        "top": [(item["function"], item["reload_address"], item["hit_count"]) for item in rows[:10]],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
