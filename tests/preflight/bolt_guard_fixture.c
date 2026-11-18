/* Small supported-ISA fixture for the FastDyn BOLT patch-contract pass. */

__attribute__((noinline)) int patch_target(int value) {
    volatile int adjusted = value + 7;
    return adjusted * 3;
}

int main(void) {
    return patch_target(5) == 36 ? 0 : 1;
}
