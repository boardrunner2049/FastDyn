.syntax unified
.cpu cortex-m3
.thumb

/* Execute both the duplicated/propagated hot tail and the original cold
   branch.  The propagated definition is register-only and r2 is not an
   admitted live-out; r0 and r1 remain the ABI result registers. */
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
  movw r1, #0
  movt r1, #0x2000

  movs r0, #0
  bl taildup_propagate
  str r0, [r1]

  movs r0, #1
  bl taildup_propagate
  str r0, [r1, #4]
.global fixture_halt
fixture_halt:
  b fixture_halt
.size Reset_Handler, . - Reset_Handler

.global taildup_propagate
.type taildup_propagate, %function
.thumb_func
taildup_propagate:
  cmp r0, #0
  bne .Lcold
.Lhot:
  mov.w r2, #5
  b.w .Lshared
.Lcold:
  movs r0, #10
  bx lr
.Lshared:
  add.w r0, r0, r2
  bx lr
.size taildup_propagate, . - taildup_propagate

/* Function-relative no-LBR counts used by the reproducible runner. */
# FDATA: 1 taildup_propagate 0 100
# FDATA: 1 taildup_propagate 4 100
# FDATA: 1 taildup_propagate c 1
# FDATA: 1 taildup_propagate 10 100
