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
  bl inline_caller
  movw r1, #0
  movt r1, #0x2000
  str r0, [r1]
.global fixture_halt
fixture_halt:
  b fixture_halt
.size Reset_Handler, . - Reset_Handler

.global inline_caller
.type inline_caller, %function
.thumb_func
inline_caller:
  push {lr}
  bl inline_callee
  adds r0, #1
  pop {pc}
.size inline_caller, . - inline_caller

.global inline_callee
.type inline_callee, %function
.thumb_func
inline_callee:
  movs r0, #42
  bx lr
.size inline_callee, . - inline_callee
