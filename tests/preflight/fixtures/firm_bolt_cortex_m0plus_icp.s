.syntax unified
.cpu cortex-m0plus
.thumb

/* Execute both the promoted direct path and the original cold BLX path.  The
   targets consume r0-r2, so a Thumb-1 guard that borrows an argument register
   without restoring it produces an observable mismatch. */
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
  ldr r4, .Lram

  movs r0, #1
  movs r1, #2
  movs r2, #3
  ldr r3, .Lhot_pointer
  bl indirect_caller
  str r0, [r4]

  movs r0, #4
  movs r1, #5
  movs r2, #6
  ldr r3, .Lcold_pointer
  bl indirect_caller
  str r0, [r4, #4]
.global fixture_halt
fixture_halt:
  b fixture_halt
  .align 2
.Lram:
  .word 0x20000000
.Lhot_pointer:
  .word promoted_target + 1
.Lcold_pointer:
  .word cold_target + 1
.size Reset_Handler, . - Reset_Handler

.align 2
.global indirect_caller
.type indirect_caller, %function
.thumb_func
indirect_caller:
  push {lr}
  blx r3
  b .Lreturn
  /* Symbol-bounded, unreachable capacity for compact publication. */
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
  adds r0, r1
  adds r0, r2
  adds r0, #7
  bx lr
.size promoted_target, . - promoted_target

.global cold_target
.type cold_target, %function
.thumb_func
cold_target:
  adds r0, r1
  adds r0, r2
  adds r0, #20
  bx lr
.size cold_target, . - cold_target
