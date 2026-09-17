#!/usr/bin/env python3
"""Avatar2 HITL passthrough microbench (paper Fig 7 methodology).

Runs a bare-metal firmware inside avatar-qemu; every MMIO load to a real
STM32F429 peripheral address (GPIOA_IDR @ 0x40020010 by default) traps in
QEMU, is forwarded through Avatar2's OpenOCDTarget -> openocd -> SWD ->
physical STM32F429I-DISC1, and the value returns to the emulated firmware.
Timed window: bench_start() -> bench_done() sentinel functions.

Emits a CSV row matching the FastDyn passthrough harness so plot.py can
render a side-by-side comparison.

Paths that can be overridden with env vars (defaults infer from the
script's location; see the README for the assumed layout):
  AVATAR_QEMU        path to avatar-qemu binary (avatar-qemu fork with
                     the `configurable` machine; built as an avatar2
                     submodule)
  OPENOCD_SCRIPT     openocd board config (default: stock stm32f429disco)
  OPENOCD_SEARCH_DIR openocd -s scripts search path
  LIBAIO_SHIM        LD_LIBRARY_PATH shim dir for libaio.so.1 (Ubuntu
                     24.04 only; harmless if the dir doesn't exist).
"""
from __future__ import annotations

import argparse
import csv
import os
import statistics
import subprocess
import sys
import time
import warnings
from pathlib import Path

import posix_ipc

# avatar2 upstream: benign pkg_resources / regex-escape warnings on 3.12.
[warnings.filterwarnings("ignore", category=c)
 for c in (DeprecationWarning, UserWarning, SyntaxWarning)]

# --------- layout inference ---------------------------------------------
# This script lives at:
#   <FastDyn>/boardrunner/boardrunner_examples/microbenchmarks/
#            hitl_comparison/avatar2/scripts/bench_avatar2.py
# → HITL_ROOT = 3 dirs up (hitl_comparison/)
# → FDYN_ROOT = 7 dirs up (FastDyn/)
HERE = Path(__file__).resolve().parent
HITL_ROOT = HERE.parent.parent
FDYN_ROOT = HITL_ROOT.parents[3]
FW_DIR    = HITL_ROOT / "firmware" / "firmware_src" / "build"

# libaio shim for Ubuntu 24.04 -- see README. Harmless where not present.
_shim = Path(os.environ.get("LIBAIO_SHIM", HITL_ROOT / "libaio_shim"))
if _shim.is_dir():
    prev = os.environ.get("LD_LIBRARY_PATH", "")
    os.environ["LD_LIBRARY_PATH"] = f"{_shim}:{prev}" if prev else str(_shim)

from avatar2 import Avatar, ARM, QemuTarget, OpenOCDTarget  # noqa: E402

CSV_HEADER = ["tool", "direction", "iters", "total_s", "per_op_ms"]
_clock = lambda: time.clock_gettime(time.CLOCK_MONOTONIC_RAW)


def _resolve_symbols(elf: Path, names: tuple[str, ...]) -> dict[str, int]:
    # Bench PCs shift with -DN_INNER; nm the ELF instead of hard-coding.
    out = subprocess.check_output(
        ["arm-none-eabi-nm", "--defined-only", str(elf)], text=True)
    resolved: dict[str, int] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[1] in ("T", "t", "W", "w") and parts[2] in names:
            resolved[parts[2]] = int(parts[0], 16)
    missing = set(names) - set(resolved)
    if missing:
        raise RuntimeError(f"missing symbols in {elf}: {sorted(missing)}")
    return resolved


def _preseed_rmem_queues(qemu_name: str = "qemu") -> None:
    # Avatar2's RemoteMemoryProtocol.connect() opens the message queues
    # O_RDONLY ~140 ms after spawning QEMU, racing avatar-rmemory's own
    # mq_open(O_CREAT). Pre-create the names so both sides always find
    # them; close-without-unlink keeps the name in the namespace.
    for name in (f"/{qemu_name}_rx_queue", f"/{qemu_name}_tx_queue"):
        try:
            posix_ipc.unlink_message_queue(name)
        except posix_ipc.ExistentialError:
            pass
        posix_ipc.MessageQueue(
            name, flags=posix_ipc.O_CREAT,
            max_messages=10, max_message_size=1024,
        ).close()


