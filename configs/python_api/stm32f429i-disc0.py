"""Build and run an STM32F429I-DISC1 machine with FastDyn's Python builder API.

This is the Python equivalent of configs/freertos_stm32f429i_discovery.toml:
FreeRTOS firmware with all peripherals handled by FastDyn's classic model, so
no board is needed. Run it from the FastDyn repository root after building
FastDyn (make ...):

    ./fastdyn-env/bin/python configs/python_api/stm32f429i-disc0.py

The firmware runs until you press Ctrl+C. Results are written to
fastdyn_work_python_api/.
"""

import os

from fastdyn.fastdyn import Fastdyn

WORK_DIR = "fastdyn_work_python_api"

fastdyn = Fastdyn()

machine0 = fastdyn.create_machine(machine_name="machine0", platform="STM32F429")

# QEMU options (the [Machine] table of a TOML config). qemu_path is optional:
# FastDyn uses the QEMU it was built against.
opts = machine0.qemu_target_opts
opts.display = "none"
opts.serial = "none"
opts.monitor_port = 0
opts.qmp_socket = "/tmp/fastdyn-python-api-f429.qmp"
opts.reset_memory_files = True
opts.print_command = True

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
cpu0.log_options = "none"
# Run-wide plugins, like [CPU.cpu0.plugins.<name>] in TOML.
cpu0.plugin_config = {"introspection": {"enabled": True}}

# Optional: CMSIS-SVD lets virtual instructions and IRQs use peripheral names.
machine0.add_cmsis_svd(cmsis_svd="third_party/common/cmsis-svd-data")

# Memories: add the main memory first (it becomes -machine ...,memory-backend=<id>).
machine0.add_memory(
    memory_name="main",
    memory_id="ram0",
    memory_start="0x20000000",
    memory_size="192K",
    memory_type="SRAM",
    backend="file",  # file | ram | memfd
    memory_file="/tmp/fastdyn-python-api-f429.ram",
    share=True,
)

# ------------------------------- Devices --------------------------------
# Models available to this machine, then devices routed to them.
machine0.add_model(name="classic")

peripherals = machine0.add_device("unmapped_peripherals")
peripherals.add_ranges(start="0x40000000", end="0x5fffffff")
peripherals.add_handler(name="classic", enabled=True)

# With a board attached, the same API routes devices to hardware instead
# (passthrough over ST-Link) or to generated models (elder), for example:
#
#   machine0.add_model(name="passthrough", backend="stlink")
#   machine0.add_model(name="elder")
#   uart0 = machine0.add_device("uart")
#   uart0.add_ranges(start="0x40021800", end="0x40021BFF")
#   uart0.add_irq([1, 20])
#   uart0.add_handler(name="passthrough", enabled=True)
#   uart0.add_handler(name="elder", enabled=False, scroll="path/to/uart_model.so")

print("Starting FastDyn")
os.makedirs(WORK_DIR, exist_ok=True)
try:
    exit_code = fastdyn.run(target="qemu", machine_name="machine0", out_path=WORK_DIR)
    print(f"QEMU exited with code {exit_code}; results in {WORK_DIR}/")
except KeyboardInterrupt:
    print(f"Stopped; results in {WORK_DIR}/")
