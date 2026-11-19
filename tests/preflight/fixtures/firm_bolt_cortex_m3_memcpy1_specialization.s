.syntax unified
.cpu cortex-m3
.thumb
.eabi_attribute Tag_CPU_arch, 10
.eabi_attribute Tag_CPU_arch_profile, 'M'

/* Executed qualification for BOLT's native SpecializeMemcpy1 pass through
   Firm-BOLT's Cortex-M target hooks and compact publisher. The selected call
   executes both the size-one fast path and the retained size-two fallback.
   The unselected call is an executable negative control. The admitted model
   is AAPCS plus valid, disjoint, byte-addressable ordinary RAM and explicit
   acceptance of the added guard and changed call/access/fault event shape. */
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
  /* Source bytes 11 22 33 44 at 0x20000010. */
  movw r1, #0x0010
  movt r1, #0x2000
  movw r3, #0x2211
  movt r3, #0x4433
  str r3, [r1]

  /* Selected call: size one takes the specialized fast path. */
  movw r0, #0x0020
  movt r0, #0x2000
  movs r2, #1
  bl copy_runtime

  /* The same selected call site with size two takes the retained fallback. */
  movw r0, #0x0021
  movt r0, #0x2000
  movw r1, #0x0011
  movt r1, #0x2000
  movs r2, #2
  bl copy_runtime

  /* Unselected control copies the fourth byte through the original call. */
  movw r0, #0x0023
  movt r0, #0x2000
  movw r1, #0x0013
  movt r1, #0x2000
  movs r2, #1
  bl copy_unselected

  movw r1, #0x0020
  movt r1, #0x2000
  ldr r0, [r1]
  movw r1, #0
  movt r1, #0x2000
  str r0, [r1]
.global fixture_halt
fixture_halt:
  b fixture_halt
.size Reset_Handler, . - Reset_Handler

.global copy_runtime
.type copy_runtime, %function
.thumb_func
copy_runtime:
  push {lr}
  bl memcpy
  b 1f
  /* Proven non-entry capacity consumed by compact publication. */
  .rept 10
  nop
  .endr
1:
  pop {pc}
.size copy_runtime, . - copy_runtime

.global copy_unselected
.type copy_unselected, %function
.thumb_func
copy_unselected:
  push {lr}
  bl memcpy
  pop {pc}
.size copy_unselected, . - copy_unselected

/* Ordinary byte-copy fallback. It deliberately supports a runtime size so
   the executed size-two call proves that specialization retains the cold
   original implementation rather than assuming all calls have size one. */
.global memcpy
.type memcpy, %function
.thumb_func
memcpy:
  cmp r2, #0
  beq 2f
1:
  ldrb r3, [r1]
  strb r3, [r0]
  adds r1, #1
  adds r0, #1
  subs r2, #1
  bne 1b
2:
  bx lr
.size memcpy, . - memcpy
