"""Exact native image identity and dependency invalidation."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from dataclasses import replace
from types import SimpleNamespace

import pytest

from scopt.analysis.api import AnalysisSpec, ScopeRef
from scopt.analysis.native_callees import prepare_native_images
from scopt.analysis.registry import AnalysisRegistry
from scopt.analysis.store import AnalysisInput, AnalysisStore, UndeclaredInput
from scopt.canon import sha256_id
from scopt.decode.symbols import Maps, Module
from scopt.formats.bundle_v1 import runtime_identity_id
from scopt.formats.scm_v1 import HelperInfo
from scopt.importer.classify import HostContext
from scopt.importer.native_callee import CalleeResolutionError, NativeCalleeResolver
from scopt.model.unit import HelperBinding
from scopt.passes.base import StaleFact
from tests.synth.fake_snapshot import fake_snapshot


@pytest.fixture(scope="module")
def native_so(tmp_path_factory):
    cc = shutil.which("cc")
    if cc is None or platform.machine() != "x86_64" or platform.system() != "Linux":
        pytest.skip("requires a Linux x86-64 C compiler")
    path = tmp_path_factory.mktemp("native") / "libexample.so"
    subprocess.run([cc, "-shared", "-O2", "-fPIC", "-Wl,--build-id", "-x", "c", "-o",
                    path, "-"], input=b"int helper_test(int value) { return value + 7; }\n",
                   check=True)
    return path


def _fixture(path, role="qemu", *, symbol="helper_test"):
    mod = Module.from_file(path, 0x10000000)
    st = path.stat()
    dev = f"{os.major(st.st_dev):x}:{os.minor(st.st_dev):x}"
    addr = mod.address_of("helper_test")
    binding = HelperBinding(1, "test", path.name, symbol, 5, addr)
    maps = Maps(replace(row, dev=dev, inode=st.st_ino) for row in Maps.from_modules((mod,)))
    qemu_ino = st.st_ino if role == "qemu" else st.st_ino + 1
    ctx = HostContext(0x80000, 0x10000, 0, 0, 0, maps,
                      helpers={1: HelperInfo(1, 5, 0, addr, b"test")}, exe_ino=qemu_ino)
    ri = SimpleNamespace(exe_st_dev=st.st_dev, exe_st_ino=qemu_ino,
                         exe_st_size=st.st_size, exe_st_mtim_ns=st.st_mtime_ns,
                         runtime_signature=1, cpuinfo=2)
    rt = {"runtime_signature": "0x1", "cpuinfo": "0x2",
          f"{role}_sha256": sha256_id(path.read_bytes()),
          ("qemu_exe_stat" if role == "qemu" else "plugin_stat"): {
              "path": str(path), "dev": st.st_dev, "ino": st.st_ino,
              "size": st.st_size, "mtime_ns": st.st_mtime_ns}}
    rt["runtime_identity_id"] = runtime_identity_id(rt)
    return binding, ctx, ri, rt


@pytest.mark.parametrize("role", ["qemu", "plugin"])
def test_native_callee_checks_file_mapping_and_returns_bytes(native_so, role):
    binding, ctx, ri, rt = _fixture(native_so, role)
    callee = NativeCalleeResolver(ctx, ri, rt, sha256_id(b"capture")).resolve(binding)
    want = native_so.read_bytes()[callee.image_offset:callee.image_offset + callee.size]
    assert callee.runtime_address == binding.func == callee.load_bias + callee.elf_address
    assert callee.abi == "sysv-amd64" and callee.role == role
    assert callee.build_id
    assert callee.image_id == rt[f"{role}_sha256"]
    assert callee.code == want and callee.code_hash == sha256_id(want)
    assert callee.code_ranges == ((callee.elf_address, callee.elf_address + callee.size),)


def test_native_callee_refuses_changed_content_even_with_same_stat(native_so, tmp_path):
    path = tmp_path / native_so.name
    shutil.copyfile(native_so, path)
    binding, ctx, ri, rt = _fixture(path)
    st = path.stat()
    with path.open("r+b") as f:
        f.seek(-1, os.SEEK_END)
        f.write(bytes([path.read_bytes()[-1] ^ 1]))
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    with pytest.raises(CalleeResolutionError) as ex:
        NativeCalleeResolver(ctx, ri, rt, sha256_id(b"capture")).resolve(binding)
    assert ex.value.reason == "native_image_mismatch"
    assert "digest" in str(ex.value)


@pytest.mark.parametrize(("change", "reason"), [
    ("bad_symbol", "ambiguous_bounds"), ("changed_binding", "call_binding_unknown"),
    ("wrong_mapping", "native_image_mismatch"), ("uncovered_file", "native_image_unavailable"),
])
def test_native_callee_refuses_unverified_origin(native_so, change, reason):
    binding, ctx, ri, rt = _fixture(native_so)
    if change == "bad_symbol":
        binding = replace(binding, symbol="nonexistent")
    elif change == "changed_binding":
        binding = replace(binding, func=binding.func + 1)
    elif change == "wrong_mapping":
        ctx.maps = Maps(replace(row, inode=row.inode + 7) for row in ctx.maps)
    else:
        del rt["qemu_exe_stat"]
    with pytest.raises(CalleeResolutionError) as ex:
        NativeCalleeResolver(ctx, ri, rt, sha256_id(b"capture")).resolve(binding)
    assert ex.value.reason == reason


def test_external_input_present_changed_removed_and_checked():
    def compute(ctx, scope):
        obj = ctx.external_input("native-images")
        ctx.certify()
        return None if obj is None else obj.value

    snap = fake_snapshot(1)
    spec = AnalysisSpec("binding", 1, "snapshot", (), frozenset({"external:native-images"}),
                        compute, object)
    store = AnalysisStore(snap, AnalysisRegistry([spec]))
    scope = ScopeRef("snapshot", snap.snapshot_id)
    missing = store.get("binding", scope)
    assert missing.value is None and "external:native-images:missing" in missing.dep_inputs

    first = AnalysisInput(sha256_id(b"first"), "qemu")
    store.set_input("native-images", first)
    with pytest.raises(StaleFact):
        store.check_fact(missing)
    r1 = store.get("binding", scope)
    assert "external:native-images:" + first.input_id in r1.dep_inputs
    store.set_input("native-images", AnalysisInput(sha256_id(b"second"), "qemu"))
    with pytest.raises(StaleFact):
        store.check_fact(r1)
    r2 = store.get("binding", scope)
    assert r2.value == r1.value and r2.dep_inputs != r1.dep_inputs
    store.set_input("native-images", None)
    assert store.get("binding", scope).value is None

    undeclared = replace(spec, inputs=frozenset())
    with pytest.raises(UndeclaredInput):
        AnalysisStore(snap, AnalysisRegistry([undeclared])).get("binding", scope)


def test_prepare_input_changes_when_policy_or_backend_changes():
    snap = fake_snapshot(1)
    a = prepare_native_images(snap, backend_revision="test-a", bridge_version=1, policy_version=1)
    b = prepare_native_images(snap, backend_revision="test-b", bridge_version=1, policy_version=1)
    c = prepare_native_images(snap, backend_revision="test-a", bridge_version=1, policy_version=2)
    assert len({a.input_id, b.input_id, c.input_id}) == 3
