"""Audit extracted Pye training observations against their immutable receipts."""
import argparse
import hashlib
import json
from pathlib import Path
import random
import statistics
import subprocess

from run_campaign import HERE, ROOT, read, require
from scopt.run.proof import frozen_append_counts
from training_contrasts import compare, orders


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_launch(raw, record, arm, case, runtime, bundle, deployed_runtime_id, initial_states):
    root = raw / f"trial-{record['block']}-{record['arm']}/launch"
    doc = read(root / "run.json")
    value = record["measurement"]
    checks = frozen_append_counts(doc)
    require(doc["qemu_sha256"] == runtime["qemu_sha256"] and doc["exit_code"] == 0 and not doc["timed_out"], "QEMU run differs")
    require(value["proof"]["sha256"] == "sha256:" + sha(root / "run.json"), "run receipt changed")
    require(all(value[k] == v for k, v in checks.items()), "late translation result differs")
    require(record["identity"] == arm["identity"], "deployment changed")
    deployed = read(root / "deploy.json")
    ids = deployed["identity"]
    require(deployed["artifact"]["path"] == arm["generation"]["artifact"]
            and ids["checked"] and not ids["reasons"]
            and ids["bundle_id"] == ids["expected_bundle_id"] == bundle["bundle_id"]
            and ids["runtime_identity_id"] == deployed_runtime_id, "deployed source or runtime differs")
    state = deployed["state"]
    stable = {key: state.get(key) for key in
              ("records_sha256", "ext_sha256", "n", "file_size", "file_version", "header_hex")}
    require(stable["records_sha256"] and doc["proof"]["n"] == stable["n"]
            and stable == initial_states.setdefault(record["arm"], stable), "sealed deployment differs")
    stage_path = raw / "ordinary/cases/update-package-version/stage.json"
    stage = read(stage_path)
    peer = doc["application_peer"]
    require(peer["spec"]["stage"]["sha256"] == "sha256:" + sha(stage_path), "input stage changed")
    out = root / "run-peer"
    result = read(out / "result.json")
    require(result == peer["result"] and peer["receipt_sha256"] == "sha256:" + sha(out / "result.json"),
            "UART oracle receipt changed")
    require(result["verdict"] == "PASS" and result["case"] == "update-package-version"
            and result["role"] == "training" and result["protocol_sha256"] == sha(HERE / "protocol-queued.json"),
            "different application protocol")
    require(all(result[k] == stage[k] == case[k] for k in
            ("role", "expected_output_sha256", "romfs_sha256", "keys_sha256")), "training case inputs changed")
    require(doc["inputs"]["psram"]["sha256"] == "sha256:" + case["romfs_sha256"], "guest staged different input")
    require(sha(out / "uart.bin") == result["uart_sha256"] and sha(out / "returned.utf8") == case["expected_output_sha256"]
            and value["oracle"]["returned_bytes"] == result["returned_bytes"] == (out / "returned.utf8").stat().st_size
            and value["oracle"]["returned_sha256"] == result["returned_sha256"] == case["expected_output_sha256"],
            "complete document or transcript differs")
    timings = doc["timings"]
    spans = [json.loads(line) for line in (root / "work/fastdyn_timing.jsonl").read_text().splitlines()]
    qemu_s, = [r["duration_s"] for r in spans if r["phase"] == "machine0.qemu_execution"
               and r["event"] == "phase_end" and r["status"] == "ok"]
    require(value["operational_ns"] == result["elapsed_ns"] and value["qemu_ns"] == round(qemu_s * 1e9)
            and value["wall_s"] == timings["wall_s"]
            and value["process_cpu_ns"] == round((timings["children_user_s"] + timings["children_sys_s"]) * 1e9),
            "timing differs from raw measurements")
    phase_starts = {r["phase"]: r["time_monotonic_s"] for r in spans if r["event"] == "phase_start"}
    costs = {"fastdyn_pre_qemu_ns": round(1e9 * (phase_starts["machine0.qemu_execution"] -
                                                 phase_starts["fastdyn.run.total"])),
             "late_translations": value["appended"]}
    for metric in ("ram_reset_ns", "bank_stage_ns"):
        if metric in timings:
            costs[metric] = timings[metric]
    return costs


