"""Proof and mutation tests for the fall-through native helper splice."""

from dataclasses import replace
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from scopt.canon import sha256_id
from scopt.decode.x86 import decoder
from scopt.formats.enums import Encoding, OpMarkKind, SiteKind
from scopt.model.native import InlineCall, NativeFragment, NativeOrigin
from scopt.model.unit import (
    Bindings,
    HelperBinding,
    Insn,
    OpMark,
    PoolEntry,
    RecoveryPoint,
    RelocInfo,
    Site,
)
from scopt.validate_sym.native_inline import NativeInlineError
from scopt.validate_sym.native_splice import prepare_splice, verify_splice, verify_splice_body

_BASE = 0x12345000
_TARGET = _BASE + 0x1000
_ARGUMENT = bytes.fromhex("8b7d00")
_CALLEE = bytes.fromhex("f30f1efa 89f8 01c0 31ff c3")


def _leaf_fragment(source: bytes) -> NativeFragment:
    decoded = list(decoder().disasm(source, 0))
    assert bytes(decoded[-1].bytes) == b"\xc3"
    return NativeFragment(
        code=source[:-1] + b"\xe9\x00\x00\x00\x00",
        origins=(
            *(NativeOrigin(i.address, i.size, i.address, i.size) for i in decoded[:-1]),
            NativeOrigin(decoded[-1].address, 1, decoded[-1].address, 5),
        ),
        continuation_fields=(len(source),),
    )


def _case(*, source=_CALLEE, pool=False, before=b"", argument=_ARGUMENT):
    start = len(before)
    call = start + len(argument)
    opcode = b"\xff\x15" if pool else b"\xe8"
    cont = call + len(opcode) + 4
    slot = 0x40 if pool else None
    disp = slot - cont if pool else _TARGET - _BASE - cont
    call_bytes = opcode + disp.to_bytes(4, "little", signed=True)
    code = (before + argument + call_bytes).ljust(0x40, b"\x90")
    if pool:
        code += _TARGET.to_bytes(8, "little")
    site = Site(call + len(opcode), call, 4,
                Encoding.POOL_INDIRECT if pool else Encoding.REL32,
                SiteKind.HELPER_CALL, 3, _TARGET, 0, disp & 0xFFFFFFFF)
    unit = SimpleNamespace(
        uid="tb:1234#0",
        content_hash=sha256_id(code),
        code=code,
        tc_offset=0,
        ldst_end=0x40,
        pool_start=slot,
        pool_end=len(code),
        pool=(PoolEntry(site.field_off, slot, 2, 1, -4, _TARGET),) if pool else (),
        sites=(site,),
        bindings=Bindings(helpers=(HelperBinding(3, "test", "qemu", "helper_test", 5, _TARGET),)),
        reloc=RelocInfo(True, (), ()),
        insns=(Insn(0, (0, 0, 0), start, cont),),
        opmarks=(OpMark(start, OpMarkKind.CALL, 0, 3),),
        recovery=(RecoveryPoint("h", cont, 0, (0, 0, 0), "helper_call"),),
    )
    request = InlineCall(
        target=unit.uid,
        unit_hash=unit.content_hash,
        call_off=call,
        call_bytes=call_bytes,
        continuation_off=cont,
        helper_id=3,
        target_abs=_TARGET,
        pool_slot_off=slot,
        guest_pcs=(0x08000000,),
        image_id=sha256_id(b"image"),
        elf_address=0x1000,
        image_offset=0x2000,
        symbol="helper_test",
        callee_code=source,
        native_input_id=sha256_id(b"native input"),
        fragment=_leaf_fragment(source),
        tcg_typemask=0x12,
    )
    return unit, request


@pytest.mark.parametrize(("pool", "padding"), [(False, "90"), (True, "6690")])
def test_splice_retargets_argument_and_falls_through(pool, padding):
    unit, request = _case(pool=pool)
    splice = prepare_splice(unit, request, _BASE)
    assert splice.start_off == 0
    assert splice.prefix == _ARGUMENT
    assert splice.code == bytes.fromhex("8b4500 01c0 31ff " + padding)
    assert (len(splice.code) - request.continuation_off) % 8 == 0
    request = replace(request, splice=splice)
    verify_splice(unit, request, _BASE)
    verify_splice_body(request)


