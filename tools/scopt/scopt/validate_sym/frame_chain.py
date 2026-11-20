"""Discover straight-line callback chains in the verified x86-64 plugin image."""

from __future__ import annotations

import io

from elftools.elf.elffile import ELFFile

from ..decode.x86 import decoder, sweep
from ..model.native import NativeFrameBody
from .native_inline import NativeInlineError, check_leaf

_ENDBR = bytes.fromhex("f30f1efa")
_PROLOGUE = bytes.fromhex("55 4889e5")
_SPILLS = bytes.fromhex("4883ec10 897dfc 488975f0")
_TIMER_OBJECTS = (("g_bench_tick_count", 8), ("g_bench_start_ts", 16), ("g_bench_started", 1))


def _operations(body: NativeFrameBody) -> tuple[int, bytes] | None:
    """Strip only exact SysV prologues with a matching normal-return epilogue."""
    code = body.code
    start = len(_ENDBR) if code.startswith(_ENDBR) else 0
    if not code.startswith(_PROLOGUE, start):
        return None
    start += len(_PROLOGUE)
    if code.startswith(_SPILLS, start):
        end = b"\xc9\xc3"  # LEAVE; RET
        start += len(_SPILLS)
    else:
        end = b"\x5d\xc3"  # pop rbp; RET
    if not code.endswith(end) or start > len(code) - len(end):
        return None
    return body.address + start, code[start:-len(end)].removesuffix(b"\x90")


def wrapper_target(body: NativeFrameBody) -> int | None:
    """Exactly one direct call, with no operations observing either return slot."""
    ops = _operations(body)
    if ops is None:
        return None
    address, code = ops
    if len(code) != 5 or code[:1] != b"\xe8":
        return None
    return address + 5 + int.from_bytes(code[1:], "little", signed=True)


def _timer_targets(body: NativeFrameBody):
    """Recognize the benchmark timer, allowing only relocation fields to vary."""
    ops = _operations(body)
    if ops is None:
        return None
    address, code = ops
    template = (bytes.fromhex("48c705 00000000 00000000"), bytes.fromhex("488d05 00000000"),
                bytes.fromhex("4889c6"), bytes.fromhex("bf01000000"),
                bytes.fromhex("e8 00000000"), bytes.fromhex("c605 00000000 01"))
    if len(code) != sum(map(len, template)):
        return None
    insns = sweep(code, address)
    if len(insns) != len(template):
        return None
    targets = []
    for ins, expected in zip(insns, template):
        normalized = bytearray(ins.raw)
        target = ins.branch_target if ins.is_call else ins.rip_target
        if target is not None:
            off = ins.imm_offset if ins.is_call else ins.disp_offset
            normalized[off:off + 4] = bytes(4)
            targets.append(target)
        if normalized != expected:
            return None
    return tuple(targets)


def _register_leaf(body: NativeFrameBody) -> bool:
    """The existing integer proof admits register arithmetic with no stack observers."""
    ops = _operations(body)
    code = body.code if ops is None else ops[1] + b"\xc3"
    # check_leaf also supports branches. Only its linear single-RET subset can
    # use our fall-through emitter.
    if len(code) > 128 * 15:  # existing integer proof limit times architectural instruction size
        return False
    insns = sweep(code, 0)
    if not insns or insns[-1].raw != b"\xc3" or any(i.is_jump or i.is_ret for i in insns[:-1]):
        return False
    try:
        check_leaf(code)
    except NativeInlineError:
        return False
    # The surrounding wrapper's LEAVE/POP expects its frame pointer intact.
    # No leaf may use RBP to expose or overwrite an elided return slot.
    for insn in decoder().disasm(code, 0):
        reads, writes = insn.regs_access()
        if any(insn.reg_name(reg) in ("rbp", "ebp", "bp", "bpl") for reg in (*reads, *writes)):
            return False
    return True


def valid_chain(root: NativeFrameBody, chain: tuple[NativeFrameBody, ...]) -> bool:
    """Recheck every source edge and the terminal, even at a shallow depth limit."""
    if not chain:
        return False
    parent = root
    seen = {root.address}
    for child in chain:
        if (type(child.address) is not int or not 0 < child.address < (1 << 64)
                or child.address in seen or wrapper_target(parent) != child.address):
            return False
        seen.add(child.address)
        parent = child
    return _timer_targets(parent) is not None or _register_leaf(parent)


def _clock_plt(image, target: int, text_bytes) -> bool:
    """Resolve the exact CET PLT jump and versioned glibc timer import."""
    elf = ELFFile(io.BytesIO(image.bytes))
    plt = elf.get_section_by_name(".plt.sec")
    relocs = elf.get_section_by_name(".rela.plt")
    versions = elf.get_section_by_name(".gnu.version")
    needed = elf.get_section_by_name(".gnu.version_r")
    if any(section is None for section in (plt, relocs, versions, needed)):
        return False
    offset = target - plt["sh_addr"]
    if offset < 0 or offset % 16 or offset + 16 > plt["sh_size"]:
        return False
    stub = text_bytes(target, 16)
    if stub is None or stub[:6] != bytes.fromhex("f30f1efa ff25"):
        return False
    got = target + 10 + int.from_bytes(stub[6:10], "little", signed=True)
    hits = [r for r in relocs.iter_relocations() if r["r_offset"] == got]
    if len(hits) != 1 or hits[0]["r_info_type"] != 7 or hits[0]["r_addend"] != 0:
        return False
    index = hits[0]["r_info_sym"]
    symbol = elf.get_section(relocs["sh_link"]).get_symbol(index)
    if (symbol.name != "clock_gettime" or symbol["st_shndx"] != "SHN_UNDEF"
            or symbol["st_info"]["type"] != "STT_FUNC"):
        return False
    version = versions.get_symbol(index)["ndx"]
    return any(lib.name == "libc.so.6" and alias.name == "GLIBC_2.17" and alias["vna_other"] == version
               for lib, aliases in needed.iter_versions() for alias in aliases)


def discover_chain(root: NativeFrameBody, image, text_bytes, writable_object) -> tuple[NativeFrameBody, ...]:
    """Follow direct edges to exact sized functions in this capture's immutable text."""
    current = root
    seen = {root.address}
    chain = []
    while (address := wrapper_target(current)) is not None:
        if address in seen:
            return ()
        seen.add(address)
        label = image.symbols.lookup(address)
        if label is None or label[1] != 0:
            return ()
        symbol = image.symbols.bounded_function(label[0], address)
        if symbol is None:
            return ()
        code = text_bytes(address, symbol.size)
        if code is None:
            return ()
        current = NativeFrameBody(address, code)
        chain.append(current)
    if not chain:
        return ()
    targets = _timer_targets(current)
    if targets is None:
        return tuple(chain) if _register_leaf(current) else ()
    tick, stamp, clock, started = targets
    for (name, size), address in zip(_TIMER_OBJECTS, (tick, stamp, started)):
        symbol = image.symbols.symbol(name)
        if (symbol is None or symbol.type != "STT_OBJECT" or symbol.value != address
                or symbol.size != size or not writable_object(address, size)):
            return ()
    return tuple(chain) if _clock_plt(image, clock, text_bytes) else ()
