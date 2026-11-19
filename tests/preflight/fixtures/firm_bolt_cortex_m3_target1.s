  .syntax unified
  .thumb
  .eabi_attribute Tag_CPU_arch, 10
  .eabi_attribute Tag_CPU_arch_profile, 'M'

  .section .isr_vector,"a",%progbits
  .p2align 2
  .globl g_pfnVectors
g_pfnVectors:
  .word 0x20001000
  .word Reset_Handler

  .section .text,"ax",%progbits
  .p2align 2
  .globl Reset_Handler
  .type Reset_Handler,%function
  .thumb_func
Reset_Handler:
  movw r3, :lower16:dispatch_slot
  movt r3, :upper16:dispatch_slot
  ldr r3, [r3]
  movs r0, #40
  blx r3
  movw r1, #0
  movt r1, #0x2000
  str r0, [r1]
  .globl fixture_halt
fixture_halt:
  b fixture_halt
  .size Reset_Handler, .-Reset_Handler

  .p2align 2
  .globl target_worker
  .type target_worker,%function
  .thumb_func
target_worker:
  adds r0, #2
  bx lr
  .size target_worker, .-target_worker

  .section .dispatch,"a",%progbits
  .p2align 2
  .globl dispatch_slot
  .type dispatch_slot,%object
dispatch_slot:
  .word target_worker(TARGET1)
  .size dispatch_slot, .-dispatch_slot
