"""§9.3 guard/link mutants built with WP18's emitter and placement (WP19 integration).

The unit stage ran the guard mutants on WP19's own reference emitter
(:mod:`.mutants`, ``needs_emitter: true``). Here every mutant starts from a
real :class:`~scopt.link.LinkResultV2`: WP18's ``grow_unit`` emitted the
§4.1 stub, ``NativeLinkerV2`` placed the grown unit and the variants and wrote
the ext table. A mutant changes the **linked artifact** (a unit's placed
bytes, or the ext table's links) and is checked with
:func:`~.link_check.certify_link`, the same call that produces
``validate_b3``'s V12/V13/V14 inputs. It is rejected when that call reports a
failing verdict (``LinkCheck.rejected``); the result records whether the
rejection is a *mismatch* (a failed code), as the design requires, or only an
``inconclusive``.

Mutant classes (plan WP19 "Acceptance (integration, WP18)", design §9.3):

* ``drop_<atom>`` — one guard compare of a placed stub replaced by nops
  (``g_pc``: the compare nopped and its ``je L_k`` turned into ``jmp L_k``,
  one mutant per target);
* ``guard_before_effect`` — R's tail jump ``jmp S; int3 x6`` hoisted above
  one of R's effect stores (``regs[15]``, ``thumb``) that precede the tail;
  the effect bytes move behind it (same length, now dead), one mutant per
  effect store;
* ``guard_stub_store`` — the g_bp compare replaced by an env store (``mov
  dword [rbp+regs[1]], imm32`` + nops): a store inside the stub;
* ``falsified_fact_link`` — a SPEC link to a variant whose fact claims a value
  R does not establish (certified value + 1, or any value where R's field is
  not constant);
* ``shared_variant_suffix`` — one variant (facts certified at a full return
  unit's slot) also linked from the slot of its suffix unit (a TB that starts
  inside the full unit and ends at the same return), by adding a second
  LinkEntry to the same variant index; and the plan form, a second copy of
  the same variant for the suffix built by NativeLinker v2 itself.

Positive controls: every unmutated grown unit and every correct link must be
accepted (otherwise rejection would be vacuous).

**Equivalent mutants.** When the unmutated control already has a guarded slot
that no path reaches (``check_guarded_chain`` data ``slots_unreached``: an atom
is concretely false at the exit, e.g. a TB that stores ``condexec_bits := 8``
before a static exit, so g_cond never passes), dropping another atom cannot
change any execution. Mutants of such a unit are marked ``equivalent`` and kept
out of the "100 % rejected" denominator; they are listed, never hidden.
"""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from .asm import nop
from .link_check import LinkCheck, certify_link, fact_field, r_prime
from .program import world_of

__all__ = ["stub_atoms", "patch_unit", "drop_atom_patches", "effect_hoists", "stub_store_patch",
           "add_link", "growable", "general_plan", "LinkMutant", "LinkSuite",
           "guard_mutants", "fact_link_mutants", "shared_variant_mutants", "suffix_pairs",
           "synthetic_fact", "STORE_DISP"]

#: env displacement the stub-store mutant writes (regs[1])
STORE_DISP = 4


# ---------------------------------------------------------------------------
# byte-level helpers on a placed stub
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StubAtom:
    atom: str
    k: int  # target index for g_pc, else -1
    start: int  # first byte of the compare (relative to tc)
    cmp_len: int
    jcc_off: int  # the 0f 8x of its branch


def stub_atoms(code: bytes, off: int) -> list[StubAtom]:
    """The compares of the §4.1 stub at ``off`` (template ``guard-v1``), in order."""
    b = bytes(code)
    out = []
    p = off

    def take(atom: str, prefix: bytes, n: int, k: int = -1) -> None:
        nonlocal p
        if b[p:p + len(prefix)] != prefix:
            raise ValueError(f"{atom}: {b[p:p + 8].hex()} @+{p:#x} is not the template")
        if b[p + n:p + n + 1] != b"\x0f":
            raise ValueError(f"{atom}: no jcc after the compare @+{p + n:#x}")
        out.append(StubAtom(atom, k, p, n, p + n))
        p += n + 6

    take("g_bp", b"\x48\x83\xbd", 8)
    take("g_thumb", b"\x80\xbd", 7)
    take("g_cond", b"\x83\xbd", 7)
    take("g_flags", b"\x81\xbd", 10)
    if b[p:p + 3] == b"\x48\x81\xbd":
        take("g_flags2", b"\x48\x81\xbd", 11)
    else:
        take("g_flags2", b"\x48\xb8", 17)
    if b[p:p + 2] == b"\x83\xbd":
        take("g_r0_zero", b"\x83\xbd", 7)
    k = 0
    while b[p:p + 2] == b"\x81\xbd":
        take("g_pc", b"\x81\xbd", 10, k)
        k += 1
    return out


