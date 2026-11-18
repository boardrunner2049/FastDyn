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
  bl loop_worker
  movw r1, #0
  movt r1, #0x2000
  str r0, [r1]
.global fixture_halt
fixture_halt:
  b fixture_halt
.size Reset_Handler, . - Reset_Handler

.global loop_worker
.type loop_worker, %function
.thumb_func
loop_worker:
  movs r1, #0
  b .Lloop_check
.Lloop_check:
  cmp r1, #4
  bge .Lloop_exit
.Lloop_body:
  adds r1, #1
  b .Lloop_check
.Lloop_exit:
  mov r0, r1
  bx lr
.size loop_worker, . - loop_worker

/* Function-relative no-LBR counts used by the reproducible runner. */
# FDATA: 1 loop_worker 0 100
# FDATA: 1 loop_worker 4 500
# FDATA: 1 loop_worker 8 400
# FDATA: 1 loop_worker c 100
