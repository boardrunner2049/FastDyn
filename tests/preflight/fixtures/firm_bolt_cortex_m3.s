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
  .word Default_Handler
  .word Default_Handler
.size g_pfnVectors, . - g_pfnVectors

.section .text, "ax", %progbits
.align 2
.global Reset_Handler
.type Reset_Handler, %function
.thumb_func
Reset_Handler:
  movs r0, #7
  bl bounded_worker
  movs r0, #5
  bl cfg_worker
  movs r0, #3
  bl spill_worker
  movw r0, #0x5680
  movt r0, #0x1234
  movs r2, #0xfe
  movw r4, #0x8001
  movt r4, #0x1234
  movw r6, #0xff80
  bl byte_half_spill_worker
  movw r0, #0x3344
  movt r0, #0x1122
  bl scaled_sp_rebase_worker
  movs r0, #2
  bl tbb_worker
  movs r0, #3
  bl tbh_worker
  movs r0, #1
  bl tbb_register_base_worker
  movs r0, #2
  bl tbb_detached_worker
  movs r0, #0
  bl profiled_worker
  movw r1, #48
  movt r1, #0x2000
  str r0, [r1]
.global fixture_halt
fixture_halt:
  b fixture_halt
.size Reset_Handler, . - Reset_Handler

.global bounded_worker
.type bounded_worker, %function
.thumb_func
bounded_worker:
  push {r4, lr}
  ldr r4, =fixture_constant
  ldr r1, [r4]
  adds r0, r0, r1
  cmp r0, #42
  ite hi
  subhi r0, r0, #1
  addls r0, r0, #1
  movw r1, #0
  movt r1, #0x2000
  str r0, [r1]
  pop {r4, pc}
.size bounded_worker, . - bounded_worker

.global cfg_worker
.type cfg_worker, %function
.thumb_func
cfg_worker:
  b 1f
1:
  adds r0, r0, #9
  cmp r0, #14
  beq 2f
  movs r0, #0
2:
  movw r1, #4
  movt r1, #0x2000
  str r0, [r1]
  bx lr
.size cfg_worker, . - cfg_worker

