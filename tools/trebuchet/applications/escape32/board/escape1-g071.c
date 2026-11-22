/* SPDX-License-Identifier: GPL-2.0-or-later */
/* ESCAPE1 boot/maintenance slice. Register definitions follow STM32 RM0444. */
#include "qemu/osdep.h"
#include <math.h>
#include "qapi/error.h"
#include "qemu/units.h"
#include "qemu/timer.h"
#include "qemu/log.h"
#include "hw/boards.h"
#include "hw/arm/armv7m.h"
#include "hw/arm/boot.h"
#include "hw/irq.h"
#include "hw/gpio/stm32l4x5_gpio.h"
#include "qemu/host-utils.h"
#include "hw/qdev-clock.h"
#include "hw/qdev-properties.h"
#include "hw/qdev-properties-system.h"
#include "hw/char/stm32l4x5_usart.h"
#include "system/address-spaces.h"
#include "system/system.h"
#include "system/runstate.h"

#define HSI 16000000U
#define PLL_ON (1U << 24)
#define PLL_READY (1U << 25)
#define REG(off) ((off) / 4)

typedef struct {
    MemoryRegion io;
    QEMUTimer *alarm;
    uint32_t reg[0x80 / 4];
    uint32_t divisor, period, hz, counter_mask;
    void (*on_update)(void *);
    void *update_ctx;
    int64_t epoch;
    bool enabled;
    qemu_irq irq;
} G0Timer;

typedef struct {
    Clock *cpu, *ref, *usart, *usart1, *gpio[6];
    MemoryRegion flash, alias, rcc, crc, dbg, flash_acr;
    uint32_t acr;
    uint32_t regs[0x64 / 4];
    uint32_t crc_reg, crc_ctl;
    QEMUTimer *lock;
    G0Timer tim1, tim2, tim3, tim6, tim15, tim16;
    MemoryRegion syscfg, mux, dma, adc, adc_common, factory, comp, iwdg, wwdg;
    qemu_irq pwm_pin, hall_pin[3];
    QEMUTimer *servo, *watchdog, *plant_timer;
    bool high, bench, iwdg_running;
    unsigned pulse_us, frame, hall_code, commutations;
    double brake_duty;
    int drive_pair;
    double drive_duty, bus_current;
    double angle, speed, current;
    uint32_t cfgr1, mux_channels[7], dma_regs[0x94 / 4], adc_regs[0x44 / 4], comp_regs[2];
    uint16_t factory_words[0x24 / 2];
    qemu_irq dma_irq, tx_dma_irq;
    QEMUTimer *tx_event;
    unsigned tx_index, tx_packets;
    QEMUTimer *adc_event;
    unsigned adc_phase, adc_index, adc_scans;
    uint32_t adc_cr, adc_cfgr2, adc_ccr;
    int64_t regulator_on_ns;
} G0Board;

static void timer_arm(G0Timer *t)
{
    if (t->enabled && t->hz && (t->reg[0] & 1) && t->period) {
        uint64_t ticks = ((uint64_t)t->period + 1) * t->divisor;
        ticks *= (t->reg[REG(0x30)] & 0xff) + 1;
        timer_mod_ns(t->alarm, t->epoch + muldiv64(ticks, 1000000000ULL, t->hz));
    } else {
        timer_del(t->alarm);
    }
}

static void timer_update(void *opaque)
{
    G0Timer *t = opaque;
    t->reg[REG(0x10)] |= 1;
    t->divisor = (t->reg[REG(0x28)] & 0xffff) + 1;
    t->period = t->reg[REG(0x2c)] & t->counter_mask;
    if (t->reg[0] & 8) {
        t->reg[0] &= ~1U; /* OPM stops after update. */
    }
    t->epoch = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    qemu_set_irq(t->irq, !!(t->reg[REG(0x0c)] & t->reg[REG(0x10)] & 63));
    timer_arm(t);
    if (t->on_update) {
        t->on_update(t->update_ctx);
    }
}

static uint64_t timer_read(void *opaque, hwaddr a, unsigned size)
{
    G0Timer *t = opaque;
    if (!t->enabled) {
        return 0;
    }
    if (a == 0x24 && (t->reg[0] & 1)) {
        uint64_t ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) - t->epoch;
        return muldiv64(ns, t->hz, 1000000000U) / t->divisor % ((uint64_t)t->period + 1);
    }
    if (a >= 0x34 && a <= 0x40) {
        unsigned channel = (a - 0x34) / 4;
        uint32_t ccmr = t->reg[REG(0x18) + channel / 2];
        if ((ccmr >> (8 * (channel % 2))) & 3) {
            t->reg[REG(0x10)] &= ~(2U << channel);
            qemu_set_irq(t->irq, !!(t->reg[REG(0x0c)] & t->reg[REG(0x10)] & 31));
        }
    }
    return t->reg[REG(a)];
}

