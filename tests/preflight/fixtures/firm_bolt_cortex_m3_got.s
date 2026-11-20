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
  // The minimal FastDyn fixture has no C runtime to copy initialized RAM from
  // its flash LMA.  Materialize the linked GOT slot and target object exactly
  // as reset code would before exercising the position-independent access.
  movw r1, #0x100
  movt r1, #0x2000
  movw r2, #0x200
  movt r2, #0x2000
  str r2, [r1]
  movs r3, #42
  str r3, [r2]
  bl got_worker
  movw r1, #0
  movt r1, #0x2000
  str r0, [r1]
.global fixture_halt
fixture_halt:
  b fixture_halt
.size Reset_Handler, . - Reset_Handler

.align 2
.global got_worker
.type got_worker, %function
.thumb_func
got_worker:
  ldr r0, .Lgot_delta
.Lgot_pc:
  add r0, pc
  ldr r0, [r0]
  ldr r0, [r0]
  bx lr
.align 2
.Lgot_delta:
  // R_ARM_GOT_PREL computes GOT(value)+A-P.  Compensate the runtime Thumb PC
  // so the ADD resolves the exact GOT slot, matching compiler PIC sequences.
  .long value(GOT_PREL)-((.Lgot_pc+4)-.Lgot_delta)
.size got_worker, . - got_worker

.section .data, "aw", %progbits
.align 2
.global value
.type value, %object
value:
  .long 42
.size value, . - value
