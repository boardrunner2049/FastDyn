import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scopt.analysis.activation_layout import (
    ActivationLayoutError,
    _Geometry,
    _Trace,
    _Unit,
    _bind,
    _choose,
    _load_trace,
    _operations,
)
from scopt.run.launcher import file_sha256


def unit(slot, entry, guest):
    return _Unit(f"unit-{slot}", slot, entry, 192, 64, entry + 64,
                 (guest,), (0, 4), (4, 0xFFFF))


def test_layout_prefers_lowest_slot_and_keeps_entry_page_boundary():
    rows = (unit(0, 0x100C0, 0x100), unit(1, 0x10400, 0x200))
    ops = [("M", None), ("X", 0), ("W", ((1, -96),)), ("X", 0),
           ("W", ((1, -96),)), ("X", 0)]
    one = _choose(rows, ops, {1, 0}, 0x40000, 8)
    two = _choose(rows, ops, {0, 1}, 0x40000, 8)
    assert one == two == ([0], [0x18000, 0x10400], (2, 2), (0, 0))
    assert _choose(rows, ops, {0, 1}, 0x18020, 8) == (
        [], [0x100C0, 0x10400], (2, 2), (2, 2))


def test_trace_uses_abi_metadata_and_rejects_absent_slot():
    units = (unit(0, 0x100C0, 0x100), unit(1, 0x10400, 0x200))
    geometry = _Geometry(152, (96, 120, 136, 80, 112), (8, 8, 8, 1, 8))
    trace = _Trace(((1, "dispatch", (0,)), (2, "link", (0, 0, 1))), (1, 3), "explicit_dispatch")
    ops, activated, writes = _operations(geometry, units, trace)
    assert activated == {0, 1}
    assert writes == {"activation": 2, "link": 1}
    assert ops[-1] == ("W", ((0, 4), (0, -96), (0, -72), (0, -56), (1, -112), (1, -80)))
    bad = _Trace(((1, "dispatch", (0,)), (2, "link", (0, 1, 1))), (1, 3), "explicit_dispatch")
    with pytest.raises(ActivationLayoutError, match="absent outgoing slot"):
        _operations(geometry, units, bad)


def test_terminal_evidence_is_bound_to_trace_and_last_guest_extent(tmp_path):
    trace = tmp_path / "trace.log"
    trace.write_text("Trace 0: 0x100c0 [0/00000100/\n"
                     "Linking TBs 0x100c0 index 0 -> 0x10400\n"
                     "Trace 0: 0x10400 [0/00000200/\n")
    rows = (unit(0, 0x100C0, 0x100), unit(1, 0x10400, 0x200))
    marks = {"benchmark_start": 0x100, "benchmark_end": 0x204}
    digest = file_sha256(trace)
    plugin = "sha256:" + "1" * 64
    receipt = {"trace_sha256": digest, "plugin_sha256": plugin, "last_dispatch": [3, 0x10400, 0x200],
               "terminal_marker_pc": 0x204, "synchronous_exit_after_timer_and_flush": True,
               "exact_guest_oracle": True}
    check = _load_trace(trace, rows, marks, digest, receipt, plugin)
    assert check.bounds == (1, 4)
    assert check.end_kind == "verified_terminal_callback"
    with pytest.raises(ActivationLayoutError, match="requires terminal"):
        _load_trace(trace, rows, marks, digest, None, plugin)
    with pytest.raises(ActivationLayoutError, match="does not bind"):
        _load_trace(trace, rows, marks, digest, receipt | {"trace_sha256": "wrong"}, plugin)
    with pytest.raises(ActivationLayoutError, match="does not bind"):
        _load_trace(trace, rows, marks | {"benchmark_end": 0x202}, digest,
                    receipt | {"terminal_marker_pc": 0x202}, plugin)
    with pytest.raises(ActivationLayoutError, match="unknown host code address"):
        trace.write_text(trace.read_text().replace("Trace 0: 0x10400", "Trace 0: 0x10600"))
        _load_trace(trace, rows, marks, file_sha256(trace), receipt, plugin)


def test_manifest_checks_marker_instruction_and_final_event(tmp_path):
    trace = tmp_path / "trace.log"
    trace.write_text("Trace 0: 0x100c0 [0/00000100/\nTrace 0: 0x10400 [0/00000200/\n")
    units = (unit(0, 0x100C0, 0x100), unit(1, 0x10400, 0x200))
    marks = {"benchmark_start": 0x100, "benchmark_end": 0x204}
    first = {"line": 1, "guest_start": "0x100", "uid": "unit-0", "slot": 0,
             "native_entry": "0x100c0", "marker_pc": "0x100", "marker_insn_index": 0}
    last = {"line": 2, "guest_start": "0x200", "uid": "unit-1", "slot": 1,
            "native_entry": "0x10400", "marker_pc": "0x204", "marker_insn_index": 1}
    manifest = {"trace_bounds": [1, 2], "start_marker": first, "end_marker": last}
    trace_data = _load_trace(trace, units, marks, file_sha256(trace), None, "", manifest)
    assert trace_data.bounds == (1, 2)
    assert trace_data.end_kind == "verified_marker_dispatch"
    with pytest.raises(ActivationLayoutError, match="manifest markers"):
        _load_trace(trace, units, marks, file_sha256(trace), None, "",
                    manifest | {"end_marker": last | {"marker_insn_index": 0}})
    trace.write_text(trace.read_text() + "Linking TBs 0x100c0 index 0 -> 0x10400\n")
    with pytest.raises(ActivationLayoutError, match="final dispatch"):
        _load_trace(trace, units, marks, file_sha256(trace), None, "", manifest)


