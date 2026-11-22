#!/usr/bin/env python3
"""Qualify one sealed training inventory on a fixed-ASLR host, with the real motor oracle."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import resource
import subprocess
import sys
import time

from motor import oracle, settings

sys.path.insert(0, str(Path(__file__).resolve().parents[5] / "tools/scopt"))
from scopt.run.proof import deployed_state, frozen_append_counts, prove


def digest(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def run(qemu, firmware, out):
    out.mkdir(parents=True, exist_ok=False)
    flash = bytearray(firmware.read_bytes())
    changes = settings(flash)
    image = out / "deployed-flash.bin"
    image.write_bytes(flash)
    seal = out / "training-frozen.sc"
    active = out / "active.sc"
    env = {**os.environ, "ESCAPE1_BENCH": "maxon-283828"}
    cpu = max(os.sched_getaffinity(0))
    os.sched_setaffinity(0, {cpu})
    context = {"platform": platform.platform(), "cpu": cpu, "host_cpuinfo": Path("/proc/cpuinfo").read_text().split("\n\n")[0],
               "qemu_sha256": digest(qemu), "original_bank_sha256": digest(firmware),
               "deployed_bank_sha256": digest(image), "settings": changes,
               "scenario": "known training-only 430-frame RC-car ramp, load, brake and reverse"}
    subprocess.run(["setarch", "x86_64", "-R", "true"], check=True)
    save(out / "identity.json", context)
    states = {}
    for label in ("population", "fresh-replay"):
        d = out / label
        d.mkdir()
        if label == "fresh-replay":
            subprocess.run(["cp", "--sparse=always", seal, active], check=True)
            # cp inherits the sealed 0444 mode. Native QEMU opens each trial's
            # deployment read/write even when it appends zero translations.
            active.chmod(0o600)
            before = deployed_state(active, None, frontend="none")
            save(d / "deployed-state.json", before)
            if before["n"] <= 0 or before["n"] != before["header"]["record_count"] or digest(active) != context["sealed_sha256"]:
                raise ValueError("deployment differs from populated and sealed inventory")
        trace, wire = d / "board.trace", d / "kiss.bin"
        cmd = ["setarch", "x86_64", "-R", str(qemu), "-M", "escape1-g071", "-kernel", str(image),
               "-serial", "null", "-serial", "file:" + str(wire), "-display", "none", "-monitor", "none",
               "-no-reboot", "-icount", "shift=4,align=off,sleep=off", "-accel", "tcg,supercache=" + str(active),
               "-d", "page", "-D", str(trace)]
        before_cpu = resource.getrusage(resource.RUSAGE_CHILDREN)
        start = time.monotonic_ns()
        with (d / "qemu.log").open("wb") as log:
            process = subprocess.run(cmd, env=env, stdout=log, stderr=log, timeout=240)
        wall_ns = time.monotonic_ns() - start
        after_cpu = resource.getrusage(resource.RUSAGE_CHILDREN)
        result = {"cmd": cmd, "exit_code": process.returncode, "wall_ns": wall_ns,
                  "user_cpu_ns": round((after_cpu.ru_utime-before_cpu.ru_utime)*1e9),
                  "system_cpu_ns": round((after_cpu.ru_stime-before_cpu.ru_stime)*1e9)}
        save(d / "execution.json", result)
        if process.returncode:
            print((d / "qemu.log").read_text(errors="replace")[-4000:], file=sys.stderr)
            process.check_returncode()
        observed = oracle(trace.read_text(), wire.read_bytes())
        observed.update(kiss_sha256=digest(wire), trace_sha256=digest(trace))
        save(d / "oracle.json", observed)
        if observed["verdict"] != "PASS":
            raise ValueError("application oracle failed")
        if label == "population":
            active.rename(seal)
            seal.chmod(0o444)
            context["sealed_sha256"] = digest(seal)
            states[label] = deployed_state(seal, None, frontend="none")
            save(out / "identity.json", context)
        else:
            proof = prove(before)
            states[label] = {"verdict": proof["verdict"], "appended": proof["appended"], "proof": proof}
            save(d / "proof.json", states[label])
            frozen_append_counts(states[label])
        save(out / "qualification.json", states)
    active.unlink()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    for a in ("qemu", "flash", "out"):
        p.add_argument(a, type=Path)
    args = p.parse_args()
    run(args.qemu.resolve(), args.flash.resolve(), args.out.resolve())
