"""The complete UDIV expansion must preserve both zero outcomes and GETPC."""

import ctypes
from dataclasses import replace
import mmap
import platform
import shutil
import subprocess

import pytest

from scopt.canon import sha256_id
from scopt.decode.x86 import sweep, tiles
from scopt.model.native import FULL_UDIV_CLEANUP_KIND, FULL_UDIV_KIND
from scopt.validate_sym.native_inline import NativeInlineError
from scopt.validate_sym.native_udiv_full import (
    full_udiv_exception_call, prepare_full_udiv_splice, verify_full_udiv_body, verify_full_udiv_splice,
)
from tests.unit.test_native_udiv import _fixture


_IMAGE = "sha256:2da1d94f82da8f182282877b47959507b1282e11469e7b418fdf7913e99f28b6"


def _case(target=0x555555e236e0, *, slot=32):
    unit, req, ca = _fixture(slot=slot)
    code = unit.code[:unit.pool_start] + target.to_bytes(8, "little")
    site = replace(unit.sites[0], value=target)
    pool = replace(unit.pool[0], data0=target)
    helper = replace(unit.bindings.helpers[0], func=target)
    unit = replace(unit, code=code, sites=(site,), pool=(pool,),
                   bindings=replace(unit.bindings, helpers=(helper,)),
                   content_hash=sha256_id(unit.record + code + unit.search))
    req = replace(req, image_id=_IMAGE, image_offset=0x8cf6e0, elf_address=0x8cf6e0,
                  target_abs=target, unit_hash=unit.content_hash)
    return unit, req, ca


def test_full_udiv_has_one_child_call_and_an_exact_position_independent_guest_pc():
    unit, req, ca = _case()
    req = replace(req, splice=prepare_full_udiv_splice(unit, req, ca))
    verify_full_udiv_splice(unit, req, ca)
    assert req.growth == 96
    call_off, target = full_udiv_exception_call(req)
    assert target == req.target_abs - 0x210
    for placement in (0x1000, 0x7fff78000000):
        start = placement + req.call_off
        decoded = sweep(req.splice.code, start)
        assert tiles(decoded, start, start + len(req.splice.code))
        call, = (i for i in decoded if i.is_call)
        assert call.raw == bytes.fromhex("ff1424") and call.address - start == call_off
        assert not any(i.is_ret for i in decoded)
        pc, = (i for i in decoded if i.is_lea)
        assert pc.rip_target == placement + req.continuation_off + req.growth


def test_full_udiv_rejects_changed_cold_path_exception_image_and_nested_call():
    unit, req, ca = _case()
    for changed in (replace(req, image_id=sha256_id(b"another build")),
                    replace(req, elf_address=0x8cf6e4),
                    replace(req, callee_code=req.callee_code.replace(b"\x83\xe0\x10", b"\x83\xe0\x08")),
                    replace(req, callee_code=req.callee_code[:-1] + b"\x00")):
        with pytest.raises(NativeInlineError):
            prepare_full_udiv_splice(unit, changed, ca)
    splice = prepare_full_udiv_splice(unit, req, ca)
    for off in (3, 34, 42, 51, 63, 65):
        mutated = bytearray(splice.code)
        mutated[off] ^= 1
        with pytest.raises(NativeInlineError):
            verify_full_udiv_body(replace(req, splice=replace(splice, code=bytes(mutated))))


_DEFINE_SCRATCH = bytes.fromhex("89c2 89c1 89c6 89c7")


def _with_tail(unit, req, tail):
    start = req.continuation_off
    code = unit.code[:start] + tail + unit.code[start + len(tail):]
    digest = sha256_id(unit.record + code + unit.search)
    return replace(unit, code=code, content_hash=digest), replace(req, unit_hash=digest)


