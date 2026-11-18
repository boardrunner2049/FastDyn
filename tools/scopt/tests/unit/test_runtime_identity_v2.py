"""WP17: RuntimeIdentity v2 admission block, V20 and obs-contract@1 (design §7.2, §9.1)."""

from __future__ import annotations

import copy
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from scopt.analysis.obs_contract import build_contract
from scopt.formats.bundle_v1 import (BundleError, BundleIdMismatch, BundleSchemaError,
                                     admission_facts, admission_of, extend_runtime_identity,
                                     mem_get_value_outside_mem_cb, read_runtime_identity,
                                     tb_start_cb_possible)
from scopt.formats.envlayout_v1 import find_envlayout
from scopt.formats.enums import OpMarkKind
from scopt.model.unit import OpMark
from scopt.validate import Validation, v20_rows
from scopt.paths import fastdyn_root

ROOT = fastdyn_root()
QINT_SHA = "sha256:8864c72671971cf4488b5afa76eeee02a3e2a472c45af81ec1314464e6035f41"
FASTDYN = ROOT / "src/fastdyn"
PLUGIN = FASTDYN / "build/libfastdyn.so"
COMPUTE_TOML = ROOT / "runs/b2/compute/prep/oracle/run.toml"
COMPUTE_VIRTUALS = ROOT / "runs/b2/compute/prep/oracle/work/virtuals/virtuals.txt"
VW_TOML = FASTDYN / "configs/variable_watch.toml"
MIN_SNAP = ROOT / "runs/wp15a/minimal/store/snapshots/" \
    "f1ed14d98db4bcae54a1c37f07b1d6fabcfcea345a56edf1df1846c4d0aaee44"


def manifest(*, twintrace: str = "off", fuzzing: str = "0", cpu: str = "cortex-m3",
             plugin: Path | None = PLUGIN, extra_cf: dict | None = None,
             vlibs: list[str] | None = None, machine: str = "cortexm") -> dict:
    nf: dict = {"plugin": None if plugin is None else {"path": str(plugin), "size": 1,
                                                       "mtime_ns": 1}}
    for i, p in enumerate(vlibs or []):
        nf[f"virtual_library_{i}"] = {"path": p, "size": 1, "mtime_ns": 1}
    cf = {"virtuals": {"sha256": "00" * 32, "size": 1}}
    cf.update(extra_cf or {})
    return {"version": 1, "inputs": {
        "content_files": cf, "native_files": nf,
        "settings": {"cpus": [{"arch": "arm", "cpu": cpu, "machine": machine,
                               "init_nsvtor": "0x0"}],
                     "plugin_options": ["log_level=20", "coverage=0", f"fuzzing={fuzzing}",
                                        "edge_coverage=0", f"twintrace={twintrace}"],
                     "supercache_flat": False}}}


def need_plugin():
    if not PLUGIN.is_file():
        pytest.skip(f"{PLUGIN} not present")


def ri_v1(sha: str = QINT_SHA) -> dict:
    from scopt.ids import runtime_identity_id

    doc = {"schema": "scopt.runtime_identity/1", "qemu_sha256": sha, "supercache_flat": False,
           "code_address": "0x7fff6c000000"}
    doc["runtime_identity_id"] = runtime_identity_id(doc)
    return doc


def facts(**kw) -> dict:
    lay = find_envlayout(kw.pop("sha", QINT_SHA))
    return admission_facts(manifest=kw.pop("man", manifest()), qemu_sha256=QINT_SHA,
                           envlayout=lay, **kw)


# -- tb_start_cb_possible ------------------------------------------------------------------------

@pytest.mark.real
def test_compute_config_has_no_tb_start_callback():
    need_plugin()
    if not COMPUTE_TOML.is_file():
        pytest.skip(f"{COMPUTE_TOML} not present")
    a = facts(config_text=COMPUTE_TOML.read_text(),
              virtuals_text=COMPUTE_VIRTUALS.read_text() if COMPUTE_VIRTUALS.is_file() else None)
    assert a["tb_start_cb_possible"] is False
    assert a["tb_start_cb_reasons"] == ["config:no_tb_hook_no_twintrace"]
    assert a["cpu_model"] == "cortex-m3"
    assert a["mem_get_value_outside_mem_cb"] is False  # libfastdyn.so does not import it
    assert a["mem_get_value_evidence"] == {"plugin": "absent"}
    assert a["ac4_excluded_virtuals_present"] == []
    assert a["exec_vaddr_aliases"] == []


@pytest.mark.real
def test_twintrace_config_has_tb_start_callback():
    need_plugin()
    a = facts(man=manifest(twintrace="record"))
    assert a["tb_start_cb_possible"] is True
    assert "twintrace=record" in a["tb_start_cb_reasons"]


