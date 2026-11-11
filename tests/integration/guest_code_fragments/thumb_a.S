.syntax unified
.cpu cortex-a15
.arm
.text
.global _start
_start:
    adr r2, thumb_entry + 1
    bx r2
.thumb
.thumb_func
thumb_entry:
    movs r0, #1
.global fragment_site_thumb_a
fragment_site_thumb_a:
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
    movs r0, #4
    svc #0xab
    movs r0, #0x18
    movw r1, #0x26
    movt r1, #2
    svc #0xab
1:  b 1b
pass_message: .asciz "PASS thumb-a\n"
fail_message: .asciz "FAIL thumb-a\n"
