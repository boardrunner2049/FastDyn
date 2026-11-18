/* Exact SmartSpeaker O0 and BOLT-FOP bodies plus the report's short probe. */

        .text
        .globl lemix_accessor_original
        .type lemix_accessor_original,@function
lemix_accessor_original:
        pushq %rbp
        movq %rsp, %rbp
        movq %rdi, -0x18(%rbp)
        movq -0x18(%rbp), %rax
        movq (%rax), %rax
        movq %rax, -0x8(%rbp)
        movq -0x8(%rbp), %rax
        addq $0x8, %rax
        popq %rbp
        retq
        .size lemix_accessor_original,.-lemix_accessor_original

        .globl lemix_accessor_fop
        .type lemix_accessor_fop,@function
lemix_accessor_fop:
        pushq %rbp
        movq %rsp, %rbp
        movq %rdi, -0x18(%rbp)
        movq %rdi, %rax
        movq (%rax), %rax
        movq %rax, -0x8(%rbp)
        addq $0x8, %rax
        popq %rbp
        retq
        .size lemix_accessor_fop,.-lemix_accessor_fop

        .globl lemix_accessor_frs
        .type lemix_accessor_frs,@function
lemix_accessor_frs:
        pushq %rbp
        movq %rsp, %rbp
        movq %rdi, %rax
        movq (%rax), %rax
        addq $0x8, %rax
        popq %rbp
        retq
        .size lemix_accessor_frs,.-lemix_accessor_frs

        .globl lemix_accessor_short
        .type lemix_accessor_short,@function
lemix_accessor_short:
        movq (%rdi), %rax
        addq $0x8, %rax
        retq
        .size lemix_accessor_short,.-lemix_accessor_short

        .data
        .globl lemix_alias_pointer
lemix_alias_pointer:
        .quad 0
        .globl lemix_alias_observed
lemix_alias_observed:
        .quad 0

        .text
        .globl lemix_alias_call
        .type lemix_alias_call,@function
lemix_alias_call:
        pushq %rbx
        movq %rdi, %rbx
        movq %rsi, -0x28(%rsp)
        leaq -0x28(%rsp), %rdi
        movq %rdi, lemix_alias_pointer(%rip)
        call *%rbx
        movq -0x28(%rsp), %rdx
        movq %rdx, lemix_alias_observed(%rip)
        popq %rbx
        retq
        .size lemix_alias_call,.-lemix_alias_call

        .section .note.GNU-stack,"",@progbits
