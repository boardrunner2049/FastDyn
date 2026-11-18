"""Derived splice capture metadata and physical cross-checks."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from scopt.decode.x86 import sweep
from scopt.formats.layouts import SCM_TB_CHUNK_VERSION_3, SCM_TB_CHUNK_VERSION_4
from scopt.formats.scm_v1 import ScmEncodeError, ScmInsn, TbChunk, parse_scm
from scopt.importer.crosscheck import _native_splice_error, _relocated_splice_code
from scopt.model.native import InlineCall, NativeFragment, NativeOrigin, NativeRelocation, NativeSplice
from scopt.validate_sym import native_virtual as framed

from tests.synth import builders as B


def _fixture():
    raw = next(iter(parse_scm(B.build_scm(B.simple_units(1))).tbs.values()))
    identity = "sha256:" + "2" * 64
    arithmetic = b"\x83\xc0\x01" * 4 + b"\x31\xff"
    callee = b"\x89\xf8" + arithmetic + b"\xc3"
    fragment = NativeFragment(callee[:-1] + b"\xe9\0\0\0\0",
                              tuple(NativeOrigin(i.address, i.size, i.address,
                                                 5 if i.is_ret else i.size)
                                    for i in sweep(callee, 0)), (len(callee),))
    prefix, new_prefix = b"\x8b\x7d\x10", b"\x8b\x45\x10"
    start, call_off, cont = 8, 11, 17
    old_pool = 24
    call = b"\xff\x15" + (old_pool - cont).to_bytes(4, "little", signed=True)
    splice = NativeSplice(start, prefix, new_prefix + arithmetic)
    helper = 0xaabb_ccdd
    request = InlineCall(target="tb:fixture#0", unit_hash=identity, call_off=call_off,
                         call_bytes=call, continuation_off=cont, helper_id=2,
                         target_abs=helper, pool_slot_off=old_pool, guest_pcs=(raw.vaddr,),
                         image_id=identity, elf_address=0x3000, image_offset=0x1000,
                         symbol="helper_fixture", callee_code=callee,
                         native_input_id=identity, fragment=fragment,
                         bridge_sha256=identity, splice=splice, tcg_typemask=18)
    assert request.growth == 8
    pool_start = old_pool + request.growth
    code = b"\x90" * start + splice.code
    code += b"\x90" * (pool_start - len(code)) + helper.to_bytes(8, "little")
    span = ScmInsn(raw.insns[0].words, 0, pool_start)
    chunk = replace(raw, insn0_start=0, insns=(span,), sites=(), pool=(), opmarks=(),
                    body_end=pool_start, ldst_end=pool_start,
                    pool_start=pool_start, code_size=len(code),
                    tb_chunk_version=SCM_TB_CHUNK_VERSION_4, reserved=start,
                    native_fragments=(request,))
    context = SimpleNamespace(helper=lambda _: SimpleNamespace(func=helper))
    return chunk, code, context


def _check(chunk, code, context):
    address = 0x1000
    physical = sweep(code[:chunk.pool_start], address)
    return _native_splice_error(code, chunk, context, address, physical, 10)


def test_splice_v4_preserves_certificate_and_uses_shifted_pool():
    chunk, code, context = _fixture()
    parsed, pad = TbChunk.decode(chunk.encode())
    assert not pad and parsed == chunk
    assert parsed.inline_start is None and parsed.pool_end == len(code)
    assert parsed.splice_start == 8
    assert parsed.native_fragments[0].pool_slot_off == 24
    assert _check(parsed, code, context) is None
    with pytest.raises(ScmEncodeError, match="v3_native_layout"):
        replace(chunk, tb_chunk_version=SCM_TB_CHUNK_VERSION_3).encode()
    with pytest.raises(ScmEncodeError, match="v4_native_layout"):
        replace(chunk, reserved=chunk.splice_start + 1).encode()


def test_splice_v4_rejects_interior_entry_bad_bytes_and_wrong_guest_origin():
    chunk, code, context = _fixture()
    jump_to_interior = b"\xe9\x06\0\0\0" + code[5:]
    assert "outside reference" in _check(chunk, jump_to_interior, context)
    # MOV EAX,[RIP+1] at offset 0 reads four bytes beginning one byte before the splice.
    straddling_read = b"\x8b\x05\x01\0\0\0" + code[6:]
    assert "RIP-relative access overlaps" in _check(chunk, straddling_read, context)
    bad_body = code[:8] + b"\x90" + code[9:]
    assert "bytes or instruction boundaries" in _check(chunk, bad_body, context)
    request, = chunk.native_fragments
    wrong = replace(request, guest_pcs=(request.guest_pcs[0] + 2,))
    assert "guest origin" in _check(replace(chunk, native_fragments=(wrong,)), code, context)


def _frame_fixture():
    chunk, _, _ = _fixture()
    old, = chunk.native_fragments
    base = 0x1000
    func = 0x50000000 + framed._BENCHMARK_START
    start = 8
    continuation = start + 5
    disp = (func - (base + continuation)).to_bytes(4, "little", signed=True)
    request = replace(old, call_off=start, call_bytes=b"\xe8" + disp,
                      continuation_off=continuation, target_abs=func, pool_slot_off=None,
                      image_id=framed._PLUGIN_IMAGE_ID, elf_address=framed._BENCHMARK_START,
                      image_offset=framed._BENCHMARK_START, symbol="benchmark_start",
                      callee_code=framed._BENCHMARK_START_CODE, tcg_typemask=400,
                      fragment=NativeFragment(b"", (), ()), splice=None)
    splice = framed._build_frame(request)
    request = replace(request, splice=splice)
    code = b"\x90" * start + _relocated_splice_code(splice, base) + b"\x90"
    span = ScmInsn(chunk.insns[0].words, 0, len(code))
    chunk = replace(chunk, insns=(span,), body_end=len(code), ldst_end=len(code),
                    pool_start=len(code), code_size=len(code), native_fragments=(request,))
    ctx = SimpleNamespace(helper=lambda _: SimpleNamespace(func=func))
    return chunk, code, ctx


def test_frame_v4_roundtrip_and_checks_physical_relocations():
    chunk, code, context = _frame_fixture()
    parsed, pad = TbChunk.decode(chunk.encode())
    assert not pad and parsed == chunk
    request, = parsed.native_fragments
    splice = request.splice
    assert splice.kind == "frame"
    assert len(splice.relocations) == 4
    for row in splice.relocations:
        field = splice.start_off + row.field_off
        displacement = int.from_bytes(code[field:field + 4], "little", signed=True)
        assert 0x1000 + splice.start_off + row.next_off + displacement == row.target_abs
    assert _check(parsed, code, context) is None
    field = splice.start_off + splice.relocations[0].field_off
    altered = code[:field] + bytes([code[field] ^ 1]) + code[field + 1:]
    assert "bytes or instruction boundaries" in _check(parsed, altered, context)


def test_frame_v4_rejects_overlapping_or_noncanonical_relocations():
    chunk, _, _ = _frame_fixture()
    request, = chunk.native_fragments
    splice = request.splice
    first, *rest = splice.relocations
    for changed in (
        replace(splice, relocations=(first, *splice.relocations)),
        replace(splice, code=splice.code[:first.field_off] + b"\x01" +
                splice.code[first.field_off + 1:]),
    ):
        with pytest.raises(ScmEncodeError, match="v4_native_layout"):
            replace(chunk, native_fragments=(replace(request, splice=changed),)).encode()
    with pytest.raises(ScmEncodeError, match="v4_native_layout"):
        replace(chunk, native_fragments=(replace(request, tcg_typemask=0),)).encode()
    outside = NativeRelocation(first.field_off, first.next_off, 1 << 63)
    with pytest.raises(ValueError, match="signed rel32"):
        _relocated_splice_code(replace(splice, relocations=(outside, *rest)), 0x1000)


def test_v4_depth_is_canonical_and_limited_by_splice_kind():
    chunk, _, _ = _frame_fixture()
    request, = chunk.native_fragments
    assert "depth" not in request.splice.to_json()
    deep = replace(request, splice=replace(request.splice, depth=2))
    changed = replace(chunk, native_fragments=(deep,))
    assert TbChunk.decode(changed.encode())[0] == changed
    assert deep.splice.to_json()["depth"] == 2
    for depth in (True, 0, 3):
        changed = replace(chunk, native_fragments=(replace(
            request, splice=replace(request.splice, depth=depth)),))
        with pytest.raises(ScmEncodeError, match="v4_native_(data|layout)"):
            changed.encode()

    integer, _, _ = _fixture()
    request, = integer.native_fragments
    changed = replace(integer, native_fragments=(replace(
        request, splice=replace(request.splice, depth=2)),))
    with pytest.raises(ScmEncodeError, match="v4_native_(data|layout)"):
        changed.encode()
