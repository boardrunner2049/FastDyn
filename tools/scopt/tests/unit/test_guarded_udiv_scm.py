"""Guarded UDIV sidecar serialization, physical correspondence and recovery."""

import pytest

from dataclasses import replace
from types import SimpleNamespace

from scopt.decode.search import decode_search, is_pcrel, unwind
from scopt.decode.x86 import sweep
from scopt.formats.layouts import SCM_TB_CHUNK_VERSION_4
from scopt.formats.sc_records import ScRecord
from scopt.formats.scm_v1 import ScmInsn, ScmOpMark, ScmPool, TbChunk, parse_scm
from scopt.importer.crosscheck import _native_splice_error
from scopt.link import _scm_sites
from scopt.splice_layout import materialize_splice
from scopt.validate_sym.native_udiv import prepare_udiv_splice, prepare_reciprocal29
from tests.synth import builders
from tests.unit.test_native_udiv import _fixture


@pytest.mark.parametrize("prepare", [prepare_udiv_splice, prepare_reciprocal29])
def test_derived_capture_rejects_fallback_recovery_and_binding_mutations(prepare):
    unit, req, base = _fixture()
    req = replace(req, splice=prepare(unit, req, base))
    out = materialize_splice(unit, req, base)
    raw, = parse_scm(builders.build_scm(builders.simple_units(1))).tbs.values()
    mark, = out.opmarks
    pool, = out.pool
    chunk = replace(raw, record=out.record, tb_chunk_version=SCM_TB_CHUNK_VERSION_4, reserved=req.call_off,
                    native_fragments=(req,), vaddr=req.guest_pcs[0], guest_bytes=bytes(8),
                    insns=tuple(ScmInsn(i.words, i.host_start, i.host_end) for i in out.insns),
                    pool_start=out.pool_start, insn0_start=out.insn0_start, body_end=out.body_end,
                    ldst_end=out.ldst_end, code_size=out.tc_size, sites=_scm_sites(out),
                    opmarks=(ScmOpMark(mark.host_off, mark.kind, mark.insn_idx, mark.helper_id),),
                    pool=(ScmPool(pool.user_field_off, pool.slot_off, pool.rtype, pool.nlong,
                                  pool.addend, pool.data0),))
    helper = SimpleNamespace(name_str="udiv", tcg_flags=1, typemask=1202, func=req.target_abs)
    context = SimpleNamespace(helper=lambda _: helper)
    tc = base + out.tc_offset
    def check(ch, code=out.code):
        physical = sweep(code[:out.pool_start], tc)
        return _native_splice_error(code, ch, context, tc, physical, 10)

    parsed, remainder = TbChunk.decode(chunk.encode())
    assert parsed == chunk and not remainder
    assert check(parsed) is None
    # The loader's unwind (GETPC_ADJ) must recover guest zero from the actual
    # instruction end; moving its search boundary to the guard breaks this.
    record = ScRecord.from_bytes(out.record)
    rows = decode_search(out.search, out.icount, pcrel=is_pcrel(record.cflags), pc=record.pc)
    end = mark.host_off + 6
    assert unwind(rows, end) == (0, unit.insns[0].words)
    wrong = (replace(rows[0], host_end=req.call_off), rows[1])
    assert unwind(wrong, end) == (1, unit.insns[1].words)
    for offset in (req.call_off+2, mark.host_off, mark.host_off+2, out.pool_start):
        altered = bytearray(out.code)
        altered[offset] ^= 1
        assert check(parsed, bytes(altered)) is not None
    for changed in (
        replace(chunk, sites=()),
        replace(chunk, opmarks=()),
        replace(chunk, pool=()),
        replace(chunk, sites=(replace(chunk.sites[0], insn_off=mark.host_off-1),)),
        replace(chunk, opmarks=(replace(chunk.opmarks[0], insn_idx=1),)),
        replace(chunk, pool=(replace(chunk.pool[0], slot_off=pool.slot_off-8),)),
    ):
        assert check(changed) is not None
    helper.tcg_flags = 5
    assert check(chunk) is not None
