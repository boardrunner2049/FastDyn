"""WP7 round 2: import identity checks, foreign captures, store options, region and
graph details (review findings of WP7#0)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scopt.canon import content_id, hexaddr, sha256_id
from scopt.formats import scm_v1 as S
from scopt.formats.region_v1 import build_region
from scopt.graph import _exit_order
from scopt.ids import bundle_id
from scopt.importer.import_cache import (ImportRejected, import_snapshot, manifest_contract_id,
                                         manifest_plan_hash, open_snapshot)
from tests.synth import builders as B
from tests.synth.rich_tb import rich_units, write_rich_pair


def _pair(tmp: Path, units, **kw):
    tmp.mkdir(parents=True, exist_ok=True)
    return write_rich_pair(list(units), str(tmp), **kw)


def _manifest(native: dict | None = None) -> dict:
    return {"version": 1, "fingerprint": "x", "inputs": {
        "content_files": {"virtuals": {"sha256": "22" * 32, "size": 1},
                          "modifiers": {"sha256": "11" * 32, "size": 1},
                          "fuzzing_schema": None},
        "native_files": native or {},
        "settings": {"plugin_options": ["log_level=20", "coverage=0"], "log_options": "none",
                     "icount": "", "supercache_flat": False}}}


def _native(path: Path) -> dict:
    st = path.stat()
    return {"path": str(path), "size": st.st_size, "mtime_ns": st.st_mtime_ns}


def _bundle(man: dict, image_hash: str) -> dict:
    z = sha256_id(b"z")
    b = {"schema_version": 1, "original_image_hash": image_hash, "image_hash": image_hash,
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


# ---------------------------------------------------------------------------
# bundle
# ---------------------------------------------------------------------------

def test_bundle_image_hash_checked_against_firmware(tmp_path):
    sc, _ = _pair(tmp_path / "p", rich_units(1))
    fw = tmp_path / "fw.elf"
    fw.write_bytes(b"firmware bytes")
    man = _manifest({"firmware": _native(fw)})
    Path(sc + ".fastdyn.json").write_text(json.dumps(man))
    good = _bundle(man, sha256_id(b"firmware bytes"))
    imp = dict(use_manifest_firmware=False)  # fw.elf is not an ELF: no guest-origin check
    res = import_snapshot(str(tmp_path / "p"), bundle=good, **imp)
    chk = res.summary["identity_checks"]["bundle"]
    assert "image_hash" in chk["verified"] and "memory_map_hash" in chk["unverified"]
    # a bundle with a fake image_hash (bundle_id recomputed) is a mismatch
    with pytest.raises(ImportRejected) as e:
        import_snapshot(str(tmp_path / "p"), bundle=_bundle(man, sha256_id(b"other")), **imp)
    assert e.value.reason == "bundle_mismatch" and "image_hash" in e.value.detail
    # firmware changed after the prep run: stale, not silently re-hashed
    os.utime(fw, ns=(fw.stat().st_atime_ns, fw.stat().st_mtime_ns + 10**9))
    with pytest.raises(ImportRejected) as e:
        import_snapshot(str(tmp_path / "p"), bundle=good, **imp)
    assert e.value.reason == "bundle_stale"


def test_bundle_without_manifest_is_unverified(tmp_path):
    _pair(tmp_path / "p", rich_units(1))
    man = _manifest()
    with pytest.raises(ImportRejected) as e:
        import_snapshot(str(tmp_path / "p"), bundle=_bundle(man, sha256_id(b"x")))
    assert e.value.reason == "bundle_unverified"


# ---------------------------------------------------------------------------
# RuntimeIdentity
# ---------------------------------------------------------------------------

def _ri(sc_path: str, scm_path: str, **over) -> dict:
    h = S.read_scm(scm_path).run_info
    from scopt.formats.sc_records import read_sc

    hdr = read_sc(sc_path).header
    doc = {"schema": "scopt.runtime_identity/1",
           "runtime_signature": hexaddr(hdr.runtime_signature),
           "code_address": hexaddr(hdr.code_address), "code_size": hexaddr(hdr.code_size),
           "prologue_end": hexaddr(hdr.prologue_end), "tb_size": hdr.tb_size,
           "host_page_size": hdr.host_page_size, "target_page_bits": hdr.target_page_bits,
           "prologue_sha256": sha256_id(h.prologue), "icache_linesize": h.icache_linesize,
           "cpuinfo": hexaddr(h.cpuinfo),
           "qemu_exe_stat": {"path": "/q", "size": h.exe_st_size,
                             "mtime_ns": h.exe_st_mtim_ns},
           "qemu_sha256": None, "plugin_sha256": None, "supercache_flat": False}
    doc.update(over)
    doc["runtime_identity_id"] = content_id(doc, "runtime_identity_id")
    return doc


def test_runtime_identity_checked_against_run_info(tmp_path):
    sc, scm = _pair(tmp_path / "p", rich_units(1))
    res = import_snapshot(str(tmp_path / "p"), runtime_identity=_ri(sc, scm))
    v = set(res.summary["identity_checks"]["runtime_identity"]["verified"])
    assert {"prologue_sha256@run_info", "icache_linesize@run_info", "cpuinfo@run_info",
            "qemu_exe_stat.size@run_info", "qemu_exe_stat.mtime_ns@run_info",
            "runtime_signature@sc"} <= v
    for field, bad in (("prologue_sha256", sha256_id(b"other prologue")),
                       ("icache_linesize", 128), ("cpuinfo", "0x1"),
                       ("code_size", "0x1000")):
        with pytest.raises(ImportRejected) as e:
            import_snapshot(str(tmp_path / "p"), runtime_identity=_ri(sc, scm, **{field: bad}))
        assert e.value.reason == f"runtime_mismatch:{field}", field
    est = {"path": "/q", "size": 1, "mtime_ns": 2}
    with pytest.raises(ImportRejected) as e:
        import_snapshot(str(tmp_path / "p"), runtime_identity=_ri(sc, scm, qemu_exe_stat=est))
    assert e.value.reason == "runtime_mismatch:qemu_exe_stat.size"


def test_runtime_identity_qemu_and_plugin_sha_against_manifest_files(tmp_path):
    q = tmp_path / "qemu-bin"
    q.write_bytes(b"qemu executable")
    pl = tmp_path / "plugin.so"
    pl.write_bytes(b"plugin")
    qs = q.stat()
    sc, scm = _pair(tmp_path / "p", rich_units(1),
                    run_info={"exe_st_size": qs.st_size, "exe_st_mtim_ns": qs.st_mtime_ns})
    Path(sc + ".fastdyn.json").write_text(json.dumps(
        _manifest({"qemu": _native(q), "plugin": _native(pl)})))
    good = dict(qemu_sha256=sha256_id(b"qemu executable"), plugin_sha256=sha256_id(b"plugin"),
                qemu_exe_stat={"path": str(q), "size": qs.st_size, "mtime_ns": qs.st_mtime_ns})
    res = import_snapshot(str(tmp_path / "p"), runtime_identity=_ri(sc, scm, **good))
    v = set(res.summary["identity_checks"]["runtime_identity"]["verified"])
    assert {"qemu_sha256@file", "plugin_sha256@file", "qemu_exe_stat.size@manifest"} <= v
    with pytest.raises(ImportRejected) as e:
        import_snapshot(str(tmp_path / "p"), runtime_identity=_ri(
            sc, scm, **dict(good, qemu_sha256=sha256_id(b"rebuilt"))))
    assert e.value.reason == "runtime_mismatch:qemu_sha256"
    with pytest.raises(ImportRejected) as e:
        import_snapshot(str(tmp_path / "p"), runtime_identity=_ri(
            sc, scm, **dict(good, plugin_sha256=sha256_id(b"other plugin"))))
    assert e.value.reason == "runtime_mismatch:plugin_sha256"
    pl.write_bytes(b"plugin, rebuilt")
    with pytest.raises(ImportRejected) as e:
        import_snapshot(str(tmp_path / "p"), runtime_identity=_ri(sc, scm, **good))
    assert e.value.reason == "runtime_stale:plugin"


# ---------------------------------------------------------------------------
# foreign captures (§2.5.8)
# ---------------------------------------------------------------------------

def test_foreign_capture_with_other_run_info_is_left_out(tmp_path):
    sc, scm = _pair(tmp_path / "p", rich_units(2))
    base = import_snapshot(str(tmp_path / "p"))
    other = tmp_path / "p" / "zz-leftover.scm"
    other.write_bytes(B.swap_run(Path(scm).read_bytes(), {"runtime_signature": 1}))
    res = import_snapshot(str(tmp_path / "p"), store=str(tmp_path / "s"))
    assert res.summary["capture"]["binding"] == {"bound": 2}
    assert [(p["file"], p["code"]) for p in res.summary["capture"]["problems"]] == [
        ("zz-leftover.scm", "run_info_mismatch:runtime_signature")]
    assert res.regions == base.regions
    assert sorted(p.name for p in Path(res.store_dir).glob("*.scm")) == \
        [c["name"] for c in base.summary["capture"]["files"]]
    assert open_snapshot(res.store_dir).snapshot_id == base.snapshot.snapshot_id


def test_capture_of_another_sc_never_conflicts(tmp_path):
    """Same RUN_INFO (same binary), different .sc: its chunks do not bind, so
    they cannot conflict with the real capture's chunks for the same slots."""
    us = rich_units(2)
    _pair(tmp_path / "p", us)
    _pair(tmp_path / "o", list(reversed(us)), name="other")  # other records per slot
    os.rename(tmp_path / "o" / "other.scm", tmp_path / "p" / "other.scm")
    res = import_snapshot(str(tmp_path / "p"))
    assert res.summary["capture"]["binding"] == {"bound": 2}
    assert ("other.scm", "no_chunk_binds") in {
        (p["file"], p["code"]) for p in res.summary["capture"]["problems"]}


