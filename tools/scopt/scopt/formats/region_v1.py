"""CacheRegion v1 documents (design §4.5; schema ``schemas/cache_region.v1.json``). WP7.

The importer writes one ``tb_singleton`` region per valid slot to
``units.jsonl`` (slot order). Conventions this module fixes:

* ``native.code_sha256`` is the sha256 of the slot's arena bytes
  ``[tc_offset, end_offset)`` (host code, search data and alignment pad), the
  same range the SnapshotId hashes and the bytes stored as
  ``code/<hex>.bin``; ``code_len`` is ``tc_size``.
* ``relocations`` lists only sites that need a fixup under a whole-TB move
  (``rule`` is required by the schema); every other reference is
  position-independent under such a move.
* ``exits`` are derived from the capture: one ``goto_tb`` per GOTO_TB site
  (its patchable ``jmp_insn_offset``/``jmp_reset_offset``, static destination
  from the StaticExit of that slot); one ``goto_ptr`` per GOTO_PTR site (static
  destination only for the off-page fallback, StaticExit slot 0xFF); one
  ``exit_requested`` per EXIT_TB_RET_JMP with idx 3; one ``exit_null`` per
  EXIT_TB_EPI_JMP and per EXIT_TB_RET_JMP idx 0/1 that is not the fallback of
  a goto_tb slot. Ordered by host offset; ids ``x0``, ``x1``, ...
  A ``no_meta`` unit has only the record's goto_tb slots (destination null).
* ``maps.events``: a ``guest_ld``/``guest_st`` per qemu_ld/st (host offset of
  its OpMark, recovery = its raddr point) and a ``helper_call`` (``plugin_cb``
  for plugin targets) per HELPER_CALL site (recovery = its return point).
* ``x_meta_status`` carries ``ok | no_meta | incomplete``.
* ``region_id`` = :func:`scopt.ids.region_id` (canonical JSON without
  ``region_id`` and ``provenance``).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from ..canon import hexaddr, sha256_id
from ..decode.x86 import sweep
from ..formats.enums import STATIC_EXIT_OFFPAGE_SLOT, Encoding, ExitKind, OpMarkKind, SiteKind
from ..ids import region_id
from ..model.unit import Exit, TBUnit
from . import schemas

__all__ = ["unit_exits", "unit_exits_at", "build_region", "validate_region", "key_json", "IMPORT_PASS",
           "UNBOUND_ID", "unit_stubs", "build_multi_tb_region", "guarded_exit_rows"]

IMPORT_PASS = {"name": "import", "version": 1}
_NO_JMP = 0xFFFF
_EXIT_REQUESTED = 3

#: Placeholder for bundle/contract/runtime ids when the import had no bundle
#: or RuntimeIdentity (``snapshot.json`` then records them as null).
UNBOUND_ID = sha256_id(b"scopt:unbound/1")


def key_json(u: TBUnit) -> dict[str, Any]:
    k = u.key
    return {"page_addr0": hexaddr(k.page_addr0),
            "page_addr1": None if k.page_addr1 is None else hexaddr(k.page_addr1),
            "flags": hexaddr(k.flags), "cs_base": hexaddr(k.cs_base), "cflags": hexaddr(k.cflags)}


def _rec_offsets(u: TBUnit) -> tuple[tuple[int, int], tuple[int, int]]:
    from .sc_records import ScRecord

    r = ScRecord.from_bytes(u.record)
    return r.jmp_insn_offset, r.jmp_reset_offset


def unit_exits(u: TBUnit) -> tuple[Exit, ...]:
    """The exits of one TB unit (see the module docstring)."""
    return tuple(e for _, e in unit_exits_at(u))


def unit_stubs(u: TBUnit) -> tuple[Any, ...]:
    """The decoded guard stubs of a grown unit (``u.stub_start`` set; WP18), as
    :class:`scopt.guard.DecodedGuard`; ``()`` otherwise. Raises
    :class:`scopt.guard.GuardError` on a stub that does not match the template."""
    ss = getattr(u, "stub_start", None)
    if ss is None:
        return ()
    from ..guard import decode_guard

    jio, jro = _rec_offsets(u)
    reg = {n: (jio[n], jro[n]) for n in (0, 1) if jio[n] != _NO_JMP}
    out = []
    off = ss
    while off < u.tc_size:
        dg = decode_guard(u.code, off, tc_abs=0, struct_off=u.tb_offset - u.tc_offset,
                          registered=reg)
        out.append(dg)
        off = dg.end
    return tuple(out)


def guarded_exit_rows(u: TBUnit) -> dict[int, dict[str, Any]]:
    """``{guard slot field offset: extra exit fields}`` of a grown unit's slots."""
    out: dict[int, dict[str, Any]] = {}
    for dg in unit_stubs(u):
        for si in dg.slots:
            out[si.field_off] = {"slot_kind": si.kind, "target_vaddr": hexaddr(si.vaddr),
                                 "guard_off": dg.off, "guard_len": dg.end - dg.off,
                                 "reset": si.reset_kind, "x_registered": si.registered,
                                 "x_slot_n": si.slot_n}
    return out