def drop_atom_patches(code: bytes, a: StubAtom) -> list[tuple[int, bytes]]:
    """Same-length patches that remove compare ``a`` from the guard."""
    if a.atom != "g_pc":
        return [(a.start, nop(a.cmp_len + 6))]
    rel = struct.unpack_from("<i", code, a.jcc_off + 2)[0]
    tgt = a.jcc_off + 6 + rel
    jmp = b"\xe9" + struct.pack("<i", tgt - (a.jcc_off + 5)) + b"\x90"
    return [(a.start, nop(a.cmp_len)), (a.jcc_off, jmp)]


def stub_store_patch(a_bp: StubAtom, value: int = 0x55) -> list[tuple[int, bytes]]:
    """``mov dword [rbp+STORE_DISP], value`` + nops in place of the g_bp compare."""
    st = b"\xc7\x85" + struct.pack("<i", STORE_DISP) + struct.pack("<I", value)
    return [(a_bp.start, st + nop(a_bp.cmp_len + 6 - len(st)))]


def effect_hoists(u: Any, tail_off: int, stub_off: int, offsets: Any,
                  code_address: int) -> list[tuple[str, list[tuple[int, bytes]]]]:
    """One ``guard_before_effect`` patch set per effect store of ``u`` before its tail.

    The straight-line run of host instructions that ends at the tail ``T`` is
    scanned backwards; each ``mov [rbp+pc|thumb], …`` in it at ``E`` gives
    ``[E, T+11) := jmp S ; int3 x6 ; bytes[E, T)``. A run containing a
    branch, call, RIP-relative operand, branch target or fixup field is not
    hoisted over (the moved bytes would change meaning)."""
    import capstone
    from capstone import x86 as X

    from ..formats.enums import SiteKind

    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    md.detail = True
    code = bytes(u.code)
    end = u.pool_start if u.pool_start is not None else len(code)
    insns = [i for i in md.disasm(code[:end], 0)]
    starts = {i.address: i for i in insns}
    if tail_off not in starts:
        return []
    tc_abs = code_address + u.tc_offset
    lab = (SiteKind.LABEL_JMP, SiteKind.LABEL_JCC, SiteKind.LDST_FAST_JCC,
           SiteKind.LDST_RET_JMP, SiteKind.MOVI_LDST_RA)
    targets = {s.value - tc_abs for s in u.sites if s.kind in lab}
    fix = [(f.field_off, f.field_off + f.width) for f in u.reloc.fixups]
    run = []
    idx = [i.address for i in insns].index(tail_off)
    for i in reversed(insns[:idx]):
        bad = i.group(capstone.CS_GRP_JUMP) or i.group(capstone.CS_GRP_CALL) or \
            i.group(capstone.CS_GRP_RET) or \
            any(o.type == X.X86_OP_MEM and o.mem.base == X.X86_REG_RIP for o in i.operands)
        if bad:
            break
        run.append(i)
        if i.address in targets:
            break
    out = []
    want = {offsets.pc: "pc", offsets.thumb: "thumb"}
    for i in run:  # nearest to the tail first
        if i.mnemonic != "mov" or len(i.operands) != 2:
            continue
        d = i.operands[0]
        if d.type != X.X86_OP_MEM or d.mem.base != X.X86_REG_RBP or d.mem.index != 0 or \
                d.mem.disp not in want:
            continue
        e = i.address
        if any(e < t < tail_off + 11 for t in targets) or \
                any(a < tail_off + 11 and e < b for a, b in fix):
            continue
        moved = code[e:tail_off]
        new = b"\xe9" + struct.pack("<i", stub_off - (e + 5)) + b"\xcc" * 6 + moved
        out.append((f"before_{want[d.mem.disp]}_store@{e:#x}", [(e, new)]))
    return out


