.syntax unified
.cpu cortex-m3
.thumb

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
  movs r0, #0
  bl peephole_worker
  movw r1, #0
  movt r1, #0x2000
  str r0, [r1]
.global fixture_halt
fixture_halt:
  b fixture_halt
.size Reset_Handler, . - Reset_Handler

.global peephole_worker
.type peephole_worker, %function
.thumb_func
peephole_worker:
  cmp r0, #0
  beq .Lhop
  movs r0, #9
  bx lr
.Lhop:
  b .Ltarget
.Ltarget:
  movs r0, #5
  bx lr
.size peephole_worker, . - peephole_worker
