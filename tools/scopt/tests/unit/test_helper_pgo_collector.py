"""Regression checks for fixed-width call-site counting in the sealed cache."""

import struct
from dataclasses import replace

import pytest

from scopt.formats.enums import Encoding, SiteKind
from scopt.formats.sc_v5 import ScRecord, read_sc
from scopt.model.unit import PoolEntry, Site
from scopt.run.helper_pgo import _COUNTER_MAGIC, _diagnostic_cache, _guest_lines, _launch
from tests.synth.fake_snapshot import FakeSnapshot, fake_snapshot


def test_call_counting_preserves_both_return_pcs_and_distinguishes_sites(tmp_path):
    fixture = fake_snapshot(1)
    unit = fixture.units()[0]
    base = fixture.header.code_address + unit.tc_offset
    target = base + 0x3000
    direct_disp = target - (base + 5)
    # Both calls have the same callee. FF 15 at +5 reads the slot at +16.
    code = (b"\xe8" + struct.pack("<i", direct_disp)
            + bytes.fromhex("ff1505000000 c3 90909090") + struct.pack("<Q", target))
    record = ScRecord.from_bytes(unit.record).replace(
        tc_size=len(code), end_offset=unit.tc_offset + len(code) + len(unit.search)).with_checksum()
    sites = (Site(1, 0, 4, Encoding.REL32, SiteKind.HELPER_CALL, 0, target, 0, direct_disp),
             Site(7, 5, 4, Encoding.POOL_INDIRECT, SiteKind.LDST_HELPER_CALL, 0, target, 0, 5))
    unit = replace(unit, code=code, end_offset=record.end_offset, tc_size=record.tc_size,
                   sites=sites, record=record.to_bytes(), pool_start=16,
                   pool=(PoolEntry(7, 16, 0, 1, 0, target),))
    snapshot = FakeSnapshot([unit], fixture.snapshot_id, fixture.header.replace(version=7))
    path = tmp_path / "counted.sc"
    rows, counter = _diagnostic_cache(snapshot, path)
    image = read_sc(path, versions=(7,), strict=True).units()[0]
    assert image.search == unit.search
    assert [row["call_off"] for row in rows] == [0, 5]
    assert image.code[5] == 0x90
    for i, ret in enumerate((5, 11)):
        assert image.code[ret - 5] == 0xe8
        stub = ret + struct.unpack_from("<i", image.code, ret - 4)[0]
        assert image.code[stub:stub + 4] == bytes.fromhex("9c48ff05")
        assert base + stub + 8 + struct.unpack_from("<i", image.code, stub + 4)[0] == counter + 8*i
        assert image.code[stub + 8:stub + 15] == bytes.fromhex("9dff2500000000")
        assert struct.unpack_from("<Q", image.code, stub + 15)[0] == target
    count_off = counter - base
    assert image.code[count_off - len(_COUNTER_MAGIC):count_off] == _COUNTER_MAGIC
    assert image.code[count_off:count_off + 16] == bytes(16)


def test_custom_guest_oracle_preserves_order_and_requires_completion():
    launch = {"helper_pgo_oracle": {"output_pattern": "^UART:", "completion_pattern": "^DONE$"}}
    assert _guest_lines(launch, "debug\nUART:x\nUART:y\nDONE") == ["UART:x", "UART:y"]
    with pytest.raises(ValueError):
        _guest_lines(launch, "UART:x")
    with pytest.raises(ValueError):
        _guest_lines(launch, "DONE")


def test_training_launch_isolates_ram_preserving_aliases(tmp_path, monkeypatch):
    source = tmp_path / "input.ram"
    source.write_bytes(b"fixed input")
    rd = tmp_path / "control"
    rd.mkdir()
    bank = "memory-backend-file,mem-path=input.ram,share=on"
    launch = {"cwd": str(tmp_path), "argv": ["qemu", "-accel", "tcg,supercache=old.sc",
              "-object", bank + ",id=a", "-object", bank + ",id=b"], "env_set": {}}
    monkeypatch.setenv("QEMU_SC_PROFILE", "stale.qprof")
    argv, env, inputs = _launch(launch, rd, rd / "test.sc")
    assert argv[4].split(",id=")[0] == argv[6].split(",id=")[0]
    assert f"mem-path={rd}/bank-3.ram" in argv[4]
    assert (rd / "bank-3.ram").read_bytes() == source.read_bytes()
    assert list(inputs) == [str(source)]
    assert "QEMU_SC_PROFILE" not in env
    assert env["FASTDYN_WORK_DIR"] == str(rd)


