"""A tour of FastDyn's Python builder API.

It shows the different ways to describe a machine in Python: CPUs, virtual
instructions, modifiers, memories, devices, IRQs and device-model handlers.
The addresses and symbols below are illustrative (they come from different
firmware), so this script builds and prints the machine but does not launch
it. For a machine that runs as-is, see stm32f429i-disc0.py next to this file.

Run it from the FastDyn repository root:

    ./fastdyn-env/bin/python configs/python_api/example.py
"""

from fastdyn.fastdyn import Fastdyn, InstructionModifier
from fastdyn.machine import VirtualInstruction

fastdyn = Fastdyn()

machine0 = fastdyn.create_machine(machine_name="machine0", platform="STM32F429")

cpu0 = machine0.add_cpu(
    arch="arm",
    machine="cortexm",
    cpu="cortex-m4",
    binary="tests/binaries/freertos_stm32f429i_discovery/freertos_stm32f429i_discovery.elf",
    init_nsvtor="0x08000000",
    twintrace=None,           # "record" / "replay" for hardware traces, None otherwise
    hardware_trace=None,      # hardware I/O log, needed for twintrace replay
    exstng_config_path=None,  # existing virtuals/modifiers files to reuse, if any
)

# Optional: a CMSIS-SVD file lets virtual instructions, modifiers and IRQs use
# peripheral/IRQ names. Give the svd-data directory; the platform name above
# selects the device.
machine0.add_cmsis_svd(cmsis_svd="third_party/common/cmsis-svd-data")

# Symbols such as "spi_lld_serve_tx_interrupt" are resolved from the
# firmware's ELF symbols when the run is prepared.

# Virtual instructions can be added in four ways:

# Option 1: a string "<at> <instruction> <args>"
vi0 = "0x80000000 raise_irq 0x000001"
cpu0.add_virtual_instruction(vi0)

# Option 2: a VirtualInstruction with an address
vi0 = VirtualInstruction(at=0x80000000, instruction="raise_irq", args=["0x000001"])
cpu0.add_virtual_instruction(vi0)

# Option 3: a VirtualInstruction with a symbol and an SVD IRQ name
vi0 = VirtualInstruction(at="spi_lld_serve_tx_interrupt", instruction="raise_irq", args=["USART1"])
cpu0.add_virtual_instruction(vi0)

# Option 4: a list mixing strings and VirtualInstruction objects
vi_list = [
    "spi_lld_serve_tx_interrupt raiseirq USART1",
    "0x80000000 raise_irq 0x000001",
    VirtualInstruction(at="spi_lld_serve_tx_interrupt", instruction="raise_irq", args=["USART1"]),
    VirtualInstruction(at="spi_lld_serve_tx_interrupt", instruction="anchor", args=["/tmp/local/uart"]),
]
cpu0.add_virtual_instruction(vi_list)

print(cpu0.virtuals)

# Modifiers can be added in four ways:

# Option 1: a string "<at> <lhs> <rhs>"
mod0 = "0x80d9182 r15 0x80d9184"
cpu0.add_modifier(mod0)

# Option 2: an InstructionModifier with an address
mod0 = InstructionModifier(at=0x80d9182, patch="r15 0x80d9184")
cpu0.add_modifier(mod0)

# Option 3: an InstructionModifier with a symbol, e.g. to set a return value
# or to redirect control flow through registers
mod0 = InstructionModifier(at="_ZN8AP_Param5flushEv", patch="r0 0")
cpu0.add_modifier(mod0)

mod1 = InstructionModifier(at="_ZN8AP_Param5flushEv", patch="r15 r14")
cpu0.add_modifier(mod1)

# Option 4: a list mixing strings and InstructionModifier objects
mod_list = [
    "_ZN8AP_Param5flushEv r0 0",
    "_ZN8AP_Param5flushEv r15 r14",
    "0x8028f2e r15 0x802a1ea",
    InstructionModifier(at="stm32_clock_init", patch="r0 0"),
    InstructionModifier(at="stm32_clock_init", patch="r15 r14"),
]
cpu0.add_modifier(mod_list)

print(cpu0.modifiers)

# Memories: add the main memory first (it becomes -machine ...,memory-backend=<id>),
# then any other region.
machine0.add_memory(
    memory_name="main",
    memory_id="ram0",
    memory_start="0x20000000",
    memory_size="512M",
    memory_type="SRAM",
    backend="file",  # file | ram | memfd
    memory_file="/tmp/fastdyn-example-ram0",
    share=True,
)

machine0.add_memory(
    memory_name="region1",
    memory_id="ram1",
    memory_start="0x30000000",
    memory_size="512K",
    memory_type="SRAM",
    backend="file",
    memory_file="/tmp/fastdyn-example-ram1",
    share=True,
)

print(machine0.memories)

for machine in fastdyn.machines:
    print(f"Machine: {fastdyn.machines[machine].name} details::")
    for cpu in fastdyn.machines[machine].cpus:
        print(f"{cpu}")

# ------------------------------- Devices --------------------------------
# Models available to this machine
machine0.add_model(name="elder")
machine0.add_model(name="passthrough", backend="stlink")

# Attach devices to the machine
uart0 = machine0.add_device("uart")

# Device ranges can be added in two ways:
# Option 1: start and end of the range (int or str)
uart0.add_ranges(start="0x40000000", end="0x40010FFF")
# Option 2: start and size of the range (B, KB, MB, GB)
uart0.add_ranges(start="0x40000000", size="4KB")

print(uart0.supported_ranges)

# IRQs can be added in three ways:
# Option 1: a single IRQ
uart0.add_irq(1)
# Option 2: an inclusive range [first, last]
uart0.add_irq([2, 15])
uart0.add_irq([20, 25])
uart0.add_irq(53)
# Option 3: an IRQ name from the CMSIS-SVD file given above
uart0.add_irq("UART7")

print(uart0.irq_range)

# A device can list several handlers; the enabled one serves its accesses.
uart0.add_handler(name="qemu", type="stm32f2xx-usart", enabled=False, args="device chardev")
uart0.add_handler(name="passthrough", enabled=True)
uart0.add_handler(name="elder", enabled=False, scroll="path/to/uart_model.so")

# Everything else goes to the board through passthrough.
remaining_range = machine0.add_device("unhandled")
remaining_range.add_ranges(start="0x40000000", end="0x40010FFF")
remaining_range.add_ranges(start="0x40012000", end="0xE00FFFFF")
remaining_range.add_ranges(start="0xE0000000", end="0xEFFFFFFF")
remaining_range.add_irq([0, 15])
remaining_range.add_irq([20, 25])
remaining_range.add_irq(53)
remaining_range.add_handler(name="passthrough", enabled=True)

print("Machine built.")

# To launch a machine (this one needs the board for its passthrough devices):
#
#   import os
#   os.makedirs("fastdyn_work", exist_ok=True)
#   fastdyn.run(target="qemu", machine_name="machine0", out_path="fastdyn_work")
#
# or run a TOML configuration with the same orchestration as `fastdyn run`:
#
#   import fastdyn as fd
#   results = fd.run_config("configs/function_counter.toml", work_dir="fastdyn_work")