static void timer_write(void *opaque, hwaddr a, uint64_t v, unsigned size)
{
    G0Timer *t = opaque;
    if (!t->enabled) {
        return;
    }
    switch (a) {
    case 0:
        if (!(t->reg[0] & 1) && (v & 1)) {
            t->epoch = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
        }
        t->reg[0] = v;
        break;
    case 0x14: /* UG loads PSC/ARR and restarts the prescaler. */
        if (v & 0x20) {
            t->reg[REG(0x10)] |= 0x20;
        }
        if (v & 1) {
            t->divisor = (t->reg[REG(0x28)] & 0xffff) + 1;
            t->period = t->reg[REG(0x2c)] & t->counter_mask;
            t->epoch = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
            if (!(t->reg[0] & 4)) {
                t->reg[REG(0x10)] |= 1;
            }
        }
        break;
    case 0x10:
        t->reg[REG(a)] &= v; /* rc_w0 */
        break;
    case 0x2c:
        t->reg[REG(a)] = v & t->counter_mask;
        if (!(t->reg[0] & (1 << 7))) {
            t->period = v & t->counter_mask;
        }
        break;
    default:
        t->reg[REG(a)] = v;
        break;
    }
    qemu_set_irq(t->irq, !!(t->reg[REG(0x0c)] & t->reg[REG(0x10)] & 63));
    timer_arm(t);
}

static const MemoryRegionOps tim_ops = {
    .read = timer_read, .write = timer_write, .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 4, .max_access_size = 4 },
};

static void timer_init_region(G0Timer *t, Object *owner, const char *name,
                              hwaddr base, qemu_irq irq)
{
    t->irq = irq;
    t->divisor = 1;
    t->counter_mask = base == 0x40000000 ? UINT32_MAX : UINT16_MAX;
    t->reg[REG(0x2c)] = t->period = t->counter_mask;
    t->alarm = timer_new_ns(QEMU_CLOCK_VIRTUAL, timer_update, t);
    memory_region_init_io(&t->io, owner, &tim_ops, t, name, sizeof(t->reg));
    memory_region_add_subregion(get_system_memory(), base, &t->io);
}

static void timer_peripheral_reset(G0Timer *t)
{
    memset(t->reg, 0, sizeof(t->reg));
    t->reg[REG(0x2c)] = t->period = t->counter_mask;
    t->divisor = 1;
    timer_del(t->alarm);
    qemu_irq_lower(t->irq);
}

static void clocks(G0Board *s)
{
    uint32_t cfgr = s->regs[REG(0x08)];
    uint32_t pll = s->regs[REG(0x0c)];
    unsigned freq = HSI;
    unsigned sw = cfgr & 7;
    if (sw == 2 && (s->regs[0] & PLL_READY) && (pll & (1U << 28))) {
        unsigned m = ((pll >> 4) & 7) + 1, n = (pll >> 8) & 127;
        unsigned r = ((pll >> 29) & 7) + 1;
        freq = (HSI / m) * n / r;
    } else {
        sw = 0;
    }
    s->regs[REG(0x08)] = (cfgr & ~(7U << 3)) | (sw << 3);
    clock_update_hz(s->cpu, freq);
    clock_update_hz(s->ref, freq / 8);
    clock_update_hz(s->usart, s->regs[REG(0x3c)] & (1U << 17) ? freq : 0);
    for (unsigned i = 0; i < ARRAY_SIZE(s->gpio); i++) {
        if (s->gpio[i]) {
            clock_update_hz(s->gpio[i], (s->regs[REG(0x34)] & (1U << i)) ? freq : 0);
        }
    }
    s->tim1.enabled = !!(s->regs[REG(0x40)] & (1U << 11));
    s->tim6.enabled = !!(s->regs[REG(0x3c)] & (1U << 4));
    s->tim2.enabled = !!(s->regs[REG(0x3c)] & 1);
    s->tim3.enabled = !!(s->regs[REG(0x3c)] & 2);
    s->tim15.enabled = !!(s->regs[REG(0x40)] & (1U << 16));
    s->tim16.enabled = !!(s->regs[REG(0x40)] & (1U << 17));
    clock_update_hz(s->usart1, s->regs[REG(0x40)] & (1U << 14) ? freq : 0);
    s->tim2.hz = s->tim3.hz = s->tim15.hz = s->tim16.hz = freq;
    s->tim1.hz = s->tim6.hz = freq;
    timer_arm(&s->tim2);
    timer_arm(&s->tim3);
    timer_arm(&s->tim15);
    timer_arm(&s->tim16);
    timer_arm(&s->tim1);
    timer_arm(&s->tim6);
}

static void pll_locked(void *opaque)
{
    G0Board *s = opaque;
    s->regs[0] |= PLL_READY;
    clocks(s);
}

static uint64_t rcc_read(void *opaque, hwaddr a, unsigned size)
{
    G0Board *s = opaque;
    return s->regs[REG(a)];
}

static void rcc_write(void *opaque, hwaddr a, uint64_t v, unsigned size)
{
    G0Board *s = opaque;
    switch (a) {
    case 0:
        if (!(v & PLL_ON)) {
            timer_del(s->lock);
            s->regs[0] &= ~(PLL_ON | PLL_READY);
        } else if (!(s->regs[0] & PLL_ON)) {
            s->regs[0] |= PLL_ON;
            if ((s->regs[REG(0x0c)] & 3) == 2) {
                timer_mod_ns(s->lock, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 100000);
            }
        }
        break;
    case 0x2c:
        if (v & 1) { timer_peripheral_reset(&s->tim2); }
        if (v & 2) { timer_peripheral_reset(&s->tim3); }
        if (v & 16) { timer_peripheral_reset(&s->tim6); }
        s->regs[REG(a)] = v;
        break;
    case 0x30:
        if (v & (1 << 11)) { timer_peripheral_reset(&s->tim1); }
        if (v & (1 << 16)) { timer_peripheral_reset(&s->tim15); }
        if (v & (1 << 17)) { timer_peripheral_reset(&s->tim16); }
        s->regs[REG(a)] = v;
        break;
    case 0x60:
        if (v & (1U << 23)) {
            s->regs[REG(a)] &= 0x00ffffff;
        }
        break;
    case 0x0c:
        if (s->regs[0] & PLL_ON) {
            return;
        }
        G_GNUC_FALLTHROUGH;
    default:
        s->regs[REG(a)] = v;
        break;
    }
    clocks(s);
}

