"""Plugins loaded from a `source = "<dir>"` folder outside FastDyn."""

from __future__ import annotations

import shutil
import sys
import types
from pathlib import Path

import pytest

from fastdyn import toml_parser, virtual_preprocessing as vp

REPO = Path(__file__).resolve().parents[2]
TEST_PLUGIN = REPO / "tests" / "plugins" / "mmio_count"

CONFIG = """
[Machine]
platform = "generic-cortexm"

[Device.Models.unhandled]

[Memory.main]
id = "ram0"
base_address = "0x20000000"
memory_size = "1M"
memory_type = "SRAM"
backend = "file"
memory_file = "/tmp/fastdyn-external-plugins.ram"
share = true

[CPU]
[[CPU.cpu0]]
binary = "firmware.elf"

[CPU.cpu0.plugins.mmio_count]
enabled = {enabled}
label = "unit"
source = "{source}"
"""


@pytest.fixture(autouse=True)
def _isolated_registry():
    """Undo registrations, so each test loads plugins from scratch."""
    run, virtuals, sources = dict(vp.RUN_PREPROCESSORS), dict(vp.VIRTUAL_DEFINITIONS), dict(vp.EXTERNAL_PLUGIN_SOURCES)
    modules = set(sys.modules)
    yield
    for registry, saved in ((vp.RUN_PREPROCESSORS, run), (vp.VIRTUAL_DEFINITIONS, virtuals),
                            (vp.EXTERNAL_PLUGIN_SOURCES, sources)):
        registry.clear()
        registry.update(saved)
    for name in set(sys.modules) - modules:
        del sys.modules[name]


@pytest.fixture
def plugin(tmp_path):
    """A copy of the test plugin folder, with a stand-in runtime library."""
    folder = tmp_path / "plugins" / "mmio_count"
    shutil.copytree(TEST_PLUGIN, folder)
    (folder / "runtime" / "mmio_count.so").write_bytes(b"")
    return folder


def _machine(tmp_path, source, enabled=True):
    config = tmp_path / "configs" / "board.toml"
    config.parent.mkdir(exist_ok=True)
    config.write_text(CONFIG.format(enabled=str(enabled).lower(), source=source), encoding="utf-8")
    handle = toml_parser.parser(str(tmp_path / "work"), "machine0", str(config),
                                "third_party/common/cmsis-svd-data", load_fmu=False)
    return handle.machines["machine0"]


def test_source_is_relative_to_the_config_and_not_a_setting(tmp_path, plugin):
    machine = _machine(tmp_path, "../plugins/mmio_count")
    cpu = machine.cpus[0]

    assert cpu.plugin_sources == {"mmio_count": plugin.resolve()}
    assert cpu.plugin_config["mmio_count"] == {"enabled": True, "label": "unit"}


def test_an_enabled_plugin_runs_and_its_library_is_passed_on(tmp_path, plugin):
    machine = _machine(tmp_path, str(plugin))

    vp.prepare_run_preprocessors(machine, tmp_path / "work")

    assert machine.virtual_libraries == [plugin.resolve() / "runtime" / "mmio_count.so"]
    settings = tmp_path / "work" / "run-artifacts" / "mmio_count" / "settings.txt"
    assert settings.read_text() == "unit\n"  # written by the plugin's own host half


def test_a_disabled_plugin_loads_no_library(tmp_path, plugin):
    machine = _machine(tmp_path, str(plugin), enabled=False)

    vp.prepare_run_preprocessors(machine, tmp_path / "work")

    assert machine.virtual_libraries == []


def test_loading_the_same_folder_again_is_a_no_op(plugin):
    vp.load_external_plugin("mmio_count", plugin)
    vp.load_external_plugin("mmio_count", plugin)

    assert vp.EXTERNAL_PLUGIN_SOURCES["mmio_count"] == plugin.resolve()


def _folder(tmp_path, name, preprocessor):
    folder = tmp_path / name
    (folder / "host").mkdir(parents=True)
    if preprocessor is not None:
        (folder / "host" / "preprocessor.py").write_text(preprocessor, encoding="utf-8")
    return folder


REGISTERS_OTHER = """
from fastdyn.virtual_preprocessing import RunDefinition, RunPrepareResult, register_run_preprocessor
class P:
    def prepare(self, ctx):
        return RunPrepareResult()
register_run_preprocessor(RunDefinition(name="something_else", prepare=P(), enabled=lambda ctx: True))
"""


@pytest.mark.parametrize(
    "name, make, message",
    [
        ("mmio_count", lambda t, p: t / "missing", "no such folder"),
        ("mmio_count", lambda t, p: _folder(t, "empty", None), "has no host/preprocessor.py"),
        ("mmio_count", lambda t, p: _folder(t, "other", REGISTERS_OTHER), "did not register a plugin named 'mmio_count'"),
        ("mmio_count", lambda t, p: _folder(t, "broken", "raise RuntimeError('boom')"), "cannot load .*: boom"),
        ("function_counter", lambda t, p: p, "'function_counter' is a built-in FastDyn plugin"),
    ],
)
def test_bad_plugin_folders_are_reported(tmp_path, plugin, name, make, message):
    with pytest.raises(vp.VirtualPreparationError, match=rf"\[CPU.cpu0.plugins.{name}\].source: .*{message}"):
        vp.load_external_plugin(name, make(tmp_path, plugin))


def test_one_name_cannot_come_from_two_folders(tmp_path, plugin):
    vp.load_external_plugin("mmio_count", plugin)
    other = tmp_path / "copy"
    shutil.copytree(plugin, other)

    with pytest.raises(vp.VirtualPreparationError, match="already loaded from"):
        vp.load_external_plugin("mmio_count", other)


def test_library_paths_qemu_cannot_take_are_rejected(tmp_path, plugin):
    odd = tmp_path / "a,b"
    shutil.copytree(plugin, odd)
    machine = types.SimpleNamespace(cpus=[types.SimpleNamespace(
        plugin_sources={"mmio_count": odd}, plugin_config={"mmio_count": {"enabled": True}},
        binary="firmware.elf", arch="arm", machine="cortexm", cpu="cortex-m4", symbol_dict={},
        machine_obj=types.SimpleNamespace(irq_map={}, virtual_capabilities=set(), qemu_target_opts=None,
                                          fmu_path=None))])

    with pytest.raises(vp.VirtualPreparationError, match="contains ',' or ':'"):
        vp.prepare_run_preprocessors(machine, tmp_path / "work")
