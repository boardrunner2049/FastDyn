#!/usr/bin/env python3
"""Produce and verify an exhaustive fail-closed firmware SuperCache."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib

from scopt.formats.sealed_firmware import executable_ranges, range_argument, verify


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--qemu", required=True, type=Path)
    parser.add_argument("--input", required=True, action="append", type=Path)
    parser.add_argument("--elf", type=Path)
    parser.add_argument("--trust-user-no-recovery", action="store_true",
                        help="required acknowledgement of the unsafe sealed contract")
    args = parser.parse_args()
    if not args.trust_user_no_recovery:
        parser.error("--trust-user-no-recovery is required")
    config = tomllib.loads(args.config.read_text())
    elf = args.elf or Path(config["CPU"]["cpu0"][0]["binary"])
    ranges = executable_ranges(elf)
    args.out.mkdir(parents=True, exist_ok=False)
    trace = args.out.with_suffix(".sealed.trace")
    env = dict(os.environ, QEMU_SCOPT_SEALED_RANGES=range_argument(ranges),
               QEMU_SCOPT_SEALED_TRACE=str(trace.resolve()))
    command = [sys.executable, "-m", "scopt", "prepare", "-c", str(args.config),
               "--out", str(args.out), "--qemu", str(args.qemu), "--no-oracle"]
    for input_path in args.input:
        command.extend(("--inputs", str(input_path)))
    subprocess.run(command, env=env, check=True)
    manifest = verify(elf.resolve(), trace.resolve(),
                      (args.out / "cache/fw.sc").resolve(), args.qemu.resolve())
    manifest["commands"] = {"prepare": command,
                            "replay_env": {"QEMU_SC_SEALED": "1"}}
    output = args.out / "sealed-firmware.json"
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(output)


if __name__ == "__main__":
    main()
