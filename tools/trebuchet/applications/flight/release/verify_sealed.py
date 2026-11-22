"""Verify the two downloaded release PX4 sealed caches and six recorded missions."""
import hashlib
import json
from pathlib import Path
import sys

FLIGHT = Path(__file__).resolve().parents[1]
REPO = FLIGHT.parents[3]
sys.path[:0] = [str(FLIGHT), str(REPO / "tools/scopt")]
from oracle import validate
from scopt.run.proof import deployed_state


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify(root, image, guest, manifest):
    out = root / "out"
    cache = root / "frozen/mission.sc"
    sidecar = cache.with_suffix(".sc.fastdyn.json")
    receipt = json.loads((out / "receipt.json").read_text())
    frozen = receipt["frozen_preparation"]
    identity = frozen["identity"]
    attempt, = receipt["warm_attempts"]
    current = deployed_state(cache, sidecar)
    deployed = attempt["deployed_state"]
    mission = REPO / manifest["mission"]
    if (receipt["inputs"]["elf"] != guest or digest(mission) != manifest["mission_sha256"]
            or receipt["inputs"]["mission"] != manifest["mission_sha256"]
            or receipt["runtime"]["px4_tlog_contract"] != "flush-each-packet-v2"
            or not receipt["operational_baseline"] or "training_profiler" in receipt):
        raise ValueError(f"{image}: wrong guest, mission or collection contract")
    if image == "firmbolt" and not receipt["candidate"]["placement"]["admissible"]:
        raise ValueError("transformed image lacks checked placement")
    if (current["file_size"] != identity["logical_bytes"] or digest(cache) != identity["cache_sha256"]
            or digest(sidecar) != identity["fastdyn_manifest_sha256"]
            or any(current[key] != deployed[key] for key in ("header_hex", "n", "records_sha256"))
            or current["manifest"]["fingerprint"] != deployed["manifest"]["fingerprint"]):
        raise ValueError(f"{image}: frozen bytes or deployed state differ")
    proof = attempt["proof"]
    if (proof["verdict"] != "PASS" or not frozen["qualified"] or receipt["ordinary_warm"]
            or proof["n"] != current["n"] or proof["appended"] != frozen["appended_records"]
            or proof["n"] + proof["appended"] != proof["record_count_after"] or proof["appended"] <= 0):
        raise ValueError(f"{image}: inconsistent replay verdict")
    phases = {}
    for name, phase in (("cold", receipt["cold"]), ("capture", receipt["capture"]),
                        ("replay-1", attempt["mission"])):
        folder = out / name
        for file, expected in phase["raw_outputs"].items():
            if digest(folder / file) != expected:
                raise ValueError(f"{image}/{name}: changed {file}")
        phases[name] = validate("px4", mission, manifest, folder / "plant.csv", folder / "run/mav.tlog",
                                (folder / "console.log").read_text(errors="replace"))
        if phases[name] != phase["oracle"] or phase["returncode"] != 0:
            raise ValueError(f"{image}/{name}: mission receipt differs")
    return {"guest_sha256": guest, "observations_sha256": digest(root / "observations.tgz"),
            "frozen_archive_sha256": digest(root / "frozen.tgz"),
            "frozen": frozen, "runtime": receipt["runtime"], "phases": phases,
            "strict_warm_error": receipt["error"]}


if __name__ == "__main__":
    guests = json.loads((FLIGHT / "release/cacheless-policy.json").read_text())["guest_sha256"]
    manifest = json.loads((FLIGHT / "manifest.json").read_text())
    print(json.dumps({im: verify(Path(sys.argv[1]) / im, im, guest, manifest)
                      for im, guest in guests.items()}, indent=2))
