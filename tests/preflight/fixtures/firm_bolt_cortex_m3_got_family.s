.syntax unified
.cpu cortex-m3
.thumb
.eabi_attribute Tag_CPU_arch, 10
.eabi_attribute Tag_CPU_arch_profile, 'M'

.section .isr_vector, "a", %progbits
.align 2
.global g_pfnVectors
.type g_pfnVectors, %object
g_pfnVectors:
  .word 0x20001000
  .word Reset_Handler
.size g_pfnVectors, . - g_pfnVectors

.section .text, "ax", %progbits
.align 2
.global Reset_Handler
.type Reset_Handler, %function
.thumb_func
Reset_Handler:
  // The minimal runner does not perform initialized-data copy-down. Install
  // the exact linked GOT slot and object before using the PIC sequence.
  movw r1, #0x100
  movt r1, #0x2000
  movw r2, #0x200
  movt r2, #0x2000
  str r2, [r1]
  movs r3, #42
  str r3, [r2]
  bl got_family_worker
  movw r1, #0
  movt r1, #0x2000
  str r0, [r1]
.global fixture_halt
fixture_halt:
  b fixture_halt
.size Reset_Handler, . - Reset_Handler

.align 2
.global got_family_worker
.type got_family_worker, %function
.thumb_func
got_family_worker:
  ldr r2, .Lgot_base_delta
.Lgot_base_pc:
  add r2, pc
  adds r2, #18
  ldr r0, .Lgot_slot_offset
  add r0, r2
  ldr r0, [r0]
  ldr r0, [r0]
  ldr r1, .Lvalue_offset
  add r1, r2
  ldr r1, [r1]
  adds r0, r1
  bx lr
.align 2
.Lgot_base_delta:
  // R_ARM_BASE_PREL computes GOT_ORG+A-P. The preceding constant adjustment
  // converts the runtime Thumb PC back to this relocation place. The fixture
  // validator checks the encoded instruction address and relocation place;
  // LLVM MC cannot fold this cross-fragment label difference in a .if.
  .long _GLOBAL_OFFSET_TABLE_-.
.Lgot_slot_offset:
  // R_ARM_GOT_BREL computes G(value)+A-GOT_ORG.
  .long value(GOT)
.Lvalue_offset:
  // R_ARM_GOTOFF32 computes S+A-GOT_ORG.
  .long value(GOTOFF)
.size got_family_worker, . - got_family_worker

.section .data, "aw", %progbits
.align 2
.global value
.type value, %object
value:
  .long 42
.size value, . - value
