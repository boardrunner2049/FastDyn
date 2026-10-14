"""The Python builder API (fastdyn.fastdyn) describes a machine without a TOML file."""

from __future__ import annotations

from fastdyn.fastdyn import Fastdyn, compile_device_routing


def _machine():
    handle = Fastdyn()
    machine = handle.create_machine(machine_name="machine0", platform="STM32F429")
    machine.add_cpu(
        arch="arm",
        machine="cortexm",
        cpu="cortex-m4",
        binary="firmware.elf",
        init_nsvtor="0x08000000",
        twintrace=None,
        hardware_trace=None,
        exstng_config_path=None,
    )
    return handle, machine


def test_create_machine_registers_it_by_name():
    handle, machine = _machine()

    assert handle.machines["machine0"] is machine
    assert machine.cpus[0].cpu == "cortex-m4"


def test_memories_and_device_ranges():
    _handle, machine = _machine()
    machine.add_memory(
        memory_name="main",
        memory_id="ram0",
        memory_start="0x20000000",
        memory_size="192K",
        memory_type="SRAM",
        backend="file",
        memory_file="/tmp/fastdyn-test.ram",
    )
    device = machine.add_device("uart")
    device.add_ranges(start="0x40000000", end="0x40000FFF")
    device.add_ranges(start="0x40010000", size="4KB")
    device.add_irq(1)
    device.add_irq([20, 22])

    assert list(machine.memories) == ["main"]
    assert device.supported_ranges == [[0x40000000, 0x40000FFF], [0x40010000, 0x40010FFF]]
    assert device.irq_range == [1, 20, 21, 22]


def test_device_routing_for_the_enabled_handler():
    _handle, machine = _machine()
    machine.add_model(name="classic")
    peripherals = machine.add_device("peripherals")
    peripherals.add_ranges(start="0x40000000", end="0x5fffffff")
    peripherals.add_handler(name="classic", enabled=True)

    compile_device_routing(machine.devices, machine.parsed_device, models=machine.models)

    window = [[0x40000000, 0x5FFFFFFF]]
    assert machine.parsed_device["classic"]["overall"] == window
    assert machine.parsed_device["classic"]["peripherals"]["range"] == window