def test_full_cleanup_requires_original_and_relocated_register_liveness():
    unit, req, ca = _case()
    live, live_req = _with_tail(unit, req, bytes.fromhex("89d3") + _DEFINE_SCRATCH)
    unit, req = _with_tail(unit, req, _DEFINE_SCRATCH)
    baseline = prepare_full_udiv_splice(unit, req, ca)
    compact = prepare_full_udiv_splice(unit, req, ca, cleanup=True)
    assert baseline.kind == FULL_UDIV_KIND and compact.kind == FULL_UDIV_CLEANUP_KIND
    assert len(baseline.code) == len(compact.code) + 8
    assert compact.code.endswith(bytes.fromhex("89d1 89f0 31d2 f7f1 4531c0"))
    assert prepare_full_udiv_splice(live, live_req, ca, cleanup=True) == baseline
    compact_req = replace(req, splice=compact)
    verify_full_udiv_splice(unit, compact_req, ca)
    with pytest.raises(NativeInlineError):
        verify_full_udiv_splice(live, replace(live_req, splice=compact), ca)
    assert full_udiv_exception_call(compact_req) == full_udiv_exception_call(replace(req, splice=baseline))
    tc = ca + unit.tc_offset
    end = req.call_off + len(compact.code)
    prefix = unit.code[:req.call_off] + compact.code
    code = prefix + unit.code[req.continuation_off:unit.pool_start]
    verify_full_udiv_body(compact_req, code, end, (), tc)
    with pytest.raises(NativeInlineError):
        verify_full_udiv_body(compact_req)
    # Even a second safe point later in the same TB is not this continuation.
    with pytest.raises(NativeInlineError):
        verify_full_udiv_body(compact_req, code + _DEFINE_SCRATCH, len(code), (), tc)
    with pytest.raises(NativeInlineError):
        verify_full_udiv_body(compact_req, prefix + bytes.fromhex("89d3") + code[end:], end, (), tc)


_HARNESS = r"""
#include <stdint.h>
#include <setjmp.h>
struct Observation {
    uintptr_t env, code, syndrome, level, guest_pc, return_slot, outer_pc, child_return;
};
static _Thread_local struct Observation *obs;
static _Thread_local jmp_buf escape;
__attribute__((noinline,noreturn))
void trap_probe(void *env, unsigned code, unsigned syn, unsigned level, uintptr_t guest_pc) {
    uintptr_t *frame = __builtin_frame_address(0);
    *obs = (struct Observation){ (uintptr_t)env, code, syn, level, guest_pc,
                                 (uintptr_t)(frame + 1), frame[3], frame[1] };
    longjmp(escape, 1);
}
typedef uint32_t (*Guest)(void *, uint32_t, uint32_t);
uint64_t run(Guest guest, void *env, uint32_t num, uint32_t den, struct Observation *record) {
    obs = record;
    if (setjmp(escape)) return UINT64_C(1) << 32;
    return guest(env, num, den);
}
"""


@pytest.mark.skipif(platform.system() != "Linux" or platform.machine() != "x86_64",
                    reason="runs native x86-64 SysV machine code")
