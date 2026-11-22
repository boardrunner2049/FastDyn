# grblHAL motion-controller qualification

The published Open Source Hardware logo drawing exercises the grblHAL G-code parser, planner, stepper ISR and serial protocol. The BTT STM32F103RC firmware and upstream Linux simulator both use core revision `4b6a3485`. The simulator completed the job. Firmware admission remains blocked on the BTT device model. Pinned sources, runtime boundaries and image hashes are in [manifest.json](manifest.json).

## Reproduce

Run from this directory on Linux x86-64 with Git, curl, Python 3 with venv/pip, CMake 3.19+, make, a C compiler and GNU coreutils. Choose a fresh directory for each run. The build takes the upstream PlatformIO release environment, pins its resolved packages, and builds a second ELF with linker relocation records. The script checks that both flashed images are byte-identical.

```bash
work=$PWD/work-grblhal
./prepare.sh "$work"
python3 run_simulator.py "$work" "$work/native-run"
./probe.sh "$work" /path/to/patched/qemu-system-arm "$work/reset-probe"
```

The native simulator runs the unmodified core and driver with the default approximately real-time peripheral scheduler. A diagnostic linker wrapper records the simulated driver's logical GPIO step requests. Each byte records the three axis step bits in the low nibble and the latched direction bits in the high nibble. The simulator's GPIO driver omits physical pulse-width timing. Keep this diagnostic out of performance comparisons.

The runner streams the complete published file through simulated UART, keeps the sender connected until the final dwell acknowledgment, and requests the simulator's documented clean shutdown. The controller restores default settings after its initial blank-EEPROM diagnostic. The oracle checks all acknowledgments, every planner endpoint against the absolute metric coordinates, and every ordered request against the direction and digital straight-line path of its segment. At 250 steps/mm an exact half-step permits either nearest integer. The minor-axis line error may reach one step owing to rasterization. Startup/reset, lost commands, missing blocks, motion beyond the endpoint, and unfinished jobs fail the check.

The real-time run accepted 101 commands and reached 96 successive planner endpoints. It issued 125,104 logical requests, comprising 72,414 X steps, 55,226 Y steps and 3,359 Z steps. Final position was (0, 0, 953) steps. The recorded transcript, block endpoints, position samples, compressed ordered step requests and receipt are in [observations/native](observations/native). An earlier unlimited-clock run produced the same request digest; a second timed out at 100 of 101 acknowledgments. Two preliminary pipe/EOF launches also stalled. These runs informed sender diagnostics only. No final scenarios were consumed and no speedup was measured.

## ARM qualification

The actual release image starts at 0x08007000 after the board bootloader. It needs 256 KiB flash and 48 KiB SRAM, UART1 command transport, UART3/TMC2209 drivers, nonvolatile settings, and timer/GPIO semantics. On this board TIM2 drives step interrupts. Step outputs use PB13, PB10 and PB0; directions use PB12, PB2 and PC5. The patched QEMU's STM32F100 discovery model provides 128 KiB flash and 8 KiB SRAM and declares the GPIO banks, RCC and TIM2 unimplemented. A launch on that model locked up before a usable reset.

The generic Cortex-M3 diagnostic configures the real vector address and 48 KiB SRAM. Its cold trace reaches `Reset_Handler`, clock setup and `driver_init`. Two sampled PCs were at 0x08009df6 with R4 at RCC base 0x40021000; the third was at 0x08027f26 inside driver initialization. It did not produce a BTT command session or observed pin train. The snapshot and compressed instruction trace are in [observations/reset](observations/reset). The firmware still needs a qualified peripheral implementation or hardware-backed replay with input states, timer IRQs, step direction and pulse evidence. Cache capture, optimizer admission and an ARM end-to-end measurement remain open.
