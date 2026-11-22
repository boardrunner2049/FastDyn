#!/usr/bin/env python3
"""Stage Pye's published module and serve a real editing session over a QEMU UART."""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import runpy
import socket
import tempfile
import time

MANIFEST = Path(__file__).with_name("manifest.json")
PSRAM_ADDRESS = 0x21000000
PROTOCOL = Path(__file__).with_name("protocol-queued.json")
READY = b"__PYE_TERMINAL_READY__\r\n"
BEGIN = b"__PYE_EDIT_BEGIN__\r\n"
END = b"__PYE_EDIT_END__\r\n"
DONE = b"__PYE_BUFFER_END__\r\n"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def checked_bytes(path, expected):
    data = path.read_bytes()
    if sha(data) != expected:
        raise ValueError(f"input hash mismatch: {path}")
    return data


def edit_keys(job):
    before = job["before"].encode()
    after = job["after"].encode()
    operation = job["operation"]
    if operation == "replace":
        # Ctrl-R, find, clear the replacement default, enter replacement, accept all.
        return b"\x12" + before + b"\r\x1b[3~" + after + b"\ra"
    if operation == "find-delete-last-letter":
        # Find the phrase, move to the end of each word, delete its last letter.
        return b"\x06" + before + b"\r" + b"\x1b[1;5C" * len(before.split()) + b"\x7f"
    if operation == "cut-and-type-first-line":
        # Go to the beginning, cut the existing first line, type its replacement.
        return b"\x14\x18" + after.rstrip(b"\n") + b"\r"
    if operation == "append-line":
        # Ctrl-G, line number, End, insert the comment.
        return b"\x07" + str(job["line"]).encode() + b"\r\x1b[F" + after[len(before) - 1:-1]
    raise ValueError(f"unknown editing operation: {operation}")


def stage(work: Path, case_name: str, out: Path):
    manifest = json.loads(MANIFEST.read_text())
    # The promotion role was frozen separately before ranking. The two files
    # keep the original four roles and their archived contract hash intact.
    jobs = dict(manifest["cases"])
    for name in ("promotion.json", "prechange.json"):
        jobs.update(json.loads(MANIFEST.with_name(name).read_text()))
    job = jobs[case_name]
    product = checked_bytes(work / "editor/pye.mpy", manifest["application_sha256"])
    source = checked_bytes(work / "editor" / job["file"], job["sha256"])
    before = job["before"].encode()
    if source.count(before) != 1:
        raise ValueError("the requested source correction must occur exactly once")
    expected = source.replace(before, job["after"].encode())
    keys = edit_keys(job)
    rom_tool = work / "micropython/tools/mpremote/mpremote/romfs.py"
    checked_bytes(rom_tool, "8fe682133c5683892bcab44380efe6113132836c69672c4e9e94b599c54399b5")
    writer = runpy.run_path(str(rom_tool))["VfsRomWriter"]()
    writer.mkfile("pye.mpy", product)
    writer.mkfile("document", source)
    rom = writer.finalise()
    out = out.resolve()
    files = {"product.romfs": rom, "keys.bin": keys, "expected.utf8": expected}
    record = {"family": manifest["family"], "case": case_name, "role": job["role"],
              "application_sha256": sha(product), "input_sha256": sha(source),
              "manifest_sha256": sha(MANIFEST.read_bytes()), "operation": job["operation"],
              "protocol": "pye-queued-uart-v1", "protocol_sha256": sha(PROTOCOL.read_bytes()),
              "romfs": str(out / "product.romfs"), "keys": str(out / "keys.bin"),
              "expected": str(out / "expected.utf8"), "romfs_sha256": sha(rom),
              "keys_sha256": sha(keys), "expected_output_sha256": sha(expected)}
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        if json.loads((out / "stage.json").read_text()) != record:
            raise ValueError(f"existing stage differs: {out}")
        for name, blob in files.items():
            checked_bytes(out / name, sha(blob))
        return record
    with tempfile.TemporaryDirectory(dir=out.parent) as staging:
        target = Path(staging) / "case"
        target.mkdir()
        for name, blob in files.items():
            (target / name).write_bytes(blob)
        (target / "stage.json").write_text(json.dumps(record, indent=2) + "\n")
        target.rename(out)
    return record


def qemu_arguments(elf: Path, staged: dict, uart: Path):
    return ["-M", "mps2-an385", "-cpu", "cortex-m3", "-display", "none", "-monitor", "none",
            "-semihosting", "-serial", f"unix:{uart.resolve()}", "-icount", "shift=0,align=off,sleep=off",
            "-kernel", str(elf.resolve()), "-device",
            f"loader,file={staged['romfs']},addr={PSRAM_ADDRESS:#x},force-raw=on"]


def guest_command(size):
    # The documented MicroPython port exits QEMU normally on machine.reset().
    # Send the full buffer after the end marker so transmitting it cannot
    # contribute to the operational editor latency.
    return f"""import machine, sys, uctypes, vfs
vfs.mount(vfs.VfsRom(uctypes.bytearray_at({PSRAM_ADDRESS}, {size})), '/app')
sys.path.append('/app')
from pye import pye
document = open('/app/document', 'rb').read().decode().split('\\n')
print('__PYE_TERMINAL_READY__')
if sys.stdin.read(1) != '!':
    raise ValueError('incorrect UART release byte')
print('__PYE_EDIT_BEGIN__')
result = pye(document)
print('__PYE_EDIT_END__')
print(repr(result))
print('__PYE_BUFFER_END__')
machine.reset()
""".encode()