static uint32_t reverse_bits(uint32_t v)
{
    uint32_t r = 0;
    for (unsigned i = 0; i < 32; i++) {
        r = (r << 1) | (v & 1);
        v >>= 1;
    }
    return r;
}

static uint64_t crc_read(void *opaque, hwaddr a, unsigned size)
{
    G0Board *s = opaque;
    if (!(s->regs[REG(0x38)] & (1U << 12))) {
        return 0;
    }
    return a == 0 ? (s->crc_ctl & 0x80 ? reverse_bits(s->crc_reg) : s->crc_reg) : s->crc_ctl;
}

static void crc_write(void *opaque, hwaddr a, uint64_t v, unsigned size)
{
    G0Board *s = opaque;
    if (!(s->regs[REG(0x38)] & (1U << 12))) {
        return;
    }
    if (a == 8) {
        s->crc_ctl = v & ~1U;
        if (v & 1) {
            s->crc_reg = UINT32_MAX;
        }
    } else if (a == 0) {
        if ((s->crc_ctl & 0x60) == 0x60) {
            v = reverse_bits(v);
        }
        s->crc_reg ^= v;
        for (unsigned i = 0; i < 32; i++) {
            s->crc_reg = (s->crc_reg << 1) ^ ((s->crc_reg & 0x80000000U) ? 0x04c11db7U : 0);
        }
    }
}

static const MemoryRegionOps rcc_ops = {
    .read = rcc_read, .write = rcc_write, .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 4, .max_access_size = 4 },
};
static const MemoryRegionOps crc_ops = {
    .read = crc_read, .write = crc_write, .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 4, .max_access_size = 4 },
};

static uint64_t acr_read(void *opaque, hwaddr a, unsigned size)
{
    return ((G0Board *)opaque)->acr;
}

static void acr_write(void *opaque, hwaddr a, uint64_t value, unsigned size)
{
    ((G0Board *)opaque)->acr = value & 0x00070307;
}

static const MemoryRegionOps acr_ops = {
    .read = acr_read, .write = acr_write, .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 4, .max_access_size = 4 },
};

static uint64_t id_read(void *opaque, hwaddr a, unsigned size)
{
    return 0x10000460; /* selected G07x DEV_ID and revision */
}

static const MemoryRegionOps id_ops = {
    .read = id_read, .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 4, .max_access_size = 4 },
};

/* Clock-gated setup, DMA routing and ADC register semantics used by ESCAPE1. */
static uint64_t setup_read(void *opaque, hwaddr a, unsigned size)
{
    G0Board *s = opaque;
    if (a == 0x3000 || a == 0x2c00) {
        return 0;
    }
    if (a == 0x10000 && (s->regs[REG(0x40)] & 1)) {
        return s->cfgr1;
    }
    if (a >= 0x20800 && a < 0x2081c && (s->regs[REG(0x38)] & 1)) {
        return s->mux_channels[REG(a - 0x20800)];
    }
    if (a >= 0x20000 && a < 0x20094 && (s->regs[REG(0x38)] & 1)) {
        return s->dma_regs[REG(a - 0x20000)];
    }
    if (a >= 0x10200 && a < 0x10208) {
        return s->comp_regs[REG(a - 0x10200)];
    }
    if (s->regs[REG(0x40)] & (1 << 20)) {
        switch (a) {
        case 0x12408: return s->adc_cr;
        case 0x12410: return s->adc_cfgr2;
        case 0x12708: return s->adc_ccr;
        default:
            if (a >= 0x12400 && a < 0x12444) {
                return s->adc_regs[REG(a - 0x12400)];
            }
        }
    }
    return 0;
}

