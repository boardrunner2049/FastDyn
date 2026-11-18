"""WP7 importer: cross-check, reason codes (T-B1-9 import side), recovery (T-B1-8), store.

Synthetic inputs come from ``tests/synth/builders.py`` (WP4) and
``tests/synth/rich_tb.py`` (a TB with every Tier-0 site kind).
"""

from __future__ import annotations

import fcntl
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from scopt.canon import sha256_id
from scopt.formats import scm_v1 as S
from scopt.formats.enums import RunInfoFlag, TbFlag
from scopt.formats.region_v1 import validate_region
from scopt.formats.sc_v5 import ScHeader
from scopt.ids import bundle_id
from scopt.importer.import_cache import (ImportRejected, import_snapshot, manifest_contract_id,
                                         manifest_plan_hash, open_snapshot)
from tests.synth import builders as B
from tests.synth.rich_tb import RICH_HELPERS, RICH_MAPS, rich_tb, rich_units, with_meta, \
    write_rich_pair


def _pair(tmp: Path, units, *, name="rich", sc_header=None, **kw) -> tuple[str, str]:
    tmp.mkdir(parents=True, exist_ok=True)
    return write_rich_pair(list(units), str(tmp), name=name, sc_header=sc_header, **kw)


def _imp(tmp: Path, **kw):
    return import_snapshot(str(tmp), **kw)


def _unit(res, slot):
    return res.snapshot.by_slot(slot)


def _has(reasons, code: str) -> bool:
    return any(r == code or r.startswith(code + "@") for r in reasons)


# ---------------------------------------------------------------------------
# clean import
# ---------------------------------------------------------------------------

def test_clean_rich_import_every_check_passes(tmp_path):
    units = rich_units(3) + B.simple_units(2, start=B.round_up(
        B.resolve_unit(rich_units(3)[-1]).end_offset, 64))
    _pair(tmp_path / "p", units)
    res = _imp(tmp_path / "p")
    assert res.snapshot.status_histogram() == {"no_meta": 0, "ok_relocatable": 5, "pinned": 0}
    for u in res.snapshot.units():
        fails = [e for e in res.checks[u.slot].evidence if e["result"] == "fail"]
        assert not fails, (u.slot, fails)
    rich = _unit(res, 0)
    assert [(f.field_off, f.target_class, f.rule) for f in rich.reloc.fixups] == [
        (25, "ext.ptr_riprel", "rel_minus_delta"), (30, "ext.plugin", "rel_minus_delta"),
        (56, "prologue.tb_ret", "rel_minus_delta"), (76, "prologue.tb_ret", "rel_minus_delta"),
        (81, "prologue.epilogue", "rel_minus_delta")]
    # recovery: raddr 22 -> insn 0; plugin call ret 34 -> insn 1; lookup call ret 66 -> insn 3
    assert [(p.host_return_offset, p.insn, p.source) for p in rich.recovery] == [
        (22, 0, "raddr"), (34, 1, "helper_call"), (66, 3, "helper_call")]
    assert [h.name for h in rich.bindings.helpers] == [None, "lookup_tb_ptr"]
    assert [h.module for h in rich.bindings.helpers] == ["libfastdyn.so", "qemu-system-arm"]
    assert [(p.cls, p.anchor) for p in rich.bindings.unchecked_host_pointers] == [
        ("abs.plugin_data", "libfastdyn.so+0x3a010")]
    for reg in res.regions:
        assert validate_region(reg) == []
    reg0 = res.regions[0]
    assert [e["kind"] for e in reg0["exits"]] == ["goto_tb", "goto_ptr", "exit_requested",
                                                   "exit_null"]
    assert reg0["exits"][0]["patchable"] == {"insn_offset": 44, "reset_offset": 48}
    assert reg0["exits"][1]["static_dest_vaddr"] is not None  # off-page goto_ptr fallback
    assert [e["kind"] for e in reg0["maps"]["events"]] == ["guest_ld", "plugin_cb",
                                                           "helper_call"]
    t = res.summary["tallies"]
    assert t["helper_pool_calls"] == 6 and t["plugin_rel32_calls"] == 3 and t["goto_ptr"] == 3


def test_region_schema_and_ids_stable(tmp_path):
    _pair(tmp_path / "p", rich_units(2))
    r1 = _imp(tmp_path / "p")
    r2 = _imp(tmp_path / "p")
    assert [r["region_id"] for r in r1.regions] == [r["region_id"] for r in r2.regions]
    assert r1.snapshot.snapshot_id == r2.snapshot.snapshot_id
    assert [u.content_hash for u in r1.snapshot.units()] == \
        [u.content_hash for u in r2.snapshot.units()]