def patch_unit(link: Any, uid: str, patches: Sequence[tuple[int, bytes]]) -> Any:
    """``link`` with same-length patches on the placed bytes of unit ``uid``."""
    units = []
    for lu in link.units:
        if lu.uid == uid:
            body = bytearray(lu.body)
            for off, b in patches:
                if off < 0 or off + len(b) > len(body):
                    raise ValueError(f"patch [{off:#x},+{len(b)}) outside {uid}")
                body[off:off + len(b)] = b
            lu = replace(lu, body=bytes(body))
        units.append(lu)
    return replace(link, units=tuple(units))


def add_link(link: Any, slot_index: int, variant_index: int) -> Any:
    """``link`` with one more ext LinkEntry ``slot -> variant`` (the variant shared)."""
    from ..formats.sc_semantic import LinkEntry

    ext = link.ext
    links = tuple(lk for lk in ext.links if lk.slot_index != slot_index) + \
        (LinkEntry(slot_index, variant_index),)
    return replace(link, ext=replace(ext, links=links))


# ---------------------------------------------------------------------------
# plans
# ---------------------------------------------------------------------------

def growable(snapshot: Any, guest_bytes: Mapping[str, bytes],
             classes: Iterable[str] = ("return", "static")) -> list[tuple[Any, Any, str]]:
    """``(unit, tail, exit class)`` for the first admissible tail of every relocatable
    unit with a free slot and an exit class in ``classes`` (§3.1 rules 1-2)."""
    from ..link import exit_class_of, free_slots, lookup_tails, tail_problem

    out = []
    ca = snapshot.header.code_address
    for u in snapshot.units():
        if not u.reloc.relocatable or u.meta_status != "ok" or not u.vaddrs:
            continue
        if not free_slots(u):
            continue
        for t in lookup_tails(u):
            if tail_problem(u, t, ca):
                continue
            xc = exit_class_of(u, t, guest_bytes.get(u.uid))
            if xc in classes:
                out.append((u, t, xc))
                break
    return out


def _admissible_key(u: Any) -> tuple[int, int]:
    from ..guard import CS_CONDEXEC_MASK, CS_M_FP_BITS, CS_THUMB, PSTATE_SS, SS_ACTIVE

    return (u.key.flags & ~(PSTATE_SS | SS_ACTIVE),
            (u.key.cs_base & ~(CS_CONDEXEC_MASK | CS_M_FP_BITS)) | CS_THUMB)


def general_plan(snapshot: Any, items: Sequence[tuple[Any, Any, str]], *,
                 kf2_high: bool = False) -> Any:
    """Registered GENERAL guards on every item: target 0 = the unit's own first vaddr;
    a second target (another unit's vaddr) when both slots are free. ``kf2_high``
    sets cs_base bit 40 in the key so the stub uses the ``Kf2 >= 2^31`` form."""
    from ..guard import GuardTarget
    from ..link import B3Plan, GuardedExitSpec, free_slots

    vas = sorted({u.vaddrs[0] & ~1 for u, _, _ in items})
    grow = {}
    for u, t, xc in items:
        free = free_slots(u)
        v0 = u.vaddrs[0] & ~1
        tg = [GuardTarget(v0, free[0], "GENERAL")]
        if len(free) == 2:
            other = next((v for v in vas if v != v0), v0 + 2)
            tg.append(GuardTarget(other, free[1], "GENERAL"))
        kf, kf2 = _admissible_key(u)
        if kf2_high:
            kf2 |= 1 << 40
        grow[u.uid] = (GuardedExitSpec(t.off, xc, tuple(tg), kf, kf2),)
    return B3Plan(grow=grow)


def synthetic_fact(field_name: str, value: int, tag: str) -> Any:
    """A typed :class:`~scopt.passes.base.FactRef` for a mutation test (its ids are
    hashes of ``tag``; no CalleeSummary row exists for it)."""
    from ..passes.base import FactRef

    def h(x: str) -> str:
        return "sha256:" + hashlib.sha256(x.encode()).hexdigest()

    return FactRef(h(f"fact/{tag}/{field_name}/{value}"), field_name, value & 0xFFFFFFFF, 0,
                   "patch", h(f"cert/{tag}"), h(f"row/{tag}"))


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------