def test_splice_disp32_and_explicit_edi_move():
    argument = bytes.fromhex("8bbd34120000")
    source = bytes.fromhex("8bc7 bf01000000 01f8 c3")  # mov edi,1 kills before later use
    unit, request = _case(source=source, argument=argument)
    splice = prepare_splice(unit, request, _BASE)
    assert splice.code.startswith(bytes.fromhex("8b8534120000 bf01000000 01f8"))


@pytest.mark.parametrize("body", [
    "01f8 31ff",  # old EDI feeds ADD
    "4000f8 31ff",  # old DIL feeds ADD
    "40b701 31ff",  # partial write leaves rest of RDI differing
    "01c0",  # no full RDI overwrite before continuation
])
def test_uses_original_argument_when_fused_load_would_clobber_it(body):
    unit, request = _case(source=bytes.fromhex(f"89f8 {body} c3"))
    splice = prepare_splice(unit, request, _BASE)
    assert splice.start_off == request.call_off and not splice.prefix
    assert splice.code.startswith(request.callee_code[:-1])
    verify_splice_body(replace(request, splice=splice))


def test_refuses_unmodeled_abis_and_preserves_arbitrary_argument_load():
    unit, request = _case()
    for typemask in (None, 0, 0x32, 18.0, sum(2 << (3 * i) for i in range(8))):
        with pytest.raises(NativeInlineError) as exc:
            prepare_splice(unit, replace(request, tcg_typemask=typemask), _BASE)
        assert exc.value.reason == "splice_abi"
    unit, request = _case(argument=b"\x8b\x3e")  # mov edi,[rsi]
    splice = prepare_splice(unit, request, _BASE)
    assert splice.start_off == request.call_off and not splice.prefix
    assert splice.code.startswith(request.callee_code[4:-1])


@pytest.mark.parametrize("branch", ["7403", "0f8403000000"])
def test_branch_to_final_ret_reaches_fallthrough_and_rejects_mutation(branch):
    source = bytes.fromhex(f"f30f1efa 4889f8 4885f6 {branch} 4801f0 c3")
    unit, request = _case(source=source, argument=b"")
    request = replace(request, tcg_typemask=0x124)
    splice = prepare_splice(unit, request, _BASE)
    assert splice.start_off == request.call_off == 0
    assert splice.code.startswith(source[4:-1])
    instructions = list(decoder().disasm(splice.code, _BASE))
    assert instructions[2].mnemonic == "je"
    assert instructions[2].operands[0].imm == _BASE + len(source[4:-1])
    bad = replace(splice, code=splice.code[:6] + b"\x90" + splice.code[7:])
    with pytest.raises(NativeInlineError, match="differs"):
        verify_splice_body(replace(request, splice=bad))


def test_internal_loop_to_entry_keeps_endbr_for_rip_displacement():
    source = bytes.fromhex("f30f1efa ffcf 75f8 89f8 c3")
    unit, request = _case(source=source, argument=b"")
    splice = prepare_splice(unit, request, _BASE)
    assert splice.code.startswith(source[:-1])
    branch = next(i for i in decoder().disasm(splice.code, _BASE) if i.mnemonic == "jne")
    assert branch.operands[0].imm == _BASE


@pytest.mark.parametrize("instruction", [
    "66400fbed7",  # movsx dx,dil in QEMU helper_sxtb16
    "660fbed4",    # movsx dx,ah
    "66450fbec8",  # movsx r9w,r8b
])
def test_word_sign_extension_is_copied_with_exact_register_widths(instruction):
    machine = bytes.fromhex(instruction)
    unit, req = _case(source=machine + b"\xc3")
    splice = prepare_splice(unit, req, _BASE)
    assert not splice.prefix and splice.code.startswith(machine)
    verify_splice_body(replace(req, splice=splice))


@pytest.mark.parametrize("instruction", [
    "66400fbe17",    # byte memory source [rdi]
    "6466400fbed7",  # extra segment override
    "6666400fbed7",  # redundant operand-size override
    "66400fb6d7",    # MOVZX has a different operand-size-prefixed opcode
    "66400fbed4",    # reads SPL
    "66400fbee7",    # writes SP
])
def test_word_sign_extension_refuses_memory_extra_prefixes_and_stack(instruction):
    unit, req = _case(source=bytes.fromhex(instruction + "c3"))
    with pytest.raises(NativeInlineError) as exc:
        prepare_splice(unit, req, _BASE)
    assert exc.value.reason in {"unsupported_instruction", "unsupported_stack"}


