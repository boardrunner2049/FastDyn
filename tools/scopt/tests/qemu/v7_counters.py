"""Check the QEMU_SC_V7_STATS dispatcher-return counters on a sealed trusted launch.

python v7_counters.py path/to/compiled/launch.json fresh/output/dir

The launch must select a compiled firmware-region artifact whose trusted
regions are exercised (for example packet config 7).  Each arm runs on a
private copy of the cache: the default, QEMU_SC_V7_EXPERIMENT=no-regions,
disable-chaining and no-trusted-exit-chain.  All arms must print the same guest
output and satisfy scopt.run.v7_stats.counter_problems.  Writes
OUT/v7-counters.json with the per-arm counter summaries.
"""

import argparse
import json
import os
from pathlib import Path
import subprocess

from scopt.formats.sc_v5 import sparse_copy
from scopt.run.helper_pgo import _guest_lines, _launch
from scopt.run.v7_stats import (COUNTERS_REVISION, SCHEMA, counter_problems,
                                summary)

ARMS = (("default", ""), ("no-regions", "no-regions"),
        ("disable-chaining", "disable-chaining"),
        ("no-trusted-exit-chain", "no-trusted-exit-chain"))


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
    expected, stats = None, {}
    for arm, experiment in ARMS:
        rd = out / arm
        rd.mkdir()
        cache = rd / "cache.sc"
        sparse_copy(source, cache)
        argv, env, _ = _launch(launch, rd, cache)
        env["QEMU_SC_V7_EXPERIMENT"] = experiment
        env["QEMU_SC_V7_STATS"] = str(rd / "stats.json")
        with (rd / "qemu.log").open("w") as stream:
            result = subprocess.run(["setarch", os.uname().machine, "-R", *argv],
                                    cwd=launch["cwd"], env=env, stdout=stream,
                                    stderr=subprocess.STDOUT, timeout=120)
        assert result.returncode == 0, arm
        observed = _guest_lines(launch, (rd / "qemu.log").read_text())
        expected = observed if expected is None else expected
        assert observed == expected, arm
        s = stats[arm] = json.loads((rd / "stats.json").read_text())
        assert s["schema"] == SCHEMA and s["counters_revision"] == COUNTERS_REVISION, arm
        assert s["loaded"] is True and s["mode"] == "trusted", arm
        assert counter_problems(s) == [], (arm, counter_problems(s))

    default = stats["default"]
    assert 0 < default["trusted_residual_exits"] <= default["loop_returns_nonnull"]
    assert sum(default["trusted_residual_exits_slot"]) == default["trusted_residual_exits"]
    assert default["trusted_tb_calls"] == (default["loop_returns_nonnull"] +
                                           default["trusted_tb_chain_probes"])
    # Revision 3 probes both ends of every chain attempt.
    assert default["trusted_tb_chain_probes"] == 2 * (
        default["chain_attempts"] - default["chain_skipped_global"])
    # Only armed residual slots of member descriptors are chained.
    assert default["chain_installs_trusted_source"] <= \
        default["chain_allowed_trusted_source"] <= \
        default["trusted_residual_exits_chainable"]
    if default["trusted_residual_slots_armed"] == 0:
        assert default["chain_installs_trusted_source"] == 0

    unarmed = stats["no-trusted-exit-chain"]
    assert unarmed["experiment"]["no_trusted_exit_chain"] is True
    assert unarmed["trusted_residual_slots_armed"] == 0
    assert unarmed["chain_installs_trusted_source"] == 0
    assert unarmed["trusted_residual_exits_chainable"] == 0
    if default["chain_installs_trusted_source"]:
        assert unarmed["trusted_residual_exits"] > default["trusted_residual_exits"]

    no_regions = stats["no-regions"]
    assert no_regions["region_entries"] == 0 == no_regions["trusted_residual_exits"]
    assert no_regions["trusted_residual_exit_regions"] == []
    assert no_regions["loop_returns_nonnull"] > 0

    unchained = stats["disable-chaining"]
    assert unchained["trusted_tb_chain_probes"] == 0 == unchained["chain_installs"]
    assert unchained["trusted_tb_calls"] == unchained["loop_returns_nonnull"]

    (out / "v7-counters.json").write_text(json.dumps(
        {arm: {**summary(s), "regions": s["trusted_residual_exit_regions"]}
         for arm, s in stats.items()}, indent=2) + "\n")


if __name__ == "__main__":
    main()