.global spill_worker
.type spill_worker, %function
.thumb_func
spill_worker:
  sub sp, sp, #8
  movs r2, #17
  str r2, [sp, #0]
  ldr r2, [sp, #0]
  adds r0, r0, r2
  add sp, sp, #8
  movw r1, #8
  movt r1, #0x2000
  str r0, [r1]
  bx lr
.size spill_worker, . - spill_worker

/* Exercise width-correct frame forwarding with values whose upper bits make
   a plain register move observably wrong.  All four stores remain observable;
   only the corresponding reloads may become flag-preserving extensions. */
.global byte_half_spill_worker
.type byte_half_spill_worker, %function
.thumb_func
byte_half_spill_worker:
  /* Force Thumb-2's unscaled wide immediate so large-frame SP tracking and
     narrow-width forwarding are execution-checked together. */
  sub.w sp, sp, #1024
  strb.w r0, [sp, #0]
  ldrb.w r1, [sp, #0]
  strb.w r2, [sp, #1]
  ldrsb.w r3, [sp, #1]
  strh.w r4, [sp, #2]
  ldrh.w r5, [sp, #2]
  strh.w r6, [sp, #4]
  ldrsh.w r8, [sp, #4]
  movw r12, #28
  movt r12, #0x2000
  str r1, [r12, #0]
  str r3, [r12, #4]
  str r5, [r12, #8]
  str r8, [r12, #12]
  add.w sp, sp, #1024
  bx lr
.size byte_half_spill_worker, . - byte_half_spill_worker

/* The first store and later load name the same entry-SP-4 word through two
   different SP values. Thumb-1 encodes each adjustment in words, not bytes. */
.global scaled_sp_rebase_worker
.type scaled_sp_rebase_worker, %function
.thumb_func
scaled_sp_rebase_worker:
  sub sp, sp, #16
  str r0, [sp, #12]
  add sp, sp, #16
  sub sp, sp, #4
  ldr r1, [sp, #0]
  movw r2, #44
  movt r2, #0x2000
  str r1, [r2]
  add sp, sp, #4
  bx lr
.size scaled_sp_rebase_worker, . - scaled_sp_rebase_worker

.global tbb_worker
.type tbb_worker, %function
.thumb_func
tbb_worker:
  cmp r0, #3
  bhi 5f
  tbb [pc, r0]
1:
  .byte (2f - 1b) / 2
  .byte (3f - 1b) / 2
  .byte (4f - 1b) / 2
  .byte (5f - 1b) / 2
2:
  movs r0, #20
  b 6f
3:
  movs r0, #21
  b 6f
4:
  movs r0, #22
  b 6f
5:
  movs r0, #23
6:
  movw r1, #12
  movt r1, #0x2000
  str r0, [r1]
  bx lr
.size tbb_worker, . - tbb_worker

.global tbh_worker
.type tbh_worker, %function
.thumb_func
tbh_worker:
  cmp r0, #3
  bhi 5f
  tbh [pc, r0, lsl #1]
1:
  .hword (2f - 1b) / 2
  .hword (3f - 1b) / 2
  .hword (4f - 1b) / 2
  .hword (5f - 1b) / 2
2:
  movs r0, #40
  b 6f
3:
  movs r0, #41
  b 6f
4:
  movs r0, #42
  b 6f
5:
  movs r0, #43
6:
  movw r1, #16
  movt r1, #0x2000
  str r0, [r1]
  bx lr
.size tbh_worker, . - tbh_worker

.global tbb_register_base_worker
.type tbb_register_base_worker, %function
.thumb_func
tbb_register_base_worker:
  cmp r0, #3
  bhi 5f
  adr.w r2, 1f
  tbb [r2, r0]
1:
  .byte (2f - 1b) / 2
  .byte (3f - 1b) / 2
  .byte (4f - 1b) / 2
  .byte (5f - 1b) / 2
2:
  movs r0, #30
  b 6f
3:
  movs r0, #31
  b 6f
4:
  movs r0, #32
  b 6f
5:
  movs r0, #33
6:
  movw r1, #20
  movt r1, #0x2000
  str r0, [r1]
  bx lr
.size tbb_register_base_worker, . - tbb_register_base_worker

.global tbb_detached_worker
.type tbb_detached_worker, %function
.thumb_func
tbb_detached_worker:
  sub sp, sp, #4
  str r0, [sp, #0]
  ldr r0, [sp, #0]
  ldr r0, [sp, #0]
  ldr r0, [sp, #0]
  ldr r0, [sp, #0]
  cmp r0, #3
  bhi 5f
  adr.w r2, 7f
  tbb [r2, r0]
1:
2:
  movs r0, #50
  b 6f
3:
  movs r0, #51
  b 6f
4:
  movs r0, #52
  b 6f
5:
  movs r0, #53
  b 6f
7:
  .byte (2b - 1b) / 2
  .byte (3b - 1b) / 2
  .byte (4b - 1b) / 2
  .byte (5b - 1b) / 2
6:
  movw r1, #24
  movt r1, #0x2000
  str r0, [r1]
  add sp, sp, #4
  bx lr
.size tbb_detached_worker, . - tbb_detached_worker

/* A count-neutral hot/cold inversion fixture for ordinary profile-driven
   BOLT block layout. Both arms return, so ext-tsp can make the trained hot
   arm fall through without adding a compensating branch. */
.global profiled_worker
.type profiled_worker, %function
.thumb_func
profiled_worker:
  cmp r0, #0
  beq 1f
  movs r0, #1
  bx lr
1:
  movs r0, #2
  bx lr
.size profiled_worker, . - profiled_worker

.global Default_Handler
.type Default_Handler, %function
.thumb_func
Default_Handler:
  bx lr
.size Default_Handler, . - Default_Handler

.section .rodata, "a", %progbits
.align 2
fixture_constant:
  .word 35
