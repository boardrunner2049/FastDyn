"""Frozen contracts: enum numbering (§2.4, §3.5), binary layout sizes (§2.5, §3.5),
JSON schemas (§3.8, §4.5, §4.2, §4.6) and the CLI scaffold (WP0)."""

from __future__ import annotations

import copy
import subprocess
import sys

import pytest

from scopt import ids
from scopt.formats import enums, layouts, schemas
from scopt.passes import base as pbase

SHA = "sha256:" + "ab" * 32
UID = "tb:" + "5f" * 16 + "#0"


def test_site_kinds_and_encodings_numbering():
    assert [(k.name, int(k)) for k in enums.SiteKind] == [
        ("LABEL_JMP", 1), ("LABEL_JCC", 2), ("LDST_FAST_JCC", 3), ("LDST_RET_JMP", 4),
        ("LDST_HELPER_CALL", 5), ("HELPER_CALL", 6), ("EXIT_TB_RET_JMP", 7),
        ("EXIT_TB_EPI_JMP", 8), ("MOVI_EXIT_TB_PTR", 9), ("MOVI_LDST_RA", 10),
        ("MOVI_CONST", 11), ("POOL_REF", 12), ("GOTO_TB", 13), ("GOTO_PTR", 14),
        ("OTHER_BRANCH", 15)]
    assert [int(e) for e in enums.Encoding] == list(range(10))
    assert enums.Encoding.POOL_INDIRECT == 8 and enums.Encoding.GOTO_TB_REL32 == 9
    assert [int(k) for k in enums.OpMarkKind] == [1, 2, 3, 4, 5, 6]
    assert [int(f) for f in enums.TbFlag] == [1, 2, 4, 8, 16, 32]
    # VARIANT (9) is Python-only (derived B3 captures, design §9.1 V19, WP18)
    assert {c.name: int(c) for c in enums.ChunkType} == {
        "RUN_INFO": 1, "HELPER": 3, "TB": 4, "OPS": 5, "PLUGIN_CBS": 6, "MAPS": 7,
        "RAMBLOCKS": 8, "VARIANT": 9, "END": 10}
    assert 2 in enums.CHUNK_TYPE_RESERVED


def test_qprof_enums():
    assert {k.name: int(k) for k in enums.QprofEdgeKind} == {
        "CHAIN0": 1, "CHAIN1": 2, "LOOKUP_HIT": 3, "LOOKUP_MISS": 4, "EXIT_NULL": 5,
        "EXIT_REQUESTED": 6, "FIRST": 7}
    assert [int(f) for f in enums.QprofEdgeFlag] == [1, 2, 4]
    assert [int(f) for f in enums.QprofHeaderFlag] == [1, 2, 4]
    assert enums.EDGE_KIND_NAMES[enums.QprofEdgeKind.CHAIN1] == "chain1"


def test_layout_sizes_frozen():
    for name, size in layouts.EXPECTED_SIZES.items():
        assert getattr(layouts, name).size == size, name
    assert layouts.qprof_tb_row_size(2) == 72 + 64
    assert layouts.SCM_TB_HEADER.size % 8 == 0


def test_scm_tb_header_field_offsets():
    # spot-check offsets from the §2.5.5 table by packing marker values
    vals = [1, 2, 3, b"R" * 104, 0x120, 0x128, 136, 140, 144, 148, 152, 156, 160, 164, 166,
            168, 172, 176, 180, 184, 188, 0]
    raw = layouts.SCM_TB_HEADER.pack(*vals)
    import struct
    assert raw[16:120] == b"R" * 104
    assert struct.unpack_from("<Q", raw, 120)[0] == 0x120
    for off in (136, 140, 144, 148, 152, 156, 160, 168, 172, 176, 180, 184, 188):
        assert struct.unpack_from("<I", raw, off)[0] == off
    assert struct.unpack_from("<HH", raw, 164) == (164, 166)


def test_qprof_header_offsets():
    import struct
    raw = layouts.QPROF_HEADER.pack(b"QSCPROF1", 1, 128, 16, 24, 32, 40, 48, 56, 60, 64, 72,
                                    80, 88, 0, 0, 0, 120)
    for off in (16, 24, 32, 40, 48, 64, 72, 80, 88, 120):
        assert struct.unpack_from("<Q", raw, off)[0] == off
    assert struct.unpack_from("<II", raw, 56) == (56, 60)


# ---------------------------------------------------------------------------
# JSON schemas
# ---------------------------------------------------------------------------

