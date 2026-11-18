"""``graph.json``: the snapshot's known control-flow edges (design §4.4, B1 gate C2). WP7.

Export (:func:`export_graph`) never guesses:

* **Nodes** are the snapshot's UnitIds (slot order) with their observed
  ``vaddrs``, slot, ``tb_offset`` and import status.
* **Edges** come only from a captured exit site (CacheRegion exits, see
  :func:`scopt.formats.region_v1.unit_exits`) or from a profile observation.
  * Static ``goto_tb`` edges carry ``dest_vaddr`` (StaticExit),
    ``dest_ram = (page_addr0 & ~page_mask) | (dest_vaddr & page_mask)`` (valid
    because goto_tb is same-page only, G16) and ``candidates``: the snapshot
    units with ``page_addr0 == dest_ram`` whose ``cflags`` equal the source's
    cflags without the one-shot bits (:data:`ONE_SHOT_CFLAGS`): an io-recompile
    TB (CF_NOIRQ | CF_MEMI_ONLY | count 1) chains to an ordinary TB.
    ``resolution`` is ``candidates_unique``, ``candidates_multiple`` or
    ``not_in_snapshot``. A static edge **never** has a ``dst``: flags and
    cs_base (condexec) at the destination are runtime state.
  * ``goto_ptr``, ``exit_null`` and ``exit_requested`` edges have
    ``resolution: dynamic`` (an off-page goto_ptr fallback keeps its static
    ``dest_vaddr``; it still has no ``dest_ram``: the other page's ram address
    is unknown offline).
  * Profile edges (Profile v1 ``edges``) add ``observed`` rows with the exact
    ``dst`` UnitId, count, status and ``profile_id``. ``chain0``/``chain1`` attach
    to the goto_tb edge of that slot; every other kind (or a chain edge whose
    slot has no captured goto_tb) becomes its own edge with
    ``exit.kind = "observed"`` and ``resolution: observed``.
* Units without capture metadata contribute nodes but no static edges.

The file is canonical JSON plus a newline; ``export → import → export`` is
byte-identical (:func:`roundtrip_ok`). :func:`check_graph` validates a graph
against its snapshot (``scopt graph import --check``).
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .canon import canonical_json, hexaddr, parse_hex
from .formats.enums import ExitKind
from .formats.region_v1 import unit_exits_at
from .ids import is_unit_id

__all__ = [
    "GRAPH_SCHEMA", "RESOLUTIONS", "GraphError", "export_graph", "graph_bytes", "load_graph",
    "validate_graph", "check_graph", "roundtrip_ok", "static_edges", "lookup_cflags",
    "ONE_SHOT_CFLAGS", "b3_edges", "variant_edges",
]

GRAPH_SCHEMA = "scopt.graph"
RESOLUTIONS = ("candidates_unique", "candidates_multiple", "not_in_snapshot", "dynamic",
               "observed", "variant")
_CHAIN_SLOT = {"chain0": 0, "chain1": 1}


class GraphError(ValueError):
    pass


#: cflags bits that only a one-shot TB carries (``cflags_next_tb``): the insn
#: count, CF_MEMI_ONLY and CF_NOIRQ (cpu_io_recompile, translate-all.c:609;
#: watchpoints, tb-maint.c:1100). The *destination* of a chained goto_tb is
#: looked up with ``curr_cflags`` (cpu-exec.c:935-953), i.e. without them.
CF_COUNT_MASK = 0x000001FF
CF_MEMI_ONLY = 0x00001000
CF_NOIRQ = 0x00010000
ONE_SHOT_CFLAGS = CF_COUNT_MASK | CF_MEMI_ONLY | CF_NOIRQ


def lookup_cflags(src_cflags: int) -> int:
    """The cflags a goto_tb destination of a TB with ``src_cflags`` is looked up with."""
    return src_cflags & ~ONE_SHOT_CFLAGS


def _candidates(snap: Any, dest_ram: int, src_cflags: int) -> list[str]:
    want = lookup_cflags(src_cflags)
    return [u.uid for u in snap.units() if u.key.page_addr0 == dest_ram and u.key.cflags == want]


def static_edges(snap: Any, u: Any, page_bits: int) -> list[dict[str, Any]]:
    """The capture-derived edges of unit ``u`` (no profile data)."""
    if u.meta_status == "no_meta":
        return []
    mask = (1 << page_bits) - 1
    out = []
    gx = None
    for at, e in unit_exits_at(u):
        ex: dict[str, Any] = {"id": e.id, "kind": e.kind.value, "insn": e.insn}
        edge: dict[str, Any] = {"src": u.uid, "exit": ex}
        if e.kind == ExitKind.GUARDED_SLOT:
            # B3 (WP18): a guard stub slot. The target is the guard's absolute pc
            # compare; candidates are the units at that vaddr that the lookup could
            # return (same flags/cflags, one page). The guard decides at run time.
            if gx is None:
                from .formats.region_v1 import guarded_exit_rows

                gx = guarded_exit_rows(u)
            x = gx.get(at, {})
            ex.update(slot=e.slot, slot_kind=x.get("slot_kind"), site_off=at,
                      registered=bool(x.get("x_registered", True)))
            want = lookup_cflags(u.key.cflags)
            c = [w.uid for w in snap.units() if e.static_dest_vaddr in w.vaddrs
                 and w.key.cflags == want and w.key.flags == u.key.flags
                 and w.key.page_addr1 is None]
            edge.update(dest_vaddr=hexaddr(e.static_dest_vaddr), candidates=c,
                        resolution=("not_in_snapshot" if not c else
                                    "candidates_unique" if len(c) == 1 else
                                    "candidates_multiple"))
        elif e.kind == ExitKind.GOTO_TB:
            ex["slot"] = e.slot
            if e.static_dest_vaddr is None:
                edge.update(candidates=[], resolution="dynamic")
            else:
                dest_ram = (u.key.page_addr0 & ~mask) | (e.static_dest_vaddr & mask)
                c = _candidates(snap, dest_ram, u.key.cflags)
                edge.update(dest_vaddr=hexaddr(e.static_dest_vaddr), dest_ram=hexaddr(dest_ram),
                            candidates=c,
                            resolution=("not_in_snapshot" if not c else
                                        "candidates_unique" if len(c) == 1 else
                                        "candidates_multiple"))
        else:
            ex["site_off"] = at
            if e.static_dest_vaddr is not None:
                edge["dest_vaddr"] = hexaddr(e.static_dest_vaddr)
            edge.update(candidates=[], resolution="dynamic")
        edge["observed"] = []
        out.append(edge)
    return out


def _observed(edge: Mapping[str, Any], prof_id: str) -> dict[str, Any]:
    o = {"dst": edge.get("dst"), "kind": edge["kind"], "count": edge.get("count", 0),
         "status": edge.get("status", "unknown"), "phase": edge.get("phase"),
         "profile_id": prof_id}
    if edge.get("flags"):
        o["flags"] = sorted(edge["flags"])
    return o


def variant_edges(variants: Sequence[Mapping[str, Any]], selected: set[str],
                  exits_by_src: Mapping[str, Sequence[Mapping[str, Any]]]
                  ) -> list[dict[str, Any]]:
    """``variant_link`` edges (B3, WP18): one per re-imported variant row
    (:func:`scopt.importer.import_cache.import_variants`): R's SPEC slot to the
    variant C' of the general C. The edge never names C' as a node (a variant
    is not a record): ``variant`` is its variant id, ``general`` the C unit."""
    out = []
    for v in variants:
        src = v.get("pred_unit")
        if src not in selected or not v.get("ok", False):
            continue
        gx = next((e["exit"]["id"] for e in exits_by_src.get(src, ())
                   if e["exit"]["kind"] == "guarded_slot" and e["exit"].get("slot") ==
                   v.get("slot_n")), None)
        out.append({"src": src, "exit": {"kind": "variant_link", "slot": v.get("slot_n"),
                                         "guarded_exit": gx},
                    "variant": v["variant_key"], "general": v["general_unit"],
                    "variant_tb_offset": v.get("tb_offset"),
                    "candidates": [], "observed": [], "resolution": "variant"})
    return out


def export_graph(snap: Any, *, units: Iterable[str] | None = None,
                 profiles: Sequence[Mapping[str, Any]] = (),
                 variants: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """The graph document of ``snap`` (or of the ``units`` subset of it).
    ``variants`` (v6 artifacts) adds ``variant_link`` edges."""
    page_bits = snap.header.target_page_bits
    all_units = snap.units()
    if units is not None:
        want = set(units)
        missing = sorted(want - {u.uid for u in all_units})
        if missing:
            raise GraphError(f"units not in the snapshot: {missing[:4]}")
        sel = [u for u in all_units if u.uid in want]
    else:
        sel = list(all_units)
    status = getattr(snap, "status", None)
    nodes = [{"unit": u.uid, "slot": u.slot, "tb_offset": hexaddr(u.tb_offset),
              "vaddrs": [hexaddr(v) for v in sorted(set(u.vaddrs))],
              "status": status(u.uid) if status else
              ("ok_relocatable" if u.reloc.relocatable else "pinned")} for u in sel]
    edges: list[dict[str, Any]] = []
    by_src: dict[str, list[dict[str, Any]]] = {}
    for u in sel:
        es = static_edges(snap, u, page_bits)
        by_src[u.uid] = es
        edges.extend(es)
    selected = {u.uid for u in sel}
    edges.extend(variant_edges(variants, selected, by_src))
    prof_ids = []
    extra: dict[tuple[str, str], dict[str, Any]] = {}
    for p in profiles:
        pid = p.get("profile_id")
        if p.get("snapshot_id") not in (None, snap.snapshot_id):
            raise GraphError(f"profile {pid} is for snapshot {p.get('snapshot_id')}")
        prof_ids.append(pid)
        for pe in p.get("edges", ()):
            src = pe.get("src")
            if src not in selected:
                continue
            obs = _observed(pe, pid)
            slot = _CHAIN_SLOT.get(pe.get("kind"))
            tgt = None
            if slot is not None:
                tgt = next((e for e in by_src.get(src, ()) if e["exit"]["kind"] == "goto_tb"
                            and e["exit"].get("slot") == slot), None)
            if tgt is None:
                k = (src, pe["kind"])
                tgt = extra.get(k)
                if tgt is None:
                    tgt = {"src": src, "exit": {"kind": "observed", "edge_kind": pe["kind"]},
                           "candidates": [], "observed": [], "resolution": "observed"}
                    extra[k] = tgt
                    edges.append(tgt)
            tgt["observed"].append(obs)
    slot_of = {u.uid: u.slot for u in all_units}
    for e in edges:
        e["observed"].sort(key=lambda o: (o["profile_id"] or "", str(o["phase"]), o["kind"],
                                          o["dst"] or "", o["count"]))
    edges.sort(key=lambda e: (slot_of[e["src"]], _exit_order(e["exit"].get("id")),
                              e["exit"].get("edge_kind", ""), e.get("variant", "")))
    doc = {"schema": GRAPH_SCHEMA, "version": 1, "snapshot_id": snap.snapshot_id,
           "target_page_bits": page_bits,
           "scope": "snapshot" if units is None else "subset",
           "profiles": sorted(p for p in prof_ids if p),
           "nodes": nodes, "edges": edges}
    return doc


def _exit_order(xid: str | None) -> tuple[int, int, str]:
    """Numeric order of region exit ids (``x2`` before ``x10``); observed-only
    edges (no exit id) after every exit of their source."""
    if xid is None:
        return (2, 0, "")
    if xid[:1] == "x" and xid[1:].isdigit():
        return (0, int(xid[1:]), "")
    return (1, 0, xid)


def graph_bytes(doc: Mapping[str, Any]) -> bytes:
    return canonical_json(doc) + b"\n"


def validate_graph(doc: Mapping[str, Any]) -> list[str]:
    """Structural rules of §4.4 (no snapshot needed)."""
    errs: list[str] = []
    if doc.get("schema") != GRAPH_SCHEMA or doc.get("version") != 1:
        return [f"not a {GRAPH_SCHEMA} v1 document"]
    allowed = {"schema", "version", "snapshot_id", "target_page_bits", "scope", "profiles",
               "nodes", "edges"}
    errs += [f"unknown key {k}" for k in sorted(set(doc) - allowed) if not k.startswith("x_")]
    nodes = doc.get("nodes", [])
    ids = [n.get("unit") for n in nodes]
    if len(set(ids)) != len(ids):
        errs.append("duplicate node")
    errs += [f"node {u!r}: not a UnitId" for u in ids if not is_unit_id(u)]
    node_set = set(ids)
    for i, e in enumerate(doc.get("edges", [])):
        w = f"edge {i}"
        if e.get("src") not in node_set:
            errs.append(f"{w}: src {e.get('src')} is not a node")
        ex = e.get("exit") or {}
        res = e.get("resolution")
        if res not in RESOLUTIONS:
            errs.append(f"{w}: resolution {res!r}")
        if "dst" in e:
            errs.append(f"{w}: an edge never asserts dst (only observed rows do)")
        obs = e.get("observed", [])
        if ex.get("kind") == "observed":
            if not obs:
                errs.append(f"{w}: observed edge without a profile observation")
        elif ex.get("kind") == "guarded_slot":
            if "id" not in ex or "dest_vaddr" not in e or "dest_ram" in e:
                errs.append(f"{w}: guarded_slot needs an exit id and dest_vaddr, no dest_ram")
            c = e.get("candidates", [])
            want = ("not_in_snapshot" if not c else "candidates_unique" if len(c) == 1
                    else "candidates_multiple")
            if res != want:
                errs.append(f"{w}: resolution {res} but {len(c)} candidates")
            if ex.get("slot_kind") not in ("GENERAL", "SPEC"):
                errs.append(f"{w}: slot_kind {ex.get('slot_kind')!r}")
        elif ex.get("kind") == "variant_link":
            if res != "variant":
                errs.append(f"{w}: variant_link must resolve to variant")
            if not is_unit_id(e.get("general")) or not str(e.get("variant", "")).startswith(
                    "sha256:"):
                errs.append(f"{w}: variant_link needs general (UnitId) and variant (sha256)")
        elif ex.get("kind") in ("goto_tb", "goto_ptr", "exit_null", "exit_requested"):
            if "id" not in ex:
                errs.append(f"{w}: exit without a captured exit id")
            if ex["kind"] == "goto_tb":
                if "dest_vaddr" in e and "dest_ram" not in e:
                    errs.append(f"{w}: goto_tb dest_vaddr without dest_ram")
                c = e.get("candidates", [])
                want = ("dynamic" if "dest_vaddr" not in e else "not_in_snapshot" if not c
                        else "candidates_unique" if len(c) == 1 else "candidates_multiple")
                if res != want:
                    errs.append(f"{w}: resolution {res} but {len(c)} candidates")
            elif res != "dynamic":
                errs.append(f"{w}: {ex['kind']} must be dynamic")
            if ex["kind"] != "goto_tb" and "dest_ram" in e:
                errs.append(f"{w}: {ex['kind']} with dest_ram")
        else:
            errs.append(f"{w}: exit kind {ex.get('kind')!r}")
        errs += [f"{w}: candidate {c!r} is not a UnitId" for c in e.get("candidates", [])
                 if not is_unit_id(c)]
        for o in obs:
            if not o.get("profile_id"):
                errs.append(f"{w}: observation without profile_id")
            if o.get("dst") is not None and not is_unit_id(o["dst"]):
                errs.append(f"{w}: observed dst {o.get('dst')!r}")
        for k in ("dest_vaddr", "dest_ram"):
            if k in e:
                try:
                    parse_hex(e[k])
                except ValueError:
                    errs.append(f"{w}: {k} not a 0x string")
    return errs


def load_graph(src: str | os.PathLike | bytes) -> dict[str, Any]:
    """Parse and validate a ``graph.json`` (raises :class:`GraphError`)."""
    data = src if isinstance(src, bytes) else Path(src).read_bytes()
    try:
        doc = json.loads(data)
    except ValueError as e:
        raise GraphError(f"not JSON: {e}") from e
    errs = validate_graph(doc)
    if errs:
        raise GraphError("; ".join(errs[:6]))
    return doc


def roundtrip_ok(data: bytes) -> bool:
    """``export → import → export`` byte identity for an exported file."""
    return graph_bytes(load_graph(data)) == data


def check_graph(doc: Mapping[str, Any], snap: Any,
                variants: Sequence[Mapping[str, Any]] = ()) -> list[str]:
    """Differences between ``doc`` and what ``snap`` gives (profile rows not re-derived);
    ``variants``: the re-imported variant rows of a v6 artifact (variant_link edges)."""
    errs = list(validate_graph(doc))
    if doc.get("snapshot_id") != snap.snapshot_id:
        return errs + [f"snapshot_id {doc.get('snapshot_id')} != {snap.snapshot_id}"]
    units = None if doc.get("scope") == "snapshot" else [n["unit"] for n in doc["nodes"]]
    try:
        ref = export_graph(snap, units=units, variants=variants)
    except GraphError as e:
        return errs + [str(e)]
    if doc["nodes"] != ref["nodes"]:
        errs.append("nodes differ from the snapshot")
    static = [{**e, "observed": []} for e in doc["edges"] if e["exit"]["kind"] != "observed"]
    if static != ref["edges"]:
        errs.append("static edges differ from the snapshot")
    known = set(snap.unit_ids()) if hasattr(snap, "unit_ids") else {u.uid for u in snap.units()}
    for e in doc["edges"]:
        for o in e.get("observed", []):
            if o.get("dst") is not None and o["dst"] not in known:
                errs.append(f"observed dst {o['dst']} not in the snapshot")
    return errs


def b3_edges(doc: Mapping[str, Any]) -> dict[str, list[tuple]]:
    """The B3 edges of a graph document: ``guarded_slot`` rows ``(src, slot,
    slot_kind, dest_vaddr, registered)`` and ``variant_link`` rows ``(src,
    slot, variant, general)``, sorted."""
    gs, vl = [], []
    for e in doc.get("edges", ()):
        ex = e.get("exit", {})
        if ex.get("kind") == "guarded_slot":
            gs.append((e["src"], ex.get("slot"), ex.get("slot_kind"), e.get("dest_vaddr"),
                       ex.get("registered", True)))
        elif ex.get("kind") == "variant_link":
            vl.append((e["src"], ex.get("slot"), e.get("variant"), e.get("general")))
    return {"guarded_slot": sorted(gs, key=repr), "variant_link": sorted(vl, key=repr)}
