#include <device.h>
#include <boardrunner/vio.h>

#include <stdint.h>
#include <stdbool.h>
#include <string.h>

// Device Model for SPI2
// Composite MMIO handling:
//   - SPI2  @ 0x40003800
//   - GPIOB @ 0x40010C00
//
// Rationale:
// The trace shows firmware using GPIOB PB12 as a software-managed chip select
// around SPI2 traffic. To model SPI2 correctly and make external SPI slaves
// reachable, this model implements the minimal GPIOB subset actually used.

#define SPI2_BASE           0x40003800ULL
#define GPIOB_BASE          0x40010C00ULL

/* SPI2 register offsets */
#define SPI_CR1_OFF         0x00
#define SPI_CR2_OFF         0x04
#define SPI_SR_OFF          0x08
#define SPI_DR_OFF          0x0C
#define SPI_CRCPR_OFF       0x10
#define SPI_RXCRCR_OFF      0x14
#define SPI_TXCRCR_OFF      0x18
#define SPI_I2SCFGR_OFF     0x1C
#define SPI_I2SPR_OFF       0x20

/* SPI bits */
#define SPI_CR1_SPE         (1u << 6)
#define SPI_CR1_DFF         (1u << 11)

#define SPI_SR_RXNE         (1u << 0)
#define SPI_SR_TXE          (1u << 1)

#define SPI_I2SCFGR_I2SMOD  (1u << 11)

/* GPIOB register offsets */
#define GPIO_CRL_OFF        0x00
#define GPIO_CRH_OFF        0x04
#define GPIO_IDR_OFF        0x08
#define GPIO_ODR_OFF        0x0C
#define GPIO_BSRR_OFF       0x10
#define GPIO_BRR_OFF        0x14
#define GPIO_LCKR_OFF       0x18

#define GPIO_PB12           (1u << 12)

typedef struct {
    /* SPI2 registers */
    uint16_t cr1;
    uint16_t cr2;
    uint16_t sr;
    uint16_t dr;
    uint16_t crcpr;
    uint16_t rxcrcr;
    uint16_t txcrcr;
    uint16_t i2scfgr;
    uint16_t i2spr;

    bool rxne;

    /* GPIOB minimal state */
    uint32_t gpio_crl;
    uint32_t gpio_crh;
    uint16_t gpio_idr;
    uint16_t gpio_odr;
    uint32_t gpio_lckr;

    /* SPI bus */
    SPIBus bus;
    int cs_level[NUM_CS_LINES];
    bool manual_cs_seen;
} SPI2State;

static SPI2State g_spi2;

static uint64_t size_mask(unsigned size)
{
    switch (size) {
    case 1: return 0xFFu;
    case 2: return 0xFFFFu;
    default: return 0xFFFFFFFFu;
    }
}

static void spi2_update_sr(SPI2State *s)
{
    uint16_t sr = 0;

    /* Keep model minimal and aligned with trace:
     * TXE is always ready; RXNE set only when unread received data exists.
     */
    sr |= SPI_SR_TXE;
    if (s->rxne) {
        sr |= SPI_SR_RXNE;
    }

    s->sr = sr;
}

static bool spi2_enabled(SPI2State *s)
{
    if ((s->cr1 & SPI_CR1_SPE) == 0) {
        return false;
    }
    if ((s->i2scfgr & SPI_I2SCFGR_I2SMOD) != 0) {
        return false;
    }
    return true;
}

static void spi2_set_cs_level(SPI2State *s, int cs_id, int level)
{
    if (cs_id < 0 || cs_id >= NUM_CS_LINES) {
        return;
    }
    if (s->cs_level[cs_id] == level) {
        return;
    }

    s->cs_level[cs_id] = level;
    api_spi_set_cs(&s->bus, cs_id, level);
}

static void spi2_sync_pb12_to_cs(SPI2State *s)
{
    int level = (s->gpio_odr & GPIO_PB12) ? 1 : 0; /* high=inactivate, low=activate */
    s->manual_cs_seen = true;
    spi2_set_cs_level(s, 0, level);
}

