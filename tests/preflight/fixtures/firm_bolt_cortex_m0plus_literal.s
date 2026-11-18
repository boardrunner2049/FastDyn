.syntax unified
.cpu cortex-m0plus
.thumb

.section .isr_vector,"a",%progbits
.balign 4
.global vector_table
.type vector_table,%object
vector_table:
  .word 0x20001000
  .word Reset_Handler + 1
  .word default_handler + 1
  .word default_handler + 1
.size vector_table, .-vector_table

.section .text,"ax",%progbits
.balign 4
.global Reset_Handler
.type Reset_Handler,%function
.thumb_func
Reset_Handler:
  ldr r4, ram_address
  bl literal_worker
  str r0, [r4, #0]
  str r1, [r4, #4]
  .global fixture_halt
fixture_halt:
  b fixture_halt
.size Reset_Handler, .-Reset_Handler
.balign 4
.type ram_address,%object
ram_address:
  .word 0x20000000

.balign 4
.global literal_worker
.type literal_worker,%function
.thumb_func
literal_worker:
  ldr r0, literal_value
  adr r1, literal_value
  bx lr
.size literal_worker, .-literal_worker
.balign 4
.type literal_value,%object
literal_value:
  .word 0x12345678

.balign 2
.global default_handler
.type default_handler,%function
.thumb_func
default_handler:
  b default_handler
.size default_handler, .-default_handler
