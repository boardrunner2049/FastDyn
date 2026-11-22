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
    blocks = []
    with (out / "observations.jsonl").open("x") as log:
        for i, order in enumerate([(reference, candidate), *orders], -1):
            block = {}
            for arm in order:
                trial = out / f"pair-{i}-{arm}"
                proc = subprocess.run([sys.executable, HERE / "cold.py", "--stage", out / "input/stage.json",
                                       "--qemu", qemu, "--elf", firmware[arm], "--out", trial])
                result = json.loads((trial / "result.json").read_text())
                print(json.dumps({"block": i, "order": order, "arm": arm, "result": result}), file=log, flush=True)
                if proc.returncode or result["verdict"] != "PASS" or result["application"]["verdict"] != "PASS":
                    raise RuntimeError(f"failed application oracle in {trial}")
                block[arm] = {"operational_ns": result["application"]["elapsed_ns"], "process_ns": result["process_wall_ns"],
                              "qemu_cpu_s": result["qemu_process_cpu_seconds"]}
            if i >= 0:
                blocks.append(block)
    report = {}
    for metric in blocks[0][reference]:
        a = [b[reference][metric] for b in blocks]
        b = [x[candidate][metric] for x in blocks]
        ratios = [x/y for x,y in zip(a,b)]
        boot = sorted(statistics.median(rng.choices(ratios, k=PAIRS)) for _ in range(2000))
        report[metric] = {reference + "_median": statistics.median(a), candidate + "_median": statistics.median(b),
                          "median_paired_speedup": statistics.median(ratios), "bootstrap_95": [boot[49],boot[1949]]}
    (out / "summary.json").write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("work", "original", "relocated", "qemu", "out"):
        parser.add_argument(name, type=Path)
    parser.add_argument("--labels", nargs=2, default=("original", "firmbolt"))
    args = parser.parse_args()
    measure(args.work.resolve(), args.original.resolve(), args.relocated.resolve(), args.qemu.resolve(),
            args.out.resolve(), args.labels)
