#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#if defined(__GNUC__)
#define NOINLINE __attribute__((noinline))
#define COLD __attribute__((cold, noinline))
#else
#define NOINLINE
#define COLD
#endif

static NOINLINE uint64_t hot_mix(uint64_t value) {
  value ^= value >> 17;
  value *= UINT64_C(0xed5ad4bb);
  value ^= value >> 11;
  return value;
}

static NOINLINE uint64_t warm_mix(uint64_t value) {
  return (value << 9) ^ (value >> 7) ^ UINT64_C(0x9e3779b97f4a7c15);
}

static COLD uint64_t cold_mix(uint64_t value) {
  for (unsigned index = 0; index != 11; ++index)
    value = (value * UINT64_C(6364136223846793005)) + index;
  return value ^ (value >> 29);
}

int main(int argc, char **argv) {
  if (argc != 3)
    return 2;
  uint64_t state = strtoull(argv[1], NULL, 0) | 1;
  const uint64_t iterations = strtoull(argv[2], NULL, 0);
  uint64_t result = state;
  for (uint64_t index = 0; index != iterations; ++index) {
    state = state * UINT64_C(2862933555777941757) + UINT64_C(3037000493);
    if ((state & 255) != 0)
      result += hot_mix(state ^ index);
    else
      result += cold_mix(state);
    if ((state & 15) == 3)
      result ^= warm_mix(result);
  }
  printf("%" PRIu64 "\n", result);
  return 0;
}
