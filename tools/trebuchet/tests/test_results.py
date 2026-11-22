"""Result checks of the Trebuchet campaign: named-field oracle and invalid-campaign handling.

Run with `python3 -m pytest tools/trebuchet/tests` (standard library only).
"""
import copy, json, re, subprocess, sys
from pathlib import Path

T = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(T / "lib"))
from result_schema import (FW_FIELDS, FW_REPORTED, REPORTED, STATS_FIELDS, TIMING_DEPENDENT, WORKLOADS,  # noqa: E402
                           expected_values, schema, stats_mismatches)

SOURCE = (T / "firmware/compute/main_compute.c").read_text()
CAMPAIGN = T / "results/2026-11-21/campaign.json"


def test_schema_matches_the_firmware_source():
    """Derive the mapping from main_compute.c independently of the table."""
    body = re.search(r"typedef struct\s*\{([^}]*)\}\s*Stats_t;", SOURCE).group(1)
    fields = re.findall(r"uint32_t\s+(\w+);", body)
    assert tuple(fields) == STATS_FIELDS
    reported = []
    for n in range(1, 5):
        call = re.search(rf"vComputeReport{n}\(\s*(xStats[^;]*?)\s*\);", SOURCE).group(1)   # the call, not the prototype
        for argument in call.split(","):
            names = re.findall(r"xStats\.(\w+)", argument)
            other = re.sub(r"xStats\.\w+|\^|\s", "", argument)
            reported.append(tuple(names) if names and not other else None)
    assert tuple(reported) == REPORTED
    assert not {name for entry in REPORTED if entry for name in entry} & set(TIMING_DEPENDENT)


def stats_for(expected):
    stats = [0x1000 + i for i in range(len(STATS_FIELDS))]          # arbitrary, all distinct
    for fields, want in zip(REPORTED, expected):
        if fields is None:
            continue
        rest = 0
        for name in fields[1:]:
            rest ^= stats[STATS_FIELDS.index(name)]
        stats[STATS_FIELDS.index(fields[0])] = want ^ rest
    return stats


EXPECTED = [0x81A512AF, 0x7896, 0x7896, 0, 0x6D6, 0x10A, 0x914, 0x98BE,
            0x16C, 0x4DE, 0x1412, 0x33, 0xDEADBEEF, 0xC0FFEE, 0x12345678, 0x9ABC]


def test_matching_statistics_pass():
    assert stats_mismatches(stats_for(EXPECTED), EXPECTED) == []


def test_timing_dependent_fields_do_not_matter():
    stats = stats_for(EXPECTED)
    for name in TIMING_DEPENDENT:
        stats[STATS_FIELDS.index(name)] ^= 0xFFFF
    assert stats_mismatches(stats, EXPECTED) == []


def test_expected_values_elsewhere_in_the_structure_do_not_pass():
    """Wrong digest, frames_ok and processed, with the right numbers parked in other words."""
    stats = stats_for(EXPECTED)
    digest, frames, processed = (STATS_FIELDS.index(n) for n in ("digest", "frames_ok", "processed"))
    spare = [STATS_FIELDS.index(n) for n in TIMING_DEPENDENT]
    for index, park in zip((digest, frames, processed), spare):
        stats[park] = stats[index]
        stats[index] ^= 0x5A5A
    problems = stats_mismatches(stats, EXPECTED)
    assert len(problems) == 3 and all("expected" in p for p in problems)


def test_swapped_fields_fail():
    stats = stats_for(EXPECTED)
    a, b = STATS_FIELDS.index("err_crc"), STATS_FIELDS.index("err_len")
    stats[a], stats[b] = stats[b], stats[a]
    assert len(stats_mismatches(stats, EXPECTED)) == 2


def test_corruption_of_any_reported_field_fails():
    for fields in REPORTED:
        if fields is None:
            continue
        stats = stats_for(EXPECTED)
        stats[STATS_FIELDS.index(fields[-1])] ^= 1
        assert stats_mismatches(stats, EXPECTED), fields


def test_reference_transcript_needs_sixteen_values():
    lines = ["Current CPU type: cortex-m3-arm-cpu"] + [f"Reg {i % 4}:0x{v:08x}" for i, v in enumerate(EXPECTED)]
    assert expected_values(lines) == EXPECTED
    try:
        expected_values(lines[:-1])
    except ValueError:
        pass
    else:
        raise AssertionError("a short transcript was accepted")


def report(path, *options):
    return subprocess.run([sys.executable, str(T / "lib/report.py"), str(path), *options], capture_output=True, text=True)


