/* SPDX-License-Identifier: GPL-2.0-or-later */
/* STM32F103 subset used by the BTT SKR Mini E3 v2 controller. */
#include "hw/irq.h"
#include "hw/char/stm32f2xx_usart.h"
#include "system/system.h"
#include "qemu/timer.h"

enum { TIM2, PWM_TIM5, TMC, I2C1, AFIO, EXTI, GPIOA, GPIOB, GPIOC, GPIOD,
       RCC, FLASHCTL, BANKS };
static const uint32_t address[BANKS] = {
    0x40000000, 0x40000c00, 0x40004800, 0x40005400,
    0x40010000, 0x40010400, 0x40010800, 0x40010c00, 0x40011000,
    0x40011400, 0x40021000, 0x40022000,
};
typedef struct {
    int id;
    MemoryRegion region;
    uint32_t r[256];
} BttBank;
static BttBank banks[BANKS];
static uint8_t eeprom[4096];
static uint16_t eeprom_addr;
static unsigned i2c_phase, i2c_addrbytes;
static uint16_t page_base;
static bool i2c_rx;
static FILE *pinlog, *io_log;
static uint32_t tmc_reads[4], tmc_writes[4], tmc_bad_crc;
static uint32_t ee_read, ee_write;
static QEMUTimer *step_clock, *tmc_clock;
static qemu_irq step_irq, tmc_irq;
static int64_t tick_origin;
static uint32_t active_psc, active_arr, next_flags;
static uint8_t packet[8], reply[8];
static unsigned npacket, nreply;
static uint32_t tmc_regs[4][128];

static int64_t btt_now(void)
{
    return qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
}

static void btt_tmc_irq(void)
{
    uint32_t *r = banks[TMC].r;
    qemu_set_irq(tmc_irq, !!(r[0] & r[3] & 0xe0));
}

static void btt_tmc_receive(void *opaque)
{
    uint32_t *r = banks[TMC].r;
    if (nreply && !(r[0] & 32)) {
        r[1] = reply[8 - nreply--];
        r[0] |= 32;
        btt_tmc_irq();
    }
    if (nreply) {
        timer_mod(tmc_clock, btt_now() + 86806); /* ten bits, 115200 baud */
    }
}

static uint8_t btt_crc(const uint8_t *p, unsigned n)
{
    uint8_t crc = 0;
    for (unsigned i = 0; i < n; i++) {
        for (unsigned bit = 0; bit < 8; bit++) {
            bool feedback = ((crc >> 7) ^ (p[i] >> bit)) & 1;
            crc = (crc << 1) ^ (feedback ? 7 : 0);
        }
    }
    return crc;
}

static void btt_tmc_send(uint8_t v)
{
    if (!npacket && v != 5) {
        return;
    }
    packet[npacket++] = v;
    if (npacket < 4 || ((packet[2] & 128) && npacket < 8)) {
        return;
    }
    unsigned n = npacket, addr = packet[1], reg = packet[2] & 127;
    npacket = 0;
    if (addr > 3 || btt_crc(packet, n - 1) != packet[n - 1]) {
        tmc_bad_crc++;
        return;
    }
    uint32_t *regs = tmc_regs[addr];
    if (n == 8) {
        tmc_writes[addr]++;
        regs[reg] = ldl_be_p(&packet[3]);
        regs[2] = (regs[2] + 1) & 255;
    } else {
        tmc_reads[addr]++;
        reply[0] = 5;
        reply[1] = 255;
        reply[2] = reg;
        stl_be_p(&reply[3], regs[reg]);
        reply[7] = btt_crc(reply, 7);
        nreply = 8;
        timer_mod(tmc_clock, btt_now() + 86806);
    }
}

static void btt_pin_change(int id, uint32_t old)
{
    uint32_t b = banks[GPIOB].r[3], c = banks[GPIOC].r[3];
    if (pinlog && (id == GPIOB || id == GPIOC) &&
        ((old ^ banks[id].r[3]) & (id == GPIOB ? 0x7c07 : 32))) {
        fprintf(pinlog, "%" PRId64 " %04x %04x\n", btt_now(), b, c);
    }
}

