# Building a FastDyn configuration from the bare-bones starter

FastDyn's discovery commands are intended to make a configuration without
memorizing architecture names, SVD identifiers, virtual callback names, or
plugin table paths. Start with the minimal template:

```bash
cp configs/bare_bones.toml my_firmware.toml
```

It is deliberately almost empty. Replace `binary` with your ELF and update
the QEMU path if it is not at the repository default. The remaining choices
can be made through `fastdyn help`.

## Start from an ELF when possible

For an ELF firmware, generate a better starting point first:

```bash
./fastdyn-env/bin/python tools/elf2config/elf_to_config.py build/my_firmware.elf \
  --output configs/my_firmware.toml
```

The utility recovers the ELF architecture, word size, entry point, loadable
writable memory, Cortex-M vector table, ARM ABI CPU metadata, and recognizable
RTOS/MCU hints. It deliberately does not invent a board or a complete RAM map;
for Cortex-M it uses the `classic` model for the conventional MMIO window as a
reviewable default. Review the comments in the output and continue with the
steps below; [`tools/elf2config/README.md`](../tools/elf2config/README.md)
documents its limits and overwrite behavior.

## 1. Choose the CPU target

Run:

```bash
fastdyn help platforms
```

Choose **CPU architecture / QEMU target**, then select the closest target.
For a generic Cortex-M board, choose the exact Cortex-M CPU model. Paste the
returned block into `[[CPU.cpu0]]` in `my_firmware.toml`:

```toml
[[CPU.cpu0]]
arch = "arm"
machine = "cortexm"
cpu = "cortex-m33"
```

The patched generic `cortexm` QEMU machine currently exposes Cortex-M0, M3,
M4, M7, M33, and M55. Its CPU model must match the firmware's ISA/features.

## 2. Choose an SVD platform when applicable

For an ARM microcontroller with CMSIS-SVD data, run:

```bash
fastdyn help platforms
```

Choose **CMSIS-SVD device platform**, walk vendor and product family, and paste
the selected value into `[Machine]`:

```toml
[Machine]
platform = "STM32F429"
```

`generic-cortexm` is an intentional fallback when no appropriate SVD exists.
RISC-V and x86 targets generally do not use a CMSIS-SVD platform.

## 3. Set the QEMU machine

Run:

```bash
fastdyn help machine
```

Choose **headless QEMU** and copy the returned `[Machine]` settings, then
set `qemu_path` only if you want a QEMU other than the one FastDyn was built
against (it is optional; see [Configuration](Configuration.md#machine)). The starter
already contains a conservative headless choice, so this step mainly makes the
available runtime controls discoverable.

## 4. Set memory

Run:

```bash
fastdyn help memory
```

Choose **primary file-backed RAM** and paste its fields into `[Memory.main]`.
Adjust the address and size to the firmware's memory map. Add another bank only
when the board needs one.

```toml
[Memory.main]
base_address = "0x20000000"
memory_size = "256K"
memory_file = "/tmp/my_firmware.ram"
```

## 5. Set the firmware binary

Run:

```bash
fastdyn help firmware
```

Choose **ELF firmware** and paste the returned settings into `[[CPU.cpu0]]`.
Set the ELF path and the initial vector/entry address:

```toml
[[CPU.cpu0]]
binary = "build/my_firmware.elf"
init_nsvtor = "0x08000000"
```

Read the surrounding configuration reference if the target needs additional
RAM banks, semihosting, a board-specific QEMU executable, or boot settings:
open the platform browser and select its **Documentation** entry.

```bash
fastdyn help platforms
```

## 6. Add peripheral handling

Run:

```bash
fastdyn help device-models
```

Choose a model and paste its emitted TOML. For example, the classic model
handles a peripheral range in software:

```toml
[Device.Models.classic]

[Device.unmapped_peripherals]
ranges = [["0x40000000", "0x5fffffff"]]
description = "Peripherals handled by the classic model."

[[Device.unmapped_peripherals.handlers]]
model = "classic"
enabled = true
```

Use multiple `[Device.<name>]` tables when different regions need different
models. Read the specific model documentation before using hardware
passthrough or a trace-backed model: open the device-model browser and select
its **Documentation** entry.

```bash
fastdyn help device-models
```

## 7. Add an optional run-wide plugin

Run:

```bash
fastdyn help plugins
```

For example, choose VariableWatch and paste its plugin table:

```toml
[CPU.cpu0.plugins.variable_watch]
enabled = true
variable = "motor_state.temperature"
access = "write"
```

Each plugin owns its settings below `[CPU.cpu0.plugins.<name>]`; FastDyn core
does not add plugin-specific command-line flags. Choose **Documentation** in
the plugin browser to locate the relevant plugin and preprocessing material.

## 8. Add a trigger action or modifier when needed

Run either:

```bash
fastdyn help virtuals
fastdyn help modifiers
```

Virtuals invoke named FastDyn behavior when a guest PC executes. Modifiers are
direct register or memory updates at a guest PC. The picker emits the complete
TOML form for the selected choice. Select **Documentation** inside either
picker when deciding between the two:

```bash
fastdyn help virtuals
fastdyn help modifiers
```

## 9. Run it

Use a dedicated work directory so generated virtual rules, plugin artifacts,
and logs stay together:

```bash
fastdyn run -c my_firmware.toml -o fastdyn_work
```

If the configuration fails, start by checking the copied CPU target, memory
map, ELF path, and initial vector/entry address. The generated work directory
contains the resolved `virtuals.txt`, `modifiers.txt`, logs, and any selected
plugin artifacts. [RunningFastDyn.md](RunningFastDyn.md) contains a
slide-ready explanation of this workflow, primary options, and the artifacts
to inspect afterwards. The same guidance is available in the terminal through
`fastdyn help run`.
