"""Check native cache bytes, generated metadata and profile of one PX4 host rewrite."""
import hashlib
import json
from pathlib import Path
import sys

from scopt.compact_nops import compact
from scopt.formats.profile_v1 import check_flow, read_profile
from scopt.formats.sc_v5 import read_sc
from scopt.formats.scm_v1 import read_scm
from scopt.run.proof import deployed_state


def verify(root):
    report = json.loads((root / "receipt.json").read_text())["scopt_training"]
    rewrite = json.loads((root / "compacted/compaction.json").read_text())
    if report["stage"] != "qualified" or rewrite != report["rewrite"]:
        raise ValueError("host rewrite receipt differs")
    source = read_sc(root / "frozen/mission.sc", strict=True)
    target = read_sc(root / "compacted/cache.sc", strict=True)
    profile = read_profile(root / "profile.json")
    if (source.snapshot_id() != rewrite["input_snapshot"] or target.snapshot_id() != rewrite["output_snapshot"]
            or profile.snapshot_id != source.snapshot_id() or profile.profile_id != rewrite["profile_id"]
            or source.unit_ids() != target.unit_ids() or not profile.collection["complete"]
            or check_flow(profile).to_json() != report["flow"]):
        raise ValueError("source, target or profile binding differs")
    slot = rewrite["slot"]
    if source.unit_ids()[slot] != rewrite["unit"]:
        raise ValueError("selected unit identity differs")
    steady, = [str(p["id"]) for p in profile.phases if p["name"] == "steady" and p["reached"]]
    row, = [u for u in profile.units if u["unit"] == rewrite["unit"]]
    if row["status"] != "exact" or row["phase"][steady]["entries"] != rewrite["phase_entries"]:
        raise ValueError("selected unit lacks claimed exact steady entries")
    a, b = list(source.units()), list(target.units())
    changed = [i for i, (x, y) in enumerate(zip(a, b, strict=True))
               if x.record_bytes != y.record_bytes or x.body != y.body]
    if changed != [slot]:
        raise ValueError(f"unexpected changed units: {changed}")
    original = read_scm(root / "training.scm").tbs[slot]
    computed, expected_chunk, _ = compact(a[slot], original, source.header.code_address)
    if computed != b[slot] or read_scm(root / "compacted/cache.scm").tbs[slot] != expected_chunk:
        raise ValueError("native rewrite or derived capture differs from recomputation")
    target_state = deployed_state(root / "compacted/cache.sc", root / "frozen/mission.sc.fastdyn.json")
    deployed = report["transformed"]["deployed_state"]
    if any(target_state[k] != deployed[k] for k in ("header_hex", "n", "records_sha256", "file_size")):
        raise ValueError("rewritten inventory differs from deployment")
    with (root / "compacted/cache.sc").open("rb") as stream:
        sha = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"candidate_cache_sha256": sha, "trained_unit": rewrite["unit"], "slot": slot,
            "guest_pc": hex(original.vaddr), "exact_steady_entries": rewrite["phase_entries"],
            "old_code_bytes": len(a[slot].code), "new_code_bytes": len(b[slot].code),
            "unchanged_units": len(a) - 1, "initial_snapshot": source.snapshot_id(),
            "rewritten_snapshot": target.snapshot_id(), "profile_id": profile.profile_id,
            "record_table_sha256": target_state["records_sha256"],
            "replay_appended": report["transformed"]["proof"]["appended"]}


if __name__ == "__main__":
    print(json.dumps(verify(Path(sys.argv[1])), indent=2))