static uint32_t btt_cnt(void)
{
    uint32_t *r = banks[TIM2].r;
    if (!(r[0] & 1)) {
        return r[9];
    }
    uint64_t elapsed = btt_now() - tick_origin;
    uint64_t ticks = muldiv64(elapsed, 72000000,
                              1000000000ULL) / (active_psc + 1);
    return (r[9] - MIN(ticks, r[9])) & 65535;
}

static void btt_schedule(void)
{
    uint32_t *r = banks[TIM2].r;
    qemu_set_irq(step_irq, !!(r[3] & r[4] & 7));
    timer_del(step_clock);
    if (!(r[0] & 1)) {
        return;
    }
    uint32_t cnt = r[9], delta = cnt + 1;
    next_flags = 1;
    for (unsigned ch = 1; ch <= 2; ch++) {
        uint32_t match = r[12 + ch] & 65535;
        if ((r[3] & (1 << ch)) && match < btt_cnt()) {
            uint32_t d = cnt - match;
            if (d < delta) {
                delta = d;
                next_flags = 0;
            }
            if (d == delta) {
                next_flags |= 1 << ch;
            }
        }
    }
    uint64_t cycles = (uint64_t)delta * (active_psc + 1);
    timer_mod(step_clock, tick_origin + DIV_ROUND_UP(cycles * 1000000000ULL, 72000000));
}

static void btt_elapsed(void *opaque)
{
    uint32_t *r = banks[TIM2].r;
    r[4] |= next_flags;
    if (next_flags & 1) {
        active_psc = r[10] & 65535;
        active_arr = r[11] & 65535;
        r[9] = active_arr;
    } else {
        r[9] = r[next_flags & 2 ? 13 : 14] & 65535;
    }
    tick_origin = btt_now();
    btt_schedule();
}

static uint64_t btt_read(void *opaque, hwaddr offset, unsigned size)
{
    BttBank *b = opaque;
    unsigned i = offset / 4;
    uint32_t *r = b->r, value = r[i];
    if (b->id >= GPIOA && b->id <= GPIOD && i == 2) {
        value = r[3];
        /* Closed, normally closed limit/control contacts to ground. */
        if (b->id == GPIOC) {
            value &= ~0xf007u;
        }
    } else if (b->id == TIM2 && i == 9) {
        value = btt_cnt();
    } else if (b->id == I2C1 && i == 4 && i2c_rx) {
        value = eeprom[eeprom_addr++ & 4095];
        ee_read++;
    } else if (b->id == I2C1 && i == 6) {
        r[5] &= ~2u;
    } else if (b->id == TMC && i == 1) {
        r[0] &= ~32u;
        btt_tmc_irq();
    }
    return value >> (8 * (offset & 3));
}

static void btt_write(void *opaque, hwaddr offset, uint64_t val, unsigned size)
{
    BttBank *b = opaque;
    unsigned i = offset / 4, shift = 8 * (offset & 3);
    uint32_t *r = b->r, old = r[i];
    uint32_t mask = (UINT32_MAX >> (32 - size * 8)) << shift;
    uint32_t v = (old & ~mask) | ((uint32_t)val << shift);
    if (b->id == TIM2 && (i == 0 || i == 5 || i == 9)) {
        r[9] = btt_cnt();
        tick_origin = btt_now();
    }
    r[i] = v;
    if (b->id == RCC && i == 0) {
        const uint32_t enables = (1u << 24) | (1u << 16) | 1;
        r[i] = (v & ~(enables << 1)) | ((v & enables) << 1);
    } else if (b->id == RCC && i == 1) {
        r[i] = (v & ~12u) | ((v & 3) << 2);
    } else if (b->id >= GPIOA && b->id <= GPIOD && i >= 3 && i <= 5) {
        uint32_t old_odr = i == 3 ? old : r[3];
        if (i == 4) {
            r[3] = (old_odr & ~(v >> 16)) | (v & 65535);
        } else if (i == 5) {
            r[3] = old_odr & ~v;
        }
        r[3] &= 65535;
        btt_pin_change(b->id, old_odr);
    } else if (b->id == I2C1 && i == 0) {
        if (v & 256) {
            r[0] &= ~256u;
            r[5] = 1;
            r[6] = 3;
            i2c_phase = 1;
        }
        if (v & 512) {
            r[0] &= ~512u;
            r[6] = 0;
            i2c_phase = 0;
        }
    } else if (b->id == I2C1 && i == 4) {
        if (i2c_phase == 1) {
            i2c_rx = v & 1;
            if ((v >> 1) == 0x50) {
                r[5] = 2 | 4 | (i2c_rx ? 64 : 128);
                r[6] = 3 | (i2c_rx ? 0 : 4);
                i2c_phase = 2;
                i2c_addrbytes = 0;
            } else {
                r[5] = 1024;
            }
        } else if (i2c_phase == 2 && !i2c_rx) {
            if (i2c_addrbytes < 2) {
                eeprom_addr = ((eeprom_addr << 8) | (v & 255)) & 4095;
                i2c_addrbytes++;
                page_base = eeprom_addr & ~31u;
            } else {
                eeprom[eeprom_addr] = v;
                ee_write++;
                eeprom_addr = page_base | ((eeprom_addr + 1) & 31);
            }
            r[5] = 4 | 128;
        }
    } else if (b->id == TMC) {
        if (i == 1) {
            btt_tmc_send(v);
            r[0] |= 192;
        } else if (i == 0) {
            r[0] = (v & old) | 128;
        }
        btt_tmc_irq();
    } else if (b->id == TIM2) {
        if (i == 4) {
            r[4] = old & v;
        } else if (i == 5 && (v & 1)) {
            active_psc = r[10] & 65535;
            active_arr = r[11] & 65535;
            r[9] = active_arr;
            r[4] |= 1;
        }
        btt_schedule();
    }
}

