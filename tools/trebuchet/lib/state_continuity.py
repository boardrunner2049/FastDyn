#!/usr/bin/env python3
"""Inventory state store/reload pairs at profiled TB chain slots, without editing code."""
import argparse
from collections import Counter, defaultdict
from functools import lru_cache
import json
from pathlib import Path

from scopt.analysis.envlayout import envlayout_for_runtime
from scopt.analysis.hcfg import build_hcfg
from scopt.analysis.hostops import OpKind, build_hostops, reg_family
from scopt.analysis.obs_contract import D1_FIELDS
from scopt.formats.profile_v1 import ProfileView, read_profile
from scopt.importer.import_cache import open_snapshot
from scopt.run.launcher import file_sha256, write_json


def observation(op):
    if op.kind == OpKind.CALL:
        return "helper_or_callback"
    if op.ldst is not None or op.kind == OpKind.GUEST_ACCESS:
        return "guest_access_fault_or_slow_path"
    if op.block == "prefix" and op.kind == OpKind.JCC:
        return "entry_check_or_interrupt_exit"
    if op.kind in (OpKind.OPAQUE, OpKind.ENV_ADDR) or any(not a.exact for a in op.env):
        return "unknown_effect_or_alias"
    if op.kind in (OpKind.HOST_ABS_STORE, OpKind.HOST_ABS_LOAD) and op.abs_addr is None:
        return "unknown_host_access"
    return None


def boundary_accesses(host, graph, exit_kind=None):
    """Nearest store on each path to the selected exit, or first read from public entry."""
    paths = graph.paths() if graph.ok and host.ok else None
    if paths is None:
        return {"reason": list(graph.reasons or host.reasons) or ["cyclic_or_path_budget"], "sites": []}
    predecessor = exit_kind is not None
    if predecessor:
        paths = [p for p in paths if graph.exit_kind(p[-1]) == exit_kind]
    sites = {}
    for path in paths:
        blocked_bytes = set()
        observations = set()
        clobbers = set()
        for i in (reversed(path) if predecessor else path):
            op = host.ops[i]
            reason = observation(op)
            if reason:
                observations.add((op.off, reason))
            for access in op.env:
                span = set(range(access.disp, access.disp + access.width))
                eligible = (op.kind in (OpKind.ENV_STORE_REG, OpKind.ENV_STORE_IMM) and access.rw == "w"
                            if predecessor else op.kind == OpKind.ENV_LOAD and access.rw == "r")
                if eligible and access.exact and access.field in D1_FIELDS and not span & blocked_bytes:
                    key = (op.off, access.field, access.disp, access.width)
                    site = sites.setdefault(key, {
                        "host_off": op.off, "guest_insn": op.insn, "field": access.field, "disp": access.disp,
                        "width": access.width, "register": op.src_reg if predecessor else op.dst_reg,
                        "immediate": op.imm if op.kind == OpKind.ENV_STORE_IMM else None,
                        "paths": 0, "observers": set(), "clobbers": set(),
                    })
                    site["paths"] += 1
                    site["observers"].update(observations)
                    site["clobbers"].update(clobbers)
                # On the predecessor, loads don't replace the last stored value.
                # On the successor, a first overlapping read or write closes the census.
                if not predecessor or "w" in access.rw:
                    blocked_bytes.update(span)
            clobbers.update(op.regs_written)
            clobbers.update(op.regs_partial)
    for site in sites.values():
        site["observers"] = [{"host_off": off, "reason": why} for off, why in sorted(site["observers"])]
        site["clobbers"] = sorted(site["clobbers"])
        site["paths_analyzed"] = len(paths)
    return {"sites": list(sites.values()), "paths": len(paths), "reason": [] if paths else ["no_matching_path"]}


def match_edge(edge, source, target):
    """A site pair can occur at most once per modeled acyclic crossing; zero is possible."""
    pairs = []
    for store in source["sites"]:
        for load in target["sites"]:
            if (store["field"], store["disp"], store["width"]) != (load["field"], load["disp"], load["width"]):
                continue
            register = reg_family(store["register"]) if store["register"] else None
            clobbers = set(store["clobbers"]) | set(load["clobbers"])
            pairs.append({"store": store, "reload": load,
                          "known_value_register_clobber": bool(register and register[0] in clobbers),
                          "possible_pair_crossings_upper_bound": edge["count"] if edge["status"] == "exact" else None})
    return pairs


