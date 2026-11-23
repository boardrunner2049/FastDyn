#!/usr/bin/env python3
"""Verify a sealed-firmware trace against its ELF, cache, and QEMU."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from scopt.formats.sealed_firmware import verify


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--elf", required=True, type=Path)
    parser.add_argument("--trace", required=True, type=Path)
    parser.add_argument("--cache", required=True, type=Path)
    parser.add_argument("--qemu", required=True, type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    result = verify(args.elf.resolve(), args.trace.resolve(),
                    args.cache.resolve(), args.qemu.resolve())
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.write_text(text)
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
