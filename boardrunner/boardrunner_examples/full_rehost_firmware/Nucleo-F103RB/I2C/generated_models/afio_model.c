#include <device.h>
#include <boardrunner/vio.h>

#define AFIO_BASE   0x40010000ULL

#define AFIO_EVCR    0x00
#define AFIO_MAPR    0x04
#define AFIO_EXTICR1 0x08
#define AFIO_EXTICR2 0x0C
#define AFIO_EXTICR3 0x10
#define AFIO_EXTICR4 0x14
#define AFIO_MAPR2   0x1C

typedef struct AFIOState {
    uint32_t evcr;
    uint32_t mapr;
    uint32_t exticr[4];
    uint32_t mapr2;
} AFIOState;

static AFIOState g_afio;

static uint32_t afio_size_mask(unsigned size)
{
    switch (size) {
    case 1:
        return 0xFFu;
    case 2:
        return 0xFFFFu;
    case 4:
        return 0xFFFFFFFFu;
    default:
        return 0;
    }
}

static int afio_decode_reg(AFIOState *s, hwaddr offset,
                           uint32_t **reg, uint32_t *writable_mask)
{
    hwaddr regoff = offset & ~0x3ULL;

    switch (regoff) {
    case AFIO_EVCR:
        *reg = &s->evcr;
        *writable_mask = 0x000000FFu;   /* PIN[3:0], PORT[6:4], EVOE[7] */
        return 1;

    case AFIO_MAPR:
        *reg = &s->mapr;
        *writable_mask = 0x071FFFFFu;   /* Valid AFIO remap bits */
        return 1;

    case AFIO_EXTICR1:
        *reg = &s->exticr[0];
        *writable_mask = 0x0000FFFFu;   /* EXTI0..3 source select */
        return 1;

    case AFIO_EXTICR2:
        *reg = &s->exticr[1];
        *writable_mask = 0x0000FFFFu;   /* EXTI4..7 source select */
        return 1;

    case AFIO_EXTICR3:
        *reg = &s->exticr[2];
        *writable_mask = 0x0000FFFFu;   /* EXTI8..11 source select */
        return 1;

    case AFIO_EXTICR4:
        *reg = &s->exticr[3];
        *writable_mask = 0x0000FFFFu;   /* EXTI12..15 source select */
        return 1;

    case AFIO_MAPR2:
        *reg = &s->mapr2;
        *writable_mask = 0x00000C1Fu;   /* Common documented MAPR2 bits */
        return 1;

    default:
        *reg = NULL;
        *writable_mask = 0;
        return 0;
    }
}

uint64_t afio_read(void *opaque, hwaddr addr, unsigned size)
{
    AFIOState *s = (AFIOState *)opaque;
    uint32_t *reg;
    uint32_t writable_mask;
    uint32_t mask;
    unsigned shift;
    hwaddr offset;

    if (!s) {
        return 0;
    }

    if (addr < AFIO_BASE) {
        return 0;
    }

    offset = addr - AFIO_BASE;
    mask = afio_size_mask(size);
    if (mask == 0) {
        dev_debug("AFIO: unsupported read size");
        return 0;
    }

    if (!afio_decode_reg(s, offset, &reg, &writable_mask) || reg == NULL) {
        return 0;
    }

    shift = (unsigned)((offset & 0x3ULL) * 8U);
    return (uint64_t)((*reg >> shift) & mask);
}

void afio_write(void *opaque, hwaddr addr, uint64_t value, unsigned size)
{
    AFIOState *s = (AFIOState *)opaque;
    uint32_t *reg;
    uint32_t writable_mask;
    uint32_t size_mask;
    uint32_t data_mask;
    uint32_t write_mask;
    uint32_t cur;
    uint32_t new_bits;
    unsigned shift;
    hwaddr offset;

    if (!s) {
        return;
    }

    if (addr < AFIO_BASE) {
        return;
    }

    offset = addr - AFIO_BASE;
    size_mask = afio_size_mask(size);
    if (size_mask == 0) {
        dev_debug("AFIO: unsupported write size");
        return;
    }

    if (!afio_decode_reg(s, offset, &reg, &writable_mask) || reg == NULL) {
        return;
    }

    shift = (unsigned)((offset & 0x3ULL) * 8U);
    data_mask = size_mask << shift;
    write_mask = writable_mask & data_mask;

    cur = *reg;
    new_bits = ((uint32_t)value << shift) & write_mask;
    *reg = (cur & ~write_mask) | new_bits;
}

/* MUST return &g_afio — framework stores this and passes it as opaque */
void* afio_init(ConfigSection *model_info)
{
    (void)model_info;

    g_afio.evcr = 0x00000000u;
    g_afio.mapr = 0x00000000u;
    g_afio.exticr[0] = 0x00000000u;
    g_afio.exticr[1] = 0x00000000u;
    g_afio.exticr[2] = 0x00000000u;
    g_afio.exticr[3] = 0x00000000u;
    g_afio.mapr2 = 0x00000000u;

    return &g_afio;
}