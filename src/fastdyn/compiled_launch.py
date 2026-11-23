"""Compile a FastDyn TOML configuration into a direct, shell-launched QEMU bundle.

Python is used once while producing the bundle.  The emitted ``run.sh`` does
not import or invoke FastDyn: it applies the small amount of per-launch setup
that was frozen here and then replaces itself with QEMU.
"""

from __future__ import annotations

import errno
import json
import os
import platform
import re
import shlex
import shutil
from pathlib import Path
from typing import Iterable, Mapping, Sequence


SCHEMA = "fastdyn.compiled-launch/1"
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class CompiledLaunchError(RuntimeError):
    """The configuration cannot be represented by the direct launcher."""


def _sparse_copy(source: Path, destination: Path) -> None:
    """Copy a potentially multi-gigabyte cache/RAM file without filling holes."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    size = source.stat().st_size
    with source.open("rb", buffering=0) as src, destination.open("wb", buffering=0) as dst:
        os.ftruncate(dst.fileno(), size)
        position = 0
        try:
            while position < size:
                try:
                    data = os.lseek(src.fileno(), position, os.SEEK_DATA)
                except OSError as exc:
                    if exc.errno == errno.ENXIO:
                        break
                    raise
                hole = os.lseek(src.fileno(), data, os.SEEK_HOLE)
                os.lseek(src.fileno(), data, os.SEEK_SET)
                os.lseek(dst.fileno(), data, os.SEEK_SET)
                remaining = hole - data
                while remaining:
                    chunk = os.read(src.fileno(), min(remaining, 1024 * 1024))
                    if not chunk:
                        raise OSError(f"short read while copying {source}")
                    view = memoryview(chunk)
                    while view:
                        written = os.write(dst.fileno(), view)
                        if written <= 0:
                            raise OSError(f"short write while copying {source}")
                        view = view[written:]
                    remaining -= len(chunk)
                position = hole
        except OSError as exc:
            unsupported = {errno.EINVAL, getattr(errno, "ENOTSUP", errno.EINVAL)}
            if exc.errno not in unsupported:
                raise
            src.seek(0)
            dst.seek(0)
            shutil.copyfileobj(src, dst, 1024 * 1024)
            dst.truncate(size)


def _stage_runtime_files(machine, out: Path) -> bool:
    """Move writable cache/RAM state into the bundle; return whether a cache existed."""
    from .targets import qemu_target

    for memory in (machine.memories or {}).values():
        if not qemu_target._memory_backend_is_file(memory):
            continue
        original = Path(memory.memory_file).expanduser().resolve() if memory.memory_file else None
        staged = out / "ram" / f"{memory.memory_id}.ram"
        if original is not None and original.is_file():
            _sparse_copy(original, staged)
        memory.memory_file = str(staged)

    cache_name = machine.qemu_target_opts.supercache
    if not cache_name:
        return False
    original_cache = Path(cache_name).expanduser().resolve()
    staged_cache = out / "cache" / original_cache.name
    existed = original_cache.is_file()
    if existed:
        _sparse_copy(original_cache, staged_cache)
        original_manifest = Path(str(original_cache) + ".fastdyn.json")
        if original_manifest.is_file():
            shutil.copy2(original_manifest, Path(str(staged_cache) + ".fastdyn.json"))
    machine.qemu_target_opts.supercache = str(staged_cache)
    return existed


def _q(value: object) -> str:
    return shlex.quote(os.fspath(value))


def _qmp_sockets(argv: Sequence[str]) -> list[str]:
    sockets: list[str] = []
    for index, arg in enumerate(argv[:-1]):
        if arg != "-qmp":
            continue
        spec = argv[index + 1]
        if spec.startswith("unix:"):
            path = spec[5:].split(",", 1)[0]
            if path:
                sockets.append(path)
    return sockets


def emit_bundle(
    out_dir: str | os.PathLike,
    qemu_argv: Sequence[str],
    cwd: str | os.PathLike,
    env_set: Mapping[str, str],
    reset_files: Iterable[tuple[str | os.PathLike, int]] = (),
    *,
    metadata: Mapping[str, object] | None = None,
    allow_existing: bool = False,
    disable_aslr: bool = False,
) -> Path:
    """Write ``launch.json`` and an executable, direct ``run.sh``.

    ``reset_files`` contains ``(path, byte_size)`` pairs.  They are recreated
    as sparse files immediately before QEMU starts.  The generated script uses
    only POSIX shell builtins plus ``mkdir``, ``rm``, ``truncate`` and, for a
    fixed-address cache, ``setarch``.  It ends in ``exec QEMU ...``.  Additional
    script arguments are appended to QEMU's frozen argument vector.
    """

    if not qemu_argv:
        raise CompiledLaunchError("QEMU argument vector is empty")
    if not all(isinstance(arg, str) and "\0" not in arg for arg in qemu_argv):
        raise CompiledLaunchError("QEMU arguments must be NUL-free strings")
    bad_env = [name for name in env_set if not _ENV_NAME.fullmatch(name)]
    if bad_env:
        raise CompiledLaunchError(f"invalid environment variable name: {bad_env[0]!r}")

    out = Path(out_dir).expanduser().resolve()
    if out.exists() and any(out.iterdir()) and not allow_existing:
        raise CompiledLaunchError(f"output directory is not empty: {out}")
    out.mkdir(parents=True, exist_ok=True)

    resets = [(str(Path(path).expanduser().resolve()), int(size)) for path, size in reset_files]
    if any(size < 0 for _path, size in resets):
        raise CompiledLaunchError("RAM reset sizes must be non-negative")
    env = {str(name): str(value) for name, value in sorted(env_set.items())}
    launch = {
        "schema": SCHEMA,
        "argv": list(qemu_argv),
        "cwd": str(Path(cwd).expanduser().resolve()),
        "env_set": env,
        "reset_files": [{"path": path, "size": size} for path, size in resets],
        "qmp_sockets": _qmp_sockets(qemu_argv),
        "runtime": "direct-shell-exec",
        "disable_aslr": bool(disable_aslr),
        "metadata": dict(metadata or {}),
    }
    (out / "launch.json").write_text(
        json.dumps(launch, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    lines = ["#!/bin/sh", "set -eu", ""]
    for path, size in resets:
        lines.extend(
            [
                f"mkdir -p {_q(Path(path).parent)}",
                f": > {_q(path)}",
                f"truncate -s {size} {_q(path)}",
            ]
        )
    for socket in launch["qmp_sockets"]:
        lines.append(f"rm -f {_q(socket)}")
    lines.append(f"cd {_q(launch['cwd'])}")
    for name, value in env.items():
        lines.append(f"export {name}={_q(value)}")
    command = " ".join(_q(arg) for arg in qemu_argv)
    if disable_aslr:
        command = f"setarch {_q(platform.machine())} -R {command}"
    lines.extend(["", f"exec {command} \"$@\"", ""])

    script = out / "run.sh"
    script.write_text("\n".join(lines), encoding="utf-8")
    script.chmod(0o755)
    return script


def _file_ram_resets(machine, force: bool | None) -> list[tuple[str, int]]:
    from .targets import qemu_target

    enabled = bool(machine.qemu_target_opts.reset_memory_files) if force is None else force
    if not enabled:
        return []
    resets = []
    for memory in (machine.memories or {}).values():
        if not qemu_target._memory_backend_is_file(memory):
            continue
        if not memory.memory_file:
            continue
        resets.append((memory.memory_file, qemu_target._parse_memory_size(memory.memory_size)))
    return resets


def compile_config(
    config: str | os.PathLike,
    out_dir: str | os.PathLike,
    *,
    svd: str | os.PathLike | None = None,
    fmu: str | None = None,
    build_fmu: bool = True,
    reset_memory: bool | None = None,
) -> Path:
    """Compile ``config`` and return the emitted ``run.sh`` path.

    The first version intentionally accepts the strict direct-execution
    subset: one QEMU machine, no configured helper processes, no device Python
    endpoints and no automatic GDB terminal.  These cases need a process graph,
    not a supposedly direct QEMU script, and are refused with a concrete reason.
    """

    from . import paths, runner, runtime_config, toml_parser
    from .fastdyn import _python_endpoints_for_machine, compile_device_routing
    from .targets import qemu_target
    from .virtual_preprocessing import prepare_run_preprocessors

    config_path = Path(config).expanduser().resolve()
    out = Path(out_dir).expanduser().resolve()
    if out.exists() and any(out.iterdir()):
        raise CompiledLaunchError(f"output directory is not empty: {out}")
    out.mkdir(parents=True, exist_ok=True)
    assets = out / "assets"
    assets.mkdir()
    machine = None

    try:
        runtime_config.validate_config(config_path)
        processes = runtime_config.load_processes(config_path)
        if processes:
            names = ", ".join(process.name for process in processes)
            raise CompiledLaunchError(
                f"direct launch does not support configured helper processes: {names}"
            )
        runner.auto_build_fmu(config_path, fmu, not build_fmu)
        runtime_env = runtime_config.configure_run_environment(config_path, assets)
        if runtime_env.get("FASTDYN_PERF_MODE", "off") != "off":
            raise CompiledLaunchError("direct launch does not support FastDyn perf wrapping")
        if runtime_env.get("FASTDYN_PYTHON_PROFILE") == "1":
            raise CompiledLaunchError("direct launch does not support Python profiling")

        with runtime_config.bridge_boot_environment(config_path):
            handle = toml_parser.parser(
                str(assets),
                machine_name="machine0",
                toml_config=str(config_path),
                svd_path=os.fspath(svd) if svd is not None else paths.default_svd_dir(),
                fmu_name=fmu,
            )
            if len(handle.machines) != 1:
                raise CompiledLaunchError(
                    f"direct launch requires exactly one machine, got {len(handle.machines)}"
                )
            machine_name, machine = next(iter(handle.machines.items()))
            endpoints = _python_endpoints_for_machine(machine)
            if endpoints:
                names = ", ".join(path for path, _device in endpoints)
                raise CompiledLaunchError(
                    f"direct launch does not support Python device endpoints: {names}"
                )
            if machine.qemu_target_opts.launch_gdb:
                raise CompiledLaunchError("direct launch cannot open an automatic GDB terminal")

            source_cache_existed = _stage_runtime_files(machine, out)
            if machine.qemu_target_opts.qmp_socket:
                machine.qemu_target_opts.qmp_socket = str(out / "qmp.sock")

            prepare_run_preprocessors(machine, str(assets))
            compile_device_routing(
                machine.devices, machine.parsed_device, models=machine.models
            )
            qemu_argv, _gdb_cmd, _launch_gdb, _binary = qemu_target.setup_qemu(
                machine, str(assets)
            )
            staged_cache = machine.qemu_target_opts.supercache
            if source_cache_existed and (not staged_cache or not Path(staged_cache).is_file()):
                raise CompiledLaunchError(
                    "the frozen FastDyn configuration does not match the supplied SuperCache; "
                    "compile with the same logging/instrumentation settings used to capture it"
                )
            cleanup = list(getattr(machine, "preprocessing_cleanup", None) or ())
            # Every preparation owns one analysis-session pool, even when no
            # preprocessor needs a live run-time resource.  It is safe and
            # necessary to close that pool now.  Additional callbacks belong
            # to extensions whose resources normally remain live through the
            # QEMU process and cannot yet be frozen into a shell bundle.
            if len(cleanup) > 1:
                raise CompiledLaunchError(
                    "direct launch does not support preprocessors with live cleanup callbacks"
                )
            for close in reversed(cleanup):
                close()
            machine.preprocessing_cleanup = []

            qemu_env = qemu_target._build_qemu_env(qemu_argv)
            # Preserve the effective FastDyn run environment that native
            # plugins and callbacks may consume.  These values point inside
            # the bundle, not at an ephemeral ordinary-run directory.
            env_set: dict[str, str] = {
                str(name): str(value) for name, value in runtime_env.items()
            }
            if qemu_env.get("LD_LIBRARY_PATH"):
                env_set["LD_LIBRARY_PATH"] = qemu_env["LD_LIBRARY_PATH"]
            bridge = os.environ.get("FASTDYN_BRIDGE_BOOT")
            if bridge is not None:
                env_set["FASTDYN_BRIDGE_BOOT"] = bridge

            metadata = {
                "config": str(config_path),
                "machine": machine_name,
                "preparation": "FastDyn configuration compiled once; run.sh directly execs QEMU",
                "runtime_hash_checks": False,
            }
            return emit_bundle(
                out,
                qemu_argv,
                Path.cwd(),
                env_set,
                _file_ram_resets(machine, reset_memory),
                metadata=metadata,
                allow_existing=True,
                disable_aslr=bool(staged_cache),
            )
    except Exception:
        # Preparation may fail after allocating analysis sessions or another
        # preprocessor resource.  Direct compilation never starts QEMU, so all
        # registered cleanup callbacks must be released on this path.
        cleanup = list(getattr(machine, "preprocessing_cleanup", None) or ())
        for close in reversed(cleanup):
            try:
                close()
            except Exception:
                pass
        if machine is not None:
            machine.preprocessing_cleanup = []
        shutil.rmtree(out, ignore_errors=True)
        raise
