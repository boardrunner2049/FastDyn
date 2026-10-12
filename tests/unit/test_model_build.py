"""fastdyn.model_build and `fastdyn build-model`, with the system C compiler."""

from __future__ import annotations

import ctypes
import json
import os
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from fastdyn import main, model_build, paths
from fastdyn.model_build import BUILT, FAILED, SKIPPED, UP_TO_DATE, Model, ModelBuildError, build_models

pytestmark = pytest.mark.skipif(shutil.which("cc") is None, reason="no C compiler")

MODEL = """\
#include <fastdyn/device_model.h>
#include "regs.h"
static void *init(const FdDevice *dev, const char *cfg, const FdHost *host) { return (void *)dev; }
static uint64_t rd(void *o, hwaddr addr, unsigned size, uint64_t pc) { return REG_VALUE; }
static void wr(void *o, hwaddr addr, uint64_t value, unsigned size, uint64_t pc) {}
const FdModel fastdyn_model = {FASTDYN_MODEL_ABI_FIELDS, .init = init, .read = rd, .write = wr};
"""
# read() lacks the pc argument: the descriptor rejects it at compile time.
BROKEN = MODEL.replace("unsigned size, uint64_t pc) { return", "unsigned size) { return")


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


_real_pkg_config_exists = model_build._pkg_config_exists