@pytest.mark.parametrize("trusted", [False, True])
def test_profile_launch_uses_selected_trust_policy(tmp_path, monkeypatch, trusted):
    monkeypatch.delenv("QEMU_SC_TRUSTED", raising=False)
    launch = {"cwd": str(tmp_path), "argv": ["qemu", "-accel", "tcg,supercache=old.sc"],
              "env_set": {}}
    for edge_prof in (None, tmp_path / "edges.qprof"):
        _, env, _ = _launch(launch, tmp_path, tmp_path / "input.sc", qprof=edge_prof, trusted=trusted)
        assert env["QEMU_SC_SEALED"] == "1"
        assert env.get("QEMU_SC_TRUSTED") == ("1" if trusted else None)


def test_profile_phases_only_on_edge_arm(tmp_path, monkeypatch):
    monkeypatch.setenv("QEMU_SC_PROFILE_PHASES", "stale@0x1")
    launch = {"cwd": str(tmp_path), "argv": ["qemu", "-accel", "tcg,supercache=old.sc"],
              "env_set": {"QEMU_SC_PROFILE_PHASES": "declared@0x2"}}
    _, env, _ = _launch(launch, tmp_path, tmp_path / "a.sc", qprof=tmp_path / "e.qprof",
                        phases="steady@0x833e8")
    assert env["QEMU_SC_PROFILE_PHASES"] == "steady@0x833e8"
    for qprof, phases in ((None, "steady@0x833e8"), (tmp_path / "e.qprof", None)):
        _, env, _ = _launch(launch, tmp_path, tmp_path / "a.sc", qprof=qprof, phases=phases)
        assert "QEMU_SC_PROFILE_PHASES" not in env


def _phase_launch(tmp_path, env_set=None, kernel=True):
    import json

    tmp_path = tmp_path / f"launch{len(list(tmp_path.glob('launch*')))}"
    tmp_path.mkdir()
    argv = ["qemu", "-accel", "tcg,supercache=x.sc"]
    if kernel:
        (tmp_path / "fw.elf").write_bytes(b"\x7fELF")
        argv += ["-kernel", "fw.elf"]
    path = tmp_path / "launch.json"
    path.write_text(json.dumps({"cwd": str(tmp_path), "argv": argv, "env_set": env_set or {}}))
    return path


def test_resolve_training_phases(tmp_path, monkeypatch):
    import scopt.run.helper_pgo as H

    symbols = {"vComputeWindowStart": [0x833e8], "twice": [0x10, 0x20]}
    monkeypatch.setattr(H, "_symbols", lambda kernel: symbols)
    launch = _phase_launch(tmp_path)
    auto = H.resolve_training_phases(launch, "auto", "steady")
    assert auto == {"phases": "steady@0x833e8", "phase": "steady", "marker": "0x833e8",
                    "source": "symbol:vComputeWindowStart", "explicit": False}
    # The launch's own markers win when they name the pipeline phase.
    marked = _phase_launch(tmp_path, {"QEMU_SC_PROFILE_PHASES": "warm@0x100,steady@0x201"})
    got = H.resolve_training_phases(marked, "auto", "steady")
    assert got["phases"] == "warm@0x100,steady@0x200" and got["source"] == "launch"
    # Explicit specs: hex, symbol, and their errors.
    assert H.resolve_training_phases(launch, "steady@0x81BA1", "steady")["marker"] == "0x81ba0"
    assert H.resolve_training_phases(launch, "steady@vComputeWindowStart",
                                     "steady")["source"] == "cli"
    for bad in ("steady@missing", "steady@twice", "steady@0xzz", "boot@0x1", "warm@0x1",
                "steady@0x1,steady@0x2", "steady"):
        with pytest.raises(H.PhaseSpecError):
            H.resolve_training_phases(launch, bad, "steady")
    # Whole-run cases are explicit or recorded, never silent.
    assert H.resolve_training_phases(launch, "boot", "steady")["source"] == "whole_run_explicit"
    assert H.resolve_training_phases(launch, "auto", "boot")["phases"] is None
    symbols.clear()
    unmarked = H.resolve_training_phases(launch, "auto", "steady")
    assert unmarked["phases"] is None and unmarked["source"] == "whole_run_unmarked"
    assert H.resolve_training_phases(_phase_launch(tmp_path, kernel=False), "auto",
                                     "steady")["source"] == "whole_run_unmarked"
