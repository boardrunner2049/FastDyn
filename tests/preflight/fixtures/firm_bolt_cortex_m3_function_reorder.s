.syntax unified
.cpu cortex-m3
.thumb
.eabi_attribute Tag_CPU_arch, 10
.eabi_attribute Tag_CPU_arch_profile, 'M'

/* Relocation-complete whole-image layout fixture.  The vector, both BL
   instructions, and every moved function symbol remain explicit in the ELF.
   The execution oracle is the ordered RAM pair (17, 42). */
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
  bl worker_a
  str r0, [r4]
  bl worker_b
  str r0, [r4, #4]
.global fixture_halt
fixture_halt:
  b fixture_halt
.size Reset_Handler, . - Reset_Handler

.align 2
.global worker_a
.type worker_a, %function
.thumb_func
worker_a:
  movs r0, #17
  bx lr
.size worker_a, . - worker_a

.align 2
.global worker_b
.type worker_b, %function
.thumb_func
worker_b:
  movs r0, #42
  bx lr
.size worker_b, . - worker_b
