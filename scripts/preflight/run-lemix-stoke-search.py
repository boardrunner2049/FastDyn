#!/usr/bin/env python3
"""Run the frozen six-case STOKE legality and matched-budget search study."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / (
    "third_party/preflight/lemix/artifact/Lemix/Ported_Applications/dataset/"
    "FreeRTOS_Apps/FreeRTOS_Apps_Ported/SmartSpeaker_Ported/SmartSpeaker_Ported"
)
REFERENCE = APP / "build/SmartSpeaker"
PRELOAD = ROOT / "build/preflight/lemix-smartspeaker/lemix_mmio_map.so"
DEFAULT_RUN = ROOT / "build/preflight/lemix-o0-rerun-2026-11-16"
CONTAINER = "fastdyn-stoke-build"
STOKE = "/tmp/stoke/bin/stoke"
EVENT = b"[JS:Debug] Interrupt_Handler_Task running"
SEEDS = [101, 202, 303, 404]
COHORT = [
    "prvGetThreadFromTask/1",
    "vListInitialiseItem",
    "IWDG_ReloadCounter",
    "WWDG_IRQHandler",
    "vApplicationTickHook",
    "_fini",
]


def invoke(command: list[str], *, timeout: int = 900, input_bytes: bytes | None = None) -> dict[str, Any]:
    started = time.monotonic()
    try:
        result = subprocess.run(command, input=input_bytes, capture_output=True, timeout=timeout)
        return {
            "argv": command,
            "returncode": result.returncode,
            "elapsed_seconds": time.monotonic() - started,
            "stdout": result.stdout.decode("utf-8", "replace"),
            "stderr": result.stderr.decode("utf-8", "replace"),
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as error:
        return {
            "argv": command,
            "returncode": None,
            "elapsed_seconds": time.monotonic() - started,
            "stdout": (error.stdout or b"").decode("utf-8", "replace"),
            "stderr": (error.stderr or b"").decode("utf-8", "replace"),
            "timed_out": True,
        }


def docker(arguments: list[str], *, timeout: int = 900) -> dict[str, Any]:
    return invoke(["docker", "exec", CONTAINER, STOKE, *arguments], timeout=timeout)


def put_remote(local: Path, remote: str) -> dict[str, Any]:
    return invoke(
        ["docker", "exec", "-i", "--user", "root", CONTAINER, "sh", "-c", f"cat > {remote}"],
        input_bytes=local.read_bytes(),
    )


def get_remote(remote: str) -> dict[str, Any]:
    return invoke(["docker", "exec", CONTAINER, "sh", "-c", f"test -s {remote} && cat {remote}"])


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def safe_name(name: str) -> str:
    return name.replace("/", "_")


def reg_set(registers: list[str]) -> str:
    return "{ " + " ".join("%" + item for item in registers) + " }"


def effect_args(contract: dict[str, Any]) -> list[str]:
    result = []
    if contract["stack_out"]:
        result.append("--stack_out")
    if contract["heap_out"]:
        result.append("--heap_out")
    return result


def write_seeds(run_dir: Path) -> dict[str, Path]:
    accessor = run_dir / "stoke-canonical/prvGetThreadFromTask_1"
    list_item = run_dir / "stoke-canonical/vListInitialiseItem"
    accessor_fop = accessor / "fop.s"
    accessor_short = accessor / "short.s"
    accessor_frs = accessor / "frs.s"
    list_fop = list_item / "fop.s"
    accessor_fop.write_text("""  .text
  .globl prvGetThreadFromTask
  .type prvGetThreadFromTask, @function
#! file-offset 0xc428
#! rip-offset  0x40c428
#! capacity    29 bytes
.prvGetThreadFromTask:
  pushq %rbp
  movq %rsp, %rbp
  movq %rdi, -0x18(%rbp)
  movq %rdi, %rax
  movq (%rax), %rax
  movq %rax, -0x8(%rbp)
  addq $0x8, %rax
  popq %rbp
  retq
.size prvGetThreadFromTask, .-prvGetThreadFromTask
""")
    accessor_short.write_text("""  .text
  .globl prvGetThreadFromTask
  .type prvGetThreadFromTask, @function
#! file-offset 0xc428
#! rip-offset  0x40c428
#! capacity    29 bytes
.prvGetThreadFromTask:
  movq (%rdi), %rax
  addq $0x8, %rax
  retq