@pytest.mark.parametrize("cleanup", [False, True])
def test_full_udiv_matches_original_zero_result_and_exception_registers_and_stack(tmp_path, cleanup):
    compiler = shutil.which("cc")
    if not compiler:
        pytest.skip("a C compiler is needed for the nonlocal-exit oracle")
    source, library = tmp_path / "probe.c", tmp_path / "probe.so"
    source.write_text(_HARNESS)
    subprocess.run([compiler, "-shared", "-fPIC", "-O0", "-fno-omit-frame-pointer",
                    "-fcf-protection=branch", "-o", str(library), str(source)], check=True)
    lib = ctypes.CDLL(str(library))
    lib.run.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    lib.run.restype = ctypes.c_uint64
    probe = ctypes.cast(lib.trap_probe, ctypes.c_void_p).value
    observation = ctypes.c_size_t * 8
    env = ctypes.create_string_buffer(0x14000)
    envp = ctypes.addressof(env)
    with mmap.mmap(-1, mmap.PAGESIZE, prot=mmap.PROT_READ | mmap.PROT_WRITE) as page:
        base = ctypes.addressof(ctypes.c_char.from_buffer(page))
        original, rewritten = 0, 0x100
        # Preserve the ELF-relative call displacement between UDIV and its
        # noreturn child. Replace only the *child* with an observation probe.
        helper, child = 0x6e0, 0x4d0
        unit, req, ca = _case(base + helper)
        unit, req = _with_tail(unit, req, _DEFINE_SCRATCH)
        splice = prepare_full_udiv_splice(unit, req, ca, cleanup=cleanup)
        assert splice.kind == (FULL_UDIV_CLEANUP_KIND if cleanup else FULL_UDIV_KIND)
        shim = bytes.fromhex("f30f1efa 49bb") + probe.to_bytes(8, "little") + bytes.fromhex("41ffe3")
        page[child:child+len(shim)] = shim
        page[helper:helper+len(req.callee_code)] = req.callee_code
        prefix = b"\x90" * (req.call_off-4) + bytes.fromhex("4883ec08")
        tail = _DEFINE_SCRATCH + bytes.fromhex("4883c408 c3")
        orig_code = prefix + req.call_bytes + tail
        new_code = prefix + splice.code + tail
        page[original:original+len(orig_code)] = orig_code
        page[req.pool_slot_off:req.pool_slot_off+8] = (base + helper).to_bytes(8, "little")
        page[rewritten:rewritten+len(new_code)] = new_code
        assert ctypes.CDLL(None).mprotect(ctypes.c_void_p(base), ctypes.c_size_t(mmap.PAGESIZE),
                                          mmap.PROT_READ | mmap.PROT_EXEC) == 0
        for m, secure, ccr0, ccr1 in ((0, 0, 16, 16), (2, 0, 0, 16), (2, 1, 16, 0),
                                      (2, 0, 16, 0), (2, 1, 0, 16)):
            ctypes.c_uint8.from_address(envp + 0x132e9).value = m
            ctypes.c_uint32.from_address(envp + 0xc60).value = secure
            ctypes.c_uint32.from_address(envp + 0xc14).value = ccr0
            ctypes.c_uint32.from_address(envp + 0xc18).value = ccr1
            for numerator, divisor in ((0,1), (2**32-1,1), (2**32-1,3), (81,9), (42,0)):
                traps = not divisor and m and (ccr0, ccr1)[secure] & 16
                expected = 1 << 32 if traps else numerator // divisor if divisor else 0
                baseline_obs, candidate_obs = observation(), observation()
                assert lib.run(base + original, envp, numerator, divisor, baseline_obs) == expected
                assert lib.run(base + rewritten, envp, numerator, divisor, candidate_obs) == expected
                if traps:
                    assert tuple(baseline_obs[:4]) == tuple(candidate_obs[:4]) == (envp,23,0,1)
                    assert baseline_obs[4] == baseline_obs[6] == base + original + req.continuation_off
                    assert candidate_obs[4] == candidate_obs[6] == base + rewritten + req.call_off + len(splice.code)
                    assert baseline_obs[5] == candidate_obs[5] and candidate_obs[5] % 16 == 8
                    assert baseline_obs[7] == base + helper + len(req.callee_code)
                    off, _ = full_udiv_exception_call(replace(req, splice=splice))
                    assert candidate_obs[7] == base + rewritten + req.call_off + off + 3
                else:
                    assert not any(baseline_obs) and not any(candidate_obs)


