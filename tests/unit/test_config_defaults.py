"""Settings a configuration may leave out, and the error when a required one is missing."""

from __future__ import annotations

import pytest

from fastdyn import toml_parser
from fastdyn.targets import qemu_target

SHORT_CONFIG = """
[Machine]
platform = "generic-cortexm"

[Memory.main]
base_address = "0x20000000"
memory_size = "1M"

[CPU]
[[CPU.cpu0]]
arch = "arm"
machine = "cortexm"
cpu = "cortex-m3"
binary = "app.elf"

[[CPU.cpu0.virtuals]]
at = "0x08000100"
instruction = "benchmark_start"

[[CPU.cpu0.modifiers]]
at = "0x08000200"
patch = "r0 <- 3"
"""


def _machine(tmp_path, text):
    config = tmp_path / "config.toml"
    config.write_text(text, encoding="utf-8")
    handle = toml_parser.parser(str(tmp_path / "work"), "machine0", str(config),
                                "third_party/common/cmsis-svd-data", load_fmu=False)
    return handle.machines["machine0"]


def test_memory_settings_default(tmp_path):
    memory = _machine(tmp_path, SHORT_CONFIG).memories["main"]

    assert memory.memory_id == "main"
    assert memory.memory_backend.name == "FILE"
    assert memory.memory_share is True
    assert not memory.memory_file  # chosen in the work directory when the run starts


def test_device_table_and_virtual_args_are_optional(tmp_path):
    machine = _machine(tmp_path, SHORT_CONFIG)

    assert machine.devices == {}
    assert machine.cpus[0].virtuals == ["0x08000100 benchmark_start"]


def test_missing_required_setting_names_table_and_key(tmp_path):
    text = SHORT_CONFIG.replace('memory_size = "1M"\n', "")

    with pytest.raises(toml_parser.ConfigError, match=r"\[Memory.main\] needs 'memory_size'"):
        _machine(tmp_path, text)


def test_default_memory_file_is_in_the_work_directory(tmp_path):
    machine = _machine(tmp_path, SHORT_CONFIG)

    qemu_target._default_memory_files(machine, str(tmp_path / "work"))

    assert machine.memories["main"].memory_file == str(tmp_path / "work" / "main.ram")


def test_ram_backend_has_no_backing_file(tmp_path):
    text = SHORT_CONFIG.replace('memory_size = "1M"\n', 'memory_size = "1M"\nbackend = "ram"\n')
    memory = _machine(tmp_path, text).memories["main"]

    assert qemu_target._memory_backend_object(memory, "on") == "memory-backend-ram,id=main,size=1M,share=on"
