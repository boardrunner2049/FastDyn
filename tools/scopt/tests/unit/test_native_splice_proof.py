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
def test_refuses_live_argument(body):
    unit, request = _case(source=bytes.fromhex(f"89f8 {body} c3"))
    with pytest.raises(NativeInlineError) as exc:
        prepare_splice(unit, request, _BASE)
    assert exc.value.reason == "splice_argument_live"


def test_refuses_wrong_argument_type_and_load():
    unit, request = _case()
    for typemask in (None, 2, 0x22, 0x92, 18.0):
        with pytest.raises(NativeInlineError) as exc:
            prepare_splice(unit, replace(request, tcg_typemask=typemask), _BASE)
        assert exc.value.reason == "splice_abi"
    unit, request = _case(argument=b"\x8b\x3e")  # mov edi,[rsi]
    with pytest.raises(NativeInlineError) as exc:
        prepare_splice(unit, request, _BASE)
    assert exc.value.reason == "splice_argument_load"


@pytest.mark.parametrize("before", ["eb03", "488d0503000000"])
def test_refuses_incoming_branch_or_rip_reference_to_call(before):
    # Both the jump and the RIP-relative LEA target the canonical CALL
    # instruction boundary. Starting at that old location skips the load.
    unit, request = _case(before=bytes.fromhex(before))
    with pytest.raises(NativeInlineError) as exc:
        prepare_splice(unit, request, _BASE)
    assert exc.value.reason == "splice_incoming_reference"


@pytest.mark.parametrize("before", ["488d0500000000", "8b05feffffff"])
def test_refuses_rip_data_reference_at_or_straddling_entry(before):
    unit, request = _case(before=bytes.fromhex(before))
    with pytest.raises(NativeInlineError) as exc:
        prepare_splice(unit, request, _BASE)
    assert exc.value.reason == "splice_incoming_reference"


def test_branch_to_load_entry_is_allowed():
    unit, request = _case(before=b"\xeb\x00")
    splice = prepare_splice(unit, request, _BASE)
    assert splice.start_off == 2


def test_refuses_interior_recovery_and_split_guest_origin():
    unit, request = _case()
    unit.recovery += (RecoveryPoint("other", 2, 0, (0, 0, 0), "raddr"),)
    with pytest.raises(NativeInlineError) as exc:
        prepare_splice(unit, request, _BASE)
    assert exc.value.reason == "splice_origin"

    unit, request = _case()
    unit.insns = (Insn(0, (0, 0, 0), 0, request.call_off),
                  Insn(1, (4, 0, 0), request.call_off, request.continuation_off))
    with pytest.raises(NativeInlineError) as exc:
        prepare_splice(unit, request, _BASE)
    assert exc.value.reason == "splice_origin"


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
