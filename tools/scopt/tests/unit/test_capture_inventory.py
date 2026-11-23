"""Exercise the actual capture loop, using deterministic retained record counts."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from scopt.run import prepare as capture


@pytest.mark.parametrize(("deltas", "expected_runs", "reason"), [
    ([5, 1, 0, 0, 1, 0, 0, 0, 0, 0, 4, 0], 10, "stable"),
    ([5, 1, 0, 1, 0, 0], 6, "cap"),
])
def test_complete_consecutive_inventory_sweeps(tmp_path, monkeypatch, deltas, expected_runs, reason):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[Machine]\nsupercache="fw.sc"\nsupercache_flat=false\n')
    out = tmp_path / "prep"
    out.mkdir()
    cache = out / "fw.sc"
    fake = SimpleNamespace(cache=cache, manifest=None)
    monkeypatch.setattr(capture, "create_run_dir", lambda *args, **kwargs: fake)
    monkeypatch.setattr(capture, "read_sc", lambda p, **_kw: SimpleNamespace(
        header=SimpleNamespace(record_count=int(p.read_text())), valid_slots=range(int(p.read_text()))))
    calls = []

    def launch(rd, **options):
        index = len(calls)
        calls.append(options["inputs"])
        cache.write_text(str(sum(deltas[:index + 1])))
        Path(options["extra_env"]["QEMU_SC_CAPTURE"]).write_bytes(b"fixture")
        log = out / "stdout.log"
        log.write_text(capture.CAPTURE_BANNER)
        return {"stdout_log": str(log), "timed_out": False, "exit_code": 0,
                "qemu": "fixture", "qemu_sha256": "fixture"}

    monkeypatch.setattr(capture, "launch", launch)
    inputs = [tmp_path / n for n in ("a", "b")] * (len(deltas) // 2)
    result = capture.prepare(cfg, out, inputs=inputs, stable_sweep_size=2, oracle=False)
    assert result["verdict"] == "PASS"
    assert len(calls) == expected_runs
    assert result["inventory"]["stopping_reason"] == reason
    by_input = [n for sweep in result["inventory"]["sweeps"] for n in sweep["appended_per_input"]]
    assert by_input == deltas[:expected_runs]
    assert [c["input"] for c in calls] == list(map(str, inputs[:expected_runs]))


def test_mismatched_inventory_sweeps_refused_before_run(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[Machine]\nsupercache="fw.sc"\nsupercache_flat=false\n')
    with pytest.raises(capture.PrepareError, match="complete sweeps"):
        capture.prepare(cfg, tmp_path / "unused", inputs=["a", "b", "a", "c"], stable_sweep_size=2)
    assert not (tmp_path / "unused").exists()
