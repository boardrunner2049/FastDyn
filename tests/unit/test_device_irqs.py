"""Device IRQ lists use the numbers the CPU reports (Cortex-M: IRQn + 16)."""

from __future__ import annotations

import pytest

from fastdyn import toml_parser
from fastdyn.fastdyn import Fastdyn

CONFIG = """
[Machine]
platform = "STM32F429"

[Device.Models.classic]

[Device.usart2]
    ranges = [["0x40004400", "0x400047FF"]]
    irq = ['USART2', '0x2D', 15, ['TIM2', 'TIM3']]
    [[Device.usart2.handlers]]
        model = "classic"
        enabled = true

[Memory.main]
id = "ram0"
base_address = "0x20000000"
memory_size = "1M"
memory_type = "SRAM"
backend = "file"
memory_file = "/tmp/fastdyn-device-irqs.ram"
share = true

[CPU]
[[CPU.cpu0]]
binary = "firmware.elf"
"""


def test_svd_names_become_exception_numbers_and_numbers_stay(tmp_path):
    config = tmp_path / "board.toml"
    config.write_text(CONFIG, encoding="utf-8")

    machine = toml_parser.parser(str(tmp_path / "work"), "machine0", str(config),
                                 "third_party/common/cmsis-svd-data", load_fmu=False).machines["machine0"]

    # STM32F429: USART2 is IRQ 38, TIM2 28, TIM3 29 -> exception numbers 54, 44, 45.
    assert machine.irq_map["USART2"] == 38
    assert machine.devices["usart2"].irq_range == [15, 44, 45, 54]


def test_an_irq_name_needs_the_cpu_first():
    machine = Fastdyn().create_machine(machine_name="machine0", platform="STM32F429")
    machine.irq_map = {"USART2": 38}
    device = machine.add_device("usart2")

    with pytest.raises(RuntimeError, match=r"\[Device.usart2\] IRQ name 'USART2': add the CPU"):
        device.add_irq("USART2")

    device.add_irq(54)  # numbers never need the CPU
    assert device.irq_range == [54]
