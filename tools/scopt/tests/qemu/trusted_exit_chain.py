"""Chainable trusted residual exits on a sealed member-descriptor launch.

python trusted_exit_chain.py path/to/compiled/launch.json fresh/output/dir

The launch must select a trusted firmware-region artifact written with per-
member descriptors (region flag SC7_REGION_MEMBER_DESCRIPTORS, for example a
rebuilt packet config 7).  Each arm runs on a private copy of the same cache
bytes:

``chain``      default: residual goto_tb exits are armed and chained.
``unchained``  QEMU_SC_V7_EXPERIMENT=no-trusted-exit-chain (old behaviour).
``skip-irq``   QEMU_SC_SEALED_SKIP_IRQ=1: trusted sources never chain.
``corrupt``    one residual rel32 made nonzero: QEMU must refuse to load.

The first three must print the same guest output.  Writes
OUT/trusted-exit-chain.json with the per-arm counter summaries.
"""

import argparse
import json
import os
from pathlib import Path
import subprocess

from scopt.formats.layouts import SC_CODE_OFFSET
from scopt.formats.sc_v5 import read_sc, sparse_copy
from scopt.formats.sc_v7 import (SC7_REGION_MEMBER_DESCRIPTORS, decode_directory,
                                 read_sc7)
from scopt.link import residual_goto_tb_slots
from scopt.run.helper_pgo import _guest_lines, _launch
from scopt.run.v7_stats import counter_problems, summary

ARMS = (("chain", {}),
        ("unchained", {"QEMU_SC_V7_EXPERIMENT": "no-trusted-exit-chain"}),
        ("skip-irq", {"QEMU_SC_SEALED_SKIP_IRQ": "1"}))


def residual_slots(cache):
    """(member index, absolute field offset in the file) of every residual slot."""
    sc = read_sc(cache, versions=(7,))
    rows = decode_directory(read_sc7(cache))
    by_tb = {sc.records[i].tb_offset: sc.records[i] for i in sc.valid_slots}
    out, regions = [], 0
    with open(cache, "rb") as stream:
        for region in rows["regions"]:
            if not region["flags"] & SC7_REGION_MEMBER_DESCRIPTORS:
                continue
            regions += 1
            lo = region["first_member"]
            for gi in range(lo, lo + region["n_members"]):
                member = rows["members"][gi]
                record = by_tb[member["source_tb_offset"]]
                stream.seek(SC_CODE_OFFSET + member["tc_offset"])
                code = stream.read(member["tc_size"])
                internal = {e["field_offset"] for e in rows["edges"]
                            if e["source_member"] == gi}
                count, problems = residual_goto_tb_slots(
                    record, code, member["tc_offset"], internal_fields=internal,
                    code_address=sc.header.code_address)
                assert not problems, (gi, problems)
                for n in (0, 1):
                    insn = record.jmp_insn_offset[n]
                    if record.jmp_reset_offset[n] != 0xFFFF and insn not in internal:
                        out.append((gi, SC_CODE_OFFSET + member["tc_offset"] + insn))
    return regions, rows, out


def run(launch, rd, source, env_extra):
    rd.mkdir()
    cache = rd / "cache.sc"
    sparse_copy(source, cache)
    argv, env, _ = _launch(launch, rd, cache)
    env.update(env_extra)
    env["QEMU_SC_V7_STATS"] = str(rd / "stats.json")
    with (rd / "qemu.log").open("w") as stream:
        result = subprocess.run(["setarch", os.uname().machine, "-R", *argv],
                                cwd=launch["cwd"], env=env, stdout=stream,
                                stderr=subprocess.STDOUT, timeout=120)
    return cache, result, (rd / "qemu.log").read_text()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("launch", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    launch = json.loads(args.launch.read_text())
    accel = launch["argv"][launch["argv"].index("-accel") + 1]
    source = next(part.split("=", 1)[1] for part in accel.split(",")
                  if part.startswith("supercache="))
    source = Path(launch["cwd"]) / source
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    regions, rows, slots = residual_slots(source)
    assert regions and slots, "launch has no member-descriptor region with residual slots"

    expected, stats = None, {}
    for arm, env_extra in ARMS:
        _, result, log = run(launch, out / arm, source, env_extra)
        assert result.returncode == 0, (arm, log[-2000:])
        observed = _guest_lines(launch, log)
        expected = observed if expected is None else expected
        assert observed == expected, arm
        s = stats[arm] = json.loads((out / arm / "stats.json").read_text())
        assert s["loaded"] is True and s["mode"] == "trusted", arm
        assert counter_problems(s) == [], (arm, counter_problems(s))

    chain, unchained, skip = stats["chain"], stats["unchained"], stats["skip-irq"]
    assert chain["trusted_residual_slots_armed"] > 0
    assert chain["chain_installs_trusted_source"] > 0
    assert chain["trusted_residual_exits"] < unchained["trusted_residual_exits"]
    # Shadows: one per non-entry member of every activated region.
    activated = {r["region"] for r in unchained["trusted_residual_exit_regions"]}
    assert chain["trusted_member_shadow_descriptors_instantiated"] >= sum(
        rows["regions"][ri]["n_members"] - 1 for ri in activated
        if rows["regions"][ri]["flags"] & SC7_REGION_MEMBER_DESCRIPTORS)
    for s in (unchained, skip):
        assert s["trusted_residual_slots_armed"] == 0
        assert s["chain_installs_trusted_source"] == 0
        assert s["trusted_residual_exits_chainable"] == 0

    # A corrupted residual rel32 must be refused before anything runs.
    rd = out / "corrupt"
    rd.mkdir()
    cache = rd / "cache.sc"
    sparse_copy(source, cache)
    member, field = slots[0]
    with cache.open("r+b") as stream:
        stream.seek(field)
        stream.write(b"\x01\0\0\0")
    argv, env, _ = _launch(launch, rd, cache)
    result = subprocess.run(["setarch", os.uname().machine, "-R", *argv],
                            cwd=launch["cwd"], env=env, capture_output=True,
                            text=True, timeout=120)
    assert result.returncode != 0
    assert f"trusted member {member} residual slot" in result.stdout + result.stderr

    (out / "trusted-exit-chain.json").write_text(json.dumps(
        {arm: summary(s) for arm, s in stats.items()}, indent=2) + "\n")


if __name__ == "__main__":
    main()
