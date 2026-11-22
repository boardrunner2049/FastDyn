#!/usr/bin/env python3
"""Run one cacheless Pye case on the upstream board and verify its returned document."""
import argparse
import hashlib
import json
from pathlib import Path
import resource
import socket
import subprocess
import tempfile
import time

from session import qemu_arguments, serve


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def children_cpu():
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime + usage.ru_stime


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", type=Path, required=True)
    parser.add_argument("--qemu", type=Path, required=True)
    parser.add_argument("--elf", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    diagnostics = parser.add_mutually_exclusive_group()
    diagnostics.add_argument("--training-profile-plugin", type=Path, help="FirmBOLT tbprof shared library")
    diagnostics.add_argument("--diagnostic-exec-trace", type=Path, help="write a training-only QEMU exec trace")
    diagnostics.add_argument("--diagnostic-editor-trace", type=Path,
                             help="record every training TB and UART TX event for phase attribution")
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    staged = json.loads(args.stage.read_text())
    outcome = {"verdict": "FAIL", "method": "native MPS2 board, cacheless",
               "case": staged["case"], "elf_sha256": digest(args.elf),
               "qemu_sha256": digest(args.qemu), "stage_sha256": digest(args.stage)}
    try:
        with tempfile.TemporaryDirectory() as tmp, socket.socket(socket.AF_UNIX) as listener:
            uart = Path(tmp) / "uart.sock"
            listener.bind(str(uart))
            listener.listen(1)
            listener.settimeout(.1)
            cmd = [str(args.qemu.resolve()), *qemu_arguments(args.elf, staged, uart)]
            trace_path = args.diagnostic_editor_trace or args.diagnostic_exec_trace
            if trace_path:
                if staged["role"] != "training":
                    raise ValueError("execution diagnostics require a training scenario")
                trace = trace_path.resolve()
                trace.touch(exist_ok=False)
                trace_flags = "exec,nochain,trace:cmsdk_apb_uart_tx" if args.diagnostic_editor_trace else "exec"
                cmd += ["-d", trace_flags, "-D", str(trace)]
                outcome.update(diagnostic="QEMU execution trace from guest reset to exit", trace_flags=trace_flags,
                               exec_trace_path=str(trace))
            if args.training_profile_plugin:
                if staged["role"] != "training":
                    raise ValueError("FirmBOLT training requires a training scenario")
                symbols = subprocess.check_output(["arm-none-eabi-nm", args.elf], text=True)
                stop = next(int(row.split()[0], 16) for row in symbols.splitlines()
                            if row.split()[1:] == ["T", "exit"]) & ~1
                profile = args.out.resolve() / "session.tbprof"
                cmd += ["-plugin", f"{args.training_profile_plugin.resolve()},out={profile},stop={stop:#x}"]
                outcome["diagnostic"] = "FirmBOLT TB profile from guest reset to exit"
                outcome["plugin_sha256"] = digest(args.training_profile_plugin)
            outcome["argv"] = cmd
            with (args.out / "qemu.log").open("xb") as log:
                cpu = children_cpu()
                start = time.monotonic_ns()
                deadline = time.monotonic() + args.timeout
                with subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                                      stdin=subprocess.DEVNULL) as process:
                    try:
                        while time.monotonic() < deadline:
                            if process.poll() is not None:
                                raise RuntimeError(f"QEMU exited {process.returncode} before UART connection")
                            try:
                                client, _ = listener.accept()
                                break
                            except socket.timeout:
                                continue
                        else:
                            raise TimeoutError("QEMU did not connect its UART")
                        with client:
                            outcome["application"] = serve(client, staged, args.out / "uart.bin",
                                                           deadline - time.monotonic())
                        rc = process.wait(timeout=max(0, deadline - time.monotonic()))
                        outcome.update(exit_code=rc, process_wall_ns=time.monotonic_ns() - start,
                                       qemu_process_cpu_seconds=children_cpu() - cpu)
                        if rc != 0:
                            raise RuntimeError(f"QEMU exited {rc}")
                    finally:
                        if process.poll() is None:
                            process.kill()
        if args.training_profile_plugin:
            if not profile.is_file() or not profile.stat().st_size:
                raise RuntimeError("the FirmBOLT plugin produced no profile")
            outcome["profile_sha256"] = digest(profile)
        if trace_path:
            if not trace.stat().st_size:
                raise RuntimeError("QEMU produced no execution trace")
            outcome.update(exec_trace_sha256=digest(trace), exec_trace_bytes=trace.stat().st_size)
        outcome["verdict"] = "PASS"
    except Exception as exc:
        outcome["error"] = str(exc)
        raise
    finally:
        (args.out / "result.json").write_text(json.dumps(outcome, indent=2) + "\n")
    print(args.out / "result.json")


if __name__ == "__main__":
    main()