static void setup_write(void *opaque, hwaddr a, uint64_t value, unsigned size)
{
    G0Board *s = opaque;
    if (a == 0x3000) {
        if (value == 0xcccc) {
            s->iwdg_running = true;
        }
        if (s->iwdg_running && (value == 0xcccc || value == 0xaaaa)) {
            /* Reset PR=4, RLR=4095, nominal LSI=32 kHz. */
            timer_mod_ns(s->watchdog, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 512000000);
        }
        return;
    }
    if (a == 0x2c00) {
        if ((s->regs[REG(0x3c)] & (1 << 11)) && (value & 128) && (value & 127) < 64) {
            qemu_log_mask(CPU_LOG_INT | CPU_LOG_PAGE, "ESCAPE1 WWDG reset\n");
            qemu_system_reset_request(SHUTDOWN_CAUSE_GUEST_RESET);
        }
        return;
    }
    if (a == 0x10000 && (s->regs[REG(0x40)] & 1)) {
        s->cfgr1 = value;
    } else if (a >= 0x20800 && a < 0x2081c && (s->regs[REG(0x38)] & 1)) {
        s->mux_channels[REG(a - 0x20800)] = value & 0x7f;
    } else if (a >= 0x20000 && a < 0x20094 && (s->regs[REG(0x38)] & 1)) {
        if (a == 0x20004) {
            s->dma_regs[0] &= ~value;
            qemu_set_irq(s->dma_irq, !!(s->dma_regs[0] & 0x00202000));
            qemu_set_irq(s->tx_dma_irq, !!(s->dma_regs[0] & 0x200));
        } else if (a != 0x20000) {
            s->dma_regs[REG(a - 0x20000)] = value;
            if (a == 0x20030 && (value & 0x11) == 0x11 && s->mux_channels[2] == 51) {
                s->tx_index = 0;
                timer_mod_ns(s->tx_event, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 100);
            }
        }
    } else if (a >= 0x10200 && a < 0x10208) {
        s->comp_regs[REG(a - 0x10200)] = value;
    } else if (s->regs[REG(0x40)] & (1 << 20)) {
        switch (a) {
        case 0x12410: s->adc_cfgr2 = value; break;
        case 0x12708: s->adc_ccr = value; break;
        case 0x12408:
            if (!(s->adc_cr & (1U << 28)) && (value & (1U << 28))) {
                s->regulator_on_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
            }
            if (value & (1U << 31)) {
                qemu_log_mask(CPU_LOG_INT | CPU_LOG_PAGE,
                    "ESCAPE1 ADCAL sysclk=%u tim6_psc=%u tim6_arr=%u "
                    "tim6_cen=%u tim6_uif=%u regulator_ns=%" PRId64
                    " adc_cfgr2=0x%x adc_ccr=0x%x mux=%u,%u,%u,%u\n",
                    s->tim6.hz, s->tim6.divisor - 1, s->tim6.period,
                    s->tim6.reg[0] & 1, s->tim6.reg[REG(0x10)] & 1,
                    qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) - s->regulator_on_ns,
                    s->adc_cfgr2, s->adc_ccr, s->mux_channels[0],
                    s->mux_channels[1], s->mux_channels[2], s->mux_channels[3]);
            }
            if (value & (1U << 31)) {
                s->adc_phase = 1;
                timer_mod_ns(s->adc_event, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 10000);
            } else if ((value & 1) && !(s->adc_cr & 1)) {
                s->adc_phase = 2;
                timer_mod_ns(s->adc_event, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 1000);
            }
            s->adc_cr = value | (s->adc_cr & 1);
            break;
        case 0x12400:
            s->adc_regs[0] &= ~value;
            break;
        default:
            if (a >= 0x12400 && a < 0x12444) {
                s->adc_regs[REG(a - 0x12400)] = value;
                if (a == 0x12428) {
                    s->adc_regs[0] |= 1U << 13;
                }
            }
            break;
        }
    }
}

/* Access callbacks take offsets relative to the APB base, while mappings
 * expose only the register windows exercised before ADC calibration. */
#define SETUP_OPS(name, offset) \
static uint64_t name ## _read(void *p, hwaddr a, unsigned n) \
{ return setup_read(p, (offset) + a, n); } \
static void name ## _write(void *p, hwaddr a, uint64_t v, unsigned n) \
{ setup_write(p, (offset) + a, v, n); } \
static const MemoryRegionOps name ## _ops = { \
    .read = name ## _read, .write = name ## _write, .endianness = DEVICE_LITTLE_ENDIAN, \
    .valid = { .min_access_size = 4, .max_access_size = 4 }, \
}

SETUP_OPS(syscfg, 0x10000);
SETUP_OPS(mux, 0x20800);
SETUP_OPS(dma, 0x20000);
SETUP_OPS(comp, 0x10200);
SETUP_OPS(iwdg, 0x3000);
SETUP_OPS(wwdg, 0x2c00);
SETUP_OPS(adc, 0x12400);
SETUP_OPS(adc_common, 0x12708);

static uint64_t factory_read(void *opaque, hwaddr a, unsigned size)
{
    G0Board *s = opaque;
    return s->factory_words[a / 2];
}

static const MemoryRegionOps factory_ops = {
    .read = factory_read, .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 2, .max_access_size = 2 },
};

static void adc_trigger(void *opaque)
{
    G0Board *s = opaque;
    if (!s->adc_phase && (s->adc_cr & 5) == 5 &&
        ((s->tim1.reg[1] >> 20) & 15) == 3 &&
        (s->adc_regs[REG(0x0c)] & (3U << 10)) == (1U << 10) &&
        s->tim1.reg[REG(0x34)] <= s->tim1.period) {
        s->adc_phase = 3;
        s->adc_index = 0;
        uint64_t compare_ns = muldiv64(s->tim1.reg[REG(0x34)], 1000000000, s->tim1.hz);
        timer_mod_ns(s->adc_event, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + compare_ns + 10813);
    }
}