def test_multiple_returns_require_a_different_splice_layout():
    source = bytes.fromhex("85ff 7401 c3 c3")
    unit, request = _case(source=source)
    bridge = NativeFragment(bytes.fromhex("85ff 7405 e900000000 e900000000"),
                            tuple(NativeOrigin(*o) for o in (
                                (0, 2, 0, 2), (2, 2, 2, 2), (4, 1, 4, 5), (5, 1, 9, 5))),
                            (5, 10))
    with pytest.raises(NativeInlineError) as exc:
        prepare_splice(unit, replace(request, fragment=bridge), _BASE)
    assert exc.value.reason == "splice_callee_control"


def test_native_six_integer_arguments_keep_all_registers_and_branch_outcomes():
    import ctypes
    import mmap
    import platform

    if platform.machine().lower() not in ("x86_64", "amd64") or not hasattr(mmap, "PROT_EXEC"):
        pytest.skip("requires executable x86-64 SysV memory")
    # Return arg1 if arg2 is zero; otherwise accumulate all six 64-bit args.
    source = bytes.fromhex("4889f8 4885f6 740f 4801f0 4801d0 4801c8 4c01c0 4c01c8 c3")
    unit, req = _case(source=source, argument=b"")
    req = replace(req, tcg_typemask=sum(4 << (3 * i) for i in range(7)))
    splice = prepare_splice(unit, req, _BASE)
    verify_splice(unit, replace(req, splice=splice), _BASE)

    # The original caller aligns the stack, calls the real helper, restores
    # RSP, increments its return value, and returns. Both callers use exactly
    # the same continuation. No stack adjustment is needed in the leaf itself.
    prologue = bytes.fromhex("4883ec08")
    tail = bytes.fromhex("4883c408 48ffc0 c3")
    call = b"\xe8" + len(tail).to_bytes(4, "little", signed=True)
    original = prologue + call + tail + source
    rewritten = prologue + splice.code + tail
    word = ctypes.c_uint64
    prototype = ctypes.CFUNCTYPE(word, *([word] * 6))
    with mmap.mmap(-1, mmap.PAGESIZE, prot=mmap.PROT_READ | mmap.PROT_WRITE | mmap.PROT_EXEC) as mem:
        mem[:len(original)] = original
        mem[128:128 + len(rewritten)] = rewritten
        base = ctypes.addressof(ctypes.c_char.from_buffer(mem))
        direct, inline = prototype(base), prototype(base + 128)
        for args in ((0, 0, 9, 8, 7, 6), (1, 2, 3, 4, 5, 6),
                     (2**63, 3, 2**62, 2**61, 2**60, 17),
                     (2**64 - 1, 1, 0, 0, 0, 0)):
            expected = ((sum(args) if args[1] else args[0]) + 1) % (1 << 64)
            assert direct(*args) == inline(*args) == expected


@pytest.mark.parametrize("before", [
    "eb03",            # Branch directly to the original CALL.
    "488d0500000000",  # LEA of the original argument instruction.
    "8b05feffffff",    # Load four bytes straddling the start of that instruction.
])
def test_preserves_argument_setup_if_only_the_wider_span_has_an_observer(before):
    unit, request = _case(before=bytes.fromhex(before))
    splice = prepare_splice(unit, request, _BASE)
    assert splice.start_off == request.call_off and splice.prefix == b""
    assert splice.code.startswith(request.callee_code[4:-1])
    candidate = replace(request, splice=splice)
    verify_splice_body(candidate)
    verify_splice(unit, candidate, _BASE)

    # Ask for the same fused certificate from an equivalent straight-line
    # caller and prove that replay against the observed caller still fails.
    other, other_req = _case(before=b"\x90" * len(bytes.fromhex(before)))
    fused = prepare_splice(other, other_req, _BASE)
    assert fused.prefix
    forged = replace(request, splice=fused)
    verify_splice_body(forged)  # its body alone is well formed
    with pytest.raises(NativeInlineError) as exc:
        verify_splice(unit, forged, _BASE)
    assert exc.value.reason == "splice_correspondence"


