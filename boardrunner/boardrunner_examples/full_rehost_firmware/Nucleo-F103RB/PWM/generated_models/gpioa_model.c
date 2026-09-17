// Device Model for GPIOA
//
// Inferred Register Functions:
// - CRL   (0x00): PA0..PA7 configuration
// - CRH   (0x04): PA8..PA15 configuration
// - IDR   (0x08): input data register
// - ODR   (0x0C): output data register
// - BSRR  (0x10): atomic set/reset of ODR bits
// - BRR   (0x14): atomic reset of ODR bits
// - LCKR  (0x18): configuration lock register
//
// Trace-backed behavior:
// - Reset CRL readback is 0x44444444
// - Firmware writes CRL = 0x44344444, configuring PA5 as output
// - ODR readback during runtime preserves a baseline latch state of 0xA000
// - Firmware repeatedly writes BSRR with 0x20 and 0x00200000 to toggle PA5,
//   producing ODR reads of 0xA020 and 0xA000 respectively
//
// Host observability:
// - Every pin-level change is written to the FIFO GPIOA_FIFO_PATH as
//   "PAn=0\n" / "PAn=1\n". Read it with: cat /tmp/gpioa_out

#include <device.h>
#include <boardrunner/vio.h>

#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <stdio.h>

#define GPIOA_BASE  0x40010800u

#define GPIO_CRL    0x00u
#define GPIO_CRH    0x04u
#define GPIO_IDR    0x08u
#define GPIO_ODR    0x0Cu
#define GPIO_BSRR   0x10u
#define GPIO_BRR    0x14u
#define GPIO_LCKR   0x18u

#define GPIOA_FIFO_PATH "/tmp/gpioa_out"

typedef struct {
    uint32_t base;

    uint32_t crl;
    uint32_t crh;
    uint16_t odr;
    uint32_t lckr;

    uint16_t published_levels;
    int fifo_fd;
} GPIOAState;

static GPIOAState g_gpioa;

static void gpioa_debug_access(const char *op, hwaddr addr, unsigned size, uint64_t value) {
    char buf[160];
    snprintf(buf, sizeof(buf),
             "GPIOA: %s unknown access addr=0x%08llx size=%u value=0x%08llx",
             op,
             (unsigned long long)addr,
             size,
             (unsigned long long)value);
    dev_debug(buf);
}

static uint32_t gpioa_size_mask(unsigned size) {
    switch (size) {
    case 1:
        return 0xFFu;
    case 2:
        return 0xFFFFu;
    case 4:
    default:
        return 0xFFFFFFFFu;
    }
}

static uint64_t gpioa_extract_u32(uint32_t regval, hwaddr offset, unsigned size) {
    if (size >= 4) {
        return regval;
    }

    unsigned shift = (unsigned)(offset & 0x3u) * 8u;
    uint32_t mask = gpioa_size_mask(size);
    return (regval >> shift) & mask;
}

static void gpioa_insert_u32(uint32_t *regval, hwaddr offset, uint64_t value, unsigned size) {
    if (size >= 4) {
        *regval = (uint32_t)value;
        return;
    }

    unsigned shift = (unsigned)(offset & 0x3u) * 8u;
    uint32_t mask = gpioa_size_mask(size) << shift;
    *regval = (*regval & ~mask) | ((((uint32_t)value) << shift) & mask);
}

static uint8_t gpioa_get_pin_cfg(GPIOAState *s, int pin) {
    if (pin < 8) {
        return (uint8_t)((s->crl >> (pin * 4)) & 0xFu);
    }
    return (uint8_t)((s->crh >> ((pin - 8) * 4)) & 0xFu);
}

static bool gpioa_get_pin_level(GPIOAState *s, int pin) {
    uint8_t cfg = gpioa_get_pin_cfg(s, pin);
    uint8_t mode = cfg & 0x3u;
    uint8_t cnf  = (cfg >> 2) & 0x3u;
    bool odr_bit = ((s->odr >> pin) & 1u) != 0;

    // STM32F1:
    // MODE != 00 => output mode, pin level follows output latch in this model.
    if (mode != 0) {
        return odr_bit;
    }

    // Input with pull-up/pull-down: ODR selects pull direction.
    if (cnf == 0x2u) {
        return odr_bit;
    }

    // Analog / floating / reserved input states default low in this minimal model.
    return false;
}

static uint16_t gpioa_compute_idr(GPIOAState *s) {
    uint16_t idr = 0;
    int pin;

    for (pin = 0; pin < 16; pin++) {
        if (gpioa_get_pin_level(s, pin)) {
            idr |= (uint16_t)(1u << pin);
        }
    }

    return idr;
}

