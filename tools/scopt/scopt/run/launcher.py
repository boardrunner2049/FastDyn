"""Run directories and the FastDyn launcher (§4.8).

Every QEMU run gets a fresh run directory::

    <root>/run.toml        base TOML with only the path keys rewritten (toml_rewrite)
    <root>/rundir.json     how the directory was made (base TOML, log mode, QEMU, cache name)
    <root>/cache/          <name>.sc and <name>.sc.fastdyn.json
    <root>/work/           FastDyn's -o work directory
    <root>/ram/            file-backed guest RAM, deleted and recreated before each run
    <root>/qmp.sock
    <root>/oracle.json     deploy oracle record (QEMU argv + env, fingerprint)  [deploy.py]
    <root>/deploy.json     what was deployed (header, record digest, manifest) [deploy.py]
    <root>/stdout.log      FastDyn+QEMU stdout and stderr, one stream
    <root>/output.norm     B0-normalized log (see :func:`norm_b0`)
    <root>/guest.norm      guest-visible lines only (see :func:`norm_guest`)
    <root>/run.json        argv, env diff, timings, BENCH, proof verdict, appended count

Launch rules: ``setarch x86_64 -R`` (QEMU refuses the supercache otherwise),
``cwd=src/fastdyn`` (the plugin path in the TOML is cwd-relative), the same
``-q`` choice as the deploy oracle (``log_level`` is in the fingerprint), a
``timeout`` wrapper plus a process-group kill as a backstop, and an
environment scrubbed of ``QEMU_SC_*`` and of every ``FASTDYN_*`` variable that
FastDyn reads as an override (``FASTDYN_QEMU`` is kept or set explicitly).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import shutil
import subprocess
import threading
import time
import tomllib
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..paths import fastdyn_root, fastdyn_src
from .toml_rewrite import memory_banks, rewrite_toml

__all__ = [
    "FASTDYN_ROOT", "FASTDYN_SRC", "FASTDYN_PY", "FASTDYN_BIN", "B0_QEMU", "FDSHIM",
    "RunDir", "RunDirError", "create_run_dir", "scrub_env", "reset_ram", "fastdyn_args",
    "launch", "norm_b0", "norm_guest", "parse_bench", "file_sha256", "write_json",
    "stray_capture_files", "qmp_socket_path", "load_inputs", "FASTDYN_DEFAULT_QEMU",
    "resolve_qemu",
]

#: workspace and FastDyn checkout: ``$FASTDYN_ROOT`` / ``$SCOPT_FASTDYN_SRC`` or the
#: repository-relative defaults of :mod:`scopt.paths`
FASTDYN_ROOT = fastdyn_root()
FASTDYN_SRC = fastdyn_src()
FASTDYN_PY = FASTDYN_SRC / "fastdyn-env/bin/python"
FASTDYN_BIN = FASTDYN_SRC / "fastdyn-env/bin/fastdyn"
B0_QEMU = FASTDYN_ROOT / "src/qemu/build/qemu-system-arm"
FDSHIM = Path(__file__).resolve().parent / "fdshim"
#: ``qemu=FASTDYN_DEFAULT_QEMU`` opts out of the integrated-runtime default: no
#: ``FASTDYN_QEMU`` is exported and FastDyn picks its own binary (the B0 build).
#: Only for B0-binary comparisons (T-B1-0 passes the B0 path explicitly anyway).
FASTDYN_DEFAULT_QEMU = "fastdyn-default"

RUNDIR_SCHEMA = "scopt.rundir/1"
RUN_SCHEMA = "scopt.run/1"
DEFAULT_TIMEOUT_S = 120.0

# FastDyn reads these as run-time overrides (toml_parser.py:110-125, 300-352;
# qemu_target.py:614, 871; profiling.py:44; runtime_config.py). They are removed
# unless a campaign fixes them explicitly via ``extra_env``.
_SCRUB_PREFIXES = ("QEMU_SC_", "FASTDYN_")
_KEEP = ("FASTDYN_QEMU", "FASTDYN_ROOT")


class RunDirError(RuntimeError):
    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def write_json(path: str | os.PathLike, obj: Any) -> None:
    """Pretty JSON, written atomically (tmp + rename)."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


