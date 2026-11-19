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
.align 1
.global hot_worker
.type hot_worker, %function
.thumb_func
hot_worker:
  movs r1, #0
  movs r2, #0
  adds r2, #1
  adds r2, #1
  adds r2, #1
.global hot_loop
hot_loop:
  adds r1, #1
  cmp r1, r0
  bne hot_loop
  mov r0, r1
  bx lr
.size hot_worker, . - hot_worker

.global Reset_Handler
.type Reset_Handler, %function
.thumb_func
Reset_Handler:
  movw r4, #0
  movt r4, #0x2000
  movs r0, #10
  bl hot_worker
  str r0, [r4]
.global fixture_halt
fixture_halt:
  b fixture_halt
.size Reset_Handler, . - Reset_Handler