static const MemoryRegionOps btt_ops = {
    .read = btt_read, .write = btt_write, .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1, .valid.max_access_size = 4,
};

static void btt_io_receipt(void)
{
    fprintf(io_log, "%u %u %u\n", ee_read, ee_write, tmc_bad_crc);
    for (unsigned a = 0; a < 4; a++) {
        fprintf(io_log, "%u %u %u\n", a, tmc_reads[a], tmc_writes[a]);
    }
    fclose(io_log);
}

static void btt_init(DeviceState *soc)
{
    DeviceState *arm = DEVICE(&CORTEXM_SOC(soc)->armv7m);
    DeviceState *uart = qdev_new(TYPE_STM32F2XX_USART);
    qdev_prop_set_uint32(uart, "addr", 0x40013800);
    qdev_prop_set_chr(uart, "chardev", serial_hd(0));
    sysbus_realize_and_unref(SYS_BUS_DEVICE(uart), &error_fatal);
    sysbus_connect_irq(SYS_BUS_DEVICE(uart), 0, qdev_get_gpio_in(arm, 37));

    step_irq = qdev_get_gpio_in(arm, 28);
    tmc_irq = qdev_get_gpio_in(arm, 39);
    step_clock = timer_new_ns(QEMU_CLOCK_VIRTUAL, btt_elapsed, NULL);
    tmc_clock = timer_new_ns(QEMU_CLOCK_VIRTUAL, btt_tmc_receive, NULL);
    memset(eeprom, 0xff, sizeof(eeprom));
    for (int i = 0; i < 4; i++) {
        tmc_regs[i][6] = 0x21000000;
    }
    banks[TMC].r[0] = 192;
    banks[RCC].r[0] = 0x83;
    active_arr = banks[TIM2].r[11] = 65535;
    if (g_getenv("BTT_IO_LOG")) {
        io_log = fopen(g_getenv("BTT_IO_LOG"), "wx");
        if (!io_log) {
            error_report("cannot create BTT_IO_LOG");
            exit(1);
        }
        atexit(btt_io_receipt);
    }
    if (g_getenv("BTT_PIN_LOG")) {
        pinlog = fopen(g_getenv("BTT_PIN_LOG"), "wx");
        if (!pinlog) {
            error_report("cannot create BTT_PIN_LOG");
            exit(1);
        }
    }
    for (int i = 0; i < BANKS; i++) {
        banks[i].id = i;
        memory_region_init_io(&banks[i].region, OBJECT(soc), &btt_ops, &banks[i],
                              "btt-peripheral", 0x400);
        memory_region_add_subregion(get_system_memory(), address[i], &banks[i].region);
    }
}