@pytest.mark.real
def test_variable_watch_config_has_tb_start_callback():
    need_plugin()
    if not VW_TOML.is_file():
        pytest.skip(f"{VW_TOML} not present")
    a = facts(config_text=VW_TOML.read_text())
    assert a["tb_start_cb_possible"] is True
    assert "plugin:variable_watch" in a["tb_start_cb_reasons"]
    # the preprocessing artifacts of a run preprocessor alone also make it possible
    b = facts(man=manifest(extra_cf={"preprocessing_artifact_variable_watch_0":
                                     {"sha256": "11" * 32, "size": 3}}))
    assert b["tb_start_cb_possible"] is True


def test_unknown_inputs_fail_closed():
    a = admission_facts(manifest=None, qemu_sha256=QINT_SHA)
    assert a["tb_start_cb_possible"] is True and a["tb_start_cb_reasons"] == ["unknown:no_manifest"]
    assert a["mem_get_value_outside_mem_cb"] is None
    b = facts(man=manifest(plugin=None))
    assert b["tb_start_cb_possible"] is True and b["mem_get_value_outside_mem_cb"] is None
    c = facts(man=manifest(vlibs=["/nonexistent/libx.so"]))
    assert c["tb_start_cb_possible"] is True
    assert "virtual_library_0:unknown" in c["tb_start_cb_reasons"]


def test_ac4_and_aliases():
    a = facts(man=manifest(fuzzing="1"), virtuals_text="0x100 ardupilot_init\n")
    assert a["ac4_excluded_virtuals_present"] == ["ardupilot", "fuzz_aflnet"]
    assert facts(man=manifest(machine="mps2-an385"))["exec_vaddr_aliases"][0]["alias"] == \
        "0x400000"
    assert facts(man=manifest(machine="weird"))["exec_vaddr_aliases"] is None


# -- RuntimeIdentity v2 ---------------------------------------------------------------------------

def test_extend_and_read_v2():
    a = facts()
    ri = ri_v1()
    v2 = extend_runtime_identity(ri, a)
    assert v2["schema"] == "scopt.runtime_identity/2"
    assert v2["runtime_identity_id"] != ri["runtime_identity_id"]
    assert read_runtime_identity(v2) == v2
    assert read_runtime_identity(ri) == ri  # v1 still accepted
    assert admission_of(v2) == a and admission_of(ri) is None
    assert tb_start_cb_possible(ri) is True  # unknown => true
    assert tb_start_cb_possible(v2) is a["tb_start_cb_possible"]
    assert mem_get_value_outside_mem_cb(ri) is None
    # tamper -> id mismatch
    t = copy.deepcopy(v2)
    t["admission"]["tb_start_cb_possible"] = not t["admission"]["tb_start_cb_possible"]
    with pytest.raises(BundleIdMismatch):
        read_runtime_identity(t)
    # malformed admission
    bad = dict(a)
    del bad["cpu_model"]
    with pytest.raises(BundleSchemaError) as e:
        extend_runtime_identity(ri, bad)
    assert e.value.code == "admission_schema"
    # admission for another exe
    with pytest.raises(BundleError) as e:
        extend_runtime_identity(ri_v1("sha256:" + "cd" * 32), a)
    assert e.value.code == "admission_exe_mismatch"


@pytest.mark.real
@pytest.mark.qemu
def test_runtime_identity_builder_emits_v2(tmp_path):
    qemu = ROOT / "wt/qemu-int/build/qemu-system-arm"
    if not qemu.is_file():
        pytest.skip(f"{qemu} not present")
    from scopt.formats.bundle_v1 import runtime_identity

    adm = facts()
    doc = runtime_identity(qemu=qemu, plugin=None, config_text="[Machine]\n", admission=adm)
    assert doc["schema"] == "scopt.runtime_identity/2" and doc["qemu_sha256"] == QINT_SHA
    read_runtime_identity(doc)


# -- V20 ---------------------------------------------------------------------------------------------

def _v20(ri, **kw):
    v = Validation()
    ok = v20_rows(v, ri, **kw)
    return ok, {r["check"]: r for r in v.rows}


def _units():
    if not (MIN_SNAP / "snapshot.json").is_file():
        pytest.skip(f"{MIN_SNAP / 'snapshot.json'} not present")
    from scopt.importer.import_cache import open_snapshot

    return list(open_snapshot(MIN_SNAP).units())


@pytest.mark.real
def test_v20_admits_cortex_m3_and_cross_checks_tier0():
    units = _units()
    a = facts()
    a["tb_start_cb_possible"] = False
    a["tb_start_cb_reasons"] = ["config:no_tb_hook_no_twintrace"]
    ri = extend_runtime_identity(ri_v1(), a)
    ok, rows = _v20(ri, units=units, need_d3=True)
    assert ok, rows
    assert rows["cpu_model"]["result"] == "pass"
    assert rows["envlayout_bound"]["result"] == "pass"
    assert rows["tb_start_cb_possible"]["result"] == "pass"
    assert rows["exec_vaddr_aliases"]["result"] == "skip"
    # a synthetic prefix plugin call contradicts the false claim
    u = units[0]
    fake = replace(u, opmarks=(OpMark(2, OpMarkKind.CALL, 0, 77),) + tuple(u.opmarks))
    ok, rows = _v20(ri, units=[fake] + units[1:])
    assert not ok
    assert rows["tb_start_cb_possible"]["reason"] == "tb_start_cb_contradicted"
    # a true claim needs no cross-check
    a2 = dict(a, tb_start_cb_possible=True, tb_start_cb_reasons=["twintrace=record"])
    ok, rows = _v20(extend_runtime_identity(ri_v1(), a2), units=[fake])
    assert rows["tb_start_cb_possible"]["result"] == "pass"


