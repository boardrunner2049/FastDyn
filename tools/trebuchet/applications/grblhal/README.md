# grblHAL motion-controller qualification

The published Open Source Hardware logo drawing exercises the grblHAL G-code parser, planner, stepper ISR and serial protocol. The BTT STM32F103RC firmware and upstream Linux simulator both use core revision `4b6a3485`. The simulator and a historical upstream BTT release completed the job. The pinned 2026 stock release hits a firmware fault during initialization. Pinned sources, runtime boundaries and image hashes are in [manifest.json](manifest.json).

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

The generic Cortex-M3 diagnostic configures the real vector address and 48 KiB SRAM. Its cold trace reaches `Reset_Handler`, clock setup and `driver_init`. Two sampled PCs were at 0x08009df6 with R4 at RCC base 0x40021000; the third was at 0x08027f26 inside driver initialization. It did not produce a BTT command session or observed pin train. The snapshot and compressed instruction trace are in [observations/reset](observations/reset). The ARM evidence below uses the dedicated BTT board model.

## BTT ARM drawing

The board model runs the normal Cortex-M3 image from its bootloader handoff address. It maps 256 KiB flash and 48 KiB SRAM, connects USART1 to the sender, routes TIM2 update/compare interrupts and records timestamped changes on the mapped GPIO banks. At power-on, the three NC limit contacts and the control inputs are grounded. Opening the contacts elicited the firmware's emergency-stop alarm during model bring-up. The EEPROM starts erased. The model implements the 24C32 two-byte address and 32-byte page write protocol and the addressed TMC2209 CRC, readback and write counter used by the firmware. USART3 replies occupy ten virtual bit-times per byte. RCC readiness transitions are immediate and the core clock runs at the final requested 72 MHz. QEMU's instruction-count clock models interrupt order and produces virtual pulse widths. The sender observes the grblHAL acknowledgment for each input line. This drawing uses no spindle motion. The model covers its PWM timer configuration; spindle waveform, loaded motor motion, switch bounce, EEPROM wear/write-cycle timing and physical pulse duration have no on-board measurement.

The release at driver `e1f83889` faults at `0x080294ae` before the banner. The optimized bound check in `aux_ctrl_get_fn` dereferences past four entries in `aux_ctrl` while scanning unassigned auxiliary inputs. The Cortex-M fault address register holds `0x2000c008`, eight bytes beyond SRAM. Moving the bound comparison ahead of the array read in the firmware source restored normal operation on the same board. This diagnostic build has its own SHA and passed the entire pin oracle. [Fault trace](observations/arm/stock-fault) · [Corrected-build receipt](observations/arm/source-fix/receipt.json)

The primary baseline uses driver `127db7f9` and its pinned core `0679a0c6`, immediately before the upstream auxiliary I/O refactor. Its historical PlatformIO file omitted the repository's board library and failed to link `board_init`. The preparation script adds the public upstream `boards` dependency from `4d2c917`. All firmware source files and board options retain their historical contents. Rebuilding with linker relocations produced identical flash bytes. [Image and source identities](manifest.json) · [Submodules and baseline build error](observations/arm/optimizer)

Over USART1 the historical firmware reported its STM32F103RC/BTT identity, 250 steps/mm on every axis and three Trinamic drivers. It accepted all 101 commands and acknowledged the final dwell. Its final idle position was (0, 0, 3.812) mm. The GPIO observer reads the port changes themselves and never supplies step requests. It recovered 125,104 ordered rise masks and directions, matching the independent upstream simulator exactly. The 130,999 individual pulses comprised 72,414 X, 55,226 Y and 3,359 Z steps. All rose while the corresponding motor was enabled and fell before shutdown. These runs recorded 3.368 µs virtual pulse highs with the firmware's nominal pulse setting at 5.0 µs. The evidence establishes edge ordering, enabled state and modeled widths. Hardware timing calibration remains open. The complete model run read 1,070 bytes and wrote 1,909 bytes of EEPROM. The three addressed TMC2209s exchanged 8/35, 8/35 and 5/19 valid read/write transactions. [UART, I/O, raw GPIO edges and FastDyn launch](observations/arm/normal)