def audit(raw, policy_bytes):
    policy = read(raw / "policy.json")
    report = read(raw / "report.json")
    require(json.loads(policy_bytes) == policy and
            "sha256:" + hashlib.sha256(policy_bytes).hexdigest() == report["policy"]["sha256"],
            "job policy differs from frozen source")
    require(report["state"] == "completed" and report["affinity"] and len(report["affinity"]) == 1, "incomplete job")
    schedule = read(raw / "orders.json")
    bundle = read(raw / "ordinary/bundle.json")
    runtime = read(raw / "ordinary/runtime.json")
    require(bundle["image_hash"] == report["firmware"]["sha256"]
            and runtime["qemu_sha256"] == report["qemu"]["sha256"]
            and runtime["plugin_sha256"] == report["plugin"]["sha256"], "runtime or firmware differs")
    expected = ["warm", *(a["name"] for a in policy["groups"][report["group"]]["arms"])]
    require(set(expected) == set(report["arms"]), "incomplete arm inventory")
    seen = {}
    aliases = {}
    for name in expected:
        arm = report["arms"][name]
        if arm["state"] == "materialized":
            aliases[name] = seen.setdefault(arm["identity"], name)
        else:
            require(arm["state"] == "refused" and arm["error"], "unexplained missing arm")
    require(schedule["aliases"] == aliases and schedule["warmup"] == list(seen.values()) and "warm" in aliases,
            "schedule differs from materialized deployments")
    deployed_runtime_id = read(raw / "trial--1-warm/launch/deploy.json")["identity"]["runtime_identity_id"]
    require(deployed_runtime_id and deployed_runtime_id.startswith("sha256:"), "missing ordinary deployment identity")
    initial_states = {}
    rng = random.Random(policy["timing"]["seed"])
    require(schedule["scored"] == orders(schedule["warmup"], rng, policy["timing"]["blocks"]), "unbalanced schedule")
    records = [json.loads(line) for line in (raw / "observations.jsonl").read_text().splitlines()]
    blocks = [{} for _ in schedule["scored"]]
    case = read(HERE / "qualification/case-identities.json")[policy["case"]]
    missing = set()
    costs = {}
    i = 0
    for index, names in enumerate([schedule["warmup"], *schedule["scored"]], -1):
        for name in names:
            if name in missing:
                continue
            r = records[i]
            i += 1
            require((r["block"], r["arm"]) == (index, name), "launch missing or out of order")
            if "error" in r:
                missing.add(name)
                require(report["arms"][name]["launch_error"] == r["error"], "unrecorded rejection")
                continue
            phase = verify_launch(raw, r, report["arms"][name], case, runtime, bundle, deployed_runtime_id, initial_states)
            if index >= 0:
                blocks[index][name] = r["measurement"]
                costs.setdefault(name, []).append(phase)
    require(i == len(records), "unexpected extra launches")
    done = {}
    for control, treatment in policy["groups"][report["group"]]["contrasts"]:
        a, b = aliases.get(control), aliases.get(treatment)
        if not a or not b or a in missing or b in missing:
            result = {"state": "unavailable"}
        elif a == b:
            result = {"state": "identical_deployment"}
        else:
            result = {"state": "complete", "blocks": len(blocks), **compare(blocks, a, b, rng)}
        name = f"{control}/{treatment}"
        require(result == report["contrasts"][name], f"incorrect paired comparison: {name}")
        done[name] = result["state"]
    return {"group": report["group"], "document_sha256": case["expected_output_sha256"],
            "verified_launches": len(records) - len(missing), "rejections": sorted(missing),
            "scored_blocks": len(blocks), "contrasts": done,
            "median_phase_costs": {name: {k: statistics.median(b[k] for b in vals) for k in vals[0]}
                                   for name, vals in costs.items() if name not in missing}}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("diagnostics", type=Path)
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--commit")
    source.add_argument("--policy-source", type=Path, help="exact worker policy bytes retained with the results")
    args = p.parse_args()
    data = args.policy_source.read_bytes() if args.policy_source else subprocess.check_output(
        ["git", "show", f"{args.commit}:tools/trebuchet/applications/pye/training-contrasts.json"], cwd=ROOT)
    print(json.dumps(audit(args.diagnostics, data), indent=2))
