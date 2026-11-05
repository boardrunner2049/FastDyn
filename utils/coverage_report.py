#!/usr/bin/env python3
"""
coverage_report.py

Given:
  - An objdump-style disassembly
  - A trace file where each line is:  <block_pc_hex>\t<insn_count>\t<timestamp>
    (logged by fuzz_bbl_add: block start PC, instruction count from QEMU TB,
     and a monotonic millisecond timestamp)

Produces an annotated copy of the disassembly with [HIT] / [---] prefixes.

Usage:
    python3 coverage_report.py firmware.dis trace.txt
    python3 coverage_report.py firmware.dis trace.txt --output firmware.annotated.dis
"""

import re
import argparse
import os
try:
    from .coverage_helpers import expand_blocks, load_disassembly_pcs, load_executed_blocks
except ImportError:  # Direct script execution.
    from coverage_helpers import expand_blocks, load_disassembly_pcs, load_executed_blocks


# ---------------------------------------------------------------------------
# 1.  Disassembly parser
# ---------------------------------------------------------------------------

_INSN_RE = re.compile(r"^(\s*)([0-9a-fA-F]+)(:\s.*)")

def parse_disasm_pcs(dis_path):
    """Return a sorted list of every instruction PC in the disassembly."""
    return load_disassembly_pcs(dis_path)


# ---------------------------------------------------------------------------
# 2.  Trace parser  (format: <block_pc_hex>\t<insn_count>\t<timestamp>)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 3.  Block expansion
# ---------------------------------------------------------------------------

def expand_executed(blocks, disasm_pcs):
    """Expand each executed block into the full set of instruction PCs."""
    return expand_blocks(
        blocks, disasm_pcs,
        missing=lambda pc: print(f"Warning: block PC 0x{pc:x} not found in disassembly, skipping."),
    )


# ---------------------------------------------------------------------------
# 4.  Annotated disassembly writer
# ---------------------------------------------------------------------------

def write_annotated(dis_path, executed_pcs, output_path):
    hit = 0
    total = 0

    with open(dis_path, "r", errors="replace") as fin, \
         open(output_path, "w") as fout:
        for line in fin:
            m = _INSN_RE.match(line)
            if m:
                pc = int(m.group(2), 16) & ~1
                total += 1
                if pc in executed_pcs:
                    prefix = "[HIT] "
                    hit += 1
                else:
                    prefix = "[---] "
                fout.write(prefix + line.lstrip())
            else:
                fout.write("      " + line)

    pct = 100 * hit / total if total else 0
    print(f"[+] {hit}/{total} instructions executed ({pct:.1f}%)")
    print(f"[+] Annotated disassembly written to {output_path}")


# ---------------------------------------------------------------------------
# 5.  Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Annotate a disassembly with basic-block coverage.")
    parser.add_argument("dis",   help="Disassembly file (objdump -d output)")
    parser.add_argument("trace", help="Trace file: one <block_pc_hex>\\t<insn_count>\\t<timestamp> per line")
    parser.add_argument("--output", metavar="FILE",
                        help="Output path (default: <dis>.annotated)")
    args = parser.parse_args()

    if args.output:
        output_path = args.output
    else:
        base, ext = os.path.splitext(args.dis)
        output_path = base + ".annotated" + ext

    print(f"[*] Parsing disassembly ({args.dis}) ...")
    disasm_pcs = parse_disasm_pcs(args.dis)
    print(f"    {len(disasm_pcs)} instruction PCs")

    print(f"[*] Loading trace ({args.trace}) ...")
    blocks = load_executed_blocks(args.trace)
    print(f"    {len(blocks)} executed basic blocks")

    print(f"[*] Expanding blocks to instruction PCs ...")
    executed_pcs = expand_executed(blocks, disasm_pcs)
    print(f"    {len(executed_pcs)} executed instruction PCs")

    write_annotated(args.dis, executed_pcs, output_path)


if __name__ == "__main__":
    main()
