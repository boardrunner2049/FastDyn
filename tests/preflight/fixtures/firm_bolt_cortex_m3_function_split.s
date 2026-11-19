.syntax unified
.cpu cortex-m3
.thumb
.eabi_attribute Tag_CPU_arch, 10
.eabi_attribute Tag_CPU_arch_profile, 'M'

/* Relocation-complete hot/cold splitting fixture.  Training sees only the
   hot return; execution checks both the hot and held-out cold path. */
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
  movw r4, #0
  movt r4, #0x2000
  movs r0, #0
  bl split_worker
  str r0, [r4]
  movs r0, #1
  bl split_worker
  str r0, [r4, #4]
.global fixture_halt
fixture_halt:
  b fixture_halt
.size Reset_Handler, . - Reset_Handler

.align 2
.global split_worker
.type split_worker, %function
.thumb_func
split_worker:
  cmp r0, #0
  bne 1f
  movs r0, #17
  bx lr
1:
  movs r0, #42
  bx lr
.size split_worker, . - split_worker
