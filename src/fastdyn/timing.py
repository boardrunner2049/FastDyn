"""Lightweight JSONL phase timing for FastDyn launches."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys
import time
from typing import Iterator
import logging

log = logging.getLogger(__name__)

_FALSE_VALUES = {"0", "false", "off", "no", "disabled"}


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in _FALSE_VALUES


def enabled() -> bool:
    return _env_bool("FASTDYN_TIMING", True)


def echo_enabled() -> bool:
    return _env_bool("FASTDYN_TIMING_ECHO", True)


def timing_file() -> Path | None:
    raw = os.environ.get("FASTDYN_TIMING_FILE")
    if not raw:
        return None
    return Path(raw).expanduser()


def process_name() -> str:
    return os.environ.get("FASTDYN_TIMING_PROCESS") or Path(sys.argv[0] or "python").name


def configure(
    *,
    file: str | Path | None = None,
    enabled_value: bool | None = None,
    echo_value: bool | None = None,
    process: str | None = None,
) -> None:
    if file is not None:
        path = Path(file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        os.environ["FASTDYN_TIMING_FILE"] = str(path)
    if enabled_value is not None:
        os.environ["FASTDYN_TIMING"] = "1" if enabled_value else "0"
    if echo_value is not None:
        os.environ["FASTDYN_TIMING_ECHO"] = "1" if echo_value else "0"
    if process:
        os.environ["FASTDYN_TIMING_PROCESS"] = process


def _json_safe(value: object) -> object:
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def _write(record: dict[str, object]) -> None:
    if not enabled():
        return

    path = timing_file()
    if path is None:
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({key: _json_safe(value) for key, value in record.items()}, sort_keys=True)
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        os.write(fd, (payload + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def mark(phase: str, *, echo: bool = False, **fields: object) -> None:
    if not enabled():
        return

    record: dict[str, object] = {
        "event": "mark",
        "phase": phase,
        "process": process_name(),
        "pid": os.getpid(),
        "time_monotonic_s": time.monotonic(),
        "time_wall_s": time.time(),
    }
    record.update(fields)
    _write(record)

    if echo and echo_enabled():
        suffix = " ".join(f"{key}={value}" for key, value in fields.items())
        log.info(f"[timing] {process_name()}:{phase}" + (f" {suffix}" if suffix else ""))


@contextmanager
def phase(name: str, *, echo: bool = True, **fields: object) -> Iterator[None]:
    if not enabled():
        yield
        return

    start_monotonic = time.monotonic()
    start_perf = time.perf_counter()
    start_record: dict[str, object] = {
        "event": "phase_start",
        "phase": name,
        "process": process_name(),
        "pid": os.getpid(),
        "time_monotonic_s": start_monotonic,
        "time_wall_s": time.time(),
    }
    start_record.update(fields)
    _write(start_record)

    status = "ok"
    error: BaseException | None = None
    try:
        yield
    except BaseException as exc:
        status = "error"
        error = exc
        raise
    finally:
        duration_s = time.perf_counter() - start_perf
        end_record: dict[str, object] = {
            "event": "phase_end",
            "phase": name,
            "process": process_name(),
            "pid": os.getpid(),
            "time_monotonic_s": time.monotonic(),
            "time_wall_s": time.time(),
            "duration_s": duration_s,
            "status": status,
        }
        end_record.update(fields)
        if error is not None:
            end_record["error_type"] = type(error).__name__
            end_record["error"] = str(error)
        _write(end_record)

        if echo and echo_enabled():
            log.info(f"[timing] {process_name()}:{name} {duration_s:.3f}s {status}")
