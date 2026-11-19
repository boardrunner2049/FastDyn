"""Retire ordinary direct jumps with low exact steady training reuse."""
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import re

from ..link import capture_join
from ..ordinary_exit import PASS, OrdinaryExit, capture, one, require, retire, targets
from .base import EditKind, Outcome, PassDescriptor, PassResult


_LINK = re.compile(r"Linking TBs (0x[0-9a-f]+) index ([01]) -> (0x[0-9a-f]+)")


def observed_installations(snapshot, profile, manifest_path):
    path = Path(manifest_path)
    doc = json.loads(path.read_bytes())
    require(set(doc) == {"schema", "input_snapshot_id", "qemu_sha256", "profile_id", "trace",
                         "trace_sha256", "steady_lines"} and doc["schema"] == "scopt.ordinary_exit_training/1",
            "invalid ordinary-exit training manifest")
    require(doc["input_snapshot_id"] == snapshot.snapshot_id and
            doc["qemu_sha256"] == snapshot.runtime["qemu_sha256"] and
            doc["profile_id"] == profile.profile_id, "ordinary-exit training manifest identity mismatch")
    trace = Path(doc["trace"])
    raw = (path.parent / trace).read_bytes()
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    require(doc["trace_sha256"] == digest, "ordinary-exit training trace SHA-256 mismatch")
    lines = raw.decode().splitlines()
    lo, hi = doc["steady_lines"]
    require(type(lo) is int and type(hi) is int and 1 <= lo <= hi <= len(lines)+1, "invalid steady trace bounds")
    by_addr = {snapshot.header.code_address + u.tc_offset: u for u in snapshot.units()}
    found = defaultdict(set)
    for line in lines[lo-1:hi]:
        match = _LINK.fullmatch(line)
        if match:
            a, n, b = match.groups()
            require(int(a,16) in by_addr and int(b,16) in by_addr, "trace links an unknown TB")
            found[by_addr[int(a,16)].uid, int(n)].add(by_addr[int(b,16)].uid)
        elif line.startswith("Linking TBs "):
            raise ValueError("malformed ordinary direct link trace line")
    return found, digest


class RetireOrdinaryExits:
    descriptor = PassDescriptor(name=PASS, version=1, requires_profile_phase="steady",
                                capabilities=frozenset({EditKind.RETIRE_DIRECT_EXIT}),
                                invalidates=frozenset({"code", "meta"}),
                                params_schema={"training_manifest": str, "max_transfers": int})

    def run(self, snap, ctx, edits):
        view = ctx.profile("steady")
        profile = view.profile if view is not None else None
        manifest = ctx.params.get("training_manifest")
        cutoff = ctx.params.get("max_transfers", 1)
        if not manifest or type(cutoff) is not int or cutoff < 1:
            return PassResult(Outcome.FAILED, "ordinary_exit_training_required",
                              "a bound training manifest and positive integer transfer cutoff are required")
        if (profile is None or not view.same_snapshot or not profile.collection["chain_disabled"] or
                not profile.collection["complete"] or profile.collection["overflow"]):
            return PassResult(Outcome.FAILED, "ordinary_exit_exact_profile_required")
        base = snap.working_set.base
        try:
            join = capture_join(base)
            epilogue, tb_ret = capture(base, join)
            installs, digest = observed_installations(snap, profile, manifest)
            entries = targets(snap.units(), snap.header.code_address)
            edges = defaultdict(list)
            for e in view.edges("steady"):
                if e["kind"] in ("chain0", "chain1"):
                    edges[e["src"], int(e["kind"][-1])].append(e)
            refused = 0
            changed = []
            for u in snap.units():
                for n in (0, 1):
                    ctx.budget()
                    hits = edges.get((u.uid, n), ())
                    if len(hits) != 1:
                        continue
                    e = hits[0]
                    if (e["status"] != "exact" or e["flags"] or not 0 < e["count"] <= cutoff or
                            installs.get((u.uid, n)) != {e["dst"]}):
                        continue
                    binding = join.slots.get(u.slot)
                    try:
                        require(binding is not None and binding.bound, "source capture not bound")
                        retire(u, binding.chunk, (n,), snap.header.code_address, epilogue, tb_ret, entries)
                        direct = one((x for x in u.static_exits if x.direct and x.slot == n),
                                     "source unit has no unique captured direct exit")
                        require(direct.dest_vaddr in snap.unit(e["dst"]).vaddrs, "profile destination disagrees")
                    except ValueError:
                        refused += 1
                        continue
                    edits.retire_direct_exit(OrdinaryExit(u.uid, u.content_hash, n, e["dst"], e["count"], digest))
                    changed.append(u.uid)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return PassResult(Outcome.FAILED, "ordinary_exit_training_invalid", str(exc))
        return PassResult(Outcome.CANDIDATE if changed else Outcome.NO_CHANGE,
                          "ordinary_exits_retired" if changed else "no_eligible_ordinary_exits",
                          affected=tuple(dict.fromkeys(changed)),
                          stats={"retired_exits":len(changed),"refused_exits":refused})


PASSES = (RetireOrdinaryExits,)
