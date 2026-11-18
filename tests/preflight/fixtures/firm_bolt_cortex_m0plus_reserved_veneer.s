.syntax unified
.cpu cortex-m0plus
.thumb

.section .isr_vector,"a",%progbits
.balign 4
.global vector_table
.type vector_table,%object
vector_table:
  .word 0x20001000
  .word Reset_Handler + 1
  .word default_handler + 1
  .word default_handler + 1
.size vector_table, .-vector_table

/* This range is a linker-declared non-entry capacity contract.  Firm-BOLT is
   permitted to replace it only as one complete, word-aligned veneer slot. */
.section .firmbolt.veneer,"ax",%progbits
.balign 4
.global __firmbolt_thumb_veneer_slot0
.type __firmbolt_thumb_veneer_slot0,%function
.thumb_func
__firmbolt_thumb_veneer_slot0:
  bx lr
  nop
  nop
  nop
.size __firmbolt_thumb_veneer_slot0, .-__firmbolt_thumb_veneer_slot0

.section .text,"ax",%progbits
.balign 4
.global Reset_Handler
.type Reset_Handler,%function
.thumb_func
Reset_Handler:
  movs r0, #41
  bl caller
  ldr r1, ram_address
  str r0, [r1]
.global fixture_halt
fixture_halt:
  b fixture_halt
.balign 4
ram_address:
  .word 0x20000000
.size Reset_Handler, .-Reset_Handler

.balign 2
.global caller
.type caller,%function
.thumb_func
caller:
  push {lr}
  bl target
  pop {pc}
.size caller, .-caller

.balign 2
.global filler
.type filler,%function
.thumb_func
filler:
  .rept 48
  adds r4, #1
  .endr
  bx lr
.size filler, .-filler

.balign 2
.global target
.type target,%function
.thumb_func
target:
  adds r0, #1
  bx lr
.size target, .-target

.balign 2
.global default_handler
.type default_handler,%function
.thumb_func
default_handler:
  b default_handler
.size default_handler, .-default_handler
