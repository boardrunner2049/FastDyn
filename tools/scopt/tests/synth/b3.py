"""Shared helpers for the WP18 tests (B3/B4 design §2-§5, §9.1).

Real snapshots are the B1/B2 store snapshots (read-only; tests skip when they
are absent). The v6 identity used for V7/V20 is a **stand-in**: the WP17 v2
RuntimeIdentity of the compute snapshot with its exe sha256 and EnvLayout id
replaced by those of the (not yet frozen) ``qint-v6`` build, whose EnvLayout
WP18 produced with ``gdb -nx`` into ``runs/wp18/envlayout/``. The snapshots
themselves were captured on ``qint``; the stand-in only exercises the V7/V20
logic, it is not a claim about a real qint-v6 run.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from scopt.canon import content_id
from scopt.paths import fastdyn_root

ROOT = fastdyn_root()
SNAPS = {
    "minimal": ROOT / "runs/wp15a/minimal/store/snapshots/"
    "f1ed14d98db4bcae54a1c37f07b1d6fabcfcea345a56edf1df1846c4d0aaee44",
    "fault": ROOT / "runs/wp15a/fault/store/snapshots/"
    "4e404d5e7335399768249ef95453594fe8098647ef7927c1234c5a27dee16841",
    "compute": ROOT / "runs/b2/compute/store/snapshots/"
    "53b4132fc05526ff2925cd3acc610bf613e8b11e2315e15e520bff7fe080dab2",
}
QINT_V6_SHA = "56d8bfb32c91c970fe0a16237b962c02791ce0a7fcc52bf6246343fbef430d90"
V6_LAYOUT_DIR = ROOT / "runs/wp18/envlayout"
COMPUTE_RI_V2 = ROOT / "runs/wp17/compute.runtime_identity.v2.json"
_CACHE: dict[str, Any] = {}


def snap(name: str):
    p = SNAPS[name]
    if not (p / "snapshot.json").is_file():
        pytest.skip(f"{p} not present")
    if name not in _CACHE:
        from scopt.importer.import_cache import open_snapshot

        _CACHE[name] = open_snapshot(p)
    return _CACHE[name]


def guest_bytes(s) -> dict[str, bytes]:
    key = ("gb", id(s))
    if key not in _CACHE:
        from scopt.link import capture_join

        jr = capture_join(s)
        _CACHE[key] = {u.uid: jr.slots[u.slot].chunk.guest_bytes for u in s.units()
                       if jr.slots.get(u.slot) is not None and jr.slots[u.slot].bound}
    return _CACHE[key]


def layout_for(s):
    from scopt.formats.envlayout_v1 import find_envlayout

    lay = find_envlayout(s.runtime.get("qemu_sha256"))
    if lay is None:
        pytest.skip("no packaged EnvLayout for the snapshot's exe")
    return lay


def offsets_for(s):
    from scopt.guard import GuardOffsets

    return GuardOffsets.from_layout(layout_for(s))


def v2_identity() -> dict[str, Any]:
    if not COMPUTE_RI_V2.is_file():
        pytest.skip(f"{COMPUTE_RI_V2} not present")
    return json.loads(COMPUTE_RI_V2.read_text())


def v6_identity(monkeypatch=None) -> dict[str, Any]:
    """The stand-in v6 RuntimeIdentity (see the module docstring). With
    ``monkeypatch``, ``SCOPT_ENVLAYOUT_PATH`` gets the qint-v6 layout dir."""
    lay = V6_LAYOUT_DIR / f"{QINT_V6_SHA}.json"
    if not lay.is_file():
        pytest.skip(f"{lay} not present")
    if monkeypatch is not None:
        monkeypatch.setenv("SCOPT_ENVLAYOUT_PATH", str(V6_LAYOUT_DIR))
    ri = v2_identity()
    lid = json.loads(lay.read_text())["envlayout_id"]
    ri6 = dict(ri, qemu_sha256="sha256:" + QINT_V6_SHA,
               admission=dict(ri["admission"], exe_sha256="sha256:" + QINT_V6_SHA,
                              envlayout_id=lid))
    ri6.pop("runtime_identity_id", None)
    ri6["runtime_identity_id"] = content_id(ri6, "runtime_identity_id")
    return ri6


def dummy_specs(s, *, kind: str = "GENERAL", classes=("return", "static"),
                limit: int | None = None) -> dict[str, tuple]:
    """A one-target dummy guard on every admissible tail of every relocatable unit
    of exit class in ``classes``: the target is the unit's own first vaddr and
    key (THUMB set, CONDEXEC/SS cleared) in its first free slot."""
    from scopt.guard import CS_CONDEXEC_MASK, CS_THUMB, PSTATE_SS, GuardTarget
    from scopt.link import (GuardedExitSpec, exit_class_of, free_slots, lookup_tails,
                            tail_problem)

    gb = guest_bytes(s)
    out: dict[str, tuple] = {}
    for u in s.units():
        if not u.reloc.relocatable or u.meta_status != "ok":
            continue
        free = free_slots(u)
        specs = []
        for t in lookup_tails(u):
            if tail_problem(u, t, s.header.code_address) or not free:
                continue
            xc = exit_class_of(u, t, gb.get(u.uid))
            if xc not in classes:
                continue
            n = free[0]
            free = free[1:]
            specs.append(GuardedExitSpec(
                t.off, xc, (GuardTarget(u.vaddrs[0] & ~1, n, kind),),
                u.key.flags & ~PSTATE_SS, (u.key.cs_base & ~CS_CONDEXEC_MASK) | CS_THUMB))
        if specs:
            out[u.uid] = tuple(specs)
        if limit is not None and len(out) >= limit:
            break
    return out


def spec_pairs(s, rs: list[Any], cs: list[Any]):
    """Pair grown R units (2 free slots, one tail) with SPEC targets C (two per R,
    distinct vaddrs): returns ``(grow specs, variant specs)``."""
    from scopt.guard import CS_CONDEXEC_MASK, CS_THUMB, PSTATE_SS, GuardTarget
    from scopt.link import GuardedExitSpec, VariantSpec, exit_class_of, lookup_tails

    gb = guest_bytes(s)
    grow, vars_ = {}, []
    ci = 0
    for r in rs:
        if ci >= len(cs):
            break
        t = lookup_tails(r)[0]
        xc = exit_class_of(r, t, gb.get(r.uid))
        xc = xc if xc in ("return", "static") else "static"
        pick = []
        while ci < len(cs) and len(pick) < 2:
            c = cs[ci]
            ci += 1
            if pick and (c.vaddrs[0] == pick[0].vaddrs[0] or
                         (c.key.flags, c.key.cs_base) != (pick[0].key.flags,
                                                          pick[0].key.cs_base)):
                ci -= 1
                break
            pick.append(c)
        if not pick:
            continue
        c0 = pick[0]
        tg = tuple(GuardTarget(c.vaddrs[0] & ~1, n, "SPEC") for n, c in enumerate(pick))
        grow[r.uid] = (GuardedExitSpec(t.off, xc, tg, c0.key.flags & ~PSTATE_SS,
                                       (c0.key.cs_base & ~CS_CONDEXEC_MASK) | CS_THUMB),)
        vars_ += [VariantSpec(c.uid, r.uid, n) for n, c in enumerate(pick)]
    return grow, vars_


def spec_ok(u) -> bool:
    from scopt.guard import CS_CONDEXEC_MASK, CS_THUMB, PSTATE_SS, SS_ACTIVE

    k = u.key
    return (u.reloc.relocatable and u.meta_status == "ok" and bool(k.cs_base & CS_THUMB)
            and not k.cs_base & CS_CONDEXEC_MASK and not k.flags & (PSTATE_SS | SS_ACTIVE)
            and k.page_addr1 is None and bool(u.vaddrs))


def r_hosts(s) -> list[Any]:
    """Units that can host two SPEC slots (one admissible tail, both slots free)."""
    from scopt.link import free_slots, lookup_tails, tail_problem

    out = []
    for u in s.units():
        ts = lookup_tails(u)
        if u.reloc.relocatable and len(ts) == 1 and len(free_slots(u)) == 2 and \
                not tail_problem(u, ts[0], s.header.code_address):
            out.append(u)
    return out
