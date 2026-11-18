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
  movs r0, #0
  bl taildup_worker
  movw r1, #0
  movt r1, #0x2000
  str r0, [r1]
.global fixture_halt
fixture_halt:
  b fixture_halt
.size Reset_Handler, . - Reset_Handler

.global taildup_worker
.type taildup_worker, %function
.thumb_func
taildup_worker:
  cmp r0, #0
  bne .Ltaildup_cold
.Ltaildup_hot:
  movs r0, #1
  b.w .Ltaildup_shared
.Ltaildup_cold:
  movs r0, #10
  bx lr
.Ltaildup_shared:
  adds r0, #2
  bx lr
.size taildup_worker, . - taildup_worker

/* Function-relative no-LBR counts used by the reproducible runner. */
# FDATA: 1 taildup_worker 0 100
# FDATA: 1 taildup_worker 4 100
# FDATA: 1 taildup_worker a 1
# FDATA: 1 taildup_worker e 100
