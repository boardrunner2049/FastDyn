"""A small synthetic world for Profile v1 tests (WP6).

``make_world(tmp)`` writes:

* ``snap.sc``: the snapshot, 3 units (slots 0-2);
* ``staged.sc``: the run's copy after the run: the same 3 records plus 2
  appended ones (slots 3-4). Slot 3 has the **same tb_uid** as slot 0 (same key
  and guest bytes; a retranslation), slot 4 is a new TB;
* ``other.sc``: a different snapshot holding units 0 and 1 of ``snap.sc`` plus a
  new unit (for UnitId joins and coverage);

and returns a :class:`World` with a trace that exercises every edge kind, IRQ
and LONGJMP flags, a one-shot (TRANSIENT) TB and both appended TBs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import Any, Sequence

from scopt.formats import profile_v1 as P

from . import builders as B

SHA_BUNDLE = "sha256:" + "b" * 64
SHA_CONTRACT = "sha256:" + "c" * 64
RUNTIME = {
    "qemu_sha256": "sha256:" + "1" * 64, "plugin_sha256": "sha256:" + "2" * 64,
    "runtime_signature": "0xcd59117cbc9f8837", "prologue_sha256": "sha256:" + "3" * 64,
    "cpuinfo": "0x1f", "qemu_exe_stat": {"size": 1, "mtime_ns": 2},
}
TRANSIENT_OFF = 0x8000
MARKER = 0x20000500
PHASES = ({"name": "boot", "marker": 0}, {"name": "steady", "marker": MARKER})


@dataclass
class World:
    dir: str
    units: list  # SynthUnit, slots 0..4 of staged.sc
    snap_sc: str
    staged_sc: str
    other_sc: str
    code_size: int
    rows: list[dict[str, Any]]

    @property
    def offs(self) -> list[int]:
        return [u.tb_offset for u in self.units]

    def binding(self, sc: str | None = None) -> dict[str, Any]:
        return B.qprof_binding(sc or self.snap_sc)

    def trace(self) -> list[B.Dispatch]:
        o = self.offs
        D = B.Dispatch
        return [
            D(o[0]),                                   # FIRST
            D(o[1], exit="chain1"),                    # chain0 0->1
            D(o[0], exit="lookup_hit"),                # chain1 1->0
            D(o[3], via_lookup=True, exit="exit_null"),  # lookup_hit 0->3 (PROFILE_ONLY)
            D(TRANSIENT_OFF, flags=1),                 # exit_null 3->T, IRQ
            D(o[2], exit="exit_requested"),            # chain0 T->2
            D(o[2], exit="longjmp"),                   # exit_requested 2->2 (aborted)
            D(o[0]),                                   # exit_null+LONGJMP 2->0   <- steady starts here (idx 7)
            D(o[1], exit="lookup_miss"),               # chain0 0->1
            D(o[4]),                                   # lookup_miss 1->4 (PROFILE_ONLY)
            D(o[0]),                                   # chain0 4->0
            D(o[1]),                                   # chain0 0->1 (last)
        ]

    def qprof(self, name: str = "run.qprof", *, trace: Sequence | None = None,
              phase_starts: dict | None = None, header: dict | None = None,
              phases: Sequence = PHASES) -> str:
        path = os.path.join(self.dir, name)
        if os.path.exists(path):
            os.unlink(path)
        h = {**self.binding(), **(header or {})}
        B.simulate_qprof(self.rows, self.trace() if trace is None else trace, phases=phases,
                         phase_starts={1: 7} if phase_starts is None else phase_starts,
                         header=h, path=path)
        return path

    def ingest(self, qprof: str | None = None, **kw) -> P.Profile:
        args = dict(bundle_id=SHA_BUNDLE, contract_id=SHA_CONTRACT, runtime_identity=RUNTIME)
        args.update(kw)
        staged = args.pop("staged", self.staged_sc)
        snap = args.pop("snapshot", self.snap_sc)
        return P.ingest(qprof or self.qprof(), staged, snap, **args)

    def target(self, sc: str | None = None, **kw) -> P.ProfileTarget:
        args = dict(bundle_id=SHA_BUNDLE, contract_id=SHA_CONTRACT, runtime_identity=RUNTIME)
        args.update(kw)
        return P.ProfileTarget.from_any(sc or self.snap_sc, **args)


def make_world(directory: str) -> World:
    os.makedirs(directory, exist_ok=True)
    base = B.simple_units(3)
    off3 = B.round_up(B.resolve_unit(base[2]).end_offset, 64)
    u3 = B.simple_tb(off3, key=base[0].key, guest_bytes=base[0].guest_bytes)  # same tb_uid as 0
    off4 = B.round_up(B.resolve_unit(u3).end_offset, 64)
    k4 = replace(B.DEFAULT_KEY, page_addr0=B.DEFAULT_KEY.page_addr0 + 0x40)
    u4 = B.simple_tb(off4, key=k4, guest_bytes=b"\x55\xbf\x00\xbf")
    units = base + [u3, u4]
    cs = B.sc_header_fields(units)["code_size"]
    snap = os.path.join(directory, "snap.sc")
    staged = os.path.join(directory, "staged.sc")
    other = os.path.join(directory, "other.sc")
    B.build_sc(base, {"code_size": cs}, path=snap)
    B.build_sc(units, {"code_size": cs}, path=staged)
    k5 = replace(B.DEFAULT_KEY, page_addr0=B.DEFAULT_KEY.page_addr0 + 0x80)
    u5 = B.simple_tb(base[2].tb_offset, key=k5, guest_bytes=b"\x66\xbf\x00\xbf")
    B.build_sc([base[0], base[1], u5], {"code_size": cs}, path=other)
    rows = B.qprof_rows_for(units)
    rows.append({"tb_offset": TRANSIENT_OFF, "first_vaddr": 0x40000000,
                 "page_addr0": 0xFFFF_FFFF_FFFF_FFFF, "size": 2, "icount": 1})
    return World(directory, units, snap, staged, other, cs, rows)
