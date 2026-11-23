"""Count original native calls in a sealed diagnostic replay (GDB on Linux x86-64)."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile


def replay(launch, entry, address, target, oracle):
    with tempfile.TemporaryDirectory(prefix="scopt-count-") as tmp:
        script = Path(tmp) / "probe.gdb"
        script.write_text(f"""set pagination off
set confirm off
set debuginfod enabled off
set disable-randomization on
start
set $hits = 0
hbreak *{address:#x}
commands
silent
set $hits = $hits + 1
continue
end
thbreak *{entry:#x}
continue
python
i = gdb.selected_inferior()
a = {address}
b = bytes(i.read_memory(a, 6))
if b[:2] == b'\\xff\\x15':
    slot = a + 6 + int.from_bytes(b[2:], 'little', signed=True)
    actual = int.from_bytes(i.read_memory(slot, 8), 'little')
elif b[0] == 0xe8:
    actual = a + 5 + int.from_bytes(b[1:5], 'little', signed=True)
else:
    raise RuntimeError('unexpected call encoding ' + b.hex())
assert int(gdb.parse_and_eval('$pc')) == {entry} and actual == {target}
end
continue
printf "SITE_COUNT %d EXIT %d\\n", $hits, $_exitcode
""")
        argv = launch["argv"].copy()
        argv[argv.index("-qmp") + 1] = f"unix:{tmp}/qmp,server,nowait"
        env = os.environ | launch["env_set"] | {
            "QEMU_SC_SEALED": "1", "FASTDYN_TIMING_FILE": f"{tmp}/timing.jsonl"}
        run = subprocess.run(["gdb", "-nx", "-q", "-batch", "-x", str(script),
                              "--args", *argv], env=env, cwd=launch["cwd"],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        count = re.search(r"^SITE_COUNT (\d+) EXIT 0$", run.stdout, re.M)
        registers = re.findall(r"^Reg \d+:(0x[\da-fA-F]+)$", run.stdout, re.M)
        if run.returncode or not count or registers != oracle:
            raise RuntimeError(run.stdout[-4000:])
        return int(count[1])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("snapshot", "launch", "receipt", "reference_results"):
        parser.add_argument(name, type=Path)
    parser.add_argument("--repeat", type=int, default=2)
    args = parser.parse_args()
    launch = json.loads(args.launch.read_text())
    oracle = json.loads(args.reference_results.read_text())["registers"]
    header = json.loads((args.snapshot / "snapshot.json").read_text())["header"]
    sites = json.loads(args.receipt.read_text())["helper_inline"]["sites"]
    wanted = {site["unit"] for site in sites}
    entries = {}
    with (args.snapshot / "units.jsonl").open() as stream:
        for line in stream:
            unit = json.loads(line)
            member = unit["members"][0]
            if member["unit"] in wanted:
                offset = int(member["origin"]["tb_offset"], 16)
                entries[member["unit"]] = (int(header["code_address"], 16) + offset
                                           - unit["native"]["struct"]["offset_from_code"])
    for site in sites:
        entry = entries[site["unit"]]
        address = entry + site["call_off"]
        counts = [replay(launch, entry, address, int(site["target"], 16), oracle)
                  for _ in range(args.repeat)]
        print(json.dumps({"unit": site["unit"], "symbol": site["symbol"],
                          "address": hex(address), "whole_run_calls": counts}), flush=True)


if __name__ == "__main__":
    main()
