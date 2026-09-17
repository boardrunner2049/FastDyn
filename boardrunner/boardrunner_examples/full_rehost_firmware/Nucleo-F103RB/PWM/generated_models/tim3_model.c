// Device Model for TIM3

#include <device.h>
#include <boardrunner/vio.h>

#include <stdint.h>
#include <stdbool.h>
#include <string.h>

#define TIM3_BASE           0x40000400ULL

#define TIM3_CR1            0x00
#define TIM3_CR2            0x04
#define TIM3_SMCR           0x08
#define TIM3_EGR            0x14
#define TIM3_CCMR1          0x18
#define TIM3_CCER           0x20
#define TIM3_CNT            0x24
#define TIM3_PSC            0x28
#define TIM3_ARR            0x2C
#define TIM3_CCR1           0x34

/* STM32F103 TIM3 is a 16-bit general-purpose timer.
 * Trace behavior matches a timer clock consistent with 72 MHz:
 *   PSC = 0x18FF => divide by 6400 => 11250 Hz counter clock.
 */
#define TIM3_INPUT_CLOCK_HZ 72000000ULL

typedef struct TIM3State {
    uint32_t cr1;
    uint32_t cr2;
    uint32_t smcr;
    uint32_t ccmr1;
    uint32_t ccer;
    uint32_t psc;
    uint32_t arr;
    uint32_t ccr1;

    /* Time-derived counter state */
    uint32_t cnt_base;      /* latched counter value at anchor_ns */
    int64_t  anchor_ns;     /* virtual time when cnt_base was latched */
} TIM3State;

static TIM3State g_tim3;

static uint32_t tim3_mask_by_size(uint32_t oldv, uint64_t value, hwaddr offset, unsigned size)
{
    if (size >= 4) {
        return (uint32_t)value;
    }

    unsigned shift = (unsigned)((offset & 0x3ULL) * 8ULL);
    uint32_t mask = (uint32_t)(((1ULL << (size * 8U)) - 1ULL) << shift);
    return (oldv & ~mask) | ((((uint32_t)value) << shift) & mask);
}

static uint64_t tim3_extract_by_size(uint32_t regv, hwaddr offset, unsigned size)
{
    if (size >= 4) {
        return regv;
    }

    unsigned shift = (unsigned)((offset & 0x3ULL) * 8ULL);
    uint64_t mask = (1ULL << (size * 8U)) - 1ULL;
    return (regv >> shift) & mask;
}

static uint32_t tim3_period(TIM3State *s)
{
    uint32_t arr = s->arr & 0xFFFFU;
    return arr + 1U;
}

static uint32_t tim3_compute_cnt_at(TIM3State *s, int64_t now_ns)
{
    uint32_t base = s->cnt_base & 0xFFFFU;

    if ((s->cr1 & 0x1U) == 0) {
        return base;
    }

    if (now_ns <= s->anchor_ns) {
        return base;
    }

    uint64_t delta_ns = (uint64_t)(now_ns - s->anchor_ns);
    uint64_t div = (uint64_t)((s->psc & 0xFFFFU) + 1U);
    uint32_t period = tim3_period(s);

    /* ticks = delta_ns * TIM3_INPUT_CLOCK_HZ / (1e9 * (PSC+1)) */
    uint64_t ticks = (uint64_t)(((unsigned __int128)delta_ns * TIM3_INPUT_CLOCK_HZ) /
                                ((unsigned __int128)1000000000ULL * div));

    if (period == 0) {
        return base;
    }

    return (uint32_t)((base + ticks) % period) & 0xFFFFU;
}

static void tim3_latch_now(TIM3State *s)
{
    int64_t now = qemu_plugin_get_virtual_timer();
    if (now < 0) {
        now = 0;
    }

    s->cnt_base = tim3_compute_cnt_at(s, now) & 0xFFFFU;
    s->anchor_ns = now;
}

static void tim3_normalize_cnt(TIM3State *s)
{
    uint32_t period = tim3_period(s);
    if (period == 0) {
        s->cnt_base = 0;
    } else {
        s->cnt_base %= period;
    }
    s->cnt_base &= 0xFFFFU;
}

uint64_t tim3_read(void *opaque, hwaddr addr, unsigned size)
{
    TIM3State *s = (TIM3State *)opaque;
    hwaddr offset = addr - TIM3_BASE;
    hwaddr reg = offset & ~0x3ULL;
    uint32_t regv = 0;

    switch (reg) {
    case TIM3_CR1:
        regv = s->cr1 & 0x03FFU;
        break;
    case TIM3_CR2:
        regv = s->cr2 & 0xFFFFU;
        break;
    case TIM3_SMCR:
        regv = s->smcr & 0xFFFFU;
        break;
    case TIM3_EGR:
        regv = 0; /* write-only in practice */
        break;
    case TIM3_CCMR1:
        regv = s->ccmr1 & 0xFFFFU;
        break;
    case TIM3_CCER:
        regv = s->ccer & 0xFFFFU;
        break;
    case TIM3_CNT: {
        int64_t now = qemu_plugin_get_virtual_timer();
        if (now < 0) {
            now = 0;
        }
        regv = tim3_compute_cnt_at(s, now) & 0xFFFFU;
        break;
    }
    case TIM3_PSC:
        regv = s->psc & 0xFFFFU;
        break;
    case TIM3_ARR:
        regv = s->arr & 0xFFFFU;
        break;
    case TIM3_CCR1:
        regv = s->ccr1 & 0xFFFFU;
        break;
    default:
        regv = 0;
        break;
    }

    return tim3_extract_by_size(regv, offset, size);
}

void tim3_write(void *opaque, hwaddr addr, uint64_t value, unsigned size)
{
    TIM3State *s = (TIM3State *)opaque;
    hwaddr offset = addr - TIM3_BASE;
    hwaddr reg = offset & ~0x3ULL;
    uint32_t merged;

    switch (reg) {
    case TIM3_CR1:
        tim3_latch_now(s);
        merged = tim3_mask_by_size(s->cr1, value, offset, size);
        s->cr1 = merged & 0x03FFU;
        break;

    case TIM3_CR2:
        merged = tim3_mask_by_size(s->cr2, value, offset, size);
        s->cr2 = merged & 0xFFFFU;
        break;

    case TIM3_SMCR:
        merged = tim3_mask_by_size(s->smcr, value, offset, size);
        s->smcr = merged & 0xFFFFU;
        break;

    case TIM3_EGR:
        /* Update generation (UG) resets counter timing state. */
        if (size >= 4) {
            merged = (uint32_t)value;
        } else {
            merged = (uint32_t)(value & ((1ULL << (size * 8U)) - 1ULL));
        }

        if (merged & 0x1U) {
            tim3_latch_now(s);
            s->cnt_base = 0;
            s->anchor_ns = qemu_plugin_get_virtual_timer();
            if (s->anchor_ns < 0) {
                s->anchor_ns = 0;
            }
        }
        break;

    case TIM3_CCMR1:
        merged = tim3_mask_by_size(s->ccmr1, value, offset, size);
        s->ccmr1 = merged & 0xFFFFU;
        break;

    case TIM3_CCER:
        merged = tim3_mask_by_size(s->ccer, value, offset, size);
        s->ccer = merged & 0xFFFFU;
        break;

    case TIM3_CNT:
        tim3_latch_now(s);
        merged = tim3_mask_by_size(s->cnt_base, value, offset, size);
        s->cnt_base = merged & 0xFFFFU;
        tim3_normalize_cnt(s);
        break;

    case TIM3_PSC:
        tim3_latch_now(s);
        merged = tim3_mask_by_size(s->psc, value, offset, size);
        s->psc = merged & 0xFFFFU;
        break;

    case TIM3_ARR:
        tim3_latch_now(s);
        merged = tim3_mask_by_size(s->arr, value, offset, size);
        s->arr = merged & 0xFFFFU;
        tim3_normalize_cnt(s);
        break;

    case TIM3_CCR1:
        merged = tim3_mask_by_size(s->ccr1, value, offset, size);
        s->ccr1 = merged & 0xFFFFU;
        break;

    default:
        /* Ignore unknown/unimplemented registers. */
        break;
    }
}

// MUST return &g_tim3 — framework stores this and passes it as opaque to _read/_write
void* tim3_init(ConfigSection* model_info)
{
    (void)model_info;

    memset(&g_tim3, 0, sizeof(g_tim3));

    /* Reset-like defaults for the subset we model. */
    g_tim3.arr = 0xFFFFU;
    g_tim3.psc = 0x0000U;
    g_tim3.cnt_base = 0x0000U;
    g_tim3.anchor_ns = qemu_plugin_get_virtual_timer();
    if (g_tim3.anchor_ns < 0) {
        g_tim3.anchor_ns = 0;
    }

    return &g_tim3;
}