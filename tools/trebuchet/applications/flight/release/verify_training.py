"""Independently verify each archived mission and prefix in one flight training run."""
import json
from pathlib import Path
import sys
from verify_sealed import FLIGHT, REPO, deployed_state, digest, validate


def verify(root):
    receipt = json.loads((root / "receipt.json").read_text())
    training = receipt["scopt_training"]
    frozen = root / "frozen/mission.sc"
    sidecar = root / "frozen/mission.sc.fastdyn.json"
    inventory = training["frozen"]
    if digest(frozen) != inventory["cache_sha256"] or digest(sidecar) != inventory["fastdyn_manifest_sha256"]:
        raise ValueError("archive differs from frozen input")
    state = deployed_state(frozen, sidecar)
    deployed = training["profiled"]["deployed_state"]
    if any(state[k] != deployed[k] for k in ("n", "header_hex", "records_sha256", "file_size")):
        raise ValueError("deployed record prefix differs")
    manifest = json.loads((FLIGHT / "manifest.json").read_text())
    mission = REPO / manifest["mission"]
    if digest(mission) != receipt["inputs"]["mission"] or digest(mission) != manifest["mission_sha256"]:
        raise ValueError("mission input differs")
    stages = {"cold": receipt["cold"], "population": training["population"],
              "profiled": training["profiled"]["mission"]}
    if "transformed" in training:
        stages["transformed"] = training["transformed"]["mission"]
    verified = {}
    for name, result in stages.items():
        directory = root / name
        for filename, expected in result["raw_outputs"].items():
            if digest(directory / filename) != expected:
                raise ValueError(f"{name}: changed {filename}")
        seen = validate("px4", mission, manifest, directory / "plant.csv", directory / "run/mav.tlog",
                        (directory / "console.log").read_text(errors="replace"))
        if seen != result["oracle"] or result["returncode"] != 0:
            raise ValueError(f"{name}: oracle differs")
        verified[name] = seen
    for name in ("profiled", "transformed"):
        if name in training and training[name]["proof"]["verdict"] != "PASS":
            raise ValueError(f"{name}: hosted cache integrity proof failed")
    return {"guest_sha256": receipt["inputs"]["elf"], "frozen": inventory, "phases": verified,
            "stage": training["stage"], "flow": training.get("flow"), "rewrite": training.get("rewrite"),
            "error": training.get("error"), "profile_header": training.get("profile_header")}


if __name__ == "__main__":
    print(json.dumps(verify(Path(sys.argv[1])), indent=2))
