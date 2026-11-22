# FirmBOLT compatibility of the application images

The Pye training run exercised the MicroPython bytecode interpreter’s indirect switch 6,822 times. This Thumb-2 switch loads PC directly from an indexed word table. FirmBOLT’s register-branch ABS32 matcher does not recognize that instruction. The native full-recipe report marked the interpreter address-stable and ignored. Its aggregate stability flag does not identify this switch as its sole cause.

## Inputs and observed instructions

[inspection.json](inspection.json) contains executable section addresses and hashes, whole-ELF hashes, static instruction counts and sample disassembly addresses. The paired source builds have identical executable section addresses, lengths and bytes with and without retained relocations. The flight inputs are the FastDyn repository’s ArduCopter 4.6.2 and PX4 v1.17.0 ELFs. The ESCape32 rows describe the release-configuration rebuild. Its loaded bytes differ from the upstream distributed BIN. grblHAL uses its normal PlatformIO board build. Pye uses the MPS2_AN385 release runtime. The application manifests identify source revisions, toolchains and scenarios.

| Input ELF | TBB sites | TBH sites | ADR then indexed LDR to PC | Calls to GCC Thumb-1 case helpers | Relocations targeting allocated sections, original / metadata |
|---|---:|---:|---:|---:|---:|
| ArduCopter | 295 | 54 | 58 | 0 | 0 / unavailable |
| PX4 | 171 | 26 | 26 | 0 | 0 / unavailable |
| grblHAL | 20 | 10 | 7 | 0 | 0 / 8,048 |
| ESCape32 | 0 | 0 | 0 | 16 | 0 / 1,891 |
| Pye runtime | 26 | 8 | 12 | 0 | 0 / 10,988 |

These columns count instructions decoded by GNU objdump in executable sections. ADR includes its GNU alias `add Rd, pc, #imm`. All indexed PC loads with shift two in these images have an immediately preceding ADR to the same base register. Helper call counts cover direct calls to symbols named `__gnu_thumb1_case_*`. Other switch lowerings can exist. GNU objdump reported a missing `sh_link` on Pye’s `.ARM.exidx` in both builds. The Pye dispatch observations and table relocations referenced here are in `.text`.

## Matched training observation

The native-MPS2 UART scenario `update-package-version` passed its document oracle and exited normally. The metadata ELF and profile hashes matched the run receipt. The profile spans guest reset through exit, including boot and the editor. Its 1,807,537 TB entries include two of the twelve indexed PC-load sites. The counts below sum entries to TBs ending at the dispatch PC. Static destinations mask the Thumb low bit. An earlier passing capture of the same job totaled 2,021,958 TB entries and had identical counts at both dispatch sites. Total reset-to-exit TB counts vary with UART polling.

| Function | Dispatch PC | Table start | Slots / ABS32 relocations | Distinct static destinations | Observed destinations | TB entries |
|---|---:|---:|---:|---:|---:|---:|
| mp_execute_bytecode | 0x1a4d6 | 0x1a4dc | 90 / 90 | 72 | 48 | 6,822 |
| mp_vprintf | 0x1904 | 0x1908 | 33 / 33 | 7 | 1 | 175 |

At 0x1a4ce the interpreter compares its index to 89 and branches above that bound to 0x1a7d4. At 0x18fe the printer compares to 32 and branches above to 0x18e2. Every table slot has an odd address in the owning function and an input `R_ARM_ABS32` relocation. The training trace covers only part of either target set. In grblHAL’s metadata ELF, `get_datetime` at 0x080157ca uses the same instruction pair and an unsigned bound of three. Its four slots at 0x080157d0 each have an `R_ARM_ABS32` relocation. No matching application training trace was used for this or the flight and ESCape32 rows.

On the input image the plugin classified 10,250 direct calls from `qstr_find_strn` (0xe00) to `strncmp` (0x2108e) as cross-page. The corresponding calls from the interpreter to `mp_binary_op` (0x1a800 to 0xdd90), `mp_obj_get_type` (0x1af12 to 0xeb84), and `mp_load_global` (0x1af52 to 0xd174) occurred 927, 857 and 740 times. Each source instruction disassembles as a direct BL to the recorded address. These are pre-layout observations on this training profile.

