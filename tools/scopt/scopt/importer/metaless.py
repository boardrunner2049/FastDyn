"""Meta-less reference classifier for a ``.sc`` (bring-up and regression only). WP2.

This classifies every native reference in every valid TB of a ``.sc``
*without* a ``.scm`` capture, using only the ``.sc`` itself, the QEMU binary
and the plugin. It is the reference the capture-based importer (WP7,
design §2.7) is compared against; it never decides relocatability for an
artifact. The heuristics (critic A2, plan WP2):

* **Pool start** = the lowest address hit by a RIP-relative memory operand
  (not LEA), found while sweeping (:func:`scopt.decode.x86.sweep_to_pool`).
  ``[tc, pool_start)`` is decoded; ``[pool_start, tc + tc_size)`` is data.
* **QEMU PIE base** = 0x555555554000 (``setarch -R``); helper addresses are
  ``base + nm value`` of ``build/qemu-system-arm``.
* **Plugin load base** = the page-aligned base that makes the most external
  direct-call targets land exactly on a plugin function symbol (there is no
  MAPS for the B0 run). A real ``/proc/<pid>/maps`` replaces both guesses.
* **Prologue offsets**: the prologue bytes are not in the file (holes). The
  ``tb_ret_addr`` offset is the jmp target that follows an own-struct
  ``lea rax`` (``tcg_out_exit_tb`` with ``a0 != 0``); ``tcg_code_gen_epilogue``
  is the target of the other prologue jmps (``exit_tb(0)``) and must equal
  ``tb_ret - 2`` (the ``xor eax, eax`` between them,
  tcg-target.c.inc:4740-4744).
* **goto_tb** = the ``jmp rel32`` whose field is at the record's
  ``jmp_insn_offset[n]``.

Classes follow design §2.7 step 6 (``intra``, ``own_struct+idx``,
``prologue.tb_ret``/``.epilogue``, ``ext.helper``/``.plugin``/``.lib``,
``ext.ptr_riprel``, ``pool``, ``arena.abs``, ``arena.other_tb``, ``goto_tb``,
``goto_ptr``, ``abs.*``) plus ``unknown`` (which would pin the TB).
"""

from __future__ import annotations

import os
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..canon import fnv1a64
from ..decode.search import SearchDecodeError, decode_search, is_pcrel
from ..decode.symbols import (PAGE_SIZE, SETARCH_R_PIE_BASE, ElfImage, Maps, Module, Symbolizer,
                              load_elf)
from ..decode.thumb import check_lengths, lengths_from_words
from ..decode.x86 import (Arena, DecodedRef, Insn, RefKind, bad_insns, extract_refs,
                          sweep_to_pool, tiles)
from ..formats.sc_v5 import ScFile, ScUnit, read_sc

__all__ = [
    "DEFAULT_QEMU", "DEFAULT_PLUGIN", "DEFAULT_FIRMWARE", "G17", "ClassifiedRef", "TBResult",
    "MetalessResult", "classify_sc", "infer_plugin_base", "infer_prologue", "g17_tallies",
    "format_g17",
]

FASTDYN_ROOT = os.environ.get("FASTDYN_ROOT", "/data/extra_copy/fastdyn")
DEFAULT_QEMU = os.path.join(FASTDYN_ROOT, "src/qemu/build/qemu-system-arm")
DEFAULT_PLUGIN = os.path.join(FASTDYN_ROOT, "src/fastdyn/build/libfastdyn.so")
DEFAULT_FIRMWARE = os.path.join(FASTDYN_ROOT,
                                "src/fastdyn/tests/sample_binaries/RTOS/RTOSDemo.axf")

#: design §0 G17 / critic A2, measured on runs/b0/cache/minimal.sc
G17: dict[str, Any] = {
    "helper_pool_calls": 394,
    "plugin_rel32_calls": 3,
    "ext_rip_lea": 3,
    "tb_ret_jmps": 316,
    "epilogue_jmps": 3,
    "own_struct_lea": 316,
    "own_struct_by_idx": {0: 105, 1: 50, 3: 161},
    "raddr_lea": 336,
    "goto_ptr": 52,
    "movabs": 0,
    "host_ptr_imm32": 0,
    "helper_names": 11,
    "bad": 0,
}

