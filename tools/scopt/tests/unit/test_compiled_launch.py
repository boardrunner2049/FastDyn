"""Contract tests for the self-contained compiled FastDyn launcher."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace


# scopt's test environment puts tools/scopt on sys.path, while the launcher is
# deliberately part of the FastDyn package in src/.  Select this checkout's
# package rather than an unrelated editable install.
FASTDYN_SOURCE = Path(__file__).resolve().parents[4] / "src"
sys.path.insert(0, str(FASTDYN_SOURCE))

from fastdyn.compiled_launch import (  # noqa: E402
    _sparse_copy,
    _stage_runtime_files,
    emit_bundle,
)


def _fake_qemu(path: Path) -> Path:
    """Make an executable that records exactly what the launcher passed it."""
    path.write_text(
        f"""#!{sys.executable}
import json
import os
import pathlib
import sys

pathlib.Path(os.environ["CAPTURE_PATH"]).write_text(json.dumps({{
    "argv": sys.argv[1:],
    "cwd": os.getcwd(),
    "frozen_value": os.environ.get("FROZEN_VALUE"),
}}))
""",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _run(script: Path, *args: str, env: dict[str, str] | None = None) -> None:
    subprocess.run(
        [str(script), *args],
        check=True,
        cwd=script.parent.parent,
        env=env,
    )


def test_run_sh_is_python_free_and_preserves_launch_contract(tmp_path):
    work = tmp_path / "working directory with spaces"
    work.mkdir()
    capture = tmp_path / "captured invocation.json"
    sentinel = tmp_path / "shell-injection-must-not-run"
    qemu = _fake_qemu(tmp_path / "fake qemu;$literal")
    fixed_args = [
        "-M",
        "board name with spaces",
        "$HOME is literal",
        f"$(touch {sentinel})",
        "semi;colon",
        "single'quote\"double`backtick",
    ]
    out = tmp_path / "compiled launch"

    emit_bundle(
        out,
        [str(qemu), *fixed_args],
        cwd=work,
        env_set={
            "CAPTURE_PATH": str(capture),
            "FROZEN_VALUE": "configured value; $not-expanded",
        },
    )

    script = out / "run.sh"
    manifest = out / "launch.json"
    assert script.is_file() and manifest.is_file()
    assert stat.S_IMODE(script.stat().st_mode) & stat.S_IXUSR
    shell_text = script.read_text(encoding="utf-8")
    assert re.search(r"(?<!\w)python(?:\d+(?:\.\d+)*)?(?!\w)", shell_text,
                     flags=re.IGNORECASE) is None
    assert isinstance(json.loads(manifest.read_text(encoding="utf-8")), dict)

    extra_args = ["extra argument", "extra;$literal", "*?[not-a-glob]"]
    inherited = dict(os.environ)
    inherited["FROZEN_VALUE"] = "must be overridden"
    _run(script, *extra_args, env=inherited)

    observed = json.loads(capture.read_text(encoding="utf-8"))
    assert observed == {
        "argv": fixed_args + extra_args,
        "cwd": str(work),
        "frozen_value": "configured value; $not-expanded",
    }
    assert not sentinel.exists()


def test_run_sh_resets_ram_by_sparse_truncate_before_exec(tmp_path):
    """A reset clears stale RAM bytes without changing the configured size."""
    work = tmp_path / "work"
    work.mkdir()
    capture = tmp_path / "capture.json"
    qemu = _fake_qemu(tmp_path / "fake-qemu")
    ram = tmp_path / "guest ram.bin"
    stale = bytes(range(256)) * 16
    ram.write_bytes(stale)
    out = tmp_path / "bundle"

    emit_bundle(
        out,
        [str(qemu)],
        cwd=work,
        env_set={"CAPTURE_PATH": str(capture)},
        reset_files=[(ram, len(stale))],
    )

    # Model stale state left by a previous invocation after the bundle was
    # compiled.  The launch must reset it immediately before exec.
    ram.write_bytes(b"\xa5" * len(stale))
    _run(out / "run.sh", env=dict(os.environ))

    assert ram.stat().st_size == len(stale)
    assert ram.read_bytes() == bytes(len(stale))
    assert capture.is_file()  # the fake QEMU ran after the reset completed


def test_cached_bundle_records_and_emits_fixed_address_launch(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    out = tmp_path / "bundle"

    emit_bundle(
        out,
        ["/opt/qemu with spaces/qemu-system-arm", "-accel", "tcg,supercache=x.sc"],
        cwd=work,
        env_set={},
        disable_aslr=True,
    )

    manifest = json.loads((out / "launch.json").read_text(encoding="utf-8"))
    assert manifest["disable_aslr"] is True
    command = (out / "run.sh").read_text(encoding="utf-8")
    assert "exec setarch " in command
    assert " -R '/opt/qemu with spaces/qemu-system-arm'" in command


def test_sparse_copy_preserves_bytes_without_mutating_source(tmp_path):
    source = tmp_path / "source.sc"
    destination = tmp_path / "bundle" / "cache.sc"
    size = 8 * 1024 * 1024
    with source.open("wb") as stream:
        stream.truncate(size)
        stream.seek(4096)
        stream.write(b"first extent")
        stream.seek(size - 4096)
        stream.write(b"last extent")
    before = source.stat()

    _sparse_copy(source, destination)

    assert destination.stat().st_size == size
    assert destination.read_bytes() == source.read_bytes()
    after = source.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)


def test_runtime_state_is_staged_and_source_cache_is_untouched(tmp_path):
    source_ram = tmp_path / "source.ram"
    source_ram.write_bytes(b"RAM state")
    source_cache = tmp_path / "source.sc"
    source_cache.write_bytes(b"SCv7 cache")
    source_manifest = Path(f"{source_cache}.fastdyn.json")
    source_manifest.write_text('{"fingerprint":"frozen"}\n', encoding="utf-8")
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in (source_ram, source_cache, source_manifest)
    }
    memory = SimpleNamespace(
        memory_backend="file",
        memory_file=str(source_ram),
        memory_id="main",
    )
    machine = SimpleNamespace(
        memories={"main": memory},
        qemu_target_opts=SimpleNamespace(supercache=str(source_cache)),
    )
    bundle = tmp_path / "bundle"

    assert _stage_runtime_files(machine, bundle) is True

    assert Path(memory.memory_file) == bundle / "ram" / "main.ram"
    assert Path(memory.memory_file).read_bytes() == b"RAM state"
    staged_cache = Path(machine.qemu_target_opts.supercache)
    assert staged_cache == bundle / "cache" / "source.sc"
    assert staged_cache.read_bytes() == b"SCv7 cache"
    assert Path(f"{staged_cache}.fastdyn.json").read_text(encoding="utf-8") == (
        '{"fingerprint":"frozen"}\n'
    )
    for path, (contents, mtime_ns) in before.items():
        assert path.read_bytes() == contents
        assert path.stat().st_mtime_ns == mtime_ns
