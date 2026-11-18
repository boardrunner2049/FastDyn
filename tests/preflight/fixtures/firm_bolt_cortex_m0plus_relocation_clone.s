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
.size vector_table, .-vector_table

.section .text,"ax",%progbits
.balign 4
.global Reset_Handler
.type Reset_Handler,%function
.thumb_func
Reset_Handler:
  movs r0, #0
  bl shared_relocation_worker
  ldr r2, ram_address
  str r0, [r2]
  movs r0, #1
  bl shared_relocation_worker
  ldr r2, ram_address
  str r0, [r2, #4]
.global fixture_halt
fixture_halt:
  b fixture_halt
.balign 4
ram_address:
  .word 0x20000000
.size Reset_Handler, .-Reset_Handler

.global shared_relocation_worker
.type shared_relocation_worker,%function
.thumb_func
shared_relocation_worker:
  cmp r0, #0
  beq .Lsecond
.Lfirst:
  ldr r1, .Lliteral
  ldr r0, [r1]
  b .Lguard
.Lsecond:
  ldr r1, .Lliteral
  ldr r0, [r1]
  b .Lguard
.Lguard:
  b .Lafter_pool
  .p2align 2
.Lliteral:
  .word external_object
.Lafter_pool:
  .rept 520
  nop
  .endr
  bx lr
  .p2align 2
.global __firmbolt_thumb_literal_capacity0
.type __firmbolt_thumb_literal_capacity0,%object
__firmbolt_thumb_literal_capacity0:
  .word 0
  .word 0
.size __firmbolt_thumb_literal_capacity0, .-__firmbolt_thumb_literal_capacity0
.size shared_relocation_worker, .-shared_relocation_worker

.section .rodata,"a",%progbits
.balign 4
.global external_object
.type external_object,%object
external_object:
  .word 0x12345678
.size external_object, .-external_object