## Backend revision and placement

FirmBOLT `881dfdb5a7897f964a2219fb77f2b1f25e4a6a08` applies its Cortex-M patch to LLVM `7864c0736a8a09670dfbfc513f1e9649799f2486`.

- `ARMMCPlusBuilder::analyzeThumbAbsoluteJumpTable` accepts a bounded register dispatch ending in `mov pc, Rt` or `bx Rt`. The matcher tracks the indexed word load, index scaling and literal base. `BinaryFunction` checks the table extent, target decoding and function ownership. Recognition can preserve an embedded table at its original address even without saved input relocations. Detached table promotion requires a complete input relocation graph and an explicit atomic update contract.
- The backend also handles native TBB/TBH. The checked-in Thumb tests exercise guarded promotion, preservation of the original dispatch, and refusal when guest-instruction-count preservation is mandatory.
- The Pye and grblHAL instruction pair uses `ldr.w pc, [base, index, lsl #2]` directly. Its lack of an intermediate target register excludes it from the ABS32 matcher above. An individual Thumb-1 case-helper call also requires separate analysis before treating it as a bounded switch.
- `FirmBOLTFirmwarePasses.cpp` already keeps address-stable callers at their original address in relocation mode. `--firmbolt-keep-input-entry-min-calls` traverses their direct calls and tail calls to retain the input entry of selected moved callees. It uses caller block counts when available and otherwise checks the callee entry count, an upper bound. `--reorder-functions=tb-page` packs profiled functions into guest pages of configurable size. The front-end tests passed all 20 cases, including flag forwarding.
- The historical Trebuchet address 0x10000 lies inside Pye’s existing executable section [0x0, 0x305e4). Evaluating a region requires both the board memory map and space available to this linked image. The findings above do not establish a time saving, a preferred region or a winning guest image.

