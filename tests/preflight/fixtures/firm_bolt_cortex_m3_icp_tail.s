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
  movw r3, :lower16:promoted_tail_target + 1
  movt r3, :upper16:promoted_tail_target + 1
  bl tail_dispatch
  movw r1, #0
  movt r1, #0x2000
  str r0, [r1]
.global fixture_halt
fixture_halt:
  b fixture_halt
.size Reset_Handler, . - Reset_Handler

.global tail_dispatch
.type tail_dispatch, %function
.thumb_func
tail_dispatch:
  /* A terminal callee-saved restore is the conservative epilogue evidence
     used to distinguish this profiled BX from a computed intra-function
     jump. The push/pop memory effects remain in the installed candidate. */
  push {r4}
  pop {r4}
  bx r3
  /* Symbol-bounded, executable but unreachable capacity. It becomes usable
     only after BOLT proves the preceding BX is a tail call and removes this
     block as unreachable. */
  .rept 16
  nop
  .endr
.size tail_dispatch, . - tail_dispatch

.global promoted_tail_target
.type promoted_tail_target, %function
.thumb_func
promoted_tail_target:
  adds r0, #9
  bx lr
.size promoted_tail_target, . - promoted_tail_target