static uint16_t spi2_do_transfer(SPI2State *s, uint16_t tx_word)
{
    bool dff16 = (s->cr1 & SPI_CR1_DFF) != 0;
    uint16_t idle_rx = dff16 ? 0xFFFFu : 0x00FFu;
    uint32_t rx32;

    if (!spi2_enabled(s)) {
        return idle_rx;
    }

    if (s->manual_cs_seen) {
        /* Honor software-controlled CS via GPIOB PB12. */
        if (s->cs_level[0] != 0) {
            return idle_rx;
        }
        rx32 = api_spi_transfer(&s->bus, tx_word);
        return dff16 ? (uint16_t)(rx32 & 0xFFFFu) : (uint16_t)(rx32 & 0x00FFu);
    }

    /* Fallback: if GPIOB traffic is not routed here, pulse CS0 per word so the
     * attached slave remains reachable.
     */
    spi2_set_cs_level(s, 0, 0);
    rx32 = api_spi_transfer(&s->bus, tx_word);
    spi2_set_cs_level(s, 0, 1);

    return dff16 ? (uint16_t)(rx32 & 0xFFFFu) : (uint16_t)(rx32 & 0x00FFu);
}

static uint64_t spi2_read_reg(SPI2State *s, hwaddr offset, unsigned size)
{
    uint64_t val = 0;

    switch (offset) {
    case SPI_CR1_OFF:
        val = s->cr1;
        break;
    case SPI_CR2_OFF:
        val = s->cr2;
        break;
    case SPI_SR_OFF:
        spi2_update_sr(s);
        val = s->sr;
        break;
    case SPI_DR_OFF:
        val = s->dr;
        s->rxne = false;
        spi2_update_sr(s);
        break;
    case SPI_CRCPR_OFF:
        val = s->crcpr;
        break;
    case SPI_RXCRCR_OFF:
        val = s->rxcrcr;
        break;
    case SPI_TXCRCR_OFF:
        val = s->txcrcr;
        break;
    case SPI_I2SCFGR_OFF:
        val = s->i2scfgr;
        break;
    case SPI_I2SPR_OFF:
        val = s->i2spr;
        break;
    default:
        dev_debug("SPI2: unhandled SPI read");
        val = 0;
        break;
    }

    return val & size_mask(size);
}

static void spi2_write_reg(SPI2State *s, hwaddr offset, uint64_t value, unsigned size)
{
    uint16_t v16 = (uint16_t)(value & 0xFFFFu);
    (void)size;

    switch (offset) {
    case SPI_CR1_OFF:
        s->cr1 = v16;
        spi2_update_sr(s);
        break;

    case SPI_CR2_OFF:
        s->cr2 = v16;
        break;

    case SPI_DR_OFF: {
        bool dff16 = (s->cr1 & SPI_CR1_DFF) != 0;
        uint16_t tx_word = dff16 ? v16 : (uint16_t)(v16 & 0x00FFu);
        uint16_t rx_word = spi2_do_transfer(s, tx_word);

        s->dr = rx_word;
        s->rxcrcr = rx_word;
        s->txcrcr = tx_word;
        s->rxne = true;
        spi2_update_sr(s);
        break;
    }

    case SPI_CRCPR_OFF:
        s->crcpr = v16;
        break;

    case SPI_I2SCFGR_OFF:
        s->i2scfgr = v16;
        spi2_update_sr(s);
        break;

    case SPI_I2SPR_OFF:
        s->i2spr = v16;
        break;

    default:
        dev_debug("SPI2: unhandled SPI write");
        break;
    }
}

static uint64_t gpiob_read_reg(SPI2State *s, hwaddr offset, unsigned size)
{
    uint64_t val = 0;

    switch (offset) {
    case GPIO_CRL_OFF:
        val = s->gpio_crl;
        break;
    case GPIO_CRH_OFF:
        val = s->gpio_crh;
        break;
    case GPIO_IDR_OFF:
        val = s->gpio_idr;
        break;
    case GPIO_ODR_OFF:
        val = s->gpio_odr;
        break;
    case GPIO_LCKR_OFF:
        val = s->gpio_lckr;
        break;
    case GPIO_BSRR_OFF:
    case GPIO_BRR_OFF:
        val = 0;
        break;
    default:
        dev_debug("SPI2: unhandled GPIOB read");
        val = 0;
        break;
    }

    return val & size_mask(size);
}