def test_v20_refusals():
    a = facts()
    # other CPU model
    ok, rows = _v20(extend_runtime_identity(ri_v1(), dict(a, cpu_model="cortex-m4")),
                    need_d2=False)
    assert not ok and rows["cpu_model"]["reason"] == "unsupported_guest"
    # v1 identity: no admission facts
    ok, rows = _v20(ri_v1())
    assert not ok and rows["admission"]["reason"] == "observer_contract_unknown"
    # another executable: no EnvLayout bound
    other = "sha256:" + "cd" * 32
    a3 = dict(admission_facts(manifest=manifest(), qemu_sha256=other, check_files=False))
    ok, rows = _v20(extend_runtime_identity(ri_v1(other), a3), need_d2=False)
    assert not ok and rows["envlayout_bound"]["reason"] == "guard_layout_unbound"
    # D3 without the plugin fact
    a4 = dict(a, mem_get_value_outside_mem_cb=None)
    ok, rows = _v20(extend_runtime_identity(ri_v1(), a4), need_d2=False, need_d3=True)
    assert not ok and rows["mem_get_value"]["reason"] == "observer_contract_unknown"
    # AC4
    a5 = dict(a, ac4_excluded_virtuals_present=["fuzz_aflnet"])
    ok, rows = _v20(extend_runtime_identity(ri_v1(), a5), need_d2=False)
    assert not ok and rows["ac4_virtuals"]["reason"] == "ac4_excluded_virtual"
    # a false claim without units cannot be checked
    a6 = dict(a, tb_start_cb_possible=False)
    ok, rows = _v20(extend_runtime_identity(ri_v1(), a6))
    assert not ok and rows["tb_start_cb_possible"]["reason"] == "tb_start_cb_unchecked"


# -- obs-contract@1 ----------------------------------------------------------------------------------

def test_obs_contract_rules():
    lay = find_envlayout(QINT_SHA)
    a = facts()
    v1 = build_contract(lay, ri_v1())
    assert v1.tb_start_cb_possible is True and v1.mem_get_value_outside_mem_cb is None
    t = build_contract(lay, extend_runtime_identity(ri_v1(), dict(a, tb_start_cb_possible=True)))
    f = build_contract(lay, extend_runtime_identity(
        ri_v1(), dict(a, tb_start_cb_possible=False, mem_get_value_outside_mem_cb=False)))
    # classes
    assert f.deletable_class("NF") == "D1" and f.deletable_class("regs[3]") == "D1"
    assert f.deletable_class("neg.can_do_io") == "D2"
    assert f.deletable_class("neg.plugin_mem_value_low") == "D3"
    assert v1.deletable_class("neg.plugin_mem_value_low") is None
    assert v1.not_deletable_reason("neg.plugin_mem_value_low") == "observer_contract_unknown"
    assert f.not_deletable_reason("regs[15]") == "pc_store_required"
    assert f.not_deletable_reason("thumb") == "not_bookkeeping_class"
    assert f.not_deletable_reason("hflags.flags2") == "not_bookkeeping_class"
    assert f.not_deletable_reason("QF") == "field_not_deletable_class"
    # D1 observable at real sync points, not at pseudo points
    assert all(f.observable("NF", e) for e in ("S1", "S2", "S3", "S4", "S5", "S6"))
    assert not f.observable("NF", "P")
    # can_do_io: S4/S5 always; direct-chain S2 only when tb_start_cb_possible
    for c in (t, f):
        assert c.observable("neg.can_do_io", "S4") and c.observable("neg.can_do_io", "S5")
        assert not c.observable("neg.can_do_io", "S1")
        for k in ("exit_tb", "exit_requested", "goto_ptr", "exit_null"):
            assert not c.cdio_observed_at_exit(k)
    assert t.cdio_observed_at_exit("goto_tb") and t.cdio_observed_at_exit("guarded_slot")
    assert not f.cdio_observed_at_exit("goto_tb") and not f.cdio_observed_at_exit("guarded_slot")
    assert t.observable("neg.can_do_io", "S2")  # unknown exit kind: direct (fail closed)
    # D3 observable at S4 only
    assert f.observable("neg.plugin_mem_value_low", "S4")
    assert not f.observable("neg.plugin_mem_value_low", "S5")
    # unlisted fields: observable everywhere, P included
    assert f.observable("thumb", "P") and f.observable("neg.icount_decr.u32", "S1")
    with pytest.raises(ValueError):
        f.observable("NF", "S9")
    assert f.offsets["neg.can_do_io"] == -4 and f.offsets["neg.plugin_mem_value_high"] == -0x10
    assert t.contract_id != f.contract_id and json.dumps(f.to_json())
