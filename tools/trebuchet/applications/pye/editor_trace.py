#!/usr/bin/env python3
"""Attribute a training-only execution trace to the UI and its surrounding phases."""
import argparse
from collections import Counter
import gzip
import hashlib
import json
from pathlib import Path
import re

from session import BEGIN, END, READY


TRACE = re.compile(rb"Trace 0: 0x[0-9a-f]+ \[[^/]+/([0-9a-f]+)/[^\]]+\]\s*(.*?)\s*$")
TX = re.compile(rb"^cmsdk_apb_uart_tx CMSDK APB UART: character 0x([0-9a-f]+) sent to backend")
PHASES = ("startup", "operator_release", "editor", "return_and_reset")


def summarize(run, trace=None, original=None, candidate=None):
    receipt = json.loads((run / "result.json").read_bytes())
    if (receipt["verdict"] != "PASS" or receipt["application"]["role"] != "training"
            or receipt["trace_flags"] != "exec,nochain,trace:cmsdk_apb_uart_tx"):
        raise ValueError("a successful training-only complete execution trace is required")
    functions = {p: Counter() for p in PHASES}
    pcs = {p: Counter() for p in PHASES}
    markers = (READY, BEGIN, END)
    phase = 0
    uart = bytearray()
    digest = hashlib.sha256()
    path = trace or Path(receipt["exec_trace_path"])
    with (gzip.open(path, "rb") if path.suffix == ".gz" else path.open("rb")) as stream:
        for line in stream:
            digest.update(line)
            if line.startswith(b"Trace "):
                match = TRACE.fullmatch(line)
                if not match:
                    raise ValueError("unrecognized TB trace record")
                pc, name = match.groups()
                pcs[PHASES[phase]][int(pc, 16)] += 1
                functions[PHASES[phase]][name.decode() or "<unknown>"] += 1
            elif line.startswith(b"cmsdk_apb_uart_tx "):
                match = TX.match(line)
                if not match:
                    raise ValueError("unrecognized UART transmit record")
                uart.append(int(match[1], 16))
                if phase < len(markers) and uart.endswith(markers[phase]):
                    phase += 1
    if digest.hexdigest() != receipt["exec_trace_sha256"] or phase != len(markers):
        raise ValueError("trace identity or marker sequence mismatch")
    if bytes(uart) != (run / "uart.bin").read_bytes() or hashlib.sha256(uart).hexdigest() != receipt["application"]["uart_sha256"]:
        raise ValueError("guest UART events and received session transcript differ")
    result = {k: receipt[k] for k in ("elf_sha256", "stage_sha256", "qemu_sha256", "exec_trace_sha256")}
    result["definition"] = "Executed unchained TB entries attributed at sequential guest UART READY, BEGIN and END marker writes"
    result["phases"] = {p: {"tb_entries": sum(pcs[p].values()), "unique_tb_pcs": len(pcs[p]),
                            "function_tb_entries": dict(functions[p].most_common())} for p in PHASES}
    if bool(original) != bool(candidate):
        raise ValueError("supply both original and candidate for relocated execution evidence")
    if candidate:
        from guest_layout import check
        if hashlib.sha256(candidate.read_bytes()).hexdigest() != receipt["elf_sha256"]:
            raise ValueError("candidate differs from the traced image")
        moved = check(original, candidate)["relocated_sections"]
        result["relocated_sections"] = moved
        for phase_name, counts in pcs.items():
            hits = {hex(pc): n for pc, n in sorted(counts.items())
                    if any(s["start"] <= pc < s["end"] for s in moved)}
            result["phases"][phase_name].update(relocated_tb_entries=sum(hits.values()), relocated_pcs=hits)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--trace", type=Path, help="raw or gzip-compressed trace with the receipt's SHA-256")
    parser.add_argument("--original", type=Path)
    parser.add_argument("--candidate", type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize(args.run, args.trace, args.original, args.candidate), indent=2))