@pytest.fixture(autouse=True)
def _no_vio(monkeypatch):
    """Tests do not depend on whether LibVIO happens to be installed."""
    monkeypatch.setattr(model_build, "_pkg_config_exists",
                        lambda pkg, env=None: pkg != "vio" and _real_pkg_config_exists(pkg, env))
    for var in ("CC", "CFLAGS", "LDFLAGS"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def models(tmp_path):
    folder = tmp_path / "models"
    _write(folder / "include" / "regs.h", "#define REG_VALUE 7\n")
    for name in ("uart", "spi", "i2c", "timer"):
        _write(folder / f"{name}.c", MODEL)
    return folder


def _statuses(report):
    return {r.name: r.status for r in report.results}


def _touch_later(path: Path, *outputs: Path):
    """Make `path` newer than the outputs, whatever the filesystem's resolution."""
    newest = max(o.stat().st_mtime for o in outputs)
    os.utime(path, (newest + 2, newest + 2))


def test_a_folder_builds_in_parallel(models, tmp_path):
    report = build_models([models], tmp_path / "out", jobs=4)

    assert report.ok and report.summary() == "built 4, up to date 0, failed 0"
    for name in ("uart", "spi", "i2c", "timer"):
        library = ctypes.CDLL(str(tmp_path / "out" / f"{name}.so"))
        assert ctypes.c_uint32.in_dll(library, "fastdyn_model").value == 2


def test_only_what_changed_is_rebuilt(models, tmp_path, monkeypatch):
    out = tmp_path / "out"
    build_models([models], out)
    assert set(_statuses(build_models([models], out)).values()) == {UP_TO_DATE}

    _touch_later(models / "spi.c", out / "spi.so")
    assert _statuses(build_models([models], out)) == {
        "i2c": UP_TO_DATE, "spi": BUILT, "timer": UP_TO_DATE, "uart": UP_TO_DATE}

    _touch_later(models / "include" / "regs.h", *out.glob("*.so"))  # a header every model uses
    assert set(_statuses(build_models([models], out)).values()) == {BUILT}

    monkeypatch.setenv("CFLAGS", "-O1")  # a different command
    assert set(_statuses(build_models([models], out)).values()) == {BUILT}

    (out / "uart.so").unlink()
    assert _statuses(build_models([models], out))["uart"] == BUILT


def test_keep_going_builds_everything_else_and_reports_the_failure(models, tmp_path):
    _write(models / "aaa_broken.c", BROKEN)

    report = build_models([models], tmp_path / "out", keep_going=True)

    assert not report.ok
    assert report.summary() == "built 4, up to date 0, failed 1"
    failed = report.results[0]
    assert failed.name == "aaa_broken" and failed.status == FAILED
    assert "incompatible-pointer-types" in failed.first_error
    assert not (tmp_path / "out" / "aaa_broken.so").exists()


def test_without_keep_going_no_new_build_starts_after_a_failure(models, tmp_path):
    _write(models / "aaa_broken.c", BROKEN)

    report = build_models([models], tmp_path / "out", jobs=1)

    assert _statuses(report)["aaa_broken"] == FAILED
    assert {s for n, s in _statuses(report).items() if n != "aaa_broken"} == {SKIPPED}


def test_a_source_with_the_librarys_own_time_is_rebuilt(models, tmp_path):
    """File times advance in coarse steps: an edit right after a build can carry
    the library's exact time, and must not count as up to date."""
    out = tmp_path / "out"
    build_models([models / "uart.c"], out)
    built = (out / "uart.so").stat().st_mtime_ns
    os.utime(models / "uart.c", ns=(built, built))

    assert _statuses(build_models([models / "uart.c"], out)) == {"uart": BUILT}


def test_a_failed_rebuild_removes_the_old_library(models, tmp_path):
    out = tmp_path / "out"
    build_models([models / "uart.c"], out)
    _write(models / "uart.c", BROKEN)

    assert _statuses(build_models([models / "uart.c"], out)) == {"uart": FAILED}
    assert not (out / "uart.so").exists()

    _write(models / "uart.c", MODEL)
    assert _statuses(build_models([models / "uart.c"], out)) == {"uart": BUILT}


def test_a_model_made_of_several_files(tmp_path):
    folder = tmp_path / "spi4"
    _write(folder / "regs.h", "#define REG_VALUE helper()\nunsigned helper(void);\n")
    _write(folder / "spi4.c", MODEL)
    _write(folder / "spi4_dma.c", "unsigned helper(void) { return 3; }\n")

    report = build_models([Model("spi4", [folder / "spi4.c", folder / "spi4_dma.c"])], tmp_path / "out")

    assert report.ok and (tmp_path / "out" / "spi4.so").is_file()
    assert model_build.find_models([folder / "spi4.c", folder / "spi4_dma.c"], name="spi4") == [
        Model("spi4", [folder / "spi4.c", folder / "spi4_dma.c"])]


def test_recursive_and_exclude(models, tmp_path):
    _write(models / "board_b" / "gpio.c", MODEL.replace('"regs.h"', '"../include/regs.h"'))
    _write(models / "scratch_test.c", BROKEN)

    flat = build_models([models], tmp_path / "flat", exclude=["scratch_*"])
    deep = build_models([models], tmp_path / "deep", recursive=True, exclude=["scratch_*"])

    assert sorted(_statuses(flat)) == ["i2c", "spi", "timer", "uart"]
    assert sorted(_statuses(deep)) == ["gpio", "i2c", "spi", "timer", "uart"]


@pytest.mark.parametrize(
    "setup, message",
    [
        (lambda d: [d / "empty"], "no .c files found"),
        (lambda d: [d / "a" / "uart.c", d / "b" / "uart.c"], "two models are called 'uart'"),
    ],
)
def test_requests_that_cannot_be_built(tmp_path, setup, message):
    (tmp_path / "empty").mkdir()
    _write(tmp_path / "a" / "uart.c", MODEL)
    _write(tmp_path / "b" / "uart.c", MODEL)

    with pytest.raises(ModelBuildError, match=message):
        build_models(setup(tmp_path), tmp_path / "out")


def test_missing_compiler_or_package_is_reported(models, tmp_path, monkeypatch):
    with pytest.raises(ModelBuildError, match="--pkg no-such-package: not found"):
        build_models([models], tmp_path / "out", pkgs=["no-such-package"])
    with pytest.raises(ModelBuildError, match="--name needs source files"):
        model_build.find_models([models], name="x")
    monkeypatch.setenv("CC", "no-such-compiler")
    with pytest.raises(ModelBuildError, match="C compiler not found"):
        build_models([models], tmp_path / "out")


@pytest.mark.skipif(shutil.which("pkg-config") is None, reason="needs pkg-config")
def test_the_checkouts_libvio_build_comes_before_an_installed_one(tmp_path, monkeypatch):
    """setup.sh --build-libvio installs the submodule into out/libvio/install;
    build-model takes LibVIO from there first."""
    prefix = tmp_path / "out" / "libvio" / "install"
    _write(prefix / "lib" / "pkgconfig" / "vio.pc", (
        "prefix=${pcfiledir}/../..\nlibdir=${prefix}/lib\nincludedir=${prefix}/include\n"
        "Name: vio\nDescription: LibVIO\nVersion: 1.0.0\n"
        "Cflags: -I${includedir}\nLibs: -L${libdir} -lvio\n"))
    monkeypatch.setattr(model_build, "_pkg_config_exists", _real_pkg_config_exists)
    monkeypatch.setattr(paths, "libvio_prefix", lambda: prefix)
    monkeypatch.setenv("PKG_CONFIG_PATH", str(tmp_path / "elsewhere"))

    toolchain = model_build._Toolchain.resolve(vio=True, pkgs=())

    assert toolchain.vio
    assert f"-I{prefix / 'include'}" in [os.path.normpath(f) if f.startswith("-I") else f
                                          for f in toolchain.cflags]
    assert f"-Wl,-rpath,{prefix / 'lib'}" in toolchain.libs


def test_a_libvio_model_without_libvio_gets_a_hint(tmp_path):
    source = _write(tmp_path / "pty.c", "#include <vio/vio.h>\n" + MODEL.replace('#include "regs.h"\n', "#define REG_VALUE 0\n"))

    report = build_models([source], tmp_path / "out")

    assert not report.vio
    assert "LibVIO was not found by pkg-config" in report.results[0].diagnostics


def test_cli(models, tmp_path):
    _write(models / "aaa_broken.c", BROKEN)
    runner = CliRunner(mix_stderr=False)

    result = runner.invoke(main.cli, ["build-model", str(models), "-o", str(tmp_path / "out"), "-k"])

    assert result.exit_code == 1
    lines = result.stdout.splitlines()
    assert lines[0].startswith("FAILED      aaa_broken: ")
    assert lines[1] == f"built       i2c -> {tmp_path / 'out' / 'i2c.so'}"
    assert lines[-1] == "built 4, up to date 0, failed 1"

    (models / "aaa_broken.c").unlink()
    result = runner.invoke(main.cli, ["build-model", str(models), "-o", str(tmp_path / "out"), "--json"])
    assert result.exit_code == 0
    report = json.loads(result.stdout)
    assert report["ok"] and report["counts"] == {"built": 0, "up-to-date": 4, "failed": 0, "skipped": 0}


def test_build_model_is_in_fastdyn_help():
    from fastdyn import feature_browser

    entry = {e.name: e for e in feature_browser.run_entries()}["build device models"]
    assert "fastdyn build-model" in entry.toml
