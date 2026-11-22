"""Offline physical CALL, NOP and B3 encoding-refusal census for a captured image."""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from graphlib import TopologicalSorter, CycleError
from pathlib import Path

from scopt.analysis.api import ScopeRef
from scopt.analysis.hostops import OpKind
from scopt.analysis.native_callees import NATIVE_IMAGES, _verified_captures, prepare_native_images
from scopt.analysis.store import AnalysisStore
from scopt.canon import sha256_id
from scopt.commands.host_calls import _calls, _inventory, _public_row
from scopt.decode.guest_symbols import load_guest_image
from scopt.decode.x86 import sweep
from scopt.formats.artifact import read_artifact, verify_manifest
from scopt.formats.profile_v1 import ProfileView, read_profile
from scopt.formats.sc_v6 import read_sc6
from scopt.importer.import_cache import import_variants, open_snapshot


def decoded(unit, code, base):
    end = unit.pool_start if unit.pool_start is not None else unit.ldst_end
    extra = unit.inline_start if unit.inline_start is not None else unit.stub_start
    spans = [(0, end)] + ([] if extra is None else [(extra, unit.tc_size)])
    return {i.address - base: i for lo, hi in spans for i in sweep(code[lo:hi], base + lo)}


def at_most_once(store, unit):
    scope = ScopeRef("unit", unit.uid)
    host = store.get("hostops", scope).value
    cfg = store.get("hcfg", scope).value
    if host.status != "ok" or not cfg.ok or unit.native_fragments or unit.stub_start is not None:
        return set()
    # Include slow paths and spins. An unresolved control transfer, including
    # an indirect internal jump, would invalidate the per-dispatch bound.
    covered = [0] * (len(host.ops) + 1)
    for i, op in enumerate(host.ops):
        if op.kind in (OpKind.JMP, OpKind.JCC) and op.target is None and i not in cfg.exits:
            return set()
        # Every instruction in a cycle lies within at least one backward
        # edge's closed interval. Sites outside those intervals cannot repeat.
        for successor in cfg.succ_full[i]:
            if successor <= i:
                covered[successor] += 1
                covered[i + 1] -= 1
    # LD/ST slow paths commonly jump backwards to a continuation without
    # forming a cycle. All sites of a verified DAG are at most once.
    try:
        TopologicalSorter(dict(enumerate(cfg.succ_full))).prepare()
    except CycleError:
        pass
    else:
        return {op.off for op in host.ops if op.block != "pad"}
    depth, once = 0, set()
    for i, op in enumerate(host.ops):
        depth += covered[i]
        if not depth and op.block != "pad":
            once.add(op.off)
    return once


def observed_lookup_events(profile, phase):
    """P3 fires inside helper_lookup_tb_ptr; retain in-domain exact unflagged events."""
    if profile is None:
        return None
    pid = profile.phase_id(phase)
    members = profile.unit_map()
    counts = {"lookup_hit": 0, "lookup_miss": 0}
    for edge in profile.edges:
        if (edge["phase"] == pid and edge["src"] in members and edge["dst"] in members
                and edge["status"] == "exact" and not edge["flags"] and edge["kind"] in counts):
            counts[edge["kind"]] += edge["count"]
    return {"exact_in_domain_events": counts, "minimum_helper_calls": sum(counts.values())}


def whole_run_slot_counts(view, phase):
    """Exact unflagged transitions. Profile edge phases belong to the destination."""
    if view is None:
        return {}
    profile = view.profile
    pid = profile.phase_id(phase)
    row = next(p for p in profile.phases if p["id"] == pid)
    if row["first_dispatch_index"] != 0 or any(p["reached"] for p in profile.phases if p["id"] > pid):
        return {}
    counts = defaultdict(Counter)
    for edge in view.edges(phase):
        if edge["status"] == "exact" and not edge["flags"] and edge["kind"] in ("chain0", "chain1"):
            counts[edge["src"]][int(edge["kind"][-1])] += edge["count"]
    return counts


