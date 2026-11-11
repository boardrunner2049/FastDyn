section .multiboot
align 4
    dd 0x1badb002
    dd 0
    dd -(0x1badb002)

section .text
BITS 32
global _start, fragment_site_retranslation
_start:
    mov eax, 1
    mov ecx, 3
again:
fragment_site_retranslation:
    mov ebx, eax
    mov byte [patch_byte], 0x90
    dec ecx
    jnz again
    cmp ebx, 4
    jne failed
    xor eax, eax
    jmp finish
failed:
    mov eax, 1
finish:
    mov dx, 0xf4
    out dx, eax
    cli
    hlt
patch_byte:
    nop
