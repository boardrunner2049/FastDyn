.syntax unified
.cpu cortex-m0plus
.thumb

/* Exercise a Thumb-1 immutable-vtable hot path, a different valid cold path,
   and an invalid method-table access.  The cold paths must retain the original
   method load; the faulting instruction is compared through its origin map. */
.section .isr_vector, "a", %progbits
.align 2
.global g_pfnVectors
.type g_pfnVectors, %object
g_pfnVectors:
  .word 0x20001000
  .word Reset_Handler
  .word 0
  .word HardFault_Handler
.size g_pfnVectors, . - g_pfnVectors

.section .text, "ax", %progbits
.align 2
.global Reset_Handler
.type Reset_Handler, %function
.thumb_func
Reset_Handler:
  ldr r4, .Lram
  ldr r3, .Lhot_vtable
  str r3, [r4, #0x20]
  ldr r3, .Lcold_vtable
  str r3, [r4, #0x24]
  ldr r3, .Linvalid_table
  str r3, [r4, #0x28]

  mov r0, r4
  adds r0, #0x20
  bl virtual_caller
  str r0, [r4]
  mov r0, r4
  adds r0, #0x24
  bl virtual_caller
  str r0, [r4, #4]
  mov r0, r4
  adds r0, #0x28
  bl virtual_caller
.global fixture_halt
fixture_halt:
  b fixture_halt
  .align 2
.Lram:
  .word 0x20000000
.Lhot_vtable:
  .word immutable_hot_vtable
.Lcold_vtable:
  .word immutable_cold_vtable
.Linvalid_table:
  /* The configured SRAM ends at 0x3fffffff.  This unaligned word crosses the
     mapped boundary and must fault at the method load, not at the later BLX. */
  .word 0x3ffffffe
.size Reset_Handler, . - Reset_Handler

.global HardFault_Handler
.type HardFault_Handler, %function
.thumb_func
HardFault_Handler:
  mrs r0, msp
  ldr r2, [r0, #24]
  ldr r1, .Lhandler_ram
  str r2, [r1, #8]
  ldr r3, .Lhfsr
  ldr r2, [r3]
  str r2, [r1, #12]
  b fixture_halt
  .align 2
.Lhandler_ram:
  .word 0x20000000
.Lhfsr:
  .word 0xE000ED2C
.size HardFault_Handler, . - HardFault_Handler

.align 2
.global virtual_caller
.type virtual_caller, %function
.thumb_func
virtual_caller:
  push {lr}
  ldr r3, [r0]
  ldr r2, [r3]
  blx r2
  b .Lreturn
  .rept 20
  nop
  .endr
.Lreturn:
  pop {pc}
.size virtual_caller, . - virtual_caller

.global promoted_target
.type promoted_target, %function
.thumb_func
promoted_target:
  movs r0, #17
  bx lr
.size promoted_target, . - promoted_target

.global cold_target
.type cold_target, %function
.thumb_func
cold_target:
  movs r0, #19
  bx lr
.size cold_target, . - cold_target

.section .rodata.vtable, "a", %progbits
.align 2
.global immutable_hot_vtable
.type immutable_hot_vtable, %object
immutable_hot_vtable:
  .word promoted_target
.size immutable_hot_vtable, . - immutable_hot_vtable

.global immutable_cold_vtable
.type immutable_cold_vtable, %object
immutable_cold_vtable:
  .word cold_target
.size immutable_cold_vtable, . - immutable_cold_vtable
