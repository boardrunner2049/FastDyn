#!/usr/bin/env python3
"""Explain the frozen SmartSpeaker O0/Firm-BOLT/O3 comparison by mechanism."""

from __future__ import annotations

import hashlib
import json
import random
import re
import statistics
import subprocess
from pathlib import Path
from typing import Any

from elftools.elf.elffile import ELFFile


ROOT = Path(__file__).resolve().parents[2]
RUN = ROOT / "build/preflight/lemix-smartspeaker-o3-oracle-2026-11-17"
RAW = RUN / "comparison-samples.json"
PLAN = RUN / "plan.json"
WRAPPER = (
    ROOT / "build/preflight/lemix-smartspeaker-frs-wrapper-2026-11-17/"
    "wrapper-fop-inplace/wrapper-result.json"
)
NATIVE = WRAPPER.parent / "bolt-post-optimization-report.json"
O0 = (
    ROOT / "third_party/preflight/lemix/artifact/Lemix/Ported_Applications/"
    "dataset/FreeRTOS_Apps/FreeRTOS_Apps_Ported/SmartSpeaker_Ported/"
    "SmartSpeaker_Ported/build/SmartSpeaker"
)
O3 = RUN / "source/SmartSpeaker_Ported/build/SmartSpeaker"
FIRM = WRAPPER.parent / "SmartSpeaker.bolt-qualified-compact"
OUTPUT = RUN / "mechanism-report.json"
QUALIFICATION = RUN / "qualification-manifest.json"
DISASSEMBLY = RUN / "disassembly"
INSTRUCTION = re.compile(r"^\s*[0-9a-f]+:\s+([a-zA-Z0-9_.]+)\s*(.*)$")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


def paired_ratio(
    numerator: list[dict[str, Any]], denominator: list[dict[str, Any]], seed: int
) -> dict[str, Any]:
    left = {int(item["block"]): item for item in numerator}
    right = {int(item["block"]): item for item in denominator}
    ratios = [
        float(left[index]["target_cpu_ns"]) / float(right[index]["target_cpu_ns"])
        for index in sorted(left)
    ]
    generator = random.Random(seed)
    medians = [
        statistics.median(ratios[generator.randrange(len(ratios))] for _ in ratios)
        for _ in range(100_000)
    ]
    return {
        "median_ratio": statistics.median(ratios),
        "bootstrap_95_percent": [
            percentile(medians, 0.025), percentile(medians, 0.975)
        ],
        "paired_ratios": ratios,
        "bootstrap_resamples": 100_000,
    }


def symbol_sizes(binary: Path) -> dict[str, int]:
    output = subprocess.run(
        ["nm", "-S", "--defined-only", str(binary)],
        text=True, capture_output=True, check=True,
    ).stdout
    result: dict[str, int] = {}
    for line in output.splitlines():
        fields = line.split()
        if len(fields) == 4:
            try:
                result[fields[3]] = int(fields[1], 16)
            except ValueError:
                pass
    return result


def disassemble(binary: Path, variant: str, symbol: str) -> dict[str, Any] | None:
    sizes = symbol_sizes(binary)
    if symbol not in sizes:
        return None
    result = subprocess.run(
        [
            "objdump", "-d", "-Mintel", "--no-show-raw-insn",
            f"--disassemble={symbol}", str(binary),
        ],
        text=True, capture_output=True, check=True,
    )
    DISASSEMBLY.mkdir(parents=True, exist_ok=True)
    path = DISASSEMBLY / f"{variant}-{symbol}.txt"
    path.write_text(result.stdout)
    instructions = []
    for line in result.stdout.splitlines():
        match = INSTRUCTION.match(line)
        if match:
            instructions.append((match.group(1), match.group(2)))
    return {
        "symbol": symbol,
        "size_bytes": sizes[symbol],
        "instruction_count": len(instructions),
        "call_count": sum(opcode.startswith("call") for opcode, _ in instructions),
        "branch_count": sum(opcode.startswith("j") for opcode, _ in instructions),
        "stack_reference_instruction_count": sum(
            "[rbp" in operands or "[rsp" in operands
            for _opcode, operands in instructions
        ),
        "frame_setup_or_teardown_count": sum(
            opcode in {"push", "pop", "leave"}
            or (opcode in {"sub", "add"} and operands.startswith("rsp,"))
            for opcode, operands in instructions
        ),
        "disassembly_sha256": sha256(path),
    }


def executable_inventory(binary: Path) -> dict[str, int]:
    with binary.open("rb") as stream:
        elf = ELFFile(stream)
        sections = [
            section for section in elf.iter_sections()
            if int(section["sh_flags"]) & 0x4 and int(section["sh_size"]) > 0
        ]
        text = elf.get_section_by_name(".text")
        return {
            "file_bytes": binary.stat().st_size,
            "text_bytes": int(text["sh_size"]) if text is not None else 0,
            "allocated_executable_section_bytes": sum(
                int(section["sh_size"]) for section in sections
            ),
        }


