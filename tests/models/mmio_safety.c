/* Ordinary input model for the automatic-read correctness regressions. */
#include <fastdyn/device_model.h>
#include <stdlib.h>

typedef struct {
    uint64_t padding;
    uint64_t base;
    uint32_t clear;
    volatile uint32_t probe;
    uint32_t array[2];
    uint8_t byte;
} State;

static void *init(const FdDevice *dev, const char *json, const FdHost *host) {
    State *s = calloc(1, sizeof(*s));
    if (!s) return NULL;
    s->base = 0x40010020;
    s->clear = 0x12345678;
    s->probe = 7;
    s->array[0] = 0x11111111;
    s->array[1] = 0x22222222;
    s->byte = 255;
    return s;
}

static uint64_t read_reg(void *opaque, hwaddr addr, unsigned size, uint64_t pc) {
    State *s = opaque;
    switch (addr) {
    case 0x40010000: return 0x55;
    case 0x40010001: return 0x56;
    case 0x40010004: {
        uint32_t old = s->clear;
        s->clear = 0;
        return old;
    }
    case 0x40010008: return s->clear;
    case 0x4001000c: (void)s->probe; return s->array[0];
    case 0x40010010: return s->array[1];
    case 0x40010014: return (uint8_t)(s->byte + 1);
    default: return addr == s->base ? 0x99 : 0;
    }
}

__attribute__((noinline)) static void set_base(uint64_t *field, uint64_t value) {
    *field = value;
}

static void write_reg(void *opaque, hwaddr addr, uint64_t value,
                      unsigned size, uint64_t pc) {
    State *s = opaque;
    set_base(&s->base, value);
}

const FdModel fastdyn_model = {
    FASTDYN_MODEL_ABI_FIELDS,
    .init = init, .read = read_reg, .write = write_reg,
};
