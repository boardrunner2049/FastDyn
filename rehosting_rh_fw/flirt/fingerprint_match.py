#!/usr/bin/env python3
"""
Fingerprint-match ArduPilot symbols from a symbolized reference ELF to
addresses in a stripped target ELF, by normalizing the first N bytes of
each function's Thumb instruction stream (masking PC-relative and
branch-target immediates) and hashing what remains.

Usage:
    fingerprint_match.py REFERENCE.elf TARGET.elf OUTPUT.map \
                         [--target-funcs FCN_LIST]

FCN_LIST is optional; when given it's a text file with candidate target
function addresses (one hex 0x... per line, or the first whitespace-
separated column). Use radare2's `afl` output. Without it, the target
is scanned every 2 bytes across executable sections (slower).

Output: one line per confident match, in the form
    <mangled_name>:0x<addr>

Suitable as input to
    virtuals/physics/flight_controllers/courbet/preprocess.py
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB, CS_MODE_MCLASS
from elftools.elf.elffile import ELFFile
from elftools.elf.sections import SymbolTableSection

PREFIX_INSNS = 12               # instructions in the structural fingerprint
MAX_SPAN_BYTES = 128            # bytes read to satisfy PREFIX_INSNS
MIN_FUNC_SIZE = 8
# Mnemonic-only fingerprints ignore register allocation and immediates,
# which vary between independent builds of the same source. Set to False
# to require exact operand shape too (higher precision, lower recall).
MNEMONIC_ONLY = True


# Mnemonics whose immediate operand encodes an absolute or PC-relative
# quantity that will differ between two independently linked binaries.
# For these we keep the opcode+regs but replace the immediate operand
# with a wildcard.
VARIABLE_MNEMONICS = frozenset({
    "b", "bl", "blx", "bx",
    "beq", "bne", "bcs", "bcc", "bmi", "bpl", "bvs", "bvc",
    "bhi", "bls", "bge", "blt", "bgt", "ble", "bal", "bhs", "blo",
    "cbz", "cbnz",
    "adr",
    "movw", "movt",
})

# LDR-style mnemonics whose operand encoding contains a PC-relative
# literal pool offset (op_str contains "[pc").
LDR_LITERAL = frozenset({
    "ldr", "ldrh", "ldrb", "ldrsh", "ldrsb", "ldrd",
    "vldr",
})

# Regex to strip immediate/absolute constants from op_str while keeping
# register names and the shape of the operand list.
_IMM_STRIP = re.compile(r"#-?0x[0-9a-fA-F]+|#-?\d+|0x[0-9a-fA-F]+")


def _mask_operands(insn) -> str:
    """Reduce an instruction's operands to a shape that ignores linker-varying immediates."""
    mnem = insn.mnemonic.lower()
    op = insn.op_str.lower()
    if mnem in VARIABLE_MNEMONICS:
        # BL/B/CBZ/ADR/MOVW/MOVT — the immediate is the whole point;
        # collapse to a bare "*".
        return "*"
    if mnem in LDR_LITERAL and "[pc" in op:
        # ldr r0, [pc, #0x48]  ->  ldr r0, [pc, *]
        return _IMM_STRIP.sub("*", op)
    # For everything else, keep immediates because they are usually
    # struct offsets, register widths, or shift amounts — all invariant
    # across independent builds.
    return op


def normalize_prefix(md: Cs, code: bytes, base_addr: int):
    """Return a tuple of PREFIX_INSNS entries capturing the invariant shape."""
    seq = []
    for insn in md.disasm(code, base_addr):
        if MNEMONIC_ONLY:
            # Just the mnemonic; ignore all operands so register-allocator
            # differences and PC-relative offsets don't defeat the match.
            seq.append(insn.mnemonic.lower())
        else:
            seq.append((insn.mnemonic.lower(), _mask_operands(insn)))
        if len(seq) >= PREFIX_INSNS:
            return tuple(seq)
    return None


def load_exec_sections(elf_path: Path):
    """Return list of (base_vaddr, bytes) for every allocatable executable section."""
    result = []
    with open(elf_path, "rb") as f:
        elf = ELFFile(f)
        for sec in elf.iter_sections():
            flags = sec["sh_flags"]
            if sec["sh_type"] != "SHT_PROGBITS":
                continue
            if not (flags & 0x2 and flags & 0x4):    # SHF_ALLOC | SHF_EXECINSTR
                continue
            data = sec.data()
            if not data:
                continue
            result.append((sec["sh_addr"], data))
    return sorted(result, key=lambda pair: pair[0])


def read_at(sections, addr, length):
    for base, data in sections:
        if base <= addr < base + len(data):
            off = addr - base
            return data[off:off + length]
    return None


