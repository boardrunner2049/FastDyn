#!/usr/bin/env python3
import subprocess
import sys
import re
import argparse
from collections import defaultdict
try:
    from .coverage_helpers import (disassembly_pcs, expand_blocks,
                                   load_executed_blocks as _load_executed_blocks)
except ImportError:  # Direct script execution.
    from coverage_helpers import (disassembly_pcs, expand_blocks,
                                  load_executed_blocks as _load_executed_blocks)

# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------

def run(cmd):
    return subprocess.check_output(cmd, shell=True, text=True)

def addr2line(elf, pc):
    out = run(f"arm-none-eabi-addr2line -e {elf} -f -C -p {pc}")
    return out.strip()

def remap_path(path):
    """Remap build-machine paths to local paths for STM32CubeF7 sources."""
    marker = "STM32CubeF7"
    idx = path.find(marker)
    if idx == -1:
        return path
    return "/data/Code/rehosting/" + path[idx:]

# ------------------------------------------------------------
# Load executed blocks: <pc>\t<count>
# ------------------------------------------------------------

def load_executed_blocks(path):
    return _load_executed_blocks(path, strict=True)

# ------------------------------------------------------------
# Parse disassembly into ordered list of PCs
# ------------------------------------------------------------

def parse_disasm_pcs(disasm_text):
    return disassembly_pcs(disasm_text.splitlines())

# ------------------------------------------------------------
# Expand executed blocks into full instruction PCs
# ------------------------------------------------------------

# disasm_pcs must be a sorted list of all pcs in the binary
def expand_executed(blocks, disasm_pcs):
    return expand_blocks(blocks, disasm_pcs, strict_bounds=True,
                         missing=lambda _pc: (_ for _ in ()).throw(
                             ValueError("block_pc not in disassembled pcs")))


# ------------------------------------------------------------
# Post-process genhtml output to use softer colors
# ------------------------------------------------------------

# Uncovered lines use a faded green so unexecuted code is visible but not
# distracting. Covered lines keep whatever color genhtml chose (blue).
def patch_coverage_colors(output_dir):
    """Patch gcov.css to replace harsh reds with a soft faded green for uncovered lines."""
    import os

    css_path = os.path.join(output_dir, "gcov.css")
    if not os.path.exists(css_path):
        print("[!] gcov.css not found, skipping color patch")
        return

    with open(css_path, "r") as f:
        css = f.read()

    # #FF6230 is the orange-red used for uncovered source lines, branches, and legend
    # #FF0000 is the bright red used for uncovered function/percentage table entries
    css = css.replace("#FF6230", "#dceede")  # soft faded green for source lines
    css = css.replace("#FF0000", "#c8e6c9")  # slightly deeper faded green for table entries

    with open(css_path, "w") as f:
        f.write(css)

    print("[+] Patched gcov.css: replaced red uncovered-line colors with faded green")


# ------------------------------------------------------------
# Main
# ------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("elf")
    parser.add_argument("disasm")
    parser.add_argument("executed_blocks")
    args = parser.parse_args()

    elf = args.elf
    disasm_text = open(args.disasm).read()
    executed_blocks = load_executed_blocks(args.executed_blocks)

    print("[+] Parsing disassembly...")

    disasm_pcs = parse_disasm_pcs(disasm_text)
    
    all_pcs = sorted(disasm_pcs)
    
    print(f"[+] Found {len(disasm_pcs)} instruction PCs")

    print("[+] Expanding executed blocks...")

    executed_pcs = expand_executed(executed_blocks, disasm_pcs)
    print(f"[+] Executed PCs: {len(executed_pcs)}")

    missing_pcs = sorted(set(all_pcs) - executed_pcs)
    print(f"[+] Missing PCs: {len(missing_pcs)}")

    print("[+] Mapping missing PCs to source lines...")

    all_files = set()
    executed_by_file = defaultdict(set)

    for pc in executed_pcs:
        src = addr2line(elf, hex(pc))
        if " at " not in src or ":" not in src:
            continue
        _, file_and_line = src.split(" at ", 1)
        if ":" not in file_and_line:
            continue
        file, line_str = file_and_line.rsplit(":", 1)
        file = remap_path(file)
        if file == "??" or file.endswith(".o") or file.endswith(".o:?"):
            continue
        all_files.add(file)

        m = re.search(r":(\d+)", file_and_line)
        if not m:
            continue

        line = int(m.group(1))
        executed_by_file[file].add(line)

    for pc in missing_pcs:
        src = addr2line(elf, hex(pc))
        if " at " not in src or ":" not in src:
            continue
        _, file_and_line = src.split(" at ", 1)
        if ":" not in file_and_line:
            continue
        file, line_str = file_and_line.rsplit(":", 1)
        file = remap_path(file)
        if file == "??" or file.endswith(".o") or file.endswith(".o:?"):
            continue
        all_files.add(file)

    # ------------------------------------------------------------
    # Generate LCOV tracefile
    # ------------------------------------------------------------
    print("\n[+] Generating LCOV tracefile: coverage.info")

    with open("coverage.info", "w") as out:
        for file in all_files:
            try:
                with open(file, errors="ignore") as f:
                    total_lines = len(f.readlines())
            except Exception as e:
                print("File Error:", file, "->", repr(e))
                continue

            out.write("TN:\n")
            out.write(f"SF:{file}\n")

            # Mark each line as executed (1) or not (0)
            for line in range(1, total_lines + 1):
                if line in executed_by_file[file]:
                    hits = 1
                else:
                    hits = 0
                out.write(f"DA:{line},{hits}\n")

            out.write("end_of_record\n")

    print("[+] LCOV tracefile written to coverage.info")

    # ------------------------------------------------------------
    # Generate HTML report
    # ------------------------------------------------------------
    print("[+] Generating HTML report in ./coverage_html")

    run("genhtml coverage.info --output-directory coverage_html")

    patch_coverage_colors("coverage_html")

    print("[+] Opening HTML report...")
    run("open coverage_html/index.html")



if __name__ == "__main__":
    main()