[Backend patch and pinned LLVM source](https://github.com/boardrunner2049/firmbolt/tree/881dfdb5a7897f964a2219fb77f2b1f25e4a6a08)

## Native publication and execution

The pinned FirmBOLT backend passed the embedded ABS32, detached ABS32 (eligible and rejected), native Thumb jump-table promotion and live-flags refusal tests. Direct application processing then revealed two independent input issues. The metadata ELF has an explicitly sized 64-byte `isr_vector` object in mixed executable/data `.text` at zero. Eleven relocations occupy that object. Its ABI `$d` marker starts at 0 and its next code marker is at 0x40. It also has an ABS32 linker-boundary reference to `_sidata` at 0x20d94. GNU ld defined that zero-sized symbol in an empty, nonallocated `.ARM.exidx` section sharing the loaded `.data` start.

The local backend overlay (SHA-256 `f94d0d776ebb572f3db0f4c39e4ecc78a891b8492cf709fa6901559af4cbbe68`, native tool `296a4683f651480506284025558abe99d88e0ba1718a1a24971d076388b7a1b0`) qualifies the mixed vector’s object bounds, data mapping, stack and handler targets. It checks the full-width, zero-addend fixed data boundary on every use and preserves the symbol and its retained relocation. It also distinguishes the retained original code section from a newly allocated code section during linking and retains the correct ELF section index for original-only relocation sites. Mixed relocation sites requiring two target sections are refused. [native-checks.json](native-checks.json) records the local FirmBOLT revision, tool hash, fixture hashes and canonical load equivalence. All nine native test cases passed their pinned RUN and FileCheck sequences, including the two new cases covering object bounds, observable bytes, retained symbol identity and refusals. The LLVM assembler and FileCheck were version 20, and LLD was 22.1.0.

The full profiled recipe retained 60 address-stable input functions and kept six moved callees’ input entries. Its final verifier rejected the `gc_mark_subtree` literal reference at input 0x2f4 after block layout changed its observable offset. A targeted recipe restricted to `strncmp` passed the Pye training job’s complete returned-document oracle at 0x60000 and 0x80000. In the canonical 0x80000 diagnostic, 14 relocated TB start addresses accounted for 72,796 entries. The moved executable section is 66 bytes, while the second PT_LOAD maps 198,184 bytes. This placement adds the full segment to the guest load footprint. The comparison uses the same guest image input and training job at each address. It establishes execution through relocated code, without determining which placement performs better.

The canonical corrected ELF SHA-256 is `d38a3d05a53b079e3821f311da97e7ade10a586f5adbc11747f9f0cd64aee016`. Its two PT_LOAD payloads, addresses, lengths and flags match the earlier qualified ELF `caab33a72ffc62ce66712d653ab31f13b3b920ad9640cd05caa23a568aee28eb` byte for byte. The earlier `.rel.text` header incorrectly named moved `.text` as its target. In the corrected image all 10,988 relocation sites fall inside the section identified by `sh_info`, `.bolt.org.text`.

## Bounded native layout follow-up

The private FirmBOLT follow-up commit `3b3f1165d1433f981a5be01ff920248273528ce0` corrected allocation of the original ARM text section during detached firmware relocation. JITLink needs the section's ID to resolve its fixups. The code allocator also used that ID as evidence of a new code allocation and copied the input section behind the moved function. The native writer now maps that input-backed section at its input address. An ABS32 section relocation at a sized data object immediately after a Thumb function also required correction. In MicroPython the reference at 0x2d6e4 must contain the data address 0x1bdb0.

| Training image | Moved function | New flash address | Previous extra PT_LOAD | Corrected extra PT_LOAD |
| --- | --- | ---: | ---: | ---: |
| MicroPython/Pye MPS2 | `strncmp` | 0x80000 | 198,184 B | 68 B (66 B code) |
| PX4 FMU v6x | `memset` | 0x081f6800 | 1,922,016 B | 156 B (154 B code) |

The native replay retained every allocated section, symbol/string table, static relocation payload, DWARF section and retained load byte from each previous candidate. The shorter PX4 range ends at 0x081f689c inside board flash. The two-byte tails in the new loads are zero alignment padding. These are separately built training candidates. The frozen Pye runtime matrix uses the earlier ELF. The executed bounded Pye image passed the complete editor oracle on stock and patched QEMU, with 72,796 entries across 14 distinct relocated TB start PCs. PX4 passed its uninstrumented mission and disarmed. The archived native replays and the corresponding executed images differ only in their unallocated tool note, which contains output paths. [Pye layout evidence](pye-bounded-load.json) · [PX4 layout evidence](px4-bounded-load.json)

Use `python check_load_layout.py PREVIOUS_ELF CANDIDATE_ELF` with pyelftools and Python 3.10 or newer to repeat both coverage checks. The final native binary SHA-256 is `39d570481b98887dfef7437ad795c3a9b2c145f8e7c1794640c9ba8ee250dadf`. The private overlay SHA-256 is `8ef629c94de5c85b48a8863596126bee7c396e8c7b15475d5eb9e6b5a547be44`.

## Reproduce the inspection

Install pyelftools and provide the GNU ARM disassembler. From this directory, pass any number of named ELFs. For matched profile counts, supply the directory containing `result.json` and `session.tbprof` from the Pye training runner.

```sh
PYE="$TREBUCHET_WORK/pye/micropython/ports/qemu/build-MPS2_AN385"
python inspect_images.py --objdump arm-none-eabi-objdump \
    --profile pye-runtime-metadata "$TREBUCHET_WORK/pye/firmbolt-training/run" \
    "pye-runtime=$PYE/firmware.elf" "pye-runtime-metadata=$PYE/firmware-relocs.elf"
arm-none-eabi-objdump -dr --start-address=0x1a4c4 --stop-address=0x1a644 "$PYE/firmware-relocs.elf"
arm-none-eabi-objdump -dr --start-address=0x18fa --stop-address=0x198c "$PYE/firmware-relocs.elf"
awk '$1 == "E" && ($2 == "e00" || $2 == "1a800" || $2 == "1af12" || $2 == "1af52")' \
    "$TREBUCHET_WORK/pye/firmbolt-training/run/session.tbprof"
```
