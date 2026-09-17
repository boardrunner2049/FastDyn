// Device Model for GPIOC

#include <device.h>
#include <boardrunner/vio.h>

#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <stdio.h>

#define GPIOC_BASE          0x40011000ULL

#define GPIO_CRL_OFFSET     0x00
#define GPIO_CRH_OFFSET     0x04
#define GPIO_IDR_OFFSET     0x08
#define GPIO_ODR_OFFSET     0x0C
#define GPIO_BSRR_OFFSET    0x10
#define GPIO_BRR_OFFSET     0x14
#define GPIO_LCKR_OFFSET    0x18

#define GPIO_NUM_PINS       16
#define GPIO_PC13           13

/* EXTI line 13 signal id. */
#define GPIOC_PC13_SIGNAL_ID 13

/* Host-side FIFO for driving the PC13 input (e.g. user button). */
#define GPIOC_FIFO_PATH     "/tmp/gpioc_pc13"
#define GPIOC_POLL_PERIOD_NS 1000000ULL  /* 1 ms */

typedef struct {
    uint32_t crl;
    uint32_t crh;
    uint32_t odr;
    uint32_t lckr;

    /* Host-driven external state for PC13. */
    bool pc13_host_override_valid;
    bool pc13_host_level;

    /* Last published logical level for api_signal_set(). */
    bool pc13_last_published_valid;
    bool pc13_last_published_level;

    int fifo_fd;
    uint64_t poll_timer;
} GPIOCState;

static GPIOCState g_gpioc;

static void gpioc_debug(const char *msg)
{
    char buf[128];
    snprintf(buf, sizeof(buf), "GPIOC: %s", msg);
    dev_debug(buf);
}

static uint64_t gpioc_extract_subreg(uint32_t reg, hwaddr addr, unsigned size)
{
    unsigned shift = (unsigned)(addr & 0x3U) * 8U;
    uint64_t mask;

    if (size >= 4) {
        return reg;
    }

    mask = (1ULL << (size * 8U)) - 1ULL;
    return (reg >> shift) & mask;
}

static uint32_t gpioc_apply_subwrite32(uint32_t oldv, hwaddr addr, uint64_t value, unsigned size)
{
    unsigned shift = (unsigned)(addr & 0x3U) * 8U;
    uint32_t mask;
    uint32_t v = (uint32_t)value;

    if (size >= 4) {
        return v;
    }

    mask = ((1U << (size * 8U)) - 1U) << shift;
    return (oldv & ~mask) | ((v << shift) & mask);
}

static uint8_t gpioc_get_pin_cfg(GPIOCState *s, int pin)
{
    if (pin < 8) {
        return (s->crl >> (pin * 4)) & 0xF;
    }
    return (s->crh >> ((pin - 8) * 4)) & 0xF;
}

static bool gpioc_pin_is_output(GPIOCState *s, int pin)
{
    uint8_t cfg = gpioc_get_pin_cfg(s, pin);
    return (cfg & 0x3) != 0; /* MODE != 00 */
}

static bool gpioc_get_pin_level(GPIOCState *s, int pin)
{
    uint8_t cfg = gpioc_get_pin_cfg(s, pin);
    bool is_output = (cfg & 0x3) != 0;
    uint8_t cnf = (cfg >> 2) & 0x3;

    if (pin == GPIO_PC13) {
        if (is_output) {
            return ((s->odr >> pin) & 1U) != 0;
        }

        if (s->pc13_host_override_valid) {
            return s->pc13_host_level;
        }

        /* Input with pull-up/pull-down: ODR selects pull direction. */
        if (cnf == 0x2) {
            return ((s->odr >> pin) & 1U) != 0;
        }

        /* For unmodeled external inputs, default high matches observed trace. */
        return true;
    }

    if (is_output) {
        return ((s->odr >> pin) & 1U) != 0;
    }

    if (cnf == 0x2) {
        return ((s->odr >> pin) & 1U) != 0;
    }

    /* Unmodeled non-PC13 inputs default high to match observed runtime values. */
    return true;
}

static uint32_t gpioc_compute_idr(GPIOCState *s)
{
    uint32_t idr = 0;
    int pin;

    for (pin = 0; pin < GPIO_NUM_PINS; pin++) {
        if (gpioc_get_pin_level(s, pin)) {
            idr |= (1U << pin);
        }
    }

    return idr & 0xFFFFU;
}

static void gpioc_publish_pc13(GPIOCState *s)
{
    bool level = gpioc_get_pin_level(s, GPIO_PC13);

    if (!s->pc13_last_published_valid || s->pc13_last_published_level != level) {
        api_signal_set(GPIOC_PC13_SIGNAL_ID, level);
        s->pc13_last_published_level = level;
        s->pc13_last_published_valid = true;
    }
}

static void gpioc_handle_fifo_byte(GPIOCState *s, uint8_t b)
{
    switch (b) {
    case '0':
    case 'p':
    case 'P':
        s->pc13_host_override_valid = true;
        s->pc13_host_level = false; /* button pressed / line low */
        gpioc_publish_pc13(s);
        break;

    case '1':
    case 'r':
    case 'R':
        s->pc13_host_override_valid = true;
        s->pc13_host_level = true;  /* button released / line high */
        gpioc_publish_pc13(s);
        break;

    case 't':
    case 'T':
        s->pc13_host_override_valid = true;
        s->pc13_host_level = !gpioc_get_pin_level(s, GPIO_PC13);
        gpioc_publish_pc13(s);
        break;

    case 'x':
    case 'X':
        /* Clear external override; line falls back to GPIO configuration/pull. */
        s->pc13_host_override_valid = false;
        gpioc_publish_pc13(s);
        break;

    case '\n':
    case '\r':
    case ' ':
    case '\t':
        break;

    default:
        gpioc_debug("ignoring unknown FIFO command byte");
        break;
    }
}

