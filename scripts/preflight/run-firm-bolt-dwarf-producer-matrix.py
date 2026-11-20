#!/usr/bin/env python3
"""Qualify FirmBOLT debug rewriting across Cortex-M DWARF producers/versions."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/preflight/fixtures/firm_bolt_dwarf_fixture.c"
ARANGES = ROOT / "tests/preflight/fixtures/firm_bolt_dwarf_fixture_aranges.s"
LINKER = ROOT / "tests/preflight/fixtures/firm_bolt_dwarf_fixture.ld"
LLVM_BUILD = ROOT / "third_party/preflight/llvm-build/bin"
LLD_BUILD = ROOT / "third_party/preflight/lld-build/bin"
DEFAULT_GCC = Path(
    "/data/arm-gnu-toolchain-13.3.rel1-x86_64-arm-none-eabi/bin/"
    "arm-none-eabi-gcc"
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_tool(value: str | None, fallback: Path, name: str) -> Path:
    if value:
        # LLD selects its driver from argv[0]; preserve an ld.lld symlink's
        # basename instead of resolving it to the generic `lld` dispatcher.
        path = (
            (Path.cwd() / value).absolute()
            if not Path(value).is_absolute()
            else Path(value)
        )
    elif fallback.exists():
        path = fallback.absolute()
    elif found := shutil.which(name):
        path = Path(found).absolute()
    else:
        raise RuntimeError(f"required tool not found: {name}")
    if not path.is_file():
        raise RuntimeError(f"required tool is not a file: {path}")
    return path


def run(command: list[str], log: Path, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    process = subprocess.run(command, text=True, capture_output=True)
    log.write_text(
        "$ " + " ".join(command) + "\n" + process.stdout + process.stderr,
        encoding="utf-8",
    )
    if check and process.returncode:
        raise RuntimeError(f"command failed ({process.returncode}); see {log}")
    return process


def verifier_counts(output: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for name, count in re.findall(
        r"error: (.+?) occurred ([0-9]+) time\(s\)\.", output
    ):
        counts[name] = int(count)
    return counts


def section_names(readelf: Path, image: Path, log: Path) -> list[str]:
    process = run([str(readelf), "--sections", str(image)], log)
    return sorted(set(re.findall(r"\] (\.debug_[^ ]+)", process.stdout)))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bolt")
    parser.add_argument("--clang")
    parser.add_argument("--arm-gcc")
    parser.add_argument("--lld")
    parser.add_argument("--llvm-mc")
    parser.add_argument("--llvm-dwp")
    parser.add_argument("--llvm-dwarfdump")
    parser.add_argument("--llvm-readelf")
    parser.add_argument(
        "--output",
        default=str(ROOT / "build/preflight/firmbolt-dwarf-producer-matrix"),
    )
    args = parser.parse_args()

    tools = {
        "bolt": resolve_tool(args.bolt, LLVM_BUILD / "llvm-bolt", "llvm-bolt"),
        "clang": resolve_tool(args.clang, Path("/usr/bin/clang-18"), "clang"),
        "gcc": resolve_tool(args.arm_gcc, DEFAULT_GCC, "arm-none-eabi-gcc"),
        "lld": resolve_tool(args.lld, LLD_BUILD / "ld.lld", "ld.lld"),
        "mc": resolve_tool(args.llvm_mc, LLVM_BUILD / "llvm-mc", "llvm-mc"),
        "dwp": resolve_tool(args.llvm_dwp, LLVM_BUILD / "llvm-dwp", "llvm-dwp"),
        "dwarfdump": resolve_tool(
            args.llvm_dwarfdump, LLVM_BUILD / "llvm-dwarfdump", "llvm-dwarfdump"
        ),
        "readelf": resolve_tool(
            args.llvm_readelf, LLVM_BUILD / "llvm-readelf", "llvm-readelf"
        ),
    }
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    logs = output / "commands-and-logs"
    logs.mkdir(exist_ok=True)

    versions: dict[str, str] = {}
    for name, tool in tools.items():
        process = run([str(tool), "--version"], logs / f"version-{name}.log", check=False)
        versions[name] = (process.stdout + process.stderr).splitlines()[0]

    aranges = output / "aranges.o"
    run(
        [str(tools["mc"]), "-filetype=obj", "-triple", "thumbv7m-none-eabi",
         "-mcpu=cortex-m3", str(ARANGES), "-o", str(aranges)],
        logs / "assemble-aranges.log",
    )

    cases: dict[str, Any] = {}
    case_specs = [
        (producer, version, False)
        for producer in ("clang", "gcc")
        for version in range(2, 6)
    ] + [(producer, 5, True) for producer in ("clang", "gcc")]
    for producer, version, split in case_specs:
        name = f"{producer}-dwarf{version}" + ("-split" if split else "")
        obj = output / f"{name}.o"
        reference = output / f"{name}.reference.elf"
        candidate = output / f"{name}.firmbolt.elf"
        dwo_output = output / f"{name}.dwo-output"
        compiler = tools[producer]
        command = [str(compiler)]
        if producer == "clang":
            command += ["--target=thumbv7m-none-eabi"]
        command += [
            "-mcpu=cortex-m3", "-mthumb", "-O2", "-g",
            f"-gdwarf-{version}", "-ffreestanding", "-fno-unwind-tables",
            "-fno-asynchronous-unwind-tables", "-c", str(FIXTURE),
            "-o", str(obj),
        ]
        if split:
            command.insert(-4, "-gsplit-dwarf")
        run(command, logs / f"compile-{name}.log")
        run(
            [str(tools["lld"]), "--emit-relocs", "-T", str(LINKER),
             str(obj), str(aranges), "-o", str(reference)],
            logs / f"link-{name}.log",
        )
        bolt_command = [
            str(tools["bolt"]), str(reference),
            "--firmbolt-cortex-m-cpu=cortex-m3", "--relocs",
            "--use-old-text", "--update-debug-sections", "--keep-aranges",
            "--always-convert-to-ranges", "--align-functions=1",
            "-o", str(candidate),
        ]
        if split:
            dwo_output.mkdir(exist_ok=True)
            bolt_command.insert(-2, f"--dwarf-output-path={dwo_output}")
        run(bolt_command, logs / f"firmbolt-{name}.log")
        verify_options = ["--verify"] + (["--dwo"] if split else [])
        reference_verify = run(
            [str(tools["dwarfdump"]), *verify_options, str(reference)],
            logs / f"verify-reference-{name}.log", check=False,
        )
        candidate_verify = run(
            [str(tools["dwarfdump"]), *verify_options, str(candidate)],
            logs / f"verify-candidate-{name}.log", check=False,
        )
        reference_output = reference_verify.stdout + reference_verify.stderr
        candidate_output = candidate_verify.stdout + candidate_verify.stderr
        reference_counts = verifier_counts(reference_output)
        candidate_counts = verifier_counts(candidate_output)
        reference_sections = section_names(
            tools["readelf"], reference, logs / f"sections-reference-{name}.log"
        )
        candidate_sections = section_names(
            tools["readelf"], candidate, logs / f"sections-candidate-{name}.log"
        )
        reference_dwo = obj.with_suffix(".dwo") if split else None
        candidate_dwos = sorted(dwo_output.glob("*.dwo")) if split else []
        reference_dwo_sections = (
            section_names(
                tools["readelf"], reference_dwo,
                logs / f"sections-reference-dwo-{name}.log",
            )
            if reference_dwo else []
        )
        candidate_dwo_sections = (
            section_names(
                tools["readelf"], candidate_dwos[0],
                logs / f"sections-candidate-dwo-{name}.log",
            )
            if len(candidate_dwos) == 1 else []
        )
        accepted = (
            reference_verify.returncode == candidate_verify.returncode
            and reference_counts == candidate_counts
            and set(reference_sections).issubset(candidate_sections)
            and (not split or (
                reference_dwo is not None
                and reference_dwo.is_file()
                and len(candidate_dwos) == 1
                and set(reference_dwo_sections).issubset(
                    candidate_dwo_sections
                )
            ))
        )
        if version == 5:
            accepted = accepted and candidate_verify.returncode == 0
        cases[name] = {
            "accepted": accepted,
            "producer": producer,
            "dwarf_version": version,
            "split_dwarf": split,
            "reference_sha256": sha256(reference),
            "candidate_sha256": sha256(candidate),
            "reference_dwo_sha256": (
                sha256(reference_dwo) if reference_dwo else None
            ),
            "candidate_dwo_sha256": (
                sha256(candidate_dwos[0]) if len(candidate_dwos) == 1
                else None
            ),
            "reference_verifier_returncode": reference_verify.returncode,
            "candidate_verifier_returncode": candidate_verify.returncode,
            "reference_verifier_counts": reference_counts,
            "candidate_verifier_counts": candidate_counts,
            "reference_debug_sections": reference_sections,
            "candidate_debug_sections": candidate_sections,
            "reference_dwo_debug_sections": reference_dwo_sections,
            "candidate_dwo_debug_sections": candidate_dwo_sections,
        }

    # A DWP is not equivalent to reading independent DWO files: its CU index
    # slices shared sections, including strings and location lists, for each
    # unit.  Use two compilation units so the second contribution has nonzero
    # section offsets and catches accidental skeleton/split-unit aliasing.
    for producer in ("clang", "gcc"):
        name = f"{producer}-dwarf5-dwp"
        compiler = tools[producer]
        objects = [output / f"{name}-main.o", output / f"{name}-aux.o"]
        for role, obj in zip(("main", "aux"), objects):
            command = [str(compiler)]
            if producer == "clang":
                command += ["--target=thumbv7m-none-eabi"]
            command += [
                "-mcpu=cortex-m3", "-mthumb", "-O2", "-g", "-gdwarf-5",
                "-gsplit-dwarf", "-ffreestanding", "-fno-unwind-tables",
                "-fno-asynchronous-unwind-tables",
            ]
            if role == "aux":
                command += [
                    "-Dworker=worker_aux", "-DReset_Handler=Aux_Handler",
                    "-Dg_pfnVectors=g_aux_vectors",
                ]
            command += ["-c", str(FIXTURE), "-o", str(obj)]
            run(command, logs / f"compile-{name}-{role}.log")

        reference = output / f"{name}.reference.elf"
        reference_dwp = output / f"{name}.reference.dwp"
        candidate = output / f"{name}.firmbolt.elf"
        dwo_output = output / f"{name}.dwo-output"
        dwo_output.mkdir(exist_ok=True)
        run(
            [str(tools["lld"]), "--emit-relocs", "-T", str(LINKER),
             *(str(obj) for obj in objects), str(aranges), "-o", str(reference)],
            logs / f"link-{name}.log",
        )
        reference_dwos = [obj.with_suffix(".dwo") for obj in objects]
        run(
            [str(tools["dwp"]), *(str(dwo) for dwo in reference_dwos),
             "-o", str(reference_dwp)],
            logs / f"package-{name}.log",
        )
        run(
            [str(tools["bolt"]), str(reference), f"--dwp={reference_dwp}",
             "--firmbolt-cortex-m-cpu=cortex-m3", "--relocs",
             "--use-old-text", "--update-debug-sections",
             f"--dwarf-output-path={dwo_output}", "--keep-aranges",
             "--always-convert-to-ranges", "--align-functions=1",
             "-o", str(candidate)],
            logs / f"firmbolt-{name}.log",
        )
        reference_verify = run(
            [str(tools["dwarfdump"]), "--verify", str(reference_dwp)],
            logs / f"verify-reference-{name}.log", check=False,
        )
        candidate_verify = run(
            [str(tools["dwarfdump"]), "--verify", "--dwo", str(candidate)],
            logs / f"verify-candidate-{name}.log", check=False,
        )
        candidate_dump = run(
            [str(tools["dwarfdump"]), "--debug-info", "--dwo", str(candidate)],
            logs / f"dump-candidate-{name}.log", check=False,
        )
        reference_output = reference_verify.stdout + reference_verify.stderr
        candidate_output = candidate_verify.stdout + candidate_verify.stderr
        reference_sections = section_names(
            tools["readelf"], reference, logs / f"sections-reference-{name}.log"
        )
        candidate_sections = section_names(
            tools["readelf"], candidate, logs / f"sections-candidate-{name}.log"
        )
        reference_dwp_sections = section_names(
            tools["readelf"], reference_dwp,
            logs / f"sections-reference-dwp-{name}.log",
        )
        candidate_dwos = sorted(dwo_output.glob("*.dwo"))
        candidate_dwo_sections: dict[str, list[str]] = {}
        for dwo in candidate_dwos:
            candidate_dwo_sections[dwo.name] = section_names(
                tools["readelf"], dwo,
                logs / f"sections-candidate-{name}-{dwo.name}.log",
            )
        candidate_dwo_section_union = {
            section
            for sections in candidate_dwo_sections.values()
            for section in sections
        }
        reference_payload_sections = set(reference_dwp_sections) - {
            ".debug_cu_index"
        }
        expected_dwo_names = {
            f"{obj.with_suffix('.dwo').name}0.dwo" for obj in objects
        }
        resolved_dwo_compile_units = (
            candidate_dump.stdout + candidate_dump.stderr
        ).count("DW_TAG_compile_unit")
        accepted = (
            reference_verify.returncode == 0
            and candidate_verify.returncode == 0
            and candidate_dump.returncode == 0
            and not verifier_counts(reference_output)
            and not verifier_counts(candidate_output)
            and set(reference_sections).issubset(candidate_sections)
            and {dwo.name for dwo in candidate_dwos} == expected_dwo_names
            and reference_payload_sections.issubset(
                candidate_dwo_section_union
            )
            and resolved_dwo_compile_units == 2
        )
        cases[name] = {
            "accepted": accepted,
            "producer": producer,
            "dwarf_version": 5,
            "split_dwarf": True,
            "packaged_dwp_input": True,
            "compilation_units": 2,
            "reference_sha256": sha256(reference),
            "reference_dwp_sha256": sha256(reference_dwp),
            "reference_dwo_sha256": {
                dwo.name: sha256(dwo) for dwo in reference_dwos
            },
            "candidate_sha256": sha256(candidate),
            "candidate_dwo_sha256": {
                dwo.name: sha256(dwo) for dwo in candidate_dwos
            },
            "reference_verifier_returncode": reference_verify.returncode,
            "candidate_verifier_returncode": candidate_verify.returncode,
            "reference_verifier_counts": verifier_counts(reference_output),
            "candidate_verifier_counts": verifier_counts(candidate_output),
            "reference_debug_sections": reference_sections,
            "candidate_debug_sections": candidate_sections,
            "reference_dwp_debug_sections": reference_dwp_sections,
            "candidate_dwo_debug_sections": candidate_dwo_sections,
            "candidate_resolved_dwo_compile_units": resolved_dwo_compile_units,
        }

    report = {
        "schema_version": 1,
        "status": "accepted" if all(case["accepted"] for case in cases.values()) else "rejected",
        "scope": "ELF32 Cortex-M3 debug preservation; no performance claim",
        "host": platform.platform(),
        "fixtures": {path.name: sha256(path) for path in (FIXTURE, ARANGES, LINKER)},
        "tools": {
            name: {"path": str(path), "sha256": sha256(path), "version": versions[name]}
            for name, path in tools.items()
        },
        "cases": cases,
    }
    report_path = output / "report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(report_path)
    print(report["status"])
    return 0 if report["status"] == "accepted" else 1


if __name__ == "__main__":
    raise SystemExit(main())