@pytest.mark.parametrize("before", [
    "488d0503000000",  # LEA observes the exact CALL entry bytes.
    "8b0501000000",    # Four-byte load starts in the MOV and crosses into the CALL.
])
def test_call_only_fallback_still_rejects_observation_of_call_encoding(before):
    unit, request = _case(before=bytes.fromhex(before))
    with pytest.raises(NativeInlineError) as exc:
        prepare_splice(unit, request, _BASE)
    assert exc.value.reason == "splice_incoming_reference"


def test_branch_to_load_entry_is_allowed():
    unit, request = _case(before=b"\xeb\x00")
    splice = prepare_splice(unit, request, _BASE)
    assert splice.start_off == 2


def test_call_only_fallback_rechecks_guest_and_recovery_boundaries():
    unit, request = _case()
    unit.recovery += (RecoveryPoint("other", request.call_off+2, 0, (0, 0, 0), "raddr"),)
    with pytest.raises(NativeInlineError) as exc:
        prepare_splice(unit, request, _BASE)
    assert exc.value.reason == "splice_origin"

    unit, request = _case()
    unit.insns = (Insn(0, (0, 0, 0), 0, request.call_off),
                  Insn(1, (4, 0, 0), request.call_off, request.continuation_off))
    # The original helper Site's guest owner must contain the CALL.
    with pytest.raises(NativeInlineError) as exc:
        prepare_splice(unit, request, _BASE)
    assert exc.value.reason == "splice_origin"
    unit.sites = (replace(unit.sites[0], insn_idx=1),)
    unit.opmarks = (replace(unit.opmarks[0], host_off=request.call_off, insn_idx=1),)
    unit.recovery = (replace(unit.recovery[0], insn=1, words=(4, 0, 0)),)
    splice = prepare_splice(unit, request, _BASE)
    assert not splice.prefix and splice.start_off == request.call_off
    verify_splice(unit, replace(request, splice=splice), _BASE)


def test_recomputes_bytes_and_padding_and_rechecks_original_prefix():
    unit, request = _case()
    splice = prepare_splice(unit, request, _BASE)
    for bad in (b"\x90" + splice.code[1:], splice.code[:-1] + b"\xcc"):
        with pytest.raises(NativeInlineError) as exc:
            verify_splice_body(replace(request, splice=replace(splice, code=bad)))
        assert exc.value.reason == "splice_correspondence"

    changed = replace(splice, prefix=bytes.fromhex("8b7d01"),
                      code=bytes.fromhex("8b4501") + splice.code[3:])
    verify_splice_body(replace(request, splice=changed))
    with pytest.raises(NativeInlineError) as exc:
        verify_splice(unit, replace(request, splice=changed), _BASE)
    assert exc.value.reason == "splice_correspondence"


def test_real_rbit_splice_replays_baseline_capture():
    evidence = os.environ.get("SCOPT_RBIT_SPLICE_EVIDENCE")
    if not evidence:
        pytest.skip("set SCOPT_RBIT_SPLICE_EVIDENCE to the bootstrap evidence root")

    from scopt.formats.scm_v1 import read_scm
    from scopt.importer.import_cache import open_snapshot

    root = Path(evidence)
    metadata = json.loads((root / "baseline.json").read_text())
    baseline = open_snapshot(metadata["snapshot"])
    helper_types = read_scm(metadata["capture"]).helpers
    source, = (root / "inline-final-reimport" / "snapshots").iterdir()
    previous = open_snapshot(source)
    matched = []
    for unit in previous.units():
        for request in unit.native_fragments:
            if request.symbol != "helper_rbit":
                continue
            request = replace(request, tcg_typemask=helper_types[request.helper_id].typemask)
            original = baseline.unit(request.target)
            splice = prepare_splice(original, request, baseline.header.code_address)
            matched.append(splice.start_off)
            assert splice.prefix == _ARGUMENT
            assert splice.code.startswith(bytes.fromhex("8b4500 0fc8"))
            assert len(splice.code) - (request.continuation_off - splice.start_off) == 72
            request = replace(request, splice=splice)
            verify_splice(original, request, baseline.header.code_address)
            verify_splice_body(request)
    assert len(set(matched)) == 2
