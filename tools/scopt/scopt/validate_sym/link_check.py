"""``hostsym`` on a NativeLinker v2 result: the V12/V13/V14 inputs of ``validate_b3``. WP19 integration.

WP18's :class:`~scopt.link.NativeLinkerV2` materializes a :class:`~scopt.link.B3Plan`
(grown units with the §4.1 guard stub, relocated variants, same-length rewrites)
and :func:`scopt.validate.validate_b3` runs WP19's row bodies on verdicts the
caller computes (``hostsym={"equiv", "entailment", "facts", "selftest"}``).
This module computes those verdicts **from the linked artifact**, not from the
plan, so that a wrong byte, slot entry or link in the artifact is what the
kernel sees:

* :func:`r_prime` — the :class:`~.program.Program` of a grown unit R' at its
  placed offsets (the bytes of its :class:`~scopt.link.UnitLink`), with one
  :class:`~.program.GSlot` per guard target. The slot's position is the
  record's ``jmp_insn_offset[n]`` (registered) or the emitter's slot jump
  (unregistered v5 slots). The **declared atoms** are independent of the stub
  bytes: g_bp/g_thumb/g_cond, g_flags/g_flags2 from the continuation key
  (C's record key for a linked SPEC slot, the plan's ``GuardedExitSpec`` key
  otherwise, through :func:`scopt.guard.guard_key`) and g_pc from the ext slot
  entry's ``target_vaddr`` (the plan's target for an unregistered slot), at the
  EnvLayout's displacements (:class:`scopt.guard.GuardOffsets`).
* :func:`c_prime` — the Program of a variant C' at its arena offsets, with C's
  metadata (identity offset map: a variant is ``relocate(C)`` + same-length patches).
  NativeLinker v2 clones the *effective* C: when C is itself grown in the same
  link, C' carries C's guard stub and slots, and is compared with the grown C
  (:func:`c_reference`), which is in turn checked against the captured C.
* :func:`certify_link` — for every grown unit ``check_guarded_chain(R, R')``
  (V12 + V13 entailment); for every rewritten, non-grown unit
  ``check_equiv(U, U')``; for every variant ``check_equiv(C, C', P)`` with
  ``P = facts ∧ guard atoms`` of each ext link that enters it; and for every
  ext link and every fact of its variant ``certify_fact(R', n, field)`` (V14).
  The result is the ``hostsym=`` mapping of ``validate_b3``.

A link whose slot or variant cannot be resolved in the artifact is reported as a
V14 item with an ``unreached`` certificate (fails closed).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from .equiv import FactCert, Verdict, certify_fact, check_equiv, check_guarded_chain
from .program import GSlot, GuardAtom, Program, World, program_of

__all__ = ["fact_field", "declared_atoms", "r_prime", "c_prime", "certify_link", "LinkCheck",
           "slot_owner", "variant_of_index", "r_label", "u_label", "c_label", "link_label",
           "fact_label", "HostsymRequired", "required_hostsym"]

_RET_NONE = 0xFFFF
_FLAG_FIELDS = ("NF", "ZF", "CF", "VF")


def fact_field(name: str, layout: Any = None) -> tuple[int, int]:
    """Env ``(offset, width)`` of a fact field (``r0..r12``, ``NF``, ``ZF``, ``CF``, ``VF``)."""
    if name.startswith("r") and name[1:].isdigit() and 0 <= int(name[1:]) <= 15:
        n = int(name[1:])
        if layout is not None and layout.has_field(f"regs[{n}]"):
            return layout.offset(f"regs[{n}]"), 4
        return 4 * n, 4
    if name in _FLAG_FIELDS:
        if layout is None or not layout.has_field(name):
            raise KeyError(f"fact field {name} needs an EnvLayout")
        return layout.offset(name), 4
    raise KeyError(f"unknown fact field {name!r}")


def _fact_name_value(f: Any) -> tuple[str, int]:
    if isinstance(f, Mapping):
        v = f["value"]
        return str(f["field"]), int(v, 0) if isinstance(v, str) else int(v)
    return str(f.field), int(f.value)


def declared_atoms(offsets: Any, kf: int, kf2: int, vaddr: int, *, r0_zero: bool = False) -> tuple[GuardAtom, ...]:
    """The six atoms a guard pass must imply for one target (§4.1, §4.2)."""
    o = offsets
    atoms = (GuardAtom("g_bp", o.bp, 8, 0, "zero", o.FIELDS["bp"]),
            GuardAtom("g_thumb", o.thumb, 1, 1, "eq", o.FIELDS["thumb"]),
            GuardAtom("g_cond", o.condexec, 4, 0, "zero", o.FIELDS["condexec"]),
            GuardAtom("g_flags", o.flags, 4, kf & 0xFFFFFFFF, "eq", o.FIELDS["flags"]),
            GuardAtom("g_flags2", o.flags2, 8, kf2 & ((1 << 64) - 1), "eq", o.FIELDS["flags2"]),
            GuardAtom("g_pc", o.pc, 4, vaddr & 0xFFFFFFFF, "eq", o.FIELDS["pc"]))
    if r0_zero:
        assert o.r0 is not None
        return atoms[:-1] + (GuardAtom("g_r0_zero", o.r0, 4, 0, "zero", "regs[0]"), atoms[-1])
    return atoms


def slot_owner(link: Any) -> dict[int, str]:
    """Placed ``tb_offset`` -> uid of every linked unit."""
    return {lu.new_tb_offset: lu.uid for lu in link.units}


def variant_of_index(link: Any, index: int) -> Any:
    return next((v for v in getattr(link, "variants", ()) if v.index == index), None)


# ---------------------------------------------------------------------------
# hostsym labels and the coverage a link requires (B3/B4 design §9.1)
# ---------------------------------------------------------------------------
# certify_link names its verdicts with these helpers and scopt.validate
# computes the labels a link REQUIRES with the same helpers (required_hostsym),
# so coverage is decided from the link itself, never from what a caller
# happened to supply.

def r_label(uid: str) -> str:
    """V12 label of a grown unit R' (``check_guarded_chain(R, R')``)."""
    return f"R':{uid}"


def u_label(uid: str) -> str:
    """V12 label of a same-length-rewritten (not grown) unit U' (``check_equiv(U, U')``)."""
    return f"U':{uid}"


def c_label(v: Any, r_uid: str | None = None, slot_n: int | None = None) -> str:
    """V12 label of variant C' (``check_equiv(C, C', P)``) entered from ``(r_uid, slot_n)``
    (no entering link: the bare variant)."""
    return f"C':{v.src_uid}@v{v.index}" + ("" if r_uid is None else f"<-{r_uid}|{slot_n}")


def link_label(r_uid: str | None, slot_n: int | None, variant_index: int) -> str:
    """V14 prefix of one SPEC link (ext ``LinkEntry``)."""
    return f"{r_uid}|{slot_n}->v{variant_index}"


def fact_label(prefix: str, name: str) -> str:
    """V14 label of one fact of the variant a SPEC link enters."""
    return f"{prefix}:{name}"


def _resolve_link(link: Any, lk: Any) -> tuple[Any, Any, Any, str | None]:
    """``(slot entry, variant entry, VariantLink, R uid)`` of an ext link (None when it
    does not resolve in the artifact)."""
    ext = link.ext
    se = ext.slots[lk.slot_index] if 0 <= lk.slot_index < len(ext.slots) else None
    ve = ext.variants[lk.variant_index] if 0 <= lk.variant_index < len(ext.variants) else None
    v = variant_of_index(link, ve.variant_id) if ve is not None else None
    r_uid = slot_owner(link).get(se.src_tb_offset) if se is not None else None
    return se, ve, v, r_uid


def _has_guarded_slot(g: Any) -> bool:
    return any(st.slots for _spec, st in getattr(g, "stubs", ()))


@dataclass(frozen=True)
class HostsymRequired:
    """The hostsym verdicts a link needs before an edited artifact is certified.

    ``equiv`` (V12): every changed unit — every grown R', every same-length-rewritten
    unit U', every variant C' once per entering SPEC link. ``entailment`` (V13 path
    part): every grown R' with at least one guarded slot. ``links`` (V14): one
    ``(prefix, fact labels)`` per SPEC link; every fact its variant declares must be
    certified. A link to a variant that declares no fact has an empty tuple (its
    ``P`` is the guard atoms alone: V13 entailment on R' and V12 on the C' context
    certify it). A link that does not resolve in the artifact requires its bare
    prefix, which only certify_link's ``unreached`` item carries (fails closed)."""

    equiv: tuple[str, ...] = ()
    entailment: tuple[str, ...] = ()
    links: tuple[tuple[str, tuple[str, ...]], ...] = ()

    @property
    def facts(self) -> tuple[str, ...]:
        return tuple(x for _p, fs in self.links for x in fs)

    @property
    def empty(self) -> bool:
        return not (self.equiv or self.entailment or self.links)

    def to_json(self) -> dict[str, Any]:
        return {"equiv": list(self.equiv), "entailment": list(self.entailment),
                "links": [[p, list(fs)] for p, fs in self.links]}


