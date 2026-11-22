"""Balanced stock-QEMU training diagnostic for two prequalified Pye images."""
import argparse
import json
import os
import platform
import random
import statistics
import subprocess
import sys
from pathlib import Path

from cold import digest
from session import stage

HERE = Path(__file__).resolve().parent
SEED = 901783
PAIRS = 24


def measure(work, original, relocated, qemu, out, labels=("original", "firmbolt")):
    reference, candidate = labels
    if reference == candidate or any(not label.isidentifier() for label in labels):
        raise ValueError("the two deployment labels must be distinct identifiers")
    out.mkdir(parents=True, exist_ok=False)
    if not int(Path("/proc/self/personality").read_text(), 16) & 0x40000:
        raise RuntimeError("setarch -R is required")
    cpu = max(os.sched_getaffinity(0))
    os.sched_setaffinity(0, {cpu})
    stage(work, "update-package-version", out / "input")
    rng = random.Random(SEED)
    orders = [(reference, candidate), (candidate, reference)] * (PAIRS // 2)
    rng.shuffle(orders)
    firmware = {reference: original, candidate: relocated}
    policy = {"scope": "stock QEMU, cacheless, training edit only", "seed": SEED, "pairs": PAIRS,
              "reference": reference, "candidate": candidate,
              "cpu": cpu, "host": platform.uname()._asdict(), "qemu_sha256": digest(qemu),
              "qemu_version": subprocess.check_output([qemu, "--version"], text=True).splitlines()[0],
              "firmware_sha256": {k: digest(f) for k, f in firmware.items()}, "orders": orders,
              "warmup_pairs": 1, "primary": "operational_ns", "interval": "paired percentile bootstrap, 2000 samples"}
    (out / "plan.json").write_text(json.dumps(policy, indent=2))
    with (out / "observations.jsonl").open("x") as log:
        for i, order in enumerate([(reference, candidate), *orders], -1):
            for arm in order:
                trial = out / f"pair-{i}-{arm}"
                proc = subprocess.run([sys.executable, HERE / "cold.py", "--stage", out / "input/stage.json",
                                       "--qemu", qemu, "--elf", firmware[arm], "--out", trial])
                result = json.loads((trial / "result.json").read_text())
                print(json.dumps({"block": i, "order": order, "arm": arm, "result": result}), file=log, flush=True)
                if proc.returncode or result["verdict"] != "PASS" or result["application"]["verdict"] != "PASS":
                    raise RuntimeError(f"failed application oracle in {trial}")
    summarize_recording(out)


def summarize_recording(out):
    """Replay the declared schedule and check the saved product outputs."""
    summary = out / "summary.json"
    summary.unlink(missing_ok=True)
    policy = json.loads((out / "plan.json").read_text())
    reference, candidate = policy.get("reference", "original"), policy.get("candidate", "firmbolt")
    pairs = policy["pairs"]
    if reference == candidate or any(not arm.isidentifier() for arm in (reference, candidate)):
        raise ValueError("invalid deployment labels")
    if pairs <= 0 or pairs % 2 or policy["warmup_pairs"] != 1:
        raise ValueError("invalid paired trial plan")
    rng = random.Random(policy["seed"])
    orders = [(reference, candidate), (candidate, reference)] * (pairs // 2)
    rng.shuffle(orders)
    if [list(order) for order in orders] != policy["orders"]:
        raise ValueError("stored plan disagrees with seeded orders")
    rows = [json.loads(line) for line in (out / "observations.jsonl").read_text().splitlines()]
    if len(rows) != 2 * (pairs + 1):
        raise ValueError("incomplete trial recording")
    blocks = []
    for index, order in enumerate([(reference, candidate), *orders], -1):
        block = {}
        start = 2 * (index + 1)
        for arm, row in zip(order, rows[start:start + 2]):
            trial = out / f"pair-{index}-{arm}"
            result = json.loads((trial / "result.json").read_text())
            app = result["application"]
            if (row["block"] != index or row["arm"] != arm or row["order"] != list(order)
                    or row["result"] != result or result["verdict"] != "PASS" or app["verdict"] != "PASS"
                    or result["elf_sha256"] != policy["firmware_sha256"][arm]
                    or digest(trial / "returned.utf8") != app["expected_output_sha256"]
                    or app["returned_sha256"] != app["expected_output_sha256"]
                    or digest(trial / "uart.bin") != app["uart_sha256"]):
                raise ValueError(f"trial evidence mismatch for {trial.name}")
            block[arm] = {"operational_ns": app["elapsed_ns"], "process_ns": result["process_wall_ns"],
                          "qemu_cpu_s": result["qemu_process_cpu_seconds"]}
        if index >= 0:
            blocks.append(block)
    report = {}
    for metric in blocks[0][reference]:
        a = [b[reference][metric] for b in blocks]
        b = [x[candidate][metric] for x in blocks]
        ratios = [x/y for x,y in zip(a,b)]
        boot = sorted(statistics.median(rng.choices(ratios, k=pairs)) for _ in range(2000))
        report[metric] = {reference + "_median": statistics.median(a), candidate + "_median": statistics.median(b),
                          "median_paired_speedup": statistics.median(ratios), "bootstrap_95": [boot[49],boot[1949]]}
    pending = summary.with_suffix(".tmp")
    pending.write_text(json.dumps(report, indent=2) + "\n")
    pending.replace(summary)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__,
                                     epilog="Use --summarize DIR to verify and summarize an existing recording.")
    for name in ("work", "original", "relocated", "qemu", "out"):
        parser.add_argument(name, type=Path)
    parser.add_argument("--labels", nargs=2, default=("original", "firmbolt"))
    if sys.argv[1:2] == ["--summarize"]:
        replay = argparse.ArgumentParser(description="Verify and summarize a complete paired recording.")
        replay.add_argument("--summarize", type=Path, required=True, metavar="DIR")
        summarize_recording(replay.parse_args().summarize)
        sys.exit(0)
    args = parser.parse_args()
    measure(args.work.resolve(), args.original.resolve(), args.relocated.resolve(), args.qemu.resolve(),
            args.out.resolve(), args.labels)
