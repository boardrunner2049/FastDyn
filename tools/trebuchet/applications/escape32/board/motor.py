#!/usr/bin/env python3
"""Run the normal ESCAPE1 RC-car configuration against the 12 V Hall-sensored bench."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import subprocess


def sha(data):
    return hashlib.sha256(data).hexdigest()


def settings(array):
    # Cfg from release 17.3 (src/common.h). Only its writable configuration
    # page changes. The bootloader, product code, vectors and constants stay exact.
    if array[0x1000:0x100b] != bytes.fromhex("ea321103") + b"ESCAPE1":
        raise ValueError("expected ESCAPE1 revision 17.3 configuration")
    changes = {}
    for name, offset, before, after in (("damp", 21, 1, 0), ("throt_mode", 36, 0, 2),
                                        ("telem_mode", 55, 0, 1)):
        if array[0x1000 + offset] != before:
            raise ValueError(f"unexpected initial {name}")
        array[0x1000 + offset] = after
        changes[name] = {"offset_in_configuration": offset, "from": before, "to": after}
    return changes


def crc8(data):
    remainder = 0
    for byte in data:
        remainder ^= byte
        for _ in range(8):
            remainder = ((remainder << 1) ^ (7 if remainder & 128 else 0)) & 255
    return remainder


def decode_phase(row):
    timing = [int(row["m1"], 16), int(row["m2"], 16)]
    enables = int(row["er"], 16)
    high, low = [], []
    for i in range(3):
        mode = (timing[i // 2] >> (4 + 8 * (i % 2))) & 7
        gate = (enables >> (4 * i)) & 7
        if mode == 6 and gate & 1:
            high.append(i + 1)
        if mode == 5 and gate & 4:
            low.append(i + 1)
    if len(high) == len(low) == 1 and int(row["bdtr"], 16) & 0x8000:
        return 10 * high[0] + low[0]
    return 0


def expected_pulse(f):
    if f < 200 or 320 <= f < 340 or f >= 395:
        return 1500
    if f < 210:
        return 1550 + 9 * (f - 200)
    return 1640 if f < 300 else 1390


def oracle(trace, wire):
    rows = []
    phases = []
    for line in trace.splitlines():
        if line.startswith("ESCAPE1 BENCH ") or line.startswith("ESCAPE1 PHASE "):
            fields = dict(word.split("=") for word in line.split()[2:])
            (rows if line.startswith("ESCAPE1 BENCH") else phases).append(fields)
    frames = {int(r["frame"]): r for r in rows}
    checks = {}
    checks["all_430_command_edges_and_widths"] = list(frames) == list(range(1, 431)) and all(
        int(r["pulse"]) == expected_pulse(i) for i, r in frames.items())
    if not checks["all_430_command_edges_and_widths"]:
        raise ValueError("incomplete operator sequence")
    checks["regulator_and_conversions"] = "ESCAPE1 ADCAL sysclk=64000000" in trace and (
        "ESCAPE1 ADC sequence=0xfdc654 dma_remaining=0" in trace) and int(frames[430]["adc"]) >= 7900
    checks["no_watchdog_reset"] = "ESCAPE1 WWDG reset" not in trace and "ESCAPE1 IWDG expired" not in trace
    checks["neutral_before_ramp"] = all(abs(float(frames[f]["rpm"])) < 1 and int(frames[f]["pair"]) == 0
                                         for f in range(175, 200))
    # This phase order is independently decoded from TIM1 output configuration.
    # Positive/negative terminal pairs, 120-degree Hall geometry and one pole pair.
    forward = [12, 13, 23, 21, 31, 32]
    phase_counts = {}
    for name, start, stop, step in (("forward", 220, 245, 1), ("loaded", 263, 292, 1),
                                     ("reverse", 355, 386, -1)):
        turns = [decode_phase(row) for row in phases if start <= int(row["frame"]) < stop]
        phase_counts[name] = len(turns)
        checks[name + "_six_step_order"] = set(turns) == set(forward) and len(turns) >= 50 and all(
            forward[(forward.index(a) + step) % 6] == b for a, b in zip(turns, turns[1:]))
    # Static, averaged catalog motor equation for a different calculation from
    # the emulator's incremental electrical/mechanical integration.
    kt = 0.0078
    ke = 60 / (2 * math.pi * 1220)
    drag = kt * 0.0602 / (13500 * 2 * math.pi / 60)
    metrics = {}
    packets = [wire[i:i + 10] for i in range(0, len(wire), 10)]
    checks["all_kiss_crc"] = len(packets) >= 240 and all(len(p) == 10 and crc8(p) == 0 for p in packets)
    checks["complete_uart_dma"] = len(packets) == int(frames[430]["packets"])
    checks["supply_and_temperature"] = all(p[0] == 30 and 1198 <= int.from_bytes(p[1:3], "big") <= 1202
                                            for p in packets[5:])
    for name, start, stop, load, sign in (("forward", 225, 245, 0, 1),
                                           ("loaded", 270, 290, .000004, 1),
                                           ("reverse", 362, 385, 0, -1)):
        selected = [frames[f] for f in range(start, stop)]
        duty = statistics.median(float(r["duty"]) for r in selected)
        rpm = statistics.median(float(r["rpm"]) for r in selected)
        expected = (sign * kt * 12 * duty / (kt * ke + 15.7 * (drag + load))) * 60 / (2 * math.pi)
        tel = [100 * int.from_bytes(packets[int(r["packets"])-1][7:9], "big") for r in selected]
        metrics[name] = {"median_rpm": rpm, "catalog_equilibrium_rpm": round(expected, 2),
                         "median_kiss_erpm": statistics.median(tel), "duty": duty}
        # Motor catalog data omits waveform shape and bearing losses. Allow
        # 15% in the independent average-speed check, 1.5 KISS rpm quanta.
        checks[name + "_catalog_equilibrium"] = expected * rpm > 0 and abs(rpm / expected - 1) < .15
        checks[name + "_independent_telemetry_rpm"] = abs(statistics.median(tel) - abs(rpm)) <= 150
    checks["load_response"] = metrics["loaded"]["median_rpm"] < .75 * metrics["forward"]["median_rpm"]
    checks["brake_precedes_reverse"] = all(float(frames[i]["brake"]) > .10 and int(frames[i]["pair"]) == 0
                                           for i in range(303, 319)) and abs(float(frames[319]["rpm"])) < 10
    checks["neutral_between_brake_and_reverse"] = all(float(frames[i]["brake"]) == 0 and
        abs(float(frames[i]["rpm"])) < 10 for i in range(325, 338))
    checks["completed_at_rest"] = abs(float(frames[430]["rpm"])) < 15 and int(frames[430]["pair"]) == 0
    return {"verdict": "PASS" if all(checks.values()) else "FAIL", "checks": checks, "phase_events": phase_counts,
            "metrics": metrics, "kiss_packets": len(packets), "adc_scans": int(frames[430]["adc"])}


def run(qemu, source, out):
    out.mkdir(parents=True, exist_ok=False)
    original = source.read_bytes()
    if len(original) != 128 * 1024:
        raise ValueError("expected complete 128 KiB ESCAPE1 flash bank")
    flash = bytearray(original)
    changes = settings(flash)
    image = out / "application-flash.bin"
    image.write_bytes(flash)
    trace, telemetry = out / "board.trace", out / "kiss.bin"
    cmd = [str(qemu), "-M", "escape1-g071", "-kernel", str(image), "-serial", "null", "-serial",
           "file:" + str(telemetry), "-display", "none", "-monitor", "none", "-no-reboot",
           "-icount", "shift=4,align=off,sleep=off", "-d", "page", "-D", str(trace)]
    with (out / "qemu.log").open("wb") as log:
        subprocess.run(cmd, stdout=log, stderr=log, env={**os.environ, "ESCAPE1_BENCH": "maxon-283828"},
                       check=True, timeout=150)
    raw = trace.read_bytes()
    wire = telemetry.read_bytes()
    result = oracle(raw.decode(), wire)
    result.update(command=cmd, input_sha256=sha(original), configuration=changes, deployed_sha256=sha(flash),
                  executable_flash_sha256=sha(flash[0x1800:]), qemu_sha256=sha(qemu.read_bytes()),
                  trace_sha256=sha(raw), kiss_sha256=sha(wire))
    (out / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    if result["verdict"] != "PASS":
        raise ValueError([key for key, accepted in result["checks"].items() if not accepted])
    print(out / "result.json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ("qemu", "input_flash", "out"):
        parser.add_argument(arg, type=Path)
    args = parser.parse_args()
    run(args.qemu.resolve(), args.input_flash.resolve(), args.out.resolve())
