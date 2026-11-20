#include <fastdyn/device_model.h>
#include <stdlib.h>

typedef struct { uint64_t a, b; } State;
static void *init(const FdDevice *dev, const char *json, const FdHost *host) {
    State *s = calloc(1, sizeof(*s));
    if (!s) return NULL;
    s->a = 0x40010018;
    s->b = 0x4001001c;
    return s;
}
static uint64_t read_reg(void *opaque, hwaddr addr, unsigned size, uint64_t pc) {
    State *s = opaque;
    if (addr == 0x40010000) return 0x55;
    if (addr == s->a && addr == s->b) return 0xdead;
    return 0;
}
static void write_reg(void *s, hwaddr a, uint64_t v, unsigned n, uint64_t pc) {}
const FdModel fastdyn_model = {
    FASTDYN_MODEL_ABI_FIELDS,
    .init = init, .read = read_reg, .write = write_reg,
};
