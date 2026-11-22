"""Re-ingest a release flight's original native QPROF using its archived identities."""
import argparse
import hashlib
import json
from pathlib import Path

from scopt.formats.bundle_v1 import read_bundle, read_runtime_identity
from scopt.formats.profile_v1 import check_flow, ingest
from scopt.formats.qprof_v1 import read_qprof


def recover(out):
    receipt = json.loads((out / "receipt.json").read_text())
    training = receipt["scopt_training"]
    files = {}
    for name, field in (("training.qprof", "qprof"), ("training.scm", "capture")):
        with (out / name).open("rb") as stream:
            files[name] = hashlib.file_digest(stream, "sha256").hexdigest()
        if files[name] != training[field]["sha256"]:
            raise ValueError(f"changed native input {name}")
    bundle = read_bundle(out / "bundle.json")
    runtime = read_runtime_identity(out / "runtime.json")
    if (bundle["image_hash"] != "sha256:" + receipt["inputs"]["elf"]
            or runtime["qemu_sha256"] != "sha256:" + receipt["runtime"]["qemu"]
            or runtime["plugin_sha256"] != "sha256:" + receipt["runtime"]["plugin"]):
        raise ValueError("training bundle or runtime does not match the flown firmware")
    qprof = read_qprof(out / "training.qprof")
    staged = out / "profiled-post.sc"
    if not staged.is_file():
        staged = out / "mission.sc"
    result = ingest(qprof, staged, out / "frozen/mission.sc",
                    bundle_id=bundle["bundle_id"], contract_id=bundle["contract_id"],
                    runtime_identity=runtime, inputs=[{"name": "copter_mission.waypoints",
                    "split": "train", "sha256": "sha256:" + receipt["inputs"]["mission"]}])
    flow = check_flow(result)
    if not flow.ok or not flow.exact:
        raise ValueError(f"profile flow failure: {flow}")
    return {"raw_inputs": files, "receipt_error": training.get("error"),
            "bundle_id": result.bundle_id, "runtime_identity_id": result.runtime_identity_id,
            "snapshot_id": result.snapshot_id, "profile_id": result.profile_id,
            "header": qprof.header.to_json(), "phases": result.phases,
            "flow": flow.to_json(), "captured_tbs": training["captured_tbs"],
            "replay_appends": training["profiled"]["proof"]["appended"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    print(json.dumps(recover(args.out), indent=2))
