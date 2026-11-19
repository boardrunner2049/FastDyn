.syntax unified
.cpu cortex-m3
.thumb
.eabi_attribute Tag_CPU_arch, 10
.eabi_attribute Tag_CPU_arch_profile, 'M'

/* Executed qualification for BOLT's native InlineMemcpy pass through the
   Cortex-M target hooks and Firm-BOLT's compact publisher. The admitted
   contract is AAPCS, disjoint ordinary RAM, three valid bytes, and explicit
   acceptance of the removed call plus changed access/fault event shape. */
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
  /* Source at 0x20000010, destination at 0x20000020. */
  movw r1, #0x0010
  movt r1, #0x2000
  movw r3, #0x2211
  movt r3, #0x0033
  str r3, [r1]
  movw r0, #0x0020
  movt r0, #0x2000
  bl copy_three
  ldr r0, [r0]
  movw r1, #0
  movt r1, #0x2000
  str r0, [r1]
.global fixture_halt
fixture_halt:
  b fixture_halt
.size Reset_Handler, . - Reset_Handler

.global copy_three
.type copy_three, %function
.thumb_func
copy_three:
  push {lr}
  movs r2, #3
  bl memcpy
  b 1f
  /* Proven non-entry capacity consumed by compact publication. */
  .rept 8
  nop
  .endr
1:
  pop {pc}
.size copy_three, . - copy_three

/* Negative controls: these remain byte-identical because the size is not a
   straight-line constant in [1, 32]. */
.global copy_unknown
.type copy_unknown, %function
.thumb_func
copy_unknown:
  push {lr}
  bl memcpy
  pop {pc}
.size copy_unknown, . - copy_unknown

.global copy_large
.type copy_large, %function
.thumb_func
copy_large:
  push {lr}
  movs r2, #33
  bl memcpy
  pop {pc}
.size copy_large, . - copy_large

.global copy_after_call
.type copy_after_call, %function
.thumb_func
copy_after_call:
  push {lr}
  movs r2, #3
  bl clobber_r2
  bl memcpy
  pop {pc}
.size copy_after_call, . - copy_after_call

.global clobber_r2
.type clobber_r2, %function
.thumb_func
clobber_r2:
  movs r2, #17
  bx lr
.size clobber_r2, . - clobber_r2

/* Exact three-byte reference implementation. */
.global memcpy
.type memcpy, %function
.thumb_func
memcpy:
  ldrb r3, [r1]
  strb r3, [r0]
  ldrb r3, [r1, #1]
  strb r3, [r0, #1]
  ldrb r3, [r1, #2]
  strb r3, [r0, #2]
  bx lr
.size memcpy, . - memcpy
