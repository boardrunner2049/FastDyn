.syntax unified
.arm
.text
.global _start
_start:
    mov r0, #1
fragment_site:
    bne failed
    bcc failed
    bmi failed
    bvs failed
    mov r1, r0
    cmp r1, #100
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
pass_message: .asciz "PASS a32\n"
fail_message: .asciz "FAIL a32\n"