static void gpioa_publish_levels(GPIOAState *s) {
    uint16_t levels = gpioa_compute_idr(s);
    uint16_t changed = (uint16_t)(levels ^ s->published_levels);
    int pin;

    for (pin = 0; pin < 16; pin++) {
        if (changed & (1u << pin)) {
            char line[16];
            int n = snprintf(line, sizeof(line), "PA%d=%d\n", pin, ((levels >> pin) & 1u) != 0);
            api_fifo_write(s->fifo_fd, line, n);
        }
    }

    s->published_levels = levels;
}

// This function will emulate all device reads
uint64_t gpioa_read(void *opaque, hwaddr addr, unsigned size) {
    GPIOAState *s = (GPIOAState *)opaque;
    hwaddr offset = addr - s->base;
    hwaddr reg = offset & ~0x3u;
    uint32_t regval = 0;

    switch (reg) {
    case GPIO_CRL:
        regval = s->crl;
        break;
    case GPIO_CRH:
        regval = s->crh;
        break;
    case GPIO_IDR:
        regval = (uint32_t)gpioa_compute_idr(s);
        break;
    case GPIO_ODR:
        regval = (uint32_t)s->odr;
        break;
    case GPIO_BSRR:
        // Write-only in hardware; return 0 in this model.
        regval = 0;
        break;
    case GPIO_BRR:
        // Write-only in hardware; return 0 in this model.
        regval = 0;
        break;
    case GPIO_LCKR:
        regval = s->lckr & 0x0001FFFFu;
        break;
    default:
        gpioa_debug_access("READ", addr, size, 0);
        return 0;
    }

    return gpioa_extract_u32(regval, offset, size);
}

// This function will emulate all device writes
void gpioa_write(void *opaque, hwaddr addr, uint64_t value, unsigned size) {
    GPIOAState *s = (GPIOAState *)opaque;
    hwaddr offset = addr - s->base;
    hwaddr reg = offset & ~0x3u;

    switch (reg) {
    case GPIO_CRL: {
        uint32_t new_crl = s->crl;
        gpioa_insert_u32(&new_crl, offset, value, size);
        s->crl = new_crl;
        gpioa_publish_levels(s);
        break;
    }

    case GPIO_CRH: {
        uint32_t new_crh = s->crh;
        gpioa_insert_u32(&new_crh, offset, value, size);
        s->crh = new_crh;
        gpioa_publish_levels(s);
        break;
    }

    case GPIO_ODR: {
        uint32_t new_odr = (uint32_t)s->odr;
        gpioa_insert_u32(&new_odr, offset, value, size);
        s->odr = (uint16_t)(new_odr & 0xFFFFu);
        gpioa_publish_levels(s);
        break;
    }

    case GPIO_BSRR: {
        uint32_t bsrr = 0;
        uint16_t set_bits;
        uint16_t reset_bits;

        gpioa_insert_u32(&bsrr, offset, value, size);

        set_bits = (uint16_t)(bsrr & 0xFFFFu);
        reset_bits = (uint16_t)((bsrr >> 16) & 0xFFFFu);

        s->odr = (uint16_t)(((uint32_t)s->odr | set_bits) & ~(uint32_t)reset_bits);
        gpioa_publish_levels(s);
        break;
    }

    case GPIO_BRR: {
        uint32_t brr = 0;
        gpioa_insert_u32(&brr, offset, value, size);
        s->odr &= (uint16_t)~(brr & 0xFFFFu);
        gpioa_publish_levels(s);
        break;
    }

    case GPIO_LCKR: {
        uint32_t new_lckr = s->lckr;
        gpioa_insert_u32(&new_lckr, offset, value, size);
        s->lckr = new_lckr & 0x0001FFFFu;
        break;
    }

    case GPIO_IDR:
        // Read-only register.
        break;

    default:
        gpioa_debug_access("WRITE", addr, size, value);
        break;
    }
}

// MUST return pointer to state — framework passes it as opaque to _read/_write
void* gpioa_init(ConfigSection* model_info) {
    (void)model_info;

    memset(&g_gpioa, 0, sizeof(g_gpioa));

    g_gpioa.base = GPIOA_BASE;

    // Trace-authoritative startup values:
    // CRL/CRH match the STM32F103 reset state, but runtime traces show the ODR
    // latch already contains PA13|PA15 high before the PA5 toggle loop begins.
    // ODR readback must preserve those latched bits across BSRR operations.
    g_gpioa.crl = 0x44444444u;
    g_gpioa.crh = 0x44444444u;
    g_gpioa.odr = 0xA000u;
    g_gpioa.lckr = 0x00000000u;

    g_gpioa.fifo_fd = api_fifo_open(GPIOA_FIFO_PATH);
    g_gpioa.published_levels = gpioa_compute_idr(&g_gpioa);

    return &g_gpioa;
}