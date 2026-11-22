# Pye terminal editor

Pye is a deployed MicroPython text editor. The performance job is an operator editing a real document through its serial terminal. The editor renders the screen, interprets keys, searches, modifies an in-memory buffer, prompts on exit, and returns the edited text through its documented list interface. The end-to-end result is the complete returned document. This scenario makes no claim about persistent storage writes. [Upstream application and usage](https://github.com/robert-hh/Micropython-Editor/tree/b64e20050416cd8770ca2cd6d1ffe736637da224)

The module is the binary distributed by the editor’s installation package. The unchanged runtime is the upstream MicroPython v1.27.0 QEMU port for MPS2-AN385, built with the board’s default 140 KiB interpreter heap and release optimization. This experimental upstream port runs a real Cortex-M3 interpreter and the modeled CMSDK APB UART. The editor is the product workload. It displays V2.80 although its pinned package manifest still says 2.78. [Upstream binary and installer](https://github.com/robert-hh/Micropython-Editor/blob/b64e20050416cd8770ca2cd6d1ffe736637da224/package.json) · [Runtime port and board](https://github.com/micropython/micropython/tree/78ff170de9e32c79db6e64d3e33d2bd60002bdcd/ports/qemu)

## Build and run

In the shared workspace, run the following from the FastDyn checkout. On another Linux host, replace the example paths and omit the container wrapper when the Arm GCC/binutils/newlib, C compiler, Make, Git and Python 3.11+ prerequisites are installed.

```sh
a=tools/trebuchet/applications/pye
w=/workspace/trebuchet-work/pye
"$a/prepare.sh" fetch "$w"
/workspace/trebuchet-shell "$a/prepare.sh" build "$w"
case_name=update-package-version
python3 "$a/session.py" stage --work "$w" --case "$case_name" --out "$w/cases-queued/$case_name"
elf="$w/micropython/ports/qemu/build-MPS2_AN385/firmware.elf"
/workspace/trebuchet-shell python3 "$a/cold.py" \
    --stage "$w/cases-queued/$case_name/stage.json" --elf "$elf" \
    --qemu /usr/bin/qemu-system-arm --out "$w/stock-qualification"
/workspace/trebuchet-shell python3 "$a/cold.py" \
    --stage "$w/cases-queued/$case_name/stage.json" --elf "$elf" \
    --qemu third_party/common/qemu/build/qemu-system-arm --out "$w/patched-qualification"
```

Both QEMUs accepted the package update and returned identical 166-byte documents, SHA-256 `379a2081d39ea4d038ba71f307375062ef6fd8199dc4f4d5bdef3936c4a8cb43`. The independent host oracle derives the expected bytes from the pinned original and the declared change. It also checks the version, active edit prompt, terminal negotiation and guest exit. The queued protocol produced identical full UART transcripts on both engines, SHA-256 `f4377ecd610eea3865caf5152194dad26b3c4b347784a573ce0b0402dbe674c4`. Raw streams and receipts appear in `qualification/{queued-stock,queued-patched}/`. Earlier reactive qualifications are preserved in `qualification/{stock,patched,fastdyn}/`. Their durations came from functional qualification on a loaded host and carry no speedup claim.

## Scenario and timing contract

The training, ranking, confirmation and final roles were frozen in `manifest.json`. A separate promotion job was frozen in `promotion.json` before ranking to compare a deployment with the pre-change incumbent. That incumbent has its own independent ranking and confirmation inputs in `prechange.json`. All source documents come from the pinned editor repository. The terminal scripts are authored operational workflows using the documented keys. The terminal is 24 rows by 80 columns, the board’s CPU and UART remain emulated, and the same read-only ROM filesystem contains the published module and each scenario’s input document. The peer verifies the document, module-bearing ROMFS, key sequence and oracle against the frozen hashes before serving. [Frozen inputs](qualification/case-identities.json)

| Role | Document | Useful work |
| --- | --- | --- |
| Train | package.json | Bring the package version into agreement with the displayed release using the replace dialog. |
| Pre-change rank | pye_lcd.py | Rotate the ST7789 display for the documented configurable mounting orientation. |
| Pre-change confirm | pye_ux.py | Document macOS support in the Unix terminal backend heading. |
| Rank | lcd_io.md | Find the wiring-instruction grammar error, navigate two words and delete the extra letter. |
| Confirm | peteensy.py | Go to the serial import line and append a descriptive comment. |
| Promote | strip.sh | Cut the malformed shebang and type the correct POSIX shell declaration. |
| Final | README.md | Correct the duplicated verb in the editor manual using the replace dialog. |

The primary elapsed-time interval begins when the host releases the ready guest and queues the terminal responses and editing keys in one write. It ends at the host-observed marker immediately after the application returns. This includes application initialization, terminal interaction, redraws and cleanup. Returning the document to the host for checking occurs after the interval. Per-process time and CPU include startup, staging inside the guest and the oracle transfer. The queued stream removes mid-job delays waiting for a host keystroke generator. Each terminal response is consumed by the guest UART in application order. Host scheduling still affects observed latency, so timing comparisons need paired, interleaved runs on otherwise idle CPUs. No equation between whole-process CPU and CPU spent exclusively inside the editor is assumed.

The queued peer receipt pins `protocol-queued.json`. The operational observer rejects the earlier reactive qualifications, which waited for individual dialogs before sending their answers. Two reactive training runs attributed different cost to UART polling despite the same final byte stream. The role inputs were frozen before ranking, and all arms in a timed comparison must run the same queued protocol.

The standalone peer runs `session.py peer --stage STAGE.json --socket PATH --out NEW_DIR`. It binds and listens at a fresh Unix socket before QEMU connects its UART. A passing peer receipt covers the application. The calling runner must also check the emulator exit and the proof appropriate to its cache mode. Direct cold qualification uses the same board, kernel, semihosting, icount settings, UART and input PSRAM address on each QEMU. The app’s return uses the upstream port’s `machine.reset()` semihosting exit. The fully warm campaign constructs its inventory over all declared cases, including final. Final scoring is reserved for the frozen deployment. That track makes no unseen-path generalization claim.

## Guest profiling and placement

```sh
/workspace/trebuchet-shell "$a/firmbolt.sh" profile "$w" \
    "$w/cases-queued/update-package-version/stage.json" "$w/firmbolt-training"
/workspace/trebuchet-shell "$a/firmbolt.sh" optimize "$w" \
    "$w/cases-queued/update-package-version/stage.json" "$w/firmbolt-deployment" \
    "$w/firmbolt-training"
```

The build relinks the original object files to retain relocations and verifies byte-for-byte equality of the complete loadable binary. The profile covers a full training launch through guest exit, including UART polling, REPL command compilation and oracle output. Its weighted per-function instruction figures use translated block lengths. They are estimates when an exception can interrupt a block. The whole-session profile observed 2.02 million TB executions and about 0.34 million indirect or cross-page transitions. The largest instruction attribution was to `qstr_find_strn` (37.3%). Those figures alone cannot establish that optimizing this function will reduce editing latency. These host-reactive diagnostics are preserved in `qualification/training-profile/`. The queued input used for guest optimization is a separate training run in `qualification/queued-profile/`; it recorded 1.80 million TB executions and 0.21 million indirect or cross-page transitions. All profile instrumentation stays out of scored comparisons.

To separate interpreter startup and oracle transfer from the editing job, add `--diagnostic-editor-trace NEW_TRACE` to the training-only cold launch and run `python3 "$a/editor_trace.py" RUN_DIR`. QEMU disables chaining to expose each executed TB and records the modeled UART transmitter's individual writes. The parser verifies the UART stream against the peer’s complete transcript. This diagnostic counted 817,525 TB entries before the ready marker, 1,640 through the begin marker, 928,581 through the editor return marker and 19,108 through exit. During editing, 451,950 entries (48.7%) were in `qstr_find_strn`, followed by `gc_info` (85,392), `mp_execute_bytecode` (60,269) and `mp_map_lookup` (55,263). These are guest TB entry counts from one instrumented training run. They identify active code and cannot attribute host elapsed time. [Per-function counts and identities](qualification/editor-trace/attribution.json)

The initial pinned backend rejected relocations on the vector table and halted during symbol analysis. Its invocation and failure appear in `qualification/firmbolt-initial/`. After repairs to ELF input and emission, the broad recipe still refused functions with observable literal-slot addresses. The retained narrow recipe targets `strncmp`, a frequently called cross-page callee of `qstr_find_strn` in training. The emitted image has 66 bytes of new executable text at `0x80000`, with the old entry redirected. The new load segment occupies 198,184 bytes and contains an additional copy of most input code. Its full extent counts toward image and board-memory footprint. In the separate training diagnostic, 72,796 TB entries reached 14 distinct relocated PCs. A separate UART-segmented trace observed 21,498 entries inside the 66-byte relocated code during editing itself. Uninstrumented cold release runs on both stock and patched QEMU returned all 166 expected bytes and reproduced the original UART transcript exactly. The canonical emission repairs the retained static relocation section association. Its load segment addresses, sizes, flags and bytes match the earlier narrow image exactly. A fresh canonical cold run on stock and patched QEMU, and a separate candidate TB profile, passed. [Canonical image identity and qualification](qualification/firmbolt-canonical/identity.json) · [Original narrow candidate and editor phase trace](qualification/firmbolt-narrow/identity.json)

The link map reserves 4 MiB at zero with the stack growing down from `0x400000`. The static allocation ends at `0x53914`, including the interpreter heap. The guest-image pipeline requests new code at `0x80000` and rejects load segments ending above `0x200000`. This leaves at least 2 MiB below the stack top. The pipeline requires relocated executable bytes, the same full document and normal uninstrumented exit, and diagnostic evidence that a TB in the relocated code executed. [Upstream linker script](https://github.com/micropython/micropython/blob/78ff170de9e32c79db6e64d3e33d2bd60002bdcd/ports/qemu/mcu/arm/mps2.ld)

A separate training-only build bounded the second load segment at 68 bytes while retaining the 66-byte executable range. The ELF hash is `19faf6446f5ee8d30788667ad8eb6891000f1ff48e52ed40b4bcda552700c463`. On both stock and patched QEMU the editor returned the identical full UART transcript and exited successfully. Training again observed 72,796 entries at 14 distinct relocated TB starts. The timed campaigns use their previously frozen image. [Bounded-load diagnostic](qualification/firmbolt-bounded/executed-layout.json)
