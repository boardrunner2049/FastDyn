"""Post-inline UDIV DCE, cold-state preservation and independent continuation checks."""

import ctypes
from dataclasses import replace
import mmap
import platform
import random

import pytest

from scopt.formats.enums import Encoding, SiteKind
from scopt.model.native import UDIV_KIND
from scopt.model.unit import Fixup, Site
from scopt.validate_sym.native_inline import NativeInlineError
from scopt.validate_sym.native_udiv import prepare_udiv_splice
from scopt.validate_sym.native_udiv_cleanup import (
    KIND, prepare_udiv_cleanup, verify_udiv_cleanup_body, verify_udiv_cleanup_splice,
)
from tests.unit.test_native_udiv import _fixture


def _tail(unit, req, tc, body, *, conditional=False, target=None):
    off = req.continuation_off
    target = tc - 64 if target is None else target
    stop = off + len(body)
    delta = target - (tc + stop + 5)
    jmp = b"\xe9" + delta.to_bytes(4, "little", signed=True)
    rows = [Site(stop + 1, stop, 4, Encoding.REL32, SiteKind.EXIT_TB_RET_JMP,
                 0, target, 1, delta & 0xffffffff)]
    if conditional:
        assert body[:1] == b"\x74"
        rows.append(Site(off + 1, off, 1, Encoding.REL8, SiteKind.LABEL_JCC,
                         0, tc + off + 2 + body[1], 1, body[1]))
    assert stop + len(jmp) <= req.pool_slot_off
    code = bytearray(unit.code)
    code[off:stop + len(jmp)] = body + jmp
    fix = Fixup(stop + 1, 4, "rel_minus_delta", int(SiteKind.EXIT_TB_RET_JMP),
                "prologue.tb_ret", target)
    return replace(unit, code=bytes(code), sites=unit.sites + tuple(rows),
                   reloc=replace(unit.reloc, fixups=(fix,)))


def _from(body, *, conditional=False):
    unit, req, base = _fixture()
    unit = _tail(unit, req, base + unit.tc_offset, bytes.fromhex(body), conditional=conditional)
    return unit, req, base


def test_drop_only_four_scrubs_bind_the_actual_return_and_preserve_both_arms():
    # MOV EBX,[RBP+4]; IMUL EBX,EAX -- the first two instructions after UDIV in AES.
    unit, req, base = _from("8b5d04 0fafd8")
    original = prepare_udiv_splice(unit, req, base)
    compact = prepare_udiv_cleanup(unit, req, base)
    assert compact.kind == KIND and len(original.code) - len(compact.code) == 8
    assert compact.code[4:15] == bytes.fromhex("89d1 89f0 31d2 f7f1 4531c0")
    assert compact.code[-6:] == original.code[-6:] == req.call_bytes
    req = replace(req, splice=compact)
    verify_udiv_cleanup_splice(unit, req, base)
    with pytest.raises(NativeInlineError):
        verify_udiv_cleanup_splice(replace(unit, reloc=replace(unit.reloc, fixups=())), req, base)


@pytest.mark.parametrize(("body", "admitted"), [
    ("c1c317 4519f6 41ffc6", True),  # ROL/SBB/INC on unrelated regs in hot guest
    ("d1c2", False),              # ROL EDX reads its live operand
    ("89d3", False),             # read EDX
    ("4889fb", False),           # read RDI, including the high half
    ("660fbef1 89f3", False),    # partial write/read; do not treat it as a full kill
    ("0f44d0 89d3", False),      # conditional move can keep the old EDX
    ("488d140a", False),         # RDX and RCX in an effective address
    ("8907", False),             # outside CPU env / stores through RDI
    ("ff20", False),             # unclassified indirect exit
    ("e800000000", False),       # an ordinary call can observe argument registers
    ("89c2 89d3", True),         # MOV EDX,EAX kills all of RDX, including its high half
    ("31d2 4889d3", True),       # XOR EDX,EDX also kills the entire RDX
    ("89c2 488d140a", False),    # ECX is still live
])
def test_read_before_full_definition_or_unknown_operations_keep_full_clearing(body, admitted):
    unit, req, base = _from(body)
    assert prepare_udiv_cleanup(unit, req, base).kind == (KIND if admitted else UDIV_KIND)


