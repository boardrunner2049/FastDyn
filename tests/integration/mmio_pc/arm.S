.syntax unified
.arm
.text
.global _start
_start:
    ldr r0, =0x09000018
    ldr r2, [r0]
    sub r0, r0, #0x18
    mov r2, #'P'
    str r2, [r0]
    mov r0, #0x18
    movw r1, #0x26
    movt r1, #2
    svc #0x123456
1:  b 1b
