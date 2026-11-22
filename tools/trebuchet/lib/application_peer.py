"""Lifecycle for a pinned application peer serving a native QEMU Unix serial endpoint."""
from contextlib import contextmanager
import fcntl
import json
from pathlib import Path
import subprocess
import sys
import time

from scopt.run.launcher import file_sha256


def pinned(spec):
    path = Path(spec["path"]).resolve(strict=True)
    if file_sha256(path) != spec["sha256"]:
        raise ValueError(f"peer source changed: {path}")
    return path


def uart_peer(spec):
    """The same case/peer contract applies during capture, profile and timed replay."""
    @contextmanager
    def start(rd, inputs, run_json, timeout):
        program = pinned(spec["program"])
        stage_path = pinned(spec["stage"])
        stage = json.loads(stage_path.read_text())
        if file_sha256(inputs[spec["bank"]]) != spec["input_sha256"]:
            raise ValueError("peer and firmware use different scenario input")
        serial = rd.config()["Machine"]["serial"]
        if not serial.startswith("unix:/") or "," in serial:
            raise ValueError("peer requires a native Unix client serial endpoint")
        endpoint = Path(serial.removeprefix("unix:"))
        dest = rd.root / (Path(run_json).stem + "-peer")
        if dest.exists():
            raise ValueError(f"existing peer evidence: {dest}")
        with endpoint.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if endpoint.exists():
                raise ValueError(f"serial endpoint in use: {endpoint}")
            argv = [sys.executable, str(program), "peer", "--socket", str(endpoint),
                    "--stage", str(stage_path), "--out", str(dest), "--timeout", str(timeout)]
            with (rd.root / (dest.name + ".log")).open("x") as peer_log:
                proc = subprocess.Popen(argv, stdout=peer_log, stderr=subprocess.STDOUT)
                socket_inode = None
                try:
                    deadline = time.monotonic() + min(15, timeout)
                    while not endpoint.is_socket():
                        if proc.poll() is not None or time.monotonic() >= deadline:
                            raise RuntimeError(f"UART peer failed to listen; see {peer_log.name}")
                        time.sleep(.01)
                    socket_inode = endpoint.stat().st_ino

                    def collect(doc):
                        if doc["exit_code"] != 0 or doc["timed_out"]:
                            raise RuntimeError(f"QEMU launch failed: {rd.root / run_json}")
                        if proc.wait(timeout=timeout) != 0:
                            raise RuntimeError(f"UART peer failed: {peer_log.name}")
                        result_path = dest / "result.json"
                        result = json.loads(result_path.read_text())
                        if result["verdict"] != "PASS" or result["case"] != stage["case"]:
                            raise ValueError(f"application oracle failed: {result_path}")
                        pinned(spec["program"])
                        pinned(spec["stage"])
                        return {"spec": spec, "receipt": str(result_path),
                                "receipt_sha256": file_sha256(result_path), "result": result}

                    yield collect
                finally:
                    if proc.poll() is None:
                        proc.terminate()
                        try:
                            proc.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            proc.wait()
                    if socket_inode is not None and endpoint.is_socket() and endpoint.stat().st_ino == socket_inode:
                        endpoint.unlink()
    return start
