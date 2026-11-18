#!/usr/bin/env python3
"""Bounded STOKE extraction/state/search funnel for the LEMIX O0 rerun."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from capstone import CS_ARCH_X86, CS_GRP_CALL, CS_GRP_JUMP, CS_MODE_64, Cs
from capstone.x86_const import X86_OP_MEM, X86_REG_RIP
from elftools.elf.elffile import ELFFile


ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / (
    "third_party/preflight/lemix/artifact/Lemix/Ported_Applications/dataset/"
    "FreeRTOS_Apps/FreeRTOS_Apps_Ported/SmartSpeaker_Ported/SmartSpeaker_Ported"
)
REFERENCE = APP / "build/SmartSpeaker"
PROFILED_CSV = APP / "build/stoke-profiled.csv"
UNPROFILED_CSV = APP / "build/stoke.csv"
OLD_EVIDENCE = ROOT / "build/preflight/lemix-smartspeaker"
DEFAULT_RUN = ROOT / "build/preflight/lemix-o0-rerun-2026-11-16"
CONTAINER = "fastdyn-stoke-build"
REMOTE = "/tmp/lemix-o0-focused-rerun"
STOKE = "/tmp/stoke/bin/stoke"

FROZEN_SEARCH_COHORT = [
    "prvGetThreadFromTask/1",       # accessor and historical survivor
    "vListInitialiseItem",          # ordinary memory output
    "IWDG_ReloadCounter",           # fixed-address peripheral-shaped store
    "WWDG_IRQHandler",              # bounded scalar/control body
    "vApplicationTickHook",         # small hot callback
    "_fini",                        # small non-memory control
]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def invoke(command: list[str], timeout: int = 300) -> dict[str, Any]:
    started = time.monotonic()
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=timeout)
        return {
            "argv": command,
            "returncode": result.returncode,
            "elapsed_seconds": time.monotonic() - started,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as error:
        return {
            "argv": command,
            "returncode": None,
            "elapsed_seconds": time.monotonic() - started,
            "stdout": error.stdout or "",
            "stderr": error.stderr or "",
            "timed_out": True,
        }


def docker(arguments: list[str], timeout: int = 300) -> dict[str, Any]:
    return invoke(["docker", "exec", CONTAINER, STOKE, *arguments], timeout=timeout)


def safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name)


def root_registers(text: str, *, inputs: bool) -> list[str]:
    aliases = {
        "AH": "rax", "AL": "rax", "AX": "rax", "EAX": "rax", "HAX": "rax", "RAX": "rax",
        "CH": "rcx", "CL": "rcx", "CX": "rcx", "ECX": "rcx", "HCX": "rcx", "RCX": "rcx",
        "DH": "rdx", "DL": "rdx", "DX": "rdx", "EDX": "rdx", "HDX": "rdx", "RDX": "rdx",
        "RDI": "rdi", "RSI": "rsi", "R8": "r8", "R9": "r9",
    }
    roots: set[str] = set()
    for token in re.findall(r"%([A-Za-z0-9]+)", text):
        upper = token.upper()
        if upper in aliases:
            roots.add(aliases[upper])
        elif upper.startswith(("XMM", "YMM", "ZMM")):
            roots.add("ymm" + re.sub(r"^[A-Z]+", "", upper))
    roots.add("rsp")
    roots.add("rbp")
    if inputs:
        roots.add("rdi")
    order = ["rsp", "rbp", "rax", "rdi", "rsi", "rdx", "rcx", "r8", "r9"]
    order += [f"ymm{i}" for i in range(16)]
    return [item for item in order if item in roots]


def reg_set(items: list[str]) -> str:
    return "{ " + " ".join("%" + item for item in items) + " }"


def load_candidates() -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    with PROFILED_CSV.open(newline="") as stream:
        profiled = list(csv.DictReader(stream))
    candidates = [
        row for row in profiled
        if row["Omitted"] == "0" and row["IsLoopFree"] == "1" and row["HasCall"] == "0"
    ]
    if len(profiled) != 194 or len(candidates) != 23:
        raise SystemExit("recovered candidate population differs from frozen cohort")
    return profiled, candidates


def elf_mapping_and_symbols() -> tuple[list[tuple[int, int, int]], dict[int, list[dict[str, Any]]]]:
    mappings = []
    symbols: dict[int, list[dict[str, Any]]] = {}
    with REFERENCE.open("rb") as stream:
        elf = ELFFile(stream)
        for segment in elf.iter_segments():
            if segment["p_type"] == "PT_LOAD":
                mappings.append((segment["p_offset"], segment["p_filesz"], segment["p_vaddr"]))
        for section in elf.iter_sections():
            if section.header["sh_type"] not in ("SHT_SYMTAB", "SHT_DYNSYM"):
                continue
            for symbol in section.iter_symbols():
                if symbol["st_info"]["type"] == "STT_FUNC" and symbol["st_value"]:
                    symbols.setdefault(symbol["st_value"], []).append(
                        {"name": symbol.name, "size": symbol["st_size"]}
                    )
    return mappings, symbols


def file_offset_to_va(offset: int, mappings: list[tuple[int, int, int]]) -> int:
    for file_offset, size, va in mappings:
        if file_offset <= offset < file_offset + size:
            return va + offset - file_offset
    raise ValueError(f"file offset not mapped: {offset:#x}")


def canonical_prepare(run_dir: Path) -> None:
    canonical = run_dir / "stoke-canonical"
    canonical.mkdir(parents=True, exist_ok=True)
    profiled, candidates = load_candidates()
    mappings, symbols = elf_mapping_and_symbols()
    binary = REFERENCE.read_bytes()
    disassembler = Cs(CS_ARCH_X86, CS_MODE_64)
    disassembler.detail = True
    old_emitted = {
        "CLZ", "RefillBitstreamCache", "deregister_tm_clones", "idct9", "imdct12",
        "prvGetThreadFromTask", "register_tm_clones", "vNet_LsnStart", "vNet_SendBuf",
    }
    records: list[dict[str, Any]] = []
    for row in candidates:
        file_offset = int(row["Offset"])
        size = int(row["Size"])
        va = file_offset_to_va(file_offset, mappings)
        body = binary[file_offset:file_offset + size]
        instructions = list(disassembler.disasm(body, va))
        decoded_bytes = sum(item.size for item in instructions)
        exact_decode = decoded_bytes == size and all(
            instructions[index].address + instructions[index].size == instructions[index + 1].address
            for index in range(len(instructions) - 1)
        )
        rip_relative = any(
            operand.type == X86_OP_MEM and operand.mem.base == X86_REG_RIP
            for item in instructions for operand in item.operands
        )
        external_control = []
        for item in instructions:
            if not (item.group(CS_GRP_JUMP) or item.group(CS_GRP_CALL)):
                continue
            if item.operands and item.operands[0].type == 2:
                target = int(item.operands[0].imm)
                if not (va <= target < va + size):
                    external_control.append({"address": item.address, "target": target})
        symbol_matches = symbols.get(va, [])
        actual_name = symbol_matches[0]["name"] if symbol_matches else row["FuncName"].split("/", 1)[0]
        protected_prefix = 4 if body.startswith(bytes.fromhex("f30f1efa")) else 0
        search_body = body[protected_prefix:]
        search_offset = file_offset + protected_prefix
        search_va = va + protected_prefix
        candidate_dir = canonical / safe_name(row["FuncName"])
        candidate_dir.mkdir(exist_ok=True)
        body_path = candidate_dir / "body.bin"
        body_path.write_bytes(body)
        search_body_path = candidate_dir / "search-body.bin"
        search_body_path.write_bytes(search_body)
        asm_path = candidate_dir / "fixture.S"
        asm_path.write_text(
            ".section .text\n"
            ".globl candidate\n.type candidate,@function\n"
            "candidate:\n"
            f".incbin \"{search_body_path}\"\n"
            ".size candidate,.-candidate\n"
            ".globl sentinel\n.type sentinel,@function\n"
            "sentinel:\n retq\n.size sentinel,.-sentinel\n"
            ".section .note.GNU-stack,\"\",@progbits\n"
        )
        fixture = candidate_dir / "fixture.elf"
        compile_command = [
            "gcc", "-nostdlib", "-no-pie", "-Wl,--build-id=none,-e,candidate",
            str(asm_path), "-o", str(fixture),
        ]
        compiled = invoke(compile_command)
        write_json(candidate_dir / "fixture-build.json", compiled)
        record = {
            **row,
            "ActualSymbol": actual_name,
            "VirtualAddress": va,
            "RegionSha256": sha256_bytes(body),
            "ProtectedPrefixBytes": protected_prefix,
            "ProtectedPrefixHex": body[:protected_prefix].hex(),
            "SearchOffset": search_offset,
            "SearchVirtualAddress": search_va,
            "SearchSize": len(search_body),
            "SearchRegionSha256": sha256_bytes(search_body),
            "DecodedInstructions": len(instructions),
            "DecodedBytes": decoded_bytes,
            "ExactDecode": exact_decode,
            "RipRelativeDecoded": rip_relative,
            "ExternalControlTargets": external_control,
            "StockExtracted": actual_name in old_emitted,
            "CanonicalFixtureBuilt": compiled["returncode"] == 0,
            "CanonicalPath": str(candidate_dir),
            "FrozenForSearch": row["FuncName"] in FROZEN_SEARCH_COHORT,
            "DefInForStoke": root_registers(row["DefIn"], inputs=True),
            "LiveOutForStoke": root_registers(row["LiveOut"], inputs=False),
        }
        write_json(
            candidate_dir / "region.json",
            {
                **record,
                "instructions": [
                    {
                        "address": item.address,
                        "offset": item.address - va,
                        "size": item.size,
                        "bytes": item.bytes.hex(),
                        "mnemonic": item.mnemonic,
                        "operands": item.op_str,
                    }
                    for item in instructions
                ],
            },
        )
        records.append(record)

    fieldnames = list(records[0])
    with (run_dir / "candidate-universe.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        for record in records:
            writer.writerow({key: json.dumps(value) if isinstance(value, (list, dict)) else value for key, value in record.items()})

    old_errors = (OLD_EVIDENCE / "stoke-extract.stderr").read_text(errors="replace")
    reconcile = {
        "schema_version": 1,
        "defined_text_symbols": 1330,
        "bolt_functions": 1337,
        "extractor_attempts": 1332,
        "extractor_failures": old_errors.count("Cannot parse function"),
        "extractor_emitted": len(old_emitted),
        "unprofiled_csv_rows": sum(1 for _ in csv.DictReader(UNPROFILED_CSV.open())),
        "profiled_csv_rows": len(profiled),
        "frozen_structural_candidates": len(candidates),
        "frozen_search_cohort": FROZEN_SEARCH_COHORT,
        "identity_rule": "file offset + virtual address + size + SHA-256; names are descriptive only",
        "historical_raw_data_reused": True,
    }
    write_json(run_dir / "stoke-candidate-reconciliation.json", reconcile)
    recovered = run_dir / "commands-and-logs" / "historical-stoke-recovered"
    recovered.mkdir(parents=True, exist_ok=True)
    for name in ("stoke-extract.stderr", "stoke-extract.stdout", "prv-testcase.stdout", "prv-testcase.stderr", "prv-search.stdout", "prv-search.stderr", "prv-search-10m.stdout", "prv-search-10m.stderr"):
        source = OLD_EVIDENCE / name
        if source.exists():
            shutil.copy2(source, recovered / name)


def rewrite_extracted(text: str, actual_name: str, file_offset: int, va: int, size: int) -> str:
    text = re.sub(r"\bcandidate\b", actual_name, text)
    text = re.sub(r"(#! file-offset\s+)0x[0-9a-fA-F]+", rf"\g<1>{file_offset:#x}", text)
    text = re.sub(r"(#! rip-offset\s+)0x[0-9a-fA-F]+", rf"\g<1>{va:#x}", text)
    text = re.sub(r"(#! capacity\s+)\d+ bytes", rf"\g<1>{size} bytes", text)
    return text


def canonical_extract(run_dir: Path) -> None:
    candidate_rows = list(csv.DictReader((run_dir / "candidate-universe.csv").open()))
    remote_setup = invoke(["docker", "exec", "--user", "root", CONTAINER, "sh", "-c", f"rm -rf {REMOTE}; mkdir -p {REMOTE}"])
    if remote_setup["returncode"]:
        raise SystemExit("could not prepare STOKE container")
    results = []
    for index, row in enumerate(candidate_rows):
        local_dir = Path(row["CanonicalPath"])
        fixture = local_dir / "fixture.elf"
        remote_dir = f"{REMOTE}/{index:02d}-{safe_name(row['FuncName'])}"
        invoke(["docker", "exec", "--user", "root", CONTAINER, "mkdir", "-p", remote_dir])
        copied = invoke(["docker", "cp", str(fixture), f"{CONTAINER}:{remote_dir}/fixture.elf"])
        extraction = docker(["extract", "-i", f"{remote_dir}/fixture.elf", "-o", f"{remote_dir}/bins"])
        candidate_s = f"{remote_dir}/bins/candidate.s"
        fetched = invoke(["docker", "exec", CONTAINER, "sh", "-c", f"test -s {candidate_s} && cat {candidate_s}"])
        status = "canonical_extracted" if fetched["returncode"] == 0 else "canonical_extract_failed"
        if fetched["returncode"] == 0:
            rewritten = rewrite_extracted(
                fetched["stdout"], row["ActualSymbol"], int(row["SearchOffset"]),
                int(row["SearchVirtualAddress"]), int(row["SearchSize"]),
            )
            (local_dir / "canonical.s").write_text(rewritten)
            invoke(["docker", "cp", str(local_dir / "canonical.s"), f"{CONTAINER}:{remote_dir}/canonical.s"])
        write_json(local_dir / "extract.json", {"copy": copied, "extract": extraction, "fetch": fetched, "status": status, "remote_dir": remote_dir})
        results.append({"FuncName": row["FuncName"], "status": status, "remote_dir": remote_dir, "diagnostic": extraction["stderr"].strip()})
    write_json(run_dir / "stoke-canonical-extraction.json", {"schema_version": 1, "results": results})


def state_construction(run_dir: Path) -> None:
    candidate_rows = list(csv.DictReader((run_dir / "candidate-universe.csv").open()))
    extraction = {row["FuncName"]: row for row in json.loads((run_dir / "stoke-canonical-extraction.json").read_text())["results"]}
    funnel = []
    for index, row in enumerate(candidate_rows):
        name = row["FuncName"]
        base = {
            "candidate": name,
            "selected": True,
            "stock_extraction": "accepted" if row["StockExtracted"] == "True" else "representation_failure",
            "canonical_export": extraction[name]["status"],
            "state_construction": "not_run",
            "search": "not_run",
            "testcase_check": "not_run",
            "verification": "not_run",
            "emission": "not_run",
            "installation": "not_run",
            "final_acceptance": "not_run",
            "reason": "",
        }
        if extraction[name]["status"] != "canonical_extracted":
            base["state_construction"] = "blocked_by_representation"
            base["reason"] = extraction[name]["diagnostic"]
            funnel.append(base)
            continue
        if row["RipRelativeDecoded"] == "True" or json.loads(row["ExternalControlTargets"]):
            base["state_construction"] = "blocked_by_unresolved_reference"
            base["reason"] = "canonical bytes decoded, but rebased RIP/external control semantics are not installable without linking"
            funnel.append(base)
            continue
        remote_dir = extraction[name]["remote_dir"]
        def_in = json.loads(row["DefInForStoke"])
        live_out = json.loads(row["LiveOutForStoke"])
        effects = []
        if row["StackOut"] == "1":
            effects.append("--stack_out")
        if row["HeapOut"] == "1":
            effects.append("--heap_out")
        command = [
            "testcase", "--target", f"{remote_dir}/canonical.s",
            "--def_in", reg_set(def_in), "--live_out", reg_set(live_out), *effects,
            "--max_testcases", "64", "--max_attempts", "512", "--max_memory", "4096",
            "--seed", str(index + 1), "-o", f"{remote_dir}/states.tc",
        ]
        result = docker(command, timeout=180)
        local_dir = Path(row["CanonicalPath"])
        write_json(local_dir / "states.json", {"command": command, "result": result, "contract": {"def_in": def_in, "live_out": live_out, "stack_out": row["StackOut"] == "1", "heap_out": row["HeapOut"] == "1"}})
        base["state_construction"] = "accepted" if result["returncode"] == 0 else ("timeout" if result["timed_out"] else "failed")
        base["reason"] = (result["stderr"] or result["stdout"]).strip()
        funnel.append(base)

    fields = list(funnel[0])
    with (run_dir / "stage-funnel.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(funnel)


def environment_manifest(run_dir: Path) -> None:
    commands = {
        "container": ["docker", "inspect", CONTAINER],
        "image": ["docker", "image", "inspect", "stanfordpl/stoke:latest"],
        "stoke_hashes": ["docker", "exec", CONTAINER, "sha256sum", "/tmp/stoke/bin/stoke", "/tmp/stoke/bin/stoke_search", "/tmp/stoke/bin/stoke_testcase", "/tmp/stoke/bin/stoke_debug_verify", "/tmp/stoke/bin/stoke_replace"],
        "solver_links": ["docker", "exec", CONTAINER, "ldd", "/tmp/stoke/bin/stoke_debug_verify"],
        "compiler": ["docker", "exec", CONTAINER, "gcc", "--version"],
        "os": ["docker", "exec", CONTAINER, "sh", "-c", "cat /etc/os-release; uname -a"],
    }
    results = {name: invoke(command) for name, command in commands.items()}
    write_json(run_dir / "stoke-environment.json", {"schema_version": 1, "results": results})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_RUN)
    parser.add_argument("--stage", choices=("prepare", "extract", "states", "environment", "all"), default="all")
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    if args.stage in ("prepare", "all"):
        canonical_prepare(run_dir)
    if args.stage in ("environment", "all"):
        environment_manifest(run_dir)
    if args.stage in ("extract", "all"):
        canonical_extract(run_dir)
    if args.stage in ("states", "all"):
        state_construction(run_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
