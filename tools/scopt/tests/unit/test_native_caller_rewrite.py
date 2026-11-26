"""Caller-only B4 edits compose with an imported integer splice's SCM certificate."""
import platform
import shutil
import subprocess
from dataclasses import replace

import pytest

from scopt.analysis.hcfg import build_hcfg
from scopt.analysis.hostops import build_hostops
from scopt.analysis.obs_contract import build_contract
from scopt.analysis.syncdead import nop_bytes, syncdead_unit
from scopt.formats.envlayout_v1 import find_envlayout
from scopt.formats import scm_v1 as S
from scopt.formats.enums import Encoding, OpMarkKind, SiteKind
from scopt.formats.sc_records import write_sc
from scopt.importer.import_cache import import_snapshot
from scopt.link import B3Plan, LinkError, NativeLinker, NativeLinkerV2, rewrite_unit
from scopt.model.snapshot import WorkingSet
from scopt.passes.base import Patch
from scopt.splice_layout import prepare_splice_layout
from scopt.validate import write_b3_files
from scopt.validate_sym.equiv import check_equiv
from scopt.validate_sym.link_check import placed_program
from scopt.validate_sym.program import program_of, world_of
from scopt.validate_sym.native_splice import prepare_splice
from scopt.canon import sha256_id
from scopt.importer.native_callee import NativeCalleeResolver
from tests.unit.test_native_callee import _fixture as bind_image
from tests.synth import builders as B
from tests.unit.test_native_splice_scm import _fixture


