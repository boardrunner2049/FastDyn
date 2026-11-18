"""``hlive@1``: host register and flag liveness per HostOp (design §3.5 DHO, §7.3). WP17.

Backward may-liveness over the **full** ``hcfg`` view (slow paths included,
so a value a slow path reads stays live across the fast-path ``jne``).

Uses/defs per op (64-bit register families ``rax`` … ``r15``; flags ``CF PF
AF ZF SF OF DF``):

* ordinary op: uses = capstone reads (partial writes count as reads, zero
  idioms do not read); defs = full-width writes (32-bit writes zero-extend);
  flags from capstone's eflags (TEST = use; MODIFY/RESET/SET/UNDEFINED = def);
* call: uses = the SysV argument registers ``rdi rsi rdx rcx r8 r9`` (all six:
  the capture does not record the helper typemask, so this over-approximates,
  which only keeps more values live), plus ``rbp rsp``; defs = the caller-saved
  registers and all flags (the return value is in ``rax``). This is the SysV ABI
  contract every helper obeys, so it does not depend on the helper's row in the
  helper effect table (:mod:`.helper_effects`): an env or host-memory effect is
  not a register effect, and a helper that faults never returns into the TB.
  It is therefore sound for every row and for fail-closed (unknown) helpers;
  DHO never removes an op with a memory operand, so env effects never reach it;
* exits: ``rbp`` and ``rsp`` are live after every exit; ``jmp tb_ret`` reads
  ``rax`` (the ``lea rax,[tb+idx]`` before it); ``jmp reg`` reads its
  register; nothing else (the epilogue restores callee-saved registers from
  the frame, flags are dead).

``dead_after(i)`` tells whether every register and flag op ``i`` defines is
dead right after it — the DHO criterion together with "no memory operand".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .api import AnalysisSpec, ScopeRef
from .hcfg import HCfg
from .hostops import FLAG_NAMES, OpKind, UnitHostOps, reg_family

__all__ = ["HLive", "build_hlive", "HLIVE", "ANALYSES", "ARG_REGS", "CALLER_SAVED"]

ARG_REGS = frozenset({"rdi", "rsi", "rdx", "rcx", "r8", "r9"})
CALLER_SAVED = frozenset({"rax", "rcx", "rdx", "rsi", "rdi", "r8", "r9", "r10", "r11"})
_ALWAYS = frozenset({"rbp", "rsp"})
_FLAGS = frozenset(FLAG_NAMES)


@dataclass(frozen=True)
class HLive:
    uid: str
    live_in: tuple[frozenset[str], ...]
    live_out: tuple[frozenset[str], ...]
    defs: tuple[frozenset[str], ...]
    uses: tuple[frozenset[str], ...]
    writes: tuple[frozenset[str], ...] = ()

    def dead_after(self, i: int) -> bool:
        """Every register/flag op ``i`` writes (fully or partially) is dead after it,
        and it writes something."""
        d = self.writes[i] if self.writes else self.defs[i]
        return bool(d) and not (d & self.live_out[i])

    def live_at(self, i: int) -> frozenset[str]:
        return self.live_in[i]

    def content_digest(self) -> str:
        import hashlib
        import json

        return "sha256:" + hashlib.sha256(json.dumps(
            [self.uid, [sorted(s) for s in self.live_in],
             [sorted(s) for s in self.live_out]]).encode()).hexdigest()


def _uses_defs(h: UnitHostOps, i: int, exit_kind: str | None) -> tuple[frozenset[str],
                                                                         frozenset[str]]:
    o = h.ops[i]
    if o.kind == OpKind.CALL:
        uses = ARG_REGS | _ALWAYS | (o.regs_read - {"rip"})
        return frozenset(uses), frozenset(CALLER_SAVED | _FLAGS)
    uses = set(o.regs_read) | set(o.flags_read)
    defs = set(o.regs_written) | set(o.flags_written)
    if o.kind == OpKind.EXIT_JMP and "EXIT_TB_RET_JMP" in o.sites:
        uses.add("rax")
    if exit_kind is not None:
        uses |= _ALWAYS
    uses.discard("rip")
    defs.discard("rip")
    if o.dst_reg and o.kind in (OpKind.CONST, OpKind.COPY):
        f = reg_family(o.dst_reg)
        if f and f[1] < 4:  # partial write keeps the rest of the register
            uses.add(f[0])
    return frozenset(uses), frozenset(defs - set(o.regs_partial))


def build_hlive(h: UnitHostOps, g: HCfg) -> HLive:
    n = len(h.ops)
    ud = [_uses_defs(h, i, g.exits.get(i)) for i in range(n)]
    uses = tuple(u for u, _ in ud)
    defs = tuple(d for _, d in ud)
    lin: list[frozenset[str]] = [frozenset()] * n
    lout: list[frozenset[str]] = [frozenset()] * n
    changed = True
    while changed:
        changed = False
        for i in range(n - 1, -1, -1):
            succ = g.successors(i, atomic=False)
            out = frozenset().union(*(lin[j] for j in succ)) if succ else frozenset()
            if i in g.exits:
                out = out | _ALWAYS
            inn = (out - defs[i]) | uses[i]
            if out != lout[i] or inn != lin[i]:
                lout[i], lin[i] = out, inn
                changed = True
    writes = tuple(defs[i] | (h.ops[i].regs_partial if h.ops[i].kind != OpKind.CALL
                              else frozenset()) for i in range(n))
    return HLive(h.uid, tuple(lin), tuple(lout), defs, uses, writes)


def _compute(ctx: Any, scope: ScopeRef) -> HLive:
    h = ctx.analysis("hostops", scope).value
    g = ctx.analysis("hcfg", scope).value
    return build_hlive(h, g)


HLIVE = AnalysisSpec(name="hlive", version=1, scope="unit",
                     requires=(("hostops", 1), ("hcfg", 1)), inputs=frozenset(),
                     compute=_compute, result_schema=HLive)
ANALYSES = (HLIVE,)
