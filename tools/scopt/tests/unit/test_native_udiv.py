"""Guarded UDIV correspondence, executable semantics and zero-path unwind."""

import ctypes
from dataclasses import replace
import mmap
import platform
import random

import pytest

from scopt.canon import sha256_id
from scopt.decode.search import decode_search, encode_search, is_pcrel, unwind
from scopt.decode.x86 import sweep
from scopt.formats.enums import Encoding, OpMarkKind, SiteKind
from scopt.formats.sc_v5 import ScRecord
from scopt.model.native import InlineCall, NativeFragment
from scopt.model.unit import Bindings, HelperBinding, Insn, OpMark, PoolEntry, RecoveryPoint, Site
from scopt.splice_layout import materialize_splice, prepare_splice_layout
from scopt.validate_sym.native_inline import NativeInlineError, verify_site
from scopt.validate_sym.native_udiv import prepare_reciprocal29, prepare_udiv_splice, verify_udiv_body, verify_udiv_splice
from tests.synth.fake_snapshot import fake_snapshot


# helper_udiv from the QEMU image used to capture nettle-aes.
_NATIVE = bytes.fromhex(
    "f30f1efa 85d2 7418 89d1 89f0 31d2 f7f1 31d2 31c9 31f6 31ff 4531c0 c3 0f1f4000 "
    "4883ec08 4c8b442408 f687e932010002 7516 31c0 4883c408 31d2 31c9 31f6 31ff "
    "4531c0 c3 0f1f4000 8b87600c0000 8b8487140c0000 83e010 74da b901000000 "
    "31d2 be17000000 e885fdffff"
)


def _fixture(*, slot=32):
    snapshot = fake_snapshot(1)
    unit, = snapshot.units()
    record = ScRecord.from_bytes(unit.record)
    # Caller source and register convention from the AES capture. One extra
    # guest follows the CALL to distinguish unwind to the wrong instruction.
    setup = bytes.fromhex("8b5504 8b7500 488bfd")
    target = 0x11223344
    call_off, cont = len(setup), len(setup) + 6
    call = b"\xff\x15" + (slot-cont).to_bytes(4, "little")
    code = (setup+call).ljust(slot, b"\x90") + target.to_bytes(8, "little")
    words = (0x0800060e, 0, 0)
    next_words = (0x08000612, 0, 0)
    search = encode_search([(words, cont), (next_words, slot)], pcrel=is_pcrel(record.cflags), pc=record.pc)
    end = unit.tc_offset + len(code) + len(search)
    record = record.replace(icount=2, tc_size=len(code), end_offset=end).to_bytes(fix_checksum=True)
    index = 4
    site = Site(call_off+2, call_off, 4, Encoding.POOL_INDIRECT, SiteKind.HELPER_CALL, index,
                target, 0, slot-cont)
    helper = HelperBinding(index, "udiv", "qemu-system-arm", "helper_udiv", 1, target)
    unit = replace(unit, tc_size=len(code), code=code, record=record, search=search, end_offset=end,
                   icount=2, insn0_start=0, body_end=slot, ldst_end=slot, pool_start=slot,
                   sites=(site,), pool=(PoolEntry(site.field_off, slot, 2, 1, -4, target),),
                   insns=(Insn(0, words, 0, cont), Insn(1, next_words, cont, slot)),
                   recovery=(RecoveryPoint("r0", cont, 0, words, "helper_call"),),
                   opmarks=(OpMark(0, OpMarkKind.CALL, 0, index),), bindings=Bindings((helper,)),
                   content_hash=sha256_id(record + code + search))
    digest = sha256_id(b"fixture")
    req = InlineCall(unit.uid, unit.content_hash, call_off, call, cont, index, target, slot,
                     (words[0],), digest, 0x8cf6e0, 0x8cf6e0, "helper_udiv", _NATIVE,
                     digest, NativeFragment(b"", (), ()), digest, tcg_typemask=1202)
    return unit, req, snapshot.header.code_address


def test_materialized_udiv_retains_bound_call_and_unwinds_original_guest():
    unit, req, base = _fixture()
    splice = prepare_udiv_splice(unit, req, base)
    req = prepare_splice_layout(unit, replace(req, splice=splice), base)
    verify_udiv_body(req)
    verify_udiv_splice(unit, req, base)
    output = materialize_splice(unit, req, base)
    assert req.growth == 32
    new_cont = req.continuation_off + req.growth
    fallback = new_cont - len(req.call_bytes)
    assert output.code[fallback:new_cont] == req.call_bytes
    assert output.sites == (replace(unit.sites[0], insn_off=fallback, field_off=fallback+2),)
    assert output.pool == (replace(unit.pool[0], slot_off=req.pool_slot_off+req.growth,
                                   user_field_off=fallback+2),)
    assert output.opmarks == (replace(unit.opmarks[0], host_off=fallback),)
    assert output.recovery == (replace(unit.recovery[0], host_return_offset=new_cont),)
    record = ScRecord.from_bytes(output.record)
    rows = decode_search(output.search, output.icount, pcrel=is_pcrel(record.cflags), pc=record.pc)
    assert unwind(rows, new_cont) == (0, unit.insns[0].words)
    assert rows[1].host_end == output.pool_start
    assert output.code[output.pool_start:] == unit.code[unit.pool_start:]
    assembly = sweep(output.code[req.call_off:new_cont], req.call_off)
    assert assembly[1].branch_target == fallback
    assert next(ins for ins in assembly if ins.mnemonic == "jmp").branch_target == new_cont
    assert assembly[-1].is_call and assembly[-1].rip_target == output.pool_start