def _run_session(fw_axf: Path, fw_bin: Path, args: argparse.Namespace,
                 syms: dict[str, int]) -> float:
    _preseed_rmem_queues("qemu")
    avatar = Avatar(arch=ARM, output_directory=args.output_dir)
    # Cortex-M reset vector fetch happens from address 0; alias flash there.
    avatar.add_memory_range(0x08000000, 0x00200000, name="flash",
                            file=str(fw_bin), permissions="r-x")
    avatar.add_memory_range(0x00000000, 0x00100000, name="alias",
                            file=str(fw_bin), permissions="r-x")
    avatar.add_memory_range(0x20000000, 0x00030000, name="ram")
    # cortex-m3, not m4: avatar-qemu's `configurable` machine only special-
    # cases m3 for NVIC wiring. Our firmware is M4-compiled with soft-float
    # and no DSP, so its instruction stream is M3-compatible.
    #
    # entry_address is the reset PC (thumb bit set), from the vector table.
    # avatar-qemu does NOT emulate the Cortex-M vector-table SP/PC fetch;
    # without this the CPU boots to PC=0 and immediately faults.
    # Reset_Handler's first instruction loads SP from the vector table itself,
    # so we don't need to set SP explicitly.
    reset_pc = syms.get("Reset_Handler", 0) | 1
    qemu = avatar.add_target(
        QemuTarget, name="qemu",
        gdb_port=args.qemu_gdb_port,
        executable=args.avatar_qemu,
        cpu_model="cortex-m3",
        entry_address=reset_pc,
        log_items="int,unimp,guest_errors",
    )
    openocd = avatar.add_target(
        OpenOCDTarget, name="openocd",
        openocd_script=args.openocd_script,
        additional_args=["-s", args.openocd_search_dir],
    )
    # Forward EVERY STM32 peripheral access (0x40000000-0x60000000, 512 MB)
    # to the real chip. QEMU doesn't have to model any STM32 peripheral --
    # any MMIO the firmware touches round-trips to the physical target.
    # 0xE0000000-0xE0040000 is deliberately NOT forwarded: it's the ARMv7-M
    # NVIC/SysTick/SCB, which QEMU's CPU model owns; forwarding would break
    # interrupt delivery inside the emulated CPU.
    avatar.add_memory_range(0x40000000, 0x20000000, name="stm32_periph",
                            forwarded=True, forwarded_to=openocd)
    avatar.init_targets()
    qemu.set_breakpoint(syms["bench_start"])
    qemu.set_breakpoint(syms["bench_done"])
    qemu.cont(); qemu.wait()
    if qemu.regs.pc != syms["bench_start"]:
        raise RuntimeError(f"expected bench_start, got 0x{qemu.regs.pc:x}")
    t0 = _clock()
    qemu.cont(); qemu.wait()
    t1 = _clock()
    if qemu.regs.pc != syms["bench_done"]:
        raise RuntimeError(f"expected bench_done, got 0x{qemu.regs.pc:x}")
    avatar.shutdown()
    return t1 - t0