@dataclass
class LinkMutant:
    name: str
    unit: str
    rejected: bool
    mismatch: bool  # rejected with a failed code (not only inconclusive)
    reasons: list[str]
    detail: str
    structural: str | None = None  # WP18 decode_guard verdict on the mutated stub (info)
    source: str = ""
    equivalent: str | None = None  # why this is an equivalent mutant (control vacuous)

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "unit": self.unit, "rejected": self.rejected,
                "mismatch": self.mismatch, "reasons": self.reasons,
                "detail": self.detail[:300], "structural_v13": self.structural,
                "source": self.source, "equivalent_mutant": self.equivalent}


@dataclass
class LinkSuite:
    results: list[LinkMutant] = field(default_factory=list)
    controls: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)

    def add_control(self, name: str, unit: str, lc: LinkCheck) -> list[int]:
        """Record a positive control; returns its unreached slots (vacuous guard)."""
        unreached = sorted({n for _, v in lc.entailment
                            for n in v.data.get("slots_unreached", ())})
        self.controls.append({"name": name, "unit": unit, "ok": not lc.rejected,
                              "failures": lc.failures()[:4], "slots_unreached": unreached})
        return unreached

    @property
    def counted(self) -> list["LinkMutant"]:
        return [r for r in self.results if r.equivalent is None]

    @property
    def all_rejected(self) -> bool:
        return bool(self.counted) and all(r.rejected for r in self.counted)

    @property
    def all_mismatch(self) -> bool:
        return bool(self.counted) and all(r.mismatch for r in self.counted)

    @property
    def controls_ok(self) -> bool:
        return bool(self.controls) and all(c["ok"] for c in self.controls)

    def extend(self, other: "LinkSuite") -> None:
        self.results += other.results
        self.controls += other.controls
        self.skipped += other.skipped

    def summary(self) -> dict[str, Any]:
        by: dict[str, dict[str, int]] = {}
        for r in self.counted:
            k = r.name.split("@")[0]
            d = by.setdefault(k, {"mutants": 0, "rejected": 0, "mismatch": 0})
            d["mutants"] += 1
            d["rejected"] += r.rejected
            d["mismatch"] += r.mismatch
        eq = [r for r in self.results if r.equivalent is not None]
        return {"mutants": len(self.counted),
                "rejected": sum(r.rejected for r in self.counted),
                "mismatch": sum(r.mismatch for r in self.counted),
                "all_rejected": self.all_rejected, "all_mismatch": self.all_mismatch,
                "equivalent_mutants": len(eq),
                "equivalent_rejected": sum(r.rejected for r in eq),
                "equivalent_units": sorted({r.unit for r in eq}),
                "vacuous_controls": [c for c in self.controls if c.get("slots_unreached")],
                "controls": len(self.controls),
                "controls_ok": self.controls_ok,
                "controls_failed": [c for c in self.controls if not c["ok"]][:20],
                "skipped": len(self.skipped), "by_class": by}

    def to_json(self) -> dict[str, Any]:
        d = self.summary()
        d["results"] = [r.to_json() for r in self.results]
        d["skipped_items"] = self.skipped
        return d


def _verdict(name: str, uid: str, lc: LinkCheck, *, structural: str | None = None,
             source: str = "") -> LinkMutant:
    reasons: list[str] = []
    mismatch = False
    for _, v in lc.equiv + lc.entailment:
        if not v.ok:
            reasons.append(f"{v.status}:{v.reason}")
            mismatch |= v.status == "mismatch"
    for f in lc.facts:
        c = f["cert"]
        if not c.certified:
            reasons.append(f"fact_{c.status}")
            mismatch |= c.status in ("not_constant", "unreached")
        elif c.value != f["proposed"]:
            reasons.append("fact_derivation_mismatch")
            mismatch = True
    return LinkMutant(name, uid, lc.rejected, mismatch, sorted(set(reasons)),
                      "; ".join(lc.failures())[:600], structural, source)


def _structural(link: Any, uid: str, st_off: int, offsets: Any, code_address: int) -> str:
    """WP18's strict template decoder on the (mutated) placed stub (informational)."""
    from ..formats.sc_records import ScRecord
    from ..guard import GuardError, decode_guard

    lu = link.by_uid()[uid]
    rec = ScRecord.from_bytes(lu.record)
    reg = {n: (rec.jmp_insn_offset[n], rec.jmp_reset_offset[n]) for n in (0, 1)
           if rec.jmp_insn_offset[n] != 0xFFFF}
    try:
        decode_guard(lu.body[:rec.tc_size], st_off, tc_abs=code_address + rec.tc_offset,
                     struct_off=rec.tb_offset - rec.tc_offset, offsets=offsets, registered=reg)
        return "decodes"
    except GuardError as e:
        return f"{e.reason}: {e.detail[:80]}"