@pytest.fixture
def run_udiv_pass(tmp_path, monkeypatch):
    """Exercise real candidate sizing and physical layout, isolating ELF lookup."""
    from types import SimpleNamespace
    from scopt.analysis.api import AnalysisSpec
    from scopt.analysis.native_callees import NativeCallBinding
    from scopt.analysis.registry import AnalysisRegistry
    from scopt.analysis.store import AnalysisInput
    from scopt.manager import PassManager
    from scopt.passes import host_inline
    from tests.synth.fake_snapshot import FakeSnapshot, fake_snapshot

    bridge = tmp_path / "bridge"
    bridge.write_bytes(b"test bridge")
    monkeypatch.setenv("SCOPT_HOST_INLINE_BRIDGE", str(bridge))
    monkeypatch.setattr(host_inline, "_bridge_call", lambda *_: b"scopt-native bridge 1 (LLVM/BOLT 20.1.2)")
    identity = sha256_id(b"size-limited UDIV")
    monkeypatch.setattr(host_inline, "prepare_native_images", lambda *a, **kw: AnalysisInput(identity, ()))

    def run(unit, req, *, count=1, **params):
        base = fake_snapshot(count)
        units = [replace(unit, uid=u.uid, slot=u.slot) for u in base.units()]
        snap = FakeSnapshot(units, base.snapshot_id, base.header, runtime={"supercache_flat": False})
        callee = SimpleNamespace(symbol=req.symbol, role="qemu", abi="sysv-amd64",
                                 elf_address=req.elf_address, image_offset=req.image_offset, image_id=req.image_id,
                                 code=req.callee_code, size=len(req.callee_code), runtime_address=req.target_abs,
                                 tcg_typemask=req.tcg_typemask)

        def resolve(ctx, scope):
            ctx.external_input("native-images")
            ctx.certify()
            call = SimpleNamespace(symbol=req.symbol, helper_name="udiv", helper_id=req.helper_id,
                                   helper_flags=1, uid=scope.id, guest_pcs=req.guest_pcs,
                                   original_bytes=req.call_bytes, host_off=req.call_off,
                                   continuation_off=req.continuation_off, pool_slot_off=req.pool_slot_off)
            return (NativeCallBinding(call, callee, None, identity),)

        analyses = AnalysisRegistry([
            AnalysisSpec("envlayout", 1, "snapshot", (), frozenset(), lambda *_: {"bound": True}, dict),
            AnalysisSpec("native-callees", 1, "unit", (), frozenset({"external:native-images"}), resolve, tuple),
        ])
        return PassManager(snap, "host-inline-splice", analyses=analyses,
                           params={"host-inline-splice": params}).execute()

    return run


def test_auto_retries_smaller_udiv_when_growth_is_limited(run_udiv_pass):
    from scopt.model.native import UDIV_CLEANUP_KIND

    unit, req, _ = _case()
    unit, req = _with_tail(unit, req, _DEFINE_SCRATCH)

    def forms(run):
        return [e.payload.splice.kind for e in run.edits]

    assert forms(run_udiv_pass(unit, req, max_growth_per_unit=64)) == [UDIV_CLEANUP_KIND]
    assert forms(run_udiv_pass(unit, req, max_growth_per_unit=20)) == []
    # The first block consumes 88 bytes. The second can still grow by 24.
    assert forms(run_udiv_pass(unit, req, count=2, max_total_growth=112)) == [
        FULL_UDIV_CLEANUP_KIND, UDIV_CLEANUP_KIND]
    assert forms(run_udiv_pass(unit, req, udiv_mode="full", max_growth_per_unit=64)) == []
    assert forms(run_udiv_pass(unit, req, udiv_mode="guarded", max_growth_per_unit=64)) == ["guarded-udiv"]


def test_auto_retries_smaller_udiv_when_a_short_branch_cannot_reach(run_udiv_pass):
    from scopt.formats.enums import Encoding, SiteKind
    from scopt.model.native import UDIV_CLEANUP_KIND
    from scopt.model.unit import Site

    unit, req, ca = _case(slot=96)
    unit, req = _with_tail(unit, req, _DEFINE_SCRATCH)
    # JZ at zero targets +65. Its original displacement 63 fits after adding
    # the guarded form's 24 bytes, but cannot encode the full form's +88.
    code = b"\x74\x3f\x90" + unit.code[3:]
    branch = Site(1, 0, 1, Encoding.REL8, SiteKind.LABEL_JCC, 0,
                  ca + unit.tc_offset + 65, 0, 63)
    digest = sha256_id(unit.record + code + unit.search)
    unit = replace(unit, code=code, sites=(branch, *unit.sites), content_hash=digest)
    req = replace(req, unit_hash=digest)
    auto = run_udiv_pass(unit, req)
    assert [e.payload.splice.kind for e in auto.edits] == [UDIV_CLEANUP_KIND]
    forced = run_udiv_pass(unit, req, udiv_mode="full")
    assert not forced.edits and forced.passes[0].stats["sites:splice_reference_range"] == 1
    # Missing original return-PC provenance must still refuse every form.
    bad = run_udiv_pass(replace(unit, recovery=()), req)
    assert not bad.edits and bad.passes[0].stats["sites:splice_origin"] == 1
