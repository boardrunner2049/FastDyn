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
  .word 0
  .word HardFault_Handler
.size g_pfnVectors, . - g_pfnVectors

.section .text, "ax", %progbits
.align 2
.global Reset_Handler
.type Reset_Handler, %function
.thumb_func
Reset_Handler:
  /* Enable a 32-byte privileged no-access MPU region at 0x60000000 while
     retaining the default map elsewhere. This turns the negative method-table
     dereference into a precise architectural data-access violation instead of
     relying on a board-specific unassigned-memory policy. */
  movw r2, #0xed90
  movt r2, #0xe000
  movs r3, #0
  str r3, [r2, #8]
  movw r3, #0
  movt r3, #0x6000
  str r3, [r2, #12]
  movw r3, #9
  movt r3, #0x1000
  str r3, [r2, #16]
  movs r3, #5
  str r3, [r2, #4]
  dsb
  isb

  /* Two ordinary RAM objects hold distinct immutable vtable pointers.  The
     first exercises the promoted hot path; the second must take the cold
     fallback and execute its original method load. */
  movw r4, #0
  movt r4, #0x2000
  movw r3, :lower16:immutable_hot_vtable
  movt r3, :upper16:immutable_hot_vtable
  str r3, [r4, #0x20]
  movw r3, :lower16:immutable_cold_vtable
  movt r3, :upper16:immutable_cold_vtable
  str r3, [r4, #0x24]
  movw r3, #0
  movt r3, #0x6000
  str r3, [r4, #0x28]

  add r0, r4, #0x20
  bl virtual_caller
  str r0, [r4]
  add r0, r4, #0x24
  bl virtual_caller
  str r0, [r4, #4]
  /* This object has a valid RAM-resident vptr field whose value names an
     unmapped method table. The hot guard must miss and the original cold
     method load must raise the same architectural fault identity/state. */
  add r0, r4, #0x28
  bl virtual_caller
.global fixture_halt
fixture_halt:
  b fixture_halt
.size Reset_Handler, . - Reset_Handler

.global HardFault_Handler
.type HardFault_Handler, %function
.thumb_func
HardFault_Handler:
  /* Record the precise stacked PC plus CFSR, HFSR and MMFAR. The validation
     maps the candidate PC through the emitted origin ledger before comparing
     it with the reference method-load identity. */
  mrs r0, msp
  ldr r2, [r0, #24]
  movw r1, #0
  movt r1, #0x2000
  str r2, [r1, #8]
  movw r3, #0xed28
  movt r3, #0xe000
  ldr r2, [r3]
  str r2, [r1, #12]
  ldr r2, [r3, #4]
  str r2, [r1, #16]
  ldr r2, [r3, #12]
  str r2, [r1, #20]
  b fixture_halt
.size HardFault_Handler, . - HardFault_Handler

.global virtual_caller
.type virtual_caller, %function
.thumb_func
virtual_caller:
  push {lr}
  ldr r3, [r0]
  ldr r2, [r3]
  blx r2
  b .Lreturn
  /* Proven-unreachable, symbol-bounded capacity for the vtable guard and
     guarded direct/fallback call expansion. */
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
  /* ARM ELF STT_FUNC relocations encode the Thumb-state bit. */
  .word promoted_target
.size immutable_hot_vtable, . - immutable_hot_vtable

.global immutable_cold_vtable
.type immutable_cold_vtable, %object
immutable_cold_vtable:
  .word cold_target
.size immutable_cold_vtable, . - immutable_cold_vtable