OWN_STRUCT_IDX = (0, 1, 3)  # TB_EXIT_IDX0, TB_EXIT_IDX1, TB_EXIT_REQUESTED
XOR_EAX_LEN = 2  # tcg_out_movi(EAX, 0) -> "31 c0" between epilogue and tb_ret_addr


@dataclass(frozen=True, slots=True)
class ClassifiedRef:
    """One decoded reference with its class.

    Offsets are relative to the TB's first code byte (``tc_offset``).
    ``binding`` classifies an absolute value that is *data* (a pool slot's
    contents or a RIP-LEA target): ``ext.helper``, ``abs.plugin_data`` etc.
    """

    slot: int
    insn_off: int
    field_off: int
    width: int
    kind: str
    cls: str
    target: int | None
    binding: str = ""
    symbol: str = ""
    detail: str = ""

    def to_json(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "insn_off": self.insn_off, "field_off": self.field_off, "width": self.width,
            "kind": self.kind, "class": self.cls,
            "target": None if self.target is None else f"0x{self.target:x}",
        }
        for k in ("binding", "symbol", "detail"):
            if getattr(self, k):
                d[k] = getattr(self, k)
        return d


@dataclass
class TBResult:
    slot: int
    tc_offset: int
    tc_size: int
    pool_start: int  # relative to tc
    n_insns: int
    bad: list[int] = field(default_factory=list)  # (bad) offsets relative to tc
    tiled: bool = True
    refs: list[ClassifiedRef] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    irq_check: bool = False
    guest: dict[str, Any] = field(default_factory=dict)

    @property
    def unknown(self) -> list[ClassifiedRef]:
        return [r for r in self.refs if r.cls == "unknown"]

    def to_json(self, *, refs: bool = True) -> dict[str, Any]:
        d: dict[str, Any] = {
            "slot": self.slot, "tc_offset": f"0x{self.tc_offset:x}", "tc_size": self.tc_size,
            "pool_start": self.pool_start, "insns": self.n_insns, "bad": self.bad,
            "tiled": self.tiled, "irq_check": self.irq_check, "problems": self.problems,
            "classes": dict(sorted(Counter(r.cls for r in self.refs).items())),
        }
        if self.guest:
            d["guest"] = self.guest
        if refs:
            d["refs"] = [r.to_json() for r in self.refs]
        return d


@dataclass
class MetalessResult:
    sc_path: str
    code_address: int
    prologue_end: int
    tb_ret_off: int | None
    epilogue_off: int | None
    prologue_notes: list[str]
    modules: list[dict[str, Any]]
    tbs: list[TBResult]
    skipped_slots: list[int]
    guest_summary: dict[str, Any] = field(default_factory=dict)

    # -- tallies ----------------------------------------------------------
    def refs(self) -> Iterable[ClassifiedRef]:
        for t in self.tbs:
            yield from t.refs

    def class_tallies(self) -> dict[str, int]:
        return dict(sorted(Counter(r.cls for r in self.refs()).items()))

    def kind_class_tallies(self) -> dict[str, int]:
        c = Counter(f"{r.kind}:{r.cls}" + (f"->{r.binding}" if r.binding else "")
                    for r in self.refs())
        return dict(sorted(c.items()))

    def helper_calls(self) -> Counter:
        return Counter(r.symbol for r in self.refs()
                       if r.kind == RefKind.RIP_CALL and r.cls == "pool"
                       and r.binding == "ext.helper")

    def g17(self) -> dict[str, Any]:
        return g17_tallies(self)

    def to_json(self, *, refs: bool = False) -> dict[str, Any]:
        g = self.g17()
        return {
            "sc": self.sc_path,
            "code_address": f"0x{self.code_address:x}",
            "prologue_end": f"0x{self.prologue_end:x}",
            "tb_ret_off": None if self.tb_ret_off is None else f"0x{self.tb_ret_off:x}",
            "epilogue_off": None if self.epilogue_off is None else f"0x{self.epilogue_off:x}",
            "prologue_notes": self.prologue_notes,
            "modules": self.modules,
            "tbs": len(self.tbs),
            "skipped_slots": self.skipped_slots,
            "g17": {k: ({str(i): n for i, n in v.items()} if isinstance(v, dict) else v)
                    for k, v in g.items()},
            "g17_line": format_g17(g),
            "g17_expected_line": format_g17(G17),
            "g17_match": g17_match(g),
            "helpers": dict(sorted(self.helper_calls().items())),
            "classes": self.class_tallies(),
            "kind_classes": self.kind_class_tallies(),
            "unknown": [dict(slot=r.slot, **r.to_json()) for r in self.refs()
                        if r.cls == "unknown"],
            "tbs_with_problems": [t.to_json(refs=False) for t in self.tbs if t.problems],
            "irq_check": sum(t.irq_check for t in self.tbs),
            "guest": self.guest_summary,
            **({"per_tb": [t.to_json() for t in self.tbs]} if refs else {}),
        }