static void gpiob_apply_outputs(SPI2State *s)
{
    /* Minimal line model: reflect outputs into IDR for readable pin state. */
    s->gpio_idr = s->gpio_odr;

    /* PB12 is software NSS / external chip select. */
    spi2_sync_pb12_to_cs(s);
}

static void gpiob_write_reg(SPI2State *s, hwaddr offset, uint64_t value, unsigned size)
{
    uint32_t v32 = (uint32_t)(value & size_mask(size));

    switch (offset) {
    case GPIO_CRL_OFF:
        s->gpio_crl = v32;
        break;

    case GPIO_CRH_OFF:
        s->gpio_crh = v32;
        break;

    case GPIO_ODR_OFF:
        s->gpio_odr = (uint16_t)(v32 & 0xFFFFu);
        gpiob_apply_outputs(s);
        break;

    case GPIO_BSRR_OFF: {
        uint16_t set_mask = (uint16_t)(v32 & 0xFFFFu);
        uint16_t clr_mask = (uint16_t)((v32 >> 16) & 0xFFFFu);
        s->gpio_odr |= set_mask;
        s->gpio_odr &= (uint16_t)~clr_mask;
        gpiob_apply_outputs(s);
        break;
    }

    case GPIO_BRR_OFF: {
        uint16_t clr_mask = (uint16_t)(v32 & 0xFFFFu);
        s->gpio_odr &= (uint16_t)~clr_mask;
        gpiob_apply_outputs(s);
        break;
    }

    case GPIO_LCKR_OFF:
        s->gpio_lckr = v32;
        break;

    default:
        dev_debug("SPI2: unhandled GPIOB write");
        break;
    }
}

uint64_t spi2_read(void *opaque, hwaddr addr, unsigned size)
{
    SPI2State *s = (SPI2State *)opaque;

    if (addr >= SPI2_BASE && addr < (SPI2_BASE + 0x400)) {
        hwaddr offset = addr - SPI2_BASE;
        return spi2_read_reg(s, offset, size);
    }

    if (addr >= GPIOB_BASE && addr < (GPIOB_BASE + 0x400)) {
        hwaddr offset = addr - GPIOB_BASE;
        return gpiob_read_reg(s, offset, size);
    }

    dev_debug("SPI2: read outside handled ranges");
    return 0;
}

void spi2_write(void *opaque, hwaddr addr, uint64_t value, unsigned size)
{
    SPI2State *s = (SPI2State *)opaque;

    if (addr >= SPI2_BASE && addr < (SPI2_BASE + 0x400)) {
        hwaddr offset = addr - SPI2_BASE;
        spi2_write_reg(s, offset, value, size);
        return;
    }

    if (addr >= GPIOB_BASE && addr < (GPIOB_BASE + 0x400)) {
        hwaddr offset = addr - GPIOB_BASE;
        gpiob_write_reg(s, offset, value, size);
        return;
    }

    dev_debug("SPI2: write outside handled ranges");
}

// MUST return &g_state — framework stores this and passes it as opaque to _read/_write
void* spi2_init(ConfigSection* model_info)
{
    int i;
    SPI2State *s = &g_spi2;

    memset(s, 0, sizeof(*s));

    /* SPI2 reset-ish state aligned with trace */
    s->cr1 = 0x0000;
    s->cr2 = 0x0000;
    s->crcpr = 0x0007;   /* common STM32 SPI reset value */
    s->i2scfgr = 0x0000;
    s->i2spr = 0x0002;   /* common STM32 I2SPR reset value */
    s->dr = 0x0000;
    s->rxcrcr = 0x0000;
    s->txcrcr = 0x0000;
    s->rxne = false;
    spi2_update_sr(s);   /* yields 0x2, matching observed idle SR */

    /* GPIOB reset-ish state aligned with trace */
    s->gpio_crl = 0x44444444u;
    s->gpio_crh = 0x44444444u;
    s->gpio_odr = 0x0000u;
    s->gpio_idr = 0x0000u;
    s->gpio_lckr = 0x00000000u;
    s->manual_cs_seen = false;

    s->bus = api_spi_init_bus(model_info);

    for (i = 0; i < NUM_CS_LINES; i++) {
        s->cs_level[i] = -1;
        spi2_set_cs_level(s, i, 1); /* inactive */
    }

    return s;
}