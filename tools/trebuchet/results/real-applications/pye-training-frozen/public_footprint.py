"""Recover public host-code placement in the original, SFB and SFB+B3 reimports."""
import argparse
import hashlib
import json
from pathlib import Path


def snapshot(root):
    path, = root.glob("snapshots/*/units.jsonl")
    units = {}
    with path.open() as rows:
        for line in rows:
            region = json.loads(line)
            assert region["kind"] == "tb_singleton"
            member, = region["members"]
            origin = member["origin"]
            assert origin["snapshot_id"] == "sha256:" + path.parent.name
            uid = member["unit"]
            assert uid not in units
            units[uid] = (member, region["native"])
    return "sha256:" + path.parent.name, units


def footprint(snapshot_units, hot):
    lines, pages = set(), set()
    size = tails = grown = 0
    for uid in hot:
        member, native = snapshot_units[uid]
        start = int(member["origin"]["tb_offset"], 16) - native["struct"]["offset_from_code"]
        end = start + native["code_len"]
        lines.update(range(start // 64, (end - 1) // 64 + 1))
        pages.update(range(start // 4096, (end - 1) // 4096 + 1))
        size += end - start
        if "stub_start" in native:
            tails += native["code_len"] - native["stub_start"]
            grown += 1
    return dict(records=len(hot), code_bytes=size, lines64=len(lines), pages4k=len(pages),
                records_with_guard_tails=grown, reserved_guard_tail_bytes=tails)


def compare(root):
    ordinary_id, ordinary = snapshot(root / "original/store")
    profile = json.loads((root / "original/profile/merged.profile.json").read_text())
    assert profile["snapshot_id"] == ordinary_id and profile["collection"]["complete"]
    phase, = [p for p in profile["phases"] if p["name"] == "steady" and p["reached"]]
    counts = {r["unit"]: r["phase"][str(phase["id"])]["entries"] for r in profile["units"]
              if r["status"] == "exact"}
    assert len(profile["units"]) == len(ordinary) == len(counts) and counts.keys() == ordinary.keys()
    total = sum(counts.values())
    hot = []
    covered = 0
    for uid in sorted(counts, key=lambda unit: (-counts[unit], unit)):
        hot.append(uid)
        covered += counts[uid]
        if covered * 10 >= total * 9:
            break
    result = dict(profile=profile["profile_id"], snapshot=ordinary_id, total_exact_entries=total,
                  covered_entries=covered, profile_only_rows=len(profile["profile_only"]),
                  ordinary=footprint(ordinary, hot))
    parent_id, parent = ordinary_id, ordinary
    for label in ["original.sfb", "original.sfb.b3"]:
        folder = root / "registry" / label
        child_id, child = snapshot(folder / "import")
        sidecar_path, = folder.glob("artifacts/*/cache.sc.scopt.json")
        sidecar = json.loads(sidecar_path.read_text())
        origin_path, = folder.glob("import/warm-origins/*/*.json")
        assert json.loads(origin_path.read_text()) == {"parent": parent_id, "snapshot_id": child_id}
        assert (sidecar["input_snapshot_id"], sidecar["output_snapshot_id"]) == (parent_id, child_id)
        moves = sidecar["move_map"]
        assert child.keys() == parent.keys() and moves.keys() <= parent.keys()
        for uid, (src, _) in parent.items():
            dst, _ = child[uid]
            assert src["guest"] == dst["guest"] and src["key"] == dst["key"]
            old, new = src["origin"], dst["origin"]
            assert old["slot"] == new["slot"]
            if uid in moves:
                move = moves[uid]
                assert (move["slot"], move["old_tb_offset"], move["new_tb_offset"]) == (
                    old["slot"], old["tb_offset"], new["tb_offset"])
                assert int(new["tb_offset"], 16) - int(old["tb_offset"], 16) == move["delta"]
            else:
                assert old["tb_offset"] == new["tb_offset"]
        result[label] = dict(footprint(child, hot), snapshot=child_id, public_units_verified=len(child),
                             moved_public_units=len(moves))
        if "x_b3" in sidecar:
            grown = {uid for uid, (_, n) in child.items() if "stub_start" in n}
            assert set(sidecar["x_b3"]["grown"]) == grown
            result[label]["private_variants_without_activity_counts"] = len(sidecar["x_b3"]["variants"])
        parent_id, parent = child_id, child
    result["source_sha256"] = {str(f.relative_to(root)): hashlib.sha256(f.read_bytes()).hexdigest()
                               for f in sorted(root.rglob("*")) if f.is_file()
                               and (f.name in {"merged.profile.json", "units.jsonl", "cache.sc.scopt.json"}
                                    or "warm-origins" in f.parts)}
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign", type=Path)
    print(json.dumps(compare(parser.parse_args().campaign), indent=2))
