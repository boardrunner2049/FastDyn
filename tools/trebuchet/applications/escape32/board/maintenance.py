#!/usr/bin/env python3
"""Read back the user configuration using the unmodified ESCape32 half-duplex boot protocol."""
import argparse
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import zlib

def sha(data):
    return hashlib.sha256(data).hexdigest()

def run(qemu, boot, app, out):
    out.mkdir(parents=True, exist_ok=False)
    bootrom, application = boot.read_bytes(), app.read_bytes()
    if len(bootrom) > 4096 or len(application) + 4096 > 128 * 1024:
        raise ValueError("input images exceed the board flash map")
    flash = bytearray(b"\xff" * (128 * 1024))
    flash[:len(bootrom)] = bootrom
    flash[4096:4096 + len(application)] = application
    image = out / "flash.bin"
    image.write_bytes(flash)
    report = {"verdict": "FAIL", "workload": "read both 1-KiB blocks of ESCAPE1 configuration flash",
              "boot_sha256": sha(bootrom), "application_sha256": sha(application),
              "qemu_sha256": sha(qemu.read_bytes()), "operations": []}
    trace = out / "startup.trace"
    with tempfile.TemporaryDirectory() as tmp, socket.socket(socket.AF_UNIX) as listener, (out/"qemu.log").open("wb") as log:
        path = Path(tmp)/"uart"
        listener.bind(str(path))
        listener.listen(1)
        listener.settimeout(5)
        cmd = [str(qemu), "-M", "escape1-g071", "-kernel", str(image), "-serial", f"unix:{path}",
               "-display", "none", "-monitor", "none", "-icount", "shift=0,align=off,sleep=off",
               "-d", "in_asm,guest_errors,trace:stm32l4x5_usart_write,trace:stm32l4x5_usart_rx", "-D", str(trace)]
        report["command"] = cmd
        with subprocess.Popen(cmd, stdout=log, stderr=log) as proc:
            try:
                uart, _ = listener.accept()
                with uart:
                    uart.settimeout(3)
                    def receive(length):
                        result = bytearray()
                        while len(result) < length:
                            chunk = uart.recv(length - len(result))
                            if not chunk:
                                raise EOFError("UART connection closed")
                            result.extend(chunk)
                        return bytes(result)

                    last_rx_enable = 0
                    def send(*words):
                        nonlocal last_rx_enable
                        # An operator can transmit only after the single-wire receiver
                        # is enabled. Each firmware response switches back to receive.
                        deadline = time.monotonic() + 3
                        while time.monotonic() < deadline:
                            with trace.open("rb") as stream:
                                stream.seek(last_rx_enable)
                                data = stream.read()
                            marker = b"USART: Write <0x0> <- 0xd"
                            pos = data.rfind(marker)
                            if pos >= 0:
                                last_rx_enable += pos + len(marker)
                                break
                            time.sleep(.001)
                        else:
                            raise TimeoutError("USART2 receiver was not enabled")
                        uart.sendall(bytes(v for word in words for v in (word, word ^ 255)))

                    def read_payload(size):
                        length = size // 4 - 1
                        if receive(2) != bytes((length, length ^ 255)):
                            raise ValueError("wrong bootloader response length")
                        data = receive(size)
                        crc = int.from_bytes(receive(4), "little")
                        if crc != zlib.crc32(data):
                            raise ValueError("firmware CRC disagrees with independent host CRC")
                        return data

                    send(0)
                    if receive(2) != b"\x00\xff":
                        raise ValueError("probe acknowledgement mismatch")
                    report["operations"].append("probe")
                    send(1)
                    info = read_payload(32)
                    if info != bytes.fromhex("040160040010") + bytes(26):
                        raise ValueError(f"board identity mismatch: {info.hex()}")
                    report["operations"].append("read board identity and verify CRC")
                    blocks = []
                    for block in range(2):
                        send(2, block, 255)
                        blocks.append(read_payload(1024))
                    data = b"".join(blocks)
                    if data != application[:2048]:
                        raise ValueError("configuration readback differs from loaded flash")
                    (out / "returned-config.bin").write_bytes(data)
                    report.update(verdict="PASS", device_info=info.hex(), readback_bytes=len(data),
                                  returned_sha256=sha(data))
                    report["operations"].append("read both configuration blocks and verify CRC and every byte")
            except Exception as exc:
                report["error"] = str(exc)
                raise
            finally:
                proc.terminate()
                proc.wait(timeout=5)
                (out/"result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(out/"result.json")

if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    for key in ("qemu", "boot", "application", "out"):
        p.add_argument(key, type=Path)
    args = p.parse_args()
    run(*(v.resolve() for v in vars(args).values()))