# ---------------------------------------------------------------------------
# suites
# ---------------------------------------------------------------------------

def guard_mutants(snapshot: Any, link: Any, *, layout: Any, contract: Any, offsets: Any,
                  source: str = "", effects: bool = True,
                  units: Iterable[str] | None = None) -> LinkSuite:
    """Atom drops, guard-before-effect and stub-store mutants on every grown unit of
    ``link`` (each checked alone with :func:`certify_link` ``only={uid}``), plus the
    unmutated unit as a positive control."""
    out = LinkSuite()
    w = world_of(snapshot, layout)
    ca = snapshot.header.code_address

    def run(lk: Any, uid: str) -> LinkCheck:
        return certify_link(snapshot, lk, layout=layout, contract=contract, offsets=offsets,
                            world=w, only={uid})

    for uid in sorted(units if units is not None else link.grown):
        g = link.grown[uid]
        unreached = out.add_control("unmutated_grown_unit", uid, run(link, uid))
        n0 = len(out.results)
        lu = link.by_uid()[uid]
        for spec, st in g.stubs:
            try:
                atoms = stub_atoms(lu.body, st.off)
            except ValueError as e:
                out.skipped.append({"unit": uid, "why": f"stub not parsed: {e}"})
                continue
            for a in atoms:
                nm = f"drop_{a.atom}" + (f"[{a.k}]" if a.atom == "g_pc" else "")
                m = patch_unit(link, uid, drop_atom_patches(lu.body, a))
                out.results.append(_verdict(nm, uid, run(m, uid),
                                            structural=_structural(m, uid, st.off, offsets, ca),
                                            source=source))
            m = patch_unit(link, uid, stub_store_patch(atoms[0]))
            out.results.append(_verdict("guard_stub_store", uid, run(m, uid),
                                        structural=_structural(m, uid, st.off, offsets, ca),
                                        source=source))
            if effects:
                hoists = effect_hoists(g.unit, spec.tail_off, st.off, offsets, ca)
                if not hoists:
                    out.skipped.append({"unit": uid, "why": "guard_before_effect: no hoistable "
                                        "effect store before the tail"})
                for tag, ps in hoists:
                    m = patch_unit(link, uid, ps)
                    out.results.append(_verdict(f"guard_before_effect@{tag}", uid, run(m, uid),
                                                source=source))
        if unreached:
            why = (f"control: slots {unreached} unreachable (an atom is concretely false at "
                   "the exit), so the guard never passes")
            for r in out.results[n0:]:
                r.equivalent = why
    return out


def _cert_fields(snapshot: Any, link: Any, uid: str, n: int, *, layout: Any, offsets: Any,
                 fields: Sequence[str]) -> dict[str, Any]:
    from .equiv import certify_fact

    w = world_of(snapshot, layout)
    rp = r_prime(link, uid, w, offsets=offsets, snapshot=snapshot)
    return {f: certify_fact(rp, n, fact_field(f, layout), layout=layout) for f in fields}


FACT_FIELDS = ("r0", "r1", "r2", "r3")


def _spec_c_pool(snapshot: Any) -> list[Any]:
    """SPEC-eligible continuation units (key admissible, one page, relocatable)."""
    from ..guard import CS_CONDEXEC_MASK, CS_M_FP_BITS, CS_THUMB, PSTATE_SS, SS_ACTIVE

    out = []
    for u in snapshot.units():
        k = u.key
        if u.reloc.relocatable and u.meta_status == "ok" and bool(k.cs_base & CS_THUMB) and \
                not k.cs_base & (CS_CONDEXEC_MASK | CS_M_FP_BITS) and \
                not k.flags & (PSTATE_SS | SS_ACTIVE) and k.page_addr1 is None and u.vaddrs:
            out.append(u)
    out.sort(key=lambda u: u.vaddrs[0])
    return out


