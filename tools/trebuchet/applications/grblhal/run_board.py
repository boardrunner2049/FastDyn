"""Replay the unchanged drawing over USART1 and inspect TIM2-driven GPIO edges."""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import select
import socket
import subprocess
import tempfile
import time

from run_simulator import JOB_SHA, require


def sha(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def pins(out):
    reference = Path(__file__).parent / "observations/native/step-requests.bin.gz"
    expected = gzip.decompress(reference.read_bytes())
    require(hashlib.sha256(expected).hexdigest() == "142b861eb33e74522b85251b8b5fb0d75a99f03e23eadc3567616db2ddba156f",
            "native oracle digest mismatch")
    events = bytearray()
    directions = [0, 0, 0]
    axis_steps = [0, 0, 0]
    last_fall = [None, None, None]
    widths = []
    last = [0, 0, 0]
    start = [None, None, None]
    prior_t = -1
    for line in (out / "pins.txt").read_text().splitlines():
        t, b, c = line.split()
        t, b, c = int(t), int(b, 16), int(c, 16)
        require(t >= prior_t, "virtual time reversed")
        prior_t = t
        direction = (b >> 12 & 1) | ((b >> 2 & 1) << 1) | ((c >> 5 & 1) << 2)
        rise = 0
        for axis, bit in enumerate((13, 10, 0)):
            level = (b >> bit) & 1
            sign = (direction >> axis) & 1
            if last[axis]:
                require(sign == directions[axis], f"direction changed during active pulse on axis {axis}")
            if level and not last[axis]:
                require(last_fall[axis] is None or t > last_fall[axis], "zero-width pulse gap")
                directions[axis] = sign
                axis_steps[axis] += 1
                require(not (b & (1 << (14, 11, 1)[axis])), f"axis {axis} stepped while disabled")
                start[axis] = t
                rise |= 1 << axis
            elif not level and last[axis]:
                width = t - start[axis]
                require(width > 0, "zero-width step")
                widths.append(width)
                last_fall[axis] = t
                start[axis] = None
            last[axis] = level
        if rise:
            events.append((direction << 4) | rise)
    require(not any(last), "unfinished pulse")
    (out / "requests.bin").write_bytes(events)
    require(events == expected, f"ordered pin-direction and rise stream mismatch ({len(events)} events)")
    return {"rises": len(events), "axis_steps": axis_steps, "completed_axis_pulses": len(widths),
            "pulse_width_ns": [min(widths), max(widths)], "ordered_rises_sha256": sha(out / "requests.bin")}


def run(qemu, image, job, out, timeout, profile_plugin=None, fastdyn_plugin=None, cache=None):
    deadline = time.monotonic() + timeout
    commands = job.read_bytes().splitlines()
    require(sha(job) == JOB_SHA, "unexpected drawing")
    receipt = {"image_sha256": sha(image), "qemu_sha256": sha(qemu), "job_sha256": sha(job),
               "protocol": "UART1 line/ack, final dwell then idle position and drained pins",
               "accepted_commands": 0}
    with tempfile.TemporaryDirectory() as temp, (out / "serial.txt").open("wb") as log, (out / "stderr.txt").open("wb") as err:
        if cache:
            # The TOML itself enters FastDyn's cache fingerprint. Every private
            # deployment of one frozen image must use the same QMP endpoint.
            token = hashlib.sha256(os.fsencode(cache.resolve())).hexdigest()[:20]
            path = Path(tempfile.gettempdir()) / f"grbl-{token}.qmp"
            require(not path.exists(), f"QMP endpoint already in use: {path}")
        else:
            path = Path(temp) / "qmp"
        args = [str(qemu), "-M", "btt-skr-e3-v2,memory-backend=ram0", "-cpu", "cortex-m3",
                "-object", "memory-backend-ram,id=ram0,size=48K", "-m", "48K",
                "-global", "cortexm-soc.ram_baseaddr0=0x20000000", "-global", "armv7m.init-nsvtor=0x08007000",
                "-kernel", str(image), "-display", "none", "-serial", "stdio", "-monitor", "none",
                "-icount", "shift=3,sleep=off", "-qmp", f"unix:{path},server,wait=off"]
        env = dict(os.environ)
        if fastdyn_plugin:
            from board.fastdyn_config import command
            launch = command(qemu, image, fastdyn_plugin, out, path, cache)
            args = launch["argv"]
            env.update(launch["env_set"])
            for key in launch["env_unset"]:
                env.pop(key, None)
            receipt["fastdyn_plugin_sha256"] = sha(fastdyn_plugin)
            receipt["fastdyn_config_sha256"] = sha(out / "fastdyn.toml")
            if cache:
                args = ["setarch", os.uname().machine, "-R", *args]
        if profile_plugin:
            args += ["-plugin", f"{profile_plugin.resolve()},out={out / 'drawing.tbprof'}"]
            receipt["profile_plugin_sha256"] = sha(profile_plugin)
        proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err,
                                env=dict(env, BTT_PIN_LOG=str(out / "pins.txt"), BTT_IO_LOG=str(out / "io.txt")))
        pending = b""
        def until(test, boot=False):
            nonlocal pending
            while True:
                while b"\n" not in pending:
                    left = deadline - time.monotonic()
                    if left <= 0 or not select.select([proc.stdout], [], [], left)[0]:
                        raise TimeoutError(f"timeout after {receipt['accepted_commands']} drawing commands")
                    data = os.read(proc.stdout.fileno(), 8192)
                    require(data, "unexpected QEMU exit")
                    log.write(data)
                    log.flush()
                    pending += data
                line, pending = pending.split(b"\n", 1)
                line = line.strip()
                if not (boot and line == b"error:7"):
                    require(not line.startswith((b"error:", b"ALARM:")), f"controller {line!r}")
                require(boot or not line.startswith(b"GrblHAL "), "unexpected controller reset")
                if test(line):
                    return line

        def send(data):
            proc.stdin.write(data)
            proc.stdin.flush()
        try:
            until(lambda line: line.startswith(b"GrblHAL "), boot=True)
            send(b"$I\n")
            until(lambda line: line == b"ok")
            send(b"$$\n")
            until(lambda line: line == b"ok")
            send(b"?")
            until(lambda line: line.startswith(b"<Idle|"))
            start = time.monotonic_ns()
            for line in commands:
                send(line + b"\n")
                until(lambda line: line == b"ok")
                receipt["accepted_commands"] += 1
            send(b"?")
            state = until(lambda line: line.startswith(b"<")).decode()
            receipt["job_wall_ns"] = time.monotonic_ns() - start
            receipt["final_status"] = state
            require(re.search(r"^<Idle\|MPos:0\.000,0\.000,3\.812(?:\||>)", state), f"final state {state}")
            with socket.socket(socket.AF_UNIX) as sock:
                sock.settimeout(10)
                sock.connect(str(path))
                with sock.makefile("rwb", buffering=0) as rpc:
                    require("QMP" in json.loads(rpc.readline()), "missing QMP greeting")
                    for cmd in ("qmp_capabilities", "quit"):
                        rpc.write(json.dumps({"execute": cmd}).encode() + b"\n")
                        while "return" not in json.loads(rpc.readline()):
                            pass
            receipt["qemu_exit"] = proc.wait(timeout=10)
            require(receipt["qemu_exit"] == 0, "QEMU shutdown failed")
            receipt.update(pins(out))
            ee, *drivers = [list(map(int, row.split())) for row in (out / "io.txt").read_text().splitlines()]
            require(ee[0] > 0 and ee[1] > 0 and ee[2] == 0, f"EEPROM or CRC evidence {ee}")
            require(sum(reads > 0 and writes > 0 for _, reads, writes in drivers) == 3, f"TMC evidence {drivers}")
            receipt.update(eeprom_read_bytes=ee[0], eeprom_write_bytes=ee[1],
                           tmc_address_reads_writes=drivers, tmc_crc_errors=ee[2],
                           pins_sha256=sha(out / "pins.txt"))
            if profile_plugin:
                profile = out / "drawing.tbprof"
                require(profile.stat().st_size > 0, "missing shutdown profile")
                receipt["profile_sha256"] = sha(profile)
            receipt["state"] = "passed"
        except Exception as exc:
            receipt.update(state="failed", reason=str(exc))
            raise
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            (out / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    for arg in ("qemu", "image", "job", "out"):
        p.add_argument(arg, type=Path)
    p.add_argument("--timeout", type=float, default=180)
    p.add_argument("--profile-plugin", type=Path, help="shutdown-dumping FirmBOLT TB profiler, diagnostic only")
    p.add_argument("--fastdyn-plugin", type=Path, help="run the FastDyn-generated command with its normal plugin")
    p.add_argument("--cache", type=Path, help="ordinary supercache path on an ASLR-compatible host")
    args = p.parse_args()
    if args.cache and (not args.fastdyn_plugin or args.profile_plugin):
        p.error("--cache requires --fastdyn-plugin on its own")
    args.out.mkdir(parents=True, exist_ok=False)
    run(*(getattr(args, a).resolve() for a in ("qemu", "image", "job", "out")), args.timeout, args.profile_plugin, args.fastdyn_plugin, args.cache)
