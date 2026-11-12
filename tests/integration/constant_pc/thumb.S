.syntax unified
.cpu cortex-m4
.thumb

.ifndef ITERATIONS
.equ ITERATIONS, 10000
.endif

.section .vectors, "a"
.word 0x20010000
.word reset + 1

.text
.thumb_func
.global reset
.global redirect_site
.global redirect_target
.global occupied_site
.global occupied_target
reset:
    movs r4, #0
    movs r6, #0
    movs r7, #0
    movs r0, #0
    mov r8, r0
    ldr r5, =ITERATIONS
    ldr lr, =redirect_target

loop:
    adds r4, r4, #1
redirect_site:
    adds r6, r6, #1
    adds r7, r7, #1
    b failed

redirect_target:
    add.w r8, r8, #3
    subs r5, r5, #1
occupied_site:
    bne loop

occupied_target:
    ldr r0, =ITERATIONS
    cmp r4, r0
    bne failed
    cmp r6, #0
    bne failed
    cmp r7, #0
    bne failed
    ldr r0, =(ITERATIONS * 3)
    cmp r8, r0
    bne failed
    adr r1, pass_message
    b report

failed:
    adr r1, fail_message
report:
    movs r0, #4
    bkpt #0xab
    movs r0, #0x18
    movw r1, #0x26
    movt r1, #2
    bkpt #0xab
1:  b 1b

pass_message: .asciz "PASS constant-pc thumb checksum=iterations*3\n"
fail_message: .asciz "FAIL constant-pc thumb\n"