# --------------------------------------------------------------------------
# G17 tallies
# --------------------------------------------------------------------------

def g17_tallies(res: MetalessResult) -> dict[str, Any]:
    refs = list(res.refs())
    own = Counter(int(r.cls.split("+")[1]) for r in refs if r.cls.startswith("own_struct+"))
    helpers = res.helper_calls()
    return {
        "helper_pool_calls": sum(helpers.values()),
        "plugin_rel32_calls": sum(1 for r in refs
                                  if r.kind == RefKind.CALL_REL32 and r.cls == "ext.plugin"),
        "ext_rip_lea": sum(1 for r in refs if r.kind == RefKind.RIP_LEA
                           and r.cls == "ext.ptr_riprel"),
        "tb_ret_jmps": sum(1 for r in refs if r.cls == "prologue.tb_ret"),
        "epilogue_jmps": sum(1 for r in refs if r.cls == "prologue.epilogue"),
        "own_struct_lea": sum(own.values()),
        "own_struct_by_idx": dict(sorted(own.items())),
        "raddr_lea": sum(1 for r in refs if r.kind == RefKind.RIP_LEA and r.cls == "intra"),
        "goto_ptr": sum(1 for r in refs if r.cls == "goto_ptr"),
        "movabs": sum(1 for r in refs if r.kind == RefKind.MOVABS),
        "host_ptr_imm32": sum(1 for r in refs if r.kind in (RefKind.IMM32, RefKind.DISP32)),
        "helper_names": len(helpers),
        "bad": sum(len(t.bad) for t in res.tbs),
    }


def format_g17(g: dict[str, Any]) -> str:
    own = g["own_struct_by_idx"]
    own_s = ", ".join(f"+{i}:{n}" for i, n in sorted(own.items()))
    return (f"{g['helper_pool_calls']} / {g['plugin_rel32_calls']} / {g['ext_rip_lea']} / "
            f"{g['tb_ret_jmps']} / {g['epilogue_jmps']} / {g['own_struct_lea']} ({own_s}) / "
            f"{g['raddr_lea']} / {g['goto_ptr']} / {g['movabs']}")


def g17_match(g: dict[str, Any]) -> bool:
    return all(g[k] == v for k, v in G17.items())


# --------------------------------------------------------------------------
# Inference helpers
# --------------------------------------------------------------------------

def infer_plugin_base(targets: Iterable[int], image: ElfImage) -> tuple[int | None, int, int]:
    """Page-aligned load base that makes the most ``targets`` land on a function start.

    Returns ``(base, explained, total)``; base is None when nothing is explained.
    """
    targets = sorted(set(targets))
    by_low: dict[int, list[int]] = {}
    for f in image.funcs():
        by_low.setdefault(f.value & (PAGE_SIZE - 1), []).append(f.value)
    votes: Counter = Counter()
    for t in targets:
        for v in by_low.get(t & (PAGE_SIZE - 1), ()):
            b = t - v
            if b > 0:
                votes[b] += 1
    if not votes:
        return None, 0, len(targets)
    (base, n), = votes.most_common(1)
    return base, n, len(targets)


