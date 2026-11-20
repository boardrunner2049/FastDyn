.syntax unified
.cpu cortex-m3
.thumb
.eabi_attribute Tag_CPU_arch, 10
.eabi_attribute Tag_CPU_arch_profile, 'M'

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
  .fnstart
  movw r4, #0
  movt r4, #0x2000
  bl worker_a
  str r0, [r4]
  bl worker_b
  str r0, [r4, #4]
.global fixture_halt
fixture_halt:
  b fixture_halt
  .personality __firmbolt_personality_v0
  .handlerdata
  // Opaque language-specific bytes and the bare-metal TARGET2 type-info
  // reference: the gate requires exact preservation and symbolic rebinding.
  .long 0x46424f4c
  .long __firmbolt_typeinfo(TARGET2)
  .section .text
  .fnend
.size Reset_Handler, . - Reset_Handler

.align 2
.global worker_a
.type worker_a, %function
.thumb_func
worker_a:
  .fnstart
  push {r4, lr}
  .save {r4, lr}
  movs r0, #17
  pop {r4, pc}
  .fnend
.size worker_a, . - worker_a

.align 2
.global worker_b
.type worker_b, %function
.thumb_func
worker_b:
  .fnstart
  movs r0, #42
  bx lr
  .cantunwind
  .fnend
.size worker_b, . - worker_b

.align 1
.global __firmbolt_personality_v0
.type __firmbolt_personality_v0, %function
.thumb_func
__firmbolt_personality_v0:
  bx lr
.size __firmbolt_personality_v0, . - __firmbolt_personality_v0

.section .rodata, "a", %progbits
.align 2
.global __firmbolt_typeinfo
.type __firmbolt_typeinfo, %object
__firmbolt_typeinfo:
  .long 0x54415247
.size __firmbolt_typeinfo, . - __firmbolt_typeinfo