def test_valid_campaign_reproduces_the_published_medians():
    run = report(CAMPAIGN)
    assert run.returncode == 0
    medians = dict(re.findall(r"^(\S+)\s+([\d.]+)\s+[\d.]+$", run.stdout, re.M))
    assert {k: medians[k] for k in ("stock-O0", "fd-O0", "fd-fb", "fd-fb-warm", "fd-fb-scopt")} == {
        "stock-O0": "1173.7", "fd-O0": "1437.4", "fd-fb": "1409.6", "fd-fb-warm": "1358.2", "fd-fb-scopt": "1270.1"}
    assert "1.131x" in run.stdout and "INVALID" not in run.stdout


def test_a_failed_launch_invalidates_the_campaign(tmp_path):
    doc = json.loads(CAMPAIGN.read_text())
    broken = copy.deepcopy(doc)
    victim = next(s for s in broken["samples"] if s["arm"] == "fd-fb-scopt")
    victim["ok"] = False; victim["failures"] = ["report value 0 = 0x00000000, expected 0x81a512af"]
    path = tmp_path / "campaign.json"; path.write_text(json.dumps(broken))
    run = report(path)
    assert run.returncode == 2
    assert "INVALID CAMPAIGN" in run.stdout and "fd-fb-scopt" in run.stdout and "expected 0x81a512af" in run.stdout
    assert "guest_ns" not in run.stdout and "x [" not in run.stdout          # no speedup table

    shown = report(path, "--diagnostic")
    assert shown.returncode == 2 and "DIAGNOSTIC ONLY" in shown.stdout and "x [" in shown.stdout


def test_a_dropped_sample_does_not_make_the_campaign_valid(tmp_path):
    doc = json.loads(CAMPAIGN.read_text())
    doc["samples"] = [s for s in doc["samples"] if not (s["arm"] == "fd-fb-warm" and s["block"] == 3)]
    path = tmp_path / "campaign.json"; path.write_text(json.dumps(doc))
    run = report(path)
    assert run.returncode == 2 and "missing: fd-fb-warm in block 3" in run.stdout


FW_MAIN = (T / "firmware/workloads/fw_main.c").read_text()


def test_fixed_work_schema_matches_the_firmware_source():
    body = re.search(r"typedef struct \{([^}]*)\} FwResult_t;", FW_MAIN).group(1)
    assert tuple(n.strip() for n in body.replace("uint32_t", "").replace(";", "").split(",")) == FW_FIELDS
    call = re.search(r"vComputeReport1\(([^;]*)\);", FW_MAIN.split("void fw_finish")[1]).group(1)
    assert tuple((a.strip(),) if a.strip() in FW_FIELDS else () for a in call.split(",")) == FW_REPORTED
    assert re.findall(r"fw_result\.(\w+) = (\w+);", FW_MAIN) == [(f, f) for f in FW_FIELDS]


def test_workload_numbers_match_the_entry_points():
    selected = dict(re.findall(r"#(?:el)?if FW_WORKLOAD == (\d)\s+(\w+)_start\(\);", FW_MAIN))
    selected = {name: int(number) for number, name in selected.items()}
    selected["algobench"] = 4                                    # the #else branch
    assert "algobench_start();" in FW_MAIN.split("#else")[1]
    assert selected == {name: number for name, number in WORKLOADS.items() if name != "compute"}
    assert schema("compute")[0] == "xStats" and schema("fwbench")[0] == "fw_result"


def test_fixed_work_results_are_checked_at_their_fields():
    expected = [0xE41C9FC6, 0x2EE0, 0, 0]
    assert stats_mismatches([0xE41C9FC6, 0x2EE0], expected, "fwbench") == []
    assert len(stats_mismatches([0x2EE0, 0xE41C9FC6], expected, "fwbench")) == 2      # swapped
    assert len(stats_mismatches([0xE41C9FC6, 0x2EE1], expected, "fwbench")) == 1
    assert stats_mismatches([0xE41C9FC6, 0x2EE0], [0xE41C9FC6, 0x2EE0, 1, 0], "fwbench")   # a non-zero spare register
    lines = [f"Reg {i}:0x{v:08x}" for i, v in enumerate(expected)]
    assert expected_values(lines, "libbench") == expected



