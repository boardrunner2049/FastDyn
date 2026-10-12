"""Files named in a configuration are found next to the configuration file."""

from __future__ import annotations

import os

import pytest

from fastdyn import toml_parser

CONFIG = """
[Machine]
platform = "generic-cortexm"

[Device.Models.elder]
[Device.Models.unhandled]

[Memory.main]
id = "ram0"
base_address = "0x20000000"
memory_size = "1M"
memory_type = "SRAM"
backend = "file"
memory_file = "/tmp/fastdyn-config-paths.ram"
share = true

[CPU]
[[CPU.cpu0]]
binary = "firmwares/app.elf"
hardware_trace = "hardware_log/io.trace"

[Device.spi2]
ranges = [["0x40003800", "0x40003BFF"]]
    [[Device.spi2.handlers]]
        model   = "elder"
        enabled = true
        scroll  = "build/spi2_model.so"
    [[Device.spi2.slaves]]
        device        = "bme280"
        cs_id         = 0
        device_scroll = "build/bme280_slave.so"
    [[Device.spi2.connections]]
        type = "endpoint"
        name = "host/feed.py"
"""


@pytest.fixture
def example(tmp_path, monkeypatch):
    """An example folder with its files, and the current directory elsewhere."""
    folder = tmp_path / "examples" / "spi"
    for name in ("firmwares/app.elf", "hardware_log/io.trace", "build/spi2_model.so", "build/bme280_slave.so",
                 "host/feed.py"):
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(b"")
    (folder / "spi.toml").write_text(CONFIG, encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    return folder


def _machine(tmp_path, config):
    handle = toml_parser.parser(str(tmp_path / "work"), "machine0", str(config),
                                "third_party/common/cmsis-svd-data", load_fmu=False)
    return handle.machines["machine0"]


def test_paths_are_found_next_to_the_config(tmp_path, example):
    machine = _machine(tmp_path, example / "spi.toml")

    cpu = machine.cpus[0]
    device = machine.devices["spi2"]
    assert cpu.binary == str(example / "firmwares/app.elf")
    assert cpu.hardware_trace == str(example / "hardware_log/io.trace")
    assert device.handlers[0].scroll == str(example / "build/spi2_model.so")
    assert device.slaves[0].device_scroll == str(example / "build/bme280_slave.so")
    # the device settings handed to the model (and to LibVIO's bus) carry it too
    assert device.config["slaves"][0]["device_scroll"] == str(example / "build/bme280_slave.so")
    # a host endpoint's Python helper too
    assert device.connections[0]["name"] == str(example / "host/feed.py")


def test_paths_not_next_to_the_config_stay_relative_to_the_current_directory(tmp_path, example):
    os.remove(example / "build" / "spi2_model.so")
    os.remove(example / "hardware_log" / "io.trace")  # e.g. a tape that a record run will write
    os.remove(example / "host" / "feed.py")

    machine = _machine(tmp_path, example / "spi.toml")

    assert machine.devices["spi2"].handlers[0].scroll == "build/spi2_model.so"
    assert machine.cpus[0].hardware_trace == "hardware_log/io.trace"
    assert machine.devices["spi2"].connections[0]["name"] == "host/feed.py"


def test_absolute_paths_are_kept(tmp_path, example):
    config = example / "spi.toml"
    config.write_text(CONFIG.replace('"firmwares/app.elf"', f'"{example}/firmwares/app.elf"'), encoding="utf-8")

    assert _machine(tmp_path, config).cpus[0].binary == f"{example}/firmwares/app.elf"
