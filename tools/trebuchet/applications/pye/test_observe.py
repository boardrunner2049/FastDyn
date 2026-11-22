"""The real queued training output in a minimal launcher receipt exercises the observer."""
import json
from pathlib import Path
from shutil import copyfile
import sys

import pytest

APP = Path(__file__).resolve().parent
sys.path.insert(0, str(APP))
from observe import observe, sha


@pytest.fixture
def trial(tmp_path):
    archived = APP / "qualification/queued-patched"
    peer = tmp_path / "run-peer"
    peer.mkdir()
    result = json.loads((archived / "result.json").read_text())["application"]
    for name in ("uart.bin", "returned.utf8"):
        copyfile(archived / name, peer / name)
    receipt = peer / "result.json"
    receipt.write_text(json.dumps(result))
    stage = tmp_path / "stage.json"
    stage.write_text(json.dumps({k: result[k] for k in (
        "case", "protocol", "protocol_sha256", "role", "keys_sha256", "expected_output_sha256", "romfs_sha256"
    )}))
    trial = tmp_path / "run.json"
    trial.write_text(json.dumps({"verdict": "PASS", "exit_code": 0, "timed_out": False, "appended": 0,
                                 "inputs": {"ram0": {"sha256": "sha256:" + result["romfs_sha256"]}},
                                 "application_peer": {"receipt": str(receipt), "receipt_sha256": "sha256:" + sha(receipt),
                                                      "result": result, "spec": {"bank": "ram0", "stage": {
                                                          "path": str(stage), "sha256": "sha256:" + sha(stage)}}}}))
    return trial


def test_genuine_output_matches_frozen_edit(trial):
    result = observe(trial, trial)
    assert result["completed_work"] == 1
    assert result["observations"]["returned_bytes"] == 166


@pytest.mark.parametrize("condition", ["different_protocol", "appended_block", "edited_buffer", "stale_receipt"])
def test_rejects_incomplete_or_changed_evidence(trial, condition):
    doc = json.loads(trial.read_text())
    receipt = Path(doc["application_peer"]["receipt"])
    if condition == "different_protocol":
        result = doc["application_peer"]["result"]
        result["protocol"] = "reactive"
        receipt.write_text(json.dumps(result))
        doc["application_peer"]["receipt_sha256"] = "sha256:" + sha(receipt)
    elif condition == "appended_block":
        doc["appended"] = 1
    elif condition == "edited_buffer":
        receipt.with_name("returned.utf8").write_text("different piece of work")
    else:
        receipt.write_text("{}")
    trial.write_text(json.dumps(doc))
    with pytest.raises(ValueError):
        observe(trial, trial)