def unit_exits_at(u: TBUnit) -> tuple[tuple[int, Exit], ...]:
    """``(host offset of the exit site, Exit)`` pairs, in exit-id order.

    A grown unit (WP18) reports each slot of its guard stubs as a
    ``guarded_slot`` exit (registered or not; extra fields from
    :func:`guarded_exit_rows`); the stub's ``F`` is the exit's ``goto_ptr``."""
    jio, jro = _rec_offsets(u)
    rows: list[tuple[int, Exit]] = []
    gx = guarded_exit_rows(u) if getattr(u, "stub_start", None) is not None else {}
    if u.meta_status == "no_meta":
        for n in (0, 1):
            if jio[n] != _NO_JMP:
                rows.append((jio[n], Exit("", 0, ExitKind.GOTO_TB, n, None, None, jio[n],
                                          None if jro[n] == _NO_JMP else jro[n])))
    else:
        sx = {x.slot: x for x in u.static_exits}
        offpage = defaultdict(set)
        for x in u.static_exits:
            if x.slot == STATIC_EXIT_OFFPAGE_SLOT:
                offpage[x.insn_idx].add(x.dest_vaddr)
        lookup_count = Counter(s.insn_idx for s in u.sites if s.kind == SiteKind.GOTO_PTR)
        goto_slots = set()
        insn_of_tail = None
        for s in u.sites:
            if s.kind == SiteKind.GOTO_PTR and s.field_off >= (u.stub_start or 1 << 62):
                insn_of_tail = s.insn_idx
        for fo, x in gx.items():
            n = x["x_slot_n"]
            reg = x["x_registered"]
            if reg:
                goto_slots.add(n)
            rows.append((fo, Exit("", 0, ExitKind.GUARDED_SLOT, n if n >= 0 else None,
                                  insn_of_tail, int(x["target_vaddr"], 16),
                                  fo if reg else None, fo + 4 if reg else None)))
        for s in u.sites:
            if s.kind == SiteKind.GOTO_TB:
                n = s.aux
                if s.field_off in gx:
                    continue
                goto_slots.add(n)
                x = sx.get(n)
                io = jio[n] if 0 <= n <= 1 and jio[n] != _NO_JMP else None
                ro = jro[n] if 0 <= n <= 1 and jro[n] != _NO_JMP else None
                insn = x.insn_idx if x is not None else s.insn_idx
                rows.append((s.field_off, Exit("", 0, ExitKind.GOTO_TB, n, insn,
                                               None if x is None else x.dest_vaddr, io, ro)))
        for s in u.sites:
            if s.kind == SiteKind.GOTO_PTR:
                # A conditional off-page transfer may have two destinations
                # and two lookup tails at the same guest insn. SCM carries
                # no native-arm discriminator. Preserve the public lookup.
                targets = offpage.get(s.insn_idx, ())
                static = next(iter(targets)) if (len(targets) == 1 and
                                                 lookup_count[s.insn_idx] == 1) else None
                rows.append((s.insn_off, Exit("", 0, ExitKind.GOTO_PTR, None, s.insn_idx,
                                              static)))
            elif s.kind == SiteKind.EXIT_TB_RET_JMP:
                if s.aux == _EXIT_REQUESTED:
                    kind = ExitKind.EXIT_REQUESTED
                elif s.aux in goto_slots:
                    continue  # the chaining fallback of that goto_tb exit
                else:
                    kind = ExitKind.EXIT_NULL
                rows.append((s.insn_off, Exit("", 0, kind, None, s.insn_idx, None)))
            elif s.kind == SiteKind.EXIT_TB_EPI_JMP:
                rows.append((s.insn_off, Exit("", 0, ExitKind.EXIT_NULL, None, s.insn_idx, None)))
    rows.sort(key=lambda r: (r[0], r[1].kind.value, -1 if r[1].slot is None else r[1].slot))
    return tuple((at, replace(e, id=f"x{i}")) for i, (at, e) in enumerate(rows))


