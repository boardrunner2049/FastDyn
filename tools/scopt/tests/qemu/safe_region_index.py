"""Exercise indexed safe admission and ordinary fallback on a sealed FreeRTOS launch.

python safe_region_index.py path/to/compiled/launch.json fresh/output/dir
Requires GDB, a symbolized QEMU, and an exercised region at directory index 0.
"""

import argparse
import json
import os
import re
import subprocess
from pathlib import Path

from scopt.run.v7_stats import counter_problems


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("launch", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    launch = json.loads(args.launch.read_text())
    if not launch["disable_aslr"] or launch["reset_files"]:
        parser.error("supply a fixed-address compiled launch with prepopulated private RAM")
    # Modify one process's expected hash in memory. The cache on disk and the
    # guest instructions executed by its public fallback are identical.
    commands = [
        "set disable-randomization on", "start", "tbreak sc7_try_region", "continue",
        "set $src = sc7.source_index[sc7.regions[0].first_member]",
        "set variable supercache.entries[$src].record.guest_hash ^= 1",
        "continue", "quit $_exitcode",
    ]
    stats, outputs = {}, {}
    for arm in ("normal", "mismatch"):
        stats_path = out / f"{arm}.json"
        env = {**os.environ, **launch["env_set"], "QEMU_SC_V7_EXPERIMENT": "",
               "QEMU_SC_SEALED": "1", "QEMU_SC_V7_STATS": str(stats_path),
               "FASTDYN_TIMING_FILE": str(out / f"{arm}.timing")}
        argv = list(launch["argv"])
        for sock in launch["qmp_sockets"]:
            argv = [arg.replace(f"unix:{sock},", f"unix:{out / (arm + '.sock')},")
                    for arg in argv]
        prefix = ["setarch", "x86_64", "-R"] if arm == "normal" else [
            "gdb", "-nx", "--batch", *(v for cmd in commands for v in ("-ex", cmd)), "--args"]
        log = out / f"{arm}.log"
        with log.open("w") as stream:
            subprocess.run([*prefix, *argv], cwd=launch["cwd"], env=env, stdout=stream,
                           stderr=subprocess.STDOUT, check=True, timeout=120)
        stats[arm] = json.loads(stats_path.read_text())
        assert stats[arm]["mode"] == "safe" and stats[arm]["experiment"]["source_index"]
        assert counter_problems(stats[arm]) == [], (arm, counter_problems(stats[arm]))
        assert stats[arm]["trusted_residual_exits"] == 0 < stats[arm]["loop_returns_nonnull"]
        outputs[arm] = re.findall(r"^Reg [0-3]:0x[0-9a-fA-F]{8}$", log.read_text(), re.M)
    normal, mismatch = stats["normal"], stats["mismatch"]
    assert normal["artifact_id8"] == mismatch["artifact_id8"]
    assert normal["dependency_refusals"] == 0 < mismatch["dependency_refusals"]
    assert normal["region_activations"] > mismatch["region_activations"]
    assert len(outputs["normal"]) == 16 and outputs["normal"] == outputs["mismatch"]


if __name__ == "__main__":
    main()
