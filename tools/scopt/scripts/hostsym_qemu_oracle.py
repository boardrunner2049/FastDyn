"""Run the complete V18 suite under an independent x86-64 QEMU interpreter."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import mmap
import os
from pathlib import Path
import shutil
import subprocess
import sys


class _WriteOnceNative:
    """V18 runner with fresh immutable code addresses for QEMU user mode."""

    def __init__(self):
        self.compiled = {}
        self.ftype = ctypes.CFUNCTYPE(None, ctypes.c_void_p)
        self.io = (ctypes.c_uint64 * 3)()

    def run(self, code, rax, rcx, flags):
        from scopt.validate_sym.selftest import _HEAD, _TAIL

        if code not in self.compiled:
            page = mmap.mmap(-1, mmap.PAGESIZE,
                             prot=mmap.PROT_READ | mmap.PROT_WRITE | mmap.PROT_EXEC)
            page.write(_HEAD + code + _TAIL)
            fn = self.ftype(ctypes.addressof(ctypes.c_char.from_buffer(page)))
            self.compiled[code] = page, fn
        _, fn = self.compiled[code]
        self.io[0], self.io[1], self.io[2] = rax, rcx, flags
        fn(ctypes.addressof(self.io))
        return self.io[0], self.io[1], self.io[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--qemu-x86", default="qemu-x86_64")
    parser.add_argument("--engine", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.engine is None:
        exe = shutil.which(args.qemu_x86)
        if exe is None:
            parser.error(f"{args.qemu_x86} unavailable")
        binary = Path(exe).resolve()
        version = subprocess.check_output([str(binary), "--version"], text=True).splitlines()[0]
        prefix = [str(binary), "-cpu", "max", sys.executable, str(Path(__file__).resolve())]
        engine = {"kind": "qemu-user-tcg-x86-64", "command": prefix, "version": version,
                  "sha256": hashlib.sha256(binary.read_bytes()).hexdigest()}
        os.execv(binary, [*prefix, "--out", str(args.out.resolve()), "--engine", json.dumps(engine)])
    from scopt.validate_sym import selftest

    selftest._Native = _WriteOnceNative
    result = selftest.run_selftest()
    result["x_oracle"] = json.loads(args.engine)
    args.out.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n")
    print(result["status"], result["native"]["trials"])
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
