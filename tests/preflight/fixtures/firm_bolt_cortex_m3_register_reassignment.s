.syntax unified
.cpu cortex-m3
.thumb
.eabi_attribute Tag_CPU_arch, 10
.eabi_attribute Tag_CPU_arch_profile, 'M'

/* Executed qualification for target-aware BOLT register reassignment. The
   admitted contract treats R4/R8 as AAPCS callee-saved internal state and has
   no instruction-boundary register observer or interrupt inside worker. The
   final values of both registers remain part of the checked result. */
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
  movw r5, #0
  movt r5, #0x2000
  movs r0, #42
  str r0, [r5, #32]
  movs r4, #4
  movs r0, #8
  mov r8, r0
  add r1, r5, #32
  bl worker
  str r0, [r5]
  str r4, [r5, #4]
  mov r0, r8
  str r0, [r5, #8]
.global fixture_halt
fixture_halt:
  b fixture_halt
.size Reset_Handler, . - Reset_Handler

.global worker
.type worker, %function
.thumb_func
worker:
  sub sp, #8
  str.w r8, [sp, #4]
  mov.w r8, r1
  ldr.w r0, [r8, #0]
  str.w r0, [r8, #4]
  ldr.w r8, [sp, #4]
  add sp, #8
  bx lr
.size worker, . - worker