def required_hostsym(link: Any, plan: Any = None) -> HostsymRequired:
    """The verdict labels ``link`` (a :class:`~scopt.link.LinkResultV2`) requires,
    computed from the link (and ``plan``, a :class:`~scopt.link.B3Plan`, when given:
    its grown and rewritten units are required even if the link lost them)."""
    grown = dict(getattr(link, "grown", {}) or {})
    rewritten = dict(getattr(link, "rewritten", {}) or {})
    g_uids = set(grown) | set(getattr(plan, "grow", {}) or {})
    rw_uids = (set(rewritten) | set(getattr(plan, "rewrites", {}) or {})) - g_uids
    equiv = [r_label(u) for u in sorted(g_uids)] + [u_label(u) for u in sorted(rw_uids)]
    ent = [u for u in sorted(g_uids)
           if u not in grown or _has_guarded_slot(grown[u])]
    links: list[tuple[str, tuple[str, ...]]] = []
    entering: dict[int, list[tuple[str | None, int | None]]] = {}
    ext = getattr(link, "ext", None)
    for lk in (ext.links if ext is not None else ()):
        se, _ve, v, r_uid = _resolve_link(link, lk)
        n = None if se is None else se.slot_n
        prefix = link_label(r_uid, n, lk.variant_index)
        if v is None or r_uid is None or se is None:
            links.append((prefix, (prefix,)))
            continue
        entering.setdefault(v.index, []).append((r_uid, n))
        links.append((prefix, tuple(fact_label(prefix, _fact_name_value(f)[0])
                                    for f in v.facts)))
    for v in getattr(link, "variants", ()):
        for r_uid, n in entering.get(v.index) or [(None, None)]:
            equiv.append(c_label(v, r_uid, n))
    return HostsymRequired(tuple(equiv), tuple(ent), tuple(links))


