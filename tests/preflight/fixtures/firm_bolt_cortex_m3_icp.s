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
  movs r0, #10
  movw r3, :lower16:promoted_target + 1
  movt r3, :upper16:promoted_target + 1
  bl indirect_caller
  movw r1, #0
  movt r1, #0x2000
  str r0, [r1]
.global fixture_halt
fixture_halt:
  b fixture_halt
.size Reset_Handler, . - Reset_Handler

.global indirect_caller
.type indirect_caller, %function
.thumb_func
indirect_caller:
  push {lr}
  blx r3
  b .Lreturn
  /* Symbol-bounded, unreachable capacity. Compact publication may reuse it
     only after CFG analysis proves that it is neither an entry nor live. */
  .rept 16
  nop
  .endr
.Lreturn:
  pop {pc}
.size indirect_caller, . - indirect_caller

.global promoted_target
.type promoted_target, %function
.thumb_func
promoted_target:
  adds r0, #7
  bx lr
.size promoted_target, . - promoted_target
