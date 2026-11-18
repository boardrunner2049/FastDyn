.syntax unified
.cpu cortex-m3
.thumb
.eabi_attribute Tag_CPU_arch, 10
.eabi_attribute Tag_CPU_arch_profile, 'M'

/* Execute both read-only-load outcomes used by the qualification ledger.
   load_linked_rodata has a complete section/symbol reference and may be
   materialized only under the explicit read/fault-elision contract.
   load_unresolved_pool owns an unmodeled local literal pool; Firm-BOLT must
   reject its candidate and publish the immutable original bytes. */
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
  bl load_linked_rodata
  str r0, [r1]
  bl load_unresolved_pool
  str r0, [r1, #4]
.global fixture_halt
fixture_halt:
  b fixture_halt
.size Reset_Handler, . - Reset_Handler

.global load_linked_rodata
.type load_linked_rodata, %function
.thumb_func
load_linked_rodata:
  ldr.w r0, immutable_scalar
  bx lr
.size load_linked_rodata, . - load_linked_rodata

.global load_unresolved_pool
.type load_unresolved_pool, %function
.thumb_func
load_unresolved_pool:
  ldr.w r0, .Lunresolved_scalar
  bx lr
.size load_unresolved_pool, . - load_unresolved_pool

.align 2
.type .Lunresolved_scalar, %object
.Lunresolved_scalar:
  .word 0x5678
.size .Lunresolved_scalar, . - .Lunresolved_scalar

.section .rodata, "a", %progbits
.align 2
.global immutable_scalar
.type immutable_scalar, %object
immutable_scalar:
  .word 0x1234
.size immutable_scalar, . - immutable_scalar
