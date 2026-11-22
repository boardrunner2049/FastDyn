/* SPDX-License-Identifier: GPL-2.0-or-later */
/* ESCAPE1 boot/maintenance slice. Register definitions follow STM32 RM0444. */
#include "qemu/osdep.h"
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

#define HSI 16000000U
#define PLL_ON (1U << 24)
#define PLL_READY (1U << 25)
#define REG(off) ((off) / 4)

typedef struct {
    MemoryRegion io;
    QEMUTimer *alarm;
    uint32_t reg[0x80 / 4];
    uint32_t divisor, period, hz;
    int64_t epoch;
    bool enabled;
    qemu_irq irq;
} G0Timer;

typedef struct {
    Clock *cpu, *ref, *usart, *gpio[6];
    MemoryRegion flash, alias, rcc, crc, dbg, flash_acr;
    uint32_t acr;
    uint32_t regs[0x64 / 4];
    uint32_t crc_reg, crc_ctl;
    QEMUTimer *lock;
    G0Timer tim1, tim6;
    MemoryRegion syscfg, mux, tim2_setup, adc, adc_common;
    uint32_t cfgr1, mux_channels[4], tim2_regs[9];
    uint32_t adc_cr, adc_cfgr2, adc_ccr;
    int64_t regulator_on_ns;
} G0Board;

static void timer_arm(G0Timer *t)
{
    if (t->enabled && t->hz && (t->reg[0] & 1) && t->period) {
        uint64_t ticks = ((uint64_t)t->period + 1) * t->divisor;
        timer_mod_ns(t->alarm, t->epoch + DIV_ROUND_UP(ticks * 1000000000ULL, t->hz));
    } else {
        timer_del(t->alarm);
    }
}

static void timer_update(void *opaque)
{
    G0Timer *t = opaque;
    t->reg[REG(0x10)] |= 1;
    t->divisor = (t->reg[REG(0x28)] & 0xffff) + 1;
    t->period = t->reg[REG(0x2c)] & 0xffff;
    if (t->reg[0] & 8) {
        t->reg[0] &= ~1U; /* OPM stops after update. */
    }
    t->epoch = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    qemu_set_irq(t->irq, t->reg[REG(0x0c)] & t->reg[REG(0x10)] & 1);
    timer_arm(t);
}