def _run(args: argparse.Namespace) -> int:
    fw_axf = Path(args.firmware).resolve()
    fw_bin = fw_axf.with_suffix(".bin")
    # avatar-qemu loads a raw image; derive it from the ELF when it is absent
    # (pre_built/ ships only .axf files).
    if fw_axf.is_file() and not fw_bin.is_file():
        subprocess.run(["arm-none-eabi-objcopy", "-O", "binary", str(fw_axf), str(fw_bin)],
                       check=True)
    for p in (fw_axf, fw_bin, Path(args.openocd_script), Path(args.avatar_qemu)):
        if not p.is_file():
            print(f"bench_avatar2: missing prerequisite {p}", file=sys.stderr)
            return 2

    syms = _resolve_symbols(fw_axf, ("bench_start", "bench_done", "Reset_Handler"))
    print(f"# firmware:  {fw_axf.name}  N={args.n}  "
          f"bench_start=0x{syms['bench_start']:08x}  "
          f"bench_done=0x{syms['bench_done']:08x}")

    Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    session_times: list[float] = []
    for i in range(args.iters):
        t = _run_session(fw_axf, fw_bin, args, syms)
        session_times.append(t)
        print(f"iter {i}: {t:.4f}s  ({t / args.n * 1000:.3f} ms/op)")

    total_s = statistics.mean(session_times)
    per_op_ms = total_s / args.n * 1000.0
    stddev = statistics.stdev(session_times) if len(session_times) >= 2 else 0.0
    print(f"# avatar2 mean={total_s:.4f}s  stddev={stddev:.4f}s  "
          f"per_op={per_op_ms:.4f} ms  (N={args.n}, sessions={args.iters})")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not out_path.exists() or out_path.stat().st_size == 0
    with out_path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_HEADER)
        if write_header:
            w.writeheader()
        w.writerow({"tool": "avatar2", "direction": args.direction,
                    "iters": args.n,
                    "total_s": f"{total_s:.6f}", "per_op_ms": f"{per_op_ms:.6f}"})
    return 0


def _infer_direction(fw_name: str) -> str:
    # Firmware filenames follow microbench_hitl_{read,write}_N<N>.axf
    if "_read_" in fw_name:
        return "reads"
    if "_write_" in fw_name:
        return "writes"
    raise ValueError(
        f"could not infer direction from firmware name '{fw_name}'; "
        "pass --direction {reads,writes} explicitly")


def main() -> int:
    default_qemu = os.environ.get(
        "AVATAR_QEMU",
        # Sensible guess: avatar-qemu built inside a sibling avatar2 clone.
        str(FDYN_ROOT.parent / "avatar2" / "targets" / "src" /
            "avatar-qemu" / "build" / "qemu-system-arm"),
    )
    default_od_script = os.environ.get(
        "OPENOCD_SCRIPT",
        "/usr/share/openocd/scripts/board/stm32f429discovery.cfg")
    default_od_search = os.environ.get(
        "OPENOCD_SEARCH_DIR", "/usr/share/openocd/scripts")

    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--firmware", required=True,
                   help=f"path to microbench_hitl_N<...>.axf "
                        f"(build with `make -C {FW_DIR.parent} all`)")
    p.add_argument("--n", type=int, required=True,
                   help="MMIO reads inside the firmware's bench window "
                        "(matches -DN_INNER at compile time)")
    p.add_argument("--iters", type=int, default=1,
                   help="independent Avatar2 sessions to average (default 1)")
    p.add_argument("--direction", choices=("reads", "writes"), default=None,
                   help="operation direction (default: infer from firmware "
                        "filename, e.g. *_read_* -> reads, *_write_* -> writes)")
    p.add_argument("--avatar-qemu", default=default_qemu,
                   help="path to avatar-qemu binary with -machine configurable "
                        "(default: $AVATAR_QEMU env var, else sibling avatar2 clone)")
    p.add_argument("--qemu-gdb-port", type=int, default=1234)
    p.add_argument("--openocd-script", default=default_od_script,
                   help="openocd -f board config (default: stock STM32F429disco)")
    p.add_argument("--openocd-search-dir", default=default_od_search)
    p.add_argument("--out", default=str(HITL_ROOT / "avatar2" / "results" / "results.csv"),
                   help="CSV output path (appended)")
    p.add_argument("--output-dir", default=str(HERE.parent / ".avatar_out"),
                   help="avatar2 scratch dir for openocd/qemu logs")
    args = p.parse_args()
    if args.direction is None:
        args.direction = _infer_direction(Path(args.firmware).name)
    return _run(args)


if __name__ == "__main__":
    sys.exit(main())
