.syntax unified
.cpu cortex-a15
.arm

.ifndef ITERATIONS
.equ ITERATIONS, 10000
.endif

.text
.global _start
.global redirect_site
.global redirect_target
.global occupied_site
.global occupied_target
_start:
    mov r4, #0
    mov r6, #0
    mov r7, #0
    mov r8, #0
    ldr r5, =ITERATIONS
    ldr lr, =redirect_target

loop:
    add r4, r4, #1
redirect_site:
    add r6, r6, #1
    add r7, r7, #1
    b failed

redirect_target:
    add r8, r8, #3
    subs r5, r5, #1
occupied_site:
    bne loop

occupied_target:
    ldr r9, =ITERATIONS
    cmp r4, r9
    bne failed
    cmp r6, #0
    bne failed
    cmp r7, #0
    bne failed
    ldr r9, =(ITERATIONS * 3)
    cmp r8, r9
    bne failed
    adr r1, pass_message
    b report

failed:
    adr r1, fail_message
report:
    mov r0, #4
    svc #0x123456
    mov r0, #0x18
    movw r1, #0x26
    movt r1, #2
    svc #0x123456
1:  b 1b

pass_message: .asciz "PASS constant-pc a32 checksum=iterations*3\n"
fail_message: .asciz "FAIL constant-pc a32\n"