# ---------------------------------------------------------------------------
# store and determinism
# ---------------------------------------------------------------------------

def _tree_bytes(d: Path) -> dict[str, bytes]:
    return {str(p.relative_to(d)): p.read_bytes() for p in sorted(d.rglob("*")) if p.is_file()}


def test_reimport_is_byte_deterministic(tmp_path):
    _pair(tmp_path / "p", rich_units(3), )
    r1 = _imp(tmp_path / "p", store=str(tmp_path / "s1"))
    r2 = _imp(tmp_path / "p", store=str(tmp_path / "s2"))
    assert r1.store_status == r2.store_status == "created"
    d1, d2 = Path(r1.store_dir), Path(r2.store_dir)
    assert d1.name == d2.name == r1.snapshot.snapshot_id.split(":")[1]
    t1, t2 = _tree_bytes(d1), _tree_bytes(d2)
    assert t1 == t2
    for f in ("cache.sc", "snapshot.json", "units.jsonl", "evidence.jsonl"):
        assert f in t1
    assert any(k.startswith("code/") for k in t1)
    assert sum(1 for k in t1 if k.endswith(".scm")) == 1
    # same store again: exists, nothing changes
    r3 = _imp(tmp_path / "p", store=str(tmp_path / "s1"))
    assert r3.store_status == "exists" and _tree_bytes(d1) == t1
    # stored files are read-only
    assert not os.stat(d1 / "cache.sc").st_mode & 0o222
    # reopen rebuilds the same snapshot
    snap = open_snapshot(d1)
    assert snap.snapshot_id == r1.snapshot.snapshot_id
    assert [u.content_hash for u in snap.units()] == [u.content_hash for u in r1.snapshot.units()]


def test_store_conflict(tmp_path):
    _pair(tmp_path / "p", rich_units(2))
    r1 = _imp(tmp_path / "p", store=str(tmp_path / "s"))
    d = Path(r1.store_dir)
    os.chmod(d / "units.jsonl", 0o644)
    (d / "units.jsonl").write_bytes(b"tampered\n")
    with pytest.raises(ImportRejected) as e:
        _imp(tmp_path / "p", store=str(tmp_path / "s"))
    assert e.value.reason == "store_conflict"


def test_cache_locked(tmp_path):
    sc, _ = _pair(tmp_path / "p", rich_units(1))
    fd = os.open(sc, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ImportRejected) as e:
            _imp(tmp_path / "p")
        assert e.value.reason == "cache_locked"
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# T-B1-9, import side: each case asserts its reason code
# ---------------------------------------------------------------------------

def test_record_checksum_skipped_like_qemu(tmp_path):
    us = rich_units(3)
    us[1] = with_meta(us[1], record_checksum=0x12345678)
    _pair(tmp_path / "p", us)
    res = _imp(tmp_path / "p")
    assert [u.slot for u in res.snapshot.units()] == [0, 2]
    assert res.summary["records"]["skipped"] == [{"slot": 1, "reason": "record_checksum"}]
    assert res.summary["status"]["ok_relocatable"] == 2


def test_header_checksum_rejects(tmp_path):
    _pair(tmp_path / "p", rich_units(1), sc_header={"checksum": 1})
    with pytest.raises(ImportRejected) as e:
        _imp(tmp_path / "p")
    assert e.value.reason == "header_checksum"


def test_version_rejects(tmp_path):
    _pair(tmp_path / "p", rich_units(1), sc_header={"version": 7})
    with pytest.raises(ImportRejected) as e:
        _imp(tmp_path / "p")
    assert e.value.reason == "version"


def test_v6_without_extension_rejects(tmp_path):
    """A version-6 header (WP18: .sc v6) without a valid extension table is
    refused like the qint-v6 loader refuses it (feature EXT_TABLE clear). The
    stable reason stays B1-9's "version"."""
    _pair(tmp_path / "p", rich_units(1), sc_header={"version": 6})
    with pytest.raises(ImportRejected) as e:
        _imp(tmp_path / "p")
    assert e.value.reason == "version"
    assert "EXT_TABLE" in e.value.detail


