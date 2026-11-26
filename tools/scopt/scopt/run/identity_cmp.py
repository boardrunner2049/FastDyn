"""T-B1-1 evidence helpers (§2.6): the weaker per-key comparison, RUN_INFO and MAPS.

When the capture-off control runs are not byte-deterministic (guest timing),
the whole-file sha256 gate cannot be used. §2.6 then requires, for every unit
common to a capture-on and a capture-off run:

* the records are identical modulo offsets (every field except ``tb_offset``,
  ``end_offset``, ``tc_offset`` and ``checksum``) and the bodies have the same
  length;
* the ``[tc, end)`` bytes are identical after normalizing **only**
  self-relative fields. A 4-byte rel32/disp32 field (site list from the on
  run's ``.scm``) is masked when its target, computed on both sides, is the
  same *relative* location: inside the unit itself (intra branches,
  own-struct LEAs, ``MOVI_EXIT_TB_PTR``/``MOVI_LDST_RA`` in RIP form) or the
  same offset into the prologue (``exit_tb`` jumps to ``tb_ret_addr`` and the
  epilogue). An 8-byte ``MOVI_EXIT_TB_PTR``/``MOVI_LDST_RA`` immediate is
  masked when it points to the same offset from the unit's code start;
* every absolute host value must be equal: an external rel32/RIP target is
  compared as an absolute address, any other in-arena target by its arena
  offset, and pool data and every unmasked byte are compared verbatim.

Units are matched by UnitId (``tb_uid#k``; the key, guest_hash, size and icount
plus the occurrence index), never by slot or vaddr. The same-binary rule
applies: different QEMU binaries are never compared (helper addresses move).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any

from ..formats.enums import Encoding, SiteKind
from ..formats.sc_records import ScFile, ScRecord

__all__ = ["UnitImage", "unit_images", "per_key_compare", "maps_layout", "scm_checks"]

_SELF_IMM_KINDS = (int(SiteKind.MOVI_EXIT_TB_PTR), int(SiteKind.MOVI_LDST_RA))
_REL_ENCS = (int(Encoding.REL32), int(Encoding.RIPREL32), int(Encoding.GOTO_TB_REL32))
# record fields that move with the placement; everything else must be equal
_OFFSET_FIELDS = ("tb_offset", "end_offset", "tc_offset", "checksum")
_REC_FIELDS = tuple(f for f in ScRecord.__dataclass_fields__ if f not in _OFFSET_FIELDS)


@dataclass(frozen=True)
class UnitImage:
    """One valid slot as placed in a run: record, ``[tc,end)`` bytes, arena base."""

    uid: str
    slot: int
    record: ScRecord
    record_bytes: bytes
    body: bytes
    code_address: int
    code_size: int
    prologue_end: int

    @property
    def tc_abs(self) -> int:
        return self.code_address + self.record.tc_offset


def unit_images(sc: ScFile) -> dict[str, UnitImage]:
    """UnitId -> :class:`UnitImage` for every valid slot of ``sc``."""
    h = sc.header
    return {uid: UnitImage(uid, u.slot, u.record, u.record_bytes, u.body, h.code_address,
                           h.code_size, h.prologue_end)
            for uid, u in zip(sc.unit_ids(), sc.units())}


def _mask_sites(a: UnitImage, b: UnitImage, sites: Any) -> tuple[bytearray, bytearray, list]:
    ma, mb = bytearray(a.body), bytearray(b.body)
    bad: list[dict[str, Any]] = []
    tc_size = a.record.tc_size
    end_rel = a.record.end_offset - a.record.tc_offset  # body length
    tb_rel = a.record.tb_offset - a.record.tc_offset    # struct start (negative)
    for s in sites:
        fo, w, enc, kind = s.field_off, s.width, s.enc, s.kind
        if fo + w > tc_size:
            bad.append({"field_off": fo, "why": "site outside the code"})
            continue
        if w == 4 and enc in _REL_ENCS:
            da = struct.unpack_from("<i", a.body, fo)[0]
            db = struct.unpack_from("<i", b.body, fo)[0]
            # target relative to each side's code start (same formula on both sides)
            ra, rb = fo + 4 + da, fo + 4 + db
            ta, tb = a.tc_abs + ra, b.tc_abs + rb
            if tb_rel <= ra < end_rel:           # inside the unit (struct or body)
                eq = ra == rb
            elif 0 <= ta - a.code_address < a.prologue_end:   # into the prologue
                eq = (ta - a.code_address) == (tb - b.code_address)
            elif 0 <= ta - a.code_address < a.code_size:
                # another place in the arena: no self-relative excuse; same offset
                eq = (ta - a.code_address) == (tb - b.code_address)
            else:                                # absolute host value outside the arena
                eq = ta == tb
            if eq:
                ma[fo:fo + 4] = mb[fo:fo + 4] = b"\0" * 4
            else:
                bad.append({"field_off": fo, "kind": SiteKind(kind).name,
                            "target_a": hex(ta), "target_b": hex(tb)})
        elif w == 8 and enc == int(Encoding.IMM64) and kind in _SELF_IMM_KINDS:
            va = struct.unpack_from("<Q", a.body, fo)[0]
            vb = struct.unpack_from("<Q", b.body, fo)[0]
            if va - a.tc_abs == vb - b.tc_abs:
                ma[fo:fo + 8] = mb[fo:fo + 8] = b"\0" * 8
            else:
                bad.append({"field_off": fo, "kind": SiteKind(kind).name,
                            "value_a": hex(va), "value_b": hex(vb)})
        # every other site (pool data, IMM32 forms, XOR0, NONE) is compared verbatim
    return ma, mb, bad


def per_key_compare(on: dict[str, UnitImage], off: dict[str, UnitImage],
                    on_tbs: Any, *, max_examples: int = 10) -> dict[str, Any]:
    """§2.6 weaker mode for one (on, off) pair.

    ``on_tbs`` maps a slot of the on run to its ``.scm`` TB chunk (only chunks
    whose embedded record equals the slot's record are used). Returns counts
    and the first mismatches; ``ok`` is true when at least one unit is common
    and none mismatches.
    """
    common = mism = unbound = masked_units = 0
    examples: list[dict[str, Any]] = []
    for uid, a in on.items():
        b = off.get(uid)
        if b is None:
            continue
        common += 1
        why: list[Any] = []
        rec_diff = [f for f in _REC_FIELDS if getattr(a.record, f) != getattr(b.record, f)]
        if rec_diff:
            why.append({"record_fields": rec_diff})
        if len(a.body) != len(b.body):
            why.append({"body_len": [len(a.body), len(b.body)]})
        if not why and a.body != b.body:
            tb = on_tbs.get(a.slot)
            if tb is None or tb.record != a.record_bytes:
                unbound += 1
                why.append({"no_bound_capture": a.slot})
            else:
                masked_units += 1
                ma, mb, bad = _mask_sites(a, b, tb.sites)
                if bad:
                    why.append({"sites": bad[:4]})
                if ma != mb:
                    first = next(i for i in range(len(ma)) if ma[i] != mb[i])
                    why.append({"first_unexplained_byte": first})
        if why:
            mism += 1
            if len(examples) < max_examples:
                examples.append({"uid": uid, "on_slot": a.slot, "off_slot": b.slot,
                                 "why": why})
    return {"common_units": common, "on_units": len(on), "off_units": len(off),
            "normalized_units": masked_units, "mismatches": mism,
            "unbound": unbound, "examples": examples, "ok": common > 0 and mism == 0}


def maps_layout(raw: bytes) -> tuple[str, ...]:
    """``/proc/self/maps`` lines without the dev and inode columns.

    Addresses, permissions, offsets and paths are the layout; the inode of an
    anonymous memfd is assigned by the kernel per run and is not."""
    out = []
    for line in raw.decode("latin-1").splitlines():
        f = line.split(None, 5)
        if len(f) >= 5:
            out.append(" ".join(f[:3] + f[5:]))
    return tuple(out)


def scm_checks(scm: Any, sc: ScFile) -> dict[str, Any]:
    """Per capture-on run: RUN_INFO vs the run's ``.sc`` header, binding, MAPS.

    Returns ``{"failures": [...], ...}``; the MAPS(first_commit) layout is
    returned for the across-run comparison.
    """
    out: dict[str, Any] = {"failures": []}
    ri = scm.run_info
    if ri is None:
        out["failures"].append("no RUN_INFO chunk")
    else:
        h = sc.header
        out["run_info_code_address"] = hex(ri.code_address)
        for f in ("code_address", "code_size", "prologue_end", "runtime_signature"):
            if getattr(ri, f) != getattr(h, f):
                out["failures"].append(f"RUN_INFO {f} {getattr(ri, f):#x} != .sc header "
                                       f"{getattr(h, f):#x}")
    bound = sum(1 for slot, tb in scm.tbs.items()
                if slot < len(sc.record_bytes) and tb.record == sc.record_bytes[slot])
    out["tb_chunks"] = len(scm.tbs)
    out["tb_chunks_bound"] = bound
    out["valid_slots"] = len(sc.valid_slots)
    if not scm.tbs:
        out["failures"].append("the capture holds no TB chunk")
    m = scm.maps_by_tag(1)
    if m is None:
        out["failures"].append("no MAPS(first_commit) chunk")
        out["maps_layout"] = None
    else:
        lay = maps_layout(m.data)
        out["maps_layout"] = lay
        out["maps_truncated"] = bool(m.truncated)
        if any(line.rstrip().endswith(".scm") for line in lay):
            out["failures"].append("the .scm file is mapped into QEMU")
    out["incomplete"] = scm.incomplete
    out["rejected_chunks"] = len(scm.rejected_chunks)
    return out
