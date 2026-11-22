# ESCape32 admission

**The dedicated ESCAPE1 model passes configuration readback and reaches ADC calibration in motor application startup.** The released ESCAPE1 rev17.3 controller and BOOT2_PA2 rev4 loader target the STM32G071 on the Axen 60A and Hybrid 60A boards. The published binaries and a source rebuild both reached the bootloader clock initialization on FastDyn’s generic Cortex-M machine. Four QMP samples per image found PC `0x080000f4` testing the PLL-ready bit. The preceding load read zero from RCC_CR at `0x40021000`. The source rebuild maps this instruction to `boot/mcu/STM32G0/config.c:28`. [Upstream target table](https://github.com/neoxic/ESCape32/wiki/Targets) · [Boot initialization at the tested revision](https://github.com/neoxic/ESCape32/blob/1d718c143380c3eb7e6581478990e1496632e5b8/boot/mcu/STM32G0/config.c#L20-L29)

## Run the ESCAPE1 board

The opt-in machine uses the STM32G071GB flash and SRAM map, flash boot alias, HSI16 and PLL clock switching, gated GPIO/USART2, TIM1/TIM6 update and one-pulse events, and bit-reversed hardware CRC with polynomial 0x04c11db7. The normal bootloader opens a half-duplex UART on PA2. The operator sends the vendor probe, requests the 32-byte board identity, and reads both 1 KiB configuration blocks. The host checks the complete identity, length/complement framing, CRC32 and every returned byte against the original configuration stored in flash. Both firmware pairs returned the same configuration SHA-256, `218d9994e22d701a48fb0012645b0aaedee1e75265a1d1adef9f04f0abf98cb6`. [Receipts](observations/board.json) · [Vendor loader protocol](https://github.com/neoxic/ESCape32/tree/1d718c143380c3eb7e6581478990e1496632e5b8/boot)

Without an operator the bootloader jumps to the application after its receive timeout. Both binaries executed their own reset handler and programmed DMAMUX channels for TIM15 capture, USART1 RX/TX and the ADC. The application enabled the ADC regulator, set the clock to 16 MHz and ran TIM6 at 64 MHz with PSC 0 and ARR 1279. At the calibration request the one-pulse timer had stopped, UIF was set and 20,320 virtual ns had elapsed since regulator enable. The trace and oracle bind this observation to the exact flash and QEMU binary. The model holds ADCAL pending at this boundary. [STM32G0 reference manual RM0444](https://www.st.com/resource/en/reference_manual/rm0444-stm32g0x1-advanced-armbased-32bit-mcus-stmicroelectronics.pdf)

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

The build reuses native QEMU objects and supplies a board-local correction that wakes the UART backend when the guest drains the receive register. Both probes start a fresh process. The machine uses QEMU's Cortex-M0 ARMv6-M core for the physical Cortex-M0+, the selected G07x device ID, and a modeled 100 µs PLL lock. RCC prescalers follow the exercised divide-by-one path. Diagnostic instruction counting and RCC/TIM6 virtual time support functional ordering only. Motor qualification still requires ADC acquisitions through DMA, throttle capture, inverter outputs, timed feedback and a measured or independently validated load/response oracle.

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

A future admission requires independently checked command acceptance, phase commutation order and duty, telemetry, and rotor response to the declared load, brake and reversal. A closed-loop evaluation requires a qualified electrical/mechanical plant. A replay evaluation requires a board trace with event ordering and a separate output oracle. Neither baseline has been demonstrated here. Ordinary-warm capture, optimization, ranking and final timing therefore remain open.