def test_truncated_record_array(tmp_path):
    us = rich_units(3)
    p = tmp_path / "p"
    p.mkdir()
    B.build_sc(us, path=str(p / "t.sc"), record_count=5)
    B.build_scm(us, path=str(p / "t.scm"), maps=RICH_MAPS, helpers=RICH_HELPERS)
    res = _imp(p)
    assert res.summary["records"]["count"] == 3  # min(record_count, available)
    assert [x["code"] for x in res.summary["records"]["problems"]] == ["truncated_records"]
    assert res.summary["status"]["ok_relocatable"] == 3


def test_record_mismatch_is_no_meta(tmp_path):
    us = rich_units(2)
    other = B.resolve_unit(rich_tb(us[0].tb_offset, guest_bytes=b"\x01\xbf" * 4)).record_bytes
    us[0] = with_meta(us[0], scm_record=other)  # a chunk from another run's slot 0
    _pair(tmp_path / "p", us)
    res = _imp(tmp_path / "p")
    u0 = _unit(res, 0)
    assert u0.meta_status == "no_meta" and not u0.reloc.relocatable
    assert u0.reloc.reasons == ("no_meta", "record_mismatch")
    assert _unit(res, 1).reloc.relocatable


def test_chunk_checksum_drops_chunk_and_suffix(tmp_path):
    us = rich_units(3)
    sc, scm = _pair(tmp_path / "p", us)
    data = Path(scm).read_bytes()
    tab = B.chunk_table(data)
    tb_seqs = [c["seq"] for c in tab if c["type"] == 4]
    Path(scm).write_bytes(B.corrupt_trailer(data, tb_seqs[1]))
    res = _imp(tmp_path / "p")
    codes = [p["code"] for p in res.summary["capture"]["problems"]]
    assert "chunk_checksum" in codes and "incomplete" in codes
    assert [u.meta_status for u in res.snapshot.units()] == ["ok", "no_meta", "no_meta"]
    assert res.summary["capture"]["complete"] is False


def test_missing_reference_site_removed_from_bytes(tmp_path):
    us = rich_units(2)
    code = bytearray(us[1].code)
    code[22:29] = bytes.fromhex("0f1f8000000000")  # the MOVI_CONST lea -> 7-byte nop
    us[1] = replace(us[1], code=bytes(code))
    _pair(tmp_path / "p", us)
    res = _imp(tmp_path / "p")
    u1 = _unit(res, 1)
    assert u1.meta_status == "ok"  # the capture still binds (code_fnv of these bytes)
    assert not u1.reloc.relocatable
    assert "missing_reference@0x19" in u1.reloc.reasons
    assert _unit(res, 0).reloc.relocatable
    with pytest.raises(ImportRejected) as e:
        _imp(tmp_path / "p", strict=True)
    assert e.value.reason == "missing_reference"


def test_unexplained_reference_site_removed_from_capture(tmp_path):
    us = rich_units(2)
    sc, scm = _pair(tmp_path / "p", us)
    Path(scm).write_bytes(B.drop_site(Path(scm).read_bytes(), 1, 3))  # the plugin call
    res = _imp(tmp_path / "p")
    u1 = _unit(res, 1)
    assert u1.meta_status == "ok" and not u1.reloc.relocatable
    assert "unexplained_reference@0x1e" in u1.reloc.reasons
    assert res.snapshot.status(u1.uid) == "pinned"
    # strict mode does not reject an unexplained reference (it only pins)
    assert _imp(tmp_path / "p", strict=True).snapshot.status(u1.uid) == "pinned"


def test_code_fnv_mismatch_is_no_meta(tmp_path):
    us = rich_units(2)
    sc, _ = _pair(tmp_path / "p", us)
    r = B.resolve_unit(us[0])
    with open(sc, "r+b") as f:  # flip one code byte; the record checksum does not cover code
        f.seek(0x10000 + r.tc_offset + 35)
        b = f.read(1)
        f.seek(0x10000 + r.tc_offset + 35)
        f.write(bytes([b[0] ^ 0x01]))
    res = _imp(tmp_path / "p")
    u0 = _unit(res, 0)
    assert u0.meta_status == "no_meta" and u0.reloc.reasons == ("code_fnv_mismatch", "no_meta")
    assert res.snapshot.status(u0.uid) == "no_meta"


def test_run_info_runtime_signature_mismatch(tmp_path):
    sc, scm = _pair(tmp_path / "p", rich_units(1))
    Path(scm).write_bytes(B.swap_run(Path(scm).read_bytes(), {"runtime_signature": 1}))
    with pytest.raises(ImportRejected) as e:
        _imp(tmp_path / "p")
    assert e.value.reason == "run_info_mismatch:runtime_signature"