def exit_nop_evidence(host, cfg, once, counts, entries):
    """Bound physical NOP visits using a unique suffix at a known exit slot."""
    exits = defaultdict(list)
    for i, name in cfg.exits.items():
        if name in ("goto_tb:0", "goto_tb:1"):
            exits[int(name[-1])].append(i)
    observed = {}
    for slot, indices in exits.items():
        if len(indices) != 1 or not counts.get(slot):
            continue
        index = indices[0]
        tail = host.ops[index]
        if "GOTO_TB" not in tail.sites:
            continue
        # Each counted chainN execution used this unique slot. Walk only
        # contiguous NOPs that every path into the jump must have executed.
        while index != cfg.entry:
            previous = cfg.predecessors(index, atomic=False)
            if len(previous) != 1:
                break
            pred = previous[0]
            op = host.ops[pred]
            if (op.kind != OpKind.NOP or op.off not in once or op.end != host.ops[index].off
                    or cfg.succ_full[pred] != (index,)):
                break
            floor, ceiling = counts[slot], entries - counts.get(1 - slot, 0)
            if floor > ceiling:
                raise ValueError("outgoing unflagged slot traffic exceeds exact entries")
            observed[op.off] = {"slot": slot, "jump_offset": tail.off, "minimum_executions": floor,
                                "maximum_executions": ceiling}
            index = pred
    return observed


def mandatory_call_exits(host, cfg, counts, offsets):
    """Count observed ordinary slot exits whose every path goes through a CALL."""
    if host.status != "ok" or not cfg.ok or not offsets:
        return {}
    def reachable_without(blocked):
        seen, pending = set(), [cfg.entry]
        while pending:
            i = pending.pop()
            if i == blocked or i in seen:
                continue
            seen.add(i)
            pending.extend(cfg.succ_full[i])
        return seen
    reached = reachable_without(None)
    exits = {slot: {i for i, kind in cfg.exits.items() if kind == f"goto_tb:{slot}"} & reached
             for slot in counts}
    result = {}
    for i, op in enumerate(host.ops):
        if op.kind != OpKind.CALL or op.off not in offsets or i not in reached:
            continue
        bypass = reachable_without(i)
        proved = {str(slot): counts[slot] for slot, endpoints in exits.items()
                  if endpoints and not bypass.intersection(endpoints)}
        if proved:
            result[op.off] = {"guaranteed_by_slot": proved, "minimum_executions": sum(proved.values())}
    return result


def replay_counts(path, identity):
    if not path:
        return {}, None
    data = Path(path).read_bytes()
    doc = json.loads(data)
    if doc.get("schema") != "trebuchet.untimed-site-counts/1" or doc.get("identity") != identity:
        raise ValueError("untimed site histogram identity or schema differs")
    hits = {}
    for row in doc["sites"]:
        key = row["body"], row["offset"], row["bytes"]
        count = row["executions"]
        if (not isinstance(key[0], str) or type(key[1]) is not int or key[1] < 0
                or not isinstance(key[2], str) or not key[2] or type(count) is not int
                or count < 0 or key in hits):
            raise ValueError("invalid or duplicate site in untimed histogram")
        hits[key] = count
    return hits, sha256_id(data)


def weight(key, hits, bound):
    if key in hits:
        return {"kind": "observed_untimed_replay", "executions": hits[key]}
    if bound is not None:
        return {"kind": "profile_upper_bound", "executions": bound}
    return {"kind": "unknown", "executions": None}


def encoding_requests(path, snap):
    if not path:
        return [], {}
    data = json.loads(Path(path).read_text())
    artifact = read_artifact(data["artifact_dir"])
    if verify_manifest(artifact.path):
        raise ValueError("optimizer artifact file manifest differs")
    if not artifact.artifact_id or artifact.artifact_id != data.get("artifact_id") or (
            artifact.output_snapshot_id != snap.snapshot_id):
        raise ValueError("optimizer receipt belongs to another artifact")
    records, coverage = [], {}
    for stage in data["passes"]:
        name = stage["pass"]
        if name not in ("constant-propagation", "branch-folding"):
            continue
        captured = stage.get("params", {}).get("record_encoding_sites") is True
        coverage.setdefault(name, []).append({"site_capture": captured, "outcome": stage["outcome"]})
        for key, occurrences in stage.get("stats", {}).items():
            if captured and key.startswith("encoding_site|"):
                _, pred, slot, general, off, raw = key.split("|")
                records.append((name, (pred, int(slot), general), int(off), raw, occurrences))
    return records, coverage


def cost_proxy(call, callee, insns, off):
    mark = (call or {}).get("opmark_off")
    before = None
    if mark is not None and mark in insns and mark <= off:
        setup = [i for o, i in insns.items() if mark <= o < off]
        if sum(i.size for i in setup) == off - mark and all(not i.bad for i in setup):
            before = len(setup)
    following = insns.get(off + insns[off].size) if off in insns else None
    result_move = None if following is None else (
        following.mnemonic == "mov" and following.op_str.split(", ")[-1] in ("rax", "eax", "ax", "al"))
    return {"opmark_to_call_instructions": before, "bound_function_bytes": (callee or {}).get("size"),
            "next_is_move_from_integer_return_register": result_move}


