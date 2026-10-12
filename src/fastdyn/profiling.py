"""Optional profiler wrappers for FastDyn runtime processes."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Sequence
import logging

log = logging.getLogger(__name__)


_FALSE_VALUES = {"", "0", "false", "off", "no", "none", "disabled"}
_TRUE_VALUES = {"1", "true", "on", "yes", "enabled"}


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in _FALSE_VALUES


def _safe_name(value: str) -> str:
    value = value.strip() or "process"
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._") or "process"


def profile_dir(work_dir: str | Path | None = None) -> Path:
    root = Path(work_dir or os.environ.get("FASTDYN_WORK_DIR") or "fastdyn_work").expanduser()
    out = root / "profiles"
    out.mkdir(parents=True, exist_ok=True)
    return out


def python_profile_enabled() -> bool:
    return _env_bool("FASTDYN_PYTHON_PROFILE", False)


def perf_mode() -> str:
    raw = os.environ.get("FASTDYN_PERF_MODE") or os.environ.get("FASTDYN_PERF") or ""
    mode = raw.strip().lower()
    if mode in _FALSE_VALUES:
        return "off"
    if mode in _TRUE_VALUES:
        return "stat"
    if mode in {"stat", "record"}:
        return mode
    log.error(f"unknown FASTDYN_PERF_MODE={raw!r}; perf disabled")
    return "off"


def _perf_usable() -> bool:
    perf = shutil.which("perf")
    if perf is None:
        log.error("[profile] perf not found; QEMU perf profiling disabled")
        return False

    try:
        result = subprocess.run(
            [perf, "stat", "-o", os.devnull, "--", "true"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5.0,
            check=False,
        )
    except Exception as exc:
        log.error(f"[profile] perf probe failed ({exc}); QEMU perf profiling disabled")
        return False

    if result.returncode != 0:
        log.error("[profile] perf is not permitted on this host; QEMU perf profiling disabled")
        return False
    return True


def wrap_perf_command(command: Sequence[str], *, name: str, work_dir: str | Path | None = None) -> list[str]:
    mode = perf_mode()
    if mode == "off":
        return list(command)
    if not _perf_usable():
        return list(command)

    out_dir = profile_dir(work_dir)
    safe = _safe_name(name)
    events = (os.environ.get("FASTDYN_PERF_EVENTS") or "").strip()

    if mode == "record":
        out_file = out_dir / f"{safe}.perf.data"
        freq = (os.environ.get("FASTDYN_PERF_FREQ_HZ") or "99").strip()
        wrapped = ["perf", "record", "-F", freq, "-g", "-o", str(out_file)]
        if events:
            wrapped.extend(["-e", events])
        wrapped.extend(["--", *command])
        log.info(
            f"[profile] QEMU perf record: {out_file} "
            f"(inspect with: perf report -i {out_file})",
        )
        return wrapped

    out_file = out_dir / f"{safe}.perf-stat.txt"
    wrapped = ["perf", "stat", "-d", "-o", str(out_file)]
    if events:
        wrapped.extend(["-e", events])
    wrapped.extend(["--", *command])
    log.info(f"[profile] QEMU perf stat: {out_file}")
    return wrapped


def _looks_like_python(executable: str) -> bool:
    name = Path(executable).name.lower()
    return name.startswith("python")


def _resolve_script(command0: str, cwd: Path) -> Path | None:
    candidate = Path(command0).expanduser()
    if not candidate.is_absolute():
        local = cwd / candidate
        if local.exists():
            candidate = local
        else:
            found = shutil.which(command0)
            if found:
                candidate = Path(found)
    if candidate.exists() and candidate.name.endswith(".py"):
        return candidate
    return None


def wrap_python_profile_command(
    command: str | Sequence[str],
    *,
    name: str,
    cwd: str | Path,
    work_dir: str | Path | None = None,
    shell: bool = False,
) -> tuple[str | list[str], bool]:
    if not python_profile_enabled() or shell or isinstance(command, str):
        return command, shell

    cmd = list(command)
    if not cmd:
        return cmd, shell

    script_idx: int | None = None
    python_exe = sys.executable
    if _looks_like_python(cmd[0]):
        python_exe = cmd[0]
        for idx, part in enumerate(cmd[1:], start=1):
            if part.endswith(".py"):
                script_idx = idx
                break
        if script_idx is None:
            return cmd, shell
    else:
        script = _resolve_script(cmd[0], Path(cwd).expanduser())
        if script is None:
            return cmd, shell
        cmd[0] = str(script)
        script_idx = 0

    out_file = profile_dir(work_dir) / f"{_safe_name(name)}.cprofile"
    profiled = [
        python_exe,
        "-m",
        "cProfile",
        "-o",
        str(out_file),
        *cmd[script_idx:],
    ]
    log.info(
        f"[profile] Python cProfile for '{name}': {out_file} "
        f"(inspect with: python3 -m pstats {out_file})"
    )
    return profiled, False
