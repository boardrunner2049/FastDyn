// Device Model for GPIOB
#include <device.h>
#include <boardrunner/vio.h>

#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <stdio.h>

#define GPIOB_BASE          0x40010C00ULL

#define GPIO_REG_CRL        0x00
#define GPIO_REG_CRH        0x04
#define GPIO_REG_IDR        0x08
#define GPIO_REG_ODR        0x0C
#define GPIO_REG_BSRR       0x10
#define GPIO_REG_BRR        0x14
#define GPIO_REG_LCKR       0x18

#define GPIO_PIN_COUNT      16

typedef struct {
    uint32_t crl;          // config low, PB0..PB7
    uint32_t crh;          // config high, PB8..PB15
    uint32_t odr;          // output data latch, low 16 bits used
    uint32_t lckr;         // low 17 bits meaningful on STM32F1

    /*
     * External/default input levels for pins that are not driven from ODR.
     * No host GPIO-input API exists in the provided framework, so this remains
     * a simple internal source initialized low.
     */
    uint16_t ext_inputs;

    /*
     * Last published logical levels. We publish each pin as signal_id == pin
     * (EXTI-style logical line number 0..15).
     */
    uint16_t last_levels;
    bool levels_valid;
} GPIOBState;

static GPIOBState g_gpiob;

static void gpiob_log_bad_access(const char *op, hwaddr addr, unsigned size, uint64_t value)
{
    char msg[160];
    snprintf(msg, sizeof(msg),
             "GPIOB %s unexpected access addr=0x%llx size=%u value=0x%llx",
             op,
             (unsigned long long)addr,
             size,
             (unsigned long long)value);
    dev_debug(msg);
}

static inline uint32_t gpiob_subread_u32(uint32_t reg, hwaddr offset, unsigned size)
{
    unsigned shift = (unsigned)(offset & 0x3ULL) * 8U;

    if (size == 4) {
        return reg;
    } else if (size == 2) {
        return (reg >> shift) & 0xFFFFU;
    } else if (size == 1) {
        return (reg >> shift) & 0xFFU;
    }

    return 0;
}

static inline uint32_t gpiob_subwrite_u32(uint32_t old_reg, hwaddr offset, uint64_t value, unsigned size)
{
    unsigned shift = (unsigned)(offset & 0x3ULL) * 8U;
    uint32_t mask;
    uint32_t v = (uint32_t)value;

    if (size == 4) {
        mask = 0xFFFFFFFFU;
    } else if (size == 2) {
        mask = 0xFFFFU << shift;
    } else if (size == 1) {
        mask = 0xFFU << shift;
    } else {
        return old_reg;
    }

    return (old_reg & ~mask) | ((v << shift) & mask);
}

static inline uint32_t gpiob_write_value_to_word(hwaddr offset, uint64_t value, unsigned size)
{
    unsigned shift = (unsigned)(offset & 0x3ULL) * 8U;

    if (size == 4) {
        return (uint32_t)value;
    } else if (size == 2) {
        return ((uint32_t)value & 0xFFFFU) << shift;
    } else if (size == 1) {
        return ((uint32_t)value & 0xFFU) << shift;
    }

    return 0;
}

static inline uint8_t gpiob_pin_cfg(GPIOBState *s, int pin)
{
    if (pin < 8) {
        return (uint8_t)((s->crl >> (pin * 4)) & 0xFU);
    }
    return (uint8_t)((s->crh >> ((pin - 8) * 4)) & 0xFU);
}

static inline bool gpiob_pin_is_output(GPIOBState *s, int pin)
{
    uint8_t cfg = gpiob_pin_cfg(s, pin);
    uint8_t mode = cfg & 0x3U;
    return mode != 0;
}

static inline bool gpiob_pin_is_input_pull(GPIOBState *s, int pin)
{
    uint8_t cfg = gpiob_pin_cfg(s, pin);
    uint8_t mode = cfg & 0x3U;
    uint8_t cnf  = (cfg >> 2) & 0x3U;

    /* STM32F1 input with pull-up/pull-down: MODE=00, CNF=10 => 0b1000 */
    return (mode == 0) && (cnf == 0x2U);
}

static inline bool gpiob_effective_pin_level(GPIOBState *s, int pin)
{
    /*
     * Minimal but stateful line model:
     * - output / AF-output pins reflect ODR
     * - input pull-up/down pins also reflect ODR (pull selection)
     * - other inputs reflect ext_inputs (default low)
     */
    if (gpiob_pin_is_output(s, pin) || gpiob_pin_is_input_pull(s, pin)) {
        return ((s->odr >> pin) & 0x1U) != 0;
    }

    return ((s->ext_inputs >> pin) & 0x1U) != 0;
}

static uint32_t gpiob_compute_idr(GPIOBState *s)
{
    uint32_t idr = 0;
    int pin;

    for (pin = 0; pin < GPIO_PIN_COUNT; pin++) {
        if (gpiob_effective_pin_level(s, pin)) {
            idr |= (1U << pin);
        }
    }

    return idr;
}

static void gpiob_publish_levels(GPIOBState *s)
{
    uint16_t new_levels = (uint16_t)gpiob_compute_idr(s);
    int pin;

    for (pin = 0; pin < GPIO_PIN_COUNT; pin++) {
        bool new_level = ((new_levels >> pin) & 0x1U) != 0;
        bool old_level = ((s->last_levels >> pin) & 0x1U) != 0;

        if (!s->levels_valid || new_level != old_level) {
            api_signal_set(pin, new_level);
        }
    }

    s->last_levels = new_levels;
    s->levels_valid = true;
}