static void gpioc_poll_host(void *opaque)
{
    GPIOCState *s = (GPIOCState *)opaque;
    uint8_t b;
    int rc;

    if (s->fifo_fd < 0) {
        return;
    }

    for (;;) {
        rc = api_fifo_read_nonblock(s->fifo_fd, &b);
        if (rc != 1) {
            break;
        }
        gpioc_handle_fifo_byte(s, b);
    }
}

uint64_t gpioc_read(void *opaque, hwaddr addr, unsigned size)
{
    GPIOCState *s = (GPIOCState *)opaque;
    hwaddr offset;
    uint32_t reg = 0;

    if (addr < GPIOC_BASE) {
        gpioc_debug("read below base");
        return 0;
    }

    offset = addr - GPIOC_BASE;

    switch (offset & ~0x3ULL) {
    case GPIO_CRL_OFFSET:
        reg = s->crl;
        break;
    case GPIO_CRH_OFFSET:
        reg = s->crh;
        break;
    case GPIO_IDR_OFFSET:
        /*
         * Compute dynamically so tight polling sees the current effective pin level
         * even if timer delivery is delayed until a TB boundary.
         */
        reg = gpioc_compute_idr(s);
        break;
    case GPIO_ODR_OFFSET:
        reg = s->odr & 0xFFFFU;
        break;
    case GPIO_BSRR_OFFSET:
        reg = 0; /* write-only in practice */
        break;
    case GPIO_BRR_OFFSET:
        reg = 0; /* write-only in practice */
        break;
    case GPIO_LCKR_OFFSET:
        reg = s->lckr;
        break;
    default:
        gpioc_debug("read from unknown offset");
        reg = 0;
        break;
    }

    return gpioc_extract_subreg(reg, addr, size);
}

void gpioc_write(void *opaque, hwaddr addr, uint64_t value, unsigned size)
{
    GPIOCState *s = (GPIOCState *)opaque;
    hwaddr offset;
    uint32_t writev;

    if (addr < GPIOC_BASE) {
        gpioc_debug("write below base");
        return;
    }

    offset = addr - GPIOC_BASE;

    switch (offset & ~0x3ULL) {
    case GPIO_CRL_OFFSET:
        s->crl = gpioc_apply_subwrite32(s->crl, addr, value, size);
        gpioc_publish_pc13(s);
        break;

    case GPIO_CRH_OFFSET:
        s->crh = gpioc_apply_subwrite32(s->crh, addr, value, size);
        gpioc_publish_pc13(s);
        break;

    case GPIO_ODR_OFFSET:
        s->odr = gpioc_apply_subwrite32(s->odr, addr, value, size) & 0xFFFFU;
        gpioc_publish_pc13(s);
        break;

    case GPIO_BSRR_OFFSET:
        writev = (size >= 4) ? (uint32_t)value : gpioc_apply_subwrite32(0, addr, value, size);
        s->odr |= (writev & 0xFFFFU);
        s->odr &= ~((writev >> 16) & 0xFFFFU);
        s->odr &= 0xFFFFU;
        gpioc_publish_pc13(s);
        break;

    case GPIO_BRR_OFFSET:
        writev = (size >= 4) ? (uint32_t)value : gpioc_apply_subwrite32(0, addr, value, size);
        s->odr &= ~(writev & 0xFFFFU);
        s->odr &= 0xFFFFU;
        gpioc_publish_pc13(s);
        break;

    case GPIO_LCKR_OFFSET:
        s->lckr = gpioc_apply_subwrite32(s->lckr, addr, value, size);
        break;

    case GPIO_IDR_OFFSET:
        /* Read-only; ignore writes. */
        break;

    default:
        gpioc_debug("write to unknown offset");
        break;
    }
}

/* MUST return &g_gpioc — framework stores this and passes it as opaque to _read/_write */
void* gpioc_init(ConfigSection* model_info)
{
    (void)model_info;

    memset(&g_gpioc, 0, sizeof(g_gpioc));

    /* STM32F103 GPIO reset defaults. */
    g_gpioc.crl = 0x44444444U;
    g_gpioc.crh = 0x44444444U;
    g_gpioc.odr = 0x00000000U;
    g_gpioc.lckr = 0x00000000U;

    /*
     * Default external state for PC13 is "not actively driven";
     * once configured as pull-up input, firmware's BSRR write makes it read high.
     */
    g_gpioc.pc13_host_override_valid = false;
    g_gpioc.pc13_host_level = true;
    g_gpioc.pc13_last_published_valid = false;
    g_gpioc.pc13_last_published_level = true;

    g_gpioc.fifo_fd = api_fifo_open(GPIOC_FIFO_PATH);
    if (g_gpioc.fifo_fd < 0) {
        gpioc_debug("failed to open host FIFO");
    }

    g_gpioc.poll_timer = qemu_plugin_timer_new_period_ns(gpioc_poll_host,
                                                         &g_gpioc,
                                                         GPIOC_POLL_PERIOD_NS);

    /* Publish initial PC13 level to EXTI listeners. */
    gpioc_publish_pc13(&g_gpioc);

    return &g_gpioc;
}