.text
.global _start
_start:
    li a0, 1
fragment_site:
    mv a1, a0
    li t2, 2
    bne a1, t2, failed
    li t1, 0x5555
    j finish
failed:
    li t1, 0x13333
finish:
    li t0, 0x100000
    sw t1, 0(t0)
1:  j 1b