def _spec_plan(pairs: Sequence[tuple[Any, Any, str, Any, tuple]], *, max_variants: int = 1024
               ) -> Any:
    """``pairs``: (R, tail, exit class, C, facts) -> one SPEC slot 0/first free per R,
    an identity variant of C with ``facts``."""
    from ..guard import GuardTarget
    from ..link import B3Plan, GuardedExitSpec, VariantSpec, free_slots

    grow = {}
    vs = []
    for r, t, xc, c, facts in pairs:
        n = free_slots(r)[0]
        kf, kf2 = c.key.flags, c.key.cs_base
        grow[r.uid] = (GuardedExitSpec(t.off, xc, (GuardTarget(c.vaddrs[0] & ~1, n, "SPEC"),),
                                       kf, kf2),)
        vs.append(VariantSpec(c.uid, r.uid, n, facts=tuple(facts)))
    return B3Plan(grow=grow, variants=tuple(vs), max_variants=max_variants)


def fact_link_mutants(snapshot: Any, items: Sequence[tuple[Any, Any, str]], *, layout: Any,
                      contract: Any, offsets: Any, source: str = "",
                      batch: int = 200) -> LinkSuite:
    """For every item R: a SPEC link to an identity variant of a key-bound C.

    Control: the variant's facts are the fields hostsym certifies at R's slot
    (or none). Mutant ``falsified_fact_link``: one fact per R, value = certified
    value + 1 (``r0`` claimed 0 where r0 is not constant). Each batch is linked
    by NativeLinkerV2 and checked per R with ``only={R}``."""
    from ..link import NativeLinkerV2, free_slots

    out = LinkSuite()
    cs = _spec_c_pool(snapshot)
    if not cs:
        out.skipped.append({"why": "no SPEC-eligible continuation unit"})
        return out
    w = world_of(snapshot, layout)
    for b0 in range(0, len(items), batch):
        chunk = items[b0:b0 + batch]
        pairs = [(r, t, xc, cs[(b0 + i) % len(cs)], ()) for i, (r, t, xc) in enumerate(chunk)]
        base = NativeLinkerV2(snapshot, _spec_plan(pairs), offsets=offsets,
                              derive_captures=False).link()
        if not base.ok:
            out.skipped.append({"why": f"batch {b0}: link failed {base.failures[:1]}"})
            continue
        good, bad = [], []
        for r, t, xc, c, _ in pairs:
            n = free_slots(r)[0]
            certs = _cert_fields(snapshot, base, r.uid, n, layout=layout, offsets=offsets,
                                 fields=FACT_FIELDS)
            ok = [(f, c_.value) for f, c_ in certs.items() if c_.certified]
            tag = f"{r.uid}->{c.uid}"
            good.append((r, t, xc, c, tuple(synthetic_fact(f, v, tag) for f, v in ok)))
            if ok:
                f, v = ok[0]
                bad.append((r, t, xc, c, (synthetic_fact(f, v + 1, tag + "/bad"),),
                            f"{f}=={(v + 1) & 0xFFFFFFFF:#x} (certified {v:#x})"))
            else:
                bad.append((r, t, xc, c, (synthetic_fact("r0", 0, tag + "/bad"),),
                            "r0==0 (r0 not constant at the slot)"))
        gl = NativeLinkerV2(snapshot, _spec_plan(good), offsets=offsets,
                            derive_captures=False).link()
        bl = NativeLinkerV2(snapshot, _spec_plan([x[:5] for x in bad]), offsets=offsets,
                            derive_captures=False).link()
        if not (gl.ok and bl.ok):
            out.skipped.append({"why": f"batch {b0}: link failed {gl.failures[:1]} "
                                f"{bl.failures[:1]}"})
            continue
        for (r, _, _, _, facts), (_, _, _, _, _, why) in zip(good, bad):
            lc = certify_link(snapshot, gl, layout=layout, contract=contract, offsets=offsets,
                              world=w, only={r.uid})
            out.add_control(f"certified_link({len(facts)} facts)", r.uid, lc)
            m = certify_link(snapshot, bl, layout=layout, contract=contract, offsets=offsets,
                             world=w, only={r.uid})
            res = _verdict("falsified_fact_link", r.uid, m, source=source)
            res.detail = f"claim {why}; " + res.detail
            out.results.append(res)
    return out


def suffix_pairs(snapshot: Any, items: Sequence[tuple[Any, Any, str]]
                 ) -> list[tuple[tuple, tuple]]:
    """(full, suffix) return items: same end address, the suffix starts later."""
    rets = [x for x in items if x[2] == "return"]
    by_end: dict[int, list[tuple]] = {}
    for x in rets:
        u = x[0]
        by_end.setdefault(u.vaddrs[0] + u.guest_size, []).append(x)
    out = []
    for _, xs in sorted(by_end.items()):
        xs = sorted(xs, key=lambda x: x[0].vaddrs[0])
        full = xs[0]
        for suf in xs[1:]:
            if suf[0].vaddrs[0] > full[0].vaddrs[0]:
                out.append((full, suf))
    return out


