.text
.global _start
_start:
    li t0, 0x10000000
    lw t1, 0(t0)
    li t1, 'P'
    sw t1, 0(t0)
    li t0, 0x100000
    li t1, 0x5555
    sw t1, 0(t0)
1:  j 1b