static void gpiob_apply_bsrr(GPIOBState *s, uint32_t bsrr_word)
{
    uint16_t set_mask   = (uint16_t)(bsrr_word & 0xFFFFU);
    uint16_t reset_mask = (uint16_t)((bsrr_word >> 16) & 0xFFFFU);

    /*
     * STM32F1 semantics: lower half sets, upper half resets. If both halves
     * target the same bit in one write, keep set dominant.
     */
    reset_mask &= (uint16_t)~set_mask;

    s->odr |= set_mask;
    s->odr &= ~(uint32_t)reset_mask;
    s->odr &= 0xFFFFU;
}

static void gpiob_apply_brr(GPIOBState *s, uint32_t brr_word)
{
    s->odr &= ~((uint32_t)brr_word & 0xFFFFU);
    s->odr &= 0xFFFFU;
}

// This function will emulate all device reads
uint64_t gpiob_read(void *opaque, hwaddr addr, unsigned size)
{
    GPIOBState *s = (GPIOBState *)opaque;
    hwaddr offset;
    hwaddr reg;

    if (s == NULL) {
        s = &g_gpiob;
    }

    if (addr < GPIOB_BASE) {
        gpiob_log_bad_access("read", addr, size, 0);
        return 0;
    }

    offset = addr - GPIOB_BASE;
    reg = offset & ~0x3ULL;

    if (!(size == 1 || size == 2 || size == 4)) {
        gpiob_log_bad_access("read", addr, size, 0);
        return 0;
    }

    switch (reg) {
    case GPIO_REG_CRL:
        return gpiob_subread_u32(s->crl, offset, size);

    case GPIO_REG_CRH:
        return gpiob_subread_u32(s->crh, offset, size);

    case GPIO_REG_IDR: {
        uint32_t idr = gpiob_compute_idr(s);
        return gpiob_subread_u32(idr, offset, size);
    }

    case GPIO_REG_ODR:
        return gpiob_subread_u32(s->odr & 0xFFFFU, offset, size);

    case GPIO_REG_BSRR:
        /* Write-only in hardware; return 0 for reads. */
        return 0;

    case GPIO_REG_BRR:
        /* Write-only in hardware; return 0 for reads. */
        return 0;

    case GPIO_REG_LCKR:
        return gpiob_subread_u32(s->lckr & 0x0001FFFFU, offset, size);

    default:
        gpiob_log_bad_access("read", addr, size, 0);
        return 0;
    }
}

// This function will emulate all device writes
void gpiob_write(void *opaque, hwaddr addr, uint64_t value, unsigned size)
{
    GPIOBState *s = (GPIOBState *)opaque;
    hwaddr offset;
    hwaddr reg;

    if (s == NULL) {
        s = &g_gpiob;
    }

    if (addr < GPIOB_BASE) {
        gpiob_log_bad_access("write", addr, size, value);
        return;
    }

    offset = addr - GPIOB_BASE;
    reg = offset & ~0x3ULL;

    if (!(size == 1 || size == 2 || size == 4)) {
        gpiob_log_bad_access("write", addr, size, value);
        return;
    }

    switch (reg) {
    case GPIO_REG_CRL:
        s->crl = gpiob_subwrite_u32(s->crl, offset, value, size);
        gpiob_publish_levels(s);
        break;

    case GPIO_REG_CRH:
        s->crh = gpiob_subwrite_u32(s->crh, offset, value, size);
        gpiob_publish_levels(s);
        break;

    case GPIO_REG_IDR:
        /* Read-only in hardware; ignore writes. */
        break;

    case GPIO_REG_ODR:
        s->odr = gpiob_subwrite_u32(s->odr, offset, value, size) & 0xFFFFU;
        gpiob_publish_levels(s);
        break;

    case GPIO_REG_BSRR: {
        uint32_t bsrr_word = gpiob_write_value_to_word(offset, value, size);
        gpiob_apply_bsrr(s, bsrr_word);
        gpiob_publish_levels(s);
        break;
    }

    case GPIO_REG_BRR: {
        uint32_t brr_word = gpiob_write_value_to_word(offset, value, size);
        gpiob_apply_brr(s, brr_word);
        gpiob_publish_levels(s);
        break;
    }

    case GPIO_REG_LCKR:
        /*
         * Minimal stateful implementation: store written value.
         * Full lock-key sequencing is not needed by the trace.
         */
        s->lckr = gpiob_subwrite_u32(s->lckr, offset, value, size) & 0x0001FFFFU;
        break;

    default:
        gpiob_log_bad_access("write", addr, size, value);
        break;
    }
}

// MUST return pointer to state — framework passes it as opaque to _read/_write
void* gpiob_init(ConfigSection* model_info)
{
    (void)model_info;

    memset(&g_gpiob, 0, sizeof(g_gpiob));

    /* STM32F103 GPIO reset values */
    g_gpiob.crl = 0x44444444U;
    g_gpiob.crh = 0x44444444U;
    g_gpiob.odr = 0x00000000U;
    g_gpiob.lckr = 0x00000000U;
    g_gpiob.ext_inputs = 0x0000U;
    g_gpiob.last_levels = 0x0000U;
    g_gpiob.levels_valid = false;

    /* Publish initial levels for any listening peripherals. */
    gpiob_publish_levels(&g_gpiob);

    return &g_gpiob;
}