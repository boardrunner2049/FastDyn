#!/usr/bin/env python3
"""
Targeted byte-prefix matcher: for each symbol named in a labeled config
file, extract its function bytes from the symbolized reference ELF and
search the stripped target's .text for the longest unique byte-prefix.
When exactly one unique-prefix match exists, we record it.

Usage:
    targeted_prefix_match.py REFERENCE.elf TARGET.elf SYMBOLS.txt OUTPUT.map

SYMBOLS.txt: any labeled_conf/virtuals.txt or labeled_conf/modifiers.txt
             from FastDyn — we extract the first token of each line and
             skip lines whose first token is already a hex address.

OUTPUT.map:  symbol:0xNNNN per line — feed into preprocess.py.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from elftools.elf.elffile import ELFFile
from elftools.elf.sections import SymbolTableSection


PREFIX_LENGTHS = (64, 48, 32, 24, 16, 12, 10, 8, 6, 4)   # longest first
MIN_UNIQUE_PREFIX = 4                              # refuse anything shorter


def sym_addr(elf_path: Path, name: str):
    """Return (address_without_thumb_bit, size) for STT_FUNC symbol name; else (None, None)."""
    with open(elf_path, "rb") as f:
        elf = ELFFile(f)
        for section in elf.iter_sections():
            if not isinstance(section, SymbolTableSection):
                continue
            for sym in section.iter_symbols():
                if sym.name != name:
                    continue
                if sym["st_info"]["type"] != "STT_FUNC":
                    continue
                return int(sym["st_value"]) & ~1, int(sym["st_size"])
    return None, None


def load_text(elf_path: Path):
    with open(elf_path, "rb") as f:
        elf = ELFFile(f)
        for s in elf.iter_sections():
            if s.name == ".text":
                return int(s["sh_addr"]), s.data()
    return None, b""


def find_all(hay: bytes, needle: bytes):
    out = []
    off = 0
    while True:
        p = hay.find(needle, off)
        if p < 0:
            break
        out.append(p)
        off = p + 1
    return out


def match_symbol(ref_bytes: bytes, target_data: bytes, target_base: int,
                 function_starts: set):
    """
    Find a byte-prefix of ref_bytes whose matches in target_data align with a
    known function start. Longest prefix wins. Ties broken by preferring
    matches at r2-identified function starts.
    """
    for n in PREFIX_LENGTHS:
        if len(ref_bytes) < n:
            continue
        needle = ref_bytes[:n]
        positions = [target_base + p for p in find_all(target_data, needle)]
        if not positions:
            continue

        # Prefer positions that are also r2 function starts
        aligned = [p for p in positions if p in function_starts]

        if len(aligned) == 1:
            return (aligned[0], n, "unique-aligned")
        if len(aligned) == 0 and len(positions) == 1:
            return (positions[0], n, "unique-unaligned")
        # Multiple candidates at this prefix length — try longer or shorter?
        # Only accept if len(aligned) drops to 1 at some prefix length. Skip
        # otherwise; the outer loop tries a longer prefix. If nothing works
        # by MIN_UNIQUE_PREFIX we bail.
    return (None, 0, "ambiguous_or_missing")


def iter_labeled_symbols(paths):
    """Yield the first token from each non-blank, non-hex-address line."""
    seen = set()
    for p in paths:
        with open(p) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                tok = line.split()[0]
                if tok.startswith("0x") or tok in seen:
                    continue
                seen.add(tok)
                yield tok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("reference", type=Path)
    ap.add_argument("target", type=Path)
    ap.add_argument("symbols", type=Path, nargs="+",
                    help="labeled_conf/virtuals.txt and/or modifiers.txt")
    ap.add_argument("-o", "--output", type=Path, required=True)
    ap.add_argument("--target-funcs", type=Path,
                    help="text file with r2 afl-style '0xADDR ...' lines")
    args = ap.parse_args()

    if not args.reference.is_file():
        print(f"error: reference ELF missing: {args.reference}", file=sys.stderr)
        return 2
    if not args.target.is_file():
        print(f"error: target ELF missing: {args.target}", file=sys.stderr)
        return 2

    rbase, rdata = load_text(args.reference)
    tbase, tdata = load_text(args.target)
    if rdata is None or tdata is None:
        print("error: could not read .text from one of the ELFs", file=sys.stderr)
        return 2
    print(f"[+] reference .text 0x{rbase:x} ({len(rdata)} bytes)", file=sys.stderr)
    print(f"[+] target    .text 0x{tbase:x} ({len(tdata)} bytes)", file=sys.stderr)

    function_starts = set()
    if args.target_funcs and args.target_funcs.is_file():
        import re
        rx = re.compile(r"^0x[0-9a-fA-F]+")
        with open(args.target_funcs) as f:
            for line in f:
                m = rx.match(line.strip())
                if m:
                    function_starts.add(int(m.group(0), 16))
        print(f"[+] {len(function_starts)} target function starts loaded",
              file=sys.stderr)

    results = {}
    stats = {"unique": 0, "missing_sym": 0, "ambiguous": 0, "too_small": 0}
    for sym in iter_labeled_symbols(args.symbols):
        addr, size = sym_addr(args.reference, sym)
        if addr is None:
            stats["missing_sym"] += 1
            continue
        off = addr - rbase
        if off < 0 or off + PREFIX_LENGTHS[0] > len(rdata):
            stats["missing_sym"] += 1
            continue
        ref_bytes = rdata[off:off + min(size, 128)]
        if len(ref_bytes) < MIN_UNIQUE_PREFIX:
            stats["too_small"] += 1
            continue
        wisp_addr, pfx, reason = match_symbol(ref_bytes, tdata, tbase, function_starts)
        if wisp_addr is not None:
            results[sym] = (wisp_addr, pfx, reason)
            stats["unique"] += 1
        else:
            stats["ambiguous"] += 1

    print(f"[+] {stats['unique']} confident unique-prefix matches", file=sys.stderr)
    print(f"[+] {stats['ambiguous']} ambiguous / no-match", file=sys.stderr)
    print(f"[+] {stats['missing_sym']} symbols not in reference", file=sys.stderr)
    print(f"[+] {stats['too_small']} symbols too small (<{MIN_UNIQUE_PREFIX} bytes) to prefix-match", file=sys.stderr)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w") as f:
        for sym in sorted(results):
            addr, pfx, reason = results[sym]
            f.write(f"{sym}:0x{addr:x}\n")
    print(f"[+] wrote {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