def infer_prologue(jmps: Sequence[tuple[int, bool]], prologue_end: int,
                   tb_ret_off: int | None = None, epilogue_off: int | None = None
                   ) -> tuple[int | None, int | None, list[str]]:
    """Infer ``(tb_ret_off, epilogue_off, notes)`` from prologue jmp targets.

    ``jmps`` holds ``(target arena offset, preceded by own-struct lea rax)``.
    """
    notes: list[str] = []
    after_lea = Counter(t for t, lea in jmps if lea)
    bare = Counter(t for t, lea in jmps if not lea)
    if tb_ret_off is None and after_lea:
        tb_ret_off = after_lea.most_common(1)[0][0]
        notes.append(f"tb_ret_off 0x{tb_ret_off:x} inferred from {after_lea[tb_ret_off]} "
                     f"jmps after an own-struct lea rax")
    if epilogue_off is None and bare:
        cands = [t for t in bare if t != tb_ret_off]
        if cands:
            epilogue_off = max(cands, key=lambda t: bare[t])
            notes.append(f"epilogue_off 0x{epilogue_off:x} inferred from {bare[epilogue_off]} "
                         f"jmps without a preceding lea")
    if tb_ret_off is not None and epilogue_off is not None:
        ok = epilogue_off + XOR_EAX_LEN == tb_ret_off
        notes.append(f"epilogue_off + 2 == tb_ret_off: {'ok' if ok else 'MISMATCH'}")
    for name, v in (("tb_ret_off", tb_ret_off), ("epilogue_off", epilogue_off)):
        if v is not None and not 0 <= v < prologue_end:
            notes.append(f"{name} 0x{v:x} outside the prologue [0, 0x{prologue_end:x})")
    return tb_ret_off, epilogue_off, notes


# --------------------------------------------------------------------------
# Classifier
# --------------------------------------------------------------------------

class _Ctx:
    def __init__(self, sc: ScFile, sym: Symbolizer, qemu: Module | None,
                 plugin: Module | None) -> None:
        self.sc = sc
        h = sc.header
        self.ca = h.code_address
        self.arena = Arena(h.code_address, h.code_size)
        self.prologue_end = h.prologue_end
        self.tb_size = h.tb_size
        self.sym = sym
        self.qemu = qemu
        self.plugin = plugin
        self.tb_ret_off: int | None = None
        self.epilogue_off: int | None = None

    def module_class(self, addr: int, *, code: bool) -> tuple[str, str]:
        """Class and symbol text for an absolute address outside the arena."""
        s = self.sym.symbolize(addr)
        text = str(s) if s is not None else ""
        if self.qemu is not None and self.qemu.contains(addr):
            if code:
                return ("ext.helper" if s is not None and s.symbol and s.off == 0
                        else "unknown"), text
            return "abs.qemu_data", text
        if self.plugin is not None and self.plugin.contains(addr):
            if code:
                return ("ext.plugin" if s is not None and s.symbol and s.off == 0
                        else "unknown"), text
            return "abs.plugin_data", text
        m = self.sym.module_for(addr)
        if m is not None:
            return ("ext.lib" if code else "abs.other"), text
        e = self.sym.maps.find(addr)
        if e is not None:
            if code:
                return ("ext.lib" if e.executable else "unknown"), text
            if e.path == "[heap]":
                return "abs.heap", text
            return "abs.other", text
        return ("unknown" if code else "abs.other"), text

    def value_binding(self, v: int) -> tuple[str, str]:
        """Binding class of an absolute data value (pool slot contents)."""
        if self.arena.contains(v):
            return "arena.abs", f"arena+0x{v - self.ca:x}"
        cls, text = self.module_class(v, code=False)
        if cls in ("abs.qemu_data", "abs.plugin_data"):
            s = self.sym.symbolize(v)
            if s is not None and s.symbol and s.off == 0 and self._is_func(v):
                return ("ext.helper" if cls == "abs.qemu_data" else "ext.plugin"), text
        elif cls == "abs.other" and self.sym.module_for(v) is not None:
            m = self.sym.module_for(v)
            if m.image is not None and m.image.is_func_start(v - m.base):
                return "ext.lib", text
        return cls, text

    def _is_func(self, v: int) -> bool:
        m = self.sym.module_for(v)
        return m is not None and m.image is not None and m.image.is_func_start(v - m.base)


