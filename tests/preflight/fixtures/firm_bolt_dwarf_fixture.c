typedef unsigned int uintptr_t;

int Reset_Handler(int x, int *out);

__attribute__((section(".isr_vector"), used))
const uintptr_t g_pfnVectors[] = {
    0x20001000u,
    (uintptr_t)Reset_Handler,
};

__attribute__((noinline)) int worker(int x, int *out) {
  int value = x + 1;
  if (x & 1)
    value += x;
  *out = value;
  return value;
}

int Reset_Handler(int x, int *out) { return worker(x, out); }
