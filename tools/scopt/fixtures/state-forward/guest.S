.syntax unified
.cpu cortex-m3
.thumb

.section .vectors,"a",%progbits
.word 0x20010000
.word start

.section .start_code,"ax",%progbits
.thumb_func
.global start
start:
    mov.w r1, #0x30000000
    ldr r4, [r1]
    movw r5, #4096
    movs r6, #0
    movs r2, #1
.global benchmark_start
benchmark_start:
    nop
.global dispatch
dispatch:
    tst r4, #1
    beq source
    orr r0, r4, #7
    b.w consumer

.section .source_code,"ax",%progbits
.global source
.thumb_func
source:
    rbit r2, r2
.global write_r0
write_r0:
    nop
    b.w consumer

.section .consumer_code,"ax",%progbits
.global consumer
.thumb_func
consumer:
    .global hot_loop
hot_loop:

    mov r3, r0
    mov.w r0, #0
    cbnz r3, nonzero
    adds r6, #1
    b done
nonzero:
    adds r6, #3
done:
    subs r5, #1
    bne.w dispatch
    mov r0, r6
.global print_result
print_result:
    nop
.global benchmark_end
benchmark_end:
    nop
1:  b 1b

.section .coverage_code,"ax",%progbits
.balign 4
.global coverage_start, coverage_end, coverage_invalid
coverage_start:
    movw r3, #0x123
    bx r4
coverage_invalid:
    udf #0
coverage_end:
