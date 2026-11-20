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
  // The minimal runner has no loader-side TLS/GOT copy-down.  Materialize the
  // exact statically linked TLS descriptor and initial TLS datum before the
  // compiler-shaped global-dynamic access sequence executes.
  movw r1, #0x100
  movt r1, #0x2000
  movs r2, #1
  str r2, [r1]
  movs r2, #0
  str r2, [r1, #4]
  movw r1, #0x200
  movt r1, #0x2000
  movs r2, #7
  str r2, [r1]
  bl read_tls
  movw r1, #0
  movt r1, #0x2000
  str r0, [r1]
.global fixture_halt
fixture_halt:
  b fixture_halt
.size Reset_Handler, . - Reset_Handler

.align 2
.global read_tls
.type read_tls, %function
.thumb_func
read_tls:
  push {r7, lr}
  ldr r0, .Ltls_descriptor_delta
.Ltls_descriptor_pc:
  add r0, pc
  bl __tls_get_addr
  ldr r0, [r0]
  pop {r7, pc}
.align 2
.Ltls_descriptor_delta:
.Ltls_descriptor_word:
  // This is the compiler-emitted Cortex-M PIC form. R_ARM_TLS_GD32 resolves
  // to the first word of the retained two-word static TLS descriptor minus P.
  .long tls_data(TLSGD)-(.Ltls_descriptor_pc+4-.Ltls_descriptor_word)
.size read_tls, . - read_tls

.align 2
.global __tls_get_addr
.type __tls_get_addr, %function
.thumb_func
__tls_get_addr:
  // r0 -> {module index, offset}.  This fixture admits exactly one static TLS
  // module and therefore validates the descriptor identity as well as the
  // relocated literal; a different module index returns a visible failure.
  ldr r1, [r0]
  cmp r1, #1
  bne .Lbad_tls_module
  ldr r1, [r0, #4]
  movw r0, #0x200
  movt r0, #0x2000
  add r0, r1
  bx lr
.Lbad_tls_module:
  movs r0, #0
  bx lr
.size __tls_get_addr, . - __tls_get_addr

.align 2
.global filler
.type filler, %function
.thumb_func
filler:
  movs r0, #0
  bx lr
.size filler, . - filler

.section .tdata, "awT", %progbits
.align 2
.global tls_data
.type tls_data, %object
tls_data:
  .long 7
.size tls_data, . - tls_data

.section .bss, "aw", %nobits
.align 2
.global fixture_result
.type fixture_result, %object
fixture_result:
  .space 4
.size fixture_result, . - fixture_result

.ifdef DUPLICATE_TLS_GD_DESCRIPTOR
.section .got, "aw", %progbits
.align 2
  .long 1
  .long 0
.endif
