  .syntax unified
  .thumb

  .section .isr_vector,"a",%progbits
  .p2align 2
  .globl g_pfnVectors
g_pfnVectors:
  .word 0x20001000
  .word Reset_Handler
  .word 0
  .word 0

  .section .text,"ax",%progbits
  .p2align 2
  .globl Reset_Handler
  .type Reset_Handler,%function
  .thumb_func
Reset_Handler:
  movw r9, #0
  movt r9, #0x2000
  movs r2, #1
  bl rwpi_worker
  str r0, [r9]
  movs r2, #1
  bl checked_rwpi_worker
  str r0, [r9, #4]
  movs r2, #1
  bl sbrel32_worker
  str r0, [r9, #8]
  .globl fixture_halt
fixture_halt:
  b fixture_halt
  .size Reset_Handler, .-Reset_Handler

  .p2align 2
  .globl rwpi_worker
  .type rwpi_worker,%function
  .thumb_func
rwpi_worker:
  push {r7, lr}
  sub sp, #4
  add r7, sp, #0
  str r2, [r7]
  ldr.w r2, [r7]
  movw r0, :lower16:rw_data(sbrel) + 4
  movt r0, :upper16:rw_data(sbrel) + 4
  add r0, r9
  ldr r0, [r0]
  adds r0, r2
  mov sp, r7
  add sp, #4
  pop {r7, pc}
  .size rwpi_worker, .-rwpi_worker

  // The checked BREL form carries the complete unsigned static-base offset
  // in one MOVW. LLVM MC normally selects the unchecked producer form, so an
  // explicit relocation exercises the checked ABI record without changing
  // the instruction encoding.
  .p2align 2
  .globl checked_rwpi_worker
  .type checked_rwpi_worker,%function
  .thumb_func
checked_rwpi_worker:
  push {r7, lr}
  sub sp, #4
  add r7, sp, #0
  str r2, [r7]
  ldr.w r2, [r7]
  movw r0, #0
  .reloc .-4, R_ARM_THM_MOVW_BREL, rw_data_checked
  add r0, r9
  ldr r0, [r0]
  adds r0, r2
  mov sp, r7
  add sp, #4
  pop {r7, pc}
  .size checked_rwpi_worker, .-checked_rwpi_worker

  // R_ARM_SBREL32 is the full-width data companion to the split instruction
  // relocations above. The literal holds S+A-B(S), not an absolute pointer;
  // the worker adds the qualified r9 static base before dereferencing it.
  .p2align 2
  .globl sbrel32_worker
  .type sbrel32_worker,%function
  .thumb_func
sbrel32_worker:
  push {r7, lr}
  sub sp, #4
  add r7, sp, #0
  str r2, [r7]
  ldr.w r2, [r7]
  movw r0, :lower16:rw_offset
  movt r0, :upper16:rw_offset
  ldr r0, [r0]
  add r0, r9
  ldr r0, [r0]
  adds r0, r2
  mov sp, r7
  add sp, #4
  pop {r7, pc}
  .size sbrel32_worker, .-sbrel32_worker

  // The explicit segment-start policy still rejects BREL references whose
  // target can move with executable layout. This cold control must remain
  // byte-for-byte unchanged.
  .p2align 2
  .globl unsupported_exec_brel
  .type unsupported_exec_brel,%function
  .thumb_func
unsupported_exec_brel:
  movw r0, :lower16:flash_data(sbrel)
  movt r0, :upper16:flash_data(sbrel)
  bx lr
  .size unsupported_exec_brel, .-unsupported_exec_brel

  .p2align 2
  .globl flash_data
  .type flash_data,%object
flash_data:
  .word 7
  .size flash_data, .-flash_data

  .section .data,"aw",%progbits
  .space 32
  .p2align 2
  .globl rw_data
  .type rw_data,%object
rw_data:
  .word 13
  .globl rw_data_checked
  .type rw_data_checked,%object
rw_data_checked:
  .word 41
  .size rw_data_checked, .-rw_data_checked
  .globl rw_offset
  .type rw_offset,%object
rw_offset:
  .word rw_data_checked(sbrel)
  .size rw_offset, .-rw_offset
  .size rw_data, .-rw_data