static void adc_event(void *opaque)
{
    G0Board *s = opaque;
    uint32_t *ch = &s->dma_regs[REG(0x44)]; /* DMA1 channel 4 */
    if (s->adc_phase == 1) {
        s->adc_cr &= ~(1U << 31);
    } else if (s->adc_phase == 2) {
        s->adc_regs[0] |= 1;
    } else if (s->adc_phase == 3) {
        uint32_t seq = s->adc_regs[REG(0x28)] >> (s->adc_index * 4);
        /* Bench pin voltages. Channel numbers and order come from CHSELR. */
        const uint16_t adc_codes[16] = {[4] = 2048, [5] = 1490, [6] = 2048,
                                        [12] = 1000, [13] = 1500};
        uint16_t sample = adc_codes[seq & 15];
        if (s->bench && (seq & 15) == 4) {
            /* At 3 V reference and the board's 4 centiamp/mV transfer. */
            sample += MIN(2000.0, MAX(0.0, s->bus_current) * (100.0 / 4.0) * 4096.0 / 3000.0);
        }
        s->adc_regs[REG(0x40)] = sample;
        if ((ch[0] & 1) && ch[1] && ch[2] == 0x40012440 &&
            s->mux_channels[3] == 5 && (s->adc_regs[REG(0x0c)] & 1)) {
            uint8_t bytes[2] = { sample & 255, sample >> 8 };
            cpu_physical_memory_write(ch[3] + (ch[0] & (1 << 7) ? s->adc_index * 2 : 0),
                                      bytes, sizeof(bytes));
            if (!--ch[1]) {
                s->dma_regs[0] |= 3 << 12;
                if (ch[0] & 2) {
                    qemu_irq_raise(s->dma_irq);
                }
            }
        }
        if ((seq >> 4 & 15) != 15 && ++s->adc_index < 8) {
            timer_mod_ns(s->adc_event, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 10813);
            return;
        }
        s->adc_cr &= ~4U;
        s->adc_regs[0] |= 8;
        if (!s->adc_scans++) {
            qemu_log_mask(CPU_LOG_INT | CPU_LOG_PAGE, "ESCAPE1 ADC sequence=0x%x dma_remaining=%u\n",
                          s->adc_regs[REG(0x28)], ch[1]);
        }
    }
    s->adc_phase = 0;
}

static void led_dma(void *opaque)
{
    G0Board *s = opaque;
    uint32_t *ch = &s->dma_regs[REG(0x6c)];
    if ((ch[0] & 0x11) != 0x11 || !ch[1] || ch[2] != 0x40014434 ||
        !(s->tim16.reg[REG(0x0c)] & (1 << 9))) {
        return;
    }
    uint8_t data[2];
    cpu_physical_memory_read(ch[3] + (5 - ch[1]) * 2, data, 2);
    s->tim16.reg[REG(0x34)] = data[0] | data[1] << 8;
    if (!--ch[1]) {
        s->dma_regs[0] |= 3 << 20;
        if (ch[0] & 2) {
            qemu_irq_raise(s->dma_irq);
        }
    }
}

static void tx_dma(void *opaque)
{
    G0Board *s = opaque;
    uint32_t *ch = &s->dma_regs[REG(0x30)];
    uint32_t isr = 0, cr3 = 0, brr = 0;
    cpu_physical_memory_read(0x4001381c, &isr, 4);
    cpu_physical_memory_read(0x40013808, &cr3, 4);
    cpu_physical_memory_read(0x4001380c, &brr, 4);
    if (!(ch[0] & 1) || !ch[1] || ch[2] != 0x40013828 || !(cr3 & 128) || !brr) {
        return;
    }
    if (isr & 128) {
        uint8_t byte;
        cpu_physical_memory_read(ch[3] + ((ch[0] & 128) ? s->tx_index : 0), &byte, 1);
        uint32_t tdr = byte;
        cpu_physical_memory_write(ch[2], &tdr, sizeof(tdr));
        s->tx_index++;
        if (!--ch[1]) {
            s->tx_packets++;
            s->dma_regs[0] |= 3 << 8;
            if (ch[0] & 2) {
                qemu_irq_raise(s->tx_dma_irq);
            }
            return;
        }
    }
    timer_mod_ns(s->tx_event, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + muldiv64(brr * 10, 1000000000, s->tim1.hz));
}

static void watchdog_expired(void *opaque)
{
    qemu_log_mask(CPU_LOG_INT | CPU_LOG_PAGE, "ESCAPE1 IWDG expired\n");
    qemu_system_reset_request(SHUTDOWN_CAUSE_GUEST_RESET);
}

static void capture_edge(G0Timer *t, unsigned channel, bool reset)
{
    if (!t->enabled || !(t->reg[0] & 1)) {
        return;
    }
    if (t->reg[REG(0x20)] & (1 << (4 * channel))) {
        t->reg[REG(0x34) + channel] = timer_read(t, 0x24, 4);
        t->reg[REG(0x10)] |= 2 << channel;
    }
    if (reset && (t->reg[REG(8)] & 7) == 4) {
        t->epoch = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
        timer_arm(t);
    }
    qemu_set_irq(t->irq, !!(t->reg[REG(0x0c)] & t->reg[REG(0x10)] & 63));
}

/* Averaged terminal dynamics of maxon EC-max 16 283828. Terminal R,L,Kt,
 * speed constant, inertia, no-load speed/current and pole count are published.
 * The Hall fixture has 120-degree sensors. Dynamics derive from phase EMF
 * and terminal current; commanded speed and firmware variables are unused. */
