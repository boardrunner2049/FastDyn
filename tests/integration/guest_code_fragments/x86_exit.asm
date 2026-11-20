section .multiboot
align 4
    dd 0x1badb002
    dd 0
    dd -(0x1badb002)

section .text
BITS 32
global _start
_start:
    xor eax, eax
    mov dx, 0xf4
    out dx, eax
    cli
    hlt
