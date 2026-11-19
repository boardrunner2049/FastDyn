.syntax unified
.cpu cortex-m3
.thumb
.eabi_attribute Tag_CPU_arch, 10
.eabi_attribute Tag_CPU_arch_profile, 'M'

/* Executed qualification for target-aware BOLT register reassignment. The
   admitted contract treats R4/R8 as AAPCS callee-saved internal state and
   separately admits R2/R3/R12 caller-saved exchange only when ABI-aware
   liveness proves both exact names dead at every entry. There is no
   instruction-boundary register observer or interrupt inside either accepted
   worker. Final values and a live-in rejection control are checked. */
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
  .fnstart
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
  add r1, r5, #32
  bl aggressive_worker
  str r0, [r5, #12]
  movs r2, #5
  movs r3, #7
  add r1, r5, #32
  bl aggressive_live_in_worker
  str r0, [r5, #16]
.global fixture_halt
fixture_halt:
  b fixture_halt
  .cantunwind
  .fnend
.size Reset_Handler, . - Reset_Handler

.global worker
.type worker, %function
.thumb_func
worker:
  .fnstart
  sub sp, #8
  str.w r8, [sp, #4]
  mov.w r8, r1
  ldr.w r0, [r8, #0]
  str.w r0, [r8, #4]
  ldr.w r8, [sp, #4]
  add sp, #8
  bx lr
  .cantunwind
  .fnend
.size worker, . - worker

.global aggressive_worker
.type aggressive_worker, %function
.thumb_func
aggressive_worker:
  .fnstart
  mov.w r12, r1
  ldr.w r0, [r12, #0]
  str.w r0, [r12, #4]
  bx lr
  .cantunwind
  .fnend
.size aggressive_worker, . - aggressive_worker

/* Both available low caller-saved names are live at entry, so neither may be
   exchanged with R12 even though doing so would shorten the load. */
.global aggressive_live_in_worker
.type aggressive_live_in_worker, %function
.thumb_func
aggressive_live_in_worker:
  .fnstart
  add.w r0, r2, r3
  mov.w r12, r1
  ldr.w r1, [r12, #0]
  add r0, r1
  bx lr
  .cantunwind
  .fnend
.size aggressive_live_in_worker, . - aggressive_live_in_worker
