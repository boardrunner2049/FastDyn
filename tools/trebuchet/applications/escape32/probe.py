#!/usr/bin/env python3
"""Record where an unmodified ESCape32 board image runs on the generic machine."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import time


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def monitor(stream, execute, arguments=None):
    request = {"execute": execute}
    if arguments is not None:
        request["arguments"] = arguments
    stream.write(json.dumps(request).encode() + b"\n")
    while line := stream.readline():
        message = json.loads(line)
        if "error" in message:
            raise RuntimeError(message["error"])
        if "return" in message:
            return message["return"]
    raise EOFError("QEMU closed the QMP socket")


def read_registers(stream):
    monitor(stream, "stop")
    try:
        raw = monitor(stream, "human-monitor-command", {"command-line": "info registers"})
        result = {name: value for name, value in re.findall(r"\b(R\d\d)=([\da-fA-F]+)", raw)}
        if "R15" not in result:
            raise ValueError(f"missing ARM registers in {raw}")
        return result
    finally:
        monitor(stream, "cont")


def sample_machine(command, directory):
    # The QMP socket path is short even when the user's workspace path is long.
    with tempfile.TemporaryDirectory() as sockets, (directory / "qemu.log").open("w") as log:
        qmp = Path(sockets, "qmp.sock")
        with subprocess.Popen([*command, "-qmp", f"unix:{qmp},server=on,wait=off", "-S"],
                              stdout=log, stderr=subprocess.STDOUT) as process:
            try:
                deadline = time.monotonic() + 15
                while not qmp.exists():
                    if process.poll() is not None:
                        raise RuntimeError(f"QEMU exited {process.returncode}; inspect {log.name}")
                    if time.monotonic() >= deadline:
                        raise TimeoutError("QMP startup timed out")
                    time.sleep(.01)
                with socket.socket(socket.AF_UNIX) as client:
                    client.settimeout(5)
                    client.connect(str(qmp))
                    with client.makefile("rwb", buffering=0) as stream:
                        if "QMP" not in json.loads(stream.readline()):
                            raise ValueError("QMP greeting missing")
                        monitor(stream, "qmp_capabilities")
                        monitor(stream, "cont")
                        samples = []
                        for _ in range(4):
                            time.sleep(.1)
                            samples.append(read_registers(stream))
                        monitor(stream, "quit")
                if process.wait(timeout=5):
                    raise RuntimeError(f"QEMU exited {process.returncode}")
                return samples
            finally:
                if process.poll() is None:
                    process.kill()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("work", type=Path)
    parser.add_argument("qemu", type=Path)
    parser.add_argument("--image", choices=("release", "rebuild"), required=True)
    args = parser.parse_args()
    work, qemu = args.work.resolve(), args.qemu.resolve()
    directory = Path(tempfile.mkdtemp(prefix=f"probe-{args.image}-", dir=work))
    app = "ESCAPE1-rev17.3"
    boot = "BOOT2_PA2-rev4"
    if args.image == "release":
        inputs = [work / "release" / f"{name}.bin" for name in (boot, app)]
        expected = ("bc0e49a782a622f966b1387ef117f338ef820489b80fe1c91a7d57bd15fed42a",
                    "ca88d9c173f784717f6c75cebee4ed43ed4abb83756de8e065aa09e410a10604")
        load = []
        for image, address, image_hash in zip(inputs, (0x08000000, 0x08001000), expected, strict=True):
            if digest(image) != image_hash:
                raise ValueError(f"release image hash mismatch: {image}")
            # HEX preserves the exact bytes and their flash addresses. QEMU's
            # raw generic loader limits each input to the configured SRAM size.
            ihex = directory / image.with_suffix(".hex").name
            subprocess.run(["arm-none-eabi-objcopy", "-I", "binary", "-O", "ihex",
                            "--change-addresses", hex(address), image, ihex], check=True)
            load += ["-device", f"loader,file={ihex}"]
    else:
        inputs = [work / "build/boot" / f"{boot}.elf", work / "build" / f"{app}.elf"]
        load = ["-kernel", str(inputs[0]), "-device", f"loader,file={inputs[1]}"]
    command = [str(qemu), "-M", "cortexm,memory-backend=ram0", "-cpu", "cortex-m0",
               "-object", "memory-backend-ram,id=ram0,size=8K", "-m", "8K",
               "-global", "cortexm-soc.ram_baseaddr0=0x20000000",
               "-global", "armv7m.init-nsvtor=0x08000000", *load, "-display", "none",
               "-serial", "none", "-monitor", "none", "-icount", "shift=0,align=off,sleep=off",
               "-d", "in_asm", "-D", str(directory / "boot.trace")]
    samples = sample_machine(command, directory)
    # These register assignments and instruction bytes apply to the two tested
    # bootloader builds. Keep other outcomes available for manual diagnosis.
    boot_bytes = work / ("release" if args.image == "release" else "build/boot") / f"{boot}.bin"
    observed_wait = (boot_bytes.read_bytes()[0xf2:0xf8] == bytes.fromhex("19681142fcd0")
                     and all(int(s["R15"], 16) in (0x080000f2, 0x080000f4, 0x080000f6)
                             and int(s["R03"], 16) == 0x40021000
                             and int(s["R02"], 16) == 0x02000000
                             and int(s["R01"], 16) & 0x02000000 == 0 for s in samples))
    record = {"application": "escape32", "image": args.image,
              "observation": "rcc_pll_ready_wait" if observed_wait else "unclassified",
              "probe": "generic machine reset diagnostic", "qemu_sha256": digest(qemu),
              "argv": command, "inputs": {p.name: digest(p) for p in inputs},
              "register_samples": samples}
    if args.image == "rebuild":
        locations = subprocess.check_output(["arm-none-eabi-addr2line", "-fe", inputs[0],
                                             *["0x" + s["R15"] for s in samples]], text=True)
        record["bootloader_source_locations"] = locations.splitlines()
    output = directory / "observation.json"
    output.write_text(json.dumps(record, indent=2) + "\n")
    print(output)


if __name__ == "__main__":
    main()