def serve(sock: socket.socket, staged: dict, transcript: Path, timeout: float):
    if staged.get("protocol") != "pye-queued-uart-v1" or staged.get("protocol_sha256") != sha(PROTOCOL.read_bytes()):
        raise ValueError("stage was created for a different terminal timing protocol")
    frozen = json.loads((MANIFEST.parent / "qualification/case-identities.json").read_text())
    declared = frozen[staged["case"]]
    if any(staged.get(k) != v for k, v in declared.items()):
        raise ValueError("staged session differs from its frozen role, document, keys or oracle")
    manifest_bytes = MANIFEST.read_bytes()
    if (staged["manifest_sha256"] != sha(manifest_bytes)
            or staged["application_sha256"] != json.loads(manifest_bytes)["application_sha256"]):
        raise ValueError("staged session has a different application or manifest")
    rom = checked_bytes(Path(staged["romfs"]), staged["romfs_sha256"])
    keys = checked_bytes(Path(staged["keys"]), staged["keys_sha256"])
    expected = checked_bytes(Path(staged["expected"]), staged["expected_output_sha256"])
    transcript = Path(transcript)
    pending = bytearray()
    deadline = time.monotonic() + timeout
    with transcript.open("xb") as log:
        def until(marker):
            while marker not in pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"UART deadline before {marker!r}")
                sock.settimeout(remaining)
                chunk = sock.recv(65536)
                if not chunk:
                    raise EOFError(f"UART closed before {marker!r}")
                log.write(chunk)
                pending.extend(chunk)
                if len(pending) > 1024 * 1024:
                    raise ValueError(f"excess UART output before {marker!r}")
            end = pending.index(marker) + len(marker)
            data = bytes(pending[:end])
            del pending[:end]
            return data

        until(b">>> ")
        sock.sendall(b"\x01")
        until(b"exit\r\n>")
        sock.sendall(guest_command(len(rom)) + b"\x04")
        until(READY)
        # The recorder knows the fixed terminal geometry and affirmative quit
        # answer. Queue them with the keystrokes before the guest enters Pye.
        # The UART still delivers the bytes in order through its emulated FIFO.
        packet = b"!\x1b[24;80R" + keys + b"\x11\x7fy\r"
        start = time.monotonic_ns()
        sock.sendall(packet)
        until(BEGIN)
        until(b"\x1b[6n")
        editing = until(b"Quit (y/N/f)? ")
        tail = until(END)
        elapsed = time.monotonic_ns() - start
        if b"\x1b[1;23r" not in editing or b"\x1b[?9l" not in tail:
            raise ValueError("expected screen initialization and terminal teardown missing")
        body = until(DONE)[:-len(DONE)].strip()
        result = ast.literal_eval(body.decode())
        if type(result) is not list or not all(type(line) is str for line in result):
            raise ValueError("unexpected returned editor buffer")
        received = "\n".join(result).encode()
        (transcript.parent / "returned.utf8").write_bytes(received)
        if received != expected:
            raise ValueError(f"returned buffer hash {sha(received)} != {sha(expected)}")
        prompt = {"replace": b"Replace (yes/No/all/quit) ?",
                  "find-delete-last-letter": b"Find: ", "append-line": b"Goto Line: ",
                  "cut-and-type-first-line": b"#!/bin/sh"}[staged["operation"]]
        if prompt not in editing or b"V2.80" not in editing:
            raise ValueError("expected Pye version or active edit dialog missing from UART")
    return {"verdict": "PASS", "case": staged["case"], "role": staged["role"],
            "protocol": staged["protocol"], "protocol_sha256": staged["protocol_sha256"],
            "queued_uart_sha256": sha(packet), "elapsed_ns": elapsed, "returned_bytes": len(received), "returned_lines": len(result),
            "returned_sha256": sha(received), "uart_sha256": sha(transcript.read_bytes()),
            "expected_output_sha256": sha(expected), "keys_sha256": sha(keys),
            "romfs_sha256": sha(rom), "ui_prompt": prompt.decode()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    staging = sub.add_parser("stage")
    staging.add_argument("--work", type=Path, required=True)
    staging.add_argument("--case", required=True)
    staging.add_argument("--out", type=Path, required=True)
    peer = sub.add_parser("peer")
    peer.add_argument("--stage", type=Path, required=True)
    peer.add_argument("--socket", type=Path, required=True)
    peer.add_argument("--out", type=Path, required=True)
    peer.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args()
    if args.action == "stage":
        stage(args.work, args.case, args.out)
        print(args.out / "stage.json")
        return
    args.out.mkdir(parents=True, exist_ok=False)
    staged = json.loads(args.stage.read_text())
    outcome = {"verdict": "FAIL", "case": staged["case"]}
    try:
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(args.socket))
            try:
                listener.listen(1)
                listener.settimeout(args.timeout)
                client, _ = listener.accept()
                with client:
                    outcome = serve(client, staged, args.out / "uart.bin", args.timeout)
            finally:
                args.socket.unlink(missing_ok=True)
    except Exception as exc:
        outcome["error"] = str(exc)
        raise
    finally:
        (args.out / "result.json").write_text(json.dumps(outcome, indent=2) + "\n")


if __name__ == "__main__":
    main()
