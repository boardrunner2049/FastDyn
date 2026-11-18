"""WP8: ExecutionBundle v1 reader, identity bundle and RuntimeIdentity."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scopt.canon import canonical_json
from scopt.formats.bundle_v1 import (BundleIdMismatch, BundleMismatch, BundleSchemaError,
                                     BundleStale, check_bundle, check_runtime_identity,
                                     identity_bundle, read_bundle, read_runtime_identity,
                                     runtime_identity)
from scopt.ids import bundle_id
from scopt.run.toml_rewrite import rewrite_toml
from scopt.paths import fastdyn_root

B0_SC = (fastdyn_root() / "src/fastdyn/configs/b0-sc.toml")
B0_MANIFEST = (fastdyn_root() / "runs/b0/cache/minimal.sc.fastdyn.json")

pytestmark = pytest.mark.real


@pytest.fixture(scope="module")
def manifest():
    if not (B0_SC.exists() and B0_MANIFEST.exists()):
        pytest.skip("B0 config/manifest not present")
    return json.loads(B0_MANIFEST.read_text())


@pytest.fixture(scope="module")
def bundle(manifest):
    try:
        return identity_bundle(B0_SC, manifest)
    except BundleStale as e:  # pragma: no cover - environment changed since B0
        pytest.skip(f"B0 manifest is stale: {e}")


def test_identity_bundle_fields(bundle):
    assert bundle["schema_version"] == 1
    assert bundle["image_hash"] == bundle["original_image_hash"]
    assert bundle["target"] == {"isa": "armv7-m", "mode": "thumb", "endianness": "little",
                                "cpu": "cortex-m3", "features": ["m", "m_main", "v7"]}
    ep = bundle["entry_points"]
    assert ep["vtor"] == "0x8000000"
    assert ep["initial_sp"] == "0x20030000"  # "Loaded reset SP 0x20030000 PC 0x8018741"
    assert ep["reset_vector"] == "0x8018741"
    assert bundle["bundle_id"] == bundle_id(bundle)
    assert read_bundle(bundle) == bundle


def test_identity_bundle_ignores_run_paths(bundle, manifest, tmp_path):
    text = B0_SC.read_text()
    for name in ("a", "b"):
        r = rewrite_toml(text, supercache=f"/runs/{name}/cache/minimal.sc",
                         memory_file=lambda b, n=name: f"/runs/{n}/ram/{b.id}.ram",
                         qmp_socket=f"/runs/{name}/qmp.sock")
        p = tmp_path / f"{name}.toml"
        p.write_text(r.text)
        assert identity_bundle(p, manifest)["bundle_id"] == bundle["bundle_id"]


def test_modifier_change_is_bundle_mismatch(bundle, manifest):
    m = copy.deepcopy(manifest)
    m["inputs"]["content_files"]["modifiers"]["sha256"] = "0" * 64
    other = identity_bundle(B0_SC, m)
    with pytest.raises(BundleMismatch) as e:
        check_bundle(bundle, other)
    assert e.value.code == "bundle_mismatch"
    assert e.value.info["fields"] == ["instrumentation_hash", "patch_bindings_hash",
                                      "patch_plan_hash"]
    check_bundle(bundle, identity_bundle(B0_SC, manifest))


def test_stale_firmware(manifest):
    m = copy.deepcopy(manifest)
    m["inputs"]["native_files"]["firmware"]["mtime_ns"] += 1
    with pytest.raises(BundleStale):
        identity_bundle(B0_SC, m)


def test_reader_rejections(bundle, tmp_path):
    bad = dict(bundle, schema_version=2)
    with pytest.raises(BundleSchemaError) as e:
        read_bundle(bad)
    assert e.value.code == "unknown_schema_version"
    with pytest.raises(BundleIdMismatch):
        read_bundle(dict(bundle, contract_id="sha256:" + "1" * 64))
    with pytest.raises(BundleSchemaError) as e:
        read_bundle(dict(bundle, unknown_key=1))
    assert e.value.code == "bundle_schema"
    ext = dict(bundle, x_note="ok")
    ext["bundle_id"] = bundle_id(ext)
    assert read_bundle(ext)["x_note"] == "ok"
    p = tmp_path / "b.json"
    p.write_bytes(canonical_json(bundle))
    assert read_bundle(p) == bundle


def test_runtime_identity(manifest, minimal_sc):
    from scopt.formats.sc_v5 import ScHeader

    qemu = manifest["inputs"]["native_files"]["qemu"]["path"]
    plugin = manifest["inputs"]["native_files"]["plugin"]["path"]
    if not (Path(qemu).exists() and Path(plugin).exists()):
        pytest.skip("B0 QEMU/plugin not present")
    h = ScHeader.from_bytes(Path(minimal_sc).open("rb").read(80))
    text = B0_SC.read_text()
    ri = runtime_identity(qemu=qemu, plugin=plugin, config_text=text, sc_header=h)
    assert ri["build_options"] == {"buildtype": "debug", "optimization": "2", "debug": True}
    assert ri["tb_size"] == 152 and ri["target_page_bits"] == 10
    assert ri["code_address"] == f"0x{h.code_address:x}"
    assert read_runtime_identity(ri) == ri
    edited = text.replace('patch = "r0 <- 3"', 'patch = "r0 <- 4"')
    ri2 = runtime_identity(qemu=qemu, plugin=plugin, config_text=edited, sc_header=h)
    assert check_runtime_identity(ri, ri2) == ["config_mismatch"]
    assert check_runtime_identity(ri, ri) == []


QINT = (fastdyn_root() / "runs/qint/t-b1-1-minimal")
QINT_QEMU = (fastdyn_root() / "wt/qemu-int/build/qemu-system-arm")


def test_runtime_identity_stat_fields(minimal_sc):
    qemu = (fastdyn_root() / "src/qemu/build/qemu-system-arm")
    plugin = (fastdyn_root() / "src/fastdyn/build/libfastdyn.so")
    if not (qemu.exists() and plugin.exists()):
        pytest.skip("B0 binary/plugin not present")
    from scopt.formats.sc_v5 import ScHeader

    h = ScHeader.from_bytes(minimal_sc.open("rb").read(80))
    ri = runtime_identity(qemu=qemu, plugin=plugin, config_text=B0_SC.read_text(), sc_header=h)
    st = qemu.stat()
    assert ri["qemu_exe_stat"] == {"path": str(qemu.resolve()), "dev": st.st_dev,
                                   "ino": st.st_ino, "size": st.st_size,
                                   "mtime_ns": st.st_mtime_ns}
    assert ri["plugin_stat"]["size"] == plugin.stat().st_size
    # a rebuilt/touched binary (same bytes, new stat) changes QEMU's runtime_signature
    touched = {**ri, "qemu_exe_stat": {**ri["qemu_exe_stat"], "ino": st.st_ino + 1}}
    assert check_runtime_identity(touched, ri) == ["runtime_mismatch:qemu_exe_stat"]
    # older documents without dev/ino compare on the keys they have
    old = {**ri, "qemu_exe_stat": {k: ri["qemu_exe_stat"][k] for k in ("path", "size",
                                                                       "mtime_ns")}}
    assert check_runtime_identity(old, ri) == []
    moved = {**ri, "plugin_stat": {**ri["plugin_stat"], "path": "/elsewhere/libfastdyn.so"}}
    assert check_runtime_identity(moved, ri) == ["runtime_mismatch:plugin_stat"]


def test_runtime_identity_from_run_info():
    """RUN_INFO fills prologue_sha256/icache_linesize/cpuinfo and must match exe and header."""
    sc_p, scm_p = QINT / "archive/on1.sc", QINT / "scm/on1.scm"
    if not (sc_p.exists() and scm_p.exists() and QINT_QEMU.exists()):
        pytest.skip("WP-QINT capture evidence not present")
    import hashlib

    from scopt.formats.bundle_v1 import run_info_fields
    from scopt.formats.scm_v1 import read_scm
    from scopt.formats.sc_v5 import ScHeader

    scm = read_scm(scm_p)
    h = ScHeader.from_bytes(sc_p.open("rb").read(80))
    fields = run_info_fields(scm.run_info)
    assert fields["prologue_sha256"] == "sha256:" + hashlib.sha256(
        scm.run_info.prologue).hexdigest()
    assert fields["prologue_len"] == h.prologue_end
    st = QINT_QEMU.stat()
    if (fields["exe_st_ino"], fields["exe_st_mtim_ns"]) != (st.st_ino, st.st_mtime_ns):
        pytest.skip("the integrated binary was rebuilt after this capture")
    ri = runtime_identity(qemu=QINT_QEMU, plugin=None, config_text=B0_SC.read_text(),
                          sc_header=h, run_info=fields)
    assert ri["prologue_sha256"] == fields["prologue_sha256"]
    assert ri["icache_linesize"] == scm.run_info.icache_linesize
    assert ri["cpuinfo"] == hex(scm.run_info.cpuinfo) and ri["base_cflags"] is None
    # a capture from another runtime is refused
    with pytest.raises(BundleStale) as e:
        runtime_identity(qemu=QINT_QEMU, plugin=None, config_text=B0_SC.read_text(),
                         sc_header=h, run_info={**fields, "exe_st_ino": st.st_ino + 1})
    assert e.value.code == "runtime_mismatch:run_info_exe_st_ino"
    with pytest.raises(BundleStale) as e:
        runtime_identity(qemu=QINT_QEMU, plugin=None, config_text=B0_SC.read_text(),
                         sc_header=h.replace(code_address=h.code_address + 0x1000),
                         run_info=fields)
    assert e.value.code == "runtime_mismatch:run_info_code_address"
