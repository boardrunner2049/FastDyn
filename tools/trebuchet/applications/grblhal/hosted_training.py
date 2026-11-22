"""Bounded CNC training comparison on a fixed-address Linux x86-64 runner."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from scopt.run.proof import deployed_state, prove
from run_board import sha


def write(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n")


def clone(source, target):
    subprocess.run(["cp", "--reflink=auto", "--sparse=always", source, target], check=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for x in ("qemu", "plugin", "job", "normal", "candidate", "out"):
        p.add_argument(x, type=Path)
    args = p.parse_args()
    out = args.out.resolve()
    out.mkdir(exist_ok=False)
    images = {"normal": args.normal, "candidate": args.candidate}
    observations = []
    summary = {"role": "training only", "metric": "host monotonic time from first drawing line to Idle response",
               "budget": "up to four preparations per image, then three pairs AB/BA/AB", "runs": observations}
    runner = Path(__file__).with_name("run_board.py")
    def execute(name, label, cache, extra):
        rd = out / f"{name}-{label}"
        argv = [sys.executable, runner, args.qemu, images[name], args.job, rd, "--cache", cache,
                "--fastdyn-plugin", args.plugin, "--timeout", "420"]
        manifest = Path(str(cache) + ".fastdyn.json")
        before = deployed_state(cache, manifest) if cache.exists() else None
        proc = subprocess.run(argv, env={**os.environ, **extra})
        row = {"image": name, "phase": label, "exit": proc.returncode}
        observations.append(row)
        if (rd / "receipt.json").is_file():
            receipt = json.loads((rd / "receipt.json").read_text())
            row.update(state=receipt["state"], job_wall_ns=receipt.get("job_wall_ns"))
        if before:
            proof = prove(before)
            write(rd / "proof.json", proof)
            row.update(proof=proof["verdict"], appended=proof["appended"])
        write(out / "summary.json", summary)
        if proc.returncode or row["state"] != "passed" or (before and row["proof"] != "PASS"):
            raise RuntimeError(f"uncertified run {row}")
        if deployed_state(cache, manifest)["n"] < 1:
            raise RuntimeError("empty translation inventory")
        return row
    for name in images:
        cache = out / (name + ".sc")
        for n in range(4):
            extra = {"QEMU_SC_CAPTURE": str(out / (name + ".scm"))} if n == 0 else {}
            result = execute(name, f"prepare{n}", cache, extra)
            if result.get("appended") == 0:
                break
        state = deployed_state(cache, str(cache) + ".fastdyn.json")
        write(out / (name + "-frozen.json"), state)
        clone(cache, out / (name + "-frozen.sc"))
        shutil.copyfile(str(cache) + ".fastdyn.json", out / (name + "-frozen.manifest.json"))
    for index, pair in enumerate((("normal", "candidate"), ("candidate", "normal"), ("normal", "candidate"))):
        for name in pair:
            source = out / (name + "-frozen.sc")
            cache = out / (name + ".sc")
            clone(source, cache)
            shutil.copyfile(out / (name + "-frozen.manifest.json"), str(cache) + ".fastdyn.json")
            execute(name, f"pair{index}", cache, {})
            cache.unlink()
    paired = [r for r in observations if r["phase"].startswith("pair")]
    summary["fully_warm_training_comparison"] = all(r.get("appended") == 0 for r in paired)
    summary["images"] = {name: sha(path) for name, path in images.items()}
    write(out / "summary.json", summary)


if __name__ == "__main__":
    main()