def _exit_json(e: Exit, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    d: dict[str, Any] = {"id": e.id, "member": e.member, "kind": e.kind.value, "slot": e.slot,
                         "insn": e.insn,
                         "static_dest_vaddr": None if e.static_dest_vaddr is None
                         else hexaddr(e.static_dest_vaddr)}
    d["patchable"] = ({"insn_offset": e.insn_offset, "reset_offset": e.reset_offset}
                      if e.insn_offset is not None and e.reset_offset is not None else None)
    if extra:
        d.update({k: v for k, v in extra.items() if k not in ("x_registered", "x_slot_n")})
        if not extra.get("x_registered", True):
            d["x_registered"] = False
    return d


def _guest_pages(u: TBUnit, page_bits: int) -> list[dict[str, Any]]:
    psize = 1 << page_bits
    mask = ~(psize - 1)
    out = [{"ram_addr": hexaddr(u.key.page_addr0 & mask), "len": psize}]
    if u.key.page_addr1 is not None:
        out.append({"ram_addr": hexaddr(u.key.page_addr1 & mask), "len": psize})
    return out


def _native_relocation_encodings(u: TBUnit) -> dict[int, Encoding]:
    """Report the machine encoding of each certified relocation without a TCG Site."""
    encodings = {}
    if not u.splice_applied:
        return encodings
    for request in u.native_fragments:
        splice = request.splice
        if splice is None or not splice.relocations:
            continue
        start = splice.start_off
        by_end = {insn.end: insn for insn in sweep(u.code[start:start + len(splice.code)], start)}
        for row in splice.relocations:
            field = start + row.field_off
            insn = by_end.get(start + row.next_off)
            if insn is not None:
                if insn.branch_target is not None and insn.imm_size == 4 and (
                        insn.address + insn.imm_offset == field):
                    encodings[field] = Encoding.REL32
                elif insn.rip_target is not None and insn.disp_size == 4 and (
                        insn.address + insn.disp_offset == field):
                    encodings[field] = Encoding.RIPREL32
    return encodings


def build_region(u: TBUnit, *, snapshot_id: str, tb_size: int, page_bits: int,
                 bundle: Mapping[str, Any] | None, runtime_identity_id: str | None,
                 search_len: int | None, site_bindings: Mapping[int, Any] | None = None,
                 alignment: int = 64,
                 helper_returns: Mapping[int, int] | None = None) -> dict[str, Any]:
    """CacheRegion v1 (``tb_singleton``) for one imported unit.

    ``site_bindings`` maps a site's ``field_off`` to its
    :class:`scopt.importer.classify.AddrClass` (for module/symbol in
    relocation targets). ``helper_returns`` maps a HELPER_CALL site's index in
    ``u.sites`` to its return offset as the recovery oracle computed it (the
    end of the decoded call instruction); without an entry the offset is
    derived from the call encoding (rel32 5 bytes, pool-indirect 6).
    """
    site_bindings = site_bindings or {}
    helper_returns = helper_returns or {}
    body = u.code + u.search
    rec_points = {p.host_return_offset: p.id for p in u.recovery}
    events: list[dict[str, Any]] = []
    ldmarks = [m for m in u.opmarks if m.kind in (OpMarkKind.QEMU_LD, OpMarkKind.QEMU_ST)]
    for d in u.ldst:
        m = ldmarks[d.op_ordinal] if 0 <= d.op_ordinal < len(ldmarks) else None
        events.append({"host_offset": m.host_off if m else d.slow_start,
                       "kind": "guest_ld" if d.is_ld else "guest_st",
                       "insn": None if m is None or m.insn_idx == 0xFFFF else m.insn_idx,
                       "faultable": True, "recovery": rec_points.get(d.raddr_off)})
    for si, s in enumerate(u.sites):
        if s.kind != SiteKind.HELPER_CALL:
            continue
        b = site_bindings.get(s.field_off)
        ret = helper_returns.get(si, s.insn_off + (6 if s.enc.name == "POOL_INDIRECT" else 5))
        events.append({"host_offset": s.insn_off,
                       "kind": "plugin_cb" if b is not None and b.cls == "ext.plugin"
                       else "helper_call",
                       "insn": s.insn_idx, "faultable": True, "recovery": rec_points.get(ret)})
    events.sort(key=lambda e: (e["host_offset"], e["kind"]))

    relocs = []
    sites = {s.field_off: s for s in u.sites}
    native_encodings = _native_relocation_encodings(u)
    for f in u.reloc.fixups:
        site = sites.get(f.field_off)
        encoding = (native_encodings.get(f.field_off) if f.target_class == "ext.native"
                    else site.enc if site is not None else None)
        if encoding is None:
            raise ValueError(f"fixup at {f.field_off:#x} has no captured or certified encoding")
        b = site_bindings.get(f.field_off)
        tgt: dict[str, Any] = {"class": f.target_class}
        if b is not None and b.module:
            tgt["module"] = b.module
        if b is not None and b.symbol:
            tgt["symbol"] = b.symbol
            if b.sym_off:
                tgt["offset"] = b.sym_off
        if f.target_abs is not None:
            tgt["addr"] = hexaddr(f.target_abs)
        relocs.append({"member": 0, "field_offset": f.field_off, "width": f.width,
                       "encoding": encoding.name.lower(), "site_kind": SiteKind(f.site_kind).name,
                       "target": tgt, "rule": f.rule})

    exits_at = unit_exits_at(u)
    exits = tuple(e for _, e in exits_at)
    gx = guarded_exit_rows(u) if getattr(u, "stub_start", None) is not None else {}
    unknowns = sorted({f"{e.kind.value} dest" for e in exits
                       if e.kind in (ExitKind.GOTO_PTR, ExitKind.EXIT_NULL)
                       and e.static_dest_vaddr is None})
    if u.meta_status != "ok":
        unknowns.append(f"meta:{u.meta_status}")
    b_ = bundle or {}
    doc: dict[str, Any] = {
        "schema": "fastdyn.cache_region", "version": 1, "kind": "tb_singleton",
        "bundle_id": b_.get("bundle_id", UNBOUND_ID),
        "contract_id": b_.get("contract_id", UNBOUND_ID),
        "runtime_identity_id": runtime_identity_id or UNBOUND_ID,
        "members": [{
            "unit": u.uid, "key": key_json(u),
            "guest": {"size": u.guest_size, "icount": u.icount, "hash": hexaddr(u.guest_hash),
                      "origin": u.origin, "vaddrs": [hexaddr(v) for v in sorted(set(u.vaddrs))]},
            "origin": {"snapshot_id": snapshot_id, "slot": u.slot,
                       "tb_offset": hexaddr(u.tb_offset)}}],
        "entries": [{"id": "e0", "kind": "general", "member": 0, "host_offset": 0,
                     "precondition": {"full_key": True, "extra_guards": []}}],
        "exits": [_exit_json(e, gx.get(at) if e.kind == ExitKind.GUARDED_SLOT else None)
                  for at, e in exits_at],
        "native": {"code_sha256": sha256_id(body), "code_len": u.tc_size,
                   "insn0_start": u.insn0_start, "body_end": u.body_end, "ldst_end": u.ldst_end,
                   "pool_start": u.pool_start,
                   "search_len": search_len if search_len is not None else len(u.search),
                   "end_offset_rel": u.end_offset - u.tc_offset,
                   "struct": {"offset_from_code": u.tb_offset - u.tc_offset, "size": tb_size},
                   "alignment": alignment, **({"stub_start": u.stub_start}
                                              if getattr(u, "stub_start", None) is not None
                                              else {})},
        "relocations": relocs,
        "maps": {"insn": [{"member": 0, "index": i.index, "words": [hexaddr(w) for w in i.words],
                           "host_start": i.host_start, "host_end": i.host_end}
                          for i in u.insns],
                 "events": events},
        "recovery": [{"id": p.id, "host_return_offset": p.host_return_offset, "member": 0,
                      "insn": p.insn,
                      "restore": {"pc_mode": "pcrel_page_offset",
                                  "words": [hexaddr(w) for w in p.words]},
                      "state_fixups": []} for p in u.recovery],
        "bindings": {
            "helpers": [{"id": h.id, "name": h.name, "module": h.module, "symbol": h.symbol,
                         "flags": hexaddr(h.flags)} for h in u.bindings.helpers],
            "unchecked_host_pointers": [{"class": p.cls, "anchor": p.anchor, "site": p.site}
                                        for p in u.bindings.unchecked_host_pointers]},
        "dependencies": {"guest_pages": _guest_pages(u, page_bits), "units": [u.uid],
                         "facts": [],
                         "patch_plan_hash": b_.get("patch_plan_hash", UNBOUND_ID),
                         "instrumentation_hash": b_.get("instrumentation_hash", UNBOUND_ID)},
        "status": "ok_relocatable" if u.reloc.relocatable else "pinned",
        "pin_reasons": list(u.reloc.reasons),
        "unknowns": unknowns,
        "provenance": {"passes": [dict(IMPORT_PASS)], "source_units": [u.uid]},
        "x_meta_status": u.meta_status,
    }
    doc["region_id"] = region_id(doc)
    return doc


def validate_region(doc: Mapping[str, Any]) -> list[str]:
    """Schema errors plus the region_id recomputation (empty: valid)."""
    errs = schemas.errors(dict(doc), "cache_region.v1")
    if doc.get("region_id") != region_id(doc):
        errs.append("region_id: does not match the canonical content")
    return errs


def regions_sorted(regions: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return sorted(regions, key=lambda r: r["members"][0]["origin"]["slot"])


# ---------------------------------------------------------------------------
# multi_tb rows (B3/B4 design §3.9; WP18)
# ---------------------------------------------------------------------------

def build_multi_tb_region(r_region: Mapping[str, Any], c_region: Mapping[str, Any], *,
                          slot_n: int, variant: Mapping[str, Any], r_orig_uid: str,
                          guard_atoms: Sequence[Mapping[str, Any]],
                          facts: Sequence[Mapping[str, Any]] = (),
                          passes: Sequence[Mapping[str, Any]] = (),
                          summary_row_hash: str | None = None,
                          envlayout_id: str | None = None,
                          runtime_exe_sha256: str | None = None) -> dict[str, Any]:
    """The ``multi_tb`` row of one (R, SPEC slot ``slot_n``, C') link.

    ``r_region`` is the tb_singleton row of the grown R (output snapshot),
    ``c_region`` the row of the general C. ``variant`` gives the variant as
    written: ``{"variant_id", "tb_offset", "tc_offset", "end_offset",
    "code_sha256", "snapshot_id"}``. Member 1 (C') copies C's key/guest
    fields, rows, events and recovery points (same host offsets; patches are
    same-length), C's relocations (``x_origin: variant_relocated``) and
    bindings (``member: 1``). ``facts`` are FactRef items.
    """
    rm = r_region["members"][0]
    cm = c_region["members"][0]
    m1 = {"unit": cm["unit"], "key": cm["key"], "guest": dict(cm["guest"]),
          "origin": {"snapshot_id": variant["snapshot_id"], "slot": None,
                     "tb_offset": hexaddr(variant["tb_offset"])},
          "x_variant_of": cm["unit"], "x_variant_id": variant["variant_id"]}
    exits = []
    for x in r_region["exits"]:
        exits.append({**x, "member": 0})
    for x in c_region["exits"]:
        exits.append({**x, "member": 1, "id": f"m1.{x['id']}"})
    c_native = dict(c_region["native"])
    c_native["code_sha256"] = variant["code_sha256"]
    nat = {"members": [dict(r_region["native"]), c_native]}
    relocs = [{**r, "member": 0} for r in r_region["relocations"]]
    relocs += [{**r, "member": 1, "x_origin": "variant_relocated"}
               for r in c_region["relocations"]]
    ins = [{**i, "member": 0} for i in r_region["maps"]["insn"]]
    ins += [{**i, "member": 1} for i in c_region["maps"]["insn"]]
    ev = [{**e, "x_member": 0} for e in r_region["maps"]["events"]]
    ev += [{**e, "x_member": 1} for e in c_region["maps"]["events"]]
    rec = [{**p, "member": 0} for p in r_region["recovery"]]
    rec += [{**p, "member": 1, "id": f"m1.{p['id']}"} for p in c_region["recovery"]]
    rb, cb = r_region["bindings"], c_region["bindings"]
    helpers = {h["id"]: h for h in list(rb["helpers"]) + list(cb["helpers"])}
    unchecked = [{**p, "x_member": 0} for p in rb["unchecked_host_pointers"]]
    unchecked += [{**p, "x_member": 1} for p in cb["unchecked_host_pointers"]]
    deps = r_region["dependencies"]
    pages = list(deps["guest_pages"])
    for gp in c_region["dependencies"]["guest_pages"]:
        if gp not in pages:
            pages.append(gp)
    entry1 = {"id": "e1", "kind": "specialized", "member": 1, "host_offset": 0,
              "precondition": {"full_key": False, "extra_guards": [dict(a) for a in guard_atoms],
                               "facts": [dict(f) for f in facts]},
              "x_pred": {"member": 0, "slot": slot_n}}
    unknowns = sorted(set(r_region.get("unknowns", [])) | set(c_region.get("unknowns", [])))
    doc: dict[str, Any] = {
        "schema": "fastdyn.cache_region", "version": 1, "kind": "multi_tb",
        "bundle_id": r_region["bundle_id"], "contract_id": r_region["contract_id"],
        "runtime_identity_id": r_region["runtime_identity_id"],
        "members": [dict(rm), m1],
        "entries": [{"id": "e0", "kind": "general", "member": 0, "host_offset": 0,
                     "precondition": {"full_key": True, "extra_guards": []}}, entry1],
        "exits": exits, "native": nat, "relocations": relocs,
        "maps": {"insn": ins, "events": ev}, "recovery": rec,
        "bindings": {"helpers": [helpers[k] for k in sorted(helpers)],
                     "unchecked_host_pointers": unchecked},
        "dependencies": {"guest_pages": pages,
                         "units": [rm["unit"], r_orig_uid, cm["unit"]],
                         "facts": [dict(f) for f in facts],
                         "patch_plan_hash": deps["patch_plan_hash"],
                         "instrumentation_hash": deps["instrumentation_hash"],
                         **({"x_summary_row_hash": summary_row_hash} if summary_row_hash
                            else {}),
                         **({"x_envlayout_id": envlayout_id} if envlayout_id else {}),
                         **({"x_runtime_exe_sha256": runtime_exe_sha256}
                            if runtime_exe_sha256 else {})},
        "status": "ok_relocatable", "pin_reasons": [],
        "unknowns": unknowns,
        "provenance": {"passes": [dict(p) for p in passes] or
                       [{"name": "specialize-returns", "version": 1}],
                       "source_units": [r_orig_uid, cm["unit"]]},
        "x_slot": slot_n,
    }
    doc["region_id"] = region_id(doc)
    return doc
