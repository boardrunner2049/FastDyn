"""Audit an extracted path-count worker archive without its original absolute run paths."""
import argparse
import hashlib
import json
from pathlib import Path

from path_counts import CASE, HERE, read, require, summarize
from scopt.run.proof import frozen_append_counts


def digest(file):
    return hashlib.sha256(file.read_bytes()).hexdigest()


def audit(folder):
    report = read(folder / "report.json")
    want = read(HERE / "qualification/case-identities.json")[CASE]
    stage_file = folder / "ordinary/cases" / CASE / "stage.json"
    stage = read(stage_file)
    require(report["case"] == CASE and stage["case"] == CASE, "wrong training edit")
    result = {}
    for name, data in report["stages"].items():
        rows = []
        for i, row in enumerate(data["rows"]):
            if row.get("state") != "observed":
                rows.append({"state": "refused", "reason": row.get("error"), "plan": row["plan"]})
                continue
            run = folder / f"observe-{name}-{i}"
            plan = read(run / "points.json")
            counts = read(run / "counts.json")
            doc = read(run / "run.json")
            dep = read(run / "deploy.json")
            peer = doc["application_peer"]
            case = read(run / "run-peer/result.json")
            require(plan == row["plan"] and counts["counts"] == row["counts"]
                    and counts["exit_code"] == doc["exit_code"] == 0 and not counts["errors"]
                    and not doc["timed_out"], "debugger event or launch identity differs")
            require(counts["checked"] == {k: plan["points"][k]["expected_hex"]
                    for k, n in counts["counts"].items() if n > 0}, "live instruction prefix missing")
            require(dep["identity"]["checked"] and not dep["identity"]["reasons"]
                    and dep["state"]["n"] == doc["proof"]["n"], "unchecked deployed cache")
            require(case["protocol_sha256"] == stage["protocol_sha256"] == digest(HERE / "protocol-queued.json")
                    and report["qemu"]["path"] == read(run / "qemu_argv.json")["argv"][0]
                    and peer["result"] == case and case["case"] == CASE and case["verdict"] == "PASS"
                    and peer["receipt_sha256"] == "sha256:" + digest(run / "run-peer/result.json")
                    and peer["spec"]["stage"]["sha256"] == "sha256:" + digest(stage_file),
                    "UART result or frozen stage differs")
            for key in ("keys_sha256", "romfs_sha256", "expected_output_sha256", "role"):
                require(case[key] == stage[key] == want[key], "wrong training provenance")
            require(digest(run / "run-peer/returned.utf8") == case["returned_sha256"] == want["expected_output_sha256"]
                    and digest(run / "run-peer/uart.bin") == case["uart_sha256"]
                    and doc["inputs"]["psram"]["sha256"] == "sha256:" + want["romfs_sha256"],
                    "returned bytes, input bytes or transcript changed")
            shape = summarize(plan, counts["counts"])
            require(shape == row["paths"], "path conservation differs")
            rows.append({"state": "observed", "unit": plan["unit"], "guest_pc": plan["guest_pc"],
                         "kind": plan["kind"], "counts": counts["counts"], "paths": shape,
                         **frozen_append_counts(doc)})
        result[name] = {"rows": rows, "error": data.get("error"), "snapshot": data.get("snapshot")}
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("diagnostics", type=Path)
    args = p.parse_args()
    print(json.dumps(audit(args.diagnostics), indent=2))