def _ext_slots(link: Any) -> list[Any]:
    ext = getattr(link, "ext", None)
    return list(ext.slots) if ext is not None else []


def _spec_keys(link: Any, snapshot: Any) -> dict[tuple[str, int], tuple[int, int, str]]:
    """``(R uid, n)`` -> C's ``(flags, cs_base, C uid)`` for every ext link (by the
    variant's ``general_tb_offset`` record, i.e. the artifact, not the plan)."""
    ext = getattr(link, "ext", None)
    if ext is None:
        return {}
    owner = slot_owner(link)
    units = {u.uid: u for u in snapshot.units()}
    out: dict[tuple[str, int], tuple[int, int, str]] = {}
    for lk in ext.links:
        if not (0 <= lk.slot_index < len(ext.slots)) or not (0 <= lk.variant_index < len(ext.variants)):
            continue
        se = ext.slots[lk.slot_index]
        ve = ext.variants[lk.variant_index]
        r_uid = owner.get(se.src_tb_offset)
        c_uid = owner.get(ve.general_tb_offset)
        if r_uid is None or c_uid is None or c_uid not in units:
            continue
        c = units[c_uid]
        out[(r_uid, se.slot_n)] = (c.key.flags, c.key.cs_base, c_uid)
    return out


def r_prime(link: Any, uid: str, world: World, *, offsets: Any,
            snapshot: Any = None) -> Program:
    """The Program of grown unit ``uid`` as placed in ``link`` (see the module doc)."""
    from ..formats.sc_records import ScRecord
    from ..formats.sc_semantic import KIND_SPEC
    from ..guard import GuardError, guard_key

    g = link.grown[uid]
    lu = link.by_uid()[uid]
    rec = ScRecord.from_bytes(lu.record)
    ca = world.code_address
    base = program_of(g.unit, world)
    keys = _spec_keys(link, snapshot) if snapshot is not None else {}
    ext_slots = {(se.src_tb_offset, se.slot_n): se for se in _ext_slots(link)}
    slots: dict[int, GSlot] = {}
    for spec, st in g.stubs:
        for si in st.slots:
            n = si.slot_n
            registered = rec.jmp_insn_offset[n] != _RET_NONE if n in (0, 1) else False
            jmp_off = rec.jmp_insn_offset[n] - 1 if registered else si.jmp_off
            se = ext_slots.get((lu.new_tb_offset, n)) if registered else None
            vaddr = se.target_vaddr if se is not None else si.vaddr
            kf_src = keys.get((uid, n))
            try:
                if kf_src is not None:
                    kf, kf2 = guard_key(kf_src[0], kf_src[1])
                else:
                    kf, kf2 = guard_key(spec.key_flags, spec.key_cs_base)
            except GuardError:
                # an inadmissible key: declare an atom no guard can entail (fails closed)
                kf, kf2 = 0xFFFFFFFF, (1 << 64) - 1
            kind = "SPEC" if (se is not None and se.kind == KIND_SPEC) or si.kind == "SPEC" \
                else "GENERAL"
            want_r0 = any(t.slot_n == n and t.r0_zero for t in spec.targets)
            slots[jmp_off] = GSlot(n, kind, vaddr,
                                   declared_atoms(offsets, kf, kf2, vaddr, r0_zero=want_r0))
    code = bytes(lu.body[:rec.tc_size])
    return replace(base, uid=uid + "'", code=code, base=ca + rec.tc_offset,
                   struct_base=ca + rec.tb_offset, own_end=ca + rec.end_offset,
                   slots=slots, stub=(g.unit.stub_start, rec.tc_size))


