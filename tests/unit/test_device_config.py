"""A device's own settings reach its model as `config` (plan §5.3)."""

from __future__ import annotations

from fastdyn import toml_parser
from fastdyn.fastdyn import Fastdyn, compile_device_routing

CONFIG = """
[Machine]
platform = "generic-cortexm"

[Device.Models.elder]
[Device.Models.classic]

[Device.i2c1]
    description = "I2C controller"
    ranges = [["0x40005400", "0x400057FF"]]
    irq = ['31']
    read_priority = "elder"
    baud = 9600
    [[Device.i2c1.handlers]]
        model = "elder"
        enabled = true
        scroll = "build/models/i2c.so"
    [[Device.i2c1.handlers]]
        model = "classic"
        enabled = false
    [[Device.i2c1.slaves]]
        model = ["elder"]
        device = "bme280"
        address = "0x76"
        device_scroll = "build/models/bme280.so"

[Device.rest]
    ranges = [["0x40000000", "0x400053FF"]]
    [[Device.rest.handlers]]
        model = "classic"
        enabled = true

[Memory.main]
id = "ram0"
base_address = "0x20000000"
memory_size = "1M"
memory_type = "SRAM"
backend = "file"
memory_file = "/tmp/fastdyn-device-config.ram"
share = true

[CPU]
[[CPU.cpu0]]
binary = "firmware.elf"
"""

SLAVES = [{"model": ["elder"], "device": "bme280", "address": "0x76",
           "device_scroll": "build/models/bme280.so"}]


def _machine(tmp_path):
    config = tmp_path / "board.toml"
    config.write_text(CONFIG, encoding="utf-8")
    handle = toml_parser.parser(str(tmp_path / "work"), "machine0", str(config),
                                "third_party/common/cmsis-svd-data", load_fmu=False)
    return handle.machines["machine0"]


def test_config_is_the_device_table_without_fastdyn_keys(tmp_path):
    machine = _machine(tmp_path)

    assert machine.devices["i2c1"].config == {"baud": 9600, "slaves": SLAVES}
    assert machine.devices["rest"].config == {}


def test_routing_passes_config_to_each_enabled_handler(tmp_path):
    machine = _machine(tmp_path)

    compile_device_routing(machine.devices, machine.parsed_device, models=machine.models)

    assert machine.parsed_device["elder"]["i2c1"]["config"] == {"baud": 9600, "slaves": SLAVES}
    assert machine.parsed_device["classic"]["rest"]["config"] == {}
    assert "i2c1" not in machine.parsed_device["classic"]  # its classic handler is disabled


def test_builder_api_sets_config_directly():
    handle = Fastdyn()
    machine = handle.create_machine(machine_name="machine0", platform="STM32F429")
    machine.add_model(name="elder")
    uart = machine.add_device("usart2")
    uart.add_ranges(start="0x40004400", end="0x400047FF")
    uart.add_handler(name="elder", enabled=True, scroll="build/models/usart.so")
    uart.config = {"pty": "usart2"}

    compile_device_routing(machine.devices, machine.parsed_device, models=machine.models)

    routed = machine.parsed_device["elder"]["usart2"]["config"]
    assert routed == {"pty": "usart2"} and routed is not uart.config
