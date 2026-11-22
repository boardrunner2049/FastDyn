"""Paired training diagnostics for B4 and guarded UDIV on fresh copies of a sealed store."""
import argparse
import itertools
import json
import os
import random
import statistics
import sys
from pathlib import Path

from observe import observe
from run_campaign import HERE, ROOT, command, pin, read, require, write_json
from candidates import deployment_identity
from scopt.run.launcher import RunDir


def compare(work, preparation, output):
    output.mkdir()
    src = preparation / "ordinary"
    ordinary, = (src / "store/snapshots").iterdir()
    report = read(preparation / "report.json")
    source = read(preparation / "plan.json")
    elf = Path(source["firmware"]["path"])
    qemu = Path(source["qemu"]["path"])
    require(pin(elf) == source["firmware"] and pin(qemu) == source["qemu"], "runtime or firmware changed")
    os.environ.update(FASTDYN_ROOT=str(work / "scopt"), SCOPT_FASTDYN_SRC=str(ROOT))
    paths = {"ordinary": ordinary}
    for name in ("b4", "native"):
        generation = report["stages"][name]["generation"]
        require(generation["snapshot_id"] != report["snapshot_id"], f"{name} made no changes")
        paths[name] = Path(generation["artifact"])
    def identities():
        return {k: deployment_identity(p, src / "pye.toml", elf, qemu, pin(preparation / "plan.json")["sha256"])
                for k, p in paths.items()}
    sealed = identities()
    require(len(set(sealed.values())) == 3, "control arms are equivalent")
    rng = random.Random(25687)
    orders = list(itertools.permutations(paths)) * 2
    rng.shuffle(orders)
    write_json(output / "plan.json", {"case": source["case"], "role": "training only", "track": "frozen inventory",
               "arms": sealed, "orders": orders, "warmup_blocks": 1, "seed": 25687,
               "cpu_affinity": sorted(os.sched_getaffinity(0)), "primary": "operational_ns",
               "failure_rule": "stop and preserve all attempted observations", "bootstrap_samples": 2000})
    blocks = []
    with (output / "observations.jsonl").open("x") as log:
        for i, order in enumerate([tuple(paths), *orders], -1):
            block = {}
            for name in order:
                dest = output / f"trial-{i}-{name}"
                row = {"block": i, "order": order, "arm": name, "artifact": sealed[name]}
                try:
                    command(output, dest.name, [sys.executable, HERE / "fastdyn_runner.py", "frozen",
                            "--work", work / "product", "--run", src, "--qemu", qemu, "--elf", elf,
                            "--plugin", work / "plugin-release/libfastdyn.so", "--case", source["case"],
                            "--snapshot", paths[name], "--out", dest], timeout=150)
                    doc = read(dest / "run.json")
                    app = observe(dest / "run.json", elf, training_frozen=True)
                    spans = [json.loads(line) for line in (dest / "work/fastdyn_timing.jsonl").read_text().splitlines()]
                    qemu_time, = [s["duration_s"] for s in spans if s["phase"] == "machine0.qemu_execution"
                                  and s["event"] == "phase_end" and s["status"] == "ok"]
                    timings = doc["timings"]
                    block[name] = {"operational_ns": app["operational_elapsed_ns"], "qemu_ns": round(qemu_time * 1e9),
                                   "process_wall_s": timings["wall_s"], "appended": doc["appended"]}
                    row["result"] = block[name]
                    rd = RunDir.open(dest)
                    rd.cache.unlink()
                    (rd.ram / "psram.ram").unlink(missing_ok=True)
                except Exception as exc:
                    row["error"] = str(exc)
                    raise
                finally:
                    print(json.dumps(row), file=log, flush=True)
            if i >= 0:
                blocks.append(block)
    require(identities() == sealed, "an immutable source deployment changed")
    summary = {}
    for name in ("b4", "native"):
        stats = {}
        for metric in ("operational_ns", "qemu_ns", "process_wall_s"):
            before = [b["ordinary"][metric] for b in blocks]
            after = [b[name][metric] for b in blocks]
            ratios = [a/b for a,b in zip(before,after)]
            boot = sorted(statistics.median(rng.choices(ratios, k=len(ratios))) for _ in range(2000))
            stats[metric] = {"ordinary_median": statistics.median(before), "candidate_median": statistics.median(after),
                             "median_paired_gain": statistics.median(ratios), "bootstrap_95": [boot[49],boot[1949]]}
        summary[name] = stats
    summary["late_translations"] = {name: [b[name]["appended"] for b in blocks] for name in paths}
    write_json(output / "summary.json", summary)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for label in ("work", "preparation", "output"):
        parser.add_argument(label, type=Path)
    args = parser.parse_args()
    compare(args.work.resolve(), args.preparation.resolve(), args.output.resolve())