def placed_program(link: Any, uid: str, world: World) -> Program:
    """A rewritten (not grown) unit as placed: its effective metadata, the linked bytes."""
    from ..formats.sc_records import ScRecord

    eff = link.effective[uid]
    lu = link.by_uid()[uid]
    rec = ScRecord.from_bytes(lu.record)
    ca = world.code_address
    return replace(program_of(eff, world), uid=uid + "'", code=bytes(lu.body[:rec.tc_size]),
                   base=ca + rec.tc_offset, struct_base=ca + rec.tb_offset,
                   own_end=ca + rec.end_offset)


def c_reference(link: Any, v: Any, snapshot: Any, world: World, *, offsets: Any) -> Program:
    """What variant ``v`` is compared with: the captured C, or — when C is itself
    grown in this link (NativeLinker v2 clones the *effective* unit, so the clone
    carries C's guard stub and slots) — the grown C' as placed (:func:`r_prime`),
    which :func:`certify_link` checks against the captured C separately."""
    if v.src_uid in getattr(link, "grown", {}):
        return r_prime(link, v.src_uid, world, offsets=offsets, snapshot=snapshot)
    return program_of(snapshot.unit(v.src_uid), world)


def c_prime(link: Any, v: Any, snapshot: Any, world: World, *, offsets: Any = None) -> Program:
    """The Program of variant ``v`` (:class:`~scopt.link.VariantLink`) at its arena offsets,
    with the metadata of :func:`c_reference` (identity offset map)."""
    ca = world.code_address
    if v.src_uid in getattr(link, "grown", {}):
        from ..guard import GuardOffsets

        ref = c_reference(link, v, snapshot, world,
                          offsets=offsets or GuardOffsets.from_layout(world.layout))
    else:
        ref = program_of(snapshot.unit(v.src_uid), world)
    return replace(ref, uid=f"variant:{v.key}", code=bytes(v.code),
                   base=ca + v.tc_offset, struct_base=ca + v.tb_offset,
                   own_end=ca + v.end_offset)


@dataclass
class LinkCheck:
    """What :func:`certify_link` computed (``hostsym`` is the ``validate_b3`` input)."""

    equiv: list[tuple[str, Verdict]]
    entailment: list[tuple[str, Verdict]]
    facts: list[dict[str, Any]]
    selftest: Mapping[str, Any] | None = None

    @property
    def hostsym(self) -> dict[str, Any]:
        return {"equiv": list(self.equiv), "entailment": list(self.entailment),
                "facts": list(self.facts), "selftest": self.selftest}

    @property
    def rejected(self) -> bool:
        """True when any kernel verdict would fail its row (selftest/provenance aside)."""
        if any(not v.ok for _, v in self.equiv) or any(not v.ok for _, v in self.entailment):
            return True
        return any(not f["cert"].certified or f["cert"].value != f["proposed"]
                   for f in self.facts)

    def failures(self) -> list[str]:
        out = [f"{lab}: {v.status} {v.reason} {v.detail[:160]}" for lab, v in self.equiv
               if not v.ok]
        out += [f"entail {lab}: {v.status} {v.reason}" for lab, v in self.entailment if not v.ok]
        for f in self.facts:
            c = f["cert"]
            if not c.certified:
                out.append(f"fact {f['label']}: {c.status} ({c.detail[:120]})")
            elif c.value != f["proposed"]:
                out.append(f"fact {f['label']}: certified {c.value} != proposed {f['proposed']}"
                           " (fact_derivation_mismatch)")
        return out

    def to_json(self) -> dict[str, Any]:
        return {"rejected": self.rejected,
                "equiv": [[lab, v.to_json()] for lab, v in self.equiv],
                "entailment": [[lab, v.to_json()] for lab, v in self.entailment],
                "facts": [{"label": f["label"], "cert": f["cert"].to_json(),
                           "proposed": f["proposed"], "provenance": f.get("provenance")}
                          for f in self.facts],
                "failures": self.failures()}


