.syntax unified
.cpu cortex-m3
.thumb

.section .vectors,"a",%progbits
.word 0x20010000
.word start
.space 12 * 4
.word coverage_handler
.word 0

.section .text,"ax",%progbits
.thumb_func
.global start
start:
    mov.w r1, #0x30000000
    ldr r0, [r1]
    cmp r0, #2
    bne .Lno_irq
    movw r1, #0xed04
    movt r1, #0xe000
    mov.w r2, #0x10000000
    str r2, [r1]
    dsb
    isb
.Lno_irq:
    movw r4, #:lower16:coverage_return+1
    movt r4, #:upper16:coverage_return+1
    cbz r0, .Lcoverage_start
coverage_return:
    mov.w r1, #256
    movw r2, #0x79b9
    movt r2, #0x9e37

    .align 2
.global benchmark_start
benchmark_start:
    nop
hot_loop:
    rbit r0, r0
    adds r0, r0, r2
    subs r1, #1
    bne hot_loop
.global print_result
print_result:
    nop
.global benchmark_end
benchmark_end:
    nop
1:  b 1b

    .align 2
.global coverage_start, coverage_end, coverage_invalid
coverage_start:
.Lcoverage_start:
    movw r3, #0x123
    bx r4
coverage_invalid:
    udf #0
coverage_end:

.global coverage_handler
.thumb_func
coverage_handler:
    bx lr
