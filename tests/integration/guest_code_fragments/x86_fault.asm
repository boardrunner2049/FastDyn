section .multiboot
align 4
    dd 0x1badb002
    dd 0
    dd -(0x1badb002)

section .text
BITS 32
global _start, fragment_site_fault
_start:
    mov eax, de_handler
    mov word [idt], ax
    shr eax, 16
    mov word [idt + 6], ax
    mov word [idt + 2], 0x08
    mov byte [idt + 4], 0
    mov byte [idt + 5], 0x8e
    lidt [idtr]
    mov eax, 1
    mov ecx, 0
    or eax, 1                    ; establish ZF=0 before the fragment
fragment_site_fault:
    div ecx                      ; faults after fragment CMP establishes ZF=1
    mov eax, 2                   ; unreachable
    jmp finish

de_handler:
    test dword [esp + 8], 1 << 6
    jz failed
    cmp dword [esp], fragment_site_fault
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

section .data
align 8
idt: times 8 db 0
idtr:
    dw 7
    dd idt