def census(snapshot, profile, phase="steady", limit=100):
    view = ProfileView.open(profile, snapshot, phase=phase)
    if not view.same_snapshot or profile.phase_id(phase) is None or not profile.phase_reached(profile.phase_id(phase)):
        raise ValueError("requires an exact snapshot binding and a reached profile phase")
    layout = envlayout_for_runtime(snapshot.runtime)
    if not layout["bound"]:
        raise ValueError(f"DWARF layout unavailable for the snapshot runtime: {layout['reason']}")
    if limit < 1:
        raise ValueError("edge report limit must be positive")

    @lru_cache(maxsize=2048)
    def boundary(uid, slot):
        unit = snapshot.unit(uid)
        if unit.meta_status != "ok" or not unit.reloc.relocatable:
            return {"sites": [], "reason": ["metadata_or_relocation_unavailable"]}
        host = build_hostops(unit, layout["layout"], snapshot.header.code_address)
        return boundary_accesses(host, build_hcfg(host), slot)

    edges = view.edges(phase)
    incoming = defaultdict(set)
    for edge in edges:
        incoming[edge["dst"]].add(edge["src"])
    rows = []
    matched_edges = 0
    flagged = Counter()
    traffic = Counter()
    reasons = Counter()
    for edge in sorted(edges, key=lambda e: (e["status"] != "exact", -e["count"], e["kind"],
                                              e["src"] or "", e["dst"], e["flags"])):
        kind, status, count = edge["kind"], edge["status"], edge["count"]
        traffic[(kind, status)] += count
        if kind not in ("chain0", "chain1") or not edge["src"] or not count:
            continue
        if edge["flags"]:
            flagged[("+".join(edge["flags"]), status)] += count
            continue
        source = boundary(edge["src"], "goto_tb:" + kind[-1])
        target = boundary(edge["dst"], None)
        pairs = match_edge(edge, source, target)
        if not pairs:
            reasons.update(source["reason"] + target["reason"] or ["no_matching_access"])
            continue
        matched_edges += 1
        if len(rows) >= limit:
            continue
        rows.append({"src": edge["src"], "dst": edge["dst"], "kind": kind, "traffic": count,
                     "traffic_status": status, "traffic_reason": edge.get("reason"),
                     "observed_predecessors": len(incoming[edge["dst"]]),
                     "public_entry_possible": True, "pairs": pairs})
    return {
        "schema": "trebuchet.state-continuity-census/1", "snapshot_id": snapshot.snapshot_id,
        "profile_id": profile.profile_id, "phase": phase, "collection_method": profile.method,
        "envlayout_id": layout["envlayout_id"],
        "traffic": [{"kind": k, "status": st, "count": n} for (k, st), n in sorted(traffic.items())],
        "edges_with_pairs": matched_edges, "reported_edges": len(rows), "edges": rows,
        "flagged_chain_traffic_excluded": [{"flags": k, "status": st, "count": n}
                                          for (k, st), n in sorted(flagged.items())],
        "unmatched_edge_reasons": dict(reasons),
        "interpretation": [
            "Counts cover transitions with endpoints in the snapshot domain. Counts describe profiled exits. An unchained capture cannot prove a runtime chain hit.",
            "Paths are static atomic host CFG paths. Fault, callback, IRQ and recovery paths may observe memory.",
            "A per-pair upper bound pertains to traversals of that profiled edge, not time or measured state accesses.",
            "Independent entry remains possible even if only one predecessor was observed.",
            "No private-entry, guard-register, exit-materialization or recovery proof is established here.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("profile", type=Path)
    parser.add_argument("--phase", default="steady")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--registry", type=Path, help="Bind the diagnostic to an admitted application's ordinary image")
    parser.add_argument("--image", choices=("original", "firmbolt"), default="original")
    args = parser.parse_args()
    snapshot = open_snapshot(args.snapshot)
    profile = read_profile(args.profile)
    binding = None
    if args.registry:
        registry = json.loads(args.registry.read_text())
        admission = registry["admission"]
        record = json.loads(Path(admission["path"]).read_text())
        matches = [r for r in registry["candidates"] if r["id"] == args.image + "/warm" and r["status"] == "ready"
                   and r["ordinary_snapshot_id"] == snapshot.snapshot_id and r["profile_id"] == profile.profile_id]
        if (registry["state"] != "completed" or record["status"] != "admitted" or len(matches) != 1
                or record["application"] != registry["application"] or file_sha256(admission["path"]) != admission["sha256"]):
            raise ValueError("admitted ordinary deployment or admission receipt does not match")
        binding = {"application": registry["application"], "registry_sha256": file_sha256(args.registry),
                   "deployment_identity": matches[0]["deployment_identity"], "admission_sha256": admission["sha256"]}
    doc = census(snapshot, profile, args.phase, args.limit)
    doc["application_binding"] = binding
    write_json(args.out, doc)


if __name__ == "__main__":
    main()
