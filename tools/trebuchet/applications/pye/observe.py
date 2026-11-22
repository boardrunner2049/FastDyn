#!/usr/bin/env python3
"""Read a proof-checked Pye launch and independently bind the full editor result."""
import argparse
import hashlib
import json
from pathlib import Path


def sha(path):
    with path.open("rb") as data:
        return hashlib.file_digest(data, "sha256").hexdigest()


def observe(run_path, elf):
    doc = json.loads(run_path.read_text())
    if doc["verdict"] != "PASS" or doc["exit_code"] != 0 or doc["timed_out"] or doc["appended"] != 0:
        raise ValueError("Pye observation needs a successful fully warm run")
    if not elf.is_file():
        raise ValueError("guest ELF missing")
    peer = doc["application_peer"]
    receipt = Path(peer["receipt"])
    if receipt.resolve() != (run_path.parent / (run_path.stem + "-peer/result.json")).resolve():
        raise ValueError("UART receipt belongs to another run")
    result = json.loads(receipt.read_text())
    if peer["receipt_sha256"] != "sha256:" + sha(receipt) or result != peer["result"]:
        raise ValueError("UART receipt changed")
    stage_spec = peer["spec"]["stage"]
    stage_path = Path(stage_spec["path"])
    if stage_spec["sha256"] != "sha256:" + sha(stage_path):
        raise ValueError("UART scenario declaration changed")
    stage = json.loads(stage_path.read_text())
    if (result["verdict"] != "PASS" or result["case"] != stage["case"]
            or result["protocol"] != "pye-queued-uart-v1" or stage["protocol"] != result["protocol"]
            or result["protocol_sha256"] != sha(Path(__file__).with_name("protocol-queued.json"))
            or result["protocol_sha256"] != stage["protocol_sha256"]):
        raise ValueError("Pye run was not validated under the frozen queued terminal protocol")
    bank = peer["spec"]["bank"]
    if doc["inputs"][bank]["sha256"] != "sha256:" + result["romfs_sha256"]:
        raise ValueError("guest PSRAM stage differs from the checked session")
    case = json.loads((Path(__file__).parent / "qualification/case-identities.json").read_text())[result["case"]]
    binding_keys = ("role", "keys_sha256", "expected_output_sha256", "romfs_sha256")
    if any(result[key] != case[key] or stage[key] != case[key] for key in binding_keys):
        raise ValueError("Pye result differs from its frozen role, document or input")
    returned = receipt.with_name("returned.utf8")
    digest = sha(returned)
    if digest != case["expected_output_sha256"] or digest != result["returned_sha256"]:
        raise ValueError("complete edited buffer differs from the declared result")
    if returned.stat().st_size != result["returned_bytes"] or sha(receipt.with_name("uart.bin")) != result["uart_sha256"]:
        raise ValueError("retained application outputs changed")
    if type(result["elapsed_ns"]) is not int or result["elapsed_ns"] <= 0:
        raise ValueError("the complete editing job needs a positive operational interval")
    return {"completed_work": 1, "operational_elapsed_ns": result["elapsed_ns"],
            "observations": {"returned_sha256": result["returned_sha256"],
                             "returned_bytes": result["returned_bytes"]}}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("run_json", type=Path)
    p.add_argument("elf", type=Path)
    args = p.parse_args()
    print(json.dumps(observe(args.run_json, args.elf)))