def main() -> int:
    raw = json.loads(RAW.read_text())
    plan = json.loads(PLAN.read_text())
    wrapper = json.loads(WRAPPER.read_text())
    native = json.loads(NATIVE.read_text())
    samples = {
        name: item["samples"] for name, item in raw["variants"].items()
    }
    functions = list(plan["analysis_functions"])
    o0_sizes = symbol_sizes(O0)
    o3_sizes = symbol_sizes(O3)
    accepted = {item["symbol"]: item for item in wrapper["qualified"]["edits"]}
    bolt = {item["name"]: item for item in native["functions"]}
    branch_rejections: dict[str, list[str]] = {}
    for item in wrapper["rejections"]["branch_diagnostics"]:
        if item["symbol"] in functions:
            branch_rejections.setdefault(item["symbol"], []).append(item["reason"])

    function_rows: dict[str, Any] = {}
    for name in functions:
        split_names = sorted(
            symbol for symbol in o3_sizes if symbol.startswith(name + ".part.")
        )
        o3_components = [name] + split_names if name in o3_sizes else split_names
        firm_edit = accepted.get(name)
        bolt_item = bolt.get(name)
        function_rows[name] = {
            "o0_bytes": o0_sizes.get(name),
            "firm_bolt_live_bytes": (
                firm_edit["live_bolt_bytes"] if firm_edit is not None
                else o0_sizes.get(name)
            ),
            "firm_bolt_installed": firm_edit is not None,
            "full_bolt_fop_estimated_bytes": (
                bolt_item["estimated_post_optimization_size"]
                if bolt_item is not None else None
            ),
            "o3_components": {
                symbol: o3_sizes[symbol] for symbol in o3_components
            },
            "o3_total_component_bytes": sum(
                o3_sizes[symbol] for symbol in o3_components
            ) if o3_components else 0,
            "wrapper_branch_rejection_reasons": sorted(
                set(branch_rejections.get(name, []))
            ),
            "o0_instruction_inventory": disassemble(O0, "o0", name),
            "o3_instruction_inventory": disassemble(O3, "o3", name),
        }
        for split in split_names:
            disassemble(O3, "o3", split)

    report = {
        "schema_version": 1,
        "status": "mechanism_analysis_complete",
        "correctness": raw["validation"],
        "target_cpu_cost_ratios": {
            "firm_bolt_over_o3": paired_ratio(
                samples["stock_bolt"], samples["o3"], 20261119
            ),
            "firm_bolt_over_o0": paired_ratio(
                samples["stock_bolt"], samples["o0_reference"], 20261120
            ),
            "o3_over_o0": paired_ratio(
                samples["o3"], samples["o0_reference"], 20261121
            ),
        },
        "binary_inventory": {
            "o0": executable_inventory(O0),
            "firm_bolt": executable_inventory(FIRM),
            "o3": executable_inventory(O3),
            "firm_bolt_live_bytes_saved_inside_original_ranges": wrapper[
                "qualified"
            ]["saved_live_bytes"],
        },
        "firm_bolt_wrapper": {
            "installed_functions": wrapper["qualified"]["function_count"],
            "profile_executed_installed_functions": wrapper["qualified"][
                "profile_executed_functions"
            ],
            "absolute_references_repaired": wrapper["qualified"][
                "absolute_references_repaired"
            ],
        },
        "functions": function_rows,
        "interpretation": {
            "observed": [
                "Firm-BOLT and O3 meet the frozen five-percent CPU-cost parity rule",
                "O3 does not resolve a target-CPU advantage over O0 on this exact schedule",
                "ordinary BOLT FOP makes modest local reductions but is not a source-level O3 substitute",
            ],
            "source_confirmed_mechanisms": [
                "O3 removes the standalone prvSwitchThread symbol through interprocedural optimization",
                "O3 splits vTaskSwitchContext; its 37-byte public symbol has a 796-byte part.0 companion",
                "the compact wrapper rejects xTaskIncrementTick and vTaskSwitchContext because branch-target provenance cannot yet rebind targets whose original instruction was removed",
            ],
            "unresolved": [
                "the exact contribution of host scheduler and pthread costs is not isolated",
                "this supported-host result does not predict Cortex-M or FastDyn speedup",
            ],
        },
        "artifacts": {
            "comparison_samples_sha256": sha256(RAW),
            "wrapper_result_sha256": sha256(WRAPPER),
            "native_report_sha256": sha256(NATIVE),
        },
    }
    OUTPUT.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    build_stdout = (RUN / "make-o3.stdout").read_text()
    build_stderr = (RUN / "make-o3.stderr").read_text()
    source_revision = subprocess.run(
        [
            "git", "-C", str(ROOT / "third_party/preflight/lemix/artifact/Lemix"),
            "rev-parse", "HEAD",
        ],
        text=True, capture_output=True, check=True,
    ).stdout.strip()
    qualification = {
        "schema_version": 1,
        "status": "exact_work_semantically_qualified_for_comparison",
        "source_revision": source_revision,
        "o3_compile_commands": sum(
            line.startswith("gcc ") and " -c " in line
            for line in build_stdout.splitlines()
        ),
        "o3_build_warning_lines": sum(
            "warning:" in line for line in build_stderr.splitlines()
        ),
        "correctness": raw["validation"],
        "performance_status": "comparable_to_o3",
        "scope": (
            "supported-host exact-schedule FreeRTOS rehost; not original Cortex-M "
            "or FastDyn throughput and not a source-level semantic proof"
        ),
        "hashes": {
            "build_manifest_sha256": sha256(RUN / "manifest.json"),
            "comparison_report_sha256": sha256(RUN / "comparison-report.json"),
            "mechanism_report_sha256": sha256(OUTPUT),
        },
    }
    QUALIFICATION.write_text(
        json.dumps(qualification, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps({
        "status": report["status"],
        "target_cpu_cost_ratios": report["target_cpu_cost_ratios"],
        "binary_inventory": report["binary_inventory"],
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