def certify_link(snapshot: Any, link: Any, *, layout: Any, contract: Any = None,
                 offsets: Any = None, world: World | None = None,
                 provenance: Callable[[Any, Any], Any] | Mapping[str, Any] | None = None,
                 selftest: Mapping[str, Any] | None = None,
                 env_abs: tuple[int, int] | None = None,
                 only: Any = None) -> LinkCheck:
    """Run the kernel on every changed unit, variant and link of ``link``.

    ``provenance`` maps a fact (its ``fact_id``) to its ``patch-join@1`` row, or
    is a callable ``(variant, fact) -> row``; without it V14's provenance row
    fails (``provenance_unexplained``), which is the fail-closed default.

    ``only`` (a set of unit uids) restricts the run to those grown/rewritten
    units, the links whose R is one of them and the variants entered from
    them (mutation tests check one unit of a large link); ``None`` checks all.
    """
    from ..guard import GuardOffsets
    from .program import world_of

    w = world or world_of(snapshot, layout)
    off = offsets or GuardOffsets.from_layout(layout)
    equiv: list[tuple[str, Verdict]] = []
    ent: list[tuple[str, Verdict]] = []
    facts: list[dict[str, Any]] = []
    rps: dict[str, Program] = {}
    keep = (lambda uid: True) if only is None else (lambda uid: uid in only)
    for uid in sorted(link.grown):
        if not keep(uid):
            continue
        r = program_of(snapshot.unit(uid), w)
        rp = r_prime(link, uid, w, offsets=off, snapshot=snapshot)
        rps[uid] = rp
        v = check_guarded_chain(r, rp, contract, layout=layout, env_abs=env_abs)
        equiv.append((r_label(uid), v))
        ent.append((uid, v))
    for uid in sorted(getattr(link, "rewritten", {}) or {}):
        if uid in link.grown or not keep(uid):
            continue
        o = program_of(snapshot.unit(uid), w)
        equiv.append((u_label(uid), check_equiv(o, placed_program(link, uid, w), None, None,
                                               contract, layout=layout, env_abs=env_abs)))
    ext = getattr(link, "ext", None)
    owner = slot_owner(link)
    entering: dict[int, list[tuple[str, GSlot]]] = {}
    for lk in (ext.links if ext is not None else ()):
        se = ext.slots[lk.slot_index] if 0 <= lk.slot_index < len(ext.slots) else None
        ve = ext.variants[lk.variant_index] if 0 <= lk.variant_index < len(ext.variants) \
            else None
        v = variant_of_index(link, ve.variant_id) if ve is not None else None
        r_uid = owner.get(se.src_tb_offset) if se is not None else None
        if only is not None and r_uid not in only:
            continue
        lab = link_label(r_uid, None if se is None else se.slot_n, lk.variant_index)
        rp = rps.get(r_uid) if r_uid is not None else None
        gs = None if rp is None else next((s for s in rp.slots.values()
                                           if s.n == se.slot_n), None)
        if v is None or rp is None or gs is None:
            facts.append({"label": lab, "cert": FactCert((0, 0), "unreached",
                                                          detail="link does not resolve to a "
                                                          "grown R' slot and a variant"),
                          "proposed": None, "provenance": None})
            continue
        entering.setdefault(v.index, []).append((r_uid, gs))
        for f in v.facts:
            name, val = _fact_name_value(f)
            fld = fact_field(name, layout)
            cert = certify_fact(rp, se.slot_n, fld, layout=layout)
            prov = None
            if callable(provenance):
                prov = provenance(v, f)
            elif provenance is not None:
                prov = provenance.get(getattr(f, "fact_id", None) or
                                      (f.get("fact_id") if isinstance(f, Mapping) else None))
            facts.append({"label": fact_label(lab, name), "cert": cert, "proposed": val,
                          "provenance": prov})
    for v in getattr(link, "variants", ()):
        if only is not None and v.index not in entering:
            continue
        c = c_reference(link, v, snapshot, w, offsets=off)
        cp = c_prime(link, v, snapshot, w, offsets=off)
        pre_f: dict[tuple[int, int], int] = {}
        for f in v.facts:
            name, val = _fact_name_value(f)
            pre_f[fact_field(name, layout)] = val & 0xFFFFFFFF
        ctxs = entering.get(v.index) or [(None, None)]
        for r_uid, gs in ctxs:
            pre = dict(pre_f)
            if gs is not None:
                for a in gs.atoms:
                    pre[(a.off, a.width)] = a.value
            lab = c_label(v, r_uid, None if gs is None else gs.n)
            equiv.append((lab, check_equiv(c, cp, pre or None, None, contract, layout=layout,
                                           env_abs=env_abs)))
    return LinkCheck(equiv, ent, facts, selftest)