.size prvGetThreadFromTask, .-prvGetThreadFromTask
""")
    accessor_frs.write_text("""  .text
  .globl prvGetThreadFromTask
  .type prvGetThreadFromTask, @function
#! file-offset 0xc428
#! rip-offset  0x40c428
#! capacity    29 bytes
.prvGetThreadFromTask:
  pushq %rbp
  movq %rsp, %rbp
  movq %rdi, %rax
  movq (%rax), %rax
  addq $0x8, %rax
  popq %rbp
  retq
.size prvGetThreadFromTask, .-prvGetThreadFromTask
""")
    list_fop.write_text("""  .text
  .globl vListInitialiseItem
  .type vListInitialiseItem, @function
#! file-offset 0x31e7
#! rip-offset  0x4031e7
#! capacity    23 bytes
.vListInitialiseItem:
  pushq %rbp
  movq %rsp, %rbp
  movq %rdi, -0x8(%rbp)
  movq %rdi, %rax
  movq $0x0, 0x20(%rax)
  popq %rbp
  retq
.size vListInitialiseItem, .-vListInitialiseItem
""")
    return {
        "accessor_fop": accessor_fop,
        "accessor_short": accessor_short,
        "accessor_frs": accessor_frs,
        "list_fop": list_fop,
    }


def load_cases(run_dir: Path) -> dict[str, dict[str, Any]]:
    rows = {row["FuncName"]: row for row in csv.DictReader((run_dir / "candidate-universe.csv").open())}
    extracted = {
        row["FuncName"]: row
        for row in json.loads((run_dir / "stoke-canonical-extraction.json").read_text())["results"]
    }
    funnel = {row["candidate"]: row for row in csv.DictReader((run_dir / "stage-funnel.csv").open())}
    result = {}
    for name in COHORT:
        if funnel[name]["state_construction"] != "accepted":
            raise SystemExit(f"frozen search case lacks states: {name}")
        local_dir = Path(rows[name]["CanonicalPath"])
        states = json.loads((local_dir / "states.json").read_text())
        result[name] = {
            "row": rows[name],
            "remote_dir": extracted[name]["remote_dir"],
            "local_dir": local_dir,
            "contract": states["contract"],
        }
    return result


def run_verify(case: dict[str, Any], rewrite_name: str, strategy: str, label: str) -> dict[str, Any]:
    remote_dir = case["remote_dir"]
    contract = case["contract"]
    machine = f"{remote_dir}/{label}.machine.json"
    command = [
        "debug", "verify", "--target", f"{remote_dir}/canonical.s",
        "--rewrite", f"{remote_dir}/{rewrite_name}",
        "--def_in", reg_set(contract["def_in"]),
        "--live_out", reg_set(contract["live_out"]), *effect_args(contract),
        "--testcases", f"{remote_dir}/states.tc",
        "--training_set", "{ 0 ... 63 }", "--test_set", "{ 0 ... 63 }",
        "--strategy", strategy, "--bound", "1", "--solver_timeout", "60000",
        "--alias_strategy", "flat", "--seed", "1", "--machine_output", machine,
    ]
    result = docker(command, timeout=180)
    machine_result = get_remote(machine)
    combined = result["stdout"] + result["stderr"]
    return {
        "command": command,
        "result": result,
        "machine_output": machine_result["stdout"] if machine_result["returncode"] == 0 else None,
        "classification": "equivalent" if "Equivalent: yes" in combined else "counterexample" if "Equivalent: no" in combined else "unknown",
    }


def legality(run_dir: Path, cases: dict[str, dict[str, Any]], seeds: dict[str, Path]) -> dict[str, Any]:
    uploads = {}
    mapping = {
        "accessor_fop": ("prvGetThreadFromTask/1", "fop.s"),
        "accessor_short": ("prvGetThreadFromTask/1", "short.s"),
        "accessor_frs": ("prvGetThreadFromTask/1", "frs.s"),
        "list_fop": ("vListInitialiseItem", "fop.s"),
    }
    for key, (case_name, remote_name) in mapping.items():
        remote = f"{cases[case_name]['remote_dir']}/{remote_name}"
        uploads[key] = put_remote(seeds[key], remote)
    probes = {}
    for key, (case_name, remote_name) in mapping.items():
        for strategy in ("hold_out", "bounded"):
            label = f"legality-{key}-{strategy}"
            probes[label] = run_verify(cases[case_name], remote_name, strategy, label)
    report = {
        "schema_version": 1,
        "uploads": uploads,
        "probes": probes,
        "native_alias_probe": json.loads((run_dir / "accessor-harness/legality.json").read_text()),
        "contracts": {
            "stoke_declared": "register live-outs plus stack_out and heap_out; hold-out and loop-free bounded checks reported separately",
            "native_sysv": "ordinary and stack/heap-alias valid-address executions plus invalid-pointer SIGSEGV observation",
            "fastdyn_future": "fault attribution/event prefix is not expressed by these STOKE checks",
        },
    }
    write_json(run_dir / "stoke-legality.json", report)
    return report


def search_one(case_name: str, case: dict[str, Any], arm: str, seed: int, previous: str | None) -> dict[str, Any]:
    remote_dir = case["remote_dir"]
    contract = case["contract"]
    tag = f"{arm}-seed-{seed}"
    output = f"{remote_dir}/{tag}.result.s"
    machine = f"{remote_dir}/{tag}.machine.json"
    command = [
        "search", "--out", output, "--machine_output", machine,
        "--target", f"{remote_dir}/canonical.s",
        "--def_in", reg_set(contract["def_in"]),
        "--live_out", reg_set(contract["live_out"]), *effect_args(contract),
        "--testcases", f"{remote_dir}/states.tc",
        "--training_set", "{ 0 ... 31 }", "--test_set", "{ 32 ... 63 }",
        "--seed", str(seed), "--initial_instruction_number", "8",
        "--timeout_iterations", "2500000", "--cycle_timeout", "2500000",
        "--strategy", "hold_out", "--cost", "correctness + latency",
        "--postprocessing", "simple", "--statistics_interval", "250000",
        "--beta", "1", "--init", "previous" if previous else "zero",
    ]
    if previous:
        command.extend(["--previous", f"{remote_dir}/{previous}"])
    result = docker(command, timeout=600)
    machine_result = get_remote(machine)
    candidate_result = get_remote(output)
    machine_json = None
    if machine_result["returncode"] == 0:
        try:
            machine_json = json.loads(machine_result["stdout"])
        except json.JSONDecodeError:
            machine_json = None
    status = "candidate" if candidate_result["returncode"] == 0 else "timeout" if result["timed_out"] else "no_candidate" if "unable to discover a new rewrite" in (result["stdout"] + result["stderr"]).lower() else "unknown"
    verification = None
    if status == "candidate":
        verification = run_verify(case, f"{tag}.result.s", "bounded", f"{tag}-bounded")
        if verification["classification"] != "equivalent":
            status = "candidate_rejected"
    return {
        "case": case_name,
        "arm": arm,
        "seed": seed,
        "previous": previous,
        "status": status,
        "command": command,
        "result": result,
        "machine": machine_json,
        "machine_raw": machine_result["stdout"],
        "candidate": candidate_result["stdout"] if candidate_result["returncode"] == 0 else None,
        "bounded_verification": verification,
    }


def searches(run_dir: Path, cases: dict[str, dict[str, Any]], legality_report: dict[str, Any], workers: int) -> list[dict[str, Any]]:
    jobs: list[tuple[str, str, int, str | None]] = []
    for case_name in COHORT:
        for seed in SEEDS:
            jobs.append((case_name, "S1", seed, None))
    normalized = {
        "prvGetThreadFromTask/1": "fop.s",
        "vListInitialiseItem": "fop.s",
    }
    for case_name, previous in normalized.items():
        key = "accessor_fop" if case_name.startswith("prv") else "list_fop"
        bounded = legality_report["probes"][f"legality-{key}-bounded"]
        if bounded["classification"] == "equivalent":
            for seed in SEEDS:
                jobs.append((case_name, "S2", seed, previous))

    results = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(search_one, case_name, cases[case_name], arm, seed, previous): (case_name, arm, seed)
            for case_name, arm, seed, previous in jobs
        }
        for future in as_completed(futures):
            item = future.result()
            results.append(item)
            print(f"search {item['case']} {item['arm']} seed={item['seed']} status={item['status']}", flush=True)
            local = run_dir / "stoke-search" / safe_name(item["case"]) / f"{item['arm']}-seed-{item['seed']}"
            write_json(local / "result.json", item)
            if item["candidate"]:
                (local / "candidate.s").write_text(item["candidate"])
    results.sort(key=lambda item: (COHORT.index(item["case"]), item["arm"], item["seed"]))
    write_json(
        run_dir / "search-seeds-and-costs.json",
        {
            "schema_version": 1,
            "frozen_cohort": COHORT,
            "seeds": SEEDS,
            "proposal_limit_per_seed": 2_500_000,
            "cycle_limit_per_seed": 2_500_000,
            "beta": 1,
            "initial_instruction_number": 8,
            "training_states": [0, 31],
            "held_out_states": [32, 63],
            "S0": {
                "source": "recovered historical raw logs",
                "accessor": "four 2.5M-proposal attempts; no_candidate",
                "other_frozen_cases": "stock extraction unavailable",
            },
            "results": results,
        },
    )
    return results


def install_checked_seeds(run_dir: Path, cases: dict[str, dict[str, Any]]) -> dict[str, Any]:
    remote = "/tmp/lemix-o0-focused-rerun/installed"
    invoke(["docker", "exec", "--user", "root", CONTAINER, "mkdir", "-p", remote])
    reference_remote = f"{remote}/SmartSpeaker.reference"
    put_remote(REFERENCE, reference_remote)
    accessor_out = f"{remote}/SmartSpeaker.accessor"
    combined_out = f"{remote}/SmartSpeaker.accessor-list"
    first = docker([
        "replace", "-i", reference_remote, "-o", accessor_out,
        "--rewrite", f"{cases['prvGetThreadFromTask/1']['remote_dir']}/fop.s", "--input_offset",
    ])
    second = docker([
        "replace", "-i", accessor_out, "-o", combined_out,
        "--rewrite", f"{cases['vListInitialiseItem']['remote_dir']}/fop.s", "--input_offset",
    ])
    fetched = invoke(["docker", "cp", f"{CONTAINER}:{combined_out}", str(run_dir / "binaries/SmartSpeaker.stoke-installed-fop")])
    installed = run_dir / "binaries/SmartSpeaker.stoke-installed-fop"
    execution = None
    if fetched["returncode"] == 0:
        installed.chmod(0o755)
        execution = observe(installed, 5.0)
    report = {
        "schema_version": 1,
        "first_replace": first,
        "second_replace": second,
        "fetch": fetched,
        "installed_sha256": hashlib.sha256(installed.read_bytes()).hexdigest() if installed.exists() else None,
        "execution": execution,
        "scope": "checked BOLT-FOP normalized seeds installed through STOKE replacement; not a stochastic-search result",
    }
    write_json(run_dir / "stoke-installation.json", report)
    return report


def install_search_candidate(
    run_dir: Path, cases: dict[str, dict[str, Any]], results: list[dict[str, Any]]
) -> dict[str, Any]:
    accepted = [
        item for item in results
        if item["case"] == "_fini" and item["status"] == "candidate"
        and item["bounded_verification"]["classification"] == "equivalent"
    ]
    if not accepted:
        report = {"schema_version": 1, "status": "no_verified_search_candidate"}
        write_json(run_dir / "stoke-search-installation.json", report)
        return report
    chosen = accepted[0]
    candidate = run_dir / "stoke-search/_fini/S1-seed-101/candidate.s"
    remote_candidate = f"{cases['_fini']['remote_dir']}/installed-search-candidate.s"
    upload = put_remote(candidate, remote_candidate)
    remote_reference = f"{cases['_fini']['remote_dir']}/SmartSpeaker.reference"
    put_remote(REFERENCE, remote_reference)
    remote_output = f"{cases['_fini']['remote_dir']}/SmartSpeaker.search-installed"
    replacement = docker([
        "replace", "-i", remote_reference, "-o", remote_output,
        "--rewrite", remote_candidate, "--input_offset",
    ])
    installed = run_dir / "binaries/SmartSpeaker.stoke-search-fini"
    fetched = invoke(["docker", "cp", f"{CONTAINER}:{remote_output}", str(installed)])
    execution = None
    changed_offsets: list[int] = []
    if fetched["returncode"] == 0:
        installed.chmod(0o755)
        execution = observe(installed, 5.0)
        original_bytes = REFERENCE.read_bytes()
        installed_bytes = installed.read_bytes()
        changed_offsets = [
            index for index, (left, right) in enumerate(zip(original_bytes, installed_bytes))
            if left != right
        ]
    report = {
        "schema_version": 1,
        "status": "installed" if execution and execution["returncode"] == 2 else "failed",
        "source_case": chosen["case"],
        "source_arm": chosen["arm"],
        "source_seed": chosen["seed"],
        "bounded_verification": chosen["bounded_verification"]["classification"],
        "candidate_sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
        "upload": upload,
        "replacement": replacement,
        "fetch": fetched,
        "installed_sha256": hashlib.sha256(installed.read_bytes()).hexdigest() if installed.exists() else None,
        "changed_file_offsets": changed_offsets,
        "execution": execution,
        "performance_claim": "none; _fini is cold and this is an emission/installation acceptance case",
    }
    write_json(run_dir / "stoke-search-installation.json", report)
    return report


def update_stage_funnel(
    run_dir: Path,
    results: list[dict[str, Any]],
    search_installation: dict[str, Any],
) -> None:
    """Merge the frozen search results into the earlier extraction funnel.

    The extraction script intentionally stops at state construction.  Keeping
    the merge here preserves the distinction between representation/state
    failures and later search, proof, emission, or installation outcomes.
    """
    path = run_dir / "stage-funnel.csv"
    with path.open(newline="") as source:
        reader = csv.DictReader(source)
        fieldnames = reader.fieldnames
        if fieldnames is None:
            raise SystemExit("stage-funnel.csv has no header")
        rows = list(reader)

    by_case: dict[str, list[dict[str, Any]]] = {}
    for item in results:
        by_case.setdefault(item["case"], []).append(item)

    for row in rows:
        case_results = by_case.get(row["candidate"])
        if not case_results:
            continue
        arm_summary = []
        for arm in ("S1", "S2"):
            arm_results = [item for item in case_results if item["arm"] == arm]
            if arm_results:
                counts: dict[str, int] = {}
                for item in arm_results:
                    counts[item["status"]] = counts.get(item["status"], 0) + 1
                arm_summary.append(
                    arm + ":" + ",".join(f"{key}={counts[key]}" for key in sorted(counts))
                )
        row["search"] = ";".join(arm_summary)

        candidates = [item for item in case_results if item["candidate"]]
        row["testcase_check"] = (
            "candidate_reached_holdout=" + str(len(candidates)) if candidates else "not_reached"
        )
        verification = [
            (item.get("bounded_verification") or {}).get("classification")
            for item in candidates
        ]
        verification = [item for item in verification if item]
        if verification:
            counts: dict[str, int] = {}
            for item in verification:
                counts[item] = counts.get(item, 0) + 1
            row["verification"] = ",".join(
                f"{key}={counts[key]}" for key in sorted(counts)
            )
        else:
            row["verification"] = "not_reached"

        installed = (
            row["candidate"] == search_installation.get("source_case")
            and search_installation.get("status") == "installed"
        )
        row["emission"] = "emitted" if installed else "not_reached"
        row["installation"] = "installed" if installed else "not_reached"
        if installed:
            row["final_acceptance"] = "accepted_no_performance_claim"
        elif "counterexample" in verification:
            row["final_acceptance"] = "rejected_counterexample"
        else:
            row["final_acceptance"] = "no_verified_candidate"

    with path.open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def observe(path: Path, duration: float) -> dict[str, Any]:
    environment = dict(os.environ)
    environment["LD_PRELOAD"] = str(PRELOAD)
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen([str(path)], stdout=output, stderr=subprocess.STDOUT, env=environment, start_new_session=True)
        time.sleep(duration)
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGINT)
        process.wait(timeout=5)
        output.seek(0)
        content = output.read()
    return {"returncode": process.returncode, "dispatch_markers": content.count(EVENT), "output_bytes": len(content)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_RUN)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--skip-search", action="store_true")
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    seeds = write_seeds(run_dir)
    cases = load_cases(run_dir)
    legality_report = legality(run_dir, cases, seeds)
    if not args.skip_search:
        search_results = searches(run_dir, cases, legality_report, args.workers)
    else:
        search_results = json.loads((run_dir / "search-seeds-and-costs.json").read_text())["results"]
    install_checked_seeds(run_dir, cases)
    search_installation = install_search_candidate(run_dir, cases, search_results)
    update_stage_funnel(run_dir, search_results, search_installation)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