def weighted_sites(rows):
    totals = Counter()
    for row in rows:
        w = row["weight"]
        totals[w["kind"] + "_sites"] += 1
        if w["executions"] is not None:
            totals[w["kind"] + "_executions"] += w["executions"]
    return dict(totals)


def physical_bodies(snap, units):
    bodies = [(u.uid, u, u.code, u.tc_offset, None) for u in units.values()]
    variants = {}
    if snap.header.version == 6:
        f6 = read_sc6(Path(snap.path) / "cache.sc")
        if not f6.loadable:
            raise ValueError("unloadable v6 snapshot")
        joined, _ = _verified_captures(snap)
        for v in import_variants(f6, list(units.values()), joined.captures):
            if not v["ok"]:
                raise ValueError(f"variant verification failed: {v['problems']}")
            idx = v["variant_index"]
            origin = (v["pred_unit"], v["slot_n"], v["general_unit"])
            if origin in variants:
                raise ValueError("ambiguous physical variant origin")
            variants[origin] = v["variant_key"]
            bodies.append((v["variant_key"], units[v["general_unit"]], f6.variant_bodies[idx],
                           f6.ext.variants[idx].tc_offset, origin))
    return bodies, variants


def census(args):
    if getattr(args, "archived_ordinary", False):
        from archived_snapshot import archived_ordinary
        snap, import_check = archived_ordinary(args.snapshot, args.elf)
    else:
        snap = open_snapshot(args.snapshot)
        import_check = {"mode": "full_store_reimport"}
    img = load_guest_image(args.elf)
    if not snap.bundle or img.sha256 != snap.bundle.get("image_hash"):
        raise ValueError("guest ELF does not match captured bundle image_hash")
    view = None
    if args.profile:
        profile = read_profile(args.profile)
        view = ProfileView.open(profile, snap)
        pid = profile.phase_id(args.phase)
        if not view.same_snapshot or pid is None or not profile.phase_reached(pid):
            raise ValueError("profile must name this exact snapshot and a reached phase")
    identity = {"image_hash": img.sha256, "snapshot_id": snap.snapshot_id, "phase": args.phase,
                "application": args.application, "scenario": args.scenario,
                "profile_id": view.profile_id if view else None}
    slots = whole_run_slot_counts(view, args.phase)
    hits, replay_hash = replay_counts(args.site_counts, identity)
    selected = set()
    requests, coverage = encoding_requests(args.optimizer_result, snap)
    native = prepare_native_images(snap, backend_revision="offline-census", bridge_version=1, policy_version=1)
    store = AnalysisStore(snap, external_inputs={NATIVE_IMAGES: native})
    bindings, _ = _calls(snap, store, view, args.phase)
    callees = {(r["call"]["uid"], r["call"]["host_off"]): r["callee"] for r in bindings}
    live, retired, incomplete = _inventory(snap, bindings, view, args.phase)
    calls_by_unit = defaultdict(dict)
    for (uid, off), row in live.items():
        calls_by_unit[uid][off] = row
    function_sizes = {}
    for binding in native.value.bindings.values():
        callee = binding.callee
        if callee is None or callee.code_hash in function_sizes:
            continue
        instructions = list(sweep(callee.code, callee.runtime_address))
        function_sizes[callee.code_hash] = (None if any(i.bad for i in instructions)
                                            else len(instructions))
    units = {u.uid: u for u in snap.units()}
    bodies, variants = physical_bodies(snap, units)
    refusal_by_origin = defaultdict(list)
    refusals = []
    for stage, origin, off, raw, occurrences in requests:
        row = {"pass": stage, "origin": list(origin), "offset": off, "bytes": raw,
               "optimizer_observations": occurrences, "body": variants.get(origin),
               "status": "variant_absent", "weight": weight(None, {}, None)}
        refusals.append(row)
        refusal_by_origin[origin].append(row)
    calls, nops = [], []
    excluded = set(incomplete)
    for body, unit, code, tc_offset, origin in bodies:
        physical = decoded(unit, code, snap.header.code_address + tc_offset) if unit.meta_status == "ok" else {}
        if unit.meta_status != "ok" or any(i.bad for i in physical.values()):
            excluded.add(body)
            physical = {}  # boundaries following a bad byte are ambiguous
        entries = view.count(unit.uid, args.phase) if view and origin is None else None
        once = at_most_once(store, unit) if physical and entries and entries.exact else set()

        evidence, call_evidence = {}, {}
        if entries and entries.exact and slots.get(unit.uid):
            scope = ScopeRef("unit", unit.uid)
            host, cfg = store.get("hostops", scope).value, store.get("hcfg", scope).value
            evidence = exit_nop_evidence(host, cfg, once, slots[unit.uid], entries.value)
            offsets = {off for off, row in calls_by_unit[unit.uid].items() if
                       (row.get("call") or {}).get("helper_name") not in (None, "lookup_tb_ptr")}
            call_evidence = mandatory_call_exits(host, cfg, slots[unit.uid], offsets)

        def site_weight(off):
            key = body, off, physical[off].raw.hex()
            selected.add(key)
            return weight(key, hits, entries.value if off in once else None)

        for off, inst in physical.items():
            if inst.mnemonic == "nop":
                nops.append({"body": body, "specialized": origin is not None, "offset": off,
                             "bytes": inst.raw.hex(), "size": inst.size, "weight": site_weight(off),
                             **({"exit_evidence": evidence[off]} if off in evidence else {})})
        for row in refusal_by_origin.get(origin, ()):
            off = row["offset"]
            match = off in physical and physical[off].raw.hex() == row["bytes"]
            row["status"] = "matched" if match else "bytes_changed_or_uninspected"
            if match:
                row["weight"] = site_weight(off)
        sites = (calls_by_unit[unit.uid] if origin is None else
                 {off: {"annotation": "private_decoded_call"} for off, i in physical.items() if i.is_call})
        for off, site in sites.items():
            seen = off in physical and physical[off].is_call
            callee = callees.get((unit.uid, off)) if origin is None else None
            cost = cost_proxy(site.get("call"), callee, physical, off)
            cost["bound_function_linear_instructions"] = function_sizes.get((callee or {}).get("code_hash"))
            calls.append({**_public_row(site), "body": body, "specialized": origin is not None,
                          "offset": off, "bytes": physical[off].raw.hex() if seen else None,
                          "physical_call_verified": seen,
                          **({"exit_evidence": call_evidence[off]} if seen and off in call_evidence else {}),
                          "callee": callee, "cost_proxy": cost,
                          "weight": site_weight(off) if seen else weight(None, {}, None)})
    archive_sha = None
    if getattr(args, "archived_qemu", None):
        from archived_snapshot import archive_function_footprints
        archive_sha = archive_function_footprints(snap, args.archived_qemu, calls)
    if set(hits) - selected:
        raise ValueError("untimed histogram includes unknown physical site or differing instruction bytes")
    totals = defaultdict(Counter)
    for row in nops:
        t = totals["specialized" if row["specialized"] else "ordinary"]
        w = row["weight"]
        if "exit_evidence" in row:
            floor = row["exit_evidence"]["minimum_executions"]
            t["exit_proven_minimum_instructions"] += floor
            t["exit_proven_minimum_bytes"] += floor * row["size"]
            t["exit_proven_sites"] += 1
        t["static_bytes"] += row["size"]
        t["static_instructions"] += 1
        t[w["kind"] + "_sites"] += 1
        if w["executions"] is not None:
            t[w["kind"] + "_instructions"] += w["executions"]
            t[w["kind"] + "_bytes"] += w["executions"] * row["size"]
    return {"schema": "trebuchet.site-census/1", "identity": identity,
            "import_check": import_check, "bundle_id": snap.bundle["bundle_id"], "capture_ids": native.value.capture_ids,
            "untimed_histogram_sha256": replay_hash, "uninspected_bodies": sorted(excluded),
            "archived_qemu_sha256": archive_sha, "native_images_id": native.input_id, "native_binding_reason": native.value.reason,
            "calls": calls, "lookup_helper_profile_events": observed_lookup_events(view.profile if view else None, args.phase),
            "physical_call_volume": weighted_sites(
                c for c in calls if c["physical_call_verified"]),
            "native_fragments": [{**_public_row(r), "certified_fragment_growth_bytes": (
                r["fragment"].growth if r["capture_resolved"] else None)}
                                 for r in retired.values()], "nops": nops, "nop_volume": totals,
            "encoding_capture_by_stage": coverage, "encoding_refusals": refusals,
            "matched_refusal_volume_by_pass": {
                stage: weighted_sites(r for r in refusals if r["pass"] == stage and r["status"] == "matched")
                for stage in coverage}}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("snapshot", "elf", "application", "scenario", "phase", "out"):
        p.add_argument("--" + name, required=True)
    for name in ("profile", "site-counts", "optimizer-result"):
        p.add_argument("--" + name)
    p.add_argument("--archived-qemu", help="hash-matched executable for static helper costs")
    p.add_argument("--archived-ordinary", action="store_true",
                   help="verify exported v5 physical regions when original host files are unavailable")
    args = p.parse_args()
    output = census(args)
    Path(args.out).write_text(json.dumps(output, sort_keys=True, indent=2) + "\n")


if __name__ == "__main__":
    main()
