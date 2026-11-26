"""``return-edges@1``: exit admission, continuation candidates and U-COND (B3/B4 design §3.1). WP20.

For every unit and every exit that leaves through the main loop or the lookup
helper (``goto_ptr``, ``exit_null``) the analysis decides whether the exit is
**admissible** for a guarded slot (§3.1 rules 1-6) and gives stable reason
codes otherwise:

==========================  ===========================================================
rule                        reason code
==========================  ===========================================================
1 lookup tail (G29)         ``exit_not_goto_ptr``, ``tail_is_branch_target``
2 exit class (Thumb decode) ``indirect_multiway`` (incl. ``ldr pc,[rX]``/``ldm rX,{..pc}``)
3 mode / FastDyn PC write   ``handler_mode_return``, ``pc_modifier_exit``
4 unit                      ``special_cflags_unit``, ``no_meta``, ``ram_resident``,
                            ``unit_not_relocatable``
5 slot                      ``no_free_slot``
edge (profile)              ``target_not_profiled``
==========================  ===========================================================

Rule 6 (DISAS_EXIT instructions end with ``exit_tb(NULL,0)``) needs no row of
its own: such an exit has no lookup tail, so rule 1 refuses it; V13 (WP18)
asserts it for admitted units.

Classes: ``return`` (``bx lr``, ``pop {..,pc}``, ``ldm sp!,{..,pc}``,
``ldr pc,[sp],#k``; passes ``chain-returns``/``specialize-returns``),
``static`` (an off-page direct transfer: the capture's off-page StaticExit at
the exit's instruction; pass ``chain-static``), ``indirect_multiway``
(everything else; also when guest bytes are unavailable: fail closed).

**Continuation candidates** (per R unit and exit): the observed
``lookup_hit`` edges ``R -> C`` of the steady phase of an exact profile of the
**base** snapshot; refused with ``continuation_key_mismatch`` (C.flags,
C.cs_base apart from THUMB=1/CONDEXEC=0, C.cflags), ``two_page_target``,
``unit_not_relocatable``, ``ram_resident``, ``no_meta``, or
``continuation_not_in_snapshot`` (an external edge). Candidates are **ranked**
by the train weight aggregated per (exit-insn vaddr, continuation key) — all
suffix units of one return instruction pool their counts — but every
candidate row belongs to one R unit (variants are never shared, §3.1 [R4]).
The profile only selects and ranks.

**fact_not_local_to_return_tb** (§3.1, §3.2 rule 1): when one unit of a
return instruction defines ``field = v`` (an ``exit-facts@1`` fact) and
another unit of the same return instruction starts *after* the defining
instruction (a suffix TB: icount budget, shared epilogue entered later) and
does not store the field, the suffix gets that refusal for ``field``.

**U-COND** (T-B3-42, the g_cond static-only argument): at every return exit,
the IT state after the return instruction is empty. Checked from the Tier-0
``insn_start`` words (``words[1]`` = condexec at each insn start), replayed
against the guest decode from the TB key's CONDEXEC, and against HostOps (no
``condexec_bits`` store in the body). :func:`ucond_elf_scan` is the static
variant over a firmware ELF (the fixtures have no capture).
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from ..decode.guest_symbols import GuestImage, unit_guest_bytes
from ..decode.thumb_sem import GInsn, ItState, classify_last, decode_one, decode_unit
from ..formats.enums import ExitKind, OpMarkKind
from .api import AnalysisSpec, ScopeRef
from .exit_facts import UnitExitFacts, find_lookup_tails
from .hostops import UnitHostOps
from .patch_join import PatchPlan, insn_vaddrs

__all__ = [
    "ExitRow", "Candidate", "UCondRow", "NotLocal", "ReturnEdges", "build_return_edges",
    "continuation_key_ok", "ucond_unit", "ucond_elf_scan", "RETURN_EDGES", "ANALYSES",
    "EXIT_REASON_ORDER", "CF_COUNT_MASK", "CF_MEMI_ONLY", "CF_NOIRQ", "HANDLER_BIT",
    "THUMB_BIT", "CONDEXEC_SHIFT", "edge_stability",
]

CF_COUNT_MASK = 0x000001FF
CF_MEMI_ONLY = 0x00001000
CF_NOIRQ = 0x00010000
HANDLER_BIT = 1  # TBFLAG_M32 HANDLER (cs_base bit 0)
THUMB_BIT = 1 << 23  # TBFLAG_AM32 THUMB
CONDEXEC_SHIFT = 24  # TBFLAG_AM32 CONDEXEC [24, 32)
_NO_JMP = 0xFFFF

EXIT_REASON_ORDER = (
    "exit_not_goto_ptr", "tail_is_branch_target", "indirect_multiway", "handler_mode_return",
    "pc_modifier_exit", "special_cflags_unit", "no_meta", "ram_resident", "unit_not_relocatable",
    "no_free_slot", "target_not_profiled",
)


@dataclass(frozen=True, slots=True)
class Candidate:
    dst: str | None  # UnitId of C (None: outside the snapshot)
    vaddr: int | None  # Vc (Thumb bit clear)
    count: int
    status: str  # exact | lower_bound
    share: float  # of this exit's lookup_hit edges
    group_weight: int  # summed over every unit with the same exit-insn vaddr and C key
    rank: int  # 1-based, by group weight, then edge count
    reasons: tuple[str, ...]

    @property
    def admitted(self) -> bool:
        return not self.reasons

    def to_json(self) -> dict[str, Any]:
        return {"dst": self.dst, "vaddr": None if self.vaddr is None else f"0x{self.vaddr:x}",
                "count": self.count, "status": self.status, "share": round(self.share, 6),
                "group_weight": self.group_weight, "rank": self.rank,
                "admitted": self.admitted, "reasons": list(self.reasons)}


@dataclass(frozen=True, slots=True)
class ExitRow:
    uid: str
    exit_id: str
    exit_kind: str  # goto_ptr | exit_null
    insn: int | None
    host_off: int
    tail_off: int | None
    exit_class: str  # return | static | indirect_multiway | other
    class_detail: str
    exit_vaddr: int | None  # vaddr of the exit's guest instruction
    exit_text: str | None
    static_dest: int | None
    free_slots: tuple[int, ...]
    reasons: tuple[tuple[str, str], ...]  # (code, detail), EXIT_REASON_ORDER
    candidates: tuple[Candidate, ...] = ()

    @property
    def admitted(self) -> bool:
        return not self.reasons

    @property
    def reason_codes(self) -> tuple[str, ...]:
        return tuple(c for c, _ in self.reasons)

    def to_json(self) -> dict[str, Any]:
        return {"uid": self.uid, "exit_id": self.exit_id, "exit_kind": self.exit_kind,
                "insn": self.insn, "host_off": self.host_off, "tail_off": self.tail_off,
                "exit_class": self.exit_class, "class_detail": self.class_detail,
                "exit_vaddr": None if self.exit_vaddr is None else f"0x{self.exit_vaddr:x}",
                "exit_text": self.exit_text,
                "static_dest": None if self.static_dest is None else f"0x{self.static_dest:x}",
                "free_slots": list(self.free_slots), "admitted": self.admitted,
                "reasons": [{"code": c, "detail": d} for c, d in self.reasons],
                "candidates": [c.to_json() for c in self.candidates]}


@dataclass(frozen=True, slots=True)
class NotLocal:
    uid: str
    exit_id: str
    field: str
    value: int
    defined_in: str  # UnitId of the unit that defines it
    def_vaddr: int

    def to_json(self) -> dict[str, Any]:
        return {"uid": self.uid, "exit_id": self.exit_id, "field": self.field,
                "reason": "fact_not_local_to_return_tb", "value": f"0x{self.value:x}",
                "defined_in": self.defined_in, "def_vaddr": f"0x{self.def_vaddr:x}"}


@dataclass(frozen=True, slots=True)
class UCondRow:
    uid: str
    exit_id: str
    exit_kind: str
    ok: bool | None  # None: not checkable
    detail: str
    words1: tuple[int, ...]
    final_bits: int | None
    decode_replay: str  # match | mismatch | no_decode

    def to_json(self) -> dict[str, Any]:
        return {"uid": self.uid, "exit_id": self.exit_id, "exit_kind": self.exit_kind,
                "ok": self.ok, "detail": self.detail, "words1": list(self.words1),
                "final_bits": self.final_bits, "decode_replay": self.decode_replay}


@dataclass(frozen=True)
class ReturnEdges:
    snapshot_id: str
    profile_id: str | None
    profile_phase: str
    profile_coverage: float | None
    guest_image: str | None  # sha256 of the ELF used for decode
    plan: Mapping[str, Any]
    exits: tuple[ExitRow, ...]
    not_local: tuple[NotLocal, ...]
    ucond: tuple[UCondRow, ...]
    stats: Mapping[str, Any] = field(default_factory=dict)

    def exits_of(self, uid: str) -> tuple[ExitRow, ...]:
        return tuple(x for x in self.exits if x.uid == uid)

    def exit(self, uid: str, exit_id: str) -> ExitRow | None:
        return next((x for x in self.exits if x.uid == uid and x.exit_id == exit_id), None)

    def not_local_of(self, uid: str) -> tuple[NotLocal, ...]:
        return tuple(n for n in self.not_local if n.uid == uid)

    def return_units(self) -> list[ExitRow]:
        return [x for x in self.exits if x.exit_class == "return"]

    def to_json(self) -> dict[str, Any]:
        return {"name": "return-edges", "version": 1, "snapshot_id": self.snapshot_id,
                "profile_id": self.profile_id, "profile_phase": self.profile_phase,
                "profile_coverage": self.profile_coverage, "guest_image": self.guest_image,
                "plan": dict(self.plan), "stats": dict(self.stats),
                "exits": [x.to_json() for x in self.exits],
                "not_local": [n.to_json() for n in self.not_local],
                "ucond": [u.to_json() for u in self.ucond]}

    def content_digest(self) -> str:
        return "sha256:" + hashlib.sha256(json.dumps(self.to_json(), sort_keys=True)
                                          .encode()).hexdigest()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _record(u: Any):
    from ..formats.sc_records import ScRecord

    return ScRecord.from_bytes(u.record) if len(u.record) >= 104 else None


def free_slots(u: Any) -> tuple[int, ...]:
    rec = _record(u)
    if rec is None:
        return ()
    return tuple(n for n in (0, 1) if rec.jmp_insn_offset[n] == _NO_JMP)


def continuation_key_ok(r_key: Any, c_key: Any) -> bool:
    """§3.1: C.flags == R.flags; C.cs_base == R.cs_base except THUMB (C: 1) and
    CONDEXEC (C: 0); C.cflags == R.cflags."""
    keep = ~(THUMB_BIT | (0xff << CONDEXEC_SHIFT)) & 0xffffffffffffffff
    return (c_key.flags == r_key.flags and c_key.cflags == r_key.cflags and
            bool(c_key.cs_base & THUMB_BIT) and (c_key.cs_base >> CONDEXEC_SHIFT) & 0xff == 0
            and (c_key.cs_base & keep) == (r_key.cs_base & keep))


def _unit_reasons(u: Any, h: UnitHostOps | None) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    cf = u.key.cflags
    if cf & (CF_COUNT_MASK | CF_NOIRQ | CF_MEMI_ONLY):
        out.append(("special_cflags_unit", f"cflags 0x{cf:x}"))
    if u.meta_status != "ok":
        out.append(("no_meta", u.meta_status))
    if u.origin == "ram_resident":
        out.append(("ram_resident", ""))
    if not u.reloc.relocatable:
        out.append(("unit_not_relocatable", ",".join(u.reloc.reasons)[:200]))
    elif h is not None and not h.ok:
        out.append(("unit_not_relocatable", "hostops pinned: " + ",".join(h.reasons)[:200]))
    return out


def _cont_reasons(r: Any, c: Any) -> list[str]:
    out = []
    if not continuation_key_ok(r.key, c.key):
        out.append("continuation_key_mismatch")
    if c.key.page_addr1 is not None:
        out.append("two_page_target")
    if c.meta_status != "ok":
        out.append("no_meta")
    if c.origin == "ram_resident":
        out.append("ram_resident")
    if not c.reloc.relocatable:
        out.append("unit_not_relocatable")
    return out


def _order(reasons: Iterable[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
    seen: dict[str, str] = {}
    for c, d in reasons:
        seen.setdefault(c, d)
    return tuple(sorted(seen.items(), key=lambda cd: EXIT_REASON_ORDER.index(cd[0])
                        if cd[0] in EXIT_REASON_ORDER else len(EXIT_REASON_ORDER)))


# ---------------------------------------------------------------------------
# U-COND
# ---------------------------------------------------------------------------

class _NoIt:
    is_it = False
    it_cond_mask = None


def ucond_unit(u: Any, h: UnitHostOps | None, ginsns: Sequence[GInsn] | None, exit_id: str,
               exit_kind: str) -> UCondRow:
    rows = sorted(u.insns, key=lambda r: r.index)
    w1 = tuple(int(r.words[1]) for r in rows)
    if not rows:
        return UCondRow(u.uid, exit_id, exit_kind, None, "no insn rows", (), None, "no_decode")
    problems: list[str] = []
    st = ItState(w1[-1])
    st.step(ginsns[-1] if ginsns else _NoIt())  # type: ignore[arg-type]
    final = st.bits
    if final != 0:
        problems.append(f"IT state after the return insn is 0x{final:x}")
    replay = "no_decode"
    if ginsns is not None and len(ginsns) == len(rows):
        sim = ItState((u.key.cs_base >> CONDEXEC_SHIFT) & 0xff)
        replay = "match"
        for i, g in enumerate(ginsns):
            if sim.bits != w1[i]:
                replay = "mismatch"
                problems.append(f"insn {i}: replayed condexec 0x{sim.bits:x} != words[1] "
                                f"0x{w1[i]:x}")
                break
            sim.step(g)
    if h is not None and h.ok:
        for o in h.ops:
            if o.is_env_store and o.block != "prefix" and o.store_field == "condexec_bits":
                problems.append(f"condexec_bits store in the body @0x{o.off:x}")
    return UCondRow(u.uid, exit_id, exit_kind, not problems, "; ".join(problems), w1, final,
                    replay)


def ucond_elf_scan(image: GuestImage) -> dict[str, Any]:
    """Static U-COND over a firmware ELF: linear Thumb decode of every ``$t`` range,
    replaying the IT state; every return-class instruction must leave an empty IT
    state (it is outside an IT block or the last instruction of one)."""
    returns = ok = 0
    bad: list[dict[str, Any]] = []
    in_it = 0
    ranges = image.code_ranges()
    for lo, hi in ranges:
        code = image.read(lo, hi - lo)
        if code is None:
            continue
        off = 0
        st = ItState(0)
        while off + 2 <= len(code):
            g = decode_one(code[off:off + 4], lo + off)
            if g is None:
                # undecodable halfword (data in a $t range, or a 32-bit insn split by
                # the range end): restart the IT state and skip it
                hw = int.from_bytes(code[off:off + 2], "little")
                off += 4 if (hw >> 11) in (0b11101, 0b11110, 0b11111) else 2
                st = ItState(0)
                continue
            cls, _det = classify_last(g)
            pre = st.bits
            st.step(g)
            if cls == "return":
                returns += 1
                if pre & 0xf:
                    in_it += 1
                if st.mask:
                    bad.append({"vaddr": f"0x{g.vaddr:x}", "text": g.text,
                                "it_before": f"0x{pre:x}", "it_after": f"0x{st.bits:x}"})
                else:
                    ok += 1
            off += g.size
    return {"elf": image.path, "elf_sha256": image.sha256, "code_ranges": len(ranges),
            "return_insns": returns, "in_it_block": in_it, "ok": ok,
            "counterexamples": bad, "pass": not bad and returns > 0}


# ---------------------------------------------------------------------------
# builder
# ---------------------------------------------------------------------------

def _edges(profile_view: Any, phase: str) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    """``(src -> lookup_hit edges into snapshot units, src -> external lookup_hit edges)``."""
    inside: dict[str, list[dict]] = defaultdict(list)
    outside: dict[str, list[dict]] = defaultdict(list)
    if profile_view is None or getattr(profile_view, "profile", None) is None:
        return inside, outside
    for e in profile_view.edges(phase):
        if e["kind"] == "lookup_hit" and e["src"] is not None:
            inside[e["src"]].append(dict(e))
    pid, _ = profile_view._phase(phase)
    try:
        ext = profile_view.profile["x_external_edges"] or []
    except (KeyError, TypeError):
        ext = []
    tset = set(profile_view.target.unit_ids)
    for e in ext:
        if e.get("kind") == "lookup_hit" and e.get("phase") == pid and e.get("src") in tset:
            outside[e["src"]].append(dict(e))
    return inside, outside


def build_return_edges(units: Sequence[Any], *, snapshot_id: str,
                       hostops_of: Callable[[Any], UnitHostOps],
                       facts_of: Callable[[Any], UnitExitFacts] | None = None,
                       guest: GuestImage | None = None, plan: PatchPlan | None = None,
                       profile_view: Any = None, phase: str = "steady",
                       page_bits: int = 10) -> ReturnEdges:
    from ..model.view import unit_exits

    plan = plan if plan is not None else PatchPlan.unknown()
    by_uid = {u.uid: u for u in units}
    inside, outside = _edges(profile_view, phase)
    rows: list[ExitRow] = []
    decoded: dict[str, list[GInsn] | None] = {}
    hcache: dict[str, UnitHostOps] = {}
    ucond: list[UCondRow] = []
    stats: Counter[str] = Counter()
    pending: list[tuple[Any, ExitRow, list[dict], list[dict]]] = []
    for u in units:
        xs = [x for x in unit_exits(u) if x.kind in (ExitKind.GOTO_PTR, ExitKind.EXIT_NULL)]
        if not xs:
            continue
        h = hostops_of(u)
        hcache[u.uid] = h
        gi = decode_unit(u, unit_guest_bytes(u, guest), page_bits)
        decoded[u.uid] = gi
        vaddrs = insn_vaddrs(u, page_bits)
        tails = {h.ops[gidx].off: (t, r) for gidx, t, r in find_lookup_tails(h)} if h.ok else {}
        ureasons = _unit_reasons(u, h)
        fs = free_slots(u)
        n_goto = sum(1 for x in xs if x.kind == ExitKind.GOTO_PTR)
        r_pc_mods: list[str] = []
        if vaddrs is not None and plan.modifiers_known:
            vs = set(vaddrs)
            r_pc_mods = [e.text for e in plan.entries
                         if e.kind == "modifier" and e.form == "pc" and e.vaddr in vs]
        for x in xs:
            reasons: list[tuple[str, str]] = []
            last = gi[x.insn] if gi is not None and x.insn is not None and x.insn < len(gi) \
                else None
            exit_vaddr = vaddrs[x.insn] if vaddrs and x.insn is not None and \
                x.insn < len(vaddrs) else None
            if last is not None:
                cls, det = classify_last(last)
            else:
                cls, det = "unknown", "guest_bytes_unavailable"
            tail_off = None
            if x.kind == ExitKind.GOTO_PTR:
                # the GOTO_PTR OpMark offset is the ``jmp rax`` (unit_exits sorts by it)
                host_off = next((m.host_off for m in u.opmarks
                                 if m.kind == OpMarkKind.GOTO_PTR and m.insn_idx == x.insn), -1)
                t = tails.get(host_off)
                if t is None:
                    reasons.append(("exit_not_goto_ptr", "no lookup tail" if h.ok else
                                    "hostops pinned"))
                else:
                    tail, tr = t
                    tail_off = None if tail is None else tail.off
                    if tr is not None:
                        reasons.append((tr, ""))
                if x.static_dest_vaddr is not None:
                    page = (u.vaddrs[0] >> page_bits) if u.vaddrs else None
                    if cls in ("static", "fallthrough", "unknown") and page is not None and \
                            (x.static_dest_vaddr >> page_bits) != page:
                        cls, det = "static", (det if cls != "unknown" else "capture_static_exit")
                    else:
                        reasons.append(("indirect_multiway",
                                        f"class {cls}/{det} with a static off-page exit"))
                        cls = "indirect_multiway"
                elif cls in ("static", "fallthrough"):
                    reasons.append(("indirect_multiway", f"{det} via goto_ptr without a "
                                    "captured static exit"))
                    cls = "indirect_multiway"
                elif cls == "unknown":
                    reasons.append(("indirect_multiway", "guest_bytes_unavailable"))
                    cls = "indirect_multiway"
                elif cls == "indirect_multiway":
                    reasons.append(("indirect_multiway", det))
            else:
                host_off = next((m.host_off for m in u.opmarks
                                 if m.kind == OpMarkKind.EXIT_TB and m.insn_idx == x.insn), -1)
                reasons.append(("exit_not_goto_ptr", "exit_tb(NULL)"))
                if cls in ("static", "fallthrough"):
                    cls = "other"
                elif cls == "unknown":
                    cls = "other"
            if u.key.cs_base & HANDLER_BIT:
                reasons.append(("handler_mode_return", "cs_base HANDLER"))
            if r_pc_mods:
                reasons.append(("pc_modifier_exit", r_pc_mods[0]))
            elif not plan.modifiers_known:
                reasons.append(("pc_modifier_exit", "patch_plan_unavailable"))
            reasons += ureasons
            if not fs:
                reasons.append(("no_free_slot", ""))
            ins = inside.get(u.uid, []) if x.kind == ExitKind.GOTO_PTR else []
            outs = outside.get(u.uid, []) if x.kind == ExitKind.GOTO_PTR else []
            if x.kind == ExitKind.GOTO_PTR:
                if profile_view is None or getattr(profile_view, "profile", None) is None:
                    reasons.append(("target_not_profiled", "no profile"))
                elif not ins and not outs:
                    reasons.append(("target_not_profiled", f"no {phase} lookup_hit edge"))
                elif n_goto > 1:
                    reasons.append(("target_not_profiled",
                                    f"{n_goto} goto_ptr exits: edges are not attributable"))
            row = ExitRow(u.uid, x.id, "goto_ptr" if x.kind == ExitKind.GOTO_PTR else "exit_null",
                          x.insn, host_off, tail_off, cls, det, exit_vaddr,
                          None if last is None else last.text, x.static_dest_vaddr, fs,
                          _order(reasons))
            pending.append((u, row, ins, outs))
            if cls == "return":
                ucond.append(ucond_unit(u, h, gi, x.id, row.exit_kind))
    # group weights per (exit-insn vaddr, continuation key)
    gw: Counter[tuple] = Counter()
    for u, row, ins, _outs in pending:
        for e in ins:
            c = by_uid.get(e["dst"])
            if c is None or row.exit_vaddr is None:
                continue
            gw[(row.exit_vaddr, c.vaddrs[0] if c.vaddrs else None, c.key.flags, c.key.cs_base,
                c.key.cflags)] += int(e["count"])
    for u, row, ins, outs in pending:
        cands: list[tuple[tuple, Candidate]] = []
        tot = sum(int(e["count"]) for e in ins) + sum(int(e["count"]) for e in outs)
        for e in ins:
            c = by_uid[e["dst"]]
            vc = c.vaddrs[0] if c.vaddrs else None
            w = gw.get((row.exit_vaddr, vc, c.key.flags, c.key.cs_base, c.key.cflags),
                       int(e["count"]))
            cands.append(((-w, -int(e["count"]), c.uid),
                          Candidate(c.uid, vc, int(e["count"]), e.get("status", "exact"),
                                    int(e["count"]) / tot if tot else 0.0, w, 0,
                                    tuple(_cont_reasons(u, c)))))
        for e in outs:
            cands.append(((0, -int(e["count"]), str(e.get("dst"))),
                          Candidate(None, None, int(e["count"]), e.get("status", "exact"),
                                    int(e["count"]) / tot if tot else 0.0, 0, 0,
                                    ("continuation_not_in_snapshot",))))
        cands.sort(key=lambda kc: kc[0])
        ranked = tuple(Candidate(c.dst, c.vaddr, c.count, c.status, c.share, c.group_weight,
                                 i + 1, c.reasons) for i, (_, c) in enumerate(cands))
        row = replace(row, candidates=ranked)
        rows.append(row)
        stats[f"exits:{row.exit_kind}"] += 1
        stats[f"class:{row.exit_class}"] += 1
        if row.admitted:
            stats[f"admitted:{row.exit_class}"] += 1
        for c, _ in row.reasons:
            stats[f"reason:{c}"] += 1
    not_local = _not_local(rows, by_uid, facts_of, page_bits)
    stats["not_local"] = len(not_local)
    stats["ucond_units"] = len(ucond)
    stats["ucond_counterexamples"] = sum(1 for r in ucond if r.ok is False)
    stats["ucond_unchecked"] = sum(1 for r in ucond if r.ok is None)
    stats["guest_decoded_units"] = sum(1 for v in decoded.values() if v is not None)
    stats["guest_undecoded_units"] = sum(1 for v in decoded.values() if v is None)
    return ReturnEdges(
        snapshot_id, getattr(profile_view, "profile_id", None), phase,
        getattr(profile_view, "coverage", None), None if guest is None else guest.sha256,
        {k: v for k, v in plan.to_json().items() if k != "entries"} |
        {"n_entries": len(plan.entries)},
        tuple(rows), tuple(not_local), tuple(ucond), dict(sorted(stats.items())))


def _not_local(rows: Sequence[ExitRow], by_uid: Mapping[str, Any],
               facts_of: Callable[[Any], UnitExitFacts] | None, page_bits: int
               ) -> list[NotLocal]:
    if facts_of is None:
        return []
    groups: dict[int, list[ExitRow]] = defaultdict(list)
    for r in rows:
        if r.exit_class == "return" and r.exit_kind == "goto_ptr" and r.exit_vaddr is not None:
            groups[r.exit_vaddr].append(r)
    out: list[NotLocal] = []
    for _v, rs in sorted(groups.items()):
        if len(rs) < 2:
            continue
        defined: list[tuple[str, int, str, int]] = []  # (field, value, uid, def vaddr)
        stored: dict[str, set[str]] = {}
        exits_facts: dict[str, Any] = {}
        for r in rs:
            u = by_uid[r.uid]
            uf = facts_of(u)
            stored[r.uid] = set(uf.stored_fields)
            vaddrs = insn_vaddrs(u, page_bits)
            es = next((e for e in uf.exits if e.tail.off == r.tail_off), None)
            exits_facts[r.uid] = es
            if es is None or vaddrs is None:
                continue
            for f in es.facts:
                di = f.def_insn
                if di is not None and di < len(vaddrs):
                    defined.append((f.field, f.value, r.uid, vaddrs[di]))
        for r in rs:
            u = by_uid[r.uid]
            start = u.vaddrs[0] if u.vaddrs else None
            if start is None:
                continue
            seen: set[str] = set()
            for fld, val, duid, dv in defined:
                if duid == r.uid or fld in seen or fld in stored[r.uid]:
                    continue
                if dv < start:  # the defining instruction precedes this unit's start
                    out.append(NotLocal(r.uid, r.exit_id, fld, val, duid, dv))
                    seen.add(fld)
    return out


# ---------------------------------------------------------------------------
# U-HELD: held-out stability of the ranked edges (reported outcome)
# ---------------------------------------------------------------------------

def edge_stability(train: ReturnEdges, other: ReturnEdges, *, k: int = 2) -> dict[str, Any]:
    """How well the train ranking predicts another profile's (held-out) lookup traffic.

    Per exit class, over the *admitted* exits of ``train``: the other profile's
    lookup_hit traffic on those exits (``traffic``), the share of it that goes to
    one of train's top-``k`` admitted candidates (``covered_topk``, what ``k``
    guarded slots would catch), the share on edges train observed at all
    (``seen_in_train``), and the traffic-weighted rate at which both rankings
    agree on the top admitted candidate (``top1_agree``). Unknown is not zero:
    an exit the other profile never takes contributes no traffic.
    """
    out: dict[str, Any] = {"k": k, "train_profile": train.profile_id,
                           "other_profile": other.profile_id, "classes": {}}
    for cls in ("return", "static"):
        tot = cov = seen = agree = 0
        exits = 0
        for x in train.exits:
            if x.exit_class != cls or not x.admitted:
                continue
            y = other.exit(x.uid, x.exit_id)
            if y is None:
                continue
            tr = [c for c in x.candidates if c.admitted]
            top = {c.dst for c in tr[:k]}
            allc = {c.dst for c in x.candidates}
            ot = [c for c in y.candidates]
            t = sum(c.count for c in ot)
            if t == 0:
                continue
            exits += 1
            tot += t
            cov += sum(c.count for c in ot if c.dst in top)
            seen += sum(c.count for c in ot if c.dst in allc)
            oadm = [c for c in ot if c.admitted]
            if tr and oadm and tr[0].dst == max(oadm, key=lambda c: c.count).dst:
                agree += t
        out["classes"][cls] = {
            "exits_with_traffic": exits, "traffic": tot,
            "covered_topk": None if not tot else round(cov / tot, 6),
            "seen_in_train": None if not tot else round(seen / tot, 6),
            "top1_agree": None if not tot else round(agree / tot, 6)}
    return out


# ---------------------------------------------------------------------------
# analysis
# ---------------------------------------------------------------------------

def _compute(ctx: Any, scope: ScopeRef) -> ReturnEdges:
    from .b3_inputs import guest_image_for, patch_plan_for

    bundle = ctx.bundle
    units = ctx.units()

    def hops(u):
        return ctx.analysis("hostops", ScopeRef("unit", u.uid)).value

    def facts(u):
        return ctx.analysis("exit-facts", ScopeRef("unit", u.uid)).value

    pv = ctx.profile("steady")
    return build_return_edges(units, snapshot_id=ctx.snapshot_id, hostops_of=hops,
                              facts_of=facts, guest=guest_image_for(ctx, bundle),
                              plan=patch_plan_for(ctx, bundle), profile_view=pv,
                              page_bits=ctx.header.target_page_bits)


RETURN_EDGES = AnalysisSpec(
    name="return-edges", version=1, scope="snapshot",
    requires=(("hostops", 1), ("exit-facts", 1)),
    inputs=frozenset({"code", "meta", "bundle", "profile:steady"}),
    compute=_compute, result_schema=ReturnEdges)
ANALYSES = (RETURN_EDGES,)
