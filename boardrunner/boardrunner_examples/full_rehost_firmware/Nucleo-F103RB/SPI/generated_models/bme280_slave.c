// bme280_spi_slave.c
//
// Minimal stateful SPI slave model for a BME280-like sensor.
// Required exported callbacks:
//   - slave_spi_set_cs
//   - slave_spi_transfer
//
// Build:
//   gcc -shared -fPIC -O2 -o slave.so bme280_spi_slave.c

#include <stdint.h>
#include <stdbool.h>
#include <string.h>

#include <device.h>
#include <boardrunner/vio.h>

// ---- BME280 register definitions ----
#define REG_ID         0xD0
#define REG_RESET      0xE0
#define REG_CTRL_HUM   0xF2
#define REG_STATUS     0xF3
#define REG_CTRL_MEAS  0xF4
#define REG_CONFIG     0xF5
#define REG_DATA_START 0xF7

#define CHIP_ID_BME280 0x60
#define RESET_CMD      0xB6

typedef struct {
    uint8_t regs[256];

    bool    cs_active;
    bool    expect_cmd;
    bool    is_read;
    uint8_t reg_ptr;

    bool    inited;
} bme280_state_t;

static bme280_state_t g_state;

// Load power-on defaults and reset transaction state.
static void load_defaults(bme280_state_t *s)
{
    memset(s->regs, 0, sizeof(s->regs));

    // Identification
    s->regs[REG_ID] = CHIP_ID_BME280;

    // Control/config/status defaults
    s->regs[REG_CTRL_HUM]  = 0x00;
    s->regs[REG_CTRL_MEAS] = 0x00;
    s->regs[REG_CONFIG]    = 0x00;
    s->regs[REG_STATUS]    = 0x00;

    // Deterministic calibration contents
    for (int i = 0; i < 26; i++) {
        s->regs[0x88 + i] = (uint8_t)(0x20 + i);
    }
    for (int i = 0; i < 7; i++) {
        s->regs[0xE1 + i] = (uint8_t)(0x80 + i);
    }
    s->regs[0xA1] = 0x55;

    // Explicit value from reference model
    s->regs[0x88] = 0x93;

    // Stable dummy sensor output bytes: pressure(3), temperature(3), humidity(2)
    s->regs[0xF7] = 0x64;
    s->regs[0xF8] = 0x00;
    s->regs[0xF9] = 0x00;
    s->regs[0xFA] = 0x7A;
    s->regs[0xFB] = 0x00;
    s->regs[0xFC] = 0x00;
    s->regs[0xFD] = 0x40;
    s->regs[0xFE] = 0x00;

    // Transaction state
    s->cs_active  = false;
    s->expect_cmd = true;
    s->is_read    = false;
    s->reg_ptr    = 0x00;
}

static void lazy_init(void)
{
    if (!g_state.inited) {
        memset(&g_state, 0, sizeof(g_state));
        load_defaults(&g_state);
        g_state.inited = true;
        dev_debug("[bme280-spi] initialized\n");
    }
}

// Bosch-style SPI commands often encode write addresses with bit7 cleared,
// e.g. register 0xF4 may be sent as 0x74 for writes.
// This mapping reconstructs the 0x80-0xFF register space used by BME280.
static inline uint8_t map_cmd_to_reg(uint8_t cmd)
{
    if (cmd < 0x80) {
        return (uint8_t)((cmd & 0x7F) | 0x80);
    }
    return cmd;
}

// REQUIRED callback: level 0 = CS active, level 1 = CS inactive
void slave_spi_set_cs(int level)
{
    lazy_init();

    if (level == 0) {
        g_state.cs_active  = true;
        g_state.expect_cmd = true;
    } else {
        g_state.cs_active  = false;
        g_state.expect_cmd = true;
    }
}

// REQUIRED callback: full-duplex SPI transfer.
// Only the low 8 bits are used.
uint32_t slave_spi_transfer(uint32_t val)
{
    lazy_init();

    /* On real boards this path commonly reads back as 0xFF because MISO is
     * undriven/high when the device is not actively selected.
     */
    if (!g_state.cs_active) {
        return 0xFFu;
    }

    uint8_t mosi = (uint8_t)(val & 0xFFu);
    uint8_t miso = 0xFFu;

    if (g_state.expect_cmd) {
        g_state.is_read = ((mosi & 0x80u) != 0);
        g_state.reg_ptr = map_cmd_to_reg(mosi);
        g_state.expect_cmd = false;

        /* During the command/address byte, the returned value is don't-care.
         * The hardware trace shows 0xFF on these cycles.
         */
        miso = 0xFFu;
    } else {
        if (g_state.is_read) {
            uint8_t reg = g_state.reg_ptr;

            /* Keep STATUS deterministic in this simplified model. */
            if (reg == REG_STATUS) {
                miso = 0x00;
            } else {
                miso = g_state.regs[reg];
            }

            g_state.reg_ptr = (uint8_t)(g_state.reg_ptr + 1);
        } else {
            uint8_t reg = g_state.reg_ptr;
            g_state.regs[reg] = mosi;

            if (reg == REG_RESET && mosi == RESET_CMD) {
                bool was_inited = g_state.inited;
                load_defaults(&g_state);
                g_state.inited = was_inited;
                g_state.cs_active = true;
                g_state.expect_cmd = false;
            } else {
                g_state.reg_ptr = (uint8_t)(g_state.reg_ptr + 1);
            }

            /* Write-data phase is also don't-care on MISO; match observed 0xFF. */
            miso = 0xFFu;
        }
    }

    return (uint32_t)miso;
}

// Optional name-based aliases
void bme280_set_cs(int level)
{
    slave_spi_set_cs(level);
}

uint32_t bme280_transfer(uint32_t val)
{
    return slave_spi_transfer(val);
}