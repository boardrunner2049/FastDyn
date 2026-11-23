"""Contract tests for the self-contained compiled FastDyn launcher."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path


# scopt's test environment puts tools/scopt on sys.path, while the launcher is
# deliberately part of the FastDyn package in src/.  Select this checkout's
# package rather than an unrelated editable install.
FASTDYN_SOURCE = Path(__file__).resolve().parents[4] / "src"
sys.path.insert(0, str(FASTDYN_SOURCE))

from fastdyn.compiled_launch import emit_bundle  # noqa: E402


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
