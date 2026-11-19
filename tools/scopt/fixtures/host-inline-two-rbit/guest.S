.syntax unified
.cpu cortex-m3
.thumb

.section .vectors,"a",%progbits
.word 0x20010000
.word start

.section .text,"ax",%progbits
.thumb_func
.global start
start:
    mov.w r1, #0x30000000
    ldr r0, [r1]
    movs r3, #7
    mov.w r1, #65536
    movw r2, #0x79b9
    movt r2, #0x9e37

    .align 2
.global benchmark_start
benchmark_start:
    nop
.global hot_loop
hot_loop:
    rbit r0, r0
    str.w r0, [sp, #-4]
    rbit r3, r3
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