def test_udiv_cannot_bypass_generic_purity_or_remove_unknown_arm():
    unit, req, base = _fixture()
    with pytest.raises(NativeInlineError):
        verify_site(unit, req, base)
    for field, value in (("symbol", "helper_sdiv"), ("tcg_typemask", 18), ("pool_slot_off", None),
                         ("callee_code", _NATIVE.replace(b"\xf7\xf1", b"\xf7\xf9")),
                         ("callee_code", _NATIVE.replace(b"\x74\x18", b"\x74\x02")),
                         ("callee_code", _NATIVE.replace(b"\x74\x18", b"\x74\x19"))):
        with pytest.raises(NativeInlineError):
            prepare_udiv_splice(unit, replace(req, **{field: value}), base)
    for altered in (replace(unit, recovery=()), replace(unit, bindings=Bindings((
            replace(unit.bindings.helpers[0], flags=5),)))):
        with pytest.raises(NativeInlineError):
            prepare_udiv_splice(altered, req, base)
    splice = prepare_udiv_splice(unit, req, base)
    for offset in (3, 7, len(splice.code)-1):
        corrupted = bytearray(splice.code)
        corrupted[offset] ^= 1
        with pytest.raises(NativeInlineError, match="differ"):
            verify_udiv_body(replace(req, splice=replace(splice, code=bytes(corrupted))))


@pytest.mark.skipif(platform.system() != "Linux" or platform.machine() != "x86_64",
                    reason="executes emitted x86-64 Linux SysV instructions")
def test_native_nonzero_bypasses_call_and_zero_observes_precise_return_address():
    unit, req, base = _fixture()
    splice = prepare_udiv_splice(unit, req, base)
    cont = len(splice.code)
    slot = req.pool_slot_off - req.call_off + cont - len(req.call_bytes)
    # A probe helper records its real hardware return PC in the passed env.
    # The entry wrapper starts at the original CALL, after argument setup.
    probe = bytes.fromhex("488b0424 488907 31c0 c3")
    with mmap.mmap(-1, mmap.PAGESIZE, prot=mmap.PROT_READ | mmap.PROT_WRITE) as page:
        address = ctypes.addressof(ctypes.c_char.from_buffer(page))
        page[:cont+1] = splice.code + b"\xc3"
        page[slot:slot+8] = (address + slot + 8).to_bytes(8, "little")
        page[slot+8:slot+8+len(probe)] = probe
        libc = ctypes.CDLL(None, use_errno=True)
        assert libc.mprotect(ctypes.c_void_p(address), ctypes.c_size_t(mmap.PAGESIZE),
                             mmap.PROT_READ | mmap.PROT_EXEC) == 0
        invoke = ctypes.CFUNCTYPE(ctypes.c_uint32, ctypes.POINTER(ctypes.c_size_t),
                                  ctypes.c_uint32, ctypes.c_uint32)(address)
        observed = ctypes.c_size_t()
        cases = [(0, 1), (1, 1), (0xffffffff, 1), (0xffffffff, 0xffffffff), (1, 0xffffffff)]
        rng = random.Random(0xD17)
        cases.extend((rng.getrandbits(32), rng.randrange(1, 1 << 32)) for _ in range(128))
        for numerator, denominator in cases:
            assert invoke(ctypes.byref(observed), numerator, denominator) == numerator // denominator
            assert observed.value == 0
        assert invoke(ctypes.byref(observed), 0xffffffff, 0) == 0
        assert observed.value == address + cont


@pytest.mark.skipif(platform.system() != "Linux" or platform.machine() != "x86_64",
                    reason="executes the emitted AMD64 instructions")
def test_reciprocal29_native_quotient_and_fallback_return_pc():
    unit, req, base = _fixture()
    splice = prepare_reciprocal29(unit, req, base)
    cont = len(splice.code)
    slot = req.pool_slot_off - req.call_off + cont - len(req.call_bytes)
    probe = bytes.fromhex("488b0424 488907 31c0 c3")
    with mmap.mmap(-1, mmap.PAGESIZE, prot=mmap.PROT_READ | mmap.PROT_WRITE) as page:
        addr = ctypes.addressof(ctypes.c_char.from_buffer(page))
        page[:cont+1] = splice.code + b"\xc3"
        page[slot:slot+8] = (addr+slot+8).to_bytes(8, "little")
        page[slot+8:slot+8+len(probe)] = probe
        assert ctypes.CDLL(None).mprotect(ctypes.c_void_p(addr), ctypes.c_size_t(mmap.PAGESIZE),
                                          mmap.PROT_READ | mmap.PROT_EXEC) == 0
        call = ctypes.CFUNCTYPE(ctypes.c_uint32, ctypes.POINTER(ctypes.c_size_t),
                               ctypes.c_uint32, ctypes.c_uint32)(addr)
        observed = ctypes.c_size_t()
        rng = random.Random(29)
        numerators = [0,1,28,29,30,0x7fffffff,0x80000000,0xfffffffe,0xffffffff]
        numerators.extend(rng.getrandbits(32) for _ in range(8192))
        for n in numerators:
            assert call(ctypes.byref(observed), n, 29) == n // 29
        assert observed.value == 0
        for d in (0, 1, 2, 28, 30, 0xffffffff):
            observed.value = 0
            assert call(ctypes.byref(observed), 0xffffffff, d) == 0
            assert observed.value == addr + cont
