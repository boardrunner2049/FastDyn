import json

import pytest

from historical_eligibility import excluded_b4


@pytest.mark.parametrize("change", ["none", "timeout", "wrong_snapshot", "unlinked", "missing_failure", "wrong_stage"])
def test_only_complete_deterministic_legacy_refusals_are_ineligible(tmp_path, change):
    dest = tmp_path / "pre-change"
    root = dest / "registry/firmbolt.b4"
    refusal = root / "rejections"
    refusal.mkdir(parents=True)
    digest = "a" * 64
    image = {"snapshot": "/snapshot/" + digest, "admission_runtime": "/runtime"}
    candidate = "b4-D1-D2-D3-" + digest[:12]
    path = refusal / (candidate + ".json")
    reason = "V12:validation_inconclusive"
    detail = "timeout" if change == "timeout" else "unsupported: register:bh"
    argv = ["python", str(dest / "tools/scopt"), "b4", "package", image["snapshot"], "--store", str(root),
            "--runtime", "/runtime", "--enable", "eliminate-state-sync"]
    if change == "wrong_stage":
        argv[-1] = "other-stage"
    (root / "command.json").write_text(json.dumps(argv))
    (root / "stdout.json").write_text(json.dumps({"status": "rejected", "reason": reason, "artifact_id": None,
                                                  "path": str(path), "proposal": {"outcome": "candidate"}}))
    check = {"verdict": "inconclusive", "reason": "validation_inconclusive", "result": "fail",
             "detail": detail, "code": "V12"}
    path.write_text(json.dumps({"schema": "scopt.rejection", "version": 1, "reason": reason,
                                "candidate_id": candidate, "link": {"ok": change != "unlinked"},
                                "ledger": [{"id": candidate, "verdict": "fail", "reason": reason}],
                                "validation": {"schema": "scopt.validation", "verdict": "rejected",
                                               "codes": {"V12": "fail"},
                                               "rows": [] if change == "missing_failure" else [check]}}))
    row = {"id": "firmbolt/b4", "status": "failed", "recipe": ["b4"], "image_variant": "firmbolt",
           "ordinary_snapshot_id": "sha256:" + ("b" * 64 if change == "wrong_snapshot" else digest)}
    if change == "none":
        assert excluded_b4(dest, row, image, "old")["unsupported"] == [check]
    else:
        with pytest.raises(ValueError):
            excluded_b4(dest, row, image, "old")
