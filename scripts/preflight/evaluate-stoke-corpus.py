#!/usr/bin/env python3
"""Run the pinned STOKE engine over the recovered firmware-derived corpus."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import time
from pathlib import Path
from typing import Sequence


SPECS = {
    "crc8": {"def_in": "%rsp %rbp %rdi %rsi", "live_out": "%rax %rsp %rbp", "stack": True, "heap": False},
    "crc8dvbs2": {"def_in": "%rsp %rbp %rdi %rsi", "live_out": "%rax %rsp %rbp", "stack": True, "heap": False},
    "dshotcrc": {"def_in": "%rsp %rbp %rdi %rsi", "live_out": "%rax %rsp %rbp", "stack": True, "heap": False},
    "vListInitialise": {"def_in": "%rsp %rbp %rdi", "live_out": "%rsp %rbp", "stack": True, "heap": True},
    "vListInitialiseItem": {"def_in": "%rsp %rbp %rdi", "live_out": "%rsp %rbp", "stack": True, "heap": True},
    "vListInsertEnd": {"def_in": "%rsp %rbp %rdi %rsi", "live_out": "%rsp %rbp", "stack": True, "heap": True},
    "vListInsert": {"def_in": "%rsp %rbp %rdi %rsi", "live_out": "%rsp %rbp", "stack": True, "heap": True},
    "uxListRemove": {"def_in": "%rsp %rbp %rdi", "live_out": "%rax %rsp %rbp", "stack": True, "heap": True},
}


def invoke(command: Sequence[str], *, input_bytes: bytes | None = None, timeout: int = 120) -> dict[str, object]:
    before = time.perf_counter()
    try:
        result = subprocess.run(command, input=input_bytes, capture_output=True, timeout=timeout)
        return {
            "command": list(command),
            "returncode": result.returncode,
            "elapsed_seconds": time.perf_counter() - before,
            "stdout": result.stdout.decode("utf-8", "replace").splitlines(),
            "stderr": result.stderr.decode("utf-8", "replace").splitlines(),
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as error:
        return {
            "command": list(command),
            "returncode": None,
            "elapsed_seconds": time.perf_counter() - before,
            "stdout": (error.stdout or b"").decode("utf-8", "replace").splitlines(),
            "stderr": (error.stderr or b"").decode("utf-8", "replace").splitlines(),
            "timed_out": True,
        }


def docker(container: str, arguments: Sequence[str], *, timeout: int = 120) -> dict[str, object]:
    return invoke(["docker", "exec", container, "/tmp/stoke/bin/stoke", *arguments], timeout=timeout)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--container", default="fastdyn-stoke-build")
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--iterations", type=int, default=1_000_000)
    parser.add_argument("--wall-timeout", type=int, default=120)
    args = parser.parse_args(argv)
    binary = args.binary.resolve()
    work = args.work_dir.resolve()
    work.mkdir(parents=True, exist_ok=True)
    remote = "/tmp/fastdyn-stoke-eval"

    setup = invoke(["docker", "exec", "--user", "root", args.container, "sh", "-c", f"rm -rf {remote}; mkdir -m 0777 {remote}"])
    upload = invoke(
        ["docker", "exec", "-i", "--user", "root", args.container, "sh", "-c", f"dd of={remote}/corpus-O0 status=none; chmod 0755 {remote}/corpus-O0"],
        input_bytes=binary.read_bytes(),
    )
    extract = docker(args.container, ["extract", "-i", f"{remote}/corpus-O0", "-o", f"{remote}/bins"])
    if setup["returncode"] or upload["returncode"] or extract["returncode"]:
        raise RuntimeError("STOKE setup/extraction failed")

    results: dict[str, object] = {}
    for index, (name, spec) in enumerate(SPECS.items(), start=1):
        common = [
            "--target", f"{remote}/bins/{name}.s",
            "--def_in", "{ " + str(spec["def_in"]) + " }",
            "--live_out", "{ " + str(spec["live_out"]) + " }",
        ]
        effects = (["--stack_out"] if spec["stack"] else []) + (["--heap_out"] if spec["heap"] else [])
        testcase_path = f"{remote}/{name}.tc"
        testcase = docker(
            args.container,
            ["testcase", *common, *effects, "--max_testcases", "64", "--max_attempts", "512", "--max_memory", "4096", "--seed", str(index), "-o", testcase_path],
            timeout=args.wall_timeout,
        )
        entry: dict[str, object] = {"spec": spec, "testcase": testcase}
        if testcase["returncode"] != 0 or testcase["timed_out"]:
            entry["status"] = "unsupported" if not testcase["timed_out"] else "timeout"
            results[name] = entry
            continue
        result_path = f"{remote}/{name}-result.s"
        search = docker(
            args.container,
            [
                "search", "--out", result_path, *common, *effects,
                "--testcases", testcase_path,
                "--training_set", "{ 0 ... 15 }", "--test_set", "{ 16 ... 63 }",
                "--seed", str(index), "--initial_instruction_number", "8",
                "--timeout_iterations", str(args.iterations), "--cycle_timeout", str(max(1, args.iterations // 4)),
                "--strategy", "hold_out", "--cost", "correctness + latency", "--postprocessing", "simple",
                "--statistics_interval", str(max(1, args.iterations // 10)),
            ],
            timeout=args.wall_timeout,
        )
        entry["search"] = search
        cat_result = invoke(["docker", "exec", args.container, "sh", "-c", f"test -s {result_path} && cat {result_path}"], timeout=10)
        candidate = cat_result["stdout"]
        entry["candidate"] = candidate
        if not candidate:
            combined = "\n".join(search["stdout"] + search["stderr"])
            if search["timed_out"]:
                entry["status"] = "timeout"
            elif "unable to discover a new rewrite" in combined.lower():
                entry["status"] = "no_candidate"
            else:
                entry["status"] = "unknown"
            results[name] = entry
            continue
        verify = docker(
            args.container,
            [
                "debug", "verify", *common, *effects, "--rewrite", result_path,
                "--testcases", testcase_path, "--training_set", "{ 0 ... 63 }", "--test_set", "{ 0 ... 63 }",
                "--strategy", "bounded", "--bound", "8", "--solver_timeout", "10000", "--seed", str(index),
            ],
            timeout=args.wall_timeout,
        )
        entry["bounded_verification"] = verify
        verification_text = "\n".join(verify["stdout"] + verify["stderr"]).lower()
        entry["status"] = "accepted_by_stoke_bounded_model" if "equivalent: yes" in verification_text else "unknown"
        results[name] = entry

    revision = invoke(["docker", "exec", args.container, "git", "-C", "/tmp/stoke", "rev-parse", "HEAD"])
    report = {
        "schema_version": 1,
        "classification": "pinned_stoke_recovered_corpus",
        "engine_revision": revision["stdout"][0] if revision["stdout"] else None,
        "binary": {"path": str(binary), "sha256": hashlib.sha256(binary.read_bytes()).hexdigest()},
        "budget": {"iterations_per_function": args.iterations, "wall_timeout_seconds": args.wall_timeout, "testcases": 64},
        "adapter_note": "BOLT alias-expanded register sets were normalized to ABI arguments, return value, frame/stack state, and declared heap effects.",
        "setup": setup,
        "upload": upload,
        "extract": extract,
        "results": results,
        "claims": {"x86_64_native_only": True, "cortex_m": False, "fastdyn_contract_checked": False},
    }
    path = work / "report.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(path), "statuses": {name: item["status"] for name, item in results.items()}}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
