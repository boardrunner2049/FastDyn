#!/usr/bin/env python3
"""Verify and summarize late translations in recorded Pye training-frozen trials."""
import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import statistics
import tarfile

from scopt.run.proof import frozen_append_counts


def summarize(observations):
    cases = json.loads((Path(__file__).parent / "qualification/case-identities.json").read_text())
    groups = defaultdict(list)
    starts = {}
    rejected = []
    index = {}
    with tarfile.open(observations, "r|gz") as archive:
        for member in archive:
            path = Path(member.name)
            if (member.isfile() and path.parts[:1] == ("campaign",) and len(path.parts) >= 3
                    and path.parts[1] in {"pre-change", "selection", "final", "rebuilt-promotion"}
                    and path.name in {"observations.jsonl", "run.json", "uart.bin", "returned.utf8"}):
                index[str(path)] = archive.extractfile(member).read()
    def data(path):
        return index[str(path)]

    def doc(path):
        return json.loads(data(path))

    for folder in ("pre-change", "selection", "final", "rebuilt-promotion"):
        log = f"campaign/{folder}/observations.jsonl"
        if log not in index:
            continue
        for line in data(log).splitlines():
            rec = json.loads(line)
            if "error" in rec:
                rejected.append({k: v for k, v in rec.items() if k != "objective"})
                continue
            res = rec["result"]
            if res["cache_track"] != "training-frozen":
                raise ValueError("mixed preparation tracks")
            path = Path(res["proof"])
            rel = Path(*path.parts[path.parts.index("campaign"):])
            run = doc(rel)
            counts = frozen_append_counts(run)
            if any(res[k] != v for k, v in counts.items()) or run["exit_code"] or run["timed_out"]:
                raise ValueError(f"fresh proof disagrees with trial record: {rel}")
            uart = run["application_peer"]["result"]
            name = rec["case"]
            expected = cases[name]["expected_output_sha256"]
            if (uart["case"] != name or uart["verdict"] != "PASS"
                    or uart["returned_sha256"] != expected or res["work"] != 1
                    or res["value"] != uart["elapsed_ns"]):
                raise ValueError(f"operator job disagrees with the receipt: {rel}")
            peer = rel.parent / "run-peer"
            for filename, digest in (("uart.bin", uart["uart_sha256"]), ("returned.utf8", expected)):
                if hashlib.sha256(data(peer / filename)).hexdigest() != digest:
                    raise ValueError(f"retained application output changed: {rel}")
            identity = rec.get("identity", rec.get("deployment_identity", rec.get("deployment")))
            if starts.setdefault(identity, counts["initial_records"]) != counts["initial_records"]:
                raise ValueError(f"changing initial inventory for {identity}")
            if rec.get("warmup") or rec.get("block", 0) < 0:
                continue
            role = rec.get("phase", rec.get("role", folder))
            groups[(role, name, identity)].append((counts["appended"], res))

    rows = []
    for (role, case, identity), samples in sorted(groups.items()):
        appends = [n for n, _ in samples]
        rows.append({"role": role, "case": case, "identity": identity, "runs": len(samples),
                     "initial_records": starts[identity], "appended_min": min(appends),
                     "appended_median": statistics.median(appends), "appended_max": max(appends),
                     "operational_ns_median": statistics.median(s["value"] for _, s in samples),
                     "qemu_lifetime_ns_median": statistics.median(s["qemu_lifetime_ns"] for _, s in samples),
                     "process_tree_cpu_ns_median": statistics.median(s["phase_costs"]["process_tree_cpu_ns"]
                                                                     for _, s in samples)})
    return {"cache_track": "training-frozen", "cost_scope": "full-launch translation counts and process-tree CPU",
            "scored_groups": rows, "rejected": rejected}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("observations_tgz", type=Path)
    print(json.dumps(summarize(p.parse_args().observations_tgz), indent=2))
