"""Immutable program view of capture-derived edges and full translation keys."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from ..graph import static_edges
from .api import AnalysisSpec


@dataclass(frozen=True, slots=True)
class ProgramUnit:
    uid: str
    content_hash: str
    key: tuple[int, int | None, int, int, int]
    vaddrs: tuple[int, ...]
    opaque: bool


@dataclass(frozen=True, slots=True)
class ProgramEdge:
    source: str
    exit_id: str
    kind: str
    slot: int | None
    site_off: int | None
    target_vaddr: int | None
    candidates: tuple[str, ...]
    dynamic: bool
    # Candidate sets say where a transfer could go. They do not prove key
    # transitions or preservation across the dispatcher.
    fallback: bool


@dataclass(frozen=True, slots=True)
class Program:
    snapshot_id: str
    units: tuple[ProgramUnit, ...]
    edges: tuple[ProgramEdge, ...]

    def reachable(self, roots: frozenset[str], covered: frozenset[str]) -> frozenset[str]:
        """Conservative reachability within the declared domain.

        An unresolved exit from reachable code can reenter any covered unit.
        Units outside the covered domain retain independent entry semantics.
        """
        by_source: dict[str, list[ProgramEdge]] = defaultdict(list)
        for edge in self.edges:
            by_source[edge.source].append(edge)
        seen = set(roots | ({u.uid for u in self.units} - covered))
        work = list(sorted(seen))
        while work:
            for edge in by_source[work.pop()]:
                if edge.dynamic:
                    seen.update(covered)
                    return frozenset(seen)
                for dest in edge.candidates:
                    if dest not in seen:
                        seen.add(dest)
                        work.append(dest)
        return frozenset(seen)


def build_program(snap: Any) -> Program:
    captured = snap.units()
    by_vaddr: dict[int, list[str]] = defaultdict(list)
    for unit in captured:
        for vaddr in set(unit.vaddrs):
            by_vaddr[vaddr].append(unit.uid)
    units = []
    edges = []
    for unit in captured:
        key = unit.key
        rows = static_edges(snap, unit, snap.header.target_page_bits)
        opaque = unit.meta_status != "ok" or not rows
        units.append(ProgramUnit(unit.uid, unit.content_hash,
                                 (key.page_addr0, key.page_addr1, key.flags, key.cs_base, key.cflags),
                                 tuple(sorted(set(unit.vaddrs))), opaque))
        if opaque:
            edges.append(ProgramEdge(unit.uid, "opaque", "opaque", None, None, None, (),
                                     dynamic=True, fallback=True))
        for row in rows:
            desc = row["exit"]
            kind = desc["kind"]
            vaddr = int(row["dest_vaddr"], 16) if "dest_vaddr" in row else None
            options = set(row["candidates"])
            if kind == "goto_ptr" and vaddr is not None:
                options.update(by_vaddr.get(vaddr, ()))
            # Even a static branch can exit to the dispatcher if no compatible
            # key is present. Scope outside the available graph conservatively.
            dynamic = (not options or vaddr is None or kind == "guarded_slot")
            edges.append(ProgramEdge(unit.uid, desc["id"], kind, desc.get("slot"),
                                     desc.get("site_off"), vaddr, tuple(sorted(options)),
                                     dynamic, fallback=True))
    return Program(snap.snapshot_id, tuple(units), tuple(edges))


def _compute(ctx: Any, _scope: Any) -> Program:
    # Unit content includes translation keys, correspondence and exit metadata.
    return build_program(ctx.snapshot)


PROGRAM = AnalysisSpec(
    name="program", version=1, scope="snapshot", requires=(),
    inputs=frozenset({"code", "meta"}), compute=_compute, result_schema=Program,
)
ANALYSES = (PROGRAM,)