static void plant_step(void *opaque)
{
    G0Board *s = opaque;
    const double dt = 0.00005, resistance = 15.7, inductance = 0.000254;
    const double ke = 60.0 / (2 * M_PI * 1220.0), kt = 0.0078;
    const double inertia = 0.428e-7;
    const double drag = kt * 0.0602 / (13500 * 2 * M_PI / 60);
    G0Timer *pwm = &s->tim1;
    unsigned enable = pwm->reg[REG(0x20)];
    int positive = -1, negative = -1;
    for (int i = 0; i < 3; i++) {
        unsigned mode = (pwm->reg[REG(0x18) + i / 2] >> (4 + 8 * (i % 2))) & 7;
        unsigned gate = (enable >> (4 * i)) & 7;
        if (mode == 6 && (gate & 1)) { positive = i; }
        if (mode == 5 && (gate & 4)) { negative = i; }
    }
    bool driven = positive >= 0 && negative >= 0 && (pwm->reg[REG(0x44)] & 0x8000)
                  && (pwm->reg[0] & 1);
    s->drive_duty = driven ? MIN(1., pwm->reg[REG(0x34) + positive] / (double)(pwm->period + 1)) : 0;
    int pair = driven ? 10 * (positive + 1) + negative + 1 : 0;
    if (pair != s->drive_pair) {
        s->commutations++;
    }
    bool brake_pattern = (enable & 0x777) == 0x444 &&
                         (pwm->reg[REG(0x18)] & 0x7070) == 0x6060 &&
                         (pwm->reg[REG(0x1c)] & 0x70) == 0x60 &&
                         (pwm->reg[REG(0x44)] & 0x8000);
    s->brake_duty = brake_pattern ? MIN(1., pwm->reg[REG(0x34)] / (double)(pwm->period + 1)) : 0;
    if (s->frame >= 200 && pair != s->drive_pair) {
        qemu_log_mask(CPU_LOG_PAGE, "ESCAPE1 PHASE frame=%u hall=%u m1=%x m2=%x er=%x bdtr=%x\n",
                      s->frame, s->hall_code, pwm->reg[REG(0x18)], pwm->reg[REG(0x1c)],
                      enable, pwm->reg[REG(0x44)]);
    }
    s->drive_pair = pair;
    double phi = s->angle - M_PI / 3, torque = 0;
    if (driven) {
        double factor = (cos(phi - positive * 2*M_PI/3) - cos(phi - negative * 2*M_PI/3))
                      / (sqrt(3) * cos(M_PI/6) * (3/M_PI));
        double target = (12.0 * s->drive_duty - ke * factor * s->speed) / resistance;
        double decay = exp(-dt * resistance / inductance);
        double mean_i = target + (s->current - target) * (1 - decay) * inductance / (resistance * dt);
        s->current = target + (s->current - target) * decay;
        torque = kt * factor * mean_i;
        s->bus_current = mean_i * s->drive_duty;
    } else {
        s->current = 0;
        s->bus_current = 0;
        torque = -s->brake_duty * kt * ke * s->speed / resistance;
    }
    double load = s->frame >= 250 && s->frame < 300 ? 0.000004 : 0;
    s->speed += (torque - (drag + load) * s->speed) * dt / inertia;
    s->angle = fmod(s->angle + s->speed * dt + 2*M_PI, 2*M_PI);
    static const unsigned codes[] = { 1, 3, 2, 6, 4, 5 };
    unsigned sector = (unsigned)floor((s->angle + M_PI/6) / (M_PI/3)) % 6;
    unsigned code = codes[sector];
    if (!s->hall_code || code != s->hall_code) {
        for (unsigned i = 0; i < 3; i++) {
            qemu_set_irq(s->hall_pin[i], !!(code & (1U << i)));
        }
        if (s->hall_code) {
            capture_edge(&s->tim3, 0, true);
        }
        s->hall_code = code;
    }
    timer_mod_ns(s->plant_timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 50000);
}

static void servo_edge(void *opaque)
{
    G0Board *s = opaque;
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    s->high = !s->high;
    qemu_set_irq(s->pwm_pin, s->high);
    if (s->high) {
        s->frame++;

        /* 50 Hz calibrated servo. Enter reverse once to brake, release to
         * neutral, then command reverse again per ESCape32's RC car mode. */
        if (s->frame < 200) {
            s->pulse_us = 1500;
        } else if (s->frame < 210) {
            s->pulse_us = 1550 + (s->frame - 200) * 9;
        } else if (s->frame < 300) {
            s->pulse_us = 1640;
        } else if (s->frame < 320 || (s->frame >= 340 && s->frame < 395)) {
            s->pulse_us = 1390;
        } else {
            s->pulse_us = 1500;
        }
        capture_edge(&s->tim15, 0, true);
    } else if ((s->tim15.reg[REG(0x20)] & 0x30) == 0x30) {
        capture_edge(&s->tim15, 1, false);
    }
    if (s->high) {
        qemu_log_mask(CPU_LOG_INT | CPU_LOG_PAGE, "ESCAPE1 BENCH frame=%u pulse=%u m1=%x m2=%x er=%x "
                      "cc=%u period=%u adc=%u pair=%d duty=%.5f brake=%.5f rpm=%.2f current=%.4f hall=%u comm=%u packets=%u\n", s->frame, s->pulse_us,
                      s->tim1.reg[REG(0x18)], s->tim1.reg[REG(0x1c)],
                      s->tim1.reg[REG(0x20)], s->tim1.reg[REG(0x34)],
                      s->tim1.period, s->adc_scans, s->drive_pair, s->drive_duty, s->brake_duty,
                      s->speed * 60.0 / (2 * M_PI), s->bus_current, s->hall_code, s->commutations, s->tx_packets);
    }
    if (s->frame == 430) {
        qemu_system_shutdown_request(SHUTDOWN_CAUSE_GUEST_SHUTDOWN);
        return;
    }
    timer_mod_ns(s->servo, now + (s->high ? s->pulse_us : 20000 - s->pulse_us) * 1000);
}

