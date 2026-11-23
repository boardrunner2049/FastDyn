#!/usr/bin/env python3
"""Print source/private disassembly and size evidence for a v7 firmware region."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from scopt.decode.x86 import decoder
from scopt.formats.layouts import SC_CODE_OFFSET
from scopt.importer.import_cache import open_snapshot


def _dump(label: str, code: bytes, address: int) -> None:
    print(f"\n{label} address=0x{address:x} bytes={len(code)}")
    for insn in decoder().disasm(code, address):
        print(f"{insn.address:016x}: {insn.bytes.hex():<24} "
              f"{insn.mnemonic} {insn.op_str}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--artifact", required=True, type=Path)
    parser.add_argument("--region-id", help="region_id8 prefix (default: first region)")
    args = parser.parse_args()
    snapshot = open_snapshot(args.snapshot.resolve())
    units = {unit.tb_offset: unit for unit in snapshot.units()}
    rows = [json.loads(line) for line in
            (args.artifact / "firmware-regions.jsonl").read_text().splitlines()]
    if not rows:
        parser.error("artifact has no firmware regions")
    matches = rows if args.region_id is None else [
        row for row in rows if row["region_id8"].startswith(args.region_id)]
    if len(matches) != 1:
        parser.error(f"region selector matched {len(matches)} rows")
    region = matches[0]
    print(json.dumps({k: region[k] for k in
                      ("region_id8", "callsite_id8", "n_members", "n_edges",
                       "start_offset", "end_offset")}, sort_keys=True))
    for member in region["members"]:
        source = units[member["source_tb_offset"]]
        _dump(f"PUBLIC guest=0x{member['guest_vaddr']:x}", source.code,
              snapshot.header.code_address + source.tc_offset)
    with (args.artifact / "cache.sc").open("rb") as stream:
        for member in region["members"]:
            stream.seek(SC_CODE_OFFSET + member["tc_offset"])
            code = stream.read(member["tc_size"])
            _dump(f"PRIVATE guest=0x{member['guest_vaddr']:x}", code,
                  snapshot.header.code_address + member["tc_offset"])
    source_bytes = sum(units[member["source_tb_offset"]].tc_size
                       for row in rows for member in row["members"])
    private_bytes = sum(member["tc_size"] for row in rows for member in row["members"])
    print(json.dumps({"regions": len(rows),
                      "members": sum(row["n_members"] for row in rows),
                      "internal_edges": sum(row["n_edges"] for row in rows),
                      "copied_source_bytes": source_bytes,
                      "private_native_bytes": private_bytes,
                      "guard_growth_bytes": private_bytes - source_bytes,
                      "region_footprint_bytes": sum(row["end_offset"] - row["start_offset"]
                                                    for row in rows)}, sort_keys=True))


if __name__ == "__main__":
    main()