def test_both_conditional_successors_are_checked_and_branch_interior_is_refused():
    # JE skips MOV EBX,EDX. Even if JE is usually taken, the fallthrough can read it.
    unit, req, base = _from("7402 89d3", conditional=True)
    assert prepare_udiv_cleanup(unit, req, base).kind == UDIV_KIND
    unit, req, base = _from("7402 89c3", conditional=True)
    assert prepare_udiv_cleanup(unit, req, base).kind == KIND
    code = bytearray(unit.code)
    code[req.continuation_off + 1] = 1
    s = unit.sites[-1]
    unit = replace(unit, code=bytes(code), sites=(*unit.sites[:-1],
                   replace(s, value=s.value-1, final_field=1)))
    assert prepare_udiv_cleanup(unit, req, base).kind == UDIV_KIND


def test_physical_reimport_rederives_the_liveness_and_epilogue_binding():
    unit, req, base = _from("8b5d04 0fafd8")
    compact = prepare_udiv_cleanup(unit, req, base)
    req = replace(req, splice=compact)
    tc = base + unit.tc_offset
    growth = req.growth
    end = req.continuation_off + growth
    ret = unit.reloc.fixups[0].target_abs
    # Materialize the physically relocated reset path and its capture row.
    code = bytearray(unit.code[:req.call_off] + compact.code + unit.code[req.continuation_off:unit.pool_start])
    old = unit.sites[-1]
    field = old.field_off + growth
    disp = ret - (tc + field + 4)
    code[field:field+4] = disp.to_bytes(4, "little", signed=True)
    physical = replace(old, field_off=field, insn_off=old.insn_off+growth, final_field=disp&0xffffffff)
    verify_udiv_cleanup_body(req, bytes(code), end, (physical,), tc, tb_ret=ret)
    for source, endpoint in ((code, ret+8),
                             (code[:end] + bytes.fromhex("89d3 90") + code[end+3:], ret)):
        with pytest.raises(NativeInlineError, match="live"):
            verify_udiv_cleanup_body(req, bytes(source), end, (physical,), tc, tb_ret=endpoint)
    changed = replace(req, splice=replace(compact, code=compact.code[:12]+b"\x90"+compact.code[13:]))
    with pytest.raises(NativeInlineError, match="differs"):
        verify_udiv_cleanup_body(changed, bytes(code), end, (physical,), tc, tb_ret=ret)


@pytest.mark.skipif(platform.system() != "Linux" or platform.machine() != "x86_64",
                    reason="requires x86-64 Linux SysV executable memory")
def test_native_postinline_quotient_flags_and_zero_fallback_arguments():
    unit, req, base = _from("8b5d04 0fafd8")
    old, new = prepare_udiv_splice(unit, req, base), prepare_udiv_cleanup(unit, req, base)
    # Use RBX as a private harness pointer. Record RFLAGS at the continuation.
    head = bytes.fromhex("53 4889fb")
    tail = bytes.fromhex("9c 415a 4c895310 5b c3")
    # The cold fallback records the actual hardware return PC, numerator,
    # denominator and env pointer under the same SysV ABI as the real helper.
    probe = bytes.fromhex("488b0424 488907 897708 89570c 48897f18 31c0 c3")
    signature = ctypes.CFUNCTYPE(ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32)
    with mmap.mmap(-1, mmap.PAGESIZE, prot=mmap.PROT_READ | mmap.PROT_WRITE) as mem:
        address = ctypes.addressof(ctypes.c_char.from_buffer(mem))
        methods = []
        for off, splice in ((0, old), (512, new)):
            code = head + splice.code + tail
            cont = off + len(head) + len(splice.code)
            pool = cont + req.pool_slot_off - req.continuation_off
            mem[off:off+len(code)] = code
            mem[pool:pool+8] = (address + 1024).to_bytes(8, "little")
            methods.append((signature(address+off), address+cont))
        mem[1024:1024+len(probe)] = probe
        assert ctypes.CDLL(None).mprotect(ctypes.c_void_p(address), mmap.PAGESIZE,
                                         mmap.PROT_READ | mmap.PROT_EXEC) == 0
        rng = random.Random(5481)
        cases = [(0,1), (0xffffffff, 1), (0xffffffff,0xffffffff), (1,0)]
        cases.extend((rng.getrandbits(32), rng.randrange(1,1<<32)) for _ in range(256))
        for n, den in cases:
            flags = []
            for invoke, cont in methods:
                env = (ctypes.c_uint64*4)()
                ptr = ctypes.addressof(env)
                result = invoke(ptr, n, den)
                assert result == (n//den if den else 0)
                if den:
                    assert env[0] == env[1] == env[3] == 0
                else:
                    assert (env[0],env[1],env[3]) == (cont,n,ptr)
                flags.append(env[2] & 0x8c5)  # OF,SF,ZF,PF,CF; XOR leaves AF undefined
            assert flags == [0x44,0x44]
