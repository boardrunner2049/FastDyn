  .syntax unified
  .thumb
  .eabi_attribute Tag_CPU_arch, 10
  .eabi_attribute Tag_CPU_arch_profile, 'M'

  .section .text.reset,"ax",%progbits
  .globl Reset_Handler
  .type Reset_Handler,%function
  .thumb_func
Reset_Handler:
  bl pcrel_worker
  .globl fixture_halt
fixture_halt:
  b fixture_halt
  .size Reset_Handler, .-Reset_Handler

  .section .text.spacer,"ax",%progbits
  .globl spacer
  .type spacer,%function
  .thumb_func
spacer:
  bx lr
  .size spacer, .-spacer

  .section .text.worker,"ax",%progbits
  .globl pcrel_worker
  .type pcrel_worker,%function
  .thumb_func
pcrel_worker:
.Lmovw_place:
  movw r0, :lower16:(external_data - .)
  movt r0, :upper16:(external_data - .)
  mov r1, pc
  sub.w r1, r1, #12
  add r0, r1
  ldr r3, [r0]
  movw r2, #0
  movt r2, #0x2000
  str r3, [r2]
  bx lr
  .size pcrel_worker, .-pcrel_worker

  .section .rodata,"a",%progbits
  .p2align 2
  .globl external_data
external_data:
  .word 42

  .section .isr_vector,"a",%progbits
  .globl g_pfnVectors
g_pfnVectors:
  .word 0x20001000
  .word Reset_Handler
  .word 0
  .word 0
