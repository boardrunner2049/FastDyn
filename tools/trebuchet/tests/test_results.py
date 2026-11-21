"""Result checks of the Trebuchet campaign: named-field oracle and invalid-campaign handling.

Run with `python3 -m pytest tools/trebuchet/tests` (standard library only).
"""
import copy, json, re, subprocess, sys
from pathlib import Path

T = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(T / "lib"))
from result_schema import REPORTED, STATS_FIELDS, TIMING_DEPENDENT, expected_values, stats_mismatches  # noqa: E402

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
