"""Regression checks for fixed-width call-site counting in the sealed cache."""

import struct
from dataclasses import replace

import pytest

from scopt.formats.enums import Encoding, SiteKind
from scopt.formats.sc_records import ScRecord, read_sc
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
    image = read_sc(path, strict=True).units()[0]
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


def _phase_launch(tmp_path, env_set=None, kernel=True, virtuals=None, plugin_args=None,
                  plugin_flag="--plugin"):
    import json

    tmp_path = tmp_path / f"launch{len(list(tmp_path.glob('launch*')))}"
    tmp_path.mkdir()
    argv = ["qemu", "-accel", "tcg,supercache=x.sc"]
    if kernel:
        (tmp_path / "fw.elf").write_bytes(b"\x7fELF")
        argv += ["-kernel", "fw.elf"]
    if virtuals is not None:
        (tmp_path / "virtuals.txt").write_text(virtuals)
        plugin_args = plugin_args or ["virtual=virtuals.txt"]
    if plugin_args is not None:
        argv += [plugin_flag, ",".join(["libfastdyn.so", "dev=dev.json", *plugin_args])]
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
                    "source": "symbol:vComputeWindowStart", "explicit": False,
                    "candidates": [{"source": "symbol:vComputeWindowStart", "vaddr": "0x833e8"}]}
    # The launch's own markers win when they name the pipeline phase.
    marked = _phase_launch(tmp_path, {"QEMU_SC_PROFILE_PHASES": "warm@0x100,steady@0x201"})
    got = H.resolve_training_phases(marked, "auto", "steady")
    assert got["phases"] == "warm@0x100,steady@0x200" and got["source"] == "launch"
    assert got["candidates"] == []
    # Explicit specs: hex, symbol, and their errors.
    explicit = H.resolve_training_phases(launch, "steady@0x81BA1", "steady")
    assert explicit["marker"] == "0x81ba0" and explicit["candidates"] == []
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
    assert unmarked["candidates"] == []
    no_kernel = H.resolve_training_phases(_phase_launch(tmp_path, kernel=False), "auto", "steady")
    assert no_kernel["source"] == "whole_run_unmarked" and no_kernel["candidates"] == []


@pytest.mark.parametrize("wanted", ["steady_window_16", "bad name", "", 7])
def test_invalid_pipeline_phase_is_refused_for_every_source(tmp_path, monkeypatch, wanted):
    """A pipeline phase QEMU would refuse never degrades to an unmarked whole run."""
    import scopt.run.helper_pgo as H

    monkeypatch.setattr(H, "_symbols", lambda kernel: {"vComputeWindowStart": [0x81ba0]})
    launches = [_phase_launch(tmp_path), _phase_launch(tmp_path, virtuals=_UART_VIRTUALS),
                _phase_launch(tmp_path, kernel=False)]
    for launch in launches:
        with pytest.raises(H.PhaseSpecError, match="bad pipeline phase"):
            H.resolve_training_phases(launch, "auto", wanted)
    with pytest.raises(H.PhaseSpecError, match="bad pipeline phase"):
        H.resolve_training_phases(launches[0], "steady@0x81ba0", wanted)
    # The whole run on request never names a phase.
    assert H.resolve_training_phases(launches[0], "boot", wanted)["source"] == "whole_run_explicit"


_UART_VIRTUALS = """0x822fc benchmark_start
0x822fe debug_log window-start
0x8231a printreg 0
0x81bec benchmark_end fw
0x2702 benchmark_end fw-FAILURE-DEFAULT-HANDLER
"""


