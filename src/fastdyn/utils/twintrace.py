#!/usr/bin/env python3
import argparse
import re
import struct
from pathlib import Path

from .. import fastdyn_log as fastdyn_log_conf

fastdyn_log = fastdyn_log_conf.getFastdynLogger()

# Matches all:
#   address = 0x..., size = 4 bytes, value = 0x..., pc=0x...
#   address=0x...,  size=4 bytes,   value=0x..., pc=0x...
#   Interrupt Taken:  Vector = 0x...
#   Interrupt Served: Vector = 0x...

LINE_RE = re.compile(
    r"""
    icount=(?P<icount>\d+)
    .*?\[(?P<model>[^\]]+)\]
    \s*(?P<rw>Read|Write)\s*:
    .*?address\s*=\s*(?P<addr>0x[0-9a-fA-F]+)
    .*?size\s*=\s*(?P<size>\d+)
    \s*bytes
    .*?value\s*=\s*(?P<value>0x[0-9a-fA-F]+)
    .*?pc\s*=\s*(?P<pc>0x[0-9a-fA-F]+)
    """,
    re.VERBOSE,
)

IRQ_TAKEN_RE = re.compile(
    r"""
    (?:icount=(?P<icount>\d+)\s+)?            # optional icount
    (?:\[(?P<model>[^\]]+)\]\s+)?             # optional model
    Interrupt\s+Taken:\s*Vector\s*=\s*(?P<vector>0x[0-9a-fA-F]+)
    """,
    re.VERBOSE,
)

IRQ_SERVED_RE = re.compile(
    r"""
    (?:icount=(?P<icount>\d+)\s+)?            # optional icount
    (?:\[(?P<model>[^\]]+)\]\s+)?             # optional model
    Interrupt\s+Served:\s*Vector\s*=\s*(?P<vector>0x[0-9a-fA-F]+)
    """,
    re.VERBOSE,
)

HDR_STRUCT = struct.Struct("<4sIIQ")     # magic, version, record_size, count
REC_STRUCT = struct.Struct("<QQQQII")    # icount, pc, addr, value, size, type

MAGIC = b"TTTR"
VERSION = 1
RECORD_SIZE = REC_STRUCT.size  # 40
IGNORED_IRQS = {0x0F}          # SysTick is noisy and non-deterministic


def parse_line(line: str):
    m = LINE_RE.search(line)
    if not m:
        m = IRQ_TAKEN_RE.search(line)
        if m:
            icount = int(m.group("icount")) if m.group("icount") else 0
            vector = int(m.group("vector"), 16)
            if vector in IGNORED_IRQS:
                return None, None
            return (icount, 0, vector, 0, 0, 2), m.group("model")

        m = IRQ_SERVED_RE.search(line)
        if m:
            icount = int(m.group("icount")) if m.group("icount") else 0
            vector = int(m.group("vector"), 16)
            if vector in IGNORED_IRQS:
                return None, None
            return (icount, 0, vector, 0, 0, 3), m.group("model")

        return None, None

    icount = int(m.group("icount"))
    rw = m.group("rw")
    addr = int(m.group("addr"), 16)
    size = int(m.group("size"))
    value = int(m.group("value"), 16)
    pc = int(m.group("pc"), 16)

    typ = 0 if rw.lower() == "read" else 1
    return (icount, pc, addr, value, size, typ), m.group("model")


