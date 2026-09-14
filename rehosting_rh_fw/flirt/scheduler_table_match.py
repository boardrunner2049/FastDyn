#!/usr/bin/env python3
"""
Extract ArduPilot scheduler-task tables from two ELFs and produce a
symbol-name -> target-address map by cross-referencing task name strings.

The ArduPilot scheduler task table entry has this shape (~24 bytes):

    offset 0..3   : first field (often 0 or a pointer)
    offset 4..7   : owner "this" pointer (points to the vehicle object)
    offset 8..11  : function pointer (thumb bit set)
    offset 12..15 : name string pointer  <-- our anchor
    offset 16..19 : max_time_micros (float32)
    offset 20..23 : rate_hz | priority packed

We locate every task-name string in the reference (they look like
"Copter::rc_loop", "Copter::throttle_loop" etc.), walk 4 bytes back to
read the function pointer, and record symbol -> function_addr. Repeating
the process on the target binary lets us pair each task name with its
target address.

Usage:
    scheduler_table_match.py REFERENCE.elf TARGET.elf OUTPUT.map
"""
from __future__ import annotations

import argparse
import re
import struct
import sys
from pathlib import Path

from elftools.elf.elffile import ELFFile


TASK_NAME_RE = re.compile(rb"[A-Za-z_][A-Za-z0-9_]*::[A-Za-z_][A-Za-z0-9_]*")


def load_sections(path: Path):
    sections = {}
    with open(path, "rb") as f:
        elf = ELFFile(f)
        for s in elf.iter_sections():
            if s["sh_type"] == "SHT_PROGBITS" and s.data():
                sections[s.name] = (s["sh_addr"], s.data())
    return sections


def find_all(hay: bytes, needle: bytes):
    out = []
    start = 0
    while True:
        p = hay.find(needle, start)
        if p < 0:
            break
        out.append(p)
        start = p + 1
    return out


def collect_task_name_strings(sections):
    """Yield (string_address, string_bytes) for every X::Y-style name found."""
    out = {}
    for sec_name, (base, data) in sections.items():
        if sec_name not in (".rodata", ".text"):
            continue
        for m in TASK_NAME_RE.finditer(data):
            b = m.group(0)
            # Require null termination to look like a real C string
            end = m.end()
            if end < len(data) and data[end] == 0:
                addr = base + m.start()
                out[addr] = b.decode("ascii", errors="replace")
    return out


def find_scheduler_entries(sections, string_addrs, text_base, text_end):
    """
    For each string_addr, find every 4-byte-aligned position in any
    executable/rodata section that stores that value; the word immediately
    before it is the function pointer. Return {addr_of_name_string: fn_addr}.
    """
    entries = {}
    for name_addr in string_addrs:
        key = struct.pack("<I", name_addr)
        for sec_name, (base, data) in sections.items():
            start = 0
            while True:
                p = data.find(key, start)
                if p < 0:
                    break
                if p % 4 == 0 and p >= 4:
                    fn_ptr = struct.unpack("<I", data[p - 4:p])[0]
                    if (fn_ptr & 1) == 1 and text_base <= (fn_ptr & ~1) < text_end:
                        entries[name_addr] = fn_ptr & ~1
                        break
                start = p + 1
            if name_addr in entries:
                break
    return entries


def text_range(sections):
    base, data = sections[".text"]
    return base, base + len(data)


def build_map(ref_path: Path, tgt_path: Path):
    ref_secs = load_sections(ref_path)
    tgt_secs = load_sections(tgt_path)

    ref_text_base, ref_text_end = text_range(ref_secs)
    tgt_text_base, tgt_text_end = text_range(tgt_secs)

    ref_strings = collect_task_name_strings(ref_secs)
    tgt_strings = collect_task_name_strings(tgt_secs)

    print(f"[ref] {len(ref_strings)} task-name strings", file=sys.stderr)
    print(f"[tgt] {len(tgt_strings)} task-name strings", file=sys.stderr)

    # Index target strings by their C content so we can look up by name
    tgt_by_name = {}
    for addr, name in tgt_strings.items():
        tgt_by_name.setdefault(name, []).append(addr)

    ref_entries = find_scheduler_entries(ref_secs, list(ref_strings.keys()),
                                         ref_text_base, ref_text_end)
    tgt_entries = find_scheduler_entries(tgt_secs, list(tgt_strings.keys()),
                                         tgt_text_base, tgt_text_end)

    print(f"[ref] {len(ref_entries)} scheduler-table entries", file=sys.stderr)
    print(f"[tgt] {len(tgt_entries)} scheduler-table entries", file=sys.stderr)

    # For each ref entry, we know: string_addr -> reference name and reference fn addr.
    # The reference name is the C-string content. To map to a mangled symbol we
    # would need the reference ELF's symbol table; but for the plugin the
    # C-string name is directly usable as an intent hint. We emit
    # <printable_task_name>:<target_fn_addr> lines when the same string
    # exists in the target.
    matches = {}
    for name_addr, fn_addr in ref_entries.items():
        name = ref_strings.get(name_addr)
        if not name:
            continue
        # Find the same string in target
        tgt_name_addrs = tgt_by_name.get(name)
        if not tgt_name_addrs:
            continue
        # Any of them should work; take the first that has an entry
        for tgt_name_addr in tgt_name_addrs:
            if tgt_name_addr in tgt_entries:
                matches[name] = tgt_entries[tgt_name_addr]
                break
    return matches


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("reference", type=Path)
    p.add_argument("target", type=Path)
    p.add_argument("output", type=Path)
    args = p.parse_args()

    matches = build_map(args.reference, args.target)
    print(f"[+] {len(matches)} scheduler-table matches", file=sys.stderr)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w") as f:
        for name, addr in sorted(matches.items(), key=lambda kv: kv[1]):
            f.write(f"{name}:0x{addr:x}\n")
    print(f"[+] wrote {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