_SHA_CACHE: dict[tuple, str] = {}


def file_sha256(path: str | os.PathLike) -> str:
    """``sha256:`` of a regular file's bytes, memoized on (path, size, mtime_ns, ino)."""
    p = os.path.realpath(path)
    st = os.stat(p)
    key = (p, st.st_size, st.st_mtime_ns, st.st_ino)
    if key not in _SHA_CACHE:
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for b in iter(lambda: f.read(1 << 20), b""):
                h.update(b)
        _SHA_CACHE[key] = "sha256:" + h.hexdigest()
    return _SHA_CACHE[key]


def scrub_env(base: Mapping[str, str] | None = None, *, qemu: str | os.PathLike | None = None,
              extra: Mapping[str, str] | None = None) -> tuple[dict[str, str], dict[str, Any]]:
    """The environment for FastDyn: ``base`` minus ``QEMU_SC_*``/``FASTDYN_*``.

    ``FASTDYN_QEMU`` survives from ``base`` unless ``qemu`` overrides it.
    ``extra`` is applied last (campaign-fixed values such as
    ``FASTDYN_QEMU_ICOUNT`` or a capture switch). Returns ``(env, diff)``
    where ``diff`` lists what was removed and set relative to ``base``.
    """
    base = dict(os.environ if base is None else base)
    env = {k: v for k, v in base.items()
           if not k.startswith(_SCRUB_PREFIXES) or k in _KEEP}
    if qemu is not None:
        env["FASTDYN_QEMU"] = os.fspath(qemu)
    for k, v in (extra or {}).items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = str(v)
    diff = {
        "unset": sorted(k for k in base if k not in env),
        "set": {k: env[k] for k in sorted(env) if base.get(k) != env[k]},
    }
    return env, diff


def stray_capture_files(root: str | os.PathLike) -> list[str]:
    """``*.scm`` / ``*.qprof`` files under ``root`` (must be none after a timing run, §2.1)."""
    root = Path(root)
    return sorted(str(p) for pat in ("*.scm", "*.qprof") for p in root.rglob(pat))


# --------------------------------------------------------------------------
# run directory
# --------------------------------------------------------------------------

_SUN_PATH_MAX = 107  # sizeof(sockaddr_un.sun_path) - 1 on Linux


def qmp_socket_path(root: Path) -> Path:
    p = root / "qmp.sock"
    if len(os.fsencode(p)) <= _SUN_PATH_MAX:
        return p
    tag = hashlib.sha256(os.fsencode(root)).hexdigest()[:16]
    return Path(f"/tmp/scopt-qmp-{os.getuid()}") / f"{tag}.sock"


