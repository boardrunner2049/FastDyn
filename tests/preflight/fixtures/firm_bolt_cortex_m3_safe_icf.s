.syntax unified
.cpu cortex-m3
.thumb
.eabi_attribute Tag_CPU_arch, 10
.eabi_attribute Tag_CPU_arch_profile, 'M'

.section .isr_vector, "a", %progbits
.p2align 2
.global g_pfnVectors
.type g_pfnVectors, %object
g_pfnVectors:
  .word 0x20001000
  .word Reset_Handler
.size g_pfnVectors, . - g_pfnVectors

/* These two otherwise-identical functions are deliberately address-taken.
 * Safe ICF must retain them because their distinct pointer identities are an
 * admitted program observation. */
.section .rodata.function_addresses, "a", %progbits
.p2align 2
.global function_addresses
function_addresses:
  .word address_taken_a
  .word address_taken_b
.size function_addresses, . - function_addresses

.text
.p2align 2
.global Reset_Handler
.type Reset_Handler, %function
.thumb_func
Reset_Handler:
  ldr r4, =0x20000000
  bl direct_call_a
  str r0, [r4]
  bl direct_call_b
  str r0, [r4, #4]
  bl address_taken_a
  str r0, [r4, #8]
  bl address_taken_b
  str r0, [r4, #12]
  movw r1, #:lower16:function_addresses
  movt r1, #:upper16:function_addresses
  ldr r2, [r1]
  ldr r3, [r1, #4]
  cmp r2, r3
  ite ne
  movne r0, #1
  moveq r0, #0
  str r0, [r4, #16]
.global fixture_halt
fixture_halt:
  b fixture_halt
.size Reset_Handler, . - Reset_Handler

.p2align 2
.global direct_call_a
.type direct_call_a, %function
.thumb_func
direct_call_a:
  movs r0, #42
  bx lr
.size direct_call_a, . - direct_call_a

.p2align 2
.global direct_call_b
.type direct_call_b, %function
.thumb_func
direct_call_b:
  movs r0, #42
  bx lr
.size direct_call_b, . - direct_call_b

.p2align 2
.global address_taken_a
.type address_taken_a, %function
.thumb_func
address_taken_a:
  movs r0, #17
  bx lr
.size address_taken_a, . - address_taken_a

.p2align 2
.global address_taken_b
.type address_taken_b, %function
.thumb_func
address_taken_b:
  movs r0, #17
  bx lr
.size address_taken_b, . - address_taken_b