def _pool_value(u: ScUnit, tc_abs: int, addr: int, size: int = 8) -> int | None:
    off = addr - tc_abs
    if off < 0 or off + size > u.record.tc_size:
        return None
    return int.from_bytes(u.code[off:off + size], "little")


def _decode_tb(ctx: _Ctx, u: ScUnit) -> tuple[TBResult, list[Insn], list[DecodedRef], int]:
    r = u.record
    tc_abs = ctx.ca + r.tc_offset
    insns, pool = sweep_to_pool(u.code, tc_abs)
    res = TBResult(u.slot, r.tc_offset, r.tc_size, pool - tc_abs, len(insns))
    res.bad = [i.address - tc_abs for i in bad_insns(insns)]
    res.tiled = tiles(insns, tc_abs, pool)
    if not res.tiled:
        res.problems.append(f"decode_failed@0x{(res.bad or [pool - tc_abs])[0]:x}")
    refs = extract_refs(insns, ctx.arena, ctx.sym)
    # irq check: mov ebx/..., [rbp-8]; test; jl  (insn 0 loads icount_decr)
    if insns and insns[0].mem and insns[0].mem[0].base == "rbp" and insns[0].mem[0].disp == -8:
        res.irq_check = True
    return res, insns, refs, tc_abs


def _classify_tb(ctx: _Ctx, u: ScUnit, res: TBResult, insns: list[Insn],
                 refs: list[DecodedRef], tc_abs: int) -> None:
    r = u.record
    tb_abs = ctx.ca + r.tb_offset
    pool_abs = tc_abs + res.pool_start
    code_end = tc_abs + r.tc_size
    starts = {i.address for i in insns if not i.bad}
    goto_fields = {tc_abs + o: n for n, o in enumerate(r.jmp_insn_offset) if o != 0xFFFF}

    def add(ref: DecodedRef, cls: str, *, binding: str = "", symbol: str = "",
            detail: str = "", target: int | None = None) -> None:
        res.refs.append(ClassifiedRef(
            u.slot, ref.insn_addr - tc_abs, ref.field_addr - tc_abs, ref.width, str(ref.kind),
            cls, ref.value if target is None else target, binding, symbol, detail))
        if cls == "unknown":
            res.problems.append(f"unexplained_reference@0x{ref.field_addr - tc_abs:x}")

    for ref in refs:
        k = ref.kind
        v = ref.value
        if k in (RefKind.JMP_MEM, RefKind.CALL_MEM):
            add(ref, "unknown", detail=f"{ref.mnemonic} through memory [{ref.reg}]")
            continue
        if k in (RefKind.JMP_REG, RefKind.CALL_REG):
            if k == RefKind.JMP_REG:
                add(ref, "goto_ptr", detail=f"jmp {ref.reg}")
            else:
                add(ref, "unknown", detail=f"call {ref.reg}")
            continue
        if k in (RefKind.JMP_REL8, RefKind.JMP_REL32, RefKind.JCC_REL8, RefKind.JCC_REL32,
                 RefKind.CALL_REL32):
            assert v is not None
            if ref.field_addr in goto_fields and k == RefKind.JMP_REL32:
                n = goto_fields[ref.field_addr]
                aligned = ref.field_addr % 4 == 0
                chained = v != ref.insn_end
                det = f"goto_tb[{n}]" + ("" if aligned else " field not 4-aligned") + \
                    (" (chained)" if chained else "")
                add(ref, "goto_tb" if aligned else "unknown", detail=det)
            elif tc_abs <= v < code_end:
                if v in starts:
                    add(ref, "intra")
                else:
                    add(ref, "unknown", detail="intra target not an insn boundary")
            elif ctx.arena.contains(v):
                off = v - ctx.ca
                if off < ctx.prologue_end:
                    if k == RefKind.JMP_REL32 and off == ctx.tb_ret_off:
                        add(ref, "prologue.tb_ret")
                    elif k == RefKind.JMP_REL32 and off == ctx.epilogue_off:
                        add(ref, "prologue.epilogue")
                    else:
                        add(ref, "unknown", detail=f"prologue+0x{off:x}")
                else:
                    add(ref, "arena.other_tb", detail=f"arena+0x{off:x}")
            else:
                cls, text = ctx.module_class(v, code=True)
                add(ref, cls, symbol=text)
            continue
        if k in (RefKind.RIP_LEA, RefKind.RIP_MEM, RefKind.RIP_CALL, RefKind.RIP_JMP):
            assert v is not None
            if k == RefKind.RIP_LEA:
                if tb_abs <= v < tb_abs + ctx.tb_size:
                    idx = v - tb_abs
                    add(ref, f"own_struct+{idx}" if idx in OWN_STRUCT_IDX else "unknown",
                        detail=f"lea {ref.reg}, tb+{idx}")
                elif tc_abs <= v < code_end:
                    if v in starts or v >= pool_abs:
                        add(ref, "intra", detail="pool" if v >= pool_abs else "")
                    else:
                        add(ref, "unknown", detail="intra lea target not an insn boundary")
                elif ctx.arena.contains(v):
                    add(ref, "arena.other_tb", detail=f"arena+0x{v - ctx.ca:x}")
                else:
                    cls, text = ctx.module_class(v, code=False)
                    add(ref, "ext.ptr_riprel", binding=cls, symbol=text)
                continue
            if pool_abs <= v < code_end:
                pv = _pool_value(u, tc_abs, v)
                det = f"pool+0x{v - pool_abs:x}"
                if v % 8:
                    det += " (slot not 8-aligned)"
                if pv is None:
                    add(ref, "unknown", detail=det + " slot past tc_size")
                    continue
                det += f" = 0x{pv:x}"
                if k in (RefKind.RIP_CALL, RefKind.RIP_JMP):
                    # the slot holds a branch target: an external function, or
                    # (out-of-range exit) a prologue address
                    if ctx.arena.contains(pv):
                        off = pv - ctx.ca
                        binding = ("prologue.tb_ret" if off == ctx.tb_ret_off else
                                   "prologue.epilogue" if off == ctx.epilogue_off else "")
                        text = f"arena+0x{off:x}"
                        if k != RefKind.RIP_JMP or not binding:
                            add(ref, "unknown", binding=binding or "arena.abs", symbol=text,
                                detail=det)
                            continue
                    else:
                        binding, text = ctx.module_class(pv, code=True)
                        if binding == "unknown":
                            add(ref, "unknown", symbol=text, detail=det)
                            continue
                else:
                    binding, text = ctx.value_binding(pv)
                add(ref, "pool", binding=binding, symbol=text, detail=det)
            elif ctx.arena.contains(v):
                add(ref, "arena.other_tb", detail=f"rip operand arena+0x{v - ctx.ca:x}")
            else:
                cls, text = ctx.module_class(v, code=k in (RefKind.RIP_CALL, RefKind.RIP_JMP))
                add(ref, "unknown", binding=cls, symbol=text,
                    detail="rip memory operand outside the TB")
            continue
        if k == RefKind.MOVABS:
            assert v is not None
            binding, text = (ctx.value_binding(v) if ref.region else ("abs.other", ""))
            add(ref, "arena.abs" if binding == "arena.abs" else binding, symbol=text,
                detail="movabs")
            continue
        if k in (RefKind.IMM32, RefKind.DISP32):
            assert v is not None
            binding, text = ctx.value_binding(v)
            add(ref, binding, symbol=text, detail=f"{k} {ref.enc.name}")
            continue
        add(ref, "unknown", detail=f"unhandled kind {k}")  # pragma: no cover