@dataclass(frozen=True)
class RunDir:
    root: Path

    def __post_init__(self) -> None:
        # accept str and other path-likes, like every other entry point
        if not isinstance(self.root, Path):
            object.__setattr__(self, "root", Path(self.root))

    @property
    def toml(self) -> Path:
        return self.root / "run.toml"

    @property
    def cache_dir(self) -> Path:
        return self.root / "cache"

    @property
    def work(self) -> Path:
        return self.root / "work"

    @property
    def ram(self) -> Path:
        tmpfs = self.meta.get("ram_tmpfs_root") if self.meta_path.is_file() else None
        return _tmpfs_run_dir(self.root, Path(tmpfs)) if tmpfs else self.root / "ram"

    @property
    def qmp(self) -> Path:
        """The QMP socket: ``<root>/qmp.sock``, or a short per-run path when that
        would exceed the 107-byte ``sun_path`` limit (recorded in rundir.json)."""
        if self.meta_path.is_file():
            q = self.meta.get("qmp_socket")
            if q:
                return Path(q)
        return qmp_socket_path(self.root)

    @property
    def meta_path(self) -> Path:
        return self.root / "rundir.json"

    @property
    def meta(self) -> dict[str, Any]:
        return json.loads(self.meta_path.read_text())

    @property
    def cache(self) -> Path | None:
        name = self.meta.get("cache_name")
        return None if name is None else self.cache_dir / name

    @property
    def manifest(self) -> Path | None:
        c = self.cache
        return None if c is None else Path(str(c) + ".fastdyn.json")

    @property
    def quiet(self) -> bool:
        return bool(self.meta.get("quiet", False))

    @property
    def qemu(self) -> str | None:
        return self.meta.get("qemu")

    def config(self) -> dict[str, Any]:
        return tomllib.loads(self.toml.read_text())

    @classmethod
    def open(cls, root: str | os.PathLike) -> "RunDir":
        rd = cls(Path(root).resolve())
        if not rd.meta_path.is_file():
            raise RunDirError("not_a_run_dir", f"{rd.meta_path} missing")
        return rd


def resolve_qemu(qemu: str | os.PathLike | None) -> dict[str, Any]:
    """Which QEMU a new run directory exports as ``FASTDYN_QEMU``.

    * ``None``: the integrated runtime (:func:`scopt.run.runtime.runtime_qemu`,
      sha256-checked; ``runtime_qemu_unavailable`` when missing or different);
    * :data:`FASTDYN_DEFAULT_QEMU`: nothing exported, FastDyn's own choice;
    * a path: that binary (``qemu_missing`` when it is not a file).

    Returns ``{"qemu": path | None, "qemu_selection": runtime_default | explicit |
    fastdyn, "qemu_sha256": ...}``.
    """
    if qemu is None:
        from .runtime import RuntimeMismatch, runtime_qemu_info

        try:
            info = runtime_qemu_info()
        except RuntimeMismatch as e:
            raise RunDirError("runtime_qemu_unavailable", str(e)) from e
        return {"qemu": str(Path(info["path"]).resolve()), "qemu_selection": "runtime_default",
                "qemu_sha256": info["sha256"], "qemu_runtime_override": info["override"]}
    if os.fspath(qemu) == FASTDYN_DEFAULT_QEMU:
        return {"qemu": None, "qemu_selection": "fastdyn", "qemu_sha256": None}
    p = Path(qemu).resolve()
    if not p.is_file():
        raise RunDirError("qemu_missing", f"{qemu} is not a file")
    return {"qemu": str(p), "qemu_selection": "explicit", "qemu_sha256": file_sha256(p)}