The training profile contains 2.84 billion executed TBs over the drawing. FirmBOLT refused `protocol_exec_rt_system` because an observable Thumb literal address changed. It refused `state_get` because retained relocations span the original and moved text. The narrower trained candidate moved 360 bytes of `__mulsf3` to `0x08038000` inside the device's flash. All 121 vector-section words and the data/stack allocations remained identical. A second FastDyn profile observed 179,581 TB executions at 46 distinct relocated starts, including 46,409 visits to the entry. Both images completed the same UART and physical-pin oracle. [Execution and layout evidence](observations/arm/optimizer) · [Candidate trace](observations/arm/candidate)

From the application directory, run the following after the normal QEMU, FastDyn plugin and Python environments are built.

```bash
./prepare.sh "$work"
./prepare-board.sh "$work" historical
python3 board/build.py "$qemu_source/build" "$work/board"
elf="$work/release-historical/platformio/BTT_SKR_MINI_E3_V20/firmware.elf"
python3 run_board.py "$work/board/qemu-system-arm" "$elf" "$work/OSHW_Logo.nc" "$work/btt-run" --fastdyn-plugin "$plugin"
python3 board/check_pins.py "$work/btt-run"
./train-firmbolt.sh "$work" "$work/board/qemu-system-arm" "$plugin" "$work/training"
```

The negative controls remove a measured edge, change its direction, disable its motor and force a zero-width pulse. Each fails against the original measured input. [Negative controls](observations/arm/pin-negative-tests.txt)

The pinned profiler/ELF pair has matching flashed bytes. With the optimizer restricted to one named function, PERF2BOLT warned that 100% of its aggregated traces mismatched disassembly and retained nonempty profile data for one of 987 functions. Keep that warning when interpreting profile-guided layout quality. Raw guest-PC counts independently establish execution at the new addresses.

On an x86-64 Linux host that allows the process personality needed for fixed addresses, use the following command to run the bounded training comparison. It freezes an inventory per image after at most four complete preparations and runs three paired private deployments in AB/BA/AB order. The same UART sender, pin observer and isolated CPU are used for both images. A paired time comparison requires zero appends for every measured launch. All attempts retain FastDyn's exact input manifest, QEMU argv, application receipts and the deployed-prefix proof. The only workload here is the training drawing.

```bash
taskset -c "$cpu" "$fastdyn_root/scopt-env/bin/python" hosted_training.py \
    "$work/board/qemu-system-arm" "$plugin" "$work/OSHW_Logo.nc" \
    "$elf" "$work/training/firmware.elf" "$work/hosted-training"
```

The hosted training run completed fourteen drawing jobs on a single pinned CPU with fixed process addresses. After four preparations per image, normal had 5,797 retained translations and the FirmBOLT guest had 5,750. All six private deployments preserved the frozen cache prefix, manifest and header. Each required additional translations, so the fully warm acceptance criterion withheld a speed ratio. An independent check redecoded the first pair's timestamped GPIO logs and verified the application, exit and cache receipts for every trial. [Hosted audit](observations/arm/hosted/attempt2/audit.json) · [Raw application and cache proofs](observations/arm/hosted/attempt2/raw.tgz) · [Raw first-pair edges](observations/arm/hosted/attempt2/edges.tgz)

| Training pair | Normal wall time | Normal appends | FirmBOLT wall time | FirmBOLT appends |
| --- | ---: | ---: | ---: | ---: |
| 1, normal first | 28.892 s | 85 | 28.906 s | 93 |
| 2, FirmBOLT first | 28.891 s | 64 | 28.992 s | 70 |
| 3, normal first | 28.819 s | 74 | 28.924 s | 82 |

The first hosted attempt completed two drawings but replaced its cache because a random QMP socket changed the FastDyn configuration-file hash. The retry used one stable QMP endpoint and active cache pathname per image for freshly copied deployments. Both attempts' evidence was downloaded and checked against GitHub's artifact digest. The temporary branch, both runs and both artifacts were removed. [Rejected prefix proof](observations/arm/hosted/attempt1/proof.json) · [Verified cleanup](observations/arm/hosted/cleanup.json)