def convert(in_path, out_path, *, only_model=None,
            target_ranges=None, target_irqs=None):
    """
    Convert an io.log to the binary .ttbin format the C-side replay consumes.

    Filters (any combination, AND-composed):
      only_model:    keep only events tagged with [<model>] at record time
                     (e.g. "twintrace", "passthrough"). Useful from the CLI
                     when the recording was done with mixed tags.
      target_ranges: list of (start, end) inclusive int tuples. If provided,
                     keep MMIO records (READ/WRITE) only when addr is within
                     at least one range. IRQ records bypass this filter.
      target_irqs:   set/list of IRQ vector ints. If provided, keep IRQ
                     records (TAKEN/SERVED) only when their vector is in
                     the list. MMIO records bypass this filter.

    target_ranges and target_irqs together implement the paper's
    "Replay (target model only)" column: only the target peripheral's
    events are replayed, the rest go to live HW via passthrough.
    """
    in_path = Path(in_path)
    out_path = Path(out_path)

    count = 0
    matched = 0
    skipped = 0

    target_irqs_set = set(target_irqs) if target_irqs is not None else None

    with in_path.open("r", errors="replace") as fin, out_path.open("wb+") as fout:
        # Write placeholder header (count=0 for now)
        fout.write(HDR_STRUCT.pack(MAGIC, VERSION, RECORD_SIZE, 0))

        for line in fin:
            rec, model = parse_line(line)
            if rec is None:
                skipped += 1
                continue

            matched += 1

            if only_model is not None:
                # Quick model filter using bracket content, e.g. [passthrough]
                # Only filter if a model tag is present.
                if model is not None and model != only_model:
                    continue

            # rec layout: (icount, pc, addr, value, size, type)
            # type: 0=READ, 1=WRITE, 2=IRQ_TAKEN, 3=IRQ_SERVED
            # For IRQ records, the `addr` field carries the vector.
            rec_type = rec[5]
            rec_addr = rec[2]

            if rec_type in (0, 1):  # MMIO
                if target_ranges is not None and not any(
                        start <= rec_addr <= end
                        for start, end in target_ranges):
                    continue
            elif rec_type in (2, 3):  # IRQ
                if target_irqs_set is not None and rec_addr not in target_irqs_set:
                    continue

            fout.write(REC_STRUCT.pack(*rec))
            count += 1

        # Patch header with final count
        fout.seek(0)
        fout.write(HDR_STRUCT.pack(MAGIC, VERSION, RECORD_SIZE, count))

    if only_model:
        fastdyn_log.info(f"[+] Filter:  model == [{only_model}]")
    if target_ranges is not None:
        ranges_str = ", ".join(f"0x{s:x}-0x{e:x}" for s, e in target_ranges)
        fastdyn_log.info(f"[+] Filter:  addr in [{ranges_str}]")
    if target_irqs is not None:
        irqs_str = ", ".join(str(i) for i in sorted(target_irqs_set))
        fastdyn_log.info(f"[+] Filter:  irq in [{irqs_str}]")
    fastdyn_log.info(f"[+] Wrote:   {count} records (from {matched} parsed lines)")
    # print(f"[+] Wrote:   {count} records")
    # print(f"[+] Skipped: {skipped} non-matching lines")
    # print(f"[+] Record size: {RECORD_SIZE} bytes")
    # print(f"[+] File size (approx): {HDR_STRUCT.size + count * RECORD_SIZE} bytes")

def replay_binary_verifier(replay_binary):
    HDR = struct.Struct("<4sIIQ")
    REC = struct.Struct("<QQQQII")

    with open(replay_binary,"rb") as f:
        magic, ver, rsz, count = HDR.unpack(f.read(HDR.size))
        fastdyn_log.info(f"[+] Replay tape: magic={magic!r} version={ver} record_size={rsz} records={count}")
        for i in range(5):
            icount, pc, addr, value, size, typ = REC.unpack(f.read(REC.size))
            if typ == 0:
                tname = "R"
            elif typ == 1:
                tname = "W"
            elif typ == 2:
                tname = "IRQ_TAKEN"
            elif typ == 3:
                tname = "IRQ_SERVED"
            else:
                tname = f"UNK({typ})"
            fastdyn_log.info(f"    #{i}: icount={icount} pc={pc:#x} addr={addr:#x} value={value:#x} size={size} {tname}")


def main():
    ap = argparse.ArgumentParser(description="Convert MMIO text trace to packed binary tape")
    ap.add_argument("in_log", type=Path, help="Input MMIO log (text)")
    ap.add_argument("out_bin", type=Path, help="Output binary tape")
    ap.add_argument("--only-model", default=None,
                    help="Only keep records from a specific model tag (e.g. passthrough, elder)")
    args = ap.parse_args()

    convert(args.in_log, args.out_bin, only_model=args.only_model)


if __name__ == "__main__":
    main()