def hostsym_inputs(*, layout: Any, contract: Any = None, offsets: Any = None,
                   selftest: Mapping[str, Any] | None = None,
                   provenance: Callable[[Any, Any], Any] | Mapping[str, Any] | None = None,
                   env_abs: tuple[int, int] | None = None
                   ) -> Callable[[Any, Any], dict[str, Any]]:
    """A ``hostsym=`` callable for :func:`scopt.validate.validate_b3` /
    :func:`scopt.validate.package_b3`: ``(snapshot, link) -> certify_link(...).hostsym``."""

    def run(snapshot: Any, link: Any) -> dict[str, Any]:
        return certify_link(snapshot, link, layout=layout, contract=contract, offsets=offsets,
                            provenance=provenance, selftest=selftest, env_abs=env_abs).hostsym
    return run


def summary_provenance(rows: Any) -> Callable[[Any, Any], Any]:
    """Provenance of a cited fact from WP20 CalleeSummary rows: the fact row (with its
    ``patch-join@1`` source) whose ``fact_id``/``cert_hash`` the FactRef cites, else None."""
    idx: dict[tuple[str, str | None], dict[str, Any]] = {}
    for r in rows:
        for f in r.get("facts", ()):
            idx[(f["fact_id"], f.get("cert_hash"))] = {
                "summary_row_hash": r.get("row_hash"), "return_unit": r.get("return_unit"),
                "fact_id": f["fact_id"], "source": f.get("source"), "status": f.get("status")}

    def prov(_v: Any, f: Any) -> Any:
        fid = getattr(f, "fact_id", None) or (f.get("fact_id") if isinstance(f, Mapping) else None)
        ch = getattr(f, "cert_hash", None) or (f.get("cert_hash") if isinstance(f, Mapping)
                                               else None)
        return idx.get((fid, ch))
    return prov


def summary_certifier(snapshot: Any, layout: Any, world: World | None = None
                      ) -> Callable[[Any, Any, Any], tuple[bool | None, str | None]]:
    """WP20's ``Certifier`` (:data:`scopt.analysis.callee_summary.Certifier`) backed by
    V14: ``(unit, exit fact set, fact) -> (certified?, cert_hash)``.

    The fact is certified on the captured R at the lookup exit of the fact set's
    tail (``exit_pc = T + 9``): ``True`` with the certificate hash when hostsym
    derives the same constant on every path, ``False`` when it derives another
    constant or the field is not constant there (WP20 records
    ``fact_derivation_mismatch``), ``None`` when the exploration is inconclusive or
    the exit is not reached."""
    from .program import world_of

    w = world or world_of(snapshot, layout)
    progs: dict[str, Program] = {}

    def certify(u: Any, es: Any, f: Any) -> tuple[bool | None, str | None]:
        p = progs.get(u.uid)
        if p is None:
            p = progs[u.uid] = program_of(u, w)
        try:
            fld = fact_field(str(f.field), layout)
        except KeyError:
            return None, None
        tail = getattr(es, "tail", None)
        epc = None if tail is None or getattr(tail, "off", None) is None else tail.off + 9
        cert = certify_fact(p, None, fld, layout=layout, exit_pc=epc)
        if cert.status in ("inconclusive", "unreached"):
            return None, None
        if cert.certified and cert.value == (int(f.value) & ((1 << (8 * fld[1])) - 1)):
            return True, cert.cert_hash
        return False, None
    return certify


__all__ += ["hostsym_inputs", "summary_provenance", "summary_certifier", "c_reference",
            "placed_program"]
