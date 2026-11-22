#!/usr/bin/env python3
"""Check the no-input handoff from BOOT2 to ESCAPE1's hardware acquisition setup."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time


def check(qemu, flash, out):
    out.mkdir(parents=True, exist_ok=False)
    trace = out / "startup.trace"
    command = [str(qemu), "-M", "escape1-g071", "-kernel", str(flash), "-serial", "null",
               "-display", "none", "-monitor", "none", "-icount", "shift=4,align=off,sleep=off",
               "-d", "in_asm,int,guest_errors", "-D", str(trace)]
    with (out / "qemu.log").open("wb") as log, subprocess.Popen(command, stdout=log, stderr=log) as proc:
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and proc.poll() is None:
                if trace.exists() and b"ESCAPE1 ADCAL " in trace.read_bytes():
                    break
                time.sleep(0.05)
        finally:
            if proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=5)
    log = trace.read_text()
    match = re.search(r"^ESCAPE1 ADCAL (.*)$", log, flags=re.MULTILINE)
    fields = dict(kv.split("=") for kv in match[1].split()) if match else {}
    expected = {"sysclk": "64000000", "tim6_psc": "0", "tim6_arr": "1279", "tim6_cen": "0",
                "tim6_uif": "1", "adc_cfgr2": "0xa0000000", "adc_ccr": "0xc00000", "mux": "40,50,51,5"}
    app_vectors = flash.read_bytes()[0x1800:0x1808]
    reset_pc = int.from_bytes(app_vectors[4:], "little") & ~1
    reset_seen = f"0x{reset_pc:08x}:" in log
    passed = (reset_seen and all(fields.get(k) == v for k, v in expected.items())
              and 20000 <= int(fields.get("regulator_ns", -1)) <= 22000
              and "HardFault" not in log)
    receipt = {"verdict": "PASS" if passed else "FAIL", "scope": "startup through ADC calibration request",
               "flash_sha256": hashlib.sha256(flash.read_bytes()).hexdigest(),
               "qemu_sha256": hashlib.sha256(qemu.read_bytes()).hexdigest(), "command": command,
               "reset_handler": hex(reset_pc), "reset_handler_executed": reset_seen,
               "observations": fields, "trace_sha256": hashlib.sha256(trace.read_bytes()).hexdigest()}
    (out / "result.json").write_text(json.dumps(receipt, indent=2) + "\n")
    if not passed:
        raise RuntimeError(f"startup qualification failed: {receipt}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    for arg in ("qemu", "flash", "out"):
        p.add_argument(arg, type=Path)
    args = p.parse_args()
    check(args.qemu.resolve(), args.flash.resolve(), args.out.resolve())