def test_b4_caller_rewrite_after_splice_reimport(tmp_path):
    # Three successive stores to one field, with no intervening observation;
    # erase its first two writes in separate generations around a verified splice.
    store = bytes.fromhex("c7451001000000")
    arg = bytes.fromhex("8b7d18")
    if platform.machine() != "x86_64" or platform.system() != "Linux" or not shutil.which("cc"):
        pytest.skip("requires an x86-64 Linux assembler")
    chunk, _, _ = _fixture()
    template, = chunk.native_fragments
    image = tmp_path / "native.so"
    assembly = (".text\n.global helper_test\n.type helper_test,@function\nhelper_test:\n.byte "
                + ",".join(str(b) for b in template.callee_code)
                + "\n.size helper_test, .-helper_test\n")
    subprocess.run(["cc", "-shared", "-nostdlib", "-x", "assembler", "-o", image, "-"],
                   input=assembly, text=True, check=True)
    binding, context, run_info, runtime = bind_image(image)
    native = NativeCalleeResolver(context, run_info, runtime, sha256_id(b"fixture")).resolve(binding)
    mapping_shift = 1 << 32  # import requires an arena above 4 GiB
    helper = native.runtime_address + mapping_shift
    arena = 0x120000000
    source_off = 64
    body_start = B.simple_tb(source_off).meta["insn0_start"]
    call_off = body_start + len(store) * 3 + len(arg)
    base = arena + source_off + B.round_up(B.DEFAULT_TB_SIZE, B.DEFAULT_ICACHE_LINESIZE)
    call = b"\xe8" + (helper - base - call_off - 5).to_bytes(4, "little", signed=True)
    raw = B.simple_tb(source_off, code_address=arena, body_prefix=store * 3 + arg + call)
    raw.meta["sites"].insert(0, dict(field_off=call_off + 1, insn_off=call_off, width=4,
                                     kind=SiteKind.HELPER_CALL, enc=Encoding.REL32, aux=binding.id,
                                     value=helper, insn_idx=0))
    raw.meta["opmarks"].insert(0, dict(host_off=call_off, kind=OpMarkKind.CALL,
                                       insn_idx=0, helper_id=binding.id))
    maps = "".join(f"{m.start + mapping_shift:x}-{m.end + mapping_shift:x} {m.perms} "
                   f"{m.offset:x} {m.dev} {m.inode} {m.path}\n" for m in context.maps)
    sc, scm = B.build_pair([raw], str(tmp_path), maps=maps.encode(),
                           sc_header={"code_address": arena, "runtime_signature": 1},
                           run_info=vars(run_info), helpers=[{
                               "name": binding.name, "helper_id": binding.id, "func": helper,
                               "tcg_flags": 5, "typemask": 18}])
    src = import_snapshot(sc=sc, scms=[scm], runtime_identity=runtime)
    unit, = src.snapshot.units()
    assert unit.meta_status == "ok" and unit.reloc.relocatable, src.evidence_rows()
    req = replace(template, target=unit.uid, unit_hash=unit.content_hash,
                  call_off=call_off, call_bytes=call, continuation_off=call_off + 5,
                  target_abs=helper, helper_id=binding.id, symbol=binding.symbol,
                  elf_address=native.elf_address, image_offset=native.image_offset,
                  image_id=native.image_id, pool_slot_off=None, guest_pcs=unit.vaddrs[:1], splice=None)
    req = replace(req, splice=prepare_splice(unit, req, arena))
    req = prepare_splice_layout(unit, req, arena)
    ws = WorkingSet(src.snapshot, _by_uid={unit.uid: replace(unit, native_fragments=(req,))},
                    _order=(unit.uid,))
    linked = NativeLinker(ws, join_result=src.join).link()
    assert linked.ok, linked.failures
    parent = tmp_path / "spliced"
    parent.mkdir()
    write_sc(parent / "cache.sc", src.snapshot.header, [(u.record, u.body) for u in linked.units])
    for name, data in linked.derived_scms:
        (parent / name).write_bytes(data)
    env_layout = find_envlayout("sha256:56d8bfb32c91c970fe0a16237b962c02791ce0a7fcc52bf6246343fbef430d90")
    contract = build_contract(env_layout, runtime)
    source_hashes = set()
    for generation in range(3):
        current = import_snapshot(sc=str(parent / "cache.sc"), scms=[str(parent / "cache.scm")],
                                  runtime_identity=runtime)
        u, = current.snapshot.units()
        assert u.meta_status == "ok" and u.reloc.relocatable, current.evidence_rows()
        assert u.native_fragments == (req,) and u.splice_applied
        retired = nop_bytes(len(store)) * generation
        assert u.code[body_start:body_start + len(retired)] == retired
        assert u.content_hash not in source_hashes
        source_hashes.add(u.content_hash)
        at = body_start + generation * len(store)
        ops = build_hostops(u, env_layout, arena)
        dse = syncdead_unit(u, ops, build_hcfg(ops), env_layout, contract)
        assert dse.ok, dse
        assert (at in {row.off for row in dse.dead}) == (generation < 2)
        patch = Patch(u.uid, at, store, nop_bytes(len(store)), "b4_nop_store")
        interior = req.splice.start_off
        mutation = replace(patch, host_off=interior, old_bytes=u.code[interior:interior + len(store)])
        with pytest.raises(LinkError, match="overlaps native certificate"):
            rewrite_unit(u, [mutation])
        corrupted = replace(u, code=u.code[:interior] + b"\xcc" + u.code[interior + 1:])
        with pytest.raises(LinkError, match="differs from its certified bytes"):
            rewrite_unit(corrupted, [patch])
        layout = WorkingSet(current.snapshot, placements={u.uid: u.tb_offset + 512})
        plan = B3Plan(rewrites={u.uid: (patch,)})
        if generation == 0:
            unbound = NativeLinkerV2(current.snapshot, plan, join_result=replace(current.join, slots={})).link()
            assert not unbound.ok and unbound.failures[0].reason == "native_metadata_missing"
        result = NativeLinkerV2(current.snapshot, plan, join_result=current.join,
                                placement=layout).link()
        assert result.ok, result.failures
        world = world_of(current.snapshot)
        verdict = check_equiv(program_of(u, world), placed_program(result, u.uid, world))
        if generation == 2:
            # The last store is live even though its placement and encoding are allowed.
            assert verdict.status == "mismatch", verdict
            break
        assert verdict.ok, verdict
        assert result.units[0].moved
        assert result.effective[u.uid].content_hash != u.content_hash
        parent = tmp_path / f"rewrite-{generation}"
        write_b3_files(parent, current.snapshot, result)

    # Re-encode an internally consistent SCM with a changed claim about the ELF.
    # Reimport must consult the original native image on every generation.
    capture = current.join.captures[0]
    wrong = replace(req, image_offset=req.image_offset + 1)
    chunks = [replace(c.body, native_fragments=(wrong,)) if isinstance(c.body, S.TbChunk)
              else c.body for c in capture.chunks]
    mutant = tmp_path / "mutant.scm"
    S.write_scm(mutant, S.make_scm(chunks, capture.header))
    rejected = import_snapshot(sc=str(parent / "cache.sc"), scms=[str(mutant)],
                               runtime_identity=runtime)
    assert rejected.snapshot.unit(u.uid).meta_status != "ok"