@pytest.fixture
def bound_launch(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    cache = snapshot / "cache.sc"
    cache.write_bytes(b"original contents")

    def write(name, text):
        path = tmp_path / name
        path.write_text(text)
        return path

    qemu = write("qemu", "exact qemu")
    plugin = write("plugin", "exact plugin")
    config = write("config.toml", '[[CPU.cpu0]]\narch="arm"\n'
                   '[[CPU.cpu0.virtuals]]\nat="0x100"\ninstruction="benchmark_start"\n'
                   '[[CPU.cpu0.virtuals]]\nat="0x200"\ninstruction="benchmark_end"\n')
    virtuals = write("virtuals", "0x100 benchmark_start\n0x200 benchmark_end\n")
    trace = write("trace", "raw")
    bank = write("input", "frozen")
    expected = write("guest.norm", "result")
    frozen = {str(p): file_sha256(p) for p in (qemu, plugin, config, expected, bank)}
    control = {"qemu_sha256": file_sha256(qemu), "plugin_sha256": file_sha256(plugin),
               "ordinary_warm": str(snapshot), "warm_cache_sha256": file_sha256(cache),
               "purpose": "untimed training only, outer dispatch/link-write trace",
               "frozen": frozen, "input_bank": str(bank), "expected_guest": str(expected)}
    raw = {k: control[k] for k in ("qemu_sha256", "plugin_sha256", "ordinary_warm")}
    raw.update(proof={"verdict": "PASS", "file_version": 7, "v7_kind": "public",
                      "appended": 0, "n": 2, "record_count_after": 2},
               exact_guest_oracle=True, trace=str(trace), trace_sha256=file_sha256(trace),
               input={"input": {"path": str(bank), "sha256": file_sha256(bank)}},
               argv=[str(qemu), "-accel", f"tcg,supercache={cache}", "-plugin",
                     f"{plugin},virtual={virtuals}", "-d", "exec", "-D", str(trace)])
    runtime = {k: control[k] for k in ("qemu_sha256", "plugin_sha256")}
    runtime |= {"supercache_flat": False, "runtime_identity_id": "sha256:" + "1" * 64}
    source = SimpleNamespace(header=SimpleNamespace(version=7, reserved=0, record_count=2), runtime=runtime,
                             path=snapshot, snapshot_id="sha256:" + "2" * 64)
    receipt = write("trace.json", json.dumps(raw))
    protocol = write("protocol.json", json.dumps(control))
    return source, qemu, config, receipt, protocol, trace, virtuals, cache


def test_binds_frozen_trace_and_markers(bound_launch):
    source, qemu, config, receipt, protocol, trace, *_ = bound_launch
    result = _bind(source, qemu, config, receipt, protocol)
    assert result[:3] == (trace, file_sha256(trace), {"benchmark_start": 0x100, "benchmark_end": 0x200})
    assert result[3]["snapshot_id"] == source.snapshot_id


def test_manifest_binds_snapshot_and_no_input_fixed_work(bound_launch):
    source, qemu, config, receipt, protocol, trace, *_ = bound_launch
    raw, control = json.loads(receipt.read_text()), json.loads(protocol.read_text())
    raw.update(input={}, source_revision="abc")
    control.update(input_bank=None, source_revision="abc")
    receipt.write_text(json.dumps(raw))
    protocol.write_text(json.dumps(control))
    expected = Path(control["expected_guest"])
    manifest = {
        "snapshot": source.snapshot_id, "snapshot_path": str(source.path),
        "trace": str(trace), "trace_sha256": file_sha256(trace),
        "trace_receipt": str(receipt), "trace_receipt_sha256": file_sha256(receipt),
        "config": str(config), "config_sha256": file_sha256(config),
        "expected_guest": str(expected), "expected_guest_sha256": file_sha256(expected),
        "qemu_sha256": raw["qemu_sha256"], "plugin_sha256": raw["plugin_sha256"],
        "source_revision": "abc", "training_input": {}, "exact_guest_oracle": True,
        "zero_appends": True, "snapshot_tbs": 2,
    }
    bound = _bind(source, qemu, config, receipt, protocol, manifest)[3]
    assert bound["training_input_sha256"] == {}
    with pytest.raises(ActivationLayoutError, match="manifest digests"):
        _bind(source, qemu, config, receipt, protocol, manifest | {"snapshot": "wrong"})
    with pytest.raises(ActivationLayoutError, match="manifest does not bind"):
        _bind(source, qemu, config, receipt, protocol,
              manifest | {"training_input": {"input": "unverified"}})


@pytest.mark.parametrize(("index", "message"), [(5, "trace SHA"), (6, "marker file differs"),
                                                (7, "cache bytes changed")])
def test_refuses_changed_bound_evidence(bound_launch, index, message):
    source, qemu, config, receipt, protocol, *_ = bound_launch
    path: Path = bound_launch[index]
    path.write_text("replacement")
    with pytest.raises(ActivationLayoutError, match=message):
        _bind(source, qemu, config, receipt, protocol)
