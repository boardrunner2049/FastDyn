#!/usr/bin/env python3
"""Measure a whole-program FreeRTOS POSIX rehost under a fixed RTOS-time window.

The demo is real-time/tick driven, so this reports host CPU consumed per fixed
guest tick interval.  It is not a firmware-throughput or Cortex-M-cycle metric.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import resource
import signal
import statistics
import subprocess
import time
from pathlib import Path


TICK_RE = re.compile(r"OK: No errors - tick count (\d+)")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def current_rss_kib(pid: int) -> int | None:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except (FileNotFoundError, ProcessLookupError):
        return None
    return None


def run_once(path: Path, duration: float) -> dict[str, object]:
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = time.monotonic()
    process = subprocess.Popen(
        [str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    peak_rss_kib = 0
    deadline = started + duration
    while process.poll() is None and time.monotonic() < deadline:
        rss = current_rss_kib(process.pid)
        if rss is not None:
            peak_rss_kib = max(peak_rss_kib, rss)
        time.sleep(0.05)

    if process.poll() is None:
        os.killpg(process.pid, signal.SIGINT)
    try:
        output, _ = process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        output, _ = process.communicate()

    ended = time.monotonic()
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    ticks = [int(match) for match in TICK_RE.findall(output)]
    return {
        "wall_seconds": ended - started,
        "user_seconds": after.ru_utime - before.ru_utime,
        "system_seconds": after.ru_stime - before.ru_stime,
        "cpu_seconds": (after.ru_utime - before.ru_utime)
        + (after.ru_stime - before.ru_stime),
        "peak_rss_kib": peak_rss_kib,
        "returncode": process.returncode,
        "last_ok_tick": ticks[-1] if ticks else None,
        "ok": bool(ticks) and process.returncode == 2,
        "output_tail": output[-500:],
    }


def summarize(samples: list[dict[str, object]]) -> dict[str, object]:
    def median(field: str) -> float:
        return statistics.median(float(sample[field]) for sample in samples)

    return {
        "sample_count": len(samples),
        "all_ok": all(bool(sample["ok"]) for sample in samples),
        "median_wall_seconds": median("wall_seconds"),
        "median_user_seconds": median("user_seconds"),
        "median_system_seconds": median("system_seconds"),
        "median_cpu_seconds": median("cpu_seconds"),
        "median_peak_rss_kib": median("peak_rss_kib"),
        "median_last_ok_tick": statistics.median(
            int(sample["last_ok_tick"])
            for sample in samples
            if sample["last_ok_tick"] is not None
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--variant", action="append", required=True,
                        help="NAME=/absolute/path/to/binary")
    parser.add_argument("--samples", type=int, default=11)
    parser.add_argument("--duration", type=float, default=11.25)
    parser.add_argument("--seed", type=int, default=20261116)
    args = parser.parse_args()

    variants: dict[str, Path] = {}
    for spec in args.variant:
        name, separator, raw_path = spec.partition("=")
        if not separator or not name:
            parser.error(f"invalid variant: {spec}")
        path = Path(raw_path).resolve()
        if not path.is_file():
            parser.error(f"missing binary: {path}")
        variants[name] = path

    order = [name for _ in range(args.samples) for name in variants]
    random.Random(args.seed).shuffle(order)
    samples: dict[str, list[dict[str, object]]] = {
        name: [] for name in variants
    }
    for sequence, name in enumerate(order, 1):
        sample = run_once(variants[name], args.duration)
        sample["sequence"] = sequence
        samples[name].append(sample)
        print(
            f"[{sequence}/{len(order)}] {name}: "
            f"cpu={sample['cpu_seconds']:.3f}s tick={sample['last_ok_tick']} "
            f"ok={sample['ok']}",
            flush=True,
        )

    document = {
        "schema_version": 1,
        "method": {
            "description": "host CPU consumed in a fixed RTOS-time window",
            "duration_seconds": args.duration,
            "samples_per_variant": args.samples,
            "randomization_seed": args.seed,
            "expected_returncode": 2,
            "semantic_marker": "OK: No errors",
            "limitations": [
                "real-time/tick-driven workload, not completed-work throughput",
                "x86-64 POSIX RTOS port, not Cortex-M execution",
                "measurement build maps _exit to exit so BOLT DT_FINI runs",
            ],
        },
        "variants": {
            name: {
                "path": str(path),
                "sha256": sha256(path),
                "file_bytes": path.stat().st_size,
                "samples": samples[name],
                "summary": summarize(samples[name]),
            }
            for name, path in variants.items()
        },
    }
    print("RESULT_JSON_BEGIN")
    print(json.dumps(document, indent=2, sort_keys=True))
    return 0 if all(item["summary"]["all_ok"] for item in document["variants"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