def test_campaign_checkpoint_and_completion(tmp_path):
    from campaign import campaign_errors, record_campaign, write_json

    archived = json.loads(CAMPAIGN.read_text())
    blocks = [s for s in archived["samples"] if s["block"] < 3]
    out = tmp_path / "campaign.json"
    with record_campaign(out, "compute", 1) as campaign:
        campaign["selection"] = archived["selection"]
        campaign["samples"].extend(blocks[:7])
        write_json(out, campaign)
        early = json.loads(out.read_text())
        assert early["state"] == "running" and not early["valid"]
        result = report(out)
        assert result.returncode == 2 and "x [" not in result.stdout
        campaign["samples"].extend(blocks[7:])

    completed = json.loads(out.read_text())
    assert completed["state"] == "completed" and completed["run_id"] == early["run_id"]
    assert campaign_errors(completed) == [] and report(out).returncode == 0

    dropped = copy.deepcopy(completed)
    dropped["samples"] = blocks[:7]                        # a complete early block
    assert any("missing:" in e for e in campaign_errors(dropped))
    duplicate = copy.deepcopy(completed)
    duplicate["samples"].append(blocks[0])
    assert any("duplicate:" in e for e in campaign_errors(duplicate))
    misplaced = copy.deepcopy(completed)
    misplaced["samples"][0]["input"] = "final-3"
    assert any("wrong input" in e for e in campaign_errors(misplaced))


def test_failure_before_launch_supersedes_a_successful_campaign(tmp_path):
    import pytest
    from campaign import record_campaign

    out = tmp_path / "campaign.json"
    out.write_text(CAMPAIGN.read_text())
    with pytest.raises(FileNotFoundError):
        with record_campaign(out, "compute", 2):
            raise FileNotFoundError("selection missing")
    doc = json.loads(out.read_text())
    assert doc["state"] == "failed" and doc["samples"] == []
    response = report(out, "--diagnostic")
    assert response.returncode == 2 and "selection missing" in response.stdout


def test_dataset_rejects_stale_and_partial_campaigns(tmp_path):
    import os
    from campaign import record_campaign

    def dataset(*args, dataset_id=None):
        env = dict(os.environ)
        env.pop("TREBUCHET_DATASET_ID", None)
        if dataset_id:
            env["TREBUCHET_DATASET_ID"] = dataset_id
        return subprocess.run([sys.executable, str(T / "lib/dataset_report.py"), str(tmp_path), *args],
                              capture_output=True, text=True, env=env)

    out = tmp_path / "compute/results/campaign.json"
    out.parent.mkdir(parents=True)
    out.write_text(CAMPAIGN.read_text())
    run_id = dataset("--begin", "1", "compute").stdout.strip()
    assert run_id
    assert dataset("compute").returncode == 2
    stale = dataset("compute", dataset_id=run_id)
    assert stale.returncode == 2 and "different dataset run" in stale.stdout
    assert "geometric mean" not in stale.stdout

    archive = json.loads((T / "results/2026-11-21-dataset/compute/results/campaign.json").read_text())
    run_id = dataset("--begin", "1", "compute").stdout.strip()
    with record_campaign(out, "compute", 1, run_id) as campaign:
        campaign["selection"] = archive["selection"]
        campaign["samples"].extend(archive["samples"][:21])
    assert dataset("compute", dataset_id=run_id).returncode == 0
    assert json.loads((tmp_path / "dataset.json").read_text())["state"] == "completed"

    campaign = json.loads(out.read_text())
    campaign["samples"] = campaign["samples"][:7]
    out.write_text(json.dumps(campaign))
    invalid = dataset("compute")
    assert invalid.returncode == 2 and "geometric mean" not in invalid.stdout


def test_dataset_driver_failure_replaces_stale_report(tmp_path):
    """Fail acquisition through the real driver after a previous successful report."""
    import os

    root = tmp_path / "work"
    root.mkdir()
    output = root / "dataset.json"
    output.write_text(json.dumps({"schema": "trebuchet.dataset/1", "categories": {"compute": {}}, "problems": {}}))
    commands = tmp_path / "bin"
    commands.mkdir()
    git = commands / "git"
    git.write_text("#!/bin/sh\nexit 19\n")
    git.chmod(0o755)
    (commands / "scopt").symlink_to(sys.executable)
    env = dict(os.environ, PATH=f"{commands}:{os.environ['PATH']}", TREBUCHET_WORK=str(root),
               TREBUCHET_QEMU=sys.executable, FIRMBOLT_REPO=str(tmp_path), FIRMBOLT_BOLT=sys.executable,
               SCOPT_RAM_ROOT=str(tmp_path / "ram"))
    failed = subprocess.run([str(T / "trebuchet.sh"), "fixture", "dataset", "1"], env=env, capture_output=True, text=True)
    assert failed.returncode == 19, failed.stderr
    doc = json.loads(output.read_text())
    assert doc["state"] == "failed" and doc["run_id"] and doc["categories"] == {}
    assert "geometric mean" not in failed.stdout
    reread = subprocess.run([str(T / "trebuchet.sh"), "fixture", "dataset_report"], env=env, capture_output=True, text=True)
    assert reread.returncode == 2 and "INCOMPLETE DATASET" in reread.stdout