def iter_named_functions(elf_path: Path):
    with open(elf_path, "rb") as f:
        elf = ELFFile(f)
        for section in elf.iter_sections():
            if not isinstance(section, SymbolTableSection):
                continue
            for sym in section.iter_symbols():
                if sym["st_info"]["type"] != "STT_FUNC":
                    continue
                name = sym.name
                if not name:
                    continue
                addr = sym["st_value"] & ~1     # strip Thumb bit
                size = sym["st_size"]
                if size < MIN_FUNC_SIZE:
                    continue
                yield name, addr, size


def build_reference_map(ref_elf: Path):
    md = Cs(CS_ARCH_ARM, CS_MODE_THUMB + CS_MODE_MCLASS)
    md.detail = False
    md.skipdata = True
    text = load_exec_sections(ref_elf)
    total = 0
    fp_to_names = defaultdict(list)
    for name, addr, size in iter_named_functions(ref_elf):
        raw = read_at(text, addr, min(size, MAX_SPAN_BYTES))
        if raw is None or len(raw) < 8:
            continue
        fp = normalize_prefix(md, raw, addr)
        if fp is None:
            continue
        fp_to_names[fp].append(name)
        total += 1
    unique = {fp: names[0] for fp, names in fp_to_names.items() if len(names) == 1}
    ambiguous = sum(1 for v in fp_to_names.values() if len(v) > 1)
    print(f"[ref] {total} named functions fingerprinted; "
          f"{len(unique)} unique, {ambiguous} ambiguous", file=sys.stderr)
    return unique


def parse_afl_output(path: Path):
    """Extract function addresses from `r2 afl` output."""
    addrs = []
    hex_re = re.compile(r"^0x[0-9a-fA-F]+")
    with open(path) as f:
        for line in f:
            m = hex_re.match(line.strip())
            if m:
                addrs.append(int(m.group(0), 16))
    return addrs


def scan_target_using_candidates(target_elf: Path, ref_map, candidate_addrs):
    md = Cs(CS_ARCH_ARM, CS_MODE_THUMB + CS_MODE_MCLASS)
    md.detail = False
    md.skipdata = True
    text = load_exec_sections(target_elf)
    hits = {}
    for addr in candidate_addrs:
        raw = read_at(text, addr, MAX_SPAN_BYTES)
        if raw is None or len(raw) < 8:
            continue
        fp = normalize_prefix(md, raw, addr)
        if fp is None:
            continue
        name = ref_map.get(fp)
        if name is None:
            continue
        if name not in hits:
            hits[name] = addr
    return hits


def scan_target_sliding(target_elf: Path, ref_map):
    md = Cs(CS_ARCH_ARM, CS_MODE_THUMB + CS_MODE_MCLASS)
    md.detail = False
    md.skipdata = True
    text = load_exec_sections(target_elf)
    hits = {}
    for base, data in text:
        end = len(data) - MAX_SPAN_BYTES
        for off in range(0, end, 2):
            fp = normalize_prefix(md, data[off:off + MAX_SPAN_BYTES], base + off)
            if fp is None:
                continue
            name = ref_map.get(fp)
            if name is None:
                continue
            if name not in hits:
                hits[name] = base + off
    return hits


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("reference", type=Path)
    p.add_argument("target", type=Path)
    p.add_argument("output", type=Path)
    p.add_argument("--target-funcs", type=Path,
                   help="text file with candidate target function starts (e.g. r2 afl output)")
    args = p.parse_args()

    if not args.reference.is_file():
        print(f"error: reference ELF not found: {args.reference}", file=sys.stderr)
        return 2
    if not args.target.is_file():
        print(f"error: target ELF not found: {args.target}", file=sys.stderr)
        return 2

    print(f"[+] building fingerprints from {args.reference}", file=sys.stderr)
    ref_map = build_reference_map(args.reference)

    if args.target_funcs:
        print(f"[+] using candidate function starts from {args.target_funcs}", file=sys.stderr)
        candidates = parse_afl_output(args.target_funcs)
        print(f"[+] {len(candidates)} candidate function addresses", file=sys.stderr)
        hits = scan_target_using_candidates(args.target, ref_map, candidates)
    else:
        print(f"[+] sliding-window scan of {args.target} (this is slower)", file=sys.stderr)
        hits = scan_target_sliding(args.target, ref_map)

    print(f"[+] {len(hits)} matches", file=sys.stderr)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w") as f:
        for name, addr in sorted(hits.items(), key=lambda kv: kv[1]):
            f.write(f"{name}:0x{addr:x}\n")
    print(f"[+] wrote {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
