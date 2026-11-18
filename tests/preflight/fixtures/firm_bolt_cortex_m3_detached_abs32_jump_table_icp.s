/*
 * Executed Cortex-M3 fixture for atomic promotion of a detached, read-only
 * ABS32 switch table.  Unlike the in-function fixture, the pointer word and
 * all table entries retain R_ARM_ABS32 input relocations.  Firm-BOLT may
 * update the table only when the explicit detached-table contract is present.
 */
  .syntax unified
  .cpu cortex-m3
  .thumb

  .section .isr_vector,"a",%progbits
  .word 0x20001000
  .word Reset_Handler

  .text
  .p2align 2
  .global Reset_Handler
  .type Reset_Handler,%function
  .thumb_func
Reset_Handler:
  ldr r4, =0x20000000
  movs r0, #0
  bl detached_abs32_jump_table_worker
  str r0, [r4, #0]
  movs r0, #1
  bl detached_abs32_jump_table_worker
  str r0, [r4, #4]
  movs r0, #2
  bl detached_abs32_jump_table_worker
  str r0, [r4, #8]
  movs r0, #3
  bl detached_abs32_jump_table_worker
  str r0, [r4, #12]
  .global fixture_halt
fixture_halt:
  b fixture_halt
  .size Reset_Handler, .-Reset_Handler

  .p2align 2
  .global detached_abs32_jump_table_worker
  .type detached_abs32_jump_table_worker,%function
  .thumb_func
detached_abs32_jump_table_worker:
  cmp r0, #2
  bls .Ldispatch
  movs r0, #0
  bx lr
.Ldispatch:
  lsls r1, r0, #2
  ldr r2, .Ltable_pointer
  adds r2, r1, r2
  ldr r2, [r2]
  mov pc, r2
.Lcase0:
  movs r0, #10
  bx lr
.Lcase1:
  movs r0, #20
  bx lr
.Lcase2:
  movs r0, #30
  bx lr
  .p2align 2
.Ltable_pointer:
  .word .Ldetached_table
  /* Proven unreachable capacity for the checked in-place expansion. */
  .rept 16
  nop
  .endr
  .size detached_abs32_jump_table_worker, .-detached_abs32_jump_table_worker

  .section .rodata.detached_switch,"a",%progbits
  .p2align 2
.Ldetached_table:
  .word .Lcase0
  .word .Lcase1
  .word .Lcase2
