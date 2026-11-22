#!/usr/bin/env python3
"""Run Pye in native MPS2 PSRAM using the checked FastDyn + scopt execution path."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "lib"))
from application_peer import uart_peer
from scopt.run.deploy import deploy
from scopt.run.launcher import create_run_dir, file_sha256, launch, write_json
from scopt.run.prepare import prepare
from scopt.run.profile_run import profile_run
from guest_layout import inspect
from session import stage


def configuration(elf, qemu, plugin, socket_path, cached=True):
    cache = 'supercache = "cache/fw.sc"\nsupercache_flat = false' if cached else ""
    return f'''[Machine]
platform = "mps2-an385"
qemu_path = {json.dumps(str(qemu))}
icount = {{shift = 0, sleep = false, align = false}}
display = "none"
serial = {json.dumps("unix:" + str(socket_path))}
monitor = "none"
semihosting = true
log_options = "none"
coverage = false
{cache}

[Memory.main]
id = "psram"
base_address = "0x21000000"
memory_size = "16M"
memory_type = "SRAM"
backend = "file"
memory_file = "psram.ram"
share = false
prealloc = false

[Device.Models]
[[CPU.cpu0]]
arch = "arm"
machine = "mps2-an385"
cpu = "cortex-m3"
binary = {json.dumps(str(elf))}
plugin_library = {json.dumps(str(plugin))}
[CPU.cpu0.plugins.introspection]
enabled = false
'''


def pinned(path):
    return {"path": str(path), "sha256": file_sha256(path)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("config", "capture", "warm", "frozen", "profile"))
    parser.add_argument("--work", type=Path, required=True, help="upstream Pye and MicroPython checkouts")
    parser.add_argument("--run", type=Path, required=True, help="campaign work directory")
    parser.add_argument("--elf", type=Path, required=True)
    parser.add_argument("--qemu", type=Path, required=True)
    parser.add_argument("--plugin", type=Path, required=True)
    parser.add_argument("--case", action="append", required=True, help="repeat for each capture/input")
    parser.add_argument("--stable-sweep-size", type=int, default=0, help="capture stops after two unchanged full sweeps")
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--v6-stats", action="store_true", help="collect SC6 counters on a separate diagnostic replay")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.v6_stats and args.action != "frozen":
        parser.error("--v6-stats requires a frozen diagnostic replay")
    work, run, elf, qemu, plugin, out = (p.resolve() for p in
                                        (args.work, args.run, args.elf, args.qemu, args.plugin, args.out))
    run.mkdir(parents=True, exist_ok=True)
    cfg = run / "pye.toml"
    sock = Path("/tmp") / ("pye-" + sha256(str(run).encode()).hexdigest()[:24] + ".sock")
    config_text = configuration(elf, qemu, plugin, sock)
    if cfg.exists() and cfg.read_text() != config_text:
        raise ValueError("existing config belongs to another deployment")
    if not cfg.exists():
        cfg.write_text(config_text)
    banks = []
    specs = {}
    scenarios = {}
    for name in args.case:
        case_dir = run / "cases" / name
        case = stage(work, name, case_dir)
        rom = Path(case["romfs"])
        spec = {"program": pinned(HERE / "session.py"), "stage": pinned(case_dir / "stage.json"),
                "bank": "psram", "input_sha256": file_sha256(rom)}
        specs[rom] = spec
        banks.append(rom)
        expected = case_dir / "reference.json"
        write_json(expected, {"completed_work": 1, "observations": {
                             "returned_sha256": case["expected_output_sha256"],
                             "returned_bytes": Path(case["expected"]).stat().st_size}})
        scenarios[name] = {"id": name, "weight": 1, "bank_inputs": {"psram": pinned(rom)},
                           "peer": spec, "expected": pinned(expected)}
    def peer(rd, inputs, run_json, timeout):
        return uart_peer(specs[Path(inputs["psram"]).resolve()])(rd, inputs, run_json, timeout)
    if args.action == "config":
        write_json(out, {"config": pinned(cfg), "firmware": pinned(elf), "runtime": pinned(qemu),
                         "plugin": pinned(plugin), "cases": list(specs.values()), "scenarios": scenarios,
                         "observer": pinned(HERE / "observe.py"), "secondary_measurement": "qemu_lifetime_ns"})
    elif args.action == "capture":
        result = prepare(cfg, out, inputs=banks, input_bank="psram", qemu=qemu, timeout_s=120, peer=peer,
                         stable_sweep_size=args.stable_sweep_size)
        if result["verdict"] != "PASS":
            sys.exit(f"capture failed: {result['failures']}")
    elif args.action in ("warm", "frozen"):
        if len(banks) != 1 or args.snapshot is None:
            parser.error("warm needs exactly one --case and a --snapshot")
        rd = create_run_dir(cfg, out, qemu=qemu, label="Pye native UART ordinary warm")
        identity = deploy(args.snapshot, rd)["identity"]
        if not identity["checked"] or not identity["bundle_id"] or not identity["runtime_identity_id"] or identity["reasons"]:
            sys.exit("deployment identity unchecked")
        extra = {"QEMU_SC_V6_STATS": str(out / "v6-stats.json")} if args.v6_stats else None
        result = launch(rd, inputs={"psram": banks[0]}, prove=True, timeout_s=120, peer=peer, extra_env=extra)
        if args.action == "frozen":
            from scopt.run.proof import frozen_append_counts
            frozen_append_counts(result)
        if (result["verdict"] != "PASS" or result["exit_code"] != 0 or result["timed_out"] or (args.action == "warm" and result["appended"] != 0)
                or result["qemu_sha256"] != file_sha256(qemu)):
            sys.exit(f"replay refused: {result['verdict']}, appended={result['appended']}")
    else:
        if args.snapshot is None:
            parser.error("profile needs --snapshot")
        _, _, vectors = inspect(elf)
        reset_pc = vectors[1] & ~1
        result = profile_run(cfg, args.snapshot, out, qemu=qemu, inputs=banks, input_bank="psram", input_split="train",
                             phases=[f"steady@{reset_pc:#x}"], timeout_s=120, peer=peer)
        if result["verdict"] != "PASS":
            sys.exit(f"profile refused: {out}")


if __name__ == "__main__":
    main()