def shared_variant_mutants(snapshot: Any, items: Sequence[tuple[Any, Any, str]], *,
                           layout: Any, contract: Any, offsets: Any,
                           source: str = "") -> LinkSuite:
    """F-B3-20 shape: for every (full, suffix) return pair whose full unit has a fact
    the suffix does not establish, link the full unit's variant and share it."""
    from ..link import NativeLinkerV2, free_slots

    out = LinkSuite()
    cs = _spec_c_pool(snapshot)
    w = world_of(snapshot, layout)
    used_c: dict[str, int] = {}
    for k, (full, suf) in enumerate(suffix_pairs(snapshot, items)):
        c = cs[k % len(cs)]
        rf, rs = full[0], suf[0]
        probe = NativeLinkerV2(snapshot, _spec_plan([(*full, c, ()), (*suf, c, ())]),
                               offsets=offsets, derive_captures=False).link()
        if not probe.ok:
            out.skipped.append({"unit": rs.uid, "why": f"probe link {probe.failures[:1]}"})
            continue
        nf, ns = free_slots(rf)[0], free_slots(rs)[0]
        cf = _cert_fields(snapshot, probe, rf.uid, nf, layout=layout, offsets=offsets,
                          fields=FACT_FIELDS)
        csu = _cert_fields(snapshot, probe, rs.uid, ns, layout=layout, offsets=offsets,
                           fields=FACT_FIELDS)
        pick = next(((f, x.value) for f, x in cf.items() if x.certified and
                     not (csu[f].certified and csu[f].value == x.value)), None)
        if pick is None:
            out.skipped.append({"unit": rs.uid, "full": rf.uid,
                                "why": "no field certified at the full unit and not equal "
                                "at the suffix (sharing would be sound)"})
            continue
        f, val = pick
        facts = (synthetic_fact(f, val, f"{rf.uid}->{c.uid}"),)
        used_c[c.uid] = used_c.get(c.uid, 0) + 1
        # correct plan: full -> variant(facts); suffix -> its own identity variant (no facts)
        good = NativeLinkerV2(snapshot, _spec_plan([(*full, c, facts), (*suf, c, ())]),
                              offsets=offsets, derive_captures=False).link()
        if not good.ok:
            out.skipped.append({"unit": rs.uid, "why": f"link {good.failures[:1]}"})
            continue
        lc = certify_link(snapshot, good, layout=layout, contract=contract, offsets=offsets,
                          world=w, only={rf.uid, rs.uid})
        out.add_control("full_and_suffix_own_variants", f"{rf.uid}+{rs.uid}", lc)
        # mutant 1: the suffix slot's LinkEntry points at the full unit's variant
        ext = good.ext
        owner = {lu.uid: lu.new_tb_offset for lu in good.units}
        si = next(i for i, s in enumerate(ext.slots) if s.src_tb_offset == owner[rs.uid])
        vf = next(v.index for v in good.variants if v.pred_uid == rf.uid)
        shared = add_link(good, si, vf)
        m = certify_link(snapshot, shared, layout=layout, contract=contract, offsets=offsets,
                         world=w, only={rf.uid, rs.uid})
        res = _verdict("shared_variant_suffix", f"{rf.uid}+{rs.uid}", m, source=source)
        res.detail = f"fact {f}=={val:#x} certified at the full unit; " + res.detail
        out.results.append(res)
        # mutant 2 (plan form): the suffix gets a second copy of the same variant
        dup = NativeLinkerV2(snapshot, _spec_plan([(*full, c, facts), (*suf, c, facts)]),
                             offsets=offsets, derive_captures=False).link()
        if dup.ok:
            m2 = certify_link(snapshot, dup, layout=layout, contract=contract,
                              offsets=offsets, world=w, only={rf.uid, rs.uid})
            res2 = _verdict("shared_variant_suffix_copy", f"{rf.uid}+{rs.uid}", m2,
                            source=source)
            res2.detail = f"fact {f}=={val:#x}; " + res2.detail
            out.results.append(res2)
        else:
            out.skipped.append({"unit": rs.uid, "why": f"copy plan {dup.failures[:1]}"})
    return out
