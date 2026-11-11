.text
.global _start
_start:
    li a0, 1
fragment_site:
    beq a1, zero, failed
    bge t0, zero, failed
    add a0, a0, a1
    addi a0, a0, 1
    mv a2, a0
    li t2, 100
    bne a2, t2, failed
    li t1, 0x5555
    j finish
failed:
    li t1, 0x13333
finish:
    li t0, 0x100000
    sw t1, 0(t0)
1:  j 1b
