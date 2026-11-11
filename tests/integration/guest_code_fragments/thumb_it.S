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
    cmp r0, #0
.global fragment_site_it
fragment_site_it:
    it eq
    moveq r1, r1
1:  b 1b