# --------------------------------------------------------------------------
# Guest binding (optional firmware ELF)
# --------------------------------------------------------------------------

def _firmware_segments(path: str) -> list[tuple[int, bytes]]:
    from elftools.elf.elffile import ELFFile

    out = []
    with open(path, "rb") as f:
        elf = ELFFile(f)
        for seg in elf.iter_segments():
            if seg["p_type"] == "PT_LOAD" and seg["p_filesz"]:
                out.append((seg["p_vaddr"], seg.data()))
    return out


def _guest_bind(sc: ScFile, units: Sequence[ScUnit], firmware: str,
                tbs: dict[int, TBResult]) -> dict[str, Any]:
    """Bind each TB's guest bytes to the firmware by ``guest_hash`` and check Thumb lengths.

    ``page_addr0`` is a ram_addr, not a vaddr. The ram_addr - vaddr delta is
    inferred as the one under which most TBs' firmware bytes hash to their
    ``guest_hash``; TBs whose bytes match under it are ``elf_verified``.
    """
    page_bits = sc.header.target_page_bits
    pmask = (1 << page_bits) - 1
    segs = _firmware_segments(firmware)
    votes: Counter = Counter()
    for u in units:
        r = u.record
        lo = r.page_addr0 & pmask
        for base, data in segs:
            # candidate vaddrs inside this segment with the same in-page offset
            v = base + ((lo - base) & pmask)
            while v + r.guest_size <= base + len(data):
                o = v - base
                if fnv1a64(data[o:o + r.guest_size]) == r.guest_hash:
                    votes[r.page_addr0 - v] += 1
                v += 1 << page_bits
    if not votes:
        return {"firmware": firmware, "delta": None, "elf_verified": 0,
                "ram_resident_or_unbound": len(units)}
    delta, _ = votes.most_common(1)[0]
    verified = thumb_ok = 0
    lens_bad: list[int] = []
    for u in units:
        r = u.record
        t = tbs[u.slot]
        v = r.page_addr0 - delta
        g: dict[str, Any] = {"vaddr": f"0x{v:x}"}
        data = None
        for base, d in segs:
            if base <= v and v + r.guest_size <= base + len(d):
                data = d[v - base:v - base + r.guest_size]
                break
        g["origin"] = "elf_verified" if data is not None and \
            fnv1a64(data) == r.guest_hash else "ram_resident_or_unbound"
        try:
            rows = decode_search(u.search, r.icount, pcrel=is_pcrel(r.cflags), pc=r.pc)
        except SearchDecodeError as e:
            t.problems.append(f"search_decode: {e}")
            rows = None
        if rows is not None:
            w0 = [row.words[0] for row in rows]
            lens = lengths_from_words(w0, r.guest_size, page_bits)
            if is_pcrel(r.cflags) and w0 and w0[0] != (v & pmask):
                t.problems.append("guest: word0 != vaddr page offset")
            if g["origin"] == "elf_verified":
                verified += 1
                probs = check_lengths(data, lens, v)
                g["thumb_lengths"] = "ok" if not probs else probs
                if probs:
                    lens_bad.append(u.slot)
                    t.problems.append("guest: thumb length mismatch")
                else:
                    thumb_ok += 1
        t.guest = g
    return {
        "firmware": firmware,
        "delta": f"0x{delta:x}",
        "delta_votes": votes[delta],
        "other_deltas": {f"0x{d:x}": n for d, n in votes.most_common(4) if d != delta},
        "elf_verified": verified,
        "ram_resident_or_unbound": len(units) - verified,
        "thumb_lengths_ok": thumb_ok,
        "thumb_lengths_bad_slots": lens_bad,
    }


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def classify_sc(sc: ScFile | str | os.PathLike, *, qemu: str | None = DEFAULT_QEMU,
                qemu_base: int = SETARCH_R_PIE_BASE, plugin: str | None = DEFAULT_PLUGIN,
                plugin_base: int | None = None, maps: Maps | None = None,
                tb_ret_off: int | None = None, epilogue_off: int | None = None,
                firmware: str | None = None) -> MetalessResult:
    """Classify every reference of every valid TB in ``sc`` (meta-less)."""
    if not isinstance(sc, ScFile):
        sc = read_sc(sc)
    if not sc.loadable:
        raise ValueError(f"{sc.path}: header rejected ({', '.join(sc.header_problems)})")
    units = sc.units()
    modules_info: list[dict[str, Any]] = []
    qmod = pmod = None
    if maps is not None:
        sym = Symbolizer.from_maps(maps)
        for m in sym.modules:
            if qemu and os.path.realpath(m.path) == os.path.realpath(qemu):
                qmod = m
            elif plugin and os.path.realpath(m.path) == os.path.realpath(plugin):
                pmod = m
        modules_info = [{"name": m.name, "path": m.path, "base": f"0x{m.base:x}",
                         "source": "maps"} for m in sym.modules]
    else:
        mods: list[Module] = []
        if qemu:
            qmod = Module.from_file(qemu, qemu_base, "qemu-system-arm")
            mods.append(qmod)
            modules_info.append({"name": qmod.name, "path": qmod.path, "base": f"0x{qemu_base:x}",
                                 "source": "setarch -R PIE base"})
        sym = Symbolizer(mods)
        if plugin:
            img = load_elf(plugin)
            src = "given"
            if plugin_base is None:
                # direct-call targets outside the arena and outside QEMU
                ctx0 = _Ctx(sc, sym, qmod, None)
                ext: list[int] = []
                for u in units:
                    _, _, refs, _ = _decode_tb(ctx0, u)
                    ext += [r.value for r in refs if r.kind == RefKind.CALL_REL32
                            and r.value is not None and not ctx0.arena.contains(r.value)
                            and not (qmod and qmod.contains(r.value))]
                plugin_base, n, tot = infer_plugin_base(ext, img)
                src = f"inferred: {n}/{tot} external call targets on function starts"
            if plugin_base is not None:
                pmod = Module(os.path.basename(img.path), img.path, plugin_base, img)
                mods.append(pmod)
            modules_info.append({"name": os.path.basename(img.path), "path": img.path,
                                 "base": None if plugin_base is None else f"0x{plugin_base:x}",
                                 "source": src})
        sym = Symbolizer(mods)
    ctx = _Ctx(sc, sym, qmod, pmod)

    decoded = [(u, *_decode_tb(ctx, u)) for u in units]

    # prologue offsets from exit jmps
    jmps: list[tuple[int, bool]] = []
    for u, res, insns, refs, tc_abs in decoded:
        prev = {insns[k].address: insns[k - 1] for k in range(1, len(insns))}
        tb_abs = ctx.ca + u.record.tb_offset
        for ref in refs:
            if ref.kind == RefKind.JMP_REL32 and ref.value is not None \
                    and 0 <= ref.value - ctx.ca < ctx.prologue_end:
                p = prev.get(ref.insn_addr)
                lea = (p is not None and p.is_lea and p.dst_reg == "rax"
                       and p.rip_target is not None
                       and tb_abs <= p.rip_target < tb_abs + ctx.tb_size)
                jmps.append((ref.value - ctx.ca, lea))
    ctx.tb_ret_off, ctx.epilogue_off, notes = infer_prologue(jmps, ctx.prologue_end,
                                                             tb_ret_off, epilogue_off)
    for u, res, insns, refs, tc_abs in decoded:
        _classify_tb(ctx, u, res, insns, refs, tc_abs)
    tbs = [d[1] for d in decoded]
    guest: dict[str, Any] = {}
    if firmware:
        guest = _guest_bind(sc, units, firmware, {t.slot: t for t in tbs})
    return MetalessResult(sc.path, ctx.ca, ctx.prologue_end, ctx.tb_ret_off, ctx.epilogue_off,
                          notes, modules_info, tbs, [s.slot for s in sc.skipped], guest)
