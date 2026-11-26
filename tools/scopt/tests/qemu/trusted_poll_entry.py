"""Force the entry poll of a trusted->trusted chain and check the exit path.

python trusted_poll_entry.py path/to/compiled/launch.json fresh/output/dir [--timeout S]

Needs QEMU counters revision 4 (trusted->trusted chains enter a region at its
+0 poll), a symbolized QEMU and GDB.  The launch must select a sealed trusted
artifact whose regions exit into other region entries (for example uart
``sealed-pgo-7`` with firmware-inline ``cost_model="off"`` and
``nested="public"``, or cli ``sealed-pgo-7``).  Both runs use private copies
of the same cache bytes with ``QEMU_SC_V7_STATS``:

``reference``  the launch under ``setarch -R``: the guest oracle.
``gdb``        the same argv under ``setarch -R gdb -nx -batch -x
               trusted_poll_entry_gdb.py``.  The first time a trusted->trusted
               chain is traversed, the destination's poll is forced to exit
               (``icount_decr.u16.high = -1``) and the gdb half checks the
               chain target (entry member start, poll bytes), the forced
               ``$pc``, ``synchronize_from_tb`` restoring ``regs[15] == T->pc``
               and the dispatcher re-entering T at ``tc.ptr`` with the same PC.

Asserts: both runs exit 0 with the same guest words and one BENCH line, no
``counter_problems``, ``trusted_poll_entry_exits >= 1`` in the gdb run, and
all four gdb events in order without errors.  Writes
OUT/trusted-poll-entry.json.
"""

import argparse
import json
import os
from pathlib import Path
import subprocess

from scopt.formats.sc_records import sparse_copy
from scopt.run.helper_pgo import _guest_lines, _launch
from scopt.run.v7_stats import T2T_REVISION, counter_problems, summary

HERE = Path(__file__).resolve().parent
GDB_SCRIPT = HERE / "trusted_poll_entry_gdb.py"
EVENTS = ("chain_target", "poll_entry", "synchronize", "reentry")


def source_cache(launch):
    accel = launch["argv"][launch["argv"].index("-accel") + 1]
    source = next(part.split("=", 1)[1] for part in accel.split(",")
                  if part.startswith("supercache="))
    return Path(launch["cwd"]) / source


def run(launch, rd, source, *, gdb, timeout):
    rd.mkdir()
    cache = rd / "cache.sc"
    sparse_copy(source, cache)
    argv, env, _ = _launch(launch, rd, cache)
    env["QEMU_SC_V7_STATS"] = str(rd / "stats.json")
    prefix = ["setarch", os.uname().machine, "-R"]
    if gdb:
        env["TRUSTED_POLL_ENTRY_GDB_PARAMS"] = json.dumps({"result": str(rd / "gdb.json")})
        prefix += ["gdb", "-nx", "-batch", "-x", str(GDB_SCRIPT), "--args"]
    with (rd / "qemu.log").open("w") as stream:
        result = subprocess.run([*prefix, *argv], cwd=launch["cwd"], env=env, stdout=stream,
                                stderr=subprocess.STDOUT, timeout=timeout)
    log = (rd / "qemu.log").read_text(errors="replace")
    return result.returncode, log


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("launch", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--timeout", type=float, default=900.0)
    args = parser.parse_args()
    launch = json.loads(args.launch.read_text())
    source = source_cache(launch)
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)

    rc, log = run(launch, out / "reference", source, gdb=False, timeout=args.timeout)
    assert rc == 0, log[-2000:]
    expected = _guest_lines(launch, log)
    reference = json.loads((out / "reference" / "stats.json").read_text())
    assert reference["counters_revision"] >= T2T_REVISION, \
        f"QEMU counters revision {reference['counters_revision']} has no trusted->trusted chains"
    assert reference["loaded"] is True and reference["mode"] == "trusted"
    assert counter_problems(reference) == [], counter_problems(reference)
    assert reference["chain_installs_trusted_to_trusted"] > 0, \
        "the reference run installed no trusted->trusted chain"

    rc, log = run(launch, out / "gdb", source, gdb=True, timeout=args.timeout)
    gdb_result = json.loads((out / "gdb" / "gdb.json").read_text())
    assert rc == 0 and gdb_result["exit_code"] == 0, (rc, gdb_result, log[-2000:])
    assert _guest_lines(launch, log) == expected, "guest output differs under the forced exit"
    stats = json.loads((out / "gdb" / "stats.json").read_text())
    assert counter_problems(stats) == [], counter_problems(stats)
    assert stats["trusted_poll_entry_exits"] >= 1, stats["trusted_poll_entry_exits"]
    assert not gdb_result["errors"], gdb_result["errors"]
    assert tuple(gdb_result["order"]) == EVENTS, gdb_result["order"]
    assert all(gdb_result["events"][name]["ok"] for name in EVENTS), gdb_result["events"]

    (out / "trusted-poll-entry.json").write_text(json.dumps(
        {"reference": summary(reference), "gdb": summary(stats),
         "trusted_poll_entry_exits": {"reference": reference["trusted_poll_entry_exits"],
                                      "gdb": stats["trusted_poll_entry_exits"]},
         "events": gdb_result["events"], "candidates": gdb_result["candidates"]},
        indent=2) + "\n")


if __name__ == "__main__":
    main()
