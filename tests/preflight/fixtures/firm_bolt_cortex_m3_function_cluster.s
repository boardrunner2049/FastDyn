.syntax unified
.cpu cortex-m3
.thumb
.eabi_attribute Tag_CPU_arch, 10
.eabi_attribute Tag_CPU_arch_profile, 'M'

/* Equal-size worker functions isolate call-graph clustering from simple
   execution-count and density ordering.  worker_a is hottest, worker_b is
   second, and worker_c is reached from a lower-frequency block in worker_a. */
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
.global worker_a
.type worker_a, %function
.thumb_func
worker_a:
  cmp r0, #0
  bne .La_fast
  push {r4, lr}
.global worker_a_call_c
worker_a_call_c:
  bl worker_c
  pop {r4, pc}
.La_fast:
  movs r0, #17
  nop
  bx lr
.size worker_a, . - worker_a

.global worker_b
.type worker_b, %function
.thumb_func
worker_b:
  movs r0, #29
  .rept 6
  nop
  .endr
  bx lr
.size worker_b, . - worker_b

.global worker_c
.type worker_c, %function
.thumb_func
worker_c:
  movs r0, #42
  .rept 6
  nop
  .endr
  bx lr
.size worker_c, . - worker_c

.global Reset_Handler
.type Reset_Handler, %function
.thumb_func
Reset_Handler:
  movw r4, #0
  movt r4, #0x2000
  movs r0, #1
.global cluster_call_a_hot
cluster_call_a_hot:
  bl worker_a
  str r0, [r4]
  movs r0, #0
.global cluster_call_a_cold
cluster_call_a_cold:
  bl worker_a
  str r0, [r4, #4]
  movs r0, #1
.global cluster_call_b
cluster_call_b:
  bl worker_b
  str r0, [r4, #8]
.global fixture_halt
fixture_halt:
  b fixture_halt
.size Reset_Handler, . - Reset_Handler
