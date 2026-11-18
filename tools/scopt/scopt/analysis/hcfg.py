"""``hcfg@1``: host control-flow graph of one unit over its HostOps (design §7.3, §9.2). WP17.

Nodes are HostOps indices. Edges come only from captured metadata (the
HostOps ``target`` of a LABEL/LDST site), fall-through and call returns:

* ``fall``      next instruction (not after an unconditional jmp, an exit, or a noreturn call)
* ``label``     LABEL_JMP / LABEL_JCC target (``entry_check`` for the prefix ``jl`` to the
                exit-request stub)
* ``ldst_slow`` LDST_FAST_JCC -> slow-path start (full view only)
* ``ldst_ret``  LDST_RET_JMP -> raddr (full view only)
* ``logreg_spin`` the ring-full ``je`` of a matched coverage LOG_REG block back to the
                block start (full view only; close-out, design/closeout-logreg.md §A.4)

The spin loop body has only loads, so one iteration covers every exit state
(``hostsym`` models it so); dropping its back edge from the atomic view makes a
unit whose only cycle is a matched spin acyclic for path enumeration. Any
other backward edge stays a back edge (``cyclic``). The spin edges are listed
in ``spin_edges``. A HostOps block whose ops are not the contiguous 15 ops of
the form, or whose ``je`` does not target the block start, makes the graph
not ``ok`` (``logreg_block_malformed``).

Two views (``atomic``):

* **atomic** (default; what B4's dataflow and ``hostsym`` path enumeration
  use): an LDST block is one straight-line segment — the fast-path ``jne`` to
  the slow path is not an edge and the slow path is not part of the graph, so
  the GUEST_ACCESS op stands for the whole access (S5). Calls are single ops.
* **full**: the real machine graph including slow paths (used by ``hlive``).

Exits (``exits``: op index -> kind)::

    goto_tb:<n>        the goto_tb jmp (chained or not; a direct-chain exit)
    exit_tb:<idx>      jmp tb_ret after lea rax,[tb+idx] (idx 3 = exit_requested)
    exit_null          jmp to the epilogue (exit_tb(NULL))
    goto_ptr           jmp reg after lookup_tb_ptr
    noreturn           call of a TCG_CALL_NO_RETURN helper
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from .api import AnalysisSpec, ScopeRef
from .hostops import OpKind, UnitHostOps

__all__ = ["HCfg", "build_hcfg", "exit_class", "HCFG", "ANALYSES", "TCG_CALL_NO_RETURN"]

TCG_CALL_NO_RETURN = 0x0008


def exit_class(kind: str) -> str:
    """``goto_tb:1`` -> ``goto_tb``; ``exit_tb:3`` -> ``exit_requested``; else the base kind."""
    base, _, arg = kind.partition(":")
    if base == "exit_tb":
        return "exit_requested" if arg == "3" else "exit_tb"
    return base


@dataclass(frozen=True)
class HCfg:
    uid: str
    ok: bool
    reasons: tuple[str, ...]
    n: int
    succ_atomic: tuple[tuple[int, ...], ...]
    succ_full: tuple[tuple[int, ...], ...]
    edge_kinds: dict[tuple[int, int], str]
    exits: dict[int, str]
    entry: int = 0
    back_edges: tuple[tuple[int, int], ...] = ()
    reachable_atomic: frozenset[int] = frozenset()
    spin_edges: tuple[tuple[int, int], ...] = ()

    def successors(self, i: int, *, atomic: bool = True) -> tuple[int, ...]:
        return (self.succ_atomic if atomic else self.succ_full)[i]

    def predecessors(self, i: int, *, atomic: bool = True) -> tuple[int, ...]:
        if getattr(self, "_pred", None) is None:
            pa: list[list[int]] = [[] for _ in range(self.n)]
            pf: list[list[int]] = [[] for _ in range(self.n)]
            for a in range(self.n):
                for b in self.succ_atomic[a]:
                    pa[b].append(a)
                for b in self.succ_full[a]:
                    pf[b].append(a)
            object.__setattr__(self, "_pred", (tuple(map(tuple, pa)), tuple(map(tuple, pf))))
        return self._pred[0 if atomic else 1][i]

    @property
    def cyclic(self) -> bool:
        return bool(self.back_edges)

    def exit_kind(self, i: int) -> str | None:
        return self.exits.get(i)

    def paths(self, *, max_paths: int = 256, atomic: bool = True) -> list[tuple[int, ...]] | None:
        """Every simple path from the entry to an exit (op indices), or None when
        more than ``max_paths`` exist (``path_budget_exhausted``) or the graph
        has a cycle reachable from the entry."""
        if self.n == 0:
            return []
        if self.cyclic:
            return None
        succ = self.succ_atomic if atomic else self.succ_full
        out: list[tuple[int, ...]] = []
        stack: list[tuple[int, tuple[int, ...]]] = [(self.entry, (self.entry,))]
        while stack:
            i, p = stack.pop()
            nxt = succ[i]
            if not nxt:
                out.append(p)
                if len(out) > max_paths:
                    return None
                continue
            # straight-line runs without extra stack frames
            while len(nxt) == 1 and nxt[0] not in self.exits:
                j = nxt[0]
                p = p + (j,)
                nxt = succ[j]
                if not nxt:
                    break
            if not nxt:
                out.append(p)
                if len(out) > max_paths:
                    return None
                continue
            for j in reversed(nxt):
                stack.append((j, p + (j,)))
        return out

    def iter_edges(self, *, atomic: bool = True) -> Iterator[tuple[int, int, str]]:
        succ = self.succ_atomic if atomic else self.succ_full
        for a in range(self.n):
            for b in succ[a]:
                yield a, b, self.edge_kinds.get((a, b), "fall")

    def content_digest(self) -> str:
        import hashlib
        import json

        return "sha256:" + hashlib.sha256(json.dumps(
            [self.uid, self.ok, list(self.reasons), [list(s) for s in self.succ_atomic],
             [list(s) for s in self.succ_full], sorted(self.exits.items())]).encode()).hexdigest()


def build_hcfg(h: UnitHostOps) -> HCfg:
    ops = h.ops
    n = len(ops)
    reasons: list[str] = []
    idx = {o.off: i for i, o in enumerate(ops)}
    full: list[list[int]] = [[] for _ in range(n)]
    atom: list[list[int]] = [[] for _ in range(n)]
    kinds: dict[tuple[int, int], str] = {}
    exits: dict[int, str] = {}
    spin: list[tuple[int, int]] = []
    reasons.extend(_logreg_malformed(h, idx))
    for i, o in enumerate(ops):
        nxt = i + 1 if i + 1 < n else None

        def add(j: int | None, kind: str, *, a: bool = True, f: bool = True) -> None:
            if j is None:
                reasons.append(f"falls_off@{o.off:#x}")
                return
            if f:
                full[i].append(j)
            if a:
                atom[i].append(j)
            kinds[(i, j)] = kind

        if o.kind == OpKind.EXIT_JMP:
            if "EXIT_TB_EPI_JMP" in o.sites:
                exits[i] = "exit_null"
            else:
                # the MOVI_EXIT_TB_PTR lea right before it carries idx (its site aux)
                exits[i] = f"exit_tb:{_movi_idx(h, i)}"
            continue
        if o.kind == OpKind.GOTO_PTR:
            exits[i] = "goto_ptr"
            continue
        if o.kind == OpKind.JMP and "GOTO_TB" in o.sites:
            exits[i] = f"goto_tb:{_goto_slot(h, i)}"
            continue
        if o.kind == OpKind.CALL:
            if o.helper_flags is not None and o.helper_flags & TCG_CALL_NO_RETURN:
                exits[i] = "noreturn"
                continue
            add(nxt, "fall")
            continue
        if o.kind in (OpKind.JMP, OpKind.JCC):
            tgt = idx.get(o.target) if o.target is not None else None
            if o.target is not None and tgt is None:
                reasons.append(f"branch_target_not_boundary@{o.off:#x}")
            if "LDST_FAST_JCC" in o.sites:
                add(tgt, "ldst_slow", a=False)
                add(nxt, "fall")
                continue
            if "LDST_RET_JMP" in o.sites:
                add(tgt, "ldst_ret", a=False)
                continue
            if o.spin and o.kind == OpKind.JCC:
                add(nxt, "fall")
                if tgt is not None:
                    add(tgt, "logreg_spin", a=False)
                    spin.append((i, tgt))
                continue
            k = "entry_check" if o.block == "prefix" else "label"
            if o.kind == OpKind.JCC:
                add(nxt, "fall")
            if tgt is not None:
                add(tgt, k)
            continue
        if o.block == "ldst_slow" or o.block == "pad":
            # slow paths are only in the full view; pads are unreachable
            if o.block == "ldst_slow":
                add(nxt, "fall", a=False)
            continue
        add(nxt, "fall")
    # atomic view: drop slow-path ops from every list (they are reached only via ldst_slow)
    slow = {i for i, o in enumerate(ops) if o.block in ("ldst_slow", "pad")}
    atom = [[j for j in s if j not in slow] if i not in slow else [] for i, s in enumerate(atom)]
    # reachability + back edges (iterative DFS on the atomic view)
    reach: set[int] = set()
    back: list[tuple[int, int]] = []
    if n:
        color = [0] * n
        stack: list[tuple[int, int]] = [(0, 0)]
        color[0] = 1
        while stack:
            i, k = stack[-1]
            if k < len(atom[i]):
                stack[-1] = (i, k + 1)
                j = atom[i][k]
                if color[j] == 0:
                    color[j] = 1
                    stack.append((j, 0))
                elif color[j] == 1:
                    back.append((i, j))
            else:
                color[i] = 2
                reach.add(i)
                stack.pop()
    ok = h.ok and not reasons
    return HCfg(h.uid, ok, tuple(h.reasons) + tuple(dict.fromkeys(reasons)), n,
                tuple(map(tuple, atom)), tuple(map(tuple, full)), kinds, exits, 0,
                tuple(back), frozenset(reach), tuple(spin))


def _logreg_malformed(h: UnitHostOps, idx: dict[int, int]) -> list[str]:
    """Defensive check of the HostOps LOG_REG marking (the matcher guarantees it)."""
    from ..decode.logreg import INSNS

    out: list[str] = []
    blocks = getattr(h, "log_blocks", ())
    for k, lb in enumerate(blocks):
        want = [lb.start + o for o, _n, _t in INSNS]
        got = [i for i, o in enumerate(h.ops) if o.log == k]
        if [h.ops[i].off for i in got] != want or \
                (got and got != list(range(got[0], got[0] + len(want)))):
            out.append(f"logreg_block_malformed@{lb.start:#x}")
            continue
        spins = [i for i in got if h.ops[i].spin]
        je = idx.get(lb.spin_jcc)
        if spins != [je] or h.ops[je].kind != OpKind.JCC or h.ops[je].target != lb.start:
            out.append(f"logreg_block_malformed@{lb.start:#x}")
    marked = {i for i, o in enumerate(h.ops) if o.log is not None or o.spin}
    owned = {i for i, o in enumerate(h.ops) if o.log is not None and o.log < len(blocks)}
    if marked - owned or any(o.spin and o.log is None for o in h.ops):
        out.append("logreg_block_malformed")
    return out


def _movi_idx(h: UnitHostOps, i: int) -> int:
    # The exit stub is "lea rax,[rip+..] (MOVI_EXIT_TB_PTR aux=idx); jmp tb_ret".
    for j in range(i - 1, max(-1, i - 3), -1):
        a = h.ops[j].site_aux("MOVI_EXIT_TB_PTR")
        if a is not None:
            return a
    return -1


def _goto_slot(h: UnitHostOps, i: int) -> int:
    a = h.ops[i].site_aux("GOTO_TB")
    return -1 if a is None else a


def _compute(ctx: Any, scope: ScopeRef) -> HCfg:
    h = ctx.analysis("hostops", scope).value
    return build_hcfg(h)


HCFG = AnalysisSpec(name="hcfg", version=1, scope="unit", requires=(("hostops", 1),),
                    inputs=frozenset(), compute=_compute, result_schema=HCfg)
ANALYSES = (HCFG,)