def _region():
    return {
        "schema": "fastdyn.cache_region", "version": 1, "region_id": SHA, "kind": "tb_singleton",
        "bundle_id": SHA, "contract_id": SHA, "runtime_identity_id": SHA,
        "members": [{"unit": UID, "key": {"page_addr0": "0x28098766", "page_addr1": None,
                                          "flags": "0x110", "cs_base": "0x800400",
                                          "cflags": "0xff020000"},
                     "guest": {"size": 12, "icount": 4, "hash": "0xa16f58218a9fa0e7",
                               "origin": "elf_verified", "vaddrs": ["0x8018766"]},
                     "origin": {"snapshot_id": SHA, "slot": 57, "tb_offset": "0x13c0"}}],
        "entries": [{"id": "e0", "kind": "general", "member": 0, "host_offset": 0,
                     "precondition": {"full_key": True, "extra_guards": []}}],
        "exits": [{"id": "x0", "member": 0, "kind": "goto_tb", "slot": 1, "insn": 3,
                   "static_dest_vaddr": "0x801876e",
                   "patchable": {"insn_offset": 212, "reset_offset": 216}}],
        "native": {"code_sha256": SHA, "code_len": 340, "insn0_start": 12, "body_end": 260,
                   "ldst_end": 300, "pool_start": 312, "search_len": 21, "end_offset_rel": 384,
                   "struct": {"offset_from_code": -192, "size": 152}, "alignment": 64},
        "relocations": [
            {"member": 0, "field_offset": 215, "width": 4, "encoding": "rel32",
             "site_kind": "EXIT_TB_RET_JMP", "target": {"class": "prologue.tb_ret"},
             "rule": "rel_minus_delta"},
            {"member": 0, "field_offset": 64, "width": 4, "encoding": "rel32",
             "site_kind": "HELPER_CALL",
             "target": {"class": "ext.plugin", "module": "libfastdyn.so", "symbol": "printreg",
                        "addr": "0x7fff1f025700"}, "rule": "rel_minus_delta"}],
        "maps": {"insn": [{"member": 0, "index": 0, "words": ["0x366", "0x0", "0x0"],
                           "host_start": 12, "host_end": 40}],
                 "events": [{"host_offset": 55, "kind": "guest_ld", "insn": 1, "faultable": True,
                             "recovery": "r0"}]},
        "recovery": [{"id": "r0", "host_return_offset": 120, "member": 0, "insn": 1,
                      "restore": {"pc_mode": "pcrel_page_offset", "words": ["0x36a", "0x0", "0x0"]},
                      "state_fixups": []}],
        "bindings": {"helpers": [{"id": 3, "name": "lookup_tb_ptr", "module": "qemu-system-arm",
                                  "symbol": "helper_lookup_tb_ptr", "flags": "0x8"}],
                     "unchecked_host_pointers": [{"class": "abs.plugin_data",
                                                  "anchor": "libfastdyn.so+0x3a120", "site": 7}]},
        "dependencies": {"guest_pages": [{"ram_addr": "0x28098400", "len": 1024}], "units": [UID],
                         "facts": [], "patch_plan_hash": SHA, "instrumentation_hash": SHA},
        "status": "ok_relocatable", "pin_reasons": [], "unknowns": ["goto_ptr dest"],
        "provenance": {"passes": [{"name": "identity", "version": 1}], "source_units": [UID]},
    }


def _profile():
    return {
        "schema": "fastdyn.profile", "version": 1, "profile_id": SHA, "snapshot_id": SHA,
        "bundle_id": SHA, "contract_id": SHA, "runtime_identity_id": SHA,
        "collection": {"method": "exact_dispatch_unchained_v1", "complete": True,
                       "overflow": False, "chain_disabled": True, "time_dilation": 7.9,
                       "repetitions": 5,
                       "inputs": [{"name": "train-000", "sha256": SHA, "split": "train"}],
                       "runs": [{"run_id": "r1", "clean_exit": True, "total_dispatches": 20148,
                                 "transient_entries": 0}]},
        "phases": [{"id": 0, "name": "boot", "marker": None, "reached": True},
                   {"id": 1, "name": "steady", "marker": "0x8003972", "match": "contained",
                    "reached": True}],
        "domain": {"kind": "snapshot_units", "count": 163},
        "units": [{"unit": UID, "vaddrs": ["0x8018766"], "status": "exact",
                   "phase": {"0": {"entries": 19394, "aborted": 0, "longjmp": 0},
                             "1": {"entries": 0, "aborted": 0, "longjmp": 0}}}],
        "edges": [{"src": UID, "dst": UID, "kind": "chain1", "flags": [], "phase": 0,
                   "count": 750, "status": "exact"},
                  {"src": None, "dst": UID, "kind": "first", "flags": [], "phase": 0,
                   "count": 1, "status": "exact"}],
        "profile_only": [{"key": {"page_addr0": "0x28098766", "page_addr1": None,
                                  "flags": "0x110", "cs_base": "0x800400",
                                  "cflags": "0xff020000"},
                          "guest_hash": "0x1", "phase": {"0": {"entries": 1, "aborted": 0,
                                                              "longjmp": 0}}}],
        "transient": {"entries": 0},
        "static_cost": {"note": "model, not measured"},
    }


def _bundle():
    return {
        "schema_version": 1, "bundle_id": SHA, "original_image_hash": SHA, "image_hash": SHA,
        "patch_plan_hash": SHA, "patch_bindings_hash": SHA, "origin_map_hash": SHA,
        "target": {"isa": "armv7-m", "mode": "thumb", "endianness": "little", "cpu": "cortex-m3",
                   "features": ["m", "v7"]},
        "entry_points": {"elf_entry": "0x8000131", "vtor": "0x8000000",
                         "reset_vector": "0x8000131", "initial_sp": "0x20010000"},
        "memory_map_hash": SHA, "contract_id": SHA, "instrumentation_hash": SHA,
        "device_model_hash": SHA,
    }


def _sidecar():
    return {
        "schema": "fastdyn.artifact_sidecar", "version": 1, "input_snapshot_id": SHA,
        "output_snapshot_id": SHA, "bundle_id": SHA, "runtime_identity_id": SHA,
        "profile_ids": [{"profile_id": SHA, "verdict": "compatible"}],
        "pipeline": [{"pass": "identity", "version": 1, "params_sha256": SHA,
                      "outcome": "no_change", "reason": "identity", "affected": [],
                      "edit_digest": None, "duration_s": 0.01}],
        "move_map": {UID: {"slot": 0, "old_tb_offset": "0x40", "new_tb_offset": "0x40",
                           "delta": 0}},
        "pinned": [{"unit": UID, "reasons": ["unexplained_reference"]}],
        "validation_sha256": SHA,
        "tool_versions": {"scopt": "0.1.0", "capstone": "5.0.3", "pyelftools": "0.33"},
        "loader_requirement": "qemu-sc-v5-unmodified",
    }


def _envlayout():
    from scopt.canon import content_id

    doc = {"schema": "scopt.envlayout/1", "exe_sha256": "sha256:" + "8" * 64,
           "producer": {"tool": "gdb", "version": "15.1", "method": "ptype/o -nx -batch"},
           "sizes": {"CPUState": 11200, "CPUArchState": 78736, "CPUNegativeOffsetState": 10432},
           "env_offset_in_cpu": 11200, "neg_offset_in_cpustate": 768,
           "fields": [{"name": "regs", "region": "env", "off": 0, "size": 64, "elem": 4,
                       "dims": [16]},
                      {"name": "neg.can_do_io", "region": "neg", "off": -4, "size": 1,
                       "elem": 1}]}
    doc["envlayout_id"] = content_id(doc, "envlayout_id")
    return doc


SAMPLES = {"cache_region.v1": _region, "profile.v1": _profile,
           "execution_bundle.v1": _bundle, "artifact_sidecar.v1": _sidecar,
           "envlayout.v1": _envlayout}


@pytest.mark.parametrize("name", schemas.SCHEMA_NAMES)
def test_schema_is_valid_and_accepts_design_example(name):
    schemas.validator(name)  # check_schema
    schemas.validate(SAMPLES[name](), name)


@pytest.mark.parametrize("name", schemas.SCHEMA_NAMES)
def test_schema_rejects_unknown_keys_but_allows_extensions(name):
    doc = SAMPLES[name]()
    schemas.validate({**doc, "x_note": "ok"}, name)
    with pytest.raises(schemas.SchemaValidationError):
        schemas.validate({**doc, "surprise": 1}, name)


def test_schema_negative_cases():
    r = _region()
    r["members"][0]["unit"] = "tb:abc#0"
    assert schemas.errors(r, "cache_region.v1")
    p = _profile()
    p["units"][0]["status"] = "zero"
    assert schemas.errors(p, "profile.v1")
    b = _bundle()
    b["schema_version"] = 2
    assert schemas.errors(b, "execution_bundle.v1")
    b = copy.deepcopy(_bundle())
    b["image_hash"] = "ab" * 32  # missing sha256: prefix
    assert schemas.errors(b, "execution_bundle.v1")


def test_region_id_ignores_provenance():
    r1, r2 = _region(), _region()
    r2["provenance"] = {"passes": [{"name": "cluster-hot", "version": 1}], "source_units": []}
    assert ids.region_id(r1) == ids.region_id(r2)
    r2["status"] = "pinned"
    assert ids.region_id(r1) != ids.region_id(r2)


# ---------------------------------------------------------------------------
# Pass API and CLI scaffold
# ---------------------------------------------------------------------------

def test_pass_api_types():
    d = pbase.PassDescriptor(name="identity", version=1, preserves=frozenset({"*"}))
    assert d.capabilities == frozenset() and d.budgets.max_wall_s == 60.0
    r = pbase.PassResult(pbase.Outcome.NO_CHANGE, "identity")
    assert r.outcome.value == "no_change" and dict(r.stats) == {}
    assert pbase.EditKind.PLACE in pbase.B2_EDIT_KINDS
    assert pbase.EditKind.CLONE in pbase.B3_EDIT_KINDS
    assert not (pbase.B2_EDIT_KINDS & pbase.B3_EDIT_KINDS)


def test_cli_contracts_check():
    out = subprocess.run([sys.executable, "-m", "scopt", "contracts", "check"],
                         capture_output=True, text=True, check=False)
    assert out.returncode == 0, out.stderr
    assert "contracts: ok" in out.stdout