# ---------------------------------------------------------------------------
# store: --strict is not part of the snapshot
# ---------------------------------------------------------------------------

def test_strict_and_plain_imports_share_one_store_dir(tmp_path):
    _pair(tmp_path / "p", rich_units(2))
    a = import_snapshot(str(tmp_path / "p"), store=str(tmp_path / "s"))
    b = import_snapshot(str(tmp_path / "p"), store=str(tmp_path / "s"), strict=True)
    assert a.store_dir == b.store_dir and b.store_status == "exists"
    assert "strict" not in json.loads((Path(a.store_dir) / "snapshot.json").read_text())["options"]


# ---------------------------------------------------------------------------
# region helper-call return offset and graph edge order
# ---------------------------------------------------------------------------

def test_region_helper_return_from_oracle(tmp_path):
    _pair(tmp_path / "p", rich_units(1))
    res = import_snapshot(str(tmp_path / "p"))
    u = res.snapshot.by_slot(0)
    uc = res.checks[0]
    rets = {c.ref: c.host_return_offset for c in uc.recovery_checks
            if c.source == "helper_call"}
    sb = {c.site.field_off: c.binding for c in uc.classified}
    reg = build_region(u, snapshot_id=res.snapshot.snapshot_id, tb_size=152, page_bits=10,
                       bundle=None, runtime_identity_id=None, search_len=uc.search_len,
                       site_bindings=sb, helper_returns=rets)
    assert reg["maps"]["events"] == res.regions[0]["maps"]["events"]
    calls = [e for e in reg["maps"]["events"] if e["kind"] in ("helper_call", "plugin_cb")]
    assert calls and all(e["recovery"] is not None for e in calls)
    # a return offset the oracle did not certify has no recovery point
    off = build_region(u, snapshot_id=res.snapshot.snapshot_id, tb_size=152, page_bits=10,
                       bundle=None, runtime_identity_id=None, search_len=uc.search_len,
                       site_bindings=sb, helper_returns={k: v + 1 for k, v in rets.items()})
    assert all(e["recovery"] is None for e in off["maps"]["events"]
               if e["kind"] in ("helper_call", "plugin_cb"))


def test_exit_order_is_numeric():
    ids = ["x10", "x2", None, "x1", "x0"]
    assert sorted(ids, key=_exit_order) == ["x0", "x1", "x2", "x10", None]
