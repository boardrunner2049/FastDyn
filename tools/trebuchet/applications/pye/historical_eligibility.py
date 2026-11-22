"""Recognize a pinned historical B4 validator refusal with a concrete unsupported input."""
import json
from pathlib import Path

from scopt.run.launcher import file_sha256


def excluded_b4(dest, row, image, revision):
    if row["status"] != "failed" or row["recipe"] != ["b4"]:
        raise ValueError(f"historical outcome remains unresolved: {row['id']} ({row['status']})")
    root = dest / "registry" / (row["image_variant"] + ".b4")
    argv = json.loads((root / "command.json").read_bytes())
    prefix = [str(dest / "tools/scopt"), "b4", "package", image["snapshot"], "--store", str(root),
              "--runtime", image["admission_runtime"], "--enable", "eliminate-state-sync"]
    if argv[-len(prefix):] != prefix:
        raise ValueError("historical B4 invocation or input changed")
    output = json.loads((root / "stdout.json").read_bytes())
    path = Path(output["path"])
    snap_hash = row["ordinary_snapshot_id"].split(":")[1]
    candidate = "b4-D1-D2-D3-" + snap_hash[:12]
    if path != root / "rejections" / (candidate + ".json"):
        raise ValueError("rejection belongs to a different B4 proposal")
    report = json.loads(path.read_bytes())
    reason = "V12:validation_inconclusive"
    validation = report["validation"]
    failures = [r for r in validation["rows"] if r["result"] == "fail"]
    primary = [r for r in failures if r.get("verdict") == "inconclusive"
               and r.get("reason") == "validation_inconclusive"
               and r["detail"].startswith("unsupported:") and r["code"] == "V12"]
    coverage = [r for r in failures if r["code"] == "V12"
                and r.get("reason", "").startswith("hostsym_coverage_uncertified:")]
    if not (output["status"] == "rejected" and output["reason"] == reason and output["artifact_id"] is None
            and output["proposal"]["outcome"] == "candidate" and report["schema"] == "scopt.rejection"
            and report["version"] == 1 and report["reason"] == reason and report["candidate_id"] == candidate
            and report["link"]["ok"] and validation["schema"] == "scopt.validation"
            and validation["verdict"] == "rejected" and validation["codes"]["V12"] == "fail"
            and any(e["id"] == candidate and e["verdict"] == "fail" and e["reason"] == reason for e in report["ledger"])
            and primary and len(primary) + len(coverage) == len(failures)):
        raise ValueError("historical B4 refusal is missing deterministic unsupported-symbolic-input evidence")
    return {"id": row["id"], "source_revision": revision, "reason": reason, "snapshot": row["ordinary_snapshot_id"],
            "invocation_sha256": file_sha256(root / "command.json"), "stdout_sha256": file_sha256(root / "stdout.json"),
            "rejection_sha256": file_sha256(path), "unsupported": primary}
