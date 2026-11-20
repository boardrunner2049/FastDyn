BITS 16
global fragment_x86_16
fragment_x86_16:
    and ax, 255
    add ax, 7

BITS 64
global fragment_x86_64
fragment_x86_64:
    and eax, 255
    add eax, 7