def test_unsupported_flat(tmp_path):
    sc, scm = _pair(tmp_path / "p", rich_units(1))
    Path(scm).write_bytes(B.swap_run(Path(scm).read_bytes(), {"flags": int(RunInfoFlag.FLAT)}))
    with pytest.raises(ImportRejected) as e:
        _imp(tmp_path / "p")
    assert e.value.reason == "unsupported_flat"


def test_unsupported_flat_from_manifest(tmp_path):
    sc, _ = _pair(tmp_path / "p", rich_units(1))
    man = _manifest()
    man["inputs"]["settings"]["supercache_flat"] = True
    Path(sc + ".fastdyn.json").write_text(json.dumps(man))
    with pytest.raises(ImportRejected) as e:
        _imp(tmp_path / "p")
    assert e.value.reason == "unsupported_flat"


def test_unsupported_arena_below_4g(tmp_path):
    hdr = {"code_address": 0x7F000000}
    us = B.simple_units(1, code_address=0x7F000000)
    _pair(tmp_path / "p", us, sc_header=hdr)
    with pytest.raises(ImportRejected) as e:
        _imp(tmp_path / "p")
    assert e.value.reason == "unsupported_arena_below_4g"


def _manifest(modifiers: str = "11" * 32) -> dict:
    return {"version": 1, "fingerprint": "x", "inputs": {
        "content_files": {"virtuals": {"sha256": "22" * 32, "size": 1},
                          "modifiers": {"sha256": modifiers, "size": 1},
                          "fuzzing_schema": None},
        "native_files": {},
        "settings": {"plugin_options": ["log_level=20", "coverage=0"], "log_options": "none",
                     "icount": "", "supercache_flat": False}}}


def _bundle(man: dict) -> dict:
    z = sha256_id(b"z")
    b = {"schema_version": 1, "original_image_hash": z, "image_hash": z,
         "patch_plan_hash": manifest_plan_hash(man), "patch_bindings_hash": z,
         "origin_map_hash": z,
         "target": {"isa": "armv7-m", "mode": "thumb", "endianness": "little",
                    "cpu": "cortex-m3", "features": ["m"]},
         "entry_points": {"elf_entry": "0x1", "vtor": "0x0", "reset_vector": "0x1",
                          "initial_sp": "0x2"},
         "memory_map_hash": z, "contract_id": manifest_contract_id(man),
         "instrumentation_hash": z, "device_model_hash": z}
    b["bundle_id"] = bundle_id(b)
    return b


def test_bundle_bound_and_bundle_mismatch(tmp_path):
    sc, _ = _pair(tmp_path / "p", rich_units(1))
    man = _manifest()
    Path(sc + ".fastdyn.json").write_text(json.dumps(man))
    bundle = _bundle(man)
    res = _imp(tmp_path / "p", bundle=bundle, store=str(tmp_path / "s"))
    assert res.summary["bundle_id"] == bundle["bundle_id"]
    assert res.regions[0]["bundle_id"] == bundle["bundle_id"]
    assert res.regions[0]["dependencies"]["patch_plan_hash"] == bundle["patch_plan_hash"]
    assert (Path(res.store_dir) / "manifest.json").is_file()
    # modifiers.txt edited between the bundle and the prep run
    Path(sc + ".fastdyn.json").write_text(json.dumps(_manifest(modifiers="33" * 32)))
    with pytest.raises(ImportRejected) as e:
        _imp(tmp_path / "p", bundle=bundle)
    assert e.value.reason == "bundle_mismatch"
    bad = dict(bundle, image_hash=sha256_id(b"other"))
    with pytest.raises(ImportRejected) as e:
        _imp(tmp_path / "p", bundle=bad)
    assert e.value.reason == "bundle_id_mismatch"


def test_snapshot_merge_conflict_and_union(tmp_path):
    us = rich_units(4)
    p = tmp_path / "p"
    p.mkdir()
    B.build_sc(us, path=str(p / "u.sc"))
    kw = dict(maps=RICH_MAPS, helpers=RICH_HELPERS, all_units=us)
    B.build_scm(us[:2], path=str(p / "a.scm"), **kw)
    B.build_scm(us[2:], path=str(p / "b.scm"), first_slot=2, **kw)
    res = _imp(p)
    assert res.summary["capture"]["binding"] == {"bound": 4}
    assert res.summary["status"]["ok_relocatable"] == 4
    # an overlapping capture with a different chunk for slot 1
    us2 = list(us)
    us2[1] = with_meta(us[1], attempts=7)
    B.build_scm(us2[1:2], path=str(p / "c.scm"), first_slot=1, **kw)
    with pytest.raises(ImportRejected) as e:
        _imp(p)
    assert e.value.reason == "snapshot_merge_conflict"