static uint64_t timer_read(void *opaque, hwaddr a, unsigned size)
{
    G0Timer *t = opaque;
    if (!t->enabled) {
        return 0;
    }
    if (a == 0x24 && (t->reg[0] & 1)) {
        uint64_t ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) - t->epoch;
        return muldiv64(ns, t->hz, 1000000000U) / t->divisor % (t->period + 1);
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
        if (v & 1) {
            t->divisor = (t->reg[REG(0x28)] & 0xffff) + 1;
            t->period = t->reg[REG(0x2c)] & 0xffff;
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
        t->reg[REG(a)] = v & 0xffff;
        if (!(t->reg[0] & (1 << 7))) {
            t->period = v & 0xffff;
        }
        break;
    default:
        t->reg[REG(a)] = v;
        break;
    }
    qemu_set_irq(t->irq, t->reg[REG(0x0c)] & t->reg[REG(0x10)] & 1);
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
    t->reg[REG(0x2c)] = t->period = 0xffff;
    t->alarm = timer_new_ns(QEMU_CLOCK_VIRTUAL, timer_update, t);
    memory_region_init_io(&t->io, owner, &tim_ops, t, name, sizeof(t->reg));
    memory_region_add_subregion(get_system_memory(), base, &t->io);
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
    s->tim1.hz = s->tim6.hz = freq;
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

/* The application startup proof ends at ADCAL. It does not need synthesized
 * conversion results, channel interrupts or fabricated sensor input. */
static uint64_t setup_read(void *opaque, hwaddr a, unsigned size)
{
    G0Board *s = opaque;
    if (a == 0x10000 && (s->regs[REG(0x40)] & 1)) {
        return s->cfgr1;
    }
    if (a >= 0x20800 && a < 0x20810 && (s->regs[REG(0x38)] & 1)) {
        return s->mux_channels[REG(a - 0x20800)];
    }
    if (a < sizeof(s->tim2_regs) && (s->regs[REG(0x3c)] & 1)) {
        return s->tim2_regs[REG(a)];
    }
    if (s->regs[REG(0x40)] & (1 << 20)) {
        switch (a) {
        case 0x12408: return s->adc_cr;
        case 0x12410: return s->adc_cfgr2;
        case 0x12708: return s->adc_ccr;
        }
    }
    return 0;
}

static void setup_write(void *opaque, hwaddr a, uint64_t value, unsigned size)
{
    G0Board *s = opaque;
    if (a == 0x10000 && (s->regs[REG(0x40)] & 1)) {
        s->cfgr1 = value;
    } else if (a >= 0x20800 && a < 0x20810 && (s->regs[REG(0x38)] & 1)) {
        s->mux_channels[REG(a - 0x20800)] = value & 0x7f;
    } else if (a < sizeof(s->tim2_regs) && (s->regs[REG(0x3c)] & 1)) {
        s->tim2_regs[REG(a)] = value;
    } else if (s->regs[REG(0x40)] & (1 << 20)) {
        switch (a) {
        case 0x12410: s->adc_cfgr2 = value; break;
        case 0x12708: s->adc_ccr = value; break;
        case 0x12408:
            if (!(s->adc_cr & (1U << 28)) && (value & (1U << 28))) {
                s->regulator_on_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
            }
            if (value & (1U << 31)) {
                qemu_log_mask(CPU_LOG_INT,
                    "ESCAPE1 ADCAL sysclk=%u tim6_psc=%u tim6_arr=%u "
                    "tim6_cen=%u tim6_uif=%u regulator_ns=%" PRId64
                    " adc_cfgr2=0x%x adc_ccr=0x%x mux=%u,%u,%u,%u\n",
                    s->tim6.hz, s->tim6.divisor - 1, s->tim6.period,
                    s->tim6.reg[0] & 1, s->tim6.reg[REG(0x10)] & 1,
                    qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) - s->regulator_on_ns,
                    s->adc_cfgr2, s->adc_ccr, s->mux_channels[0],
                    s->mux_channels[1], s->mux_channels[2], s->mux_channels[3]);
            }
            s->adc_cr = value;
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
SETUP_OPS(tim2_setup, 0);
SETUP_OPS(adc, 0x12408);
SETUP_OPS(adc_common, 0x12708);

static void escape1_init(MachineState *ms)
{
    G0Board *s = g_new0(G0Board, 1);
    Object *owner = OBJECT(ms);
    DeviceState *cpu = qdev_new(TYPE_ARMV7M);
    DeviceState *usart = qdev_new(TYPE_STM32L4X5_USART);
    s->cpu = clock_new(owner, "sysclk");
    s->ref = clock_new(owner, "reference");
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
    memory_region_init_io(&s->mux, owner, &mux_ops, s, "G071.mux", 16);
    memory_region_add_subregion(get_system_memory(), 0x40020800, &s->mux);
    memory_region_init_io(&s->tim2_setup, owner, &tim2_setup_ops, s, "G071.tim2_setup", 36);
    memory_region_add_subregion(get_system_memory(), 0x40000000, &s->tim2_setup);
    memory_region_init_io(&s->adc, owner, &adc_ops, s, "G071.adc", 12);
    memory_region_add_subregion(get_system_memory(), 0x40012408, &s->adc);
    memory_region_init_io(&s->adc_common, owner, &adc_common_ops, s, "G071.adc_common", 4);
    memory_region_add_subregion(get_system_memory(), 0x40012708, &s->adc_common);
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
        sysbus_mmio_map(SYS_BUS_DEVICE(gpio), 0, 0x50000000 + i * 0x400);
    }
    qdev_prop_set_chr(usart, "chardev", serial_hd(0));
    qdev_connect_clock_in(usart, "clk", s->usart);
    sysbus_realize_and_unref(SYS_BUS_DEVICE(usart), &error_fatal);
    sysbus_mmio_map(SYS_BUS_DEVICE(usart), 0, 0x40004400);
    sysbus_connect_irq(SYS_BUS_DEVICE(usart), 0, qdev_get_gpio_in(cpu, 28));
    armv7m_load_kernel(ARMV7M(cpu)->cpu, ms->kernel_filename, 0x08000000, 128 * KiB);
}

static void escape1_class_init(MachineClass *mc)
{
    mc->desc = "ESCAPE1 STM32G071 boot and maintenance slice";
    mc->init = escape1_init;
    mc->default_ram_id = "G071.sram";
    mc->default_ram_size = 36 * KiB;
}

DEFINE_MACHINE("escape1-g071", escape1_class_init)
