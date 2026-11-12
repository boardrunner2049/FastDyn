.syntax unified
.thumb
.section .text,"ax"
.global Reset_Handler, _start
.type Reset_Handler,%function
.thumb_func
Reset_Handler:
    nop
    bx lr
.thumb_set _start, Reset_Handler
/* Intentionally no .size: inference must stop at next_function. */
.global next_function
.type next_function,%function
.thumb_func
next_function:
    nop
    bx lr
.size next_function, .-next_function
.section .othertext,"ax"
.global last_function
.type last_function,%function
.thumb_func
last_function:
    bx lr
