# Guest instruction fragments

FastDyn can insert bounded guest-ISA instruction bytes into generated TCG code immediately before an existing firmware instruction. The firmware image, the original instruction address, and the original instruction's plugin identity are unchanged. Fragment operations are not guest instructions for icount or plugin enumeration. They may, of course, change the path taken by the firmware afterward.

## API and lifecycle

Native runtime plugins register during their initializer:

```c
static const uint8_t code[] = { 0x25, 0xff, 0, 0, 0, 0x83, 0xc0, 7 };
static const FastDynCodeSpec spec = {
    FASTDYN_GUEST_ISA_X86,
    FASTDYN_CODE_MODE_X86_64,
    FASTDYN_INSN_ORDER_LITTLE_ENDIAN,
};
uint64_t hook;
if (!virtual_insert_before(ctx, 0x100120, code, sizeof(code), &spec, &hook)) {
    return -1;
}
```

`virtual_insert_before` copies the descriptor and bytes. The caller may release or alter its buffer after the call. A successful non-null output receives a nonzero opaque ID; failures set it to zero and install nothing. Registration is sealed by the first guest translation and later calls fail. Registrations survive ordinary TB invalidation and retranslation.

The scope is the FastDyn execution, numeric guest PC, and complete encoding profile. Rules at one numeric address for different modes coexist but never reinterpret one another. Matching fragments execute in registration order. The current facility is shared by all vCPUs using the translated-code cache; it has no per-vCPU variants.

Limits are 128 decoded instructions and 2048 input bytes for one fragment and for all matching fragments at one site/profile. Both limits are checked independently. A fragment offset is diagnostic metadata only and never consumes a guest address. If the original instruction at `P` is `L` bytes long, its ordinary fallthrough remains the architecture-defined `P + L`.

## Profiles and admitted scalar forms

All forms are decoded by the current QEMU target frontend and emitted into the same TCG stream as the original instruction. The classifiers reject memory operands, PC-relative/literal access, control transfers, exceptions, system/privileged operations, atomics, floating point, and vectors.

| Profile | Address/order rules | Initial admitted forms |
| --- | --- | --- |
| A32 | 32-bit address, 4-byte aligned; active instruction order; `AL` condition | register/immediate AND, EOR, SUB, ADD, TST/TEQ, CMP/CMN, ORR, MOV, BIC/MVN; immediate shifts in operand 2; MOVW/MOVT |
| Thumb | 32-bit address, 2-byte aligned; active instruction order | 16-bit shifts, add/sub, move/compare, and register data-processing; selected Thumb-2 shifted-register and modified-immediate AND/ORR/EOR/ADD/SUB/compare forms, ADDW/SUBW, SBFX/UBFX |
| A64 | 4-byte aligned; little-endian instruction bytes | add/sub immediate and register forms; logical immediate and shifted-register forms; MOVZ/MOVN/MOVK; bitfield shifts and extensions |
| x86-16/32/64 | any byte address; 16/32-bit profiles require a 32-bit address; little-endian | register-only MOV, ADD, OR, AND, SUB, XOR, CMP, TEST; immediate group forms; immediate logical/arithmetic shifts; MOVZX/MOVSX and long-mode MOVSXD. Operand-size and REX prefixes, partial registers, and long-mode 32-bit zero extension retain x86 semantics |
| RV32/RV64 | 2-byte aligned; RV32 requires a 32-bit address; little-endian | LUI, base OP/OP-IMM integer arithmetic, logical operations, shifts, SLT/SLTU predicates; RV64 OP-32/OP-IMM-32 word forms; selected compressed integer forms when C/Zca is active |

AArch32 rejects a matching insertion on an IT instruction or in active IT/ECI/ICI continuation state. A64 keeps register 31's operand-dependent ZR/SP meaning and W writes zero-extend X registers. x86 retains the frontend's lazy condition-code state and rewrites original instruction-start restoration metadata after flag-changing fragments. RISC-V resets operation width before every fragment instruction and retains x0 and RV64 word-result behavior.

Fragments run when execution reaches the site, before the original instruction's semantics. An exception retry that reaches the site again runs the fragment again. A fragment is not promised exactly once per retired original instruction.

The existing start-of-instruction observer runs first, followed by matching
fragments in registration order, followed by the original instruction. Known
read-only observers remain usable at the same PC. A program-counter modifier,
detour, or callback without the explicit read-only observer classification is
rejected before the TB executes; this prevents a late control-state operation
from invalidating the inline fragment's placement contract.

## Example and byte regeneration

The shared external plugin is in `tests/plugins/guest_code_fragments/`. Its host preprocessor writes a namespaced `settings.txt`; its native initializer selects committed bytes and calls the API. Configure it with `profile` (`a32`, `thumb`, `a64`, `x86_16`, `x86_32`, `x86_64`, `rv32`, or `rv64`) and `pc`.

The source fragments and `fragments/regenerate.sh` use GNU Arm tools, LLVM's target assemblers, and NASM. The script rejects unresolved relocations. These assemblers are development tools only and are not runtime dependencies.

The complete public-path example can be run with
`tests/integration/run_fastdyn_guest_code_fragment_smoke.sh`. It compiles the
single shared C plugin, registers from its native initializer, and executes a
Cortex-M fixture without modifying the firmware image.

## Building the architecture matrix

Apply the tracked QEMU patch series in order and build the matrix with:

```bash
source ./setup.sh --build-qemu \
  --qemu-targets arm-softmmu,aarch64-softmmu,x86_64-softmmu,riscv32-softmmu,riscv64-softmmu
```

The setup script and Dockerfile apply `qemu-fastdyn-plugin-icount.patch`,
`qemu-fastdyn-insn-fragments.patch`, `qemu-fastdyn-mmio.patch`,
`qemu-fastdyn-mmio-pc.patch`, and `qemu-fastdyn-constant-pc.patch` in order,
detect an already-applied series, and fail rather than reset an incompatible
checkout. A stale QEMU header fails the FastDyn build at the required
`qemu/fastdyn-fragments.h` ABI include; it cannot accept and silently omit
fragments.

The constant-PC optimization does not relax fragment placement: a fragment
and PC modifier at the same instruction remain a translation-time conflict.
The original TB extent and fragment/plugin instruction metadata are retained;
the chainable redirect is injected at the modifier's existing position.

Guest-memory fragments, branches and guest control transfers, arbitrary helpers, host-state bindings, live updates/removal, automatic register preservation, and a public IR/TCG interface are intentionally absent.

## Validation and diagnostics

Registration rejects null or empty buffers, invalid descriptors, malformed or
truncated encodings, unavailable target profiles, unrepresentable or misaligned
addresses, and individual or combined count/byte limit violations. It is atomic:
a valid prefix followed by a forbidden instruction installs nothing. Checks that
depend on the live frontend (CPU extensions, active mode, and ARM IT/ECI state)
are completed while translating the matching site; rejection identifies the
hook PC and fragment offset and terminates translation before affected guest
code can execute. There is no callback or interpreter fallback.

Normal TB invalidation does not change the immutable registry. Retranslation
emits one copy again, while plugin instruction handles, sizes, source PCs, and
guest instruction counts continue to describe only the original firmware.
