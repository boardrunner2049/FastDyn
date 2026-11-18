"""Independent native inlining proof tests, without a BOLT dependency."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from scopt.canon import sha256_id
from scopt.formats.enums import Encoding, SiteKind
from scopt.model.native import InlineCall, NativeFragment, NativeOrigin
from scopt.model.unit import Bindings, Fixup, HelperBinding, PoolEntry, RelocInfo, Site
from scopt.validate_sym.native_inline import (
    NativeInlineError,
    check_leaf,
    verify_fragment,
    verify_site,
)

_ADDR = 0x12345000
_TARGET = _ADDR + 0x1000
_SOURCE = bytes.fromhex("01f8 7501 c3 c3")
_FRAGMENT = NativeFragment(
    code=bytes.fromhex("01f8 0f8505000000 e900000000 e900000000"),
    origins=(
        NativeOrigin(source_off=0, source_size=2, output_off=0, output_size=2),
        NativeOrigin(source_off=2, source_size=2, output_off=2, output_size=6),
        NativeOrigin(source_off=4, source_size=1, output_off=8, output_size=5),
        NativeOrigin(source_off=5, source_size=1, output_off=13, output_size=5),
    ),
    continuation_fields=(9, 14),
)


def _instruction_case(*, pool=False, prefix=bytes.fromhex("89442408")):
    # The default prefix is a caller spill at [rsp+8], in the admitted frame.
    offset = len(prefix)
    slot = 0x20 if pool else None
    opcode = bytes.fromhex("ff15" if pool else "e8")
    cont = offset + len(opcode) + 4
    disp = slot - cont if pool else _TARGET - (_ADDR + cont)
    code = prefix + opcode + disp.to_bytes(4, "little", signed=True)
    code = code.ljust(0x20, bytes.fromhex("90"))
    if pool:
        code += _TARGET.to_bytes(8, "little")

    uid = "tb:1234#0"
    helper = HelperBinding(7, "leaf", "qemu", "helper_leaf", 5, _TARGET)
    field_offset = offset + len(opcode)
    site = Site(
        field_off=field_offset,
        insn_off=offset,
        width=4,
        enc=Encoding.POOL_INDIRECT if pool else Encoding.REL32,
        kind=SiteKind.HELPER_CALL,
        aux=7,
        value=_TARGET,
        insn_idx=0,
        final_field=disp & 0xFFFFFFFF,
    )
    pool_rows = (PoolEntry(field_offset, slot, 2, 1, -4, _TARGET),) if pool else ()
    unit = SimpleNamespace(
        uid=uid,
        content_hash=sha256_id(code),
        code=code,
        tc_offset=0,
        ldst_end=0x20,
        pool_start=slot,
        pool_end=len(code),
        pool=pool_rows,
        sites=(site,),
        bindings=Bindings(helpers=(helper,)),
        reloc=RelocInfo(True, (), ()),
    )
    request = InlineCall(
        target=unit.uid,
        unit_hash=unit.content_hash,
        call_off=offset,
        call_bytes=code[offset:cont],
        continuation_off=cont,
        helper_id=helper.id,
        target_abs=_TARGET,
        pool_slot_off=slot,
        guest_pcs=(0x8000000,),
        image_id=sha256_id(b"test image"),
        elf_address=0x1000,
        image_offset=0x2000,
        symbol=helper.symbol,
        callee_code=_SOURCE,
        native_input_id=sha256_id(b"test runtime binding"),
        fragment=_FRAGMENT,
    )
    return unit, request


def _place(request, island_start):
    code = bytearray(request.fragment.code)
    for offset in request.fragment.continuation_fields:
        disp = request.continuation_off - (island_start + offset + 4)
        code[offset:offset + 4] = disp.to_bytes(4, "little", signed=True)
    return bytes(code)


@pytest.mark.parametrize("pool", [False, True])
def test_call_and_fragment_correspondence(pool):
    unit, request = _instruction_case(pool=pool)
    verify_site(unit, request, _ADDR)
    verify_fragment(request)
    verify_fragment(request, _place(request, 0x300), island_start=0x300)


def test_final_placement_detects_corrupted_return_and_base():
    _, request = _instruction_case()
    start = 0x300
    island = _place(request, start)
    corrupted = bytearray(island)
    corrupted[request.fragment.continuation_fields[1]] ^= 1
    with pytest.raises(NativeInlineError, match="continuation"):
        verify_fragment(request, bytes(corrupted), island_start=start)
    with pytest.raises(NativeInlineError, match="continuation"):
        verify_fragment(request, island, island_start=start + 1)


@pytest.mark.parametrize(("index", "byte", "message"), [
    (0, 0x29, "instruction bytes differ"),
    (3, 0x84, "internal branch successor differs"),
    (4, 0x06, "internal branch successor differs"),
])
def test_neutrality_mutations(index, byte, message):
    _, request = _instruction_case()
    island = bytearray(request.fragment.code)
    island[index] = byte
    with pytest.raises(NativeInlineError, match=message):
        verify_fragment(request, bytes(island), island_start=0)


def test_missing_origins_and_relocation_mutations():
    _, request = _instruction_case()
    fragment = request.fragment
    with pytest.raises(NativeInlineError, match="added or dropped"):
        verify_fragment(replace(request, fragment=replace(fragment, origins=fragment.origins[1:])))
    mutated = replace(fragment.origins[2], source_size=2)
    with pytest.raises(NativeInlineError, match="lengths differ"):
        verify_fragment(replace(request, fragment=replace(
            fragment, origins=(*fragment.origins[:2], mutated, fragment.origins[3])
        )))
    with pytest.raises(NativeInlineError, match="relocation fields"):
        verify_fragment(replace(request, fragment=replace(fragment, continuation_fields=(9,))))


def test_call_bytes_and_pool_binding_must_be_current():
    unit, request = _instruction_case(pool=True)
    tampered = replace(request, call_bytes=request.call_bytes[:-1] + bytes([0x05]))
    with pytest.raises(NativeInlineError) as err:
        verify_site(unit, tampered, _ADDR)
    assert err.value.reason == "stale_inline_call"

    unit.reloc = RelocInfo(True, (), (Fixup(
        request.pool_slot_off, 8, "abs_plus_delta", int(SiteKind.HELPER_CALL),
        "helper_ptr", _TARGET,
    ),))
    with pytest.raises(NativeInlineError) as err:
        verify_site(unit, request, _ADDR)
    assert err.value.reason == "call_binding_unknown"


@pytest.mark.parametrize("prefix", [
    "8b4424f8",  # mov eax, [rsp-8]
    "488d442408",  # lea rax, [rsp+8] escapes the stack
    "4883ec08",  # sub rsp, 8
    "eb01",  # jmp into the middle of the following CALL
])
def test_caller_may_not_observe_return_slot_or_jump_into_replaced_call(prefix):
    unit, request = _instruction_case(prefix=bytes.fromhex(prefix))
    with pytest.raises(NativeInlineError) as err:
        verify_site(unit, request, _ADDR)
    assert err.value.reason in ("unsupported_stack", "unsupported_control_flow")


@pytest.mark.parametrize(("code", "reason"), [
    ("8b4424f8 c3", "unsupported_stack"),
    ("8b07 c3", "unsupported_memory"),
    ("e800000000 c3", "unsupported_control_flow"),
    ("ffe7 c3", "unsupported_control_flow"),
    ("cc c3", "unsupported_instruction"),
    ("eb01 90 c3", "unsupported_control_flow"),  # unreachable NOP
])
def test_leaf_refusals(code, reason):
    with pytest.raises(NativeInlineError) as err:
        check_leaf(bytes.fromhex(code))
    assert err.value.reason == reason
