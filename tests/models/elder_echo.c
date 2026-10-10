/* A minimal device model for the elder handler (ABI v2), used by the tests.
 *
 * Registers, as offsets from the device's first range:
 *   0x00  DATA    read/write; starts at the "reset" value from the config
 *   0x04  IRQS    number of interrupts this instance received (read-only)
 *   0x08  SERVED  number of interrupt returns it saw (read-only)
 *   last  MARK    the device's last byte reads 0xAA
 */
#include <fastdyn/device_model.h>

#include <stdlib.h>
#include <string.h>

typedef struct {
    uint64_t base;
    uint64_t last;
    uint32_t data;
    uint32_t irqs;
    uint32_t served;
} Echo;

static void *echo_init(const FdDevice *dev, const char *config_json, const FdHost *host) {
    if (dev->range_count < 1) {
        host->log(FD_LOG_LEVEL_ERROR, dev->name, "needs at least one address range");
        return NULL;
    }
    if (strstr(config_json, "\"refuse\"")) {
        host->log(FD_LOG_LEVEL_ERROR, dev->name, "configuration refused");
        return NULL;
    }
    Echo *s = calloc(1, sizeof(*s));
    if (!s) {
        return NULL;
    }
    s->base = dev->ranges[0].base;
    s->last = dev->ranges[0].base + dev->ranges[0].size - 1;
    const char *reset = strstr(config_json, "\"reset\":");
    s->data = reset ? (uint32_t)strtoul(reset + 8, NULL, 0) : 0;
    host->log(FD_LOG_LEVEL_INFO, dev->name, "echo at 0x%llx, reset 0x%x",
              (unsigned long long)s->base, s->data);
    return s;
}

static uint64_t echo_read(void *opaque, hwaddr addr, unsigned size, uint64_t pc) {
    Echo *s = opaque;
    (void)size;
    (void)pc;
    if (addr == s->last) {
        return 0xAA;
    }
    switch (addr - s->base) {
    case 0x00: return s->data;
    case 0x04: return s->irqs;
    case 0x08: return s->served;
    default:   return 0;
    }
}

static void echo_write(void *opaque, hwaddr addr, uint64_t value, unsigned size, uint64_t pc) {
    Echo *s = opaque;
    (void)size;
    (void)pc;
    if (addr - s->base == 0x00) {
        s->data = (uint32_t)value;
    }
}

static void echo_interrupt(void *opaque, int irq) {
    (void)irq;
    ((Echo *)opaque)->irqs++;
}

static void echo_serve(void *opaque, int irq) {
    (void)irq;
    ((Echo *)opaque)->served++;
}

const FdModel fastdyn_model = {
    FASTDYN_MODEL_ABI_FIELDS,
    .init = echo_init,
    .read = echo_read,
    .write = echo_write,
    .interrupt = echo_interrupt,
    .serve = echo_serve,
};
