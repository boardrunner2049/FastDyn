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
  movs r2, #0
  bl large_literal_worker
  ldr r1, ram_address
  str r0, [r1]
  movs r2, #1
  bl large_literal_worker
  ldr r1, ram_address
  str r0, [r1, #4]
.global fixture_halt
fixture_halt:
  b fixture_halt
.balign 4
ram_address:
  .word 0x20000000
.size Reset_Handler, .-Reset_Handler

.global large_literal_worker
.type large_literal_worker,%function
.thumb_func
large_literal_worker:
  push {r4, lr}
  movs r0, #7
  adr r4, .Lliteral0
  ldr r4, [r4]
  ldr r4, [r4]
  adds r0, r0, r4
  b .Lcode0
  .p2align 2
.Lliteral0:
  .word external_value
.Lcode0:
  .rept 520
  nop
  .endr
  cmp r2, #0
  beq .Lprimary_literal1
.Lalternate_literal1:
  ldr r3, .Lliteral1
  adds r0, r0, r3
  b .Lcode1
.Lprimary_literal1:
  ldr r3, .Lliteral1
  adds r0, r0, r3
  b .Lcode1
  .p2align 2
.Lliteral1:
  .word 5
.Lcode1:
  pop {r4, pc}
  .p2align 2
.global __firmbolt_thumb_literal_capacity0
.type __firmbolt_thumb_literal_capacity0,%object
__firmbolt_thumb_literal_capacity0:
  .word 0
  .word 0
.size __firmbolt_thumb_literal_capacity0, .-__firmbolt_thumb_literal_capacity0
.size large_literal_worker, .-large_literal_worker

.section .rodata,"a",%progbits
.balign 4
.global external_value
.type external_value,%object
external_value:
  .word 3
.size external_value, .-external_value