def test_capture_error_flag_pins_incomplete(tmp_path):
    us = rich_units(2)
    us[0] = with_meta(us[0], flags=int(TbFlag.CAPTURE_ERROR | TbFlag.IRQ_CHECK))
    _pair(tmp_path / "p", us)
    res = _imp(tmp_path / "p")
    u0 = _unit(res, 0)
    assert u0.meta_status == "incomplete" and u0.reloc.reasons == ("capture_error",)
    assert validate_region(res.regions[0]) == []


def test_decode_failed(tmp_path):
    us = rich_units(1)
    code = bytearray(us[0].code)
    code[34:41] = b"\x06" * 7  # push es: invalid in 64-bit mode -> (bad)
    us[0] = replace(us[0], code=bytes(code))
    _pair(tmp_path / "p", us)
    res = _imp(tmp_path / "p")
    assert "decode_failed@0x22" in _unit(res, 0).reloc.reasons


def test_wrong_target_classes_pin(tmp_path):
    us = rich_units(1)
    sites = [dict(s) for s in us[0].meta["sites"]]
    sites[6]["value"] = sites[6]["value"] + 1  # EXIT_TB_RET_JMP value != tb_ret
    us[0] = with_meta(us[0], sites=sites)
    _pair(tmp_path / "p", us)
    res = _imp(tmp_path / "p")
    rs = _unit(res, 0).reloc.reasons
    # the decoded jmp no longer matches the site: both sides report, and the class pins
    assert "unexplained_reference@0x38" in rs and "missing_reference@0x38" in rs
    assert "prologue_target_mismatch@0x38" in rs


def test_irq_check_and_exit_checks(tmp_path):
    us = rich_units(1)
    us[0] = with_meta(us[0], flags=0)  # TB has the check and no CF_NOIRQ: flag must be set
    _pair(tmp_path / "p", us)
    assert "irq_check_mismatch" in _unit(_imp(tmp_path / "p"), 0).reloc.reasons


# ---------------------------------------------------------------------------
# T-B1-8: recovery oracle
# ---------------------------------------------------------------------------

def test_recovery_mismatch_shifted_host_end_row(tmp_path):
    us = rich_units(2)
    ins = list(us[1].meta["insns"])
    # move the insn 0/1 boundary from 22 to 35 in the capture AND the search data
    # (the builder regenerates the search rows from these), so search == capture
    # but the plugin call (returns at 34) now unwinds to insn 0, not its OpMark's 1
    ins[0] = S.ScmInsn(ins[0].words, ins[0].host_start, 35)
    ins[1] = S.ScmInsn(ins[1].words, 35, ins[1].host_end)
    us[1] = with_meta(us[1], insns=ins)
    _pair(tmp_path / "p", us)
    res = _imp(tmp_path / "p")
    u1 = _unit(res, 1)
    assert u1.reloc.reasons == ("recovery_mismatch",)
    bad = [c for c in res.checks[1].recovery_checks if not c.ok]
    assert [(c.source, c.host_return_offset, c.expected_insn, c.unwound_insn) for c in bad] == \
        [("helper_call", 34, 1, 0)]
    assert _unit(res, 0).reloc.relocatable


def test_shifted_search_row_only_is_a_search_mismatch(tmp_path):
    """Shifting only the .sc search data (not the capture) is caught by step 3."""
    us = rich_units(1)
    ins = list(us[0].meta["insns"])
    shifted = [S.ScmInsn(ins[0].words, 11, 35), S.ScmInsn(ins[1].words, 35, 41), *ins[2:]]
    us[0] = replace(us[0], search=B.encode_search(shifted))
    _pair(tmp_path / "p", us)
    rs = _unit(_imp(tmp_path / "p"), 0).reloc.reasons
    assert "search_mismatch" in rs and "recovery_mismatch" in rs


def test_header_problem_objects(tmp_path):
    # ScHeader sanity: the builder's header passes the reader's checks
    sc, _ = _pair(tmp_path / "p", rich_units(1))
    h = ScHeader.from_bytes(Path(sc).read_bytes()[:80])
    assert h.problems() == [] and h.code_address >= 1 << 32