static void escape1_init(MachineState *ms)
{
    G0Board *s = g_new0(G0Board, 1);
    Object *owner = OBJECT(ms);
    DeviceState *cpu = qdev_new(TYPE_ARMV7M);
    DeviceState *usart = qdev_new(TYPE_STM32L4X5_USART);
    s->cpu = clock_new(owner, "sysclk");
    s->ref = clock_new(owner, "reference");
    s->usart1 = clock_new(owner, "usart1clk");
    s->usart = clock_new(owner, "usart2clk");
    clock_set_hz(s->cpu, HSI);
    clock_set_hz(s->ref, HSI / 8);
    memory_region_init_rom(&s->flash, OBJECT(cpu), "G071.flash", 128 * KiB, &error_fatal);
    memory_region_init_alias(&s->alias, owner, "G071.boot", &s->flash, 0, 128 * KiB);
    memory_region_add_subregion(get_system_memory(), 0x08000000, &s->flash);
    memory_region_add_subregion(get_system_memory(), 0, &s->alias);
    memory_region_add_subregion(get_system_memory(), 0x20000000, ms->ram);
    object_property_add_child(owner, "armv6m", OBJECT(cpu));
    qdev_prop_set_string(cpu, "cpu-type", ARM_CPU_TYPE_NAME("cortex-m0"));
    qdev_prop_set_uint32(cpu, "num-irq", 32);
    qdev_prop_set_uint8(cpu, "num-prio-bits", 2);
    object_property_set_link(OBJECT(cpu), "memory", OBJECT(get_system_memory()), &error_fatal);
    qdev_connect_clock_in(cpu, "cpuclk", s->cpu);
    qdev_connect_clock_in(cpu, "refclk", s->ref);
    sysbus_realize_and_unref(SYS_BUS_DEVICE(cpu), &error_fatal);
    timer_init_region(&s->tim1, owner, "G071.TIM1", 0x40012c00, qdev_get_gpio_in(cpu, 13));
    timer_init_region(&s->tim6, owner, "G071.TIM6", 0x40001000, qdev_get_gpio_in(cpu, 17));
    memory_region_init_io(&s->syscfg, owner, &syscfg_ops, s, "G071.syscfg", 4);
    memory_region_add_subregion(get_system_memory(), 0x40010000, &s->syscfg);
    memory_region_init_io(&s->mux, owner, &mux_ops, s, "G071.mux", 28);
    memory_region_add_subregion(get_system_memory(), 0x40020800, &s->mux);
    memory_region_init_io(&s->dma, owner, &dma_ops, s, "G071.DMA1", sizeof(s->dma_regs));
    memory_region_add_subregion(get_system_memory(), 0x40020000, &s->dma);
    memory_region_init_io(&s->comp, owner, &comp_ops, s, "G071.COMP", 8);
    memory_region_add_subregion(get_system_memory(), 0x40010200, &s->comp);
    memory_region_init_io(&s->adc, owner, &adc_ops, s, "G071.ADC1", sizeof(s->adc_regs));
    memory_region_add_subregion(get_system_memory(), 0x40012400, &s->adc);
    memory_region_init_io(&s->adc_common, owner, &adc_common_ops, s, "G071.adc_common", 4);
    memory_region_add_subregion(get_system_memory(), 0x40012708, &s->adc_common);
    timer_init_region(&s->tim2, owner, "G071.TIM2", 0x40000000, qdev_get_gpio_in(cpu, 15));
    timer_init_region(&s->tim3, owner, "G071.TIM3", 0x40000400, qdev_get_gpio_in(cpu, 16));
    timer_init_region(&s->tim15, owner, "G071.TIM15", 0x40014000, qdev_get_gpio_in(cpu, 20));
    timer_init_region(&s->tim16, owner, "G071.TIM16", 0x40014400, qdev_get_gpio_in(cpu, 21));
    memory_region_init_io(&s->wwdg, owner, &wwdg_ops, s, "G071.WWDG", 4);
    memory_region_add_subregion(get_system_memory(), 0x40002c00, &s->wwdg);
    memory_region_init_io(&s->iwdg, owner, &iwdg_ops, s, "G071.IWDG", 4);
    memory_region_add_subregion(get_system_memory(), 0x40003000, &s->iwdg);
    s->watchdog = timer_new_ns(QEMU_CLOCK_VIRTUAL, watchdog_expired, s);
    s->dma_irq = qdev_get_gpio_in(cpu, 11);
    s->tx_dma_irq = qdev_get_gpio_in(cpu, 10);
    s->tx_event = timer_new_ns(QEMU_CLOCK_VIRTUAL, tx_dma, s);
    s->adc_event = timer_new_ns(QEMU_CLOCK_VIRTUAL, adc_event, s);
    s->tim1.on_update = adc_trigger;
    s->tim1.update_ctx = s;
    s->tim16.on_update = led_dma;
    s->tim16.update_ctx = s;
    s->factory_words[0] = 1000;
    s->factory_words[1] = 1500;
    s->factory_words[0x22 / 2] = 1500;
    memory_region_init_io(&s->factory, owner, &factory_ops, s, "G071.factory", sizeof(s->factory_words));
    memory_region_add_subregion(get_system_memory(), 0x1fff75a8, &s->factory);
    s->regs[0] = (1U << 8) | (1U << 10);
    s->regs[REG(0x60)] = 1U << 27; /* POR reset */
    s->lock = timer_new_ns(QEMU_CLOCK_VIRTUAL, pll_locked, s);
    memory_region_init_io(&s->rcc, owner, &rcc_ops, s, "G071.RCC", sizeof(s->regs));
    memory_region_add_subregion(get_system_memory(), 0x40021000, &s->rcc);
    memory_region_init_io(&s->crc, owner, &crc_ops, s, "G071.CRC", 0x0c);
    memory_region_add_subregion(get_system_memory(), 0x40023000, &s->crc);
    memory_region_init_io(&s->flash_acr, owner, &acr_ops, s, "G071.FLASH_ACR", 4);
    memory_region_add_subregion(get_system_memory(), 0x40022000, &s->flash_acr);
    memory_region_init_io(&s->dbg, owner, &id_ops, s, "G071.IDCODE", 4);
    memory_region_add_subregion(get_system_memory(), 0x40015800, &s->dbg);
    for (unsigned i = 0; i < ARRAY_SIZE(s->gpio); i++) {
        if (i == 4) {
            continue; /* port E absent */
        }
        DeviceState *gpio = qdev_new(TYPE_STM32L4X5_GPIO);
        g_autofree char *name = g_strdup_printf("gpio%c", 'A' + i);
        s->gpio[i] = clock_new(owner, name);
        qdev_prop_set_string(gpio, "name", name);
        qdev_prop_set_uint32(gpio, "mode-reset", i == 0 ? 0xebffffff : 0xffffffff);
        qdev_prop_set_uint32(gpio, "pupd-reset", i == 0 ? 0x24000000 : 0);
        qdev_connect_clock_in(gpio, "clk", s->gpio[i]);
        sysbus_realize_and_unref(SYS_BUS_DEVICE(gpio), &error_fatal);
        if (i == 0) {
            s->pwm_pin = qdev_get_gpio_in(gpio, 2);
            s->hall_pin[0] = qdev_get_gpio_in(gpio, 15);
        } else if (i == 1) {
            s->hall_pin[1] = qdev_get_gpio_in(gpio, 3);
            s->hall_pin[2] = qdev_get_gpio_in(gpio, 5);
        }
        sysbus_mmio_map(SYS_BUS_DEVICE(gpio), 0, 0x50000000 + i * 0x400);
    }
    qdev_prop_set_chr(usart, "chardev", serial_hd(0));
    qdev_connect_clock_in(usart, "clk", s->usart);
    sysbus_realize_and_unref(SYS_BUS_DEVICE(usart), &error_fatal);
    sysbus_mmio_map(SYS_BUS_DEVICE(usart), 0, 0x40004400);
    sysbus_connect_irq(SYS_BUS_DEVICE(usart), 0, qdev_get_gpio_in(cpu, 28));
    DeviceState *telemetry = qdev_new(TYPE_STM32L4X5_USART);
    qdev_prop_set_chr(telemetry, "chardev", serial_hd(1));
    qdev_connect_clock_in(telemetry, "clk", s->usart1);
    sysbus_realize_and_unref(SYS_BUS_DEVICE(telemetry), &error_fatal);
    sysbus_mmio_map(SYS_BUS_DEVICE(telemetry), 0, 0x40013800);
    sysbus_connect_irq(SYS_BUS_DEVICE(telemetry), 0, qdev_get_gpio_in(cpu, 27));
    if (g_strcmp0(g_getenv("ESCAPE1_BENCH"), "maxon-283828") == 0) {
        s->bench = true;
        s->plant_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, plant_step, s);
        timer_mod_ns(s->plant_timer, 1000000);
        s->servo = timer_new_ns(QEMU_CLOCK_VIRTUAL, servo_edge, s);
        timer_mod_ns(s->servo, 100000000);
        for (unsigned i = 0; i < 3; i++) {
            qemu_set_irq(s->hall_pin[i], i == 0);
        }
    }
    armv7m_load_kernel(ARMV7M(cpu)->cpu, ms->kernel_filename, 0x08000000, 128 * KiB);
}

static void escape1_class_init(MachineClass *mc)
{
    mc->desc = "ESCAPE1 STM32G071 with Hall-sensored RC test bench";
    mc->init = escape1_init;
    mc->default_ram_id = "G071.sram";
    mc->default_ram_size = 36 * KiB;
}

DEFINE_MACHINE("escape1-g071", escape1_class_init)
