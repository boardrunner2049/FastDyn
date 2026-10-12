"""Compile device models into shared libraries (``fastdyn build-model``).

One ``.c`` file is one model, named after the file (``usart2.c`` →
``usart2.so``); a model made of several files is given a name with
:class:`Model`. FastDyn adds its own headers (``<fastdyn/device_model.h>``, which
includes QEMU's ``<qemu/qemu-plugin.h>``), LibVIO when ``pkg-config vio`` finds it,
and the model's own folder and ``include/`` directories. Builds run in
parallel and are incremental: a model is rebuilt only when a source or
header it uses, or the compile command, changed. Nothing is written outside
the output directory. A model that fails to build has no output (an older
build is removed), so a stale library is never loaded by mistake.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Sequence, Union

from . import paths

log = logging.getLogger(__name__)

BASE_FLAGS = ("-shared", "-fPIC", "-g", "-Werror=incompatible-pointer-types")
STATE_DIR = ".fastdyn-build"  # depfiles and stamps, inside the output directory
QEMU_INCLUDE_RELATIVE = "third_party/common/qemu/include"

BUILT, UP_TO_DATE, FAILED, SKIPPED = "built", "up-to-date", "failed", "skipped"

_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")


class ModelBuildError(Exception):
    """The request cannot be built at all (bad input or missing toolchain)."""


@dataclass(frozen=True)
class Model:
    """A model to build: its name (the output is ``<name>.so``) and its sources."""

    name: str
    sources: tuple[Path, ...]

    def __init__(self, name: str, sources: Iterable[Union[str, Path]]):
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "sources", tuple(Path(s) for s in sources))


@dataclass
class ModelResult:
    """What happened to one model."""

    name: str
    sources: list[str]
    output: str
    status: str
    diagnostics: str = ""
    command: list[str] = field(default_factory=list)

    @property
    def first_error(self) -> str:
        lines = [line for line in self.diagnostics.splitlines() if line.strip()]
        errors = [line for line in lines if "error" in line]
        return (errors or lines or [""])[0].strip()


@dataclass
class BuildReport:
    """The result of :func:`build_models`, one entry per model."""

    results: list[ModelResult]
    vio: bool = False

    @property
    def ok(self) -> bool:
        return all(r.status != FAILED for r in self.results)

    def count(self, status: str) -> int:
        return sum(1 for r in self.results if r.status == status)

    def summary(self) -> str:
        parts = [f"built {self.count(BUILT)}", f"up to date {self.count(UP_TO_DATE)}",
                 f"failed {self.count(FAILED)}"]
        if self.count(SKIPPED):
            parts.append(f"skipped {self.count(SKIPPED)}")
        return ", ".join(parts)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "vio": self.vio,
            "counts": {s: self.count(s) for s in (BUILT, UP_TO_DATE, FAILED, SKIPPED)},
            "models": [
                {"name": r.name, "sources": r.sources, "output": r.output, "status": r.status,
                 "diagnostics": r.diagnostics, "command": r.command}
                for r in self.results
            ],
        }


def find_models(
    inputs: Sequence[Union[str, Path]],
    *,
    recursive: bool = False,
    exclude: Iterable[str] = (),
    name: Optional[str] = None,
) -> list[Model]:
    """Turn files and folders into models: one per ``.c`` file, or one model
    called ``name`` made of all the given files."""
    exclude = tuple(exclude)
    if not inputs:
        raise ModelBuildError("no model sources given")
    if name is not None:
        files = [Path(p) for p in inputs]
        not_files = [str(p) for p in files if not p.is_file()]
        if not_files:
            raise ModelBuildError(f"--name needs source files, not folders: {', '.join(not_files)}")
        return [Model(name, files)]

    models = []
    for item in map(Path, inputs):
        if item.is_dir():
            candidates = sorted(item.rglob("*.c") if recursive else item.glob("*.c"))
            for source in candidates:
                relative = source.relative_to(item).as_posix()
                if any(fnmatch.fnmatch(relative, p) or fnmatch.fnmatch(source.name, p) for p in exclude):
                    continue
                models.append(Model(source.stem, [source]))
        elif item.is_file():
            if any(fnmatch.fnmatch(item.name, p) for p in exclude):
                continue
            models.append(Model(item.stem, [item]))
        else:
            raise ModelBuildError(f"no such file or folder: {item}")
    if not models:
        raise ModelBuildError("no .c files found in " + ", ".join(map(str, inputs)))
    return models


def build_models(
    paths_or_models: Sequence[Union[str, Path, Model]],
    out_dir: Union[str, Path],
    *,
    jobs: Optional[int] = None,
    keep_going: bool = False,
    vio: bool = True,
    pkgs: Iterable[str] = (),
    exclude: Iterable[str] = (),
    recursive: bool = False,
) -> BuildReport:
    """Build models into ``out_dir/<name>.so``.

    ``paths_or_models`` mixes source files, folders (their ``.c`` files, or
    the whole tree with ``recursive``) and :class:`Model` objects. Without
    ``keep_going`` no new build starts after the first failure. ``vio=False``
    builds without LibVIO; ``pkgs`` adds pkg-config packages (``"fastdyn"``
    links the fuzzer hooks). Raises :class:`ModelBuildError` when nothing can
    be built (no sources, duplicate names, no compiler, missing headers).
    """
    models = [m for m in paths_or_models if isinstance(m, Model)]
    inputs = [p for p in paths_or_models if not isinstance(p, Model)]
    if inputs:
        models += find_models(inputs, recursive=recursive, exclude=exclude)
    _check_models(models)

    toolchain = _Toolchain.resolve(vio=vio, pkgs=tuple(pkgs))
    out_dir = Path(out_dir).resolve()
    (out_dir / STATE_DIR).mkdir(parents=True, exist_ok=True)

    stop = threading.Event()

    def build(model: Model) -> ModelResult:
        result = _plan(model, out_dir, toolchain)
        if result.status == UP_TO_DATE:
            return result
        if stop.is_set():
            result.status = SKIPPED
            return result
        result = _compile(model, out_dir, toolchain, result)
        if result.status == FAILED and not keep_going:
            stop.set()
        return result

    workers = jobs or os.cpu_count() or 1
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(build, models))
    return BuildReport(results, vio=toolchain.vio)


def _check_models(models: Sequence[Model]) -> None:
    seen: dict[str, Model] = {}
    for model in models:
        if not _NAME_RE.match(model.name):
            raise ModelBuildError(f"invalid model name {model.name!r}")
        if not model.sources:
            raise ModelBuildError(f"model {model.name!r} has no sources")
        missing = [str(s) for s in model.sources if not s.is_file()]
        if missing:
            raise ModelBuildError(f"model {model.name!r}: missing source {', '.join(missing)}")
        if model.name in seen:
            other = seen[model.name]
            raise ModelBuildError(
                f"two models are called {model.name!r} ({other.sources[0]} and {model.sources[0]}); "
                "build them separately or give one a --name"
            )
        seen[model.name] = model


@dataclass(frozen=True)
class _Toolchain:
    compiler: list[str]
    cflags: list[str]
    libs: list[str]
    env_cflags: list[str]
    env_ldflags: list[str]
    vio: bool

    @classmethod
    def resolve(cls, *, vio: bool, pkgs: tuple[str, ...]) -> "_Toolchain":
        compiler = shlex.split(os.environ.get("CC") or "cc")
        if not compiler or shutil.which(compiler[0]) is None:
            raise ModelBuildError(f"C compiler not found: {compiler[0] if compiler else '(empty $CC)'}")

        cflags = _fastdyn_cflags()
        libs: list[str] = []
        use_vio = False
        if vio:
            if _pkg_config_exists("vio"):
                cflags += _pkg_config("vio", "--cflags")
                libs += _pkg_config("vio", "--libs") + _rpath("vio")
                use_vio = True
            else:
                log.debug("LibVIO not found by pkg-config (vio); building without it")
        for pkg in pkgs:
            if pkg == "fastdyn" and paths.repo_root() is not None:
                library = Path(paths.default_plugin_library())
                if not library.is_file():
                    raise ModelBuildError(f"--pkg fastdyn: {library} not found; build FastDyn first")
                libs += [f"-L{library.parent}", "-lfastdyn", f"-Wl,-rpath,{library.parent}"]
            elif _pkg_config_exists(pkg):
                cflags += _pkg_config(pkg, "--cflags")
                libs += _pkg_config(pkg, "--libs") + _rpath(pkg)
            else:
                raise ModelBuildError(f"--pkg {pkg}: not found by pkg-config")

        return cls(compiler, cflags, libs,
                   shlex.split(os.environ.get("CFLAGS", "")),
                   shlex.split(os.environ.get("LDFLAGS", "")), use_vio)


def _fastdyn_cflags() -> list[str]:
    """FastDyn's headers: from the checkout, else from an installed fastdyn.pc."""
    root = paths.repo_root()
    if root is not None and (root / "include" / "fastdyn" / "device_model.h").is_file():
        flags = [f"-I{root / 'include'}"]
        qemu_include = root / QEMU_INCLUDE_RELATIVE
        if qemu_include.is_dir():
            flags.append(f"-I{qemu_include}")
        if _pkg_config_exists("glib-2.0"):  # qemu-plugin.h includes <glib.h>
            flags += _pkg_config("glib-2.0", "--cflags")
        return flags
    if _pkg_config_exists("fastdyn"):
        return _pkg_config("fastdyn", "--cflags")
    raise ModelBuildError(
        "FastDyn headers not found: run from a FastDyn checkout, or install FastDyn "
        "(meson install) and add its pkgconfig directory to PKG_CONFIG_PATH"
    )


def _pkg_config_exists(package: str) -> bool:
    if shutil.which("pkg-config") is None:
        return False
    return subprocess.run(["pkg-config", "--exists", package], capture_output=True).returncode == 0


def _pkg_config(package: str, *args: str) -> list[str]:
    result = subprocess.run(["pkg-config", *args, package], capture_output=True, text=True, check=True)
    return shlex.split(result.stdout)


def _rpath(package: str) -> list[str]:
    """Let the model find the package's libraries at load time."""
    libdir = _pkg_config(package, "--variable=libdir")
    return [f"-Wl,-rpath,{os.path.normpath(libdir[0])}"] if libdir else []


def _include_dirs(model: Model) -> list[str]:
    flags: list[str] = []
    for folder in dict.fromkeys(s.resolve().parent for s in model.sources):
        for candidate in (folder, folder / "include", folder.parent / "include"):
            flag = f"-I{candidate}"
            if candidate.is_dir() and flag not in flags:
                flags.append(flag)
    return flags


def _state_file(out_dir: Path, model: Model, suffix: str) -> Path:
    return out_dir / STATE_DIR / f"{model.name}{suffix}"


def _command(model: Model, toolchain: _Toolchain, output: Path, depfile: Path) -> list[str]:
    return [
        *toolchain.compiler, *BASE_FLAGS, *toolchain.env_cflags, *_include_dirs(model),
        *toolchain.cflags, "-MMD", "-MF", str(depfile), "-o", str(output),
        *(str(s.resolve()) for s in model.sources), *toolchain.libs, *toolchain.env_ldflags,
    ]


def _plan(model: Model, out_dir: Path, toolchain: _Toolchain) -> ModelResult:
    """Work out the command, and whether the existing output is up to date."""
    output = out_dir / f"{model.name}.so"
    depfile = _state_file(out_dir, model, ".d")
    command = _command(model, toolchain, _state_file(out_dir, model, ".so.tmp"), depfile)
    result = ModelResult(model.name, [str(s) for s in model.sources], str(output), BUILT, command=command)
    if _up_to_date(output, depfile, _state_file(out_dir, model, ".stamp"), _command_hash(command)):
        result.status = UP_TO_DATE
    return result


def _command_hash(command: list[str]) -> str:
    return hashlib.sha256("\0".join(command).encode()).hexdigest()


def _up_to_date(output: Path, depfile: Path, stamp: Path, command_hash: str) -> bool:
    try:
        if json.loads(stamp.read_text()).get("command") != command_hash:
            return False
        # File times advance in coarse steps, so a source saved right after a
        # build can carry the library's own time: equal counts as changed.
        built = output.stat().st_mtime_ns
        return all(Path(dep).stat().st_mtime_ns < built for dep in _read_depfile(depfile))
    except (OSError, ValueError):
        return False


def _read_depfile(depfile: Path) -> list[str]:
    """Dependencies listed in a compiler ``-MMD`` file (make syntax)."""
    text = depfile.read_text().replace("\\\n", " ")
    _, _, deps = text.partition(":")
    words = re.split(r"(?<!\\)\s+", deps.strip())
    return [w.replace("\\ ", " ") for w in words if w]


def _compile(model: Model, out_dir: Path, toolchain: _Toolchain, result: ModelResult) -> ModelResult:
    temp = _state_file(out_dir, model, ".so.tmp")
    stamp = _state_file(out_dir, model, ".stamp")
    stamp.unlink(missing_ok=True)
    log.debug("building %s: %s", model.name, shlex.join(result.command))
    proc = subprocess.run(result.command, capture_output=True, text=True)
    result.diagnostics = (proc.stdout + proc.stderr).strip()
    if proc.returncode != 0 and not toolchain.vio and "vio/" in result.diagnostics:
        result.diagnostics += ("\nnote: LibVIO was not found by pkg-config (vio); install it or "
                               "add its pkgconfig directory to PKG_CONFIG_PATH")
    if proc.returncode != 0:
        temp.unlink(missing_ok=True)
        # Never leave an older build behind for the emulator to load.
        Path(result.output).unlink(missing_ok=True)
        result.status = FAILED
        log.debug("%s failed:\n%s", model.name, result.diagnostics)
        return result
    os.replace(temp, result.output)
    stamp.write_text(json.dumps({"command": _command_hash(result.command)}))
    result.status = BUILT
    return result