def test_auto_prefers_the_launch_benchmark_start_virtual(tmp_path, monkeypatch):
    """FirmBOLT -O4 shape (uart): the public vComputeWindowStart body at 0x81ba0 is dead,
    the launch's benchmark_start virtual names the executed inlined copy at 0x822fc."""
    import hashlib

    import scopt.run.helper_pgo as H

    monkeypatch.setattr(H, "_symbols", lambda kernel: {"vComputeWindowStart": [0x81ba0]})
    launch = _phase_launch(tmp_path, virtuals=_UART_VIRTUALS)
    got = H.resolve_training_phases(launch, "auto", "steady")
    digest = "sha256:" + hashlib.sha256(_UART_VIRTUALS.encode()).hexdigest()
    assert got["phases"] == "steady@0x822fc" and got["marker"] == "0x822fc"
    assert got["source"] == "launch_virtual:benchmark_start" and got["explicit"] is False
    assert got["candidates"] == [
        {"source": "launch_virtual:benchmark_start", "vaddr": "0x822fc",
         "virtuals": str(launch.parent / "virtuals.txt"), "virtuals_sha256": digest},
        {"source": "symbol:vComputeWindowStart", "vaddr": "0x81ba0",
         "not_chosen": "launch_virtual:benchmark_start_precedence"}]
    # The launch's QEMU_SC_PROFILE_PHASES still wins; an explicit marker is unchanged.
    marked = _phase_launch(tmp_path, {"QEMU_SC_PROFILE_PHASES": "steady@0x900"},
                           virtuals=_UART_VIRTUALS)
    assert H.resolve_training_phases(marked, "auto", "steady")["source"] == "launch"
    explicit = H.resolve_training_phases(launch, "steady@0x81ba0", "steady")
    assert explicit["source"] == "cli" and explicit["marker"] == "0x81ba0"
    # Packet shape: the symbol itself executes, so both sources agree (Thumb bit masked).
    monkeypatch.setattr(H, "_symbols", lambda kernel: {"vComputeWindowStart": [0x833e8]})
    packet = _phase_launch(tmp_path, virtuals="0x833e9 benchmark_start\n")
    got = H.resolve_training_phases(packet, "auto", "steady")
    assert got["marker"] == "0x833e8" and got["source"] == "launch_virtual:benchmark_start"
    assert got["candidates"][1]["not_chosen"] == "same_address"
    # A virtual file without a benchmark_start row: the symbol as before.
    plain = _phase_launch(tmp_path, virtuals="0x1000 printreg 0\n")
    assert H.resolve_training_phases(plain, "auto", "steady") == {
        "phases": "steady@0x833e8", "phase": "steady", "marker": "0x833e8",
        "source": "symbol:vComputeWindowStart", "explicit": False,
        "candidates": [{"source": "symbol:vComputeWindowStart", "vaddr": "0x833e8"}]}
    # No kernel symbol: the virtual alone; neither: an unmarked whole run.
    monkeypatch.setattr(H, "_symbols", lambda kernel: {})
    assert H.resolve_training_phases(launch, "auto", "steady")["candidates"] == [
        {"source": "launch_virtual:benchmark_start", "vaddr": "0x822fc",
         "virtuals": str(launch.parent / "virtuals.txt"), "virtuals_sha256": digest}]
    assert H.resolve_training_phases(plain, "auto", "steady")["source"] == "whole_run_unmarked"
    # FastDyn's "virtual=None" placeholder and an empty "virtual=" are not files.
    for placeholder in ("virtual=None", "virtual="):
        none = _phase_launch(tmp_path, plugin_args=[placeholder, "modifier=None"])
        assert H.resolve_training_phases(none, "auto", "steady")["source"] == "whole_run_unmarked"
    # QEMU's single-dash -plugin spelling names the same file.
    dash = _phase_launch(tmp_path, virtuals=_UART_VIRTUALS, plugin_flag="-plugin")
    assert H.resolve_training_phases(dash, "auto", "steady")["phases"] == "steady@0x822fc"


def test_auto_refuses_ambiguous_or_unreadable_work_start(tmp_path, monkeypatch):
    import scopt.run.helper_pgo as H

    monkeypatch.setattr(H, "_symbols", lambda kernel: {"vComputeWindowStart": [0x81ba0]})
    two = _phase_launch(tmp_path, virtuals="0x822fc benchmark_start\n0x82fc4 benchmark_start\n")
    with pytest.raises(H.PhaseSpecError, match=r"ambiguous_work_start: .*0x822fc, 0x82fc4; "
                                               r"choose one with --pgo-phase steady@0xADDR"):
        H.resolve_training_phases(two, "auto", "steady")
    # The same address twice (e.g. two CPUs' identical rows) is one marker.
    same = _phase_launch(tmp_path, virtuals="0x822fc benchmark_start\n0x822fd benchmark_start\n")
    assert H.resolve_training_phases(same, "auto", "steady")["marker"] == "0x822fc"
    bad = _phase_launch(tmp_path, virtuals="vComputeWindowStart benchmark_start\n")
    with pytest.raises(H.PhaseSpecError, match="unreadable benchmark_start row 1"):
        H.resolve_training_phases(bad, "auto", "steady")
    missing = _phase_launch(tmp_path, plugin_args=["virtual=gone.txt"])
    with pytest.raises(H.PhaseSpecError, match="cannot read the training launch virtual file"):
        H.resolve_training_phases(missing, "auto", "steady")
    several = _phase_launch(tmp_path, plugin_args=["virtual=a.txt", "virtual=b.txt"])
    with pytest.raises(H.PhaseSpecError, match="ambiguous_work_start: .* 2 virtual files"):
        H.resolve_training_phases(several, "auto", "steady")
    # Explicit specs and boot never read the virtual file.
    assert H.resolve_training_phases(two, "steady@0x82fc4", "steady")["source"] == "cli"
    assert H.resolve_training_phases(missing, "boot", "steady")["source"] == "whole_run_explicit"