def _tmpfs_root(path: str | os.PathLike) -> Path:
    try:
        root = Path(path).resolve(strict=True)
        mount_type = subprocess.check_output(
            ["stat", "-f", "-c", "%T", "--", str(root)], text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RunDirError("ram_root_unavailable", str(path)) from exc
    if not root.is_dir() or mount_type != "tmpfs":
        raise RunDirError("ram_root_not_tmpfs", str(root))
    return root


def _tmpfs_run_dir(root: Path, tmpfs: Path) -> Path:
    tag = hashlib.sha256(os.fsencode(root)).hexdigest()[:24]
    return tmpfs / f"scopt-ram-{os.getuid()}-{tag}"


def _claim_tmpfs_run_dir(path: Path, run: Path) -> None:
    marker = path / ".scopt-owner"
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        if (path.is_symlink() or not path.is_dir() or path.stat().st_uid != os.getuid()
                or not marker.is_file() or marker.is_symlink() or marker.read_text() != str(run)):
            raise RunDirError("ram_dir_not_owned", str(path)) from None
    else:
        marker.write_text(str(run))


def create_run_dir(base_toml: str | os.PathLike, root: str | os.PathLike, *,
                   cache_name: str | None = None, quiet: bool = False,
                   qemu: str | os.PathLike | None = None, label: str | None = None,
                   allow_existing_empty: bool = True,
                   ram_root: str | os.PathLike | None = None) -> RunDir:
    """Create a fresh run directory from ``base_toml`` (refuses a used one).

    ``quiet`` selects ``fastdyn -q`` for both the oracle and the launch.
    ``qemu`` is exported as ``FASTDYN_QEMU``; without it the integrated runtime
    is used (:func:`resolve_qemu`; :data:`FASTDYN_DEFAULT_QEMU` opts out). A
    ``qemu_path`` in the TOML still wins inside FastDyn. ``ram_root`` (or
    ``SCOPT_RAM_ROOT``) puts private file-backed banks on an existing tmpfs.
    The bank backend and share setting remain part of the base configuration.
    """
    q = resolve_qemu(qemu)
    base_toml = Path(base_toml).resolve()
    root = Path(root).resolve()
    if root.exists():
        if not (allow_existing_empty and root.is_dir() and not any(root.iterdir())):
            raise RunDirError("run_dir_not_fresh", f"{root} exists and is not empty")
    text = base_toml.read_text(encoding="utf-8")
    cfg = tomllib.loads(text)
    machine = cfg.get("Machine", {})
    base_cache = machine.get("supercache")
    has_cache = base_cache not in (None, "", False)
    if has_cache and cache_name is None:
        cache_name = os.path.basename(str(base_cache))
    if not has_cache:
        cache_name = None
    rd = RunDir(root)
    private_root = ram_root if ram_root is not None else os.environ.get("SCOPT_RAM_ROOT")
    tmpfs = _tmpfs_root(private_root) if private_root else None
    banks = memory_banks(cfg)
    if tmpfs is not None and (not banks or any(not b.is_file or b.share for b in banks)):
        raise RunDirError("ram_requires_private_files", "set backend='file' and share=false on every bank")
    if tmpfs is not None and any(Path(b.id + ".ram").name != b.id + ".ram" for b in banks):
        raise RunDirError("ram_bad_bank_id", "tmpfs bank IDs must be file names")
    ram_dir = _tmpfs_run_dir(root, tmpfs) if tmpfs is not None else rd.ram
    res = rewrite_toml(
        text,
        supercache=str(rd.cache_dir / cache_name) if cache_name else None,
        memory_file=lambda b: str(ram_dir / f"{b.id}.ram"),
        qmp_socket=str(rd.qmp),
    )
    if tmpfs is not None:
        _claim_tmpfs_run_dir(ram_dir, root)
    for d in (root, rd.cache_dir, ram_dir, rd.qmp.parent):
        d.mkdir(parents=True, exist_ok=True)
    if rd.qmp.parent != root:
        os.chmod(rd.qmp.parent, 0o700)
    rd.toml.write_text(res.text, encoding="utf-8")
    write_json(rd.meta_path, {
        "schema": RUNDIR_SCHEMA,
        "base_toml": str(base_toml),
        "base_toml_sha256": "sha256:" + hashlib.sha256(text.encode()).hexdigest(),
        "run_toml_sha256": "sha256:" + hashlib.sha256(res.text.encode()).hexdigest(),
        "rewritten_keys": list(res.changed),
        "cache_name": cache_name,
        "qmp_socket": str(rd.qmp),
        **({"ram_tmpfs_root": str(tmpfs), "ram_storage": "tmpfs-private-file"} if tmpfs is not None else {}),
        "quiet": bool(quiet),
        **q,
        "label": label,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    })
    return rd


def _ram_file(rd: RunDir, bank) -> Path:
    path = Path(bank.memory_file)
    tmpfs = rd.meta.get("ram_tmpfs_root") if rd.meta_path.is_file() else None
    if tmpfs:
        owned = _tmpfs_run_dir(rd.root, Path(tmpfs))
        if (bank.share or path != owned / (bank.id + ".ram")
                or path.parent != owned or path.is_symlink()):
            raise RunDirError("ram_outside_run_dir", str(path))
    elif not path.resolve().is_relative_to(rd.root) or path.resolve() == rd.root:
        raise RunDirError("ram_outside_run_dir", str(path))
    return path


def _cleanup_tmpfs_ram(rd: RunDir) -> None:
    tmpfs = rd.meta.get("ram_tmpfs_root") if rd.meta_path.is_file() else None
    if not tmpfs:
        return
    path = _tmpfs_run_dir(rd.root, Path(tmpfs))
    if not path.exists():
        return
    _claim_tmpfs_run_dir(path, rd.root)
    for bank in memory_banks(rd.config()):
        if bank.is_file and bank.memory_file:
            _ram_file(rd, bank).unlink(missing_ok=True)
    (path / ".scopt-owner").unlink()
    path.rmdir()


def reset_ram(rd: RunDir) -> list[str]:
    """Delete every file-backed RAM image and recreate it sparse at its configured size.

    Recreating (rather than only deleting) keeps FastDyn from logging
    "Prepared QEMU memory backend" lines, so normalized outputs stay comparable
    with B0. The stale QMP socket is removed too.
    """
    out = []
    tmpfs = rd.meta.get("ram_tmpfs_root") if rd.meta_path.is_file() else None
    if tmpfs:
        root = _tmpfs_root(tmpfs)
        _claim_tmpfs_run_dir(_tmpfs_run_dir(rd.root, root), rd.root)
    for bank in memory_banks(rd.config()):
        if not bank.is_file or not bank.memory_file:
            continue
        p = _ram_file(rd, bank)
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists() or p.is_symlink():
            p.unlink()
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
        try:
            if bank.size:
                os.ftruncate(fd, bank.size)
        finally:
            os.close(fd)
        out.append(str(p))
    if rd.qmp.exists() or rd.qmp.is_symlink():
        rd.qmp.unlink()
    return out


def load_inputs(rd: RunDir, inputs: Mapping[str, str | os.PathLike]) -> dict[str, Any]:
    """Write input files into file-backed RAM banks (fixture contract, docs/fixtures.md).

    ``inputs`` maps a bank ``id`` (normally ``"input"``) to a file. Call after
    :func:`reset_ram`: the bank file is rewritten with the input's bytes and
    stays zero-padded (sparse) to the configured bank size. Bank contents are
    not part of the FastDyn fingerprint, so one artifact serves every input.
    Returns ``{bank_id: {"path", "sha256", "size", "bank_file"}}``.
    """
    banks = {b.id: b for b in memory_banks(rd.config())}
    out: dict[str, Any] = {}
    for bid, src in inputs.items():
        b = banks.get(bid)
        if b is None or not b.is_file or not b.memory_file:
            raise RunDirError("input_bank_missing",
                              f"no file-backed [Memory] bank with id {bid!r} in {rd.toml}")
        data = Path(src).read_bytes()
        if b.size is not None and len(data) > b.size:
            raise RunDirError("input_too_large", f"{src}: {len(data)} > bank {bid} size {b.size}")
        p = _ram_file(rd, b)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            os.ftruncate(fd, 0)
            os.ftruncate(fd, b.size if b.size is not None else len(data))
            off = 0
            while off < len(data):
                off += os.pwrite(fd, data[off:], off)
        finally:
            os.close(fd)
        out[bid] = {"path": str(Path(src).resolve()), "size": len(data),
                    "sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
                    "bank_file": str(p)}
    return out


def fastdyn_args(rd: RunDir, *, quiet: bool | None = None) -> list[str]:
    """``[-q] run -c run.toml -o work`` (shared by the oracle and the launch)."""
    q = rd.quiet if quiet is None else quiet
    return (["-q"] if q else []) + ["run", "-c", str(rd.toml), "-o", str(rd.work)]


# --------------------------------------------------------------------------
# output normalization
# --------------------------------------------------------------------------

_B0_DROP = [re.compile(p) for p in (
    r"^fastdyn\.runner\|",
    r"\|\s+\[timing\] ",
    r"\|\s+Parsing Config file: ",
    r"\|\s+QEMU executable: ",
    r"\|\s+(Virtual|Modifier) Instructions available at ",
    r"\|\s+Prepared QEMU memory backend ",
    r"^\[BENCH\]",
    r"^WALL [0-9.]+ s RSS [0-9]+ KB$",
)]
_CMD_HEADER = "Running following qemu command:"
_PATH_PARTS = ("mem-path=", "supercache=", "dev=", "virtual=", "modifier=")
_PATH_OPTS = {"-qmp", "-accel", "-D"}


def _canon_command(block: list[str]) -> list[str]:
    """Canonical lines for FastDyn's printed QEMU command (qemu_target.py:946-963).

    FastDyn wraps a value at its commas when it is longer than 60 characters,
    so the wrapping depends on the run-directory path length. This joins each
    option's fragments, drops the run-directory-dependent options and value
    parts (``-qmp``, ``-accel``, ``-D``, ``mem-path=``, ``supercache=``,
    ``dev=``, ``virtual=``, ``modifier=``), and renders one line per option.
    The QEMU binary line is dropped.
    """
    opts: list[tuple[str, list[str]]] = []
    for raw in block:
        line = raw.rstrip("\n")
        if line.endswith(" \\"):
            line = line[:-2]
        if re.match(r"^  -", line):
            opt, _, val = line.strip().partition(" ")
            opts.append((opt, [val] if val else []))
        elif line.startswith("    ") and opts:
            opts[-1][1].append(line.strip())
    out = []
    for opt, frags in opts:
        if opt in _PATH_OPTS:
            continue
        parts = [p for p in "".join(frags).split(",") if p]
        parts = [p for p in parts if not p.startswith(_PATH_PARTS)]
        out.append(f"  {opt} {','.join(parts)}".rstrip())
    return out


def norm_b0(text: str) -> list[str]:
    """The B0 normalization (``runs/b0/*.norm``), made independent of path lengths.

    B0's ``.norm`` files are the run logs without the timing, run-directory and
    BENCH lines and without the command-dump lines that hold paths. Applying
    this function to such a ``.norm`` file and to a fresh log gives equal lists
    when the runs are equivalent. The ``WALL ... RSS ...`` line that B0 appended
    is dropped (host time).
    """
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if any(r.search(line) for r in _B0_DROP):
            continue
        out.append(line)
        if line.endswith(_CMD_HEADER):
            block = []
            if i < len(lines) and not lines[i].startswith(" ") and lines[i].endswith(" \\"):
                i += 1  # the QEMU binary path
            while i < len(lines) and lines[i].startswith(" "):
                block.append(lines[i])
                i += 1
            out.extend(_canon_command(block))
    return out


_GUEST_RE = re.compile(r"^(Current CPU type|Reg [0-9]+:|\[BENCH\]|qemu-system-arm:|"
                       r"fastdyn\.targets\.qemu_target\|INFO\|  QEMU exited|"
                       r"fastdyn\.core\|INFO\|  FastDyn Exit)")


def norm_guest(text: str) -> list[str]:
    """Guest-visible lines plus the frontend verdicts (BENCH times masked)."""
    out = []
    for line in text.splitlines():
        if _GUEST_RE.match(line):
            out.append(re.sub(r"^(\[BENCH\]\[[^]]*\]) elapsed_ns=.*", r"\1 elapsed=<host-time>",
                              line))
    return out


_BENCH_RE = re.compile(r"^\[BENCH\]\[([^]]*)\] elapsed_ns=(\d+)")


def parse_bench(text: str) -> list[dict[str, Any]]:
    return [{"tag": m.group(1), "elapsed_ns": int(m.group(2))}
            for m in map(_BENCH_RE.match, text.splitlines()) if m]


# --------------------------------------------------------------------------
# launch
# --------------------------------------------------------------------------

def _qemu_from_log(text: str) -> str | None:
    m = re.search(r"QEMU executable: (\S+)", text)
    return m.group(1) if m else None


def launch(rd: RunDir, *, timeout_s: float = DEFAULT_TIMEOUT_S,
           extra_env: Mapping[str, str | None] | None = None, prove: bool | None = None,
           run_json: str = "run.json", reference_norm: str | os.PathLike | None = None,
           allow_rerun: bool = False, capture_files_ok: bool = False,
           inputs: Mapping[str, str | os.PathLike] | None = None,
           peer: Callable | None = None) -> dict[str, Any]:
    """Run FastDyn once in ``rd`` and write ``run.json``.

    Before the run: refuse a used run directory (unless ``allow_rerun``),
    delete the work directory (the oracle's copy; FastDyn regenerates it with
    the same content) and reset RAM. After the run: normalize the output,
    check for stray ``.scm``/``.qprof`` files, and -- when ``deploy.json``
    exists (or ``prove=True``) -- run the §4.8 proof. ``inputs`` (bank id ->
    file) is written into those RAM banks after the reset (:func:`load_inputs`).
    A peer context can start an external application peripheral before QEMU. Its
    yielded collector checks the application outcome after the normal launch and
    cache proof. A peer rejection invalidates the run and keeps the raw proof.
    """
    try:
        context = peer(rd, inputs or {}, run_json, timeout_s) if peer is not None else nullcontext()
        with context as collect:
            result = _launch(rd, timeout_s=timeout_s, extra_env=extra_env, prove=prove,
                             run_json=run_json, reference_norm=reference_norm, allow_rerun=allow_rerun,
                             capture_files_ok=capture_files_ok, inputs=inputs)
            if collect is not None:
                try:
                    result["application_peer"] = collect(result)
                except Exception as error:
                    result.update(verdict="INVALID_application_peer", application_error=str(error))
                    write_json(rd.root / run_json, result)
                    raise RunDirError("application_peer_failed", str(error)) from error
                write_json(rd.root / run_json, result)
            return result
    finally:
        _cleanup_tmpfs_ram(rd)


def _launch(rd: RunDir, *, timeout_s: float = DEFAULT_TIMEOUT_S,
           extra_env: Mapping[str, str | None] | None = None, prove: bool | None = None,
           run_json: str = "run.json", reference_norm: str | os.PathLike | None = None,
           allow_rerun: bool = False, capture_files_ok: bool = False,
           inputs: Mapping[str, str | os.PathLike] | None = None) -> dict[str, Any]:
    from .proof import deploy_refusal, prove_run

    refused = deploy_refusal(rd)
    if refused is not None and prove is not False:
        raise RunDirError("deploy_refused", f"{rd.root}: {refused}; nothing to launch")
    rjson = rd.root / run_json
    if rjson.exists() and not allow_rerun:
        raise RunDirError("run_dir_used", f"{rjson} exists; use a fresh run directory")
    if rd.work.exists():
        shutil.rmtree(rd.work)
    reset_start = time.monotonic_ns()
    ram = reset_ram(rd)
    reset_end = time.monotonic_ns()
    loaded = load_inputs(rd, inputs) if inputs else {}
    inputs_ready = time.monotonic_ns()
    env, env_diff = scrub_env(qemu=rd.qemu, extra=extra_env)
    cmd = ["setarch", "x86_64", "-R", "timeout", "--signal=TERM", "--kill-after=10",
           f"{timeout_s:g}", str(FASTDYN_BIN), *fastdyn_args(rd)]
    log_path = rd.root / ("stdout.log" if run_json == "run.json"
                          else Path(run_json).stem + ".stdout.log")
    t0 = time.time()
    m0 = time.monotonic()
    timed_out = False
    with open(log_path, "wb") as logf:
        p = subprocess.Popen(cmd, cwd=str(FASTDYN_SRC), env=env, stdout=logf,
                             stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             start_new_session=True)
        deadline = m0 + timeout_s + 30
        # Blocking wait4 (exact wall time and the children's rusage); a timer
        # kills the process group as a backstop if `timeout` itself hangs.
        killed = []

        def _backstop() -> None:
            killed.append(True)
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        timer = threading.Timer(deadline - m0, _backstop)
        timer.daemon = True
        timer.start()
        try:
            _pid, status, rusage = os.wait4(p.pid, 0)
        finally:
            timer.cancel()
        wall_end = time.monotonic()
        timed_out = bool(killed)
        p.returncode = os.waitstatus_to_exitcode(status)
    wall = wall_end - m0
    rc = p.returncode
    if rc == 124:
        timed_out = True
    text = log_path.read_text(encoding="utf-8", errors="replace")
    nb0 = norm_b0(text)
    ng = norm_guest(text)
    suffix = "" if run_json == "run.json" else Path(run_json).stem + "."
    (rd.root / f"{suffix}output.norm").write_text("\n".join(nb0) + "\n")
    (rd.root / f"{suffix}guest.norm").write_text("\n".join(ng) + "\n")
    stray = stray_capture_files(rd.root)

    oracle = rd.root / "oracle.json"
    qemu = None
    if oracle.is_file():
        try:
            qemu = json.loads(oracle.read_text()).get("argv", [None])[0]
        except (ValueError, IndexError):
            qemu = None
    qemu_logged = _qemu_from_log(text)
    qemu = qemu_logged or qemu
    doc: dict[str, Any] = {
        "schema": RUN_SCHEMA,
        "run_dir": str(rd.root),
        "argv": cmd,
        "cwd": str(FASTDYN_SRC),
        "env_diff": env_diff,
        "quiet": rd.quiet,
        "qemu": qemu,
        "qemu_sha256": file_sha256(qemu) if qemu and os.path.isfile(qemu) else None,
        "exit_code": rc,
        "timed_out": timed_out,
        "timings": {
            "start_unix": t0,
            "ram_reset_ns": reset_end - reset_start,
            "bank_stage_ns": inputs_ready - reset_end,
            "wall_s": wall,
            "children_user_s": rusage.ru_utime if rusage else None,
            "children_sys_s": rusage.ru_stime if rusage else None,
            "children_maxrss_kb": rusage.ru_maxrss if rusage else None,
        },
        "bench": parse_bench(text),
        "ram_reset": ram,
        "inputs": loaded,
        "stdout_log": str(log_path),
        "output_norm_sha256": "sha256:" + hashlib.sha256(
            ("\n".join(nb0) + "\n").encode()).hexdigest(),
        "guest_norm": ng,
        "stray_capture_files": stray,
        "cache_invalidated_by_frontend": "Invalidated stale QEMU supercache" in text,
    }
    if reference_norm is not None:
        ref = norm_b0(Path(reference_norm).read_text())
        doc["reference_norm"] = {"path": str(reference_norm), "equal": ref == nb0}
        if ref != nb0:
            import difflib
            doc["reference_norm"]["diff"] = list(difflib.unified_diff(
                ref, nb0, "reference", "run", lineterm="", n=1))[:80]
    deployed = (rd.root / "deploy.json").is_file()
    if prove or (prove is None and deployed):
        pr = prove_run(rd)
        doc["proof"] = pr
        doc["appended"] = pr.get("appended")
        verdict = pr["verdict"]
    else:
        doc["proof"] = None
        doc["appended"] = None
        verdict = "NOT_PROVED"
    if stray and not capture_files_ok:
        verdict = "INVALID_stray_capture_files"
    if rc != 0:
        verdict = "INVALID_exit_code" if verdict in ("PASS", "NOT_PROVED") else verdict
    doc["verdict"] = verdict
    write_json(rjson, doc)
    return doc

