# ESCape32 admission

**The ESCAPE1 release and normal source rebuild pass a closed-loop RC-car job on the modeled bench.** The released ESCAPE1 rev17.3 controller and BOOT2_PA2 rev4 loader target the STM32G071 on the Axen 60A and Hybrid 60A boards. The published binaries and a source rebuild both reached the bootloader clock initialization on FastDyn’s generic Cortex-M machine. Four QMP samples per image found PC `0x080000f4` testing the PLL-ready bit. The preceding load read zero from RCC_CR at `0x40021000`. The source rebuild maps this instruction to `boot/mcu/STM32G0/config.c:28`. [Upstream target table](https://github.com/neoxic/ESCape32/wiki/Targets) · [Boot initialization at the tested revision](https://github.com/neoxic/ESCape32/blob/1d718c143380c3eb7e6581478990e1496632e5b8/boot/mcu/STM32G0/config.c#L20-L29)

## Run the ESCAPE1 board

The opt-in machine uses the STM32G071GB flash and SRAM map, flash boot alias, HSI16 and PLL clock switching, gated GPIO/USART2, TIM1/TIM6 update and one-pulse events, and bit-reversed hardware CRC with polynomial 0x04c11db7. The normal bootloader opens a half-duplex UART on PA2. The operator sends the vendor probe, requests the 32-byte board identity, and reads both 1 KiB configuration blocks. The host checks the complete identity, length/complement framing, CRC32 and every returned byte against the original configuration stored in flash. Both firmware pairs returned the same configuration SHA-256, `218d9994e22d701a48fb0012645b0aaedee1e75265a1d1adef9f04f0abf98cb6`. [Receipts](observations/board.json) · [Vendor loader protocol](https://github.com/neoxic/ESCape32/tree/1d718c143380c3eb7e6581478990e1496632e5b8/boot)

Without an operator the bootloader jumps to the application after its receive timeout. Both binaries executed their own reset handler and programmed DMAMUX channels for TIM15 capture, USART1 RX/TX and the ADC. The application enabled the ADC regulator, set the clock to 16 MHz and ran TIM6 at 64 MHz with PSC 0 and ARR 1279. At the calibration request the one-pulse timer had stopped, UIF was set and 20,320 virtual ns had elapsed since regulator enable. The trace and oracle bind this observation to the exact flash and QEMU binary. The earlier startup probe ended at ADCAL. The motor model continues through ADC readiness and real DMA conversions. [STM32G0 reference manual RM0444](https://www.st.com/resource/en/reference_manual/rm0444-stm32g0x1-advanced-armbased-32bit-mcus-stmicroelectronics.pdf)

Run these commands from FastDyn, with a configured QEMU build, Ninja, its original compiler and the previously prepared images. Use fresh output directories for each invocation.

```sh
W=/workspace/trebuchet-work/escape32
D=tools/trebuchet/applications/escape32/board
python3 "$D/build.py" third_party/common/qemu/build "$W/escape1-qemu"
Q="$W/escape1-qemu/qemu-system-arm"
python3 "$D/maintenance.py" "$Q" "$W/release/BOOT2_PA2-rev4.bin" "$W/release/ESCAPE1-rev17.3.bin" "$W/release-maint"
python3 "$D/startup.py" "$Q" "$W/release-maint/flash.bin" "$W/release-start"
python3 "$D/maintenance.py" "$Q" "$W/build/boot/BOOT2_PA2-rev4.bin" "$W/build/ESCAPE1-rev17.3.bin" "$W/rebuilt-maint"
python3 "$D/startup.py" "$Q" "$W/rebuilt-maint/flash.bin" "$W/rebuilt-start"
```

The build reuses native QEMU objects and supplies a board-local correction that wakes the UART backend when the guest drains the receive register. Both probes start a fresh process. The machine uses QEMU's Cortex-M0 ARMv6-M core for the physical Cortex-M0+, the selected G07x device ID, and a modeled 100 µs PLL lock. RCC prescalers follow the exercised divide-by-one path. Diagnostic instruction counting and RCC/TIM6 virtual time support functional ordering only. The motor job exercises throttle capture, interrupt dispatch, phase outputs and sensor feedback.

## Reproduce the reset diagnosis

Run `prepare.sh fetch` with Git, curl and sha256sum. Run the build and probes with Python 3.11+, CMake, GNU Make, the Arm bare-metal GCC/binutils/newlib toolchain, and the FastDyn QEMU build. From the FastDyn checkout in the shared workspace:

```sh
export ESC_WORK=/workspace/trebuchet-work/escape32
tools/trebuchet/applications/escape32/prepare.sh fetch "$ESC_WORK"
/workspace/trebuchet-shell tools/trebuchet/applications/escape32/prepare.sh build "$ESC_WORK"
for image in release rebuild; do
    /workspace/trebuchet-shell python3 tools/trebuchet/applications/escape32/probe.py \
        "$ESC_WORK" third_party/common/qemu/build/qemu-system-arm --image "$image"
done
```

The fetch step checks the release binary hashes. The build step uses the original upstream CMake board targets with the default `-Os -g` options, pins libopencm3, and supplies no application source changes. The release assets and the rebuilt images have different bytes. A second application ELF retains relocations for FirmBOLT. The build compares its entire flash payload with the default source build. That comparison passed. The older mixed-O0 ESCape32 ELF in FirmBOLT’s qualification corpus has independent provenance.

Each probe creates a fresh output directory containing the QEMU command, both input hashes, machine register observations, translated-block trace and process log. It returns after four diagnostic samples. A successful diagnostic run carries no operational performance result. Both image pairs contain the matching bootloader at `0x08000000` and the application configuration at `0x08001000`. The application vectors begin at `0x08001800`. The release probe wraps each published flash image in Intel HEX to preserve those load addresses within the generic loader’s size limits.

## Operational admission boundary

The upstream RC-car scenario uses forward/brake/reverse mode. Throttle entering reverse first applies the brake. The motor controller must receive real command edges, drive the motor phases, and respond to changing rotor speed, load and current. The upstream documentation describes command behavior and inertial loading, but supplies no timestamped motor/ESC capture for this target in the tested sources. [Operating scenarios](https://github.com/neoxic/ESCape32/wiki/TipsAndTricks) · [Configuration interface](https://github.com/neoxic/ESCape32/wiki/Configuration)

The generic machine runs a Cortex-M0 at a fixed 168 MHz. ESCAPE1 targets a Cortex-M0+ with a 64 MHz operating clock. The bootloader needs a responding clock controller. The application then needs the TIM6 one-pulse timer, ADC calibration and conversions, TIM15 input capture and DMA, TIM1 phase outputs, TIM2 comparator crossings or the hall path, and the enabled interrupts. The repository’s existing flight-controller plant does not supply this board interface. [Board initialization](https://github.com/neoxic/ESCape32/blob/1d718c143380c3eb7e6581478990e1496632e5b8/mcu/STM32G071/config.c) · [Target clock and timers](https://github.com/neoxic/ESCape32/blob/1d718c143380c3eb7e6581478990e1496632e5b8/mcu/STM32G071/config.h)

## Closed-loop RC motor qualification

The bench connects ESCAPE1 to a maxon EC-max 16, part 283828, using its Hall sensors. The published release and normal rebuild retain their original bootloader and program bytes. Staging changes three documented settings in the writable configuration page. The settings are `throt_mode=2`, `damp=0` and `telem_mode=1`. The first two follow the upstream proportional-brake RC-car instructions. The last enables automatic KISS telemetry. The receipts record changed offsets and hashes before and after staging. [Operating instructions](https://github.com/neoxic/ESCape32/wiki/TipsAndTricks) · [Settings](https://github.com/neoxic/ESCape32/wiki/Settings)

A 50 Hz servo supplies neutral for arming, then ramps forward. A viscous load of 4 µN·m·s/rad engages while the forward command remains constant. The first reverse command engages proportional braking. After neutral, the second reverse command drives the shaft backward. The job ends at rest after 430 frame starts. Firmware consumes TIM15 capture edges, 16 kHz SysTick, 1 kHz ADC scans through DMA4, TIM1 phase gates and compare values, and GPIO Hall feedback on PA15/PB3/PB5 with TIM3 edge capture. USART1 transmits actual KISS frames through DMA3. The independent watchdog runs with a modeled 32 kHz oscillator.

The 12 V, 5 W motor data are 15.7 Ω, 0.254 mH, 7.8 mN·m/A, 1220 rpm/V, one pole pair, 0.428 g·cm² inertia, and 60.2 mA no-load current at 13,500 rpm. The 2025 catalog PDF SHA-256 is `f0b6540c859f75f262c69478d2110436878f71dff40563e6e39413627fbc8772`. The averaged electrical model integrates line current with the winding time constant. It computes signed torque from the enabled terminal pair, shaft angle and phase EMF. A 50 µs mechanical step integrates inertia, the external load and viscous drag estimated from the catalog no-load point. The Hall edges follow shaft angle. Proportional braking loads the motor windings. [Manufacturer product data](https://www.maxongroup.com/maxon/view/product/283828) · [Catalog PDF](https://www.maxongroup.com/medias/sys_master/root/9399778476062/Cataloge-Page-EN-279.pdf)

Run the motor job after the board-build and maintenance commands above. It stages a private configuration copy and emits raw events, KISS bytes, and a JSON receipt.

```sh
python3 "$D/motor.py" "$Q" "$W/release-maint/flash.bin" "$W/release-motor"
python3 "$D/motor.py" "$Q" "$W/rebuilt-maint/flash.bin" "$W/rebuilt-motor"
```

The external oracle decodes positive and negative phase terminals from raw output mode/enable register values and verifies the cyclic sequence in both directions. It computes settled speed independently from the catalog equilibrium equation, allowing 15% for waveform shape and mechanical-loss assumptions. It checks every telemetry CRC8, matches absolute KISS eRPM to the simulated shaft within 150 rpm, checks supply voltage and temperature, and requires braking before reverse and a final stop. [Oracle](board/motor.py) · [Both receipts and raw observations](observations/motor)

The release produced +2727 rpm unloaded, +1415 rpm loaded and −1857 rpm in reverse. The independent predictions were +2802, +1439 and −1912 rpm. Both tested images emitted 255 intact KISS frames and completed 8080 sensor scans. These are simulated operating points. The phase EMF, viscous drag, ideal 12 V source, calibration words, sensor offsets, Hall alignment and load fixture are model assumptions. The oracle covers the exercised Hall-sensored speed range. Silicon timing, individual transistor waveforms, heating, sensorless operation and physical RC-car behavior remain unvalidated. QEMU’s Cortex-M0 ARMv6-M implementation ignores SLEEPONEXIT. Performance measurements require separate deployment, input-role and timing qualification.

## Training and frozen-cache qualification

The training job uses the same 430-frame RC-car scenario. Its policy permits two guest recipes and one profile. The full Cortex-M0+ recipe runs first. The second restricts the same recipe to the trained free-standing application helper with the heaviest entry coverage. The profile records 193.00 million guest instructions and 60.89 million TB entries. The hot startup-music callback `delayf/2` has 4.14 million entries. Original full and narrowed backends both failed to encode a forward Thumb-1 PC-relative literal load. An intermediate fixed-order callback linked, but its old entry contained Thumb-2 B.W and faulted on the G071 at frame 71. [Training policy](board/training-policy.json) · [Raw profiles and rejection receipts](observations/motor/training)

With `FIRMBOLT_REPO` pointing to the native private checkout, `board/training.sh profile ESC_WORK BOARD_QEMU NEW_OUTPUT` builds the normal-exit TB profiler and checks the complete motor job. `board/training.sh optimize ESC_WORK BOARD_QEMU NEW_OUTPUT PROFILE_DIR` applies both recipes, checks physical/virtual guest mappings and bounded flash extent, then reruns the motor oracle and checks that relocated code executed. The 64 KiB allocation begins at physical address 0x08010000. The application’s vectors, XIP flash and initialized SRAM load images stay inside the STM32G071 memory map.

On a fixed-ASLR host, `board/frozen.py BOARD_QEMU BASE_FLASH NEW_OUTPUT` populates an ordinary native FastDyn QEMU supercache from training, seals it, checks its digest against a fresh writable sparse copy and runs one full training replay. Its proof checks the deployed header, inode, record bytes and exact count of subsequently translated records. Each process emits the entire application trace and actual serial data, CPU time, elapsed time and cache identity. This known-input training job provides no ranking or reserved-final comparison. [Replay driver](board/frozen.py)

The repaired callback recipe places 48 bytes at 0x08010000 and rebinds the original typed Thumb callback pointer at 0x080087ac. Its relocated body is byte-identical to the original body. Across 14 relocated TB addresses the independent execution profile counted 16,577,657 entries, including 4,140,400 at the function entry. The transformed firmware completed the full cold and instrumented motor jobs and emitted byte-identical KISS telemetry to the normal rebuild. Both input and linked output identities, original rejection, and the distinction between executable and debug metadata qualification are retained. [Linked image and dynamic receipt](observations/motor/training/executed.json)

One known-input replay per image ran on the same fixed-ASLR host. Both trials redeployed the sealed prefix and emitted the same 255 CRC-valid KISS packets. The counts below include every late native translation. Full QEMU process times include startup, the 430-frame job and teardown. A single training observation per image provides no held-out speedup or selection. [Raw case, hashes, elapsed and CPU costs](observations/motor/frozen/44996734243.json)

| Guest | Captured records | New records on replay | Replay wall | Replay CPU |
| --- | ---: | ---: | ---: | ---: |
| Normal source release | 3,411 | 0 | 7.075 s | 7.060 s |
| FirmBOLT callback relocation | 3,420 | 176 | 7.124 s | 7.112 s |

The original also replayed with zero appended records on a separate fixed-ASLR run. The FirmBOLT job passed the training-frozen protocol and retained 176 late translations in its record proof. Fully warmed execution of this transformed guest remains unqualified. Each raw evidence archive and sparse sealed inventory was downloaded and checked before its temporary hosted run and branch were removed. [Independent original replay](observations/motor/frozen/44996606690.json)
