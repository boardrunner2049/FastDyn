"""Materialize a bounded original-image ablation using an existing training capture."""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from run_campaign import HERE, ROOT, build, command, optimizer_bindings, pin, read, write_json


def discover(work, output):
    policy = read(HERE / "discovery-policy.json")
    output.mkdir(parents=True, exist_ok=False)
    ordinary = work / "ordinary"
    qemu = ROOT / "third_party/common/qemu/build/qemu-system-arm"
    prep = ordinary / "prep"
    profile = ordinary / "profile/merged.profile.json"
    snapshot, = (ordinary / "store/snapshots").iterdir()
    image = work / "product/micropython/ports/qemu/build-MPS2_AN385/firmware.elf"
    scopt = Path(sys.executable).parent / "scopt"
    os.environ.update(FASTDYN_ROOT=str(work / "scopt"), SCOPT_FASTDYN_SRC=str(ROOT))
    optimizer_bindings(output, qemu)
    admission = output / "admission-runtime.json"
    command(output, "admission", [scopt, "admission", "identity", "--snapshot", snapshot,
                                   "--config", ordinary / "pye.toml", "--out", admission])
    plan = {k: policy[k] for k in ("application", "scenario", "recipes", "params", "budget")}
    contract = output / "contract.json"
    write_json(contract, {"training_case": policy["scenario"], "policy": pin(HERE / "discovery-policy.json"),
                          "input": pin(ordinary / "cases" / policy["scenario"] / "stage.json"),
                          "profile": pin(profile), "scope": policy["role"]})
    base = read(HERE / "campaign-policy.json")
    plan.update(configuration=base["configuration"], objective=base["objective"],
                contract=str(contract), admission=str(HERE / "admission.json"), images={"original": {
                    "firmware": str(image), "config": str(ordinary / "pye.toml"), "runtime": str(qemu),
                    "snapshot": str(snapshot), "profile": str(profile), "manifest": str(prep / "cache/fw.sc.fastdyn.json"),
                    "prep": str(prep), "admission_runtime": str(admission)}})
    write_json(output / "plan.json", plan)
    registry = build(output / "plan.json", output / "registry")
    qualification = {}
    for row in registry["candidates"]:
        if row["status"] != "ready":
            continue
        name = row["id"].replace("/", "-")
        try:
            command(output, "replay-" + name, [sys.executable, HERE / "fastdyn_runner.py", "warm",
                    "--work", work / "product", "--run", ordinary, "--qemu", qemu,
                    "--plugin", work / "plugin-release/libfastdyn.so", "--elf", image,
                    "--case", policy["scenario"], "--snapshot", row["artifact"],
                    "--out", output / ("trial-" + name)], timeout=135)
            qualification[name] = "passed strict training replay"
        except subprocess.SubprocessError as exc:
            qualification[name] = str(exc)
        write_json(output / "training-replays.json", qualification)
    print(json.dumps([(row["id"], row["status"]) for row in registry["candidates"]]))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("warm_smoke_work", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    discover(args.warm_smoke_work.resolve(), args.output.resolve())
